"""Optional example adapter for the Signal Forge trading pipeline.

Everything here is **read-only** except the single declared action
(``restart_worker``, a launchd kickstart of the worker agent). The adapter never
reads or writes ``app_setting``, ``alerts``, Discord, probation, auto-simulation,
strategies, paper trades, provider keys, or KeepAlive's own kill switch — this is
enforced by construction: only the three named tables are ever queried, every SQL
string is a fixed literal (no interpolation of untrusted values), and the database
connection is opened ``?mode=ro`` with ``PRAGMA query_only=1`` so no write can
succeed through it even if a query were wrong. It has no brokerage capability of
any kind.

KeepAlive core does not import this module; it is loaded only when a service in
config names ``adapter = "signal_forge"`` (see ``keepalive.core.registry``).
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

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

_LAUNCHCTL = "/bin/launchctl"
_TIMEOUT_LAUNCHCTL_S = 5
_TIMEOUT_KICKSTART_S = 10
_TIMEOUT_HTTP_S = 5
_TIMEOUT_SQLITE_S = 5

_HEARTBEAT_QUERY = (
    "SELECT worker, host, pid, started_at, last_beat_at, next_runs_json, detail "
    "FROM worker_heartbeat ORDER BY last_beat_at DESC LIMIT 1"
)
_ACTIVE_JOB_QUERY = "SELECT COUNT(*) FROM job_run WHERE status = 'running'"
_FRESHNESS_QUERY = "SELECT MAX(session_date) FROM ohlcv_daily WHERE source = 'massive'"


def _as_aware(dt: datetime) -> datetime:
    """Normalize a possibly-naive datetime to aware UTC for safe subtraction."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _parse_ts(value: Any) -> datetime | None:
    """Best-effort parse of a heartbeat timestamp column (ISO string or epoch)."""
    if value is None:
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                return datetime.fromtimestamp(float(text), tz=UTC)
            except ValueError:
                return None
        return _as_aware(dt)
    return None


