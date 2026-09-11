"""End-to-end scenario tests for keepalive.core.engine.run_cycle.

Each test drives ``run_cycle`` against the shared ``FakeAdapter`` test double
(``tests/conftest.py``), a real ``StateStore``/``AuditLog`` rooted at
``tmp_path``, and a small recording ``Notifier`` stub — with ``now`` passed
explicitly on every call so backoff/cooldown/circuit timing is deterministic.
These are black-box scenario tests (ARCHITECTURE.md sections 2.2/2.3): they
assert on the ``Decision`` the engine produced, the persisted
``ServiceState``/``Incident``, audit records, notifications sent, and whether
``adapter.recover()`` was actually called — not on policy internals directly
(those are unit-tested in tests/core/test_policy.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tests.conftest import FakeAdapter

from keepalive.core.adapter import ActionResult, CheckResult, Status
from keepalive.core.audit import AuditLog
from keepalive.core.config import (
    Config,
    KeepAliveSettings,
    NotificationSettings,
    PowerSettings,
    RecoveryPolicy,
    ServiceConfig,
)
from keepalive.core.engine import run_cycle, set_kill_switch
from keepalive.core.lock import InstanceLock
from keepalive.core.power import PowerState
from keepalive.core.state import StateStore

NOW = datetime(2026, 1, 1, 12, 0, 0)


class RecordingNotifier:
    """Stand-in for ``core.notify.Notifier`` that just records every
    ``send()`` call instead of touching osascript/webhook — the real
    Notifier's gating (notify_on, macos/webhook toggles, redaction) is
    covered by tests/core/test_notify.py; these scenario tests only care
    whether the engine *decided* to notify and with which event."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, event: str, title: str, message: str) -> bool:
        self.sent.append((event, title, message))
        return True

    def events(self) -> list[str]:
        return [e for e, _t, _m in self.sent]


def _settings(tmp_path: Path, **overrides: Any) -> KeepAliveSettings:
    base: dict[str, Any] = dict(
        mode="observe",
        poll_interval_s=60,
        state_dir=tmp_path,
        failure_threshold=2,
        unknown_threshold=2,
        audit_max_bytes=10_000_000,
    )
    base.update(overrides)
    return KeepAliveSettings(**base)


def _notification_settings(**overrides: Any) -> NotificationSettings:
    base: dict[str, Any] = dict(
        macos=False,
        webhook=False,
        webhook_url_env="KEEPALIVE_WEBHOOK_URL",
        summary_daily_at="",
        notify_on=["incident_opened", "recovered", "circuit_open", "check_unavailable"],
    )
    base.update(overrides)
    return NotificationSettings(**base)


def _power_settings(**overrides: Any) -> PowerSettings:
    base: dict[str, Any] = dict(
        observe=False, wake_windows=False, allow_on_battery=False, max_wake_minutes=120
    )
    base.update(overrides)
    return PowerSettings(**base)


def _service(name: str = "svc", **overrides: Any) -> ServiceConfig:
    base: dict[str, Any] = dict(
        name=name,
        adapter="fake",
        enabled=True,
        params={},
        recovery=RecoveryPolicy(),
    )
    base.update(overrides)
    return ServiceConfig(**base)


def _config(tmp_path: Path, services: list[ServiceConfig], **settings_overrides: Any) -> Config:
    return Config(
        version=1,
        keepalive=_settings(tmp_path, **settings_overrides),
        notifications=_notification_settings(),
        power=_power_settings(),
        services=services,
    )


def _rig(tmp_path: Path) -> tuple[StateStore, AuditLog, RecordingNotifier]:
    state_store = StateStore(tmp_path / "state.json")
    audit = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    notifier = RecordingNotifier()
    return state_store, audit, notifier


