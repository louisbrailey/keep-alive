"""Tests for keepalive.adapters.job_freshness.JobFreshnessAdapter."""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

from keepalive.adapters.job_freshness import JobFreshnessAdapter
from keepalive.core.adapter import CheckContext, Status

NOW = datetime(2024, 1, 1, 12, 0, 0)


def _ctx(now: datetime = NOW) -> CheckContext:
    return CheckContext(
        service_name="freshness",
        params={},
        now=now,
        on_ac_power=True,
        slept_since_last_cycle=False,
        last_cycle_at=None,
        state_dir="/tmp/keepalive-state",
    )


def _touch(path: Path, mtime: datetime) -> None:
    path.write_text("data")
    ts = mtime.timestamp()
    os.utime(path, (ts, ts))


# --- describe / validate_params -------------------------------------------------


def test_describe_manifest_has_no_actions() -> None:
    manifest = JobFreshnessAdapter.describe()
    assert manifest.kind == "job_freshness"
    assert manifest.actions == ()


def test_describe_manifest_declares_notify_conditions() -> None:
    manifest = JobFreshnessAdapter.describe()
    events = {c.event for c in manifest.notify_on}
    assert events == {"incident_opened", "recovered", "check_unavailable"}


def test_validate_params_bad_kind() -> None:
    problems = JobFreshnessAdapter(
        {"kind": "nonsense", "target": "x", "max_age_s": 10}
    ).validate_params()
    assert any("kind" in p for p in problems)


def test_validate_params_sqlite_requires_select() -> None:
    problems = JobFreshnessAdapter(
        {
            "kind": "sqlite_max",
            "target": "db.sqlite",
            "max_age_s": 10,
            "sql": "DELETE FROM runs",
        }
    ).validate_params()
    assert any("SELECT" in p for p in problems)


def test_validate_params_bad_window() -> None:
    problems = JobFreshnessAdapter(
        {
            "kind": "file_mtime",
            "target": "x",
            "max_age_s": 10,
            "expected_windows": ["not-a-window"],
        }
    ).validate_params()
    assert any("expected_windows" in p for p in problems)


# --- file_mtime -------------------------------------------------------------


def test_file_mtime_healthy(tmp_path: Path) -> None:
    target = tmp_path / "latest.json"
    _touch(target, NOW)
    adapter = JobFreshnessAdapter({"kind": "file_mtime", "target": str(target), "max_age_s": 3600})
    result = adapter.check(_ctx())
    assert result.status == Status.OK
    assert result.metrics["age_s"] == pytest.approx(0.0, abs=1.0)


def test_file_mtime_stale_outside_window(tmp_path: Path) -> None:
    target = tmp_path / "latest.json"
    _touch(target, datetime(2023, 12, 1, 12, 0, 0))
    adapter = JobFreshnessAdapter(
        {
            "kind": "file_mtime",
            "target": str(target),
            "max_age_s": 3600,
            "expected_windows": ["01:00-02:00"],
        }
    )
    result = adapter.check(_ctx())
    assert result.status == Status.FAILED


def test_file_mtime_stale_inside_window(tmp_path: Path) -> None:
    target = tmp_path / "latest.json"
    _touch(target, datetime(2023, 12, 1, 12, 0, 0))
    adapter = JobFreshnessAdapter(
        {
            "kind": "file_mtime",
            "target": str(target),
            "max_age_s": 3600,
            "expected_windows": ["11:30-12:30"],  # NOW (12:00) falls inside
        }
    )
    result = adapter.check(_ctx())
    assert result.status == Status.EXPECTED_TRANSITION
    assert result.transition_until is not None
    assert result.transition_until.time().isoformat(timespec="minutes") == "12:30"


def test_file_mtime_unreadable_target_is_unknown(tmp_path: Path) -> None:
    adapter = JobFreshnessAdapter(
        {"kind": "file_mtime", "target": str(tmp_path / "missing.json"), "max_age_s": 3600}
    )
    result = adapter.check(_ctx())
    assert result.status == Status.UNKNOWN


# --- sqlite_max ---------------------------------------------------------------


def _make_db(tmp_path: Path, finished_at: str) -> Path:
    db_path = tmp_path / "jobs.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, finished_at TEXT)")
    conn.execute("INSERT INTO runs (finished_at) VALUES (?)", (finished_at,))
    conn.commit()
    conn.close()
    return db_path


