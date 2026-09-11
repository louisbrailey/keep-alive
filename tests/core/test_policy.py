"""Exhaustive tests of keepalive.core.policy.decide against every row of
ARCHITECTURE.md section 2.2. ``decide`` is pure, so these tests call it
directly with hand-built state/result/policy/settings/manifest — no I/O,
no adapters, no engine.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from keepalive.core.adapter import (
    ActionSpec,
    AdapterManifest,
    CheckResult,
    Status,
)
from keepalive.core.config import KeepAliveSettings, RecoveryPolicy
from keepalive.core.policy import decide
from keepalive.core.state import Incident, ServiceState

NOW = datetime(2026, 1, 1, 12, 0, 0)

_RESTART = ActionSpec(
    name="restart",
    description="restart the thing",
    allowed_during_active_job=False,
    starts_instance=True,
)
_SIGNAL = ActionSpec(
    name="signal",
    description="send a signal",
    allowed_during_active_job=True,
    starts_instance=False,
)
_DESTRUCTIVE = ActionSpec(
    name="signal_term",
    description="send SIGTERM (destructive)",
    allowed_during_active_job=True,
    starts_instance=False,
    destructive=True,
)

_MANIFEST = AdapterManifest(
    kind="fake",
    description="test adapter",
    checks=(),
    reads=(),
    actions=(_RESTART, _SIGNAL, _DESTRUCTIVE),
    notify_on=(),
    treat_wake_as_transition=True,
)


def _settings(**overrides: Any) -> KeepAliveSettings:
    base: dict[str, Any] = dict(
        mode="observe",
        poll_interval_s=60,
        state_dir=None,
        failure_threshold=3,
        unknown_threshold=2,
        audit_max_bytes=10_000_000,
    )
    base.update(overrides)
    return KeepAliveSettings(**base)


def _policy(**overrides: Any) -> RecoveryPolicy:
    base: dict[str, Any] = dict(
        enabled=True,
        allowed_actions=["restart"],
        cooldown_s=300,
        max_attempts=3,
        backoff_base_s=30,
        backoff_max_s=900,
        circuit_open_s=3600,
        failure_threshold=None,
    )
    base.update(overrides)
    return RecoveryPolicy(**base)


def _state(**overrides: Any) -> ServiceState:
    base: dict[str, Any] = dict(name="svc")
    base.update(overrides)
    return ServiceState(**base)


def _result(status: Status, **overrides: Any) -> CheckResult:
    base: dict[str, Any] = dict(status=status, detail="")
    base.update(overrides)
    return CheckResult(**base)


def _failed_state_at_threshold(threshold: int = 3) -> ServiceState:
    """A state that has just reached the failure threshold, mid-decide."""
    return _state(consecutive_failures=threshold - 1)


# --- OK ----------------------------------------------------------------


def test_ok_from_fresh_state_is_noop_and_resets_counters() -> None:
    state = _state(consecutive_failures=2, consecutive_unknown=1)
    decision = decide(
        state,
        _result(Status.OK),
        _policy(),
        _settings(),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="observe",
    )
    assert decision.kind == "noop"
    assert decision.notify is None
    assert decision.state.consecutive_failures == 0
    assert decision.state.consecutive_unknown == 0
    assert decision.state.last_healthy_at == NOW
    assert decision.state.status == "ok"


def test_ok_closes_open_incident_and_notifies_only_if_opened() -> None:
    incident = Incident(kind="failure", opened_at=NOW, notified_opened=True)
    state = _state(incident=incident, consecutive_failures=5)
    decision = decide(
        state,
        _result(Status.OK),
        _policy(),
        _settings(),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="observe",
    )
    assert decision.kind == "recovered"
    assert decision.notify == "recovered"
    assert decision.state.incident is None


def test_ok_closes_incident_without_notify_if_never_notified() -> None:
    # An incident that exists but never crossed notified_opened=True (should
    # not really happen given the state machine, but decide() must not crash
    # and must not fabricate a "recovered" notification for a silent incident).
    incident = Incident(kind="failure", opened_at=NOW, notified_opened=False)
    state = _state(incident=incident)
    decision = decide(
        state,
        _result(Status.OK),
        _policy(),
        _settings(),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="observe",
    )
    assert decision.kind == "recovered"
    assert decision.notify is None


def test_decide_does_not_mutate_input_state() -> None:
    incident = Incident(kind="failure", opened_at=NOW)
    state = _state(incident=incident, consecutive_failures=1)
    decide(
        state,
        _result(Status.OK),
        _policy(),
        _settings(),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="observe",
    )
    assert state.incident is incident  # original untouched
    assert state.consecutive_failures == 1


# --- EXPECTED_TRANSITION -------------------------------------------------


def test_expected_transition_future_is_wait_and_not_counted() -> None:
    state = _state(consecutive_failures=0)
    result = _result(Status.EXPECTED_TRANSITION, transition_until=NOW + timedelta(seconds=60))
    decision = decide(
        state, result, _policy(), _settings(), _MANIFEST, NOW, kill_switch=False, mode="observe"
    )
    assert decision.kind == "wait"
    assert decision.reason == "expected_transition"
    assert decision.state.consecutive_failures == 0


def test_expected_transition_future_aware_datetimes_do_not_raise() -> None:
    """Regression: decide() must never raise TypeError when the adapter's
    `transition_until` and the caller's `now` are both timezone-aware (the
    normal case in production, since keepalive.core.timeutil guarantees every
    datetime crossing a module boundary is aware) -- policy.py performs bare
    `>`/`<` comparisons on whatever it's handed, so mixing a naive and an
    aware datetime anywhere in this call would crash a cycle."""
    aware_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    state = _state(consecutive_failures=0)
    result = _result(Status.EXPECTED_TRANSITION, transition_until=aware_now + timedelta(seconds=60))
    decision = decide(
        state,
        result,
        _policy(),
        _settings(),
        _MANIFEST,
        aware_now,
        kill_switch=False,
        mode="observe",
    )
    assert decision.kind == "wait"
    assert decision.reason == "expected_transition"


def test_expected_transition_expired_is_treated_as_failure() -> None:
    state = _state(consecutive_failures=0)
    result = _result(Status.EXPECTED_TRANSITION, transition_until=NOW - timedelta(seconds=1))
    decision = decide(
        state,
        result,
        _policy(),
        _settings(failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="observe",
    )
    assert decision.kind == "count"
    assert decision.state.consecutive_failures == 1


def test_expected_transition_with_no_until_is_treated_as_failure() -> None:
    state = _state()
    result = _result(Status.EXPECTED_TRANSITION, transition_until=None)
    decision = decide(
        state,
        result,
        _policy(),
        _settings(failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="observe",
    )
    assert decision.kind == "count"


# --- FAILED/DEGRADED below threshold -------------------------------------


@pytest.mark.parametrize("status", [Status.FAILED, Status.DEGRADED])
def test_failure_below_threshold_counts_without_action(status: Status) -> None:
    state = _state(consecutive_failures=0)
    decision = decide(
        state,
        _result(status),
        _policy(),
        _settings(failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "count"
    assert decision.reason == "below_threshold"
    assert decision.state.consecutive_failures == 1
    assert decision.state.incident is None
    assert decision.notify is None


def test_failure_count_uses_per_service_threshold_override() -> None:
    state = _state(consecutive_failures=0)
    decision = decide(
        state,
        _result(Status.FAILED),
        _policy(failure_threshold=1),
        _settings(failure_threshold=99),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="observe",
    )
    # With an override of 1, a single failure reaches threshold immediately.
    assert decision.kind == "suppressed"
    assert decision.reason == "observe_only"


def test_unknown_status_does_not_touch_failure_counter() -> None:
    # UNKNOWN means the check itself couldn't run; it neither confirms nor
    # clears a prior FAILED/DEGRADED streak, so consecutive_failures is left
    # exactly as it was.
    state = _state(consecutive_failures=2)
    decision = decide(
        state,
        _result(Status.UNKNOWN),
        _policy(),
        _settings(unknown_threshold=5),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="observe",
    )
    assert decision.state.consecutive_failures == 2


def test_failure_resets_unknown_counter() -> None:
    state = _state(consecutive_unknown=2, consecutive_failures=0)
    decision = decide(
        state,
        _result(Status.FAILED),
        _policy(),
        _settings(failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="observe",
    )
    assert decision.state.consecutive_unknown == 0


# --- threshold reached: observe / recovery disabled -----------------------


def test_threshold_reached_observe_mode_opens_incident_suppressed() -> None:
    state = _failed_state_at_threshold(3)
    decision = decide(
        state,
        _result(Status.FAILED),
        _policy(enabled=True),
        _settings(mode="observe", failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="observe",
    )
    assert decision.kind == "suppressed"
    assert decision.reason == "observe_only"
    assert decision.notify == "incident_opened"
    assert decision.state.incident is not None
    assert decision.state.incident.kind == "failure"
    assert decision.state.incident.notified_opened is True


def test_threshold_reached_recovery_disabled_suppressed() -> None:
    state = _failed_state_at_threshold(3)
    decision = decide(
        state,
        _result(Status.FAILED),
        _policy(enabled=False),
        _settings(mode="recover", failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "suppressed"
    assert decision.reason == "observe_only"


def test_incident_opened_notification_fires_only_once() -> None:
    state = _failed_state_at_threshold(3)
    first = decide(
        state,
        _result(Status.FAILED),
        _policy(enabled=False),
        _settings(failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="observe",
    )
    assert first.notify == "incident_opened"

    second = decide(
        first.state,
        _result(Status.FAILED),
        _policy(enabled=False),
        _settings(failure_threshold=3),
        _MANIFEST,
        NOW + timedelta(seconds=1),
        kill_switch=False,
        mode="observe",
    )
    assert second.kind == "suppressed"
    assert second.notify is None


# --- kill switch ------------------------------------------------------


def test_kill_switch_suppresses_even_with_recovery_enabled() -> None:
    state = _failed_state_at_threshold(3)
    decision = decide(
        state,
        _result(Status.FAILED),
        _policy(enabled=True),
        _settings(mode="recover", failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=True,
        mode="recover",
    )
    assert decision.kind == "suppressed"
    assert decision.reason == "kill_switch"
    assert decision.notify == "incident_opened"
    assert decision.action is None


def test_kill_switch_does_not_block_incident_bookkeeping_below_threshold() -> None:
    state = _state(consecutive_failures=0)
    decision = decide(
        state,
        _result(Status.FAILED),
        _policy(enabled=True),
        _settings(failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=True,
        mode="recover",
    )
    assert decision.kind == "count"
    assert decision.state.consecutive_failures == 1


def test_kill_switch_does_not_block_recovered_notification() -> None:
    incident = Incident(kind="failure", opened_at=NOW, notified_opened=True)
    state = _state(incident=incident)
    decision = decide(
        state,
        _result(Status.OK),
        _policy(),
        _settings(),
        _MANIFEST,
        NOW,
        kill_switch=True,
        mode="recover",
    )
    assert decision.kind == "recovered"
    assert decision.notify == "recovered"


# --- action preconditions: suppressed, not counted as an attempt ---------


def test_active_job_blocks_action_not_allowed_during_job() -> None:
    state = _failed_state_at_threshold(3)
    result = _result(Status.FAILED, active_job=True)
    decision = decide(
        state,
        result,
        _policy(enabled=True, allowed_actions=["restart"]),
        _settings(mode="recover", failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "suppressed"
    assert decision.reason == "active_job"
    assert decision.state.incident.attempts == 0  # type: ignore[union-attr]
    assert decision.state.incident.next_retry_at is None  # type: ignore[union-attr]


def test_active_job_allowed_action_still_proceeds() -> None:
    state = _failed_state_at_threshold(3)
    result = _result(Status.FAILED, active_job=True)
    decision = decide(
        state,
        result,
        _policy(enabled=True, allowed_actions=["signal"]),
        _settings(mode="recover", failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "attempt"
    assert decision.action == "signal"


def test_instance_present_blocks_starts_instance_action() -> None:
    state = _failed_state_at_threshold(3)
    result = _result(Status.FAILED, instance_present=True)
    decision = decide(
        state,
        result,
        _policy(enabled=True, allowed_actions=["restart"]),
        _settings(mode="recover", failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "suppressed"
    assert decision.reason == "instance_present"


def test_lock_held_blocks_starts_instance_action() -> None:
    state = _failed_state_at_threshold(3)
    result = _result(Status.FAILED, lock_held=True)
    decision = decide(
        state,
        result,
        _policy(enabled=True, allowed_actions=["restart"]),
        _settings(mode="recover", failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "suppressed"
    assert decision.reason == "lock_held"


def test_handoff_in_progress_blocks_starts_instance_action() -> None:
    state = _failed_state_at_threshold(3)
    result = _result(Status.FAILED, handoff_until=NOW + timedelta(seconds=30))
    decision = decide(
        state,
        result,
        _policy(enabled=True, allowed_actions=["restart"]),
        _settings(mode="recover", failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "suppressed"
    assert decision.reason == "handoff_in_progress"


def test_expired_handoff_does_not_block_action() -> None:
    state = _failed_state_at_threshold(3)
    result = _result(Status.FAILED, handoff_until=NOW - timedelta(seconds=1))
    decision = decide(
        state,
        result,
        _policy(enabled=True, allowed_actions=["restart"]),
        _settings(mode="recover", failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "attempt"
    assert decision.action == "restart"


def test_suppressed_precondition_not_counted_as_attempt() -> None:
    state = _failed_state_at_threshold(3)
    result = _result(Status.FAILED, instance_present=True)
    decision = decide(
        state,
        result,
        _policy(enabled=True, allowed_actions=["restart"], max_attempts=1),
        _settings(mode="recover", failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "suppressed"
    incident = decision.state.incident
    assert incident is not None
    assert incident.attempts == 0
    assert incident.cooldown_until is None
    # Re-evaluated next cycle: since it was never counted, it should still be
    # eligible to attempt once the precondition clears.
    follow_up = decide(
        decision.state,
        _result(Status.FAILED),
        _policy(enabled=True, allowed_actions=["restart"], max_attempts=1),
        _settings(mode="recover", failure_threshold=3),
        _MANIFEST,
        NOW + timedelta(seconds=1),
        kill_switch=False,
        mode="recover",
    )
    assert follow_up.kind == "attempt"


def test_no_allowed_action_configured_is_suppressed() -> None:
    state = _failed_state_at_threshold(3)
    decision = decide(
        state,
        _result(Status.FAILED),
        _policy(enabled=True, allowed_actions=[]),
        _settings(mode="recover", failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "suppressed"
    assert decision.reason == "no_allowed_action"


def test_allowed_action_not_declared_by_adapter_is_ignored() -> None:
    state = _failed_state_at_threshold(3)
    decision = decide(
        state,
        _result(Status.FAILED),
        _policy(enabled=True, allowed_actions=["does_not_exist", "signal"]),
        _settings(mode="recover", failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "attempt"
    assert decision.action == "signal"


def test_destructive_action_is_never_auto_selected_even_if_only_option() -> None:
    # signal_term (destructive=True) is the only allow-listed action, mode is
    # recover, recovery is enabled — policy must still refuse to choose it.
    state = _failed_state_at_threshold(3)
    decision = decide(
        state,
        _result(Status.FAILED),
        _policy(enabled=True, allowed_actions=["signal_term"]),
        _settings(mode="recover", failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "suppressed"
    assert decision.action is None
    assert decision.reason == "destructive_not_auto_selectable"


def test_destructive_action_is_skipped_in_favor_of_next_allowed_action() -> None:
    state = _failed_state_at_threshold(3)
    decision = decide(
        state,
        _result(Status.FAILED),
        _policy(enabled=True, allowed_actions=["signal_term", "signal"]),
        _settings(mode="recover", failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "attempt"
    assert decision.action == "signal"


def test_destructive_action_never_chosen_as_half_open_probe() -> None:
    incident = Incident(
        kind="failure",
        opened_at=NOW - timedelta(hours=1),
        attempts=3,
        circuit_open_until=NOW - timedelta(seconds=1),
        notified_opened=True,
    )
    state = _state(incident=incident, consecutive_failures=5)
    decision = decide(
        state,
        _result(Status.FAILED),
        _policy(enabled=True, allowed_actions=["signal_term"], max_attempts=3),
        _settings(mode="recover", failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "suppressed"
    assert decision.action is None
    assert decision.reason == "destructive_not_auto_selectable"


def test_first_allowed_action_whose_preconditions_hold_is_chosen() -> None:
    # "restart" is blocked (instance present); "signal" is not.
    state = _failed_state_at_threshold(3)
    result = _result(Status.FAILED, instance_present=True)
    decision = decide(
        state,
        result,
        _policy(enabled=True, allowed_actions=["restart", "signal"]),
        _settings(mode="recover", failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "attempt"
    assert decision.action == "signal"


# --- backoff / cooldown / attempts ----------------------------------------


def test_attempt_applies_cooldown_and_backoff_bookkeeping() -> None:
    state = _failed_state_at_threshold(3)
    policy = _policy(enabled=True, allowed_actions=["signal"], cooldown_s=300, backoff_base_s=30)
    decision = decide(
        state,
        _result(Status.FAILED),
        policy,
        _settings(mode="recover", failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "attempt"
    incident = decision.state.incident
    assert incident is not None
    assert incident.attempts == 1
    assert incident.cooldown_until == NOW + timedelta(seconds=300)
    # backoff = min(30 * 2**(1-1), 900) = 30
    assert incident.next_retry_at == NOW + timedelta(seconds=30)


def test_backoff_doubles_with_each_attempt_and_caps_at_backoff_max() -> None:
    incident = Incident(kind="failure", opened_at=NOW, attempts=2, notified_opened=True)
    state = _state(incident=incident, consecutive_failures=5)
    policy = _policy(enabled=True, allowed_actions=["signal"], backoff_base_s=30, backoff_max_s=100)
    decision = decide(
        state,
        _result(Status.FAILED),
        policy,
        _settings(mode="recover", failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "attempt"
    # attempts becomes 3; backoff = min(30 * 2**2, 100) = min(120, 100) = 100
    assert decision.state.incident.attempts == 3  # type: ignore[union-attr]
    assert decision.state.incident.next_retry_at == NOW + timedelta(seconds=100)


def test_within_cooldown_waits() -> None:
    incident = Incident(
        kind="failure",
        opened_at=NOW,
        attempts=1,
        cooldown_until=NOW + timedelta(seconds=100),
        notified_opened=True,
    )
    state = _state(incident=incident, consecutive_failures=5)
    decision = decide(
        state,
        _result(Status.FAILED),
        _policy(enabled=True, allowed_actions=["signal"]),
        _settings(mode="recover", failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "wait"
    assert decision.reason == "cooldown"
    # attempts untouched while waiting
    assert decision.state.incident.attempts == 1  # type: ignore[union-attr]


def test_before_next_retry_at_waits() -> None:
    incident = Incident(
        kind="failure",
        opened_at=NOW,
        attempts=1,
        next_retry_at=NOW + timedelta(seconds=50),
        notified_opened=True,
    )
    state = _state(incident=incident, consecutive_failures=5)
    decision = decide(
        state,
        _result(Status.FAILED),
        _policy(enabled=True, allowed_actions=["signal"]),
        _settings(mode="recover", failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "wait"
    assert decision.reason == "backoff"


def test_cooldown_and_backoff_elapsed_allows_next_attempt() -> None:
    incident = Incident(
        kind="failure",
        opened_at=NOW,
        attempts=1,
        cooldown_until=NOW,
        next_retry_at=NOW,
        notified_opened=True,
    )
    state = _state(incident=incident, consecutive_failures=5)
    decision = decide(
        state,
        _result(Status.FAILED),
        _policy(enabled=True, allowed_actions=["signal"], max_attempts=5),
        _settings(mode="recover", failure_threshold=3),
        _MANIFEST,
        NOW + timedelta(seconds=1),
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "attempt"


# --- circuit breaker --------------------------------------------------


def test_max_attempts_reached_opens_circuit() -> None:
    incident = Incident(kind="failure", opened_at=NOW, attempts=3, notified_opened=True)
    state = _state(incident=incident, consecutive_failures=5)
    decision = decide(
        state,
        _result(Status.FAILED),
        _policy(enabled=True, allowed_actions=["signal"], max_attempts=3, circuit_open_s=3600),
        _settings(mode="recover", failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "circuit_open"
    assert decision.notify == "circuit_open"
    assert decision.state.incident.circuit_open_until == NOW + timedelta(seconds=3600)  # type: ignore[union-attr]


def test_circuit_open_before_expiry_waits_no_attempt() -> None:
    incident = Incident(
        kind="failure",
        opened_at=NOW,
        attempts=3,
        circuit_open_until=NOW + timedelta(seconds=1000),
        notified_opened=True,
    )
    state = _state(incident=incident, consecutive_failures=5)
    decision = decide(
        state,
        _result(Status.FAILED),
        _policy(enabled=True, allowed_actions=["signal"], max_attempts=3),
        _settings(mode="recover", failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "wait"
    assert decision.reason == "circuit_open"
    assert decision.notify is None


def test_circuit_half_open_after_expiry_allows_one_probe() -> None:
    incident = Incident(
        kind="failure",
        opened_at=NOW,
        attempts=3,
        circuit_open_until=NOW - timedelta(seconds=1),
        notified_opened=True,
    )
    state = _state(incident=incident, consecutive_failures=5)
    decision = decide(
        state,
        _result(Status.FAILED),
        _policy(enabled=True, allowed_actions=["signal"], max_attempts=3, backoff_base_s=10),
        _settings(mode="recover", failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "half_open_probe"
    assert decision.action == "signal"
    # circuit + attempts reset before the fresh attempt bookkeeping is applied
    assert decision.state.incident.circuit_open_until is None  # type: ignore[union-attr]
    assert decision.state.incident.attempts == 1  # type: ignore[union-attr]


def test_half_open_probe_failure_reopens_circuit_next_cycle() -> None:
    # Simulate: probe attempted and failed (engine would set last_action_ok);
    # next cycle with attempts>=max_attempts opens the circuit again.
    incident = Incident(kind="failure", opened_at=NOW, attempts=1, notified_opened=True)
    state = _state(incident=incident, consecutive_failures=5)
    decision = decide(
        state,
        _result(Status.FAILED),
        _policy(enabled=True, allowed_actions=["signal"], max_attempts=1, circuit_open_s=60),
        _settings(mode="recover", failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "circuit_open"


# --- UNKNOWN / check_unavailable ------------------------------------------


def test_unknown_below_threshold_counts_only() -> None:
    state = _state(consecutive_unknown=0)
    decision = decide(
        state,
        _result(Status.UNKNOWN),
        _policy(),
        _settings(unknown_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="observe",
    )
    assert decision.kind == "count"
    assert decision.reason == "unknown_below_threshold"
    assert decision.state.incident is None


def test_unknown_reaching_threshold_opens_check_unavailable_incident() -> None:
    state = _state(consecutive_unknown=1)
    decision = decide(
        state,
        _result(Status.UNKNOWN),
        _policy(),
        _settings(unknown_threshold=2),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="observe",
    )
    assert decision.kind == "open_incident"
    assert decision.reason == "check_unavailable"
    assert decision.notify == "check_unavailable"
    assert decision.state.incident is not None
    assert decision.state.incident.kind == "check_unavailable"


def test_unknown_never_triggers_recovery_even_in_recover_mode() -> None:
    state = _state(consecutive_unknown=1)
    decision = decide(
        state,
        _result(Status.UNKNOWN),
        _policy(enabled=True, allowed_actions=["restart"]),
        _settings(mode="recover", unknown_threshold=2),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "open_incident"
    assert decision.action is None


def test_unknown_check_unavailable_notifies_once() -> None:
    state = _state(consecutive_unknown=1)
    first = decide(
        state,
        _result(Status.UNKNOWN),
        _policy(),
        _settings(unknown_threshold=2),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="observe",
    )
    second = decide(
        first.state,
        _result(Status.UNKNOWN),
        _policy(),
        _settings(unknown_threshold=2),
        _MANIFEST,
        NOW + timedelta(seconds=1),
        kill_switch=False,
        mode="observe",
    )
    assert second.kind == "wait"
    assert second.notify is None


def test_unknown_during_open_failure_incident_does_not_clobber_it() -> None:
    incident = Incident(kind="failure", opened_at=NOW, attempts=1, notified_opened=True)
    state = _state(incident=incident, consecutive_unknown=1)
    decision = decide(
        state,
        _result(Status.UNKNOWN),
        _policy(),
        _settings(unknown_threshold=2),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="observe",
    )
    assert decision.kind == "wait"
    assert decision.reason == "check_unavailable_during_failure_incident"
    assert decision.state.incident is incident or decision.state.incident.kind == "failure"
    assert decision.state.incident.attempts == 1  # type: ignore[union-attr]


def test_new_failure_threshold_supersedes_check_unavailable_incident() -> None:
    incident = Incident(kind="check_unavailable", opened_at=NOW, notified_opened=True)
    state = _state(incident=incident, consecutive_failures=2)
    decision = decide(
        state,
        _result(Status.FAILED),
        _policy(enabled=False),
        _settings(failure_threshold=3),
        _MANIFEST,
        NOW,
        kill_switch=False,
        mode="observe",
    )
    assert decision.kind == "suppressed"
    assert decision.state.incident.kind == "failure"  # type: ignore[union-attr]


# --- decision validity -------------------------------------------------


def test_decision_kind_is_validated() -> None:
    from keepalive.core.policy import Decision

    with pytest.raises(AssertionError):
        Decision(kind="bogus", action=None, reason="x", notify=None, state=_state())


# --- timezone-aware inputs never raise TypeError ------------------------
#
# decide() is pure and does no timezone normalization of its own (that is
# the caller's job -- see keepalive.core.timeutil, and engine.py/state.py
# which now always hand it aware datetimes). These regressions pin down that
# a fully-aware call -- aware `now`, an aware EXPECTED_TRANSITION
# `transition_until` from the adapter, and an aware pre-existing incident
# loaded from persisted state -- never raises
# "TypeError: can't compare offset-naive and offset-aware datetimes", which
# would otherwise crash an entire cycle.

_AWARE_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def test_decide_expected_transition_with_aware_now_and_transition_until() -> None:
    state = _state()
    result = _result(
        Status.EXPECTED_TRANSITION, transition_until=_AWARE_NOW + timedelta(seconds=30)
    )
    decision = decide(
        state,
        result,
        _policy(),
        _settings(),
        _MANIFEST,
        _AWARE_NOW,
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "wait"
    assert decision.reason == "expected_transition"


def test_decide_walks_full_aware_incident_lifecycle_without_typeerror() -> None:
    incident = Incident(
        kind="failure",
        opened_at=_AWARE_NOW - timedelta(minutes=5),
        attempts=1,
        next_retry_at=_AWARE_NOW - timedelta(seconds=1),  # backoff already elapsed
        cooldown_until=_AWARE_NOW - timedelta(seconds=1),  # cooldown already elapsed
        circuit_open_until=None,
        notified_opened=True,
    )
    state = _state(incident=incident, consecutive_failures=3)
    decision = decide(
        state,
        _result(Status.FAILED),
        _policy(max_attempts=3),
        _settings(),
        _MANIFEST,
        _AWARE_NOW,
        kill_switch=False,
        mode="recover",
    )
    assert decision.kind == "attempt"
    assert decision.state.incident is not None
    assert decision.state.incident.cooldown_until is not None
    assert decision.state.incident.cooldown_until > _AWARE_NOW