def _run(
    config: Config,
    rig: tuple[StateStore, AuditLog, RecordingNotifier],
    adapter: FakeAdapter,
    *,
    now: datetime,
    power: Any = None,
) -> Any:
    state_store, audit, notifier = rig
    return run_cycle(
        config,
        state_store=state_store,
        audit=audit,
        notifier=notifier,
        power=power,
        now=now,
        adapter_factory=lambda _svc: adapter,
    )


# ---------------------------------------------------------------------------
# 1. Normal health — no notifications, audit only.
# ---------------------------------------------------------------------------


def test_normal_health_produces_no_notifications_audit_only(tmp_path: Path) -> None:
    adapter = FakeAdapter({})
    adapter.results = [CheckResult(status=Status.OK, detail="all good")]
    service = _service()
    config = _config(tmp_path, [service])
    rig = _rig(tmp_path)

    report = _run(config, rig, adapter, now=NOW)

    assert report.ran is True
    assert report.decisions[0].kind == "noop"
    _state_store, audit, notifier = rig
    assert notifier.sent == []
    events = [r["event"] for r in audit.tail(100)]
    assert "check" in events
    assert "decision" in events
    assert "notify" not in events
    saved = _state_store.load()
    assert saved["svc"].status == "ok"
    assert saved["svc"].incident is None
    assert adapter.recover_calls == []


# ---------------------------------------------------------------------------
# 2. Service crash — incident opens after failure_threshold, one notification,
#    recovery attempted only in mode=recover with recovery.enabled and the
#    action allow-listed, otherwise suppressed(observe_only).
# ---------------------------------------------------------------------------


def _crash_adapter() -> FakeAdapter:
    adapter = FakeAdapter({})
    adapter.results = [CheckResult(status=Status.FAILED, detail="process died")]
    return adapter


def test_service_crash_opens_incident_with_single_notification_in_observe_mode(
    tmp_path: Path,
) -> None:
    adapter = _crash_adapter()
    service = _service()
    config = _config(tmp_path, [service], mode="observe", failure_threshold=2)
    rig = _rig(tmp_path)

    r1 = _run(config, rig, adapter, now=NOW)
    assert r1.decisions[0].kind == "count"

    r2 = _run(config, rig, adapter, now=NOW)
    assert r2.decisions[0].kind == "suppressed"
    assert r2.decisions[0].reason == "observe_only"

    _state_store, _audit, notifier = rig
    assert notifier.events() == ["incident_opened"]
    saved = _state_store.load()
    assert saved["svc"].incident is not None
    assert saved["svc"].incident.kind == "failure"
    assert adapter.recover_calls == []

    # A further crashed cycle must not notify a second time.
    r3 = _run(config, rig, adapter, now=NOW)
    assert r3.decisions[0].kind == "suppressed"
    assert notifier.events() == ["incident_opened"]


def test_crash_recovery_suppressed_when_mode_recover_but_recovery_disabled(
    tmp_path: Path,
) -> None:
    adapter = _crash_adapter()
    service = _service(
        recovery=RecoveryPolicy(enabled=False, allowed_actions=["restart"], max_attempts=3)
    )
    config = _config(tmp_path, [service], mode="recover", failure_threshold=2)
    rig = _rig(tmp_path)

    _run(config, rig, adapter, now=NOW)
    report = _run(config, rig, adapter, now=NOW)

    assert report.decisions[0].kind == "suppressed"
    assert report.decisions[0].reason == "observe_only"
    assert adapter.recover_calls == []


def test_crash_recovery_suppressed_when_recovery_enabled_but_mode_observe(
    tmp_path: Path,
) -> None:
    adapter = _crash_adapter()
    service = _service(
        recovery=RecoveryPolicy(enabled=True, allowed_actions=["restart"], max_attempts=3)
    )
    config = _config(tmp_path, [service], mode="observe", failure_threshold=2)
    rig = _rig(tmp_path)

    _run(config, rig, adapter, now=NOW)
    report = _run(config, rig, adapter, now=NOW)

    assert report.decisions[0].kind == "suppressed"
    assert report.decisions[0].reason == "observe_only"
    assert adapter.recover_calls == []