def test_sqlite_max_healthy(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path, NOW.isoformat())
    adapter = JobFreshnessAdapter(
        {
            "kind": "sqlite_max",
            "target": str(db_path),
            "max_age_s": 3600,
            "sql": "SELECT MAX(finished_at) FROM runs",
        }
    )
    result = adapter.check(_ctx())
    assert result.status == Status.OK


def test_sqlite_max_stale(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path, datetime(2023, 1, 1, 0, 0, 0).isoformat())
    adapter = JobFreshnessAdapter(
        {
            "kind": "sqlite_max",
            "target": str(db_path),
            "max_age_s": 3600,
            "sql": "SELECT MAX(finished_at) FROM runs",
        }
    )
    result = adapter.check(_ctx())
    assert result.status == Status.FAILED


def test_sqlite_max_path_with_uri_special_chars(tmp_path: Path) -> None:
    # A target path containing '?' or '#' must not be interpreted as the start
    # of the sqlite URI's query string / fragment: it needs percent-encoding
    # (see connect_ro() in integrations/signal_forge/adapter.py for the
    # pattern this mirrors).
    weird_dir = tmp_path / "weird dir?with#chars"
    weird_dir.mkdir()
    db_path = _make_db(weird_dir, NOW.isoformat())
    adapter = JobFreshnessAdapter(
        {
            "kind": "sqlite_max",
            "target": str(db_path),
            "max_age_s": 3600,
            "sql": "SELECT MAX(finished_at) FROM runs",
        }
    )
    result = adapter.check(_ctx())
    assert result.status == Status.OK


def test_sqlite_max_nonexistent_db_is_unknown(tmp_path: Path) -> None:
    adapter = JobFreshnessAdapter(
        {
            "kind": "sqlite_max",
            "target": str(tmp_path / "missing.sqlite"),
            "max_age_s": 3600,
            "sql": "SELECT MAX(finished_at) FROM runs",
        }
    )
    result = adapter.check(_ctx())
    assert result.status == Status.UNKNOWN


def test_sqlite_max_rejects_non_select(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path, NOW.isoformat())
    adapter = JobFreshnessAdapter(
        {
            "kind": "sqlite_max",
            "target": str(db_path),
            "max_age_s": 3600,
            "sql": "DELETE FROM runs",
        }
    )
    result = adapter.check(_ctx())
    assert result.status == Status.UNKNOWN
    assert "SELECT" in result.detail

    # And the table must be untouched -- the adapter never executed the DELETE.
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_sqlite_max_rejects_attached_non_select(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path, NOW.isoformat())
    adapter = JobFreshnessAdapter(
        {
            "kind": "sqlite_max",
            "target": str(db_path),
            "max_age_s": 3600,
            "sql": "  select MAX(finished_at) FROM runs  ",
        }
    )
    # lower-case / whitespace-padded SELECT is still accepted.
    result = adapter.check(_ctx())
    assert result.status == Status.OK


# --- command_timestamp ---------------------------------------------------------


def test_command_timestamp_healthy() -> None:
    argv = [sys.executable, "-c", f"print({NOW.isoformat()!r})"]
    adapter = JobFreshnessAdapter({"kind": "command_timestamp", "target": argv, "max_age_s": 3600})
    result = adapter.check(_ctx())
    assert result.status == Status.OK


def test_command_timestamp_stale() -> None:
    old = datetime(2023, 1, 1, 0, 0, 0).isoformat()
    argv = [sys.executable, "-c", f"print({old!r})"]
    adapter = JobFreshnessAdapter({"kind": "command_timestamp", "target": argv, "max_age_s": 3600})
    result = adapter.check(_ctx())
    assert result.status == Status.FAILED


def test_command_timestamp_nonzero_exit_is_unknown() -> None:
    argv = [sys.executable, "-c", "import sys; sys.exit(1)"]
    adapter = JobFreshnessAdapter({"kind": "command_timestamp", "target": argv, "max_age_s": 3600})
    result = adapter.check(_ctx())
    assert result.status == Status.UNKNOWN


def test_command_timestamp_rejects_non_list_target() -> None:
    adapter = JobFreshnessAdapter(
        {"kind": "command_timestamp", "target": "echo 2024-01-01", "max_age_s": 3600}
    )
    result = adapter.check(_ctx())
    assert result.status == Status.UNKNOWN
