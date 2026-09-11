"""Launchd job adapter.

Read-only observation via ``/bin/launchctl print gui/<uid>/<label>`` (state, pid,
last exit code) and ``/bin/ps -o lstart= -p <pid>`` (process age, to recognise a
just-started process within the configured handoff window as an expected
transition rather than a failure). The only declared action is ``kickstart``,
which starts a new instance and is therefore refused by policy whenever an
instance is already present, a lock is held, or a handoff is under way.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta
from typing import Any

from keepalive.core.adapter import (
    ActionResult,
    ActionSpec,
    Adapter,
    AdapterManifest,
    CheckContext,
    CheckResult,
    CheckSpec,
    DataSource,
    NotifyCondition,
    Status,
)

LAUNCHCTL = "/bin/launchctl"
PS = "/bin/ps"
_SUBPROCESS_TIMEOUT_S = 5.0
_KICKSTART_TIMEOUT_S = 10.0
_LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"
_DEFAULT_HANDOFF_WINDOW_S = 90


class LaunchdAdapter(Adapter):
    """Reports OK/FAILED/EXPECTED_TRANSITION based on a launchd job's run state."""

    kind = "launchd"

    @classmethod
    def describe(cls) -> AdapterManifest:
        return AdapterManifest(
            kind=cls.kind,
            description=(
                "Watches a launchd job's run state via `launchctl print`, treating a "
                "freshly-started process within the handoff window as expected, not a failure."
            ),
            checks=(
                CheckSpec(
                    name="launchd_state",
                    description="launchctl print gui/<uid>/<label>; ps lstart for handoff age.",
                ),
            ),
            reads=(
                DataSource(kind="launchd", target="gui/<uid>/<label>"),
                DataSource(kind="command", target=f"{PS} -o lstart= -p <pid>"),
            ),
            actions=(
                ActionSpec(
                    name="kickstart",
                    description="launchctl kickstart -k gui/<uid>/<label>",
                    allowed_during_active_job=False,
                    starts_instance=True,
                    default_cooldown_s=600,
                    default_max_attempts=2,
                    destructive=False,
                ),
            ),
            notify_on=(
                NotifyCondition(
                    event="incident_opened", description="Job not running when expected"
                ),
                NotifyCondition(event="recovered", description="Job running again"),
                NotifyCondition(
                    event="circuit_open", description="Repeated kickstart attempts failed"
                ),
            ),
            treat_wake_as_transition=True,
            params_schema={
                "label": "launchd label; addressed as gui/<uid>/<label> (required)",
                "expect_running": "Whether the job is expected to be running (default true)",
                "handoff_window_s": (
                    "Age in seconds below which a freshly-started pid is EXPECTED_TRANSITION, "
                    "not a failure (default 90)"
                ),
            },
        )

    def validate_params(self) -> list[str]:
        problems = super().validate_params()
        label = self.params.get("label")
        if not label or not isinstance(label, str):
            problems.append("'label' is required and must be a non-empty string")
        return problems

    def check(self, ctx: CheckContext) -> CheckResult:
        label = str(self.params.get("label", ""))
        expect_running = bool(self.params.get("expect_running", True))
        handoff_window_s = int(self.params.get("handoff_window_s", _DEFAULT_HANDOFF_WINDOW_S))
        target = self._target(label)

        try:
            result = subprocess.run(
                [LAUNCHCTL, "print", target],
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return CheckResult(status=Status.UNKNOWN, detail=f"launchctl print failed: {exc}")

        if result.returncode != 0:
            if expect_running:
                return CheckResult(
                    status=Status.FAILED,
                    detail=f"{target} not loaded",
                    metrics={"returncode": result.returncode},
                )
            return CheckResult(status=Status.OK, detail=f"{target} not loaded (not expected to be)")

        info = self._parse_print(result.stdout)
        state = info.get("state", "")
        pid_str = info.get("pid")
        metrics: dict[str, Any] = {
            "state": state,
            "pid": pid_str,
            "last_exit_code": info.get("last exit code"),
        }

        if pid_str is None:
            if expect_running:
                return CheckResult(
                    status=Status.FAILED,
                    detail=f"{target} has no pid (state={state!r})",
                    metrics=metrics,
                )
            return CheckResult(
                status=Status.OK, detail=f"{target} state={state!r}", metrics=metrics
            )

        pid = int(pid_str)
        age_s = self._pid_age_s(pid, ctx.now)

        if age_s is not None and age_s < handoff_window_s:
            transition_until = ctx.now + timedelta(seconds=handoff_window_s - age_s)
            return CheckResult(
                status=Status.EXPECTED_TRANSITION,
                detail=f"{target} pid {pid} is {age_s:.0f}s old, within handoff window",
                metrics=metrics,
                instance_present=True,
                handoff_until=transition_until,
                transition_until=transition_until,
            )

        if state == "running":
            return CheckResult(
                status=Status.OK,
                detail=f"{target} running (pid {pid})",
                metrics=metrics,
                instance_present=True,
            )

        if expect_running:
            return CheckResult(
                status=Status.FAILED,
                detail=f"{target} state={state!r}, not running",
                metrics=metrics,
                instance_present=True,
            )
        return CheckResult(
            status=Status.OK,
            detail=f"{target} state={state!r}",
            metrics=metrics,
            instance_present=True,
        )

    def recover(self, action: str, ctx: CheckContext) -> ActionResult:
        if action != "kickstart":
            return super().recover(action, ctx)

        target = self._target(str(self.params.get("label", "")))

        if ctx.dry_run:
            return ActionResult(ok=True, detail=f"dry-run: would kickstart {target}")

        try:
            result = subprocess.run(
                [LAUNCHCTL, "kickstart", "-k", target],
                capture_output=True,
                text=True,
                timeout=_KICKSTART_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return ActionResult(ok=False, detail=f"kickstart failed: {exc}")

        if result.returncode == 0:
            return ActionResult(ok=True, detail=f"kickstarted {target}")
        return ActionResult(
            ok=False,
            detail=f"kickstart {target} exited {result.returncode}: {result.stderr.strip()}",
        )

    @staticmethod
    def _target(label: str) -> str:
        return f"gui/{os.getuid()}/{label}"

    @staticmethod
    def _parse_print(output: str) -> dict[str, str]:
        info: dict[str, str] = {}
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key in {"state", "pid", "last exit code"}:
                info[key] = value.strip()
        return info

    @staticmethod
    def _pid_age_s(pid: int, now: datetime) -> float | None:
        try:
            result = subprocess.run(
                [PS, "-o", "lstart=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        normalized = " ".join(result.stdout.split())
        if not normalized:
            return None
        try:
            started_at = datetime.strptime(normalized, _LSTART_FORMAT)
        except ValueError:
            return None
        return (now - started_at).total_seconds()


__all__ = ["LaunchdAdapter"]