def test_crash_recovery_attempted_when_mode_recover_and_enabled_and_action_allowed(
    tmp_path: Path,
) -> None:
    adapter = _crash_adapter()
    # "signal" is allowed_during_active_job and does not start an instance,
    # so nothing else blocks it here.
    service = _service(
        recovery=RecoveryPolicy(enabled=True, allowed_actions=["signal"], max_attempts=3)
    )
    config = _config(tmp_path, [service], mode="recover", failure_threshold=2)
    rig = _rig(tmp_path)

    _run(config, rig, adapter, now=NOW)
    report = _run(config, rig, adapter, now=NOW)

    assert report.decisions[0].kind == "attempt"
    assert report.decisions[0].action == "signal"
    assert len(adapter.recover_calls) == 1
    assert adapter.recover_calls[0][0] == "signal"


# ---------------------------------------------------------------------------
# 3. Duplicate-instance prevention — a starts_instance action is suppressed
#    while instance_present/lock_held, and NOT counted as an attempt.
# ---------------------------------------------------------------------------


def test_duplicate_instance_prevention_not_counted_as_attempt(tmp_path: Path) -> None:
    adapter = FakeAdapter({})
    adapter.results = [CheckResult(status=Status.FAILED, detail="down", instance_present=True)]
    service = _service(
        recovery=RecoveryPolicy(enabled=True, allowed_actions=["restart"], max_attempts=3)
    )
    config = _config(tmp_path, [service], mode="recover", failure_threshold=1)
    rig = _rig(tmp_path)

    r1 = _run(config, rig, adapter, now=NOW)
    assert r1.decisions[0].kind == "suppressed"
    assert r1.decisions[0].reason == "instance_present"
    assert adapter.recover_calls == []

    r2 = _run(config, rig, adapter, now=NOW)
    assert r2.decisions[0].kind == "suppressed"
    assert r2.decisions[0].reason == "instance_present"
    assert adapter.recover_calls == []

    incident = r2.decisions[0].state.incident
    assert incident is not None
    assert incident.attempts == 0  # never counted as an attempt
    assert incident.cooldown_until is None
    assert incident.next_retry_at is None


def test_duplicate_instance_prevention_lock_held(tmp_path: Path) -> None:
    adapter = FakeAdapter({})
    adapter.results = [CheckResult(status=Status.FAILED, detail="down", lock_held=True)]
    service = _service(
        recovery=RecoveryPolicy(enabled=True, allowed_actions=["restart"], max_attempts=3)
    )
    config = _config(tmp_path, [service], mode="recover", failure_threshold=1)
    rig = _rig(tmp_path)

    report = _run(config, rig, adapter, now=NOW)

    assert report.decisions[0].kind == "suppressed"
    assert report.decisions[0].reason == "lock_held"
    assert adapter.recover_calls == []


def test_duplicate_instance_prevention_handoff_in_progress(tmp_path: Path) -> None:
    adapter = FakeAdapter({})
    adapter.results = [
        CheckResult(status=Status.FAILED, detail="down", handoff_until=NOW + timedelta(seconds=30))
    ]
    service = _service(
        recovery=RecoveryPolicy(enabled=True, allowed_actions=["restart"], max_attempts=3)
    )
    config = _config(tmp_path, [service], mode="recover", failure_threshold=1)
    rig = _rig(tmp_path)

    report = _run(config, rig, adapter, now=NOW)

    assert report.decisions[0].kind == "suppressed"
    assert report.decisions[0].reason == "handoff_in_progress"
    assert adapter.recover_calls == []


# ---------------------------------------------------------------------------
# 4. Stale heartbeat — FAILED results open an incident.
# ---------------------------------------------------------------------------


