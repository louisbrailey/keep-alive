"""Tests for keepalive.core.power. No real pmset/caffeinate/subprocess calls —
subprocess.run and subprocess.Popen are monkeypatched."""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from keepalive.core.audit import AuditLog
from keepalive.core.config import PowerSettings
from keepalive.core.power import PowerObserver, WakeWindow

_BATT_ON_AC = "Now drawing from 'AC Power'\n -InternalBattery-0\t85%; charged; 0:00 remaining\n"
_BATT_ON_BATTERY = (
    "Now drawing from 'Battery Power'\n -InternalBattery-0\t42%; discharging; 2:00 remaining\n"
)
_LOG_WITH_SLEEP_AND_WAKE = (
    "2026-01-01 10:00:00 +0000 Sleep                   Entering Sleep state\n"
    "2026-01-01 10:05:00 +0000 Wake                    Wake from Deep Idle\n"
)
_LOG_NO_EVENTS = "2020-01-01 00:00:00 +0000 Notification            some unrelated line\n"


def _settings(**overrides: Any) -> PowerSettings:
    base = dict(observe=True, wake_windows=False, allow_on_battery=False, max_wake_minutes=120)
    base.update(overrides)
    return PowerSettings(**base)  # type: ignore[arg-type]


def _fake_run(batt_output: str | None, log_output: str | None) -> Any:
    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["/usr/bin/pmset", "-g"] and args[2] == "batt":
            if batt_output is None:
                return subprocess.CompletedProcess(args, returncode=1, stdout="")
            return subprocess.CompletedProcess(args, returncode=0, stdout=batt_output)
        if args[:2] == ["/usr/bin/pmset", "-g"] and args[2] == "log":
            if log_output is None:
                return subprocess.CompletedProcess(args, returncode=1, stdout="")
            return subprocess.CompletedProcess(args, returncode=0, stdout=log_output)
        raise AssertionError(f"unexpected command: {args}")

    return run


def test_observe_disabled_returns_all_unknown(tmp_path: Path) -> None:
    observer = PowerObserver(_settings(observe=False), tmp_path)
    state = observer.observe(datetime(2026, 1, 1), None)
    assert state.on_ac is None
    assert state.battery_pct is None
    assert state.slept_since_last is False
    assert "disabled" in state.detail


def test_observe_on_ac_power(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "keepalive.core.power.subprocess.run", _fake_run(_BATT_ON_AC, _LOG_NO_EVENTS)
    )
    observer = PowerObserver(_settings(), tmp_path)
    state = observer.observe(datetime(2026, 1, 1), None)
    assert state.on_ac is True
    assert state.battery_pct == 85


def test_observe_on_battery(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "keepalive.core.power.subprocess.run", _fake_run(_BATT_ON_BATTERY, _LOG_NO_EVENTS)
    )
    observer = PowerObserver(_settings(), tmp_path)
    state = observer.observe(datetime(2026, 1, 1), None)
    assert state.on_ac is False
    assert state.battery_pct == 42


def test_observe_pmset_batt_unavailable_is_none_not_raise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("keepalive.core.power.subprocess.run", _fake_run(None, _LOG_NO_EVENTS))
    observer = PowerObserver(_settings(), tmp_path)
    state = observer.observe(datetime(2026, 1, 1), None)
    assert state.on_ac is None
    assert state.battery_pct is None
    assert "unavailable" in state.detail


def test_observe_subprocess_oserror_is_tolerated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def raise_oserror(args: list[str], **kwargs: Any) -> Any:
        raise OSError("no pmset binary")

    monkeypatch.setattr("keepalive.core.power.subprocess.run", raise_oserror)
    observer = PowerObserver(_settings(), tmp_path)
    state = observer.observe(datetime(2026, 1, 1), None)
    assert state.on_ac is None


def test_observe_subprocess_timeout_is_tolerated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def raise_timeout(args: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd=args, timeout=10)

    monkeypatch.setattr("keepalive.core.power.subprocess.run", raise_timeout)
    observer = PowerObserver(_settings(), tmp_path)
    state = observer.observe(datetime(2026, 1, 1), None)
    assert state.on_ac is None


def test_observe_detects_sleep_since_last_cycle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "keepalive.core.power.subprocess.run", _fake_run(_BATT_ON_AC, _LOG_WITH_SLEEP_AND_WAKE)
    )
    observer = PowerObserver(_settings(), tmp_path)
    last_cycle_at = datetime(2026, 1, 1, 9, 0, 0)
    now = datetime(2026, 1, 1, 11, 0, 0)
    state = observer.observe(now, last_cycle_at)
    assert state.slept_since_last is True
    assert state.last_sleep_at == datetime(2026, 1, 1, 10, 0, 0)
    assert state.last_wake_at == datetime(2026, 1, 1, 10, 5, 0)


def test_observe_no_sleep_events_in_window(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "keepalive.core.power.subprocess.run", _fake_run(_BATT_ON_AC, _LOG_NO_EVENTS)
    )
    observer = PowerObserver(_settings(), tmp_path)
    state = observer.observe(datetime(2026, 1, 1), None)
    assert state.slept_since_last is False
    assert state.last_sleep_at is None


