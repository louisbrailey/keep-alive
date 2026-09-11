"""pmset log parsing must key on the event domain, not substrings; UNKNOWN gets no wake grace."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from keepalive.core import power as power_mod
from keepalive.core.adapter import AdapterManifest, CheckResult, Status
from keepalive.core.config import PowerSettings
from keepalive.core.engine import _apply_wake_grace
from keepalive.core.power import PowerObserver, PowerState


def _settings() -> PowerSettings:
    return PowerSettings(
        observe=True, wake_windows=False, allow_on_battery=False, max_wake_minutes=60
    )


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S %z")


def test_assertion_lines_mentioning_sleep_are_not_sleep_events(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    now = datetime.now().astimezone().replace(microsecond=0)
    last = now - timedelta(minutes=20)
    old_sleep = now - timedelta(hours=1)  # before the last cycle: must be ignored
    log = "\n".join(
        [
            f"{_fmt(old_sleep)} Sleep               \tEntering Sleep state due to 'Idle'",
            f"{_fmt(now - timedelta(seconds=30))} Assertions          \t"
            "PID 350(powerd) Created PreventUserIdleSystemSleep",
            f"{_fmt(now - timedelta(seconds=20))} DarkWake            \tDarkWake from Deep Idle",
        ]
    )
    monkeypatch.setattr(
        power_mod, "_run", lambda argv: log if "log" in argv else "Now drawing from 'AC Power'"
    )
    ps = PowerObserver(_settings(), tmp_path).observe(now, last)
    assert ps.slept_since_last is False
    assert ps.last_sleep_at is None


def test_real_sleep_domain_line_in_window_is_detected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    now = datetime.now().astimezone().replace(microsecond=0)
    last = now - timedelta(minutes=20)
    slept = now - timedelta(minutes=10)
    woke = now - timedelta(minutes=5)
    log = "\n".join(
        [
            f"{_fmt(slept)} Sleep               \tEntering Sleep state due to 'Idle Sleep'",
            f"{_fmt(woke)} Wake                \tWake from Deep Idle",
        ]
    )
    monkeypatch.setattr(
        power_mod, "_run", lambda argv: log if "log" in argv else "Now drawing from 'AC Power'"
    )
    ps = PowerObserver(_settings(), tmp_path).observe(now, last)
    assert ps.slept_since_last is True
    assert ps.last_sleep_at == slept and ps.last_wake_at == woke


def test_unknown_results_get_no_wake_grace() -> None:
    manifest = AdapterManifest(
        kind="x", description="", checks=(), reads=(), actions=(), notify_on=()
    )
    now = datetime.now().astimezone()
    state = PowerState(
        on_ac=True,
        battery_pct=50,
        slept_since_last=True,
        last_sleep_at=None,
        last_wake_at=None,
        detail="",
    )
    unknown = CheckResult(status=Status.UNKNOWN, detail="network")
    _apply_wake_grace(unknown, manifest, state, now)
    assert unknown.status is Status.UNKNOWN
    failed = CheckResult(status=Status.FAILED, detail="down")
    _apply_wake_grace(failed, manifest, state, now)
    assert failed.status is Status.EXPECTED_TRANSITION