def test_stale_heartbeat_failed_result_opens_incident(tmp_path: Path) -> None:
    adapter = FakeAdapter({})
    adapter.results = [CheckResult(status=Status.FAILED, detail="heartbeat stale 400s")]
    service = _service()
    config = _config(tmp_path, [service], mode="observe", failure_threshold=1)
    rig = _rig(tmp_path)

    report = _run(config, rig, adapter, now=NOW)

    assert report.decisions[0].kind == "suppressed"
    incident = report.decisions[0].state.incident
    assert incident is not None
    assert incident.kind == "failure"
    _state_store, _audit, notifier = rig
    assert notifier.events() == ["incident_opened"]


# ---------------------------------------------------------------------------
# 5. Planned restart handoff — EXPECTED_TRANSITION with a future
#    transition_until is "wait" and no failure is counted; it becomes FAILED
#    (and counts) only after it expires.
# ---------------------------------------------------------------------------


def test_planned_handoff_waits_then_counts_as_failure_after_expiry(tmp_path: Path) -> None:
    adapter = FakeAdapter({})
    still_future = NOW + timedelta(seconds=30)
    already_past = NOW + timedelta(seconds=10)  # < the cycle-2 "now" below
    adapter.results = [
        CheckResult(status=Status.EXPECTED_TRANSITION, transition_until=still_future),
        CheckResult(status=Status.EXPECTED_TRANSITION, transition_until=already_past),
    ]
    service = _service()
    config = _config(tmp_path, [service], mode="observe", failure_threshold=1)
    rig = _rig(tmp_path)

    r1 = _run(config, rig, adapter, now=NOW)
    assert r1.decisions[0].kind == "wait"
    assert r1.decisions[0].reason == "expected_transition"
    state_store, _audit, notifier = rig
    saved1 = state_store.load()
    assert saved1["svc"].consecutive_failures == 0
    assert saved1["svc"].incident is None
    assert notifier.sent == []

    # Same claimed transition, but now the expiry has passed: no longer credible.
    r2 = _run(config, rig, adapter, now=NOW + timedelta(seconds=60))
    assert r2.decisions[0].kind == "suppressed"  # threshold(1) reached -> observe_only
    assert r2.decisions[0].reason == "observe_only"
    saved2 = state_store.load()
    assert saved2["svc"].incident is not None
    assert saved2["svc"].incident.kind == "failure"


# ---------------------------------------------------------------------------
# 6. Long-running job protection — active_job=True suppresses an action with
#    allowed_during_active_job=False and permits one with True.
# ---------------------------------------------------------------------------


def test_active_job_suppresses_action_without_allowed_during_active_job(tmp_path: Path) -> None:
    adapter = FakeAdapter({})
    adapter.results = [CheckResult(status=Status.FAILED, detail="stuck", active_job=True)]
    service = _service(
        recovery=RecoveryPolicy(enabled=True, allowed_actions=["restart"], max_attempts=3)
    )
    config = _config(tmp_path, [service], mode="recover", failure_threshold=1)
    rig = _rig(tmp_path)

    report = _run(config, rig, adapter, now=NOW)

    assert report.decisions[0].kind == "suppressed"
    assert report.decisions[0].reason == "active_job"
    assert adapter.recover_calls == []


def test_active_job_permits_action_with_allowed_during_active_job(tmp_path: Path) -> None:
    adapter = FakeAdapter({})
    adapter.results = [CheckResult(status=Status.FAILED, detail="stuck", active_job=True)]
    service = _service(
        recovery=RecoveryPolicy(enabled=True, allowed_actions=["signal"], max_attempts=3)
    )
    config = _config(tmp_path, [service], mode="recover", failure_threshold=1)
    rig = _rig(tmp_path)

    report = _run(config, rig, adapter, now=NOW)

    assert report.decisions[0].kind == "attempt"
    assert report.decisions[0].action == "signal"
    assert len(adapter.recover_calls) == 1


# ---------------------------------------------------------------------------
# 7. Sleep/wake recovery — the first post-wake FAILED result of a
#    treat_wake_as_transition adapter is marked as an expected transition,
#    then normal failure counting resumes.
# ---------------------------------------------------------------------------