def test_observe_ignores_sleep_events_before_last_cycle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The sleep/wake pair in the log is before last_cycle_at, so it must not
    # be reported as "since last cycle".
    monkeypatch.setattr(
        "keepalive.core.power.subprocess.run", _fake_run(_BATT_ON_AC, _LOG_WITH_SLEEP_AND_WAKE)
    )
    observer = PowerObserver(_settings(), tmp_path)
    last_cycle_at = datetime(2026, 1, 1, 12, 0, 0)  # after both log events
    now = datetime(2026, 1, 1, 13, 0, 0)
    state = observer.observe(now, last_cycle_at)
    assert state.slept_since_last is False


def test_observe_ignores_sleep_events_after_now(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "keepalive.core.power.subprocess.run", _fake_run(_BATT_ON_AC, _LOG_WITH_SLEEP_AND_WAKE)
    )
    observer = PowerObserver(_settings(), tmp_path)
    now = datetime(2026, 1, 1, 9, 0, 0)  # before both log events
    state = observer.observe(now, None)
    assert state.slept_since_last is False


def test_observe_pmset_log_unavailable_is_tolerated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("keepalive.core.power.subprocess.run", _fake_run(_BATT_ON_AC, None))
    observer = PowerObserver(_settings(), tmp_path)
    state = observer.observe(datetime(2026, 1, 1), None)
    assert state.slept_since_last is False
    assert "log unavailable" in state.detail


# --- WakeWindow -------------------------------------------------------------


class _FakePopen:
    def __init__(self, args: list[str], **kwargs: Any) -> None:
        self.args = args
        self._returncode: int | None = None

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self._returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        self._returncode = 0
        return 0

    def kill(self) -> None:
        self._returncode = -9


def test_hold_refused_when_wake_windows_disabled(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    window = WakeWindow(_settings(wake_windows=False), audit)
    assert window.hold("test", 10) is False


def test_hold_refused_on_battery_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "keepalive.core.power.subprocess.run", _fake_run(_BATT_ON_BATTERY, _LOG_NO_EVENTS)
    )
    audit = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    window = WakeWindow(_settings(wake_windows=True, allow_on_battery=False), audit)
    assert window.hold("test", 10) is False


def test_hold_allowed_on_battery_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "keepalive.core.power.subprocess.run", _fake_run(_BATT_ON_BATTERY, _LOG_NO_EVENTS)
    )
    monkeypatch.setattr("keepalive.core.power.subprocess.Popen", _FakePopen)
    audit = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    window = WakeWindow(_settings(wake_windows=True, allow_on_battery=True), audit)
    assert window.hold("test", 10) is True


def test_hold_starts_caffeinate_bounded_by_max_wake_minutes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "keepalive.core.power.subprocess.run", _fake_run(_BATT_ON_AC, _LOG_NO_EVENTS)
    )
    captured: dict[str, Any] = {}

    def fake_popen(args: list[str], **kwargs: Any) -> _FakePopen:
        captured["args"] = args
        return _FakePopen(args, **kwargs)

    monkeypatch.setattr("keepalive.core.power.subprocess.Popen", fake_popen)
    audit = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    window = WakeWindow(_settings(wake_windows=True, max_wake_minutes=5), audit)

    assert window.hold("job running", 999) is True
    args = captured["args"]
    assert args[0] == "/usr/bin/caffeinate"
    assert "-s" in args
    assert "-w" in args
    assert str(5 * 60) in args  # bounded to max_wake_minutes, not the requested 999


def test_hold_refused_when_already_held(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "keepalive.core.power.subprocess.run", _fake_run(_BATT_ON_AC, _LOG_NO_EVENTS)
    )
    monkeypatch.setattr("keepalive.core.power.subprocess.Popen", _FakePopen)
    audit = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    window = WakeWindow(_settings(wake_windows=True), audit)
    assert window.hold("first", 10) is True
    assert window.hold("second", 10) is False


def test_hold_caffeinate_oserror_is_caught(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "keepalive.core.power.subprocess.run", _fake_run(_BATT_ON_AC, _LOG_NO_EVENTS)
    )

    def raise_oserror(args: list[str], **kwargs: Any) -> Any:
        raise OSError("no caffeinate")

    monkeypatch.setattr("keepalive.core.power.subprocess.Popen", raise_oserror)
    audit = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    window = WakeWindow(_settings(wake_windows=True), audit)
    assert window.hold("test", 10) is False


def test_release_terminates_running_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "keepalive.core.power.subprocess.run", _fake_run(_BATT_ON_AC, _LOG_NO_EVENTS)
    )
    monkeypatch.setattr("keepalive.core.power.subprocess.Popen", _FakePopen)
    audit = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    window = WakeWindow(_settings(wake_windows=True), audit)
    window.hold("test", 10)
    window.release()  # must not raise
    # Now that it's released, holding again should succeed (no longer "already held").
    assert window.hold("again", 10) is True


def test_release_when_never_held_is_a_noop(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    window = WakeWindow(_settings(wake_windows=True), audit)
    window.release()  # must not raise
