"""Process presence adapter.

Read-only observation via ``/usr/bin/pgrep -f`` matched against the full command
line, an optional pid-file liveness check, and an optional flock probe on a
lock file the target application itself uses. The only declared action is
``signal_term`` — it never starts anything.

The match pattern is always passed to ``pgrep`` as a single argv element (no
shell involved), so shell metacharacters in the pattern have no special effect.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import subprocess
from pathlib import Path
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

PGREP = "/usr/bin/pgrep"
_SUBPROCESS_TIMEOUT_S = 5.0


class ProcessAdapter(Adapter):
    """Reports OK/FAILED based on whether ``pattern`` matches a running process."""

    kind = "process"

    @classmethod
    def describe(cls) -> AdapterManifest:
        return AdapterManifest(
            kind=cls.kind,
            description=(
                "Watches a process by command-line pattern (pgrep -f), with an optional "
                "pid-file liveness check and an optional flock probe for duplicate instances."
            ),
            checks=(
                CheckSpec(
                    name="process_presence",
                    description=(
                        "pgrep -f match against the full command line, plus optional "
                        "pid-file and lock-file probes."
                    ),
                ),
            ),
            reads=(
                DataSource(kind="command", target=f"{PGREP} -f -- <pattern>"),
                DataSource(kind="file", target="<pid_file> (optional)"),
                DataSource(kind="file", target="<lock_file> (optional, flock probe)"),
            ),
            actions=(
                ActionSpec(
                    name="signal_term",
                    description="Send SIGTERM to the matched process. Never starts a new instance.",
                    allowed_during_active_job=False,
                    starts_instance=False,
                    default_cooldown_s=300,
                    default_max_attempts=1,
                    destructive=True,
                ),
            ),
            notify_on=(
                NotifyCondition(event="incident_opened", description="Process not found by pgrep"),
                NotifyCondition(
                    event="recovered", description="Process found again after being missing"
                ),
                NotifyCondition(
                    event="circuit_open", description="Repeated recovery attempts failed"
                ),
            ),
            treat_wake_as_transition=True,
            params_schema={
                "pattern": "Full command-line pattern matched via `pgrep -f` semantics (required)",
                "pid_file": "Optional PID file path; its pid is preferred as the signal target",
                "lock_file": "Optional flock file path; a held lock reports instance_present",
            },
        )

    def validate_params(self) -> list[str]:
        problems = super().validate_params()
        pattern = self.params.get("pattern")
        if not pattern or not isinstance(pattern, str):
            problems.append("'pattern' is required and must be a non-empty string")
        return problems

    def check(self, ctx: CheckContext) -> CheckResult:
        pattern = str(self.params.get("pattern", ""))
        pid_file = str(self.params.get("pid_file", "") or "")
        lock_file = str(self.params.get("lock_file", "") or "")

        try:
            pids = self._pgrep(pattern)
        except (OSError, subprocess.SubprocessError) as exc:
            return CheckResult(status=Status.UNKNOWN, detail=f"pgrep check failed: {exc}")

        lock_held = False
        if lock_file:
            lock_held = self._probe_lock_held(Path(lock_file).expanduser())

        pid_from_file = self._read_pid_file(Path(pid_file).expanduser()) if pid_file else None

        running = bool(pids)
        instance_present = running or lock_held

        metrics: dict[str, Any] = {"matched_pids": pids}
        if pid_from_file is not None:
            metrics["pid_from_file"] = pid_from_file

        if running:
            target_pid = pid_from_file if pid_from_file in pids else pids[0]
            return CheckResult(
                status=Status.OK,
                detail=f"running (pid {target_pid})",
                metrics=metrics,
                instance_present=instance_present,
                lock_held=lock_held,
            )

        return CheckResult(
            status=Status.FAILED,
            detail="no process matches pattern",
            metrics=metrics,
            instance_present=instance_present,
            lock_held=lock_held,
        )

    def recover(self, action: str, ctx: CheckContext) -> ActionResult:
        if action != "signal_term":
            return super().recover(action, ctx)

        pattern = str(self.params.get("pattern", ""))
        pid_file = str(self.params.get("pid_file", "") or "")

        target_pid = self._read_pid_file(Path(pid_file).expanduser()) if pid_file else None
        if target_pid is None:
            try:
                pids = self._pgrep(pattern)
            except (OSError, subprocess.SubprocessError) as exc:
                return ActionResult(ok=False, detail=f"pgrep failed: {exc}")
            target_pid = pids[0] if pids else None

        if target_pid is None:
            return ActionResult(ok=False, detail="no running process found to signal")

        if ctx.dry_run:
            return ActionResult(ok=True, detail=f"dry-run: would send SIGTERM to pid {target_pid}")

        try:
            os.kill(target_pid, signal.SIGTERM)
        except ProcessLookupError:
            return ActionResult(ok=False, detail=f"pid {target_pid} no longer exists")
        except PermissionError as exc:
            return ActionResult(
                ok=False, detail=f"permission denied signalling pid {target_pid}: {exc}"
            )

        return ActionResult(ok=True, detail=f"sent SIGTERM to pid {target_pid}")

    @staticmethod
    def _pgrep(pattern: str) -> list[int]:
        result = subprocess.run(
            [PGREP, "-f", "--", pattern],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
            check=False,
        )
        if result.returncode == 0:
            return [int(line) for line in result.stdout.split() if line.strip()]
        if result.returncode == 1:
            return []
        raise OSError(f"pgrep exited {result.returncode}: {result.stderr.strip()}")

    @staticmethod
    def _read_pid_file(path: Path) -> int | None:
        try:
            text = path.read_text().strip()
            return int(text)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _probe_lock_held(path: Path) -> bool:
        """Try to acquire ``path`` non-blocking; True if some other process holds it."""
        try:
            fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        else:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)


__all__ = ["ProcessAdapter"]