class _SleptOnceThenAwakePower:
    """Reports a sleep only on its first call; a plain awake cycle after."""

    def __init__(self) -> None:
        self.calls = 0

    def observe(self, now: datetime, last_cycle_at: datetime | None) -> PowerState:
        self.calls += 1
        slept = self.calls == 1
        return PowerState(
            on_ac=True,
            battery_pct=100,
            slept_since_last=slept,
            last_sleep_at=now if slept else None,
            last_wake_at=now if slept else None,
            detail="",
        )


def test_sleep_wake_grace_then_normal_counting_resumes(tmp_path: Path) -> None:
    adapter = FakeAdapter({})
    adapter.results = [
        CheckResult(status=Status.FAILED, detail="stale heartbeat"),  # cycle 1: post-wake
        CheckResult(status=Status.FAILED, detail="stale heartbeat"),  # cycle 2: awake
        CheckResult(status=Status.FAILED, detail="stale heartbeat"),  # cycle 3: awake
    ]
    service = _service()
    config = _config(tmp_path, [service], mode="observe", failure_threshold=2)
    rig = _rig(tmp_path)
    power = _SleptOnceThenAwakePower()

    r1 = _run(config, rig, adapter, now=NOW, power=power)
    assert r1.decisions[0].kind == "wait"
    assert r1.services[0]["status"] == "expected_transition"
    state_store, _audit, _notifier = rig
    assert state_store.load()["svc"].consecutive_failures == 0

    r2 = _run(config, rig, adapter, now=NOW + timedelta(minutes=1), power=power)
    assert r2.decisions[0].kind == "count"  # normal counting: 1st real failure
    assert state_store.load()["svc"].consecutive_failures == 1

    r3 = _run(config, rig, adapter, now=NOW + timedelta(minutes=2), power=power)
    assert r3.decisions[0].kind == "suppressed"  # threshold reached: 2nd real failure
    assert state_store.load()["svc"].consecutive_failures == 2
    assert state_store.load()["svc"].incident is not None


# ---------------------------------------------------------------------------
# 8. Network failure — UNKNOWN results never trigger recovery; a
#    check_unavailable incident opens only after unknown_threshold; notify once.
# ---------------------------------------------------------------------------


def test_network_failure_unknown_never_recovers_opens_incident_after_threshold(
    tmp_path: Path,
) -> None:
    adapter = FakeAdapter({})
    adapter.results = [CheckResult(status=Status.UNKNOWN, detail="connection refused")]
    # mode=recover with recovery enabled, to prove UNKNOWN still never recovers.
    service = _service(
        recovery=RecoveryPolicy(enabled=True, allowed_actions=["signal"], max_attempts=3)
    )
    config = _config(tmp_path, [service], mode="recover", unknown_threshold=2)
    rig = _rig(tmp_path)

    r1 = _run(config, rig, adapter, now=NOW)
    assert r1.decisions[0].kind == "count"
    assert r1.decisions[0].reason == "unknown_below_threshold"

    r2 = _run(config, rig, adapter, now=NOW)
    assert r2.decisions[0].kind == "open_incident"
    assert r2.decisions[0].reason == "check_unavailable"
    incident = r2.decisions[0].state.incident
    assert incident is not None
    assert incident.kind == "check_unavailable"

    r3 = _run(config, rig, adapter, now=NOW)
    assert r3.decisions[0].kind == "wait"
    assert r3.decisions[0].reason == "check_unavailable"

    _state_store, _audit, notifier = rig
    assert notifier.events() == ["check_unavailable"]
    assert adapter.recover_calls == []  # UNKNOWN never triggers recovery


# ---------------------------------------------------------------------------
# 9. Disk-space warning — DEGRADED counts toward the threshold and opens an
#    incident.
# ---------------------------------------------------------------------------


