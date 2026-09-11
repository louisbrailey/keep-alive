"""Pure decision engine: given state + a check result + config -> a Decision.

``decide`` performs no I/O and never mutates its inputs (the input
``ServiceState``/``Incident`` are deep-copied before anything is changed) —
everything it needs to decide comes in as arguments, and everything it
decided comes back out as the returned ``Decision``, including the
``ServiceState`` the caller should persist. See ARCHITECTURE.md section 2.2
for the table this implements.

Design notes not spelled out verbatim in the table, so a reader isn't left
guessing:

* An ``Incident`` is only created once the failure/unknown threshold is
  reached — below threshold we just count (`consecutive_failures` /
  `consecutive_unknown`) and there is no incident object yet.
* ``ServiceState`` has a single ``incident`` slot, so a failure incident and
  a check-unavailable incident can't coexist. A newly-reached failure
  threshold always supersedes a check-unavailable incident (the failure is
  the more actionable fact); the reverse defers to the existing failure
  incident instead of clobbering its attempt/backoff bookkeeping.
* ``kind="attempt"``/``"half_open_probe"`` decisions pre-commit the
  bookkeeping an attempt implies (attempts += 1, backoff, cooldown) at
  decide-time, since "cooldown after every attempt regardless of result"
  does not depend on the outcome. The engine executes the actual
  ``adapter.recover(...)`` afterwards and only fills in
  ``last_action``/``last_action_ok``/``last_action_detail`` on the incident
  ``decide`` already returned.
* The kill switch and observe-mode/recovery-disabled gates only ever
  suppress the *action* (a recover() call) — they never block the
  informational bookkeeping (incident open/close, counters, the
  ``incident_opened``/``recovered``/``check_unavailable`` notifications).
* ``EXPECTED_TRANSITION`` only produces ``wait`` while its ``transition_until``
  is still in the future; an expired one is treated as an ordinary
  FAILED/DEGRADED result, since the adapter's claim of "this is a planned
  transition" is no longer credible.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timedelta

from keepalive.core.adapter import ActionSpec, AdapterManifest, CheckResult, Status
from keepalive.core.config import KeepAliveSettings, RecoveryPolicy
from keepalive.core.state import Incident, ServiceState

_DECISION_KINDS = frozenset(
    {
        "noop",
        "wait",
        "count",
        "open_incident",
        "attempt",
        "suppressed",
        "recovered",
        "circuit_open",
        "half_open_probe",
    }
)


@dataclass(slots=True)
class Decision:
    kind: str
    action: str | None
    reason: str
    notify: str | None
    state: ServiceState

    def __post_init__(self) -> None:
        assert self.kind in _DECISION_KINDS, f"invalid decision kind {self.kind!r}"


def _effective_failure_threshold(policy: RecoveryPolicy, settings: KeepAliveSettings) -> int:
    return (
        policy.failure_threshold
        if policy.failure_threshold is not None
        else settings.failure_threshold
    )


def _action_blocked_reason(spec: ActionSpec, result: CheckResult, now: datetime) -> str | None:
    """Why `spec` can't be used right now, or None if its preconditions hold."""
    if spec.destructive:
        # ActionSpec.destructive actions are never auto-selected by policy at all
        # (ARCHITECTURE.md rule 8 / THREAT_MODEL.md 6.8) — this is a permanent
        # block, not a precondition that can later clear.
        return "destructive_not_auto_selectable"
    if result.active_job and not spec.allowed_during_active_job:
        return "active_job"
    if spec.starts_instance:
        if result.instance_present:
            return "instance_present"
        if result.lock_held:
            return "lock_held"
        if result.handoff_until is not None and result.handoff_until > now:
            return "handoff_in_progress"
    return None


def _choose_action(
    policy: RecoveryPolicy, manifest: AdapterManifest, result: CheckResult, now: datetime
) -> tuple[str | None, str | None]:
    """Returns (chosen_action, reason). reason is set (to the first blocking
    reason encountered) only when no action could be chosen."""
    actions_by_name = {a.name: a for a in manifest.actions}
    first_block_reason: str | None = None
    for name in policy.allowed_actions:
        spec = actions_by_name.get(name)
        if spec is None:
            continue
        block_reason = _action_blocked_reason(spec, result, now)
        if block_reason is None:
            return name, None
        if first_block_reason is None:
            first_block_reason = block_reason
    return None, first_block_reason or "no_allowed_action"


def _apply_attempt_bookkeeping(incident: Incident, policy: RecoveryPolicy, now: datetime) -> None:
    incident.attempts += 1
    backoff_s = min(policy.backoff_base_s * 2 ** (incident.attempts - 1), policy.backoff_max_s)
    incident.next_retry_at = now + timedelta(seconds=backoff_s)
    incident.cooldown_until = now + timedelta(seconds=policy.cooldown_s)


