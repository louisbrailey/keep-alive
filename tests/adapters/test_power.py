"""Tests for keepalive.adapters.power.PowerAdapter."""

from __future__ import annotations

from datetime import datetime

from keepalive.adapters.power import PowerAdapter
from keepalive.core.adapter import CheckContext, Status


def _ctx(*, on_ac_power: bool | None, slept: bool) -> CheckContext:
    return CheckContext(
        service_name="power",
        params={},
        now=datetime(2024, 1, 1, 12, 0, 0),
        on_ac_power=on_ac_power,
        slept_since_last_cycle=slept,
        last_cycle_at=None,
        state_dir="/tmp/keepalive-state",
    )


def test_describe_manifest_has_no_actions_and_no_wake_transition() -> None:
    manifest = PowerAdapter.describe()
    assert manifest.kind == "power"
    assert manifest.actions == ()
    assert manifest.treat_wake_as_transition is False


def test_describe_manifest_declares_notify_conditions() -> None:
    manifest = PowerAdapter.describe()
    events = {c.event for c in manifest.notify_on}
    assert events == {"incident_opened", "recovered", "check_unavailable"}


def test_on_ac_is_ok() -> None:
    adapter = PowerAdapter({"warn_on_battery": True, "report_sleep": False})
    result = adapter.check(_ctx(on_ac_power=True, slept=False))
    assert result.status == Status.OK
    assert result.metrics["on_ac_power"] is True


def test_battery_warning_when_configured() -> None:
    adapter = PowerAdapter({"warn_on_battery": True, "report_sleep": False})
    result = adapter.check(_ctx(on_ac_power=False, slept=False))
    assert result.status == Status.DEGRADED
    assert "battery" in result.detail


def test_battery_ok_when_not_configured_to_warn() -> None:
    adapter = PowerAdapter({"warn_on_battery": False, "report_sleep": False})
    result = adapter.check(_ctx(on_ac_power=False, slept=False))
    assert result.status == Status.OK


def test_sleep_reported_in_detail() -> None:
    adapter = PowerAdapter({"warn_on_battery": False, "report_sleep": True})
    result = adapter.check(_ctx(on_ac_power=True, slept=True))
    assert result.status == Status.OK
    assert "slept" in result.detail
    assert result.metrics["slept_since_last_cycle"] is True


def test_sleep_not_mentioned_when_report_sleep_false() -> None:
    adapter = PowerAdapter({"warn_on_battery": False, "report_sleep": False})
    result = adapter.check(_ctx(on_ac_power=True, slept=True))
    assert "slept" not in result.detail


def test_unknown_ac_state_is_unknown() -> None:
    adapter = PowerAdapter({"warn_on_battery": True, "report_sleep": False})
    result = adapter.check(_ctx(on_ac_power=None, slept=False))
    assert result.status == Status.UNKNOWN


def test_validate_params_unknown_key() -> None:
    problems = PowerAdapter({"bogus": True}).validate_params()
    assert any("bogus" in p for p in problems)
