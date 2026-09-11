"""Tests for keepalive.integrations.signal_forge.adapter.SignalForgeAdapter."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from keepalive.core.adapter import ActionResult, CheckContext, CheckResult, Status
from keepalive.integrations.signal_forge import adapter as sf_adapter
from keepalive.integrations.signal_forge.adapter import SignalForgeAdapter
from keepalive.integrations.signal_forge.adapter import flock_probe as real_flock_probe

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _make_db(
    tmp_path: Path,
    *,
    # (started_at offset, last_beat offset) seconds from NOW
    heartbeat: tuple[str, str] | None = ("-3600", "-10"),
    job_running: bool = False,
    session_date: str | None = "2026-09-09",
) -> Path:
    """Build a minimal Signal Forge sqlite db. Offsets are seconds-before-NOW strings."""
    db_path = tmp_path / "signalforge.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE worker_heartbeat ("
            "worker TEXT, host TEXT, pid INTEGER, started_at TEXT, last_beat_at TEXT, "
            "next_runs_json TEXT, detail TEXT)"
        )
        conn.execute(
            "CREATE TABLE job_run (name TEXT, started_at TEXT, finished_at TEXT, "
            "status TEXT, detail TEXT)"
        )
        conn.execute("CREATE TABLE ohlcv_daily (session_date TEXT, source TEXT, symbol TEXT)")
        if heartbeat is not None:
            started_offset, last_beat_offset = heartbeat
            started_at = _iso(NOW + timedelta(seconds=float(started_offset)))
            last_beat_at = _iso(NOW + timedelta(seconds=float(last_beat_offset)))
            conn.execute(
                "INSERT INTO worker_heartbeat "
                "(worker, host, pid, started_at, last_beat_at, next_runs_json, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("worker", "localhost", 4242, started_at, last_beat_at, "{}", "ok"),
            )
        if job_running:
            conn.execute(
                "INSERT INTO job_run (name, started_at, finished_at, status, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                ("universe_scan", _iso(NOW - timedelta(seconds=60)), None, "running", ""),
            )
        if session_date is not None:
            conn.execute(
                "INSERT INTO ohlcv_daily (session_date, source, symbol) "
                "VALUES (?, 'massive', 'AAPL')",
                (session_date,),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _ctx(**overrides: Any) -> CheckContext:
    base: dict[str, Any] = {
        "service_name": "signal-forge",
        "params": {},
        "now": NOW,
        "on_ac_power": True,
        "slept_since_last_cycle": False,
        "last_cycle_at": None,
        "state_dir": "/tmp/keepalive-state",
    }
    base.update(overrides)
    return CheckContext(**base)


def _adapter(tmp_path: Path, db_path: Path, **param_overrides: Any) -> SignalForgeAdapter:
    params: dict[str, Any] = {
        "repo_dir": str(tmp_path),
        "db_path": str(db_path),
        "worker_label": "com.signalforge.worker",
        "api_label": "com.signalforge.api",
        "heartbeat_stale_s": 300,
        "handoff_window_s": 90,
    }
    params.update(param_overrides)
    return SignalForgeAdapter(params)


class _FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_launchctl_running(pid: int = 4242) -> Any:
    """A subprocess.run stand-in reporting the worker/api as running."""

    def _run(cmd: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        assert cmd[0] == "/bin/launchctl"
        return _FakeCompletedProcess(returncode=0, stdout=f"    pid = {pid}\n    state = running\n")

    return _run


@pytest.fixture(autouse=True)
def _no_real_subprocess_by_default(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test must explicitly control subprocess.run; default: process not present."""

    def _absent(cmd: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        return _FakeCompletedProcess(returncode=1, stdout="", stderr="Could not find service")

    monkeypatch.setattr(sf_adapter.subprocess, "run", _absent)
    yield


@pytest.fixture(autouse=True)
def _lock_not_held_by_default(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(sf_adapter, "flock_probe", lambda _path: False)
    yield


def test_healthy_heartbeat_is_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sf_adapter.subprocess, "run", _fake_launchctl_running())
    db_path = _make_db(tmp_path, heartbeat=("-3600", "-10"))
    adapter = _adapter(tmp_path, db_path)
    result = adapter.check(_ctx())

    assert isinstance(result, CheckResult)
    assert result.status == Status.OK
    assert result.instance_present is True
    assert result.active_job is False
    assert result.metrics["heartbeat_age_s"] == pytest.approx(10.0)


def test_stale_heartbeat_is_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sf_adapter.subprocess, "run", _fake_launchctl_running())
    db_path = _make_db(tmp_path, heartbeat=("-7200", "-600"))
    adapter = _adapter(tmp_path, db_path)
    result = adapter.check(_ctx())

    assert result.status == Status.FAILED
    assert result.metrics["heartbeat_age_s"] == pytest.approx(600.0)