def decide(  # noqa: PLR0917 - signature is fixed by the cross-agent module contract
    state: ServiceState,
    result: CheckResult,
    policy: RecoveryPolicy,
    settings: KeepAliveSettings,
    manifest: AdapterManifest,
    now: datetime,
    *,
    kill_switch: bool,
    mode: str,
) -> Decision:
    new_state = copy.deepcopy(state)
    new_state.last_check_at = now
    new_state.last_detail = result.detail
    new_state.status = result.status.value

    status = result.status
    if status == Status.EXPECTED_TRANSITION:
        still_future = result.transition_until is not None and result.transition_until > now
        if not still_future:
            status = Status.FAILED  # expired claim of a planned transition: treat as a real failure

    if status == Status.OK:
        new_state.last_healthy_at = now
        new_state.consecutive_failures = 0
        new_state.consecutive_unknown = 0
        had_incident = new_state.incident is not None
        notify_opened = had_incident and new_state.incident.notified_opened  # type: ignore[union-attr]
        new_state.incident = None
        if had_incident:
            return Decision(
                kind="recovered",
                action=None,
                reason="recovered",
                notify="recovered" if notify_opened else None,
                state=new_state,
            )
        return Decision(kind="noop", action=None, reason="healthy", notify=None, state=new_state)

    if status == Status.EXPECTED_TRANSITION:
        return Decision(
            kind="wait", action=None, reason="expected_transition", notify=None, state=new_state
        )

    if status == Status.UNKNOWN:
        new_state.consecutive_unknown += 1
        if new_state.consecutive_unknown < settings.unknown_threshold:
            return Decision(
                kind="count",
                action=None,
                reason="unknown_below_threshold",
                notify=None,
                state=new_state,
            )

        if new_state.incident is not None and new_state.incident.kind != "check_unavailable":
            # A failure incident is already open; don't clobber its bookkeeping.
            return Decision(
                kind="wait",
                action=None,
                reason="check_unavailable_during_failure_incident",
                notify=None,
                state=new_state,
            )

        if new_state.incident is None:
            new_state.incident = Incident(kind="check_unavailable", opened_at=now)

        if not new_state.incident.notified_opened:
            new_state.incident.notified_opened = True
            return Decision(
                kind="open_incident",
                action=None,
                reason="check_unavailable",
                notify="check_unavailable",
                state=new_state,
            )
        return Decision(
            kind="wait", action=None, reason="check_unavailable", notify=None, state=new_state
        )

    # status is FAILED or DEGRADED (directly, or an expired EXPECTED_TRANSITION).
    new_state.consecutive_unknown = 0
    new_state.consecutive_failures += 1

    threshold = _effective_failure_threshold(policy, settings)
    if new_state.consecutive_failures < threshold:
        return Decision(
            kind="count", action=None, reason="below_threshold", notify=None, state=new_state
        )

    if new_state.incident is None or new_state.incident.kind != "failure":
        new_state.incident = Incident(kind="failure", opened_at=now)
    incident = new_state.incident
    first_open = not incident.notified_opened

    if kill_switch:
        if first_open:
            incident.notified_opened = True
        return Decision(
            kind="suppressed",
            action=None,
            reason="kill_switch",
            notify="incident_opened" if first_open else None,
            state=new_state,
        )

    recovery_active = mode == "recover" and policy.enabled
    if not recovery_active:
        if first_open:
            incident.notified_opened = True
        return Decision(
            kind="suppressed",
            action=None,
            reason="observe_only",
            notify="incident_opened" if first_open else None,
            state=new_state,
        )

    if incident.circuit_open_until is not None and incident.circuit_open_until > now:
        return Decision(
            kind="wait", action=None, reason="circuit_open", notify=None, state=new_state
        )

    is_half_open_probe = (
        incident.circuit_open_until is not None and incident.circuit_open_until <= now
    )

    if incident.cooldown_until is not None and incident.cooldown_until > now:
        return Decision(kind="wait", action=None, reason="cooldown", notify=None, state=new_state)
    if incident.next_retry_at is not None and incident.next_retry_at > now:
        return Decision(kind="wait", action=None, reason="backoff", notify=None, state=new_state)

    if incident.attempts >= policy.max_attempts and not is_half_open_probe:
        incident.circuit_open_until = now + timedelta(seconds=policy.circuit_open_s)
        return Decision(
            kind="circuit_open",
            action=None,
            reason="max_attempts_reached",
            notify="circuit_open",
            state=new_state,
        )

    chosen_action, block_reason = _choose_action(policy, manifest, result, now)
    if chosen_action is None:
        if first_open:
            incident.notified_opened = True
        return Decision(
            kind="suppressed",
            action=None,
            reason=block_reason or "no_allowed_action",
            notify="incident_opened" if first_open else None,
            state=new_state,
        )

    if is_half_open_probe:
        incident.attempts = 0
        incident.circuit_open_until = None
    _apply_attempt_bookkeeping(incident, policy, now)
    if first_open:
        incident.notified_opened = True

    return Decision(
        kind="half_open_probe" if is_half_open_probe else "attempt",
        action=chosen_action,
        reason="recovery_attempt",
        notify="incident_opened" if first_open else None,
        state=new_state,
    )


__all__ = ["Decision", "decide"]
