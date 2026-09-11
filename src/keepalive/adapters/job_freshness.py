"""Job-freshness adapter.

Observes how long ago some artifact of a job last changed — a file's mtime, the max
timestamp returned by a read-only SQL query, or the stdout of a fixed command — and
compares that age against ``max_age_s``. Staleness outside ``expected_windows`` (local
time-of-day ranges) is a failure; staleness inside a window is an expected transition
(e.g. a nightly job that legitimately hasn't produced fresh output yet). Declares no
recovery actions.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from datetime import date, datetime, time, timedelta
from urllib.parse import quote

from keepalive.core.adapter import (
    Adapter,
    AdapterManifest,
    CheckContext,
    CheckResult,
    CheckSpec,
    DataSource,
    NotifyCondition,
    Status,
)

_KINDS = ("file_mtime", "sqlite_max", "command_timestamp")
_SUBPROCESS_TIMEOUT_S = 10


class JobFreshnessError(Exception):
    """Raised internally when the freshness target cannot be read; caught by check()."""


def _parse_window(spec: str) -> tuple[time, time]:
    start_s, _, end_s = spec.partition("-")
    start = time.fromisoformat(start_s.strip())
    end = time.fromisoformat(end_s.strip())
    return start, end


def _in_window(now_t: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= now_t <= end
    # window wraps midnight
    return now_t >= start or now_t <= end


def _window_end_today(now: datetime, end: time) -> datetime:
    end_dt = datetime.combine(now.date(), end)
    if end_dt < now:
        end_dt += timedelta(days=1)
    return end_dt


def _parse_timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            pass
        try:
            return datetime.combine(date.fromisoformat(text), time.min)
        except ValueError as exc:
            raise JobFreshnessError(f"unparseable timestamp {value!r}") from exc
    raise JobFreshnessError(f"unparseable timestamp {value!r}")


def _validate_select(sql: str) -> str:
    stripped = sql.strip()
    if not stripped.upper().startswith("SELECT"):
        raise JobFreshnessError("sql must be a read-only SELECT statement")
    return stripped


class JobFreshnessAdapter(Adapter):
    """Reports staleness of a job's output against ``max_age_s``."""

    kind = "job_freshness"

    @classmethod
    def describe(cls) -> AdapterManifest:
        return AdapterManifest(
            kind=cls.kind,
            description="Observes the age of a job's last output (file, SQL query, or command).",
            checks=(
                CheckSpec(
                    name="freshness",
                    description="Age of the target's last-known timestamp vs max_age_s.",
                ),
            ),
            reads=(
                DataSource(kind="file", target="<target> (file_mtime)", read_only=True),
                DataSource(
                    kind="sqlite_ro", target="<target> (sqlite_max, via <sql>)", read_only=True
                ),
                DataSource(kind="command", target="<target> (command_timestamp)", read_only=True),
            ),
            actions=(),
            notify_on=(
                NotifyCondition(event="incident_opened", description="Output older than max_age_s"),
                NotifyCondition(event="recovered", description="Output fresh again"),
                NotifyCondition(
                    event="check_unavailable", description="Repeated failures reading the target"
                ),
            ),
            treat_wake_as_transition=True,
            params_schema={
                "kind": "One of file_mtime, sqlite_max, command_timestamp.",
                "target": (
                    "file_mtime/sqlite_max: a path. command_timestamp: an argv list "
                    "(list[str]) of a fixed command; never a shell string."
                ),
                "max_age_s": "Maximum tolerated age in seconds before FAILED/EXPECTED_TRANSITION.",
                "expected_windows": (
                    'Optional list of local "HH:MM-HH:MM" windows tolerating staleness.'
                ),
                "sql": "sqlite_max only: a read-only SELECT returning one ISO timestamp or date.",
            },
        )

    def validate_params(self) -> list[str]:
        problems = super().validate_params()
        kind = self.params.get("kind")
        if kind not in _KINDS:
            problems.append(f"'kind' must be one of {_KINDS}, got {kind!r}")
        if "target" not in self.params:
            problems.append("missing required param 'target'")
        if "max_age_s" not in self.params:
            problems.append("missing required param 'max_age_s'")
        elif not isinstance(self.params.get("max_age_s"), int | float):
            problems.append("'max_age_s' must be numeric")
        windows = self.params.get("expected_windows", [])
        if not isinstance(windows, list):
            problems.append("'expected_windows' must be a list of \"HH:MM-HH:MM\" strings")
        else:
            for spec in windows:
                try:
                    _parse_window(str(spec))
                except ValueError:
                    problems.append(f"invalid expected_windows entry {spec!r}")
        if kind == "sqlite_max":
            sql = self.params.get("sql")
            if not isinstance(sql, str) or not sql.strip():
                problems.append("sqlite_max requires a non-empty 'sql' param")
            else:
                try:
                    _validate_select(sql)
                except JobFreshnessError as exc:
                    problems.append(str(exc))
        return problems

    def _read_last_ts(self, kind: str, target: object) -> datetime:
        if kind == "file_mtime":
            if not isinstance(target, str) or not target:
                raise JobFreshnessError("file_mtime requires a string 'target' path")
            path = os.path.expanduser(target)
            try:
                mtime = os.stat(path).st_mtime
            except OSError as exc:
                raise JobFreshnessError(f"could not stat {path!r}: {exc}") from exc
            return datetime.fromtimestamp(mtime)

        if kind == "sqlite_max":
            if not isinstance(target, str) or not target:
                raise JobFreshnessError("sqlite_max requires a string 'target' path")
            sql = _validate_select(str(self.params.get("sql", "")))
            path = os.path.expanduser(target)
            uri = f"file:{quote(path)}?mode=ro"
            try:
                conn = sqlite3.connect(uri, uri=True, timeout=5)
            except sqlite3.Error as exc:
                raise JobFreshnessError(f"could not open {path!r} read-only: {exc}") from exc
            try:
                conn.execute("PRAGMA query_only=1")
                cursor = conn.execute(sql)
                row = cursor.fetchone()
            except sqlite3.Error as exc:
                raise JobFreshnessError(f"query failed against {path!r}: {exc}") from exc
            finally:
                conn.close()
            if not row or row[0] is None:
                raise JobFreshnessError(f"query against {path!r} returned no timestamp")
            return _parse_timestamp(row[0])

        if kind == "command_timestamp":
            if not isinstance(target, list) or not target:
                raise JobFreshnessError("command_timestamp requires a 'target' argv list[str]")
            argv = [str(a) for a in target]
            if not all(isinstance(a, str) for a in target):
                raise JobFreshnessError("command_timestamp requires a 'target' argv list[str]")
            try:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=_SUBPROCESS_TIMEOUT_S,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise JobFreshnessError(f"command {target!r} failed: {exc}") from exc
            if proc.returncode != 0:
                stderr = proc.stderr.strip()
                raise JobFreshnessError(f"command {target!r} exited {proc.returncode}: {stderr}")
            lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
            if not lines:
                raise JobFreshnessError(f"command {target!r} produced no output")
            return _parse_timestamp(lines[-1])

        raise JobFreshnessError(f"unknown job_freshness kind {kind!r}")

    def check(self, ctx: CheckContext) -> CheckResult:
        kind = self.params.get("kind")
        target = self.params.get("target")
        max_age_s = self.params.get("max_age_s")
        if kind not in _KINDS or not isinstance(max_age_s, int | float):
            return CheckResult(
                status=Status.UNKNOWN,
                detail="job_freshness adapter has invalid params (see validate_params)",
            )

        try:
            last_ts = self._read_last_ts(kind, target)
        except JobFreshnessError as exc:
            return CheckResult(status=Status.UNKNOWN, detail=str(exc))

        age_s = (ctx.now - last_ts).total_seconds()
        metrics = {"age_s": age_s, "last_ts": last_ts.isoformat()}

        if age_s <= max_age_s:
            return CheckResult(
                status=Status.OK,
                detail=f"fresh: age {age_s:.0f}s <= max_age_s {max_age_s:.0f}s",
                metrics=metrics,
                last_healthy_hint=last_ts,
            )

        windows_raw = self.params.get("expected_windows", []) or []
        now_t = ctx.now.time()
        for spec in windows_raw:
            try:
                start, end = _parse_window(str(spec))
            except ValueError:
                continue
            if _in_window(now_t, start, end):
                transition_until = _window_end_today(ctx.now, end)
                return CheckResult(
                    status=Status.EXPECTED_TRANSITION,
                    detail=(
                        f"stale (age {age_s:.0f}s > max_age_s {max_age_s:.0f}s) but within "
                        f"expected window {spec!r}"
                    ),
                    metrics=metrics,
                    transition_until=transition_until,
                    last_healthy_hint=last_ts,
                )

        return CheckResult(
            status=Status.FAILED,
            detail=f"stale: age {age_s:.0f}s > max_age_s {max_age_s:.0f}s",
            metrics=metrics,
            last_healthy_hint=last_ts,
        )


__all__ = ["JobFreshnessAdapter"]