def _launchctl_probe(label: str) -> tuple[int | None, str | None]:
    """Read-only ``launchctl print gui/<uid>/<label>``; never raises."""
    uid = os.getuid()
    cmd = [_LAUNCHCTL, "print", f"gui/{uid}/{label}"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_TIMEOUT_LAUNCHCTL_S, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    if proc.returncode != 0:
        return None, None
    pid: int | None = None
    state: str | None = None
    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("pid = "):
            try:
                pid = int(line.split("=", 1)[1].strip())
            except ValueError:
                pid = None
        elif line.startswith("state = "):
            state = line.split("=", 1)[1].strip()
    return pid, state


def flock_probe(lock_path: Path) -> bool:
    """Non-blocking probe of ``lock_path``: True if a live process holds it.

    Read-only: opens the file O_RDONLY (never creates it) and attempts a
    non-blocking exclusive flock. If the lock cannot be taken, something else
    holds it. Immediately releases any lock it does take.
    """
    if not lock_path.exists():
        return False
    try:
        fd = os.open(str(lock_path), os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
    finally:
        os.close(fd)


def _probe_ops_url(url: str) -> tuple[bool | None, str]:
    """GET ``url`` with a short timeout. Failure is detail-only, never FAILED."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None, "ops_url: unsupported scheme, skipped"
    req = Request(url, method="GET")  # noqa: S310
    try:
        # scheme validated above
        with urlopen(req, timeout=_TIMEOUT_HTTP_S) as resp:  # noqa: S310  # nosec B310
            status_code = getattr(resp, "status", 200)
            ok = 200 <= status_code < 300
            return ok, f"ops_url status {status_code}"
    except Exception as exc:
        return False, f"ops_url unreachable: {exc}"


def connect_ro(db_path: Path) -> sqlite3.Connection:
    """Open ``db_path`` strictly read-only: URI ``?mode=ro`` plus ``query_only``.

    Raises ``sqlite3.Error`` if the file does not exist or cannot be opened. Any
    write attempted through the returned connection raises ``sqlite3.OperationalError``.
    """
    uri = f"file:{quote(str(db_path))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=_TIMEOUT_SQLITE_S)
    conn.execute("PRAGMA query_only = 1")
    return conn


@dataclass(slots=True)
class _HeartbeatInfo:
    found: bool
    pid: int | None
    started_at: datetime | None
    last_beat_at: datetime | None
    detail: str
    latest_session_date: str | None


def _read_heartbeat(conn: sqlite3.Connection) -> _HeartbeatInfo:
    row = conn.execute(_HEARTBEAT_QUERY).fetchone()
    latest_session_date: str | None = None
    try:
        freshness_row = conn.execute(_FRESHNESS_QUERY).fetchone()
        latest_session_date = freshness_row[0] if freshness_row else None
    except sqlite3.Error:
        latest_session_date = None
    if row is None:
        return _HeartbeatInfo(False, None, None, None, "", latest_session_date)
    _worker, _host, pid, started_at_raw, last_beat_at_raw, _next_runs, detail = row
    return _HeartbeatInfo(
        True,
        pid,
        _parse_ts(started_at_raw),
        _parse_ts(last_beat_at_raw),
        detail or "",
        latest_session_date,
    )


def _job_active(conn: sqlite3.Connection) -> bool:
    row = conn.execute(_ACTIVE_JOB_QUERY).fetchone()
    return bool(row and row[0])


class SignalForgeAdapter(Adapter):
    """Observes the Signal Forge worker heartbeat/job-run tables and launchd state;
    optionally restarts the worker via a single bounded, re-confirmed action."""

    kind = "signal_forge"

    @classmethod
    def describe(cls) -> AdapterManifest:
        return AdapterManifest(
            kind="signal_forge",
            description=(
                "Observes Signal Forge's worker heartbeat, active job runs, and launchd "
                "state; optionally restarts the worker agent through a single re-confirmed "
                "action."
            ),
            checks=(
                CheckSpec(
                    name="worker_heartbeat",
                    description="Age of the most recent worker_heartbeat row.",
                ),
                CheckSpec(
                    name="job_active",
                    description="Whether any job_run row has status='running'.",
                ),
                CheckSpec(
                    name="launchd_state",
                    description="pid/state of the worker (and optional api) launchd jobs.",
                ),
                CheckSpec(
                    name="worker_lock",
                    description="Non-blocking flock probe of <repo_dir>/data/worker.lock.",
                ),
                CheckSpec(
                    name="ops_health",
                    description="Optional GET of ops_url; a metric only, never fails the check.",
                ),
                CheckSpec(
                    name="daily_bar_freshness",
                    description=(
                        "Latest ohlcv_daily.session_date for source='massive' (metric only)."
                    ),
                ),
            ),
            reads=(
                DataSource(kind="sqlite_ro", target="worker_heartbeat"),
                DataSource(kind="sqlite_ro", target="job_run"),
                DataSource(kind="sqlite_ro", target="ohlcv_daily"),
                DataSource(kind="launchd", target="gui/<uid>/<worker_label>"),
                DataSource(kind="launchd", target="gui/<uid>/<api_label>"),
                DataSource(kind="file", target="<repo_dir>/data/worker.lock"),
                DataSource(kind="http", target="<ops_url>"),
            ),
            actions=(
                ActionSpec(
                    name="restart_worker",
                    description="launchctl kickstart -k the worker launchd job.",
                    allowed_during_active_job=False,
                    starts_instance=True,
                    default_cooldown_s=900,
                    default_max_attempts=2,
                    destructive=False,
                ),
            ),
            notify_on=(
                NotifyCondition(
                    event="incident_opened",
                    description="Worker heartbeat has been stale beyond the threshold.",
                ),
                NotifyCondition(
                    event="recovered",
                    description="Worker heartbeat resumed after an open incident.",
                ),
                NotifyCondition(
                    event="circuit_open",
                    description="restart_worker attempts exhausted; manual intervention needed.",
                ),
                NotifyCondition(
                    event="check_unavailable",
                    description="The Signal Forge database could not be read.",
                ),
            ),
            treat_wake_as_transition=True,
            params_schema={
                "repo_dir": "Path to the Signal Forge repo (for the worker lock file).",
                "db_path": "Path to the Signal Forge SQLite database, opened read-only.",
                "ops_url": "Optional /ops health URL (metric only; never fails the check alone).",
                "worker_label": "launchd label of the worker agent.",
                "api_label": "launchd label of the api agent.",
                "heartbeat_stale_s": "Seconds after which a heartbeat is stale (default 300).",
                "handoff_window_s": (
                    "Seconds after a worker start that a stale heartbeat is an expected "
                    "transition, not a failure (default 90)."
                ),
            },
        )

    def check(self, ctx: CheckContext) -> CheckResult:
        db_path = Path(str(self.params.get("db_path", ""))).expanduser()
        repo_dir = Path(str(self.params.get("repo_dir", ""))).expanduser()
        heartbeat_stale_s = int(self.params.get("heartbeat_stale_s", 300))
        handoff_window_s = int(self.params.get("handoff_window_s", 90))
        worker_label = str(self.params.get("worker_label", ""))
        api_label = self.params.get("api_label")

        metrics: dict[str, Any] = {}

        worker_pid, worker_state = _launchctl_probe(worker_label) if worker_label else (None, None)
        metrics["worker_pid"] = worker_pid
        metrics["worker_launchd_state"] = worker_state
        instance_present = worker_pid is not None

        if api_label:
            api_pid, api_state = _launchctl_probe(str(api_label))
            metrics["api_pid"] = api_pid
            metrics["api_launchd_state"] = api_state

        lock_path = repo_dir / "data" / "worker.lock"
        lock_held = flock_probe(lock_path)

        detail_bits: list[str] = []
        ops_url = self.params.get("ops_url")
        if ops_url:
            ok, ops_detail = _probe_ops_url(str(ops_url))
            metrics["ops_url_ok"] = ok
            detail_bits.append(ops_detail)

        try:
            conn = connect_ro(db_path)
        except sqlite3.Error as exc:
            return CheckResult(
                status=Status.UNKNOWN,
                detail=f"signal-forge db unreadable: {exc}",
                metrics=metrics,
                instance_present=instance_present,
                lock_held=lock_held,
            )

        try:
            hb = _read_heartbeat(conn)
            active_job = _job_active(conn)
        except sqlite3.Error as exc:
            return CheckResult(
                status=Status.UNKNOWN,
                detail=f"signal-forge db query failed: {exc}",
                metrics=metrics,
                instance_present=instance_present,
                lock_held=lock_held,
            )
        finally:
            conn.close()

        metrics["latest_session_date"] = hb.latest_session_date

        if not hb.found:
            return CheckResult(
                status=Status.UNKNOWN,
                detail="no worker_heartbeat rows",
                metrics=metrics,
                instance_present=instance_present,
                lock_held=lock_held,
                active_job=active_job,
            )

        now = _as_aware(ctx.now)

        heartbeat_age_s: float | None = None
        if hb.last_beat_at is not None:
            heartbeat_age_s = (now - hb.last_beat_at).total_seconds()
        metrics["heartbeat_age_s"] = heartbeat_age_s

        process_age_s: float | None = None
        if hb.started_at is not None:
            process_age_s = (now - hb.started_at).total_seconds()
        metrics["process_age_s"] = process_age_s

        handoff_active = process_age_s is not None and 0 <= process_age_s < handoff_window_s
        handoff_until = (
            hb.started_at + timedelta(seconds=handoff_window_s)
            if handoff_active and hb.started_at is not None
            else None
        )

        if heartbeat_age_s is not None and heartbeat_age_s <= heartbeat_stale_s:
            status = Status.OK
        elif handoff_active:
            status = Status.EXPECTED_TRANSITION
        else:
            status = Status.FAILED

        detail_bits.insert(0, f"heartbeat_age_s={heartbeat_age_s}")
        if hb.detail:
            detail_bits.append(hb.detail)

        return CheckResult(
            status=status,
            detail="; ".join(detail_bits),
            metrics=metrics,
            active_job=active_job,
            instance_present=instance_present,
            lock_held=lock_held,
            handoff_until=handoff_until,
            transition_until=handoff_until if status == Status.EXPECTED_TRANSITION else None,
            last_healthy_hint=hb.last_beat_at,
        )

    def recover(self, action: str, ctx: CheckContext) -> ActionResult:
        if action != "restart_worker":
            return super().recover(action, ctx)

        db_path = Path(str(self.params.get("db_path", ""))).expanduser()
        repo_dir = Path(str(self.params.get("repo_dir", ""))).expanduser()
        worker_label = str(self.params.get("worker_label", ""))
        handoff_window_s = int(self.params.get("handoff_window_s", 90))

        try:
            conn = connect_ro(db_path)
        except sqlite3.Error as exc:
            return ActionResult(ok=False, detail=f"refused: signal-forge db unreadable: {exc}")

        try:
            active_job = _job_active(conn)
            hb = _read_heartbeat(conn)
        except sqlite3.Error as exc:
            return ActionResult(ok=False, detail=f"refused: signal-forge db query failed: {exc}")
        finally:
            conn.close()

        if active_job:
            return ActionResult(ok=False, detail="refused: a job_run row has status='running'")

        lock_path = repo_dir / "data" / "worker.lock"
        if flock_probe(lock_path):
            return ActionResult(ok=False, detail="refused: worker lock is held by a live process")

        if hb.started_at is not None:
            now = _as_aware(ctx.now)
            process_age_s = (now - hb.started_at).total_seconds()
            if 0 <= process_age_s < handoff_window_s:
                return ActionResult(ok=False, detail="refused: worker handoff already in progress")

        uid = os.getuid()
        cmd = [_LAUNCHCTL, "kickstart", "-k", f"gui/{uid}/{worker_label}"]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_TIMEOUT_KICKSTART_S, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return ActionResult(ok=False, detail=f"launchctl kickstart failed to run: {exc}")

        if proc.returncode != 0:
            return ActionResult(
                ok=False,
                detail=f"launchctl kickstart exited {proc.returncode}: {proc.stderr.strip()}",
            )
        return ActionResult(
            ok=True,
            detail="launchctl kickstart issued",
            metrics={"launchd_label": worker_label},
        )


__all__ = ["SignalForgeAdapter"]