def test_degraded_counts_toward_threshold_and_opens_incident(tmp_path: Path) -> None:
    adapter = FakeAdapter({})
    adapter.results = [CheckResult(status=Status.DEGRADED, detail="12% free")]
    service = _service()
    config = _config(tmp_path, [service], mode="observe", failure_threshold=1)
    rig = _rig(tmp_path)

    report = _run(config, rig, adapter, now=NOW)

    assert report.services[0]["status"] == "degraded"
    assert report.decisions[0].kind == "suppressed"
    assert report.decisions[0].reason == "observe_only"
    incident = report.decisions[0].state.incident
    assert incident is not None
    assert incident.kind == "failure"
    _state_store, _audit, notifier = rig
    assert notifier.events() == ["incident_opened"]


# ---------------------------------------------------------------------------
# 10. Failed recovery cooldown — a failed attempt sets cooldown_until and
#     next_retry_at with exponential backoff, no re-attempt inside cooldown,
#     circuit opens after max_attempts with a circuit_open notification, and
#     a half-open probe is allowed after circuit_open_until.
# ---------------------------------------------------------------------------


def test_failed_recovery_cooldown_backoff_circuit_then_half_open_probe(tmp_path: Path) -> None:
    adapter = FakeAdapter({})
    adapter.results = [CheckResult(status=Status.FAILED, detail="still down")]
    adapter.recover_result = ActionResult(ok=False, detail="restart failed")
    recovery = RecoveryPolicy(
        enabled=True,
        allowed_actions=["restart"],
        cooldown_s=60,
        max_attempts=2,
        backoff_base_s=30,
        backoff_max_s=900,
        circuit_open_s=3600,
    )
    service = _service(recovery=recovery)
    config = _config(tmp_path, [service], mode="recover", failure_threshold=1)
    rig = _rig(tmp_path)

    # Cycle 1: threshold reached immediately -> first attempt.
    r1 = _run(config, rig, adapter, now=NOW)
    assert r1.decisions[0].kind == "attempt"
    inc1 = r1.decisions[0].state.incident
    assert inc1 is not None
    assert inc1.attempts == 1
    assert inc1.cooldown_until == NOW + timedelta(seconds=60)
    assert inc1.next_retry_at == NOW + timedelta(seconds=30)
    assert len(adapter.recover_calls) == 1
    assert inc1.last_action_ok is False

    # Cycle 2: still inside cooldown -> wait, no new attempt.
    r2 = _run(config, rig, adapter, now=NOW + timedelta(seconds=10))
    assert r2.decisions[0].kind == "wait"
    assert r2.decisions[0].reason == "cooldown"
    assert len(adapter.recover_calls) == 1

    # Cycle 3: past cooldown and backoff -> second attempt (exponential backoff).
    now3 = NOW + timedelta(seconds=70)
    r3 = _run(config, rig, adapter, now=now3)
    assert r3.decisions[0].kind == "attempt"
    inc3 = r3.decisions[0].state.incident
    assert inc3 is not None
    assert inc3.attempts == 2
    assert inc3.next_retry_at == now3 + timedelta(seconds=60)  # 30 * 2**1
    assert inc3.cooldown_until == now3 + timedelta(seconds=60)
    assert len(adapter.recover_calls) == 2

    # Cycle 4: past cooldown/backoff again, attempts >= max_attempts -> circuit opens.
    now4 = now3 + timedelta(seconds=70)
    r4 = _run(config, rig, adapter, now=now4)
    assert r4.decisions[0].kind == "circuit_open"
    inc4 = r4.decisions[0].state.incident
    assert inc4 is not None
    assert inc4.circuit_open_until == now4 + timedelta(seconds=3600)
    assert len(adapter.recover_calls) == 2  # circuit_open performs no recover() call
    _state_store, _audit, notifier = rig
    assert notifier.events() == ["incident_opened", "circuit_open"]

    # Cycle 5: still inside the open circuit -> wait, no attempt.
    now5 = now4 + timedelta(seconds=10)
    r5 = _run(config, rig, adapter, now=now5)
    assert r5.decisions[0].kind == "wait"
    assert r5.decisions[0].reason == "circuit_open"
    assert len(adapter.recover_calls) == 2

    # Cycle 6: after circuit_open_until -> half-open probe, one more attempt.
    now6 = now4 + timedelta(seconds=3601)
    r6 = _run(config, rig, adapter, now=now6)
    assert r6.decisions[0].kind == "half_open_probe"
    inc6 = r6.decisions[0].state.incident
    assert inc6 is not None
    assert inc6.attempts == 1  # reset on half-open entry, then incremented
    assert inc6.circuit_open_until is None
    assert len(adapter.recover_calls) == 3


