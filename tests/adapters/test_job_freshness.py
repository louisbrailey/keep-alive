"""Tests for keepalive.adapters.job_freshness.JobFreshnessAdapter."""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from keepalive.adapters.job_freshness import JobFreshnessAdapter
from keepalive.core.adapter import CheckContext, Status

# Local-zone-aware: the default `ctx.now` for tests below that don't care
# about naive-timestamp interpretation (file_mtime is always an unambiguous
# epoch; the window test only cares about local wall-clock time-of-day).
NOW = datetime(2024, 1, 1, 12, 0, 0).astimezone()

# The literal wall-clock digits used to build "naive" (offset-less) fixture
# timestamps for sqlite_max/command_timestamp, which the adapter assumes are
# UTC by default (`naive_tz="utc"`) -- so the matching `ctx.now` for those
# tests is this same instant tagged as UTC, not local (see NOW_UTC below).
_NAIVE_UTC_NOW = datetime(2024, 1, 1, 12, 0, 0)
NOW_UTC = _NAIVE_UTC_NOW.replace(tzinfo=UTC)


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
    # naive_tz defaults to "utc": a naive sqlite timestamp is assumed UTC, so
    # the matching ctx.now here is NOW_UTC (the same instant, tagged UTC).
    db_path = _make_db(tmp_path, _NAIVE_UTC_NOW.isoformat())
    adapter = JobFreshnessAdapter(
        {
            "kind": "sqlite_max",
            "target": str(db_path),
            "max_age_s": 3600,
            "sql": "SELECT MAX(finished_at) FROM runs",
        }
    )
    result = adapter.check(_ctx(NOW_UTC))
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
    db_path = _make_db(weird_dir, _NAIVE_UTC_NOW.isoformat())
    adapter = JobFreshnessAdapter(
        {
            "kind": "sqlite_max",
            "target": str(db_path),
            "max_age_s": 3600,
            "sql": "SELECT MAX(finished_at) FROM runs",
        }
    )
    result = adapter.check(_ctx(NOW_UTC))
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
    db_path = _make_db(tmp_path, _NAIVE_UTC_NOW.isoformat())
    adapter = JobFreshnessAdapter(
        {
            "kind": "sqlite_max",
            "target": str(db_path),
            "max_age_s": 3600,
            "sql": "  select MAX(finished_at) FROM runs  ",
        }
    )
    # lower-case / whitespace-padded SELECT is still accepted.
    result = adapter.check(_ctx(NOW_UTC))
    assert result.status == Status.OK


# --- command_timestamp ---------------------------------------------------------


def test_command_timestamp_healthy() -> None:
    argv = [sys.executable, "-c", f"print({_NAIVE_UTC_NOW.isoformat()!r})"]
    adapter = JobFreshnessAdapter({"kind": "command_timestamp", "target": argv, "max_age_s": 3600})
    result = adapter.check(_ctx(NOW_UTC))
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


# --- naive_tz / timezone regressions -------------------------------------------


def test_validate_params_bad_naive_tz() -> None:
    problems = JobFreshnessAdapter(
        {"kind": "file_mtime", "target": "x", "max_age_s": 10, "naive_tz": "mars"}
    ).validate_params()
    assert any("naive_tz" in p for p in problems)


def test_sqlite_max_naive_utc_string_age_correct_regardless_of_ctx_now_zone(
    tmp_path: Path,
) -> None:
    """Regression: a naive (offset-less) sqlite timestamp is assumed UTC by
    default (naive_tz='utc'). ctx.now may carry a completely unrelated fixed
    offset (the way the real engine's local-zone `now` differs from UTC) --
    the two must be converted to the same absolute-time basis before
    subtracting, not compared as naive wall-clock numbers."""
    last_run_utc = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    fresh_now = (last_run_utc + timedelta(seconds=60)).astimezone(timezone(timedelta(hours=-7)))
    db_path = _make_db(tmp_path, last_run_utc.replace(tzinfo=None).isoformat())
    adapter = JobFreshnessAdapter(
        {
            "kind": "sqlite_max",
            "target": str(db_path),
            "max_age_s": 3600,
            "sql": "SELECT MAX(finished_at) FROM runs",
        }
    )
    result = adapter.check(_ctx(fresh_now))
    assert result.status == Status.OK
    assert result.metrics["age_s"] == pytest.approx(60.0, abs=1.0)


def test_sqlite_max_naive_utc_string_stale_across_nonutc_ctx_now_zone(tmp_path: Path) -> None:
    last_run_utc = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    stale_now = (last_run_utc + timedelta(hours=2)).astimezone(timezone(timedelta(hours=-7)))
    db_path = _make_db(tmp_path, last_run_utc.replace(tzinfo=None).isoformat())
    adapter = JobFreshnessAdapter(
        {
            "kind": "sqlite_max",
            "target": str(db_path),
            "max_age_s": 3600,
            "sql": "SELECT MAX(finished_at) FROM runs",
        }
    )
    result = adapter.check(_ctx(stale_now))
    assert result.status == Status.FAILED


def test_sqlite_max_naive_tz_local_interprets_naive_timestamp_as_local(tmp_path: Path) -> None:
    """With naive_tz='local', the same naive string is interpreted as local
    wall-clock time instead of UTC."""
    fresh_local_now = datetime(2026, 9, 10, 12, 0, 30).astimezone()
    naive_local_last_run = "2026-09-10T12:00:00"  # 30s before fresh_local_now, same local zone
    db_path = _make_db(tmp_path, naive_local_last_run)
    adapter = JobFreshnessAdapter(
        {
            "kind": "sqlite_max",
            "target": str(db_path),
            "max_age_s": 3600,
            "sql": "SELECT MAX(finished_at) FROM runs",
            "naive_tz": "local",
        }
    )
    result = adapter.check(_ctx(fresh_local_now))
    assert result.status == Status.OK
    assert result.metrics["age_s"] == pytest.approx(30.0, abs=1.0)