def test_planned_restart_handoff_is_expected_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sf_adapter.subprocess, "run", _fake_launchctl_running())
    # worker started 30s ago (inside the 90s handoff window); heartbeat still stale.
    db_path = _make_db(tmp_path, heartbeat=("-30", "-600"))
    adapter = _adapter(tmp_path, db_path)
    result = adapter.check(_ctx())

    assert result.status == Status.EXPECTED_TRANSITION
    assert result.handoff_until is not None
    assert result.transition_until == result.handoff_until
    assert result.handoff_until > NOW


def test_stale_with_running_job_marks_active_job_and_recover_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sf_adapter.subprocess, "run", _fake_launchctl_running())
    db_path = _make_db(tmp_path, heartbeat=("-7200", "-600"), job_running=True)
    adapter = _adapter(tmp_path, db_path)

    result = adapter.check(_ctx())
    assert result.status == Status.FAILED
    assert result.active_job is True

    action_result = adapter.recover("restart_worker", _ctx())
    assert isinstance(action_result, ActionResult)
    assert action_result.ok is False
    assert "running" in action_result.detail


def test_lock_held_recover_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sf_adapter.subprocess, "run", _fake_launchctl_running())
    monkeypatch.setattr(sf_adapter, "flock_probe", lambda _path: True)
    db_path = _make_db(tmp_path, heartbeat=("-7200", "-600"), job_running=False)
    adapter = _adapter(tmp_path, db_path)

    action_result = adapter.recover("restart_worker", _ctx())
    assert action_result.ok is False
    assert "lock" in action_result.detail


def test_handoff_in_progress_recover_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sf_adapter.subprocess, "run", _fake_launchctl_running())
    db_path = _make_db(tmp_path, heartbeat=("-30", "-600"), job_running=False)
    adapter = _adapter(tmp_path, db_path)

    action_result = adapter.recover("restart_worker", _ctx())
    assert action_result.ok is False
    assert "handoff" in action_result.detail


def test_db_missing_is_unknown(tmp_path: Path) -> None:
    missing_db = tmp_path / "does-not-exist.sqlite"
    adapter = _adapter(tmp_path, missing_db)
    result = adapter.check(_ctx())

    assert result.status == Status.UNKNOWN


def test_unknown_action_is_refused_without_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _make_db(tmp_path)
    adapter = _adapter(tmp_path, db_path)
    result = adapter.recover("some_other_action", _ctx())
    assert result.ok is False


def test_connect_ro_rejects_writes(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    conn = sf_adapter.connect_ro(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO job_run (name, started_at, finished_at, status, detail) "
                "VALUES ('x', 'y', NULL, 'running', '')"
            )
    finally:
        conn.close()


def test_describe_declares_only_restart_worker_action(tmp_path: Path) -> None:
    manifest = SignalForgeAdapter.describe()
    action_names = [a.name for a in manifest.actions]
    assert action_names == ["restart_worker"]
    assert manifest.actions[0].starts_instance is True
    assert manifest.actions[0].allowed_during_active_job is False


def test_ops_url_failure_never_fails_the_check_on_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sf_adapter.subprocess, "run", _fake_launchctl_running())

    def _raise_urlopen(*args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("connection timed out")

    monkeypatch.setattr(sf_adapter, "urlopen", _raise_urlopen)
    db_path = _make_db(tmp_path, heartbeat=("-3600", "-10"))
    adapter = _adapter(tmp_path, db_path, ops_url="http://127.0.0.1:8787/ops")
    result = adapter.check(_ctx())

    assert result.status == Status.OK
    assert result.metrics["ops_url_ok"] is False


def test_no_heartbeat_rows_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sf_adapter.subprocess, "run", _fake_launchctl_running())
    db_path = _make_db(tmp_path, heartbeat=None)
    adapter = _adapter(tmp_path, db_path)
    result = adapter.check(_ctx())
    assert result.status == Status.UNKNOWN


def test_lock_probe_is_read_only_and_does_not_create_file(tmp_path: Path) -> None:
    lock_path = tmp_path / "data" / "worker.lock"
    assert real_flock_probe(lock_path) is False
    assert not lock_path.exists()


def test_lock_probe_detects_held_lock(tmp_path: Path) -> None:
    import fcntl

    lock_dir = tmp_path / "data"
    lock_dir.mkdir()
    lock_path = lock_dir / "worker.lock"
    lock_path.write_text("")
    fd = lock_path.open("r+")
    fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert real_flock_probe(lock_path) is True
    finally:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()