# ---------------------------------------------------------------------------
# 11. Kill switch — with <state_dir>/KILL present every action is
#     suppressed(kill_switch) but checks/audit continue; removing it restores
#     normal behaviour.
# ---------------------------------------------------------------------------


def test_kill_switch_suppresses_actions_but_checks_and_audit_continue(tmp_path: Path) -> None:
    adapter = FakeAdapter({})
    adapter.results = [CheckResult(status=Status.FAILED, detail="down")]
    service = _service(
        recovery=RecoveryPolicy(enabled=True, allowed_actions=["signal"], max_attempts=3)
    )
    config = _config(tmp_path, [service], mode="recover", failure_threshold=1)
    rig = _rig(tmp_path)
    _state_store, audit, notifier = rig
    set_kill_switch(config.keepalive, on=True, reason="scenario test")

    r1 = _run(config, rig, adapter, now=NOW)
    assert r1.ran is True
    assert r1.decisions[0].kind == "suppressed"
    assert r1.decisions[0].reason == "kill_switch"
    assert adapter.recover_calls == []
    events = [r["event"] for r in audit.tail(100)]
    assert "check" in events
    assert "decision" in events
    assert notifier.events() == ["incident_opened"]  # informational bookkeeping still fires

    # A second cycle under the kill switch: already notified, still suppressed,
    # never counted as an attempt.
    r2 = _run(config, rig, adapter, now=NOW + timedelta(seconds=5))
    assert r2.decisions[0].kind == "suppressed"
    assert r2.decisions[0].reason == "kill_switch"
    incident = r2.decisions[0].state.incident
    assert incident is not None
    assert incident.attempts == 0
    assert adapter.recover_calls == []

    # Removing the kill switch restores normal recovery behaviour.
    set_kill_switch(config.keepalive, on=False, reason="")
    assert not (config.keepalive.state_dir / "KILL").exists()
    r3 = _run(config, rig, adapter, now=NOW + timedelta(seconds=10))
    assert r3.decisions[0].kind == "attempt"
    assert len(adapter.recover_calls) == 1


# ---------------------------------------------------------------------------
# 12. Overlapping-run prevention — a held InstanceLock makes run_cycle
#     return ran=False with a skipped_reason.
# ---------------------------------------------------------------------------


def test_overlapping_run_prevention_returns_ran_false(tmp_path: Path) -> None:
    adapter = FakeAdapter({})
    service = _service()
    config = _config(tmp_path, [service])
    rig = _rig(tmp_path)
    lock_path = config.keepalive.state_dir / "keepalive.lock"

    with InstanceLock(lock_path) as held:
        assert held.acquired is True
        report = _run(config, rig, adapter, now=NOW)

    assert report.ran is False
    assert report.skipped_reason == "lock_held"
    assert report.decisions == []
    assert report.services == []
    assert adapter.results == [] or adapter.recover_calls == []  # check() never reached
    _state_store, audit, notifier = rig
    events = [r["event"] for r in audit.tail(100)]
    assert events == ["overlap_skipped"]
    assert notifier.sent == []

    # Lock released: the next cycle runs normally.
    report2 = _run(config, rig, adapter, now=NOW)
    assert report2.ran is True
