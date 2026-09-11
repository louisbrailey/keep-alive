"""One cycle: lock -> kill switch -> checks -> policy -> actions -> audit -> notify.

See ARCHITECTURE.md section 2.3.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from keepalive.core.adapter import (
    ActionResult,
    Adapter,
    AdapterManifest,
    CheckContext,
    CheckResult,
    Status,
)
from keepalive.core.audit import AuditLog
from keepalive.core.config import Config, KeepAliveSettings, ServiceConfig, load_config
from keepalive.core.lock import InstanceLock
from keepalive.core.notify import Notifier
from keepalive.core.policy import Decision, decide
from keepalive.core.power import PowerObserver, PowerState
from keepalive.core.registry import get_adapter_class
from keepalive.core.state import ServiceState, StateStore

#: Grace window applied when an adapter's result would otherwise be a
#: failure but KeepAlive detected a sleep since the last cycle and the
#: adapter opts in to treating that as an expected transition.
_WAKE_GRACE_S = 60


@dataclass(slots=True)
class CycleReport:
    ran: bool
    skipped_reason: str | None
    services: list[dict[str, Any]]
    power: PowerState | None
    decisions: list[Decision]


def kill_switch_path(settings: KeepAliveSettings) -> Path:
    return settings.state_dir / "KILL"


def set_kill_switch(settings: KeepAliveSettings, on: bool, reason: str) -> None:
    path = kill_switch_path(settings)
    if on:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{datetime.now().isoformat()} {reason}\n", encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


def _default_adapter_factory(service: ServiceConfig) -> Adapter:
    adapter_cls = get_adapter_class(service.adapter)
    return adapter_cls(service.params)


def _unavailable_manifest(kind: str) -> AdapterManifest:
    return AdapterManifest(
        kind=kind,
        description="adapter unavailable",
        checks=(),
        reads=(),
        actions=(),
        notify_on=(),
        treat_wake_as_transition=False,
    )


def _parse_meta_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _build_check_context(
    service: ServiceConfig,
    now: datetime,
    power_state: PowerState | None,
    *,
    last_cycle_at: datetime | None,
    state_dir: Path,
    dry_run: bool,
) -> CheckContext:
    return CheckContext(
        service_name=service.name,
        params=service.params,
        now=now,
        on_ac_power=power_state.on_ac if power_state is not None else None,
        slept_since_last_cycle=bool(power_state is not None and power_state.slept_since_last),
        last_cycle_at=last_cycle_at,
        state_dir=str(state_dir),
        dry_run=dry_run,
    )


def _run_check(
    service: ServiceConfig, ctx: CheckContext, factory: Callable[[ServiceConfig], Adapter]
) -> tuple[Adapter | None, AdapterManifest, CheckResult]:
    try:
        adapter = factory(service)
        manifest = adapter.describe()
    except Exception as exc:
        return (
            None,
            _unavailable_manifest(service.adapter),
            CheckResult(status=Status.UNKNOWN, detail=f"adapter unavailable: {exc}"),
        )
    try:
        result = adapter.check(ctx)
    except Exception as exc:
        result = CheckResult(status=Status.UNKNOWN, detail=f"check raised: {exc}")
    return adapter, manifest, result


def _apply_wake_grace(
    result: CheckResult, manifest: AdapterManifest, power_state: PowerState | None, now: datetime
) -> None:
    if (
        power_state is None
        or not power_state.slept_since_last
        or not manifest.treat_wake_as_transition
    ):
        return
    if result.status not in (Status.FAILED, Status.DEGRADED, Status.UNKNOWN):
        return
    result.status = Status.EXPECTED_TRANSITION
    if result.transition_until is None or result.transition_until <= now:
        result.transition_until = now + timedelta(seconds=_WAKE_GRACE_S)
    result.detail = f"{result.detail} (post-wake grace period)".strip()


def run_cycle(
    config: Config,
    *,
    state_store: StateStore,
    audit: AuditLog,
    notifier: Notifier,
    power: PowerObserver | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    adapter_factory: Callable[[ServiceConfig], Adapter] | None = None,
) -> CycleReport:
    cycle_now = now if now is not None else datetime.now()
    factory = adapter_factory or _default_adapter_factory
    lock_path = config.keepalive.state_dir / "keepalive.lock"

    with InstanceLock(lock_path) as lock:
        if not lock.acquired:
            audit.record("overlap_skipped", reason="lock_held")
            return CycleReport(
                ran=False, skipped_reason="lock_held", services=[], power=None, decisions=[]
            )

        kill_switch = kill_switch_path(config.keepalive).exists()

        existing_states = state_store.load()
        last_cycle_at = _parse_meta_dt(state_store.meta.get("last_cycle_at"))

        power_state: PowerState | None = None
        if power is not None:
            power_state = power.observe(cycle_now, last_cycle_at)
            audit.record(
                "power_observed",
                on_ac=power_state.on_ac,
                battery_pct=power_state.battery_pct,
                slept_since_last=power_state.slept_since_last,
                detail=power_state.detail,
            )

        service_reports: list[dict[str, Any]] = []
        decisions: list[Decision] = []

        for service in config.services:
            if not service.enabled:
                continue

            state = existing_states.get(service.name, ServiceState(name=service.name))
            ctx = _build_check_context(
                service,
                cycle_now,
                power_state,
                last_cycle_at=last_cycle_at,
                state_dir=config.keepalive.state_dir,
                dry_run=dry_run,
            )
            adapter, manifest, result = _run_check(service, ctx, factory)
            _apply_wake_grace(result, manifest, power_state, cycle_now)

            audit.record("check", service=service.name, status=result.status, detail=result.detail)

            decision = decide(
                state,
                result,
                service.recovery,
                config.keepalive,
                manifest,
                cycle_now,
                kill_switch=kill_switch,
                mode=config.keepalive.mode,
            )
            decisions.append(decision)
            new_state = decision.state
            action_result: ActionResult | None = None

            if decision.kind in ("attempt", "half_open_probe") and decision.action is not None:
                if adapter is None:
                    action_result = ActionResult(ok=False, detail="adapter unavailable")
                elif dry_run:
                    action_result = ActionResult(ok=True, detail="dry-run: action not executed")
                else:
                    try:
                        action_result = adapter.recover(decision.action, ctx)
                    except Exception as exc:
                        action_result = ActionResult(ok=False, detail=f"recover raised: {exc}")

                if new_state.incident is not None:
                    new_state.incident.last_action = decision.action
                    new_state.incident.last_action_at = cycle_now
                    new_state.incident.last_action_ok = action_result.ok
                    new_state.incident.last_action_detail = action_result.detail

                audit.record(
                    "action",
                    service=service.name,
                    action=decision.action,
                    ok=action_result.ok,
                    detail=action_result.detail,
                    dry_run=dry_run,
                )

            audit.record(
                "decision",
                service=service.name,
                kind=decision.kind,
                action=decision.action,
                reason=decision.reason,
            )

            if decision.notify is not None:
                title = f"KeepAlive: {service.name}"
                message = decision.reason
                notifier.send(decision.notify, title, message)

            existing_states[service.name] = new_state
            service_reports.append(
                {
                    "name": service.name,
                    "adapter": service.adapter,
                    "status": new_state.status,
                    "decision": decision.kind,
                    "reason": decision.reason,
                }
            )

        state_store.meta = {
            "last_cycle_at": cycle_now.isoformat(),
            "last_power": (
                {"on_ac": power_state.on_ac, "battery_pct": power_state.battery_pct}
                if power_state is not None
                else None
            ),
        }
        state_store.save(existing_states)

        return CycleReport(
            ran=True,
            skipped_reason=None,
            services=service_reports,
            power=power_state,
            decisions=decisions,
        )


def run_loop(config_path: Path | None, *, once: bool = False) -> None:
    """Re-reads config each cycle (so edits apply without restart) and sleeps
    ``poll_interval_s`` between cycles."""
    while True:
        config = load_config(config_path)
        state_store = StateStore(config.keepalive.state_dir / "state.json")
        audit = AuditLog(
            config.keepalive.state_dir / "audit.jsonl", config.keepalive.audit_max_bytes
        )
        notifier = Notifier(config.notifications, audit)
        power = (
            PowerObserver(config.power, config.keepalive.state_dir)
            if config.power.observe
            else None
        )
        run_cycle(config, state_store=state_store, audit=audit, notifier=notifier, power=power)
        if once:
            return
        time.sleep(config.keepalive.poll_interval_s)


__all__ = ["CycleReport", "kill_switch_path", "run_cycle", "run_loop", "set_kill_switch"]
