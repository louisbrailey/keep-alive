"""Integration tests for keepalive.core.engine.run_cycle, using the shared
FakeAdapter test double (tests/conftest.py) instead of any real adapter."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

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
from keepalive.core.notify import Notifier
from keepalive.core.state import StateStore
from tests.conftest import FakeAdapter

NOW = datetime(2026, 1, 1, 12, 0, 0)


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


def _rig(tmp_path: Path) -> tuple[StateStore, AuditLog, Notifier]:
    state_store = StateStore(tmp_path / "state.json")
    audit = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    notifier = Notifier(_notification_settings(), audit)
    return state_store, audit, notifier


def test_healthy_cycle_is_noop_and_persists_state(tmp_path: Path) -> None:
    adapter = FakeAdapter({})
    adapter.results = [CheckResult(status=Status.OK)]
    service = _service()
    config = _config(tmp_path, [service])
    state_store, audit, notifier = _rig(tmp_path)

    report = run_cycle(
        config,
        state_store=state_store,
        audit=audit,
        notifier=notifier,
        now=NOW,
        adapter_factory=lambda _svc: adapter,
    )

    assert report.ran is True
    assert report.services[0]["status"] == "ok"
    assert report.services[0]["decision"] == "noop"
    saved = state_store.load()
    assert saved["svc"].status == "ok"


def test_disabled_service_is_skipped(tmp_path: Path) -> None:
    adapter = FakeAdapter({})
    service = _service(enabled=False)
    config = _config(tmp_path, [service])
    state_store, audit, notifier = _rig(tmp_path)

    report = run_cycle(
        config,
        state_store=state_store,
        audit=audit,
        notifier=notifier,
        now=NOW,
        adapter_factory=lambda _svc: adapter,
    )

    assert report.services == []
    assert adapter.results == []  # check() never called: no recorded calls either
    assert adapter.recover_calls == []


def test_overlapping_run_is_skipped_via_lock(tmp_path: Path) -> None:
    from keepalive.core.lock import InstanceLock

    service = _service()
    config = _config(tmp_path, [service])
    state_store, audit, notifier = _rig(tmp_path)
    lock_path = config.keepalive.state_dir / "keepalive.lock"

    with InstanceLock(lock_path) as held:
        assert held.acquired is True
        report = run_cycle(
            config,
            state_store=state_store,
            audit=audit,
            notifier=notifier,
            now=NOW,
            adapter_factory=lambda _svc: FakeAdapter({}),
        )

    assert report.ran is False
    assert report.skipped_reason == "lock_held"


def test_kill_switch_file_suppresses_recovery(tmp_path: Path) -> None:
    adapter = FakeAdapter({})
    adapter.results = [CheckResult(status=Status.FAILED), CheckResult(status=Status.FAILED)]
    service = _service(
        recovery=RecoveryPolicy(enabled=True, allowed_actions=["restart"], max_attempts=3)
    )
    config = _config(tmp_path, [service], mode="recover", failure_threshold=2)
    state_store, audit, notifier = _rig(tmp_path)
    set_kill_switch(config.keepalive, on=True, reason="testing")

    # Drive past the failure threshold.
    run_cycle(
        config,
        state_store=state_store,
        audit=audit,
        notifier=notifier,
        now=NOW,
        adapter_factory=lambda _svc: adapter,
    )
    report = run_cycle(
        config,
        state_store=state_store,
        audit=audit,
        notifier=notifier,
        now=NOW,
        adapter_factory=lambda _svc: adapter,
    )

    assert report.services[0]["decision"] == "suppressed"
    assert adapter.recover_calls == []  # kill switch: no recovery action ever executed


def test_recovery_attempt_calls_adapter_recover_and_persists_result(tmp_path: Path) -> None:
    adapter = FakeAdapter({})
    adapter.results = [CheckResult(status=Status.FAILED), CheckResult(status=Status.FAILED)]
    adapter.recover_result = ActionResult(ok=True, detail="restarted")
    service = _service(
        recovery=RecoveryPolicy(enabled=True, allowed_actions=["restart"], max_attempts=3)
    )
    config = _config(tmp_path, [service], mode="recover", failure_threshold=2)
    state_store, audit, notifier = _rig(tmp_path)

    run_cycle(
        config,
        state_store=state_store,
        audit=audit,
        notifier=notifier,
        now=NOW,
        adapter_factory=lambda _svc: adapter,
    )
    report = run_cycle(
        config,
        state_store=state_store,
        audit=audit,
        notifier=notifier,
        now=NOW,
        adapter_factory=lambda _svc: adapter,
    )

    assert report.services[0]["decision"] == "attempt"
    assert len(adapter.recover_calls) == 1
    assert adapter.recover_calls[0][0] == "restart"
    saved = state_store.load()
    incident = saved["svc"].incident
    assert incident is not None
    assert incident.last_action == "restart"
    assert incident.last_action_ok is True
    assert incident.last_action_detail == "restarted"


def test_dry_run_does_not_call_adapter_recover(tmp_path: Path) -> None:
    adapter = FakeAdapter({})
    adapter.results = [CheckResult(status=Status.FAILED), CheckResult(status=Status.FAILED)]
    service = _service(
        recovery=RecoveryPolicy(enabled=True, allowed_actions=["restart"], max_attempts=3)
    )
    config = _config(tmp_path, [service], mode="recover", failure_threshold=2)
    state_store, audit, notifier = _rig(tmp_path)

    run_cycle(
        config,
        state_store=state_store,
        audit=audit,
        notifier=notifier,
        now=NOW,
        dry_run=True,
        adapter_factory=lambda _svc: adapter,
    )
    report = run_cycle(
        config,
        state_store=state_store,
        audit=audit,
        notifier=notifier,
        now=NOW,
        dry_run=True,
        adapter_factory=lambda _svc: adapter,
    )

    assert report.services[0]["decision"] == "attempt"
    assert adapter.recover_calls == []  # never actually invoked in dry-run


def test_observe_mode_never_calls_recover_even_with_recovery_enabled(tmp_path: Path) -> None:
    adapter = FakeAdapter({})
    adapter.results = [CheckResult(status=Status.FAILED), CheckResult(status=Status.FAILED)]
    service = _service(
        recovery=RecoveryPolicy(enabled=True, allowed_actions=["restart"], max_attempts=3)
    )
    config = _config(tmp_path, [service], mode="observe", failure_threshold=2)
    state_store, audit, notifier = _rig(tmp_path)

    run_cycle(
        config,
        state_store=state_store,
        audit=audit,
        notifier=notifier,
        now=NOW,
        adapter_factory=lambda _svc: adapter,
    )
    report = run_cycle(
        config,
        state_store=state_store,
        audit=audit,
        notifier=notifier,
        now=NOW,
        adapter_factory=lambda _svc: adapter,
    )

    assert report.services[0]["decision"] == "suppressed"
    assert adapter.recover_calls == []


def test_recovered_notification_is_sent(tmp_path: Path) -> None:
    adapter = FakeAdapter({})
    adapter.results = [
        CheckResult(status=Status.FAILED),
        CheckResult(status=Status.FAILED),
        CheckResult(status=Status.OK),
    ]
    service = _service()
    config = _config(tmp_path, [service], mode="observe", failure_threshold=2)
    state_store, audit, notifier = _rig(tmp_path)

    for _ in range(2):
        run_cycle(
            config,
            state_store=state_store,
            audit=audit,
            notifier=notifier,
            now=NOW,
            adapter_factory=lambda _svc: adapter,
        )
    report = run_cycle(
        config,
        state_store=state_store,
        audit=audit,
        notifier=notifier,
        now=NOW,
        adapter_factory=lambda _svc: adapter,
    )

    assert report.services[0]["decision"] == "recovered"
    notify_records = [r for r in audit.tail(100) if r["event"] == "notify"]
    assert any(r["notify_event"] == "recovered" for r in notify_records)


def test_check_raising_is_treated_as_unknown_not_a_crash(tmp_path: Path) -> None:
    class ExplodingAdapter(FakeAdapter):
        kind = "fake"

        def check(self, ctx: Any) -> CheckResult:
            raise RuntimeError("boom")

    service = _service()
    config = _config(tmp_path, [service])
    state_store, audit, notifier = _rig(tmp_path)

    report = run_cycle(
        config,
        state_store=state_store,
        audit=audit,
        notifier=notifier,
        now=NOW,
        adapter_factory=lambda _svc: ExplodingAdapter({}),
    )

    assert report.ran is True
    assert report.services[0]["status"] == "unknown"


def test_unresolvable_adapter_kind_is_treated_as_unknown(tmp_path: Path) -> None:
    def _factory(_svc: ServiceConfig) -> Any:
        raise LookupError("no such adapter")

    service = _service()
    config = _config(tmp_path, [service])
    state_store, audit, notifier = _rig(tmp_path)

    report = run_cycle(
        config,
        state_store=state_store,
        audit=audit,
        notifier=notifier,
        now=NOW,
        adapter_factory=_factory,
    )

    assert report.ran is True
    assert report.services[0]["status"] == "unknown"


def test_meta_records_last_cycle_at(tmp_path: Path) -> None:
    adapter = FakeAdapter({})
    service = _service()
    config = _config(tmp_path, [service])
    state_store, audit, notifier = _rig(tmp_path)

    run_cycle(
        config,
        state_store=state_store,
        audit=audit,
        notifier=notifier,
        now=NOW,
        adapter_factory=lambda _svc: adapter,
    )

    reloaded = StateStore(tmp_path / "state.json")
    reloaded.load()
    assert reloaded.meta["last_cycle_at"] == NOW.isoformat()


def test_audit_records_check_and_decision_events(tmp_path: Path) -> None:
    adapter = FakeAdapter({})
    adapter.results = [CheckResult(status=Status.OK)]
    service = _service()
    config = _config(tmp_path, [service])
    state_store, audit, notifier = _rig(tmp_path)

    run_cycle(
        config,
        state_store=state_store,
        audit=audit,
        notifier=notifier,
        now=NOW,
        adapter_factory=lambda _svc: adapter,
    )

    events = [r["event"] for r in audit.tail(100)]
    assert "check" in events
    assert "decision" in events


def test_wake_grace_marks_failure_as_expected_transition_after_sleep(
    tmp_path: Path,
) -> None:
    """A slept_since_last cycle turns a FAILED result into EXPECTED_TRANSITION
    for adapters that opt in (treat_wake_as_transition, true by default on
    FakeAdapter's manifest) — so a stale heartbeat right after wake doesn't
    open an incident."""

    class FakePower:
        def observe(self, now: datetime, last_cycle_at: datetime | None) -> Any:
            from keepalive.core.power import PowerState

            return PowerState(
                on_ac=True,
                battery_pct=100,
                slept_since_last=True,
                last_sleep_at=now,
                last_wake_at=now,
                detail="",
            )

    adapter = FakeAdapter({})
    adapter.results = [CheckResult(status=Status.FAILED, detail="stale heartbeat")]
    service = _service()
    config = _config(tmp_path, [service], failure_threshold=1)
    state_store, audit, notifier = _rig(tmp_path)

    report = run_cycle(
        config,
        state_store=state_store,
        audit=audit,
        notifier=notifier,
        now=NOW,
        power=FakePower(),  # type: ignore[arg-type]
        adapter_factory=lambda _svc: adapter,
    )

    assert report.services[0]["decision"] == "wait"
    assert report.services[0]["status"] == "expected_transition"


def test_multiple_services_are_each_checked_independently(tmp_path: Path) -> None:
    healthy = FakeAdapter({})
    healthy.results = [CheckResult(status=Status.OK)]
    failing = FakeAdapter({})
    failing.results = [CheckResult(status=Status.FAILED)]

    services = [
        _service(name="healthy-svc"),
        _service(name="failing-svc", recovery=RecoveryPolicy()),
    ]
    config = _config(tmp_path, services, failure_threshold=5)
    state_store, audit, notifier = _rig(tmp_path)

    def factory(svc: ServiceConfig) -> Any:
        return healthy if svc.name == "healthy-svc" else failing

    report = run_cycle(
        config,
        state_store=state_store,
        audit=audit,
        notifier=notifier,
        now=NOW,
        adapter_factory=factory,
    )

    by_name = {s["name"]: s for s in report.services}
    assert by_name["healthy-svc"]["status"] == "ok"
    assert by_name["failing-svc"]["status"] == "failed"


def test_set_kill_switch_creates_and_removes_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    set_kill_switch(settings, on=True, reason="manual test")
    assert (tmp_path / "KILL").exists()
    set_kill_switch(settings, on=False, reason="")
    assert not (tmp_path / "KILL").exists()


def test_set_kill_switch_off_when_absent_does_not_raise(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    set_kill_switch(settings, on=False, reason="")  # must not raise


def test_recover_raising_is_recorded_as_a_failed_action_not_a_crash(tmp_path: Path) -> None:
    class ExplodingRecoverAdapter(FakeAdapter):
        kind = "fake"

        def recover(self, action: str, ctx: Any) -> ActionResult:
            raise RuntimeError("recover boom")

    adapter = ExplodingRecoverAdapter({})
    adapter.results = [CheckResult(status=Status.FAILED), CheckResult(status=Status.FAILED)]
    service = _service(
        recovery=RecoveryPolicy(enabled=True, allowed_actions=["restart"], max_attempts=3)
    )
    config = _config(tmp_path, [service], mode="recover", failure_threshold=2)
    state_store, audit, notifier = _rig(tmp_path)

    run_cycle(
        config,
        state_store=state_store,
        audit=audit,
        notifier=notifier,
        now=NOW,
        adapter_factory=lambda _svc: adapter,
    )
    report = run_cycle(
        config,
        state_store=state_store,
        audit=audit,
        notifier=notifier,
        now=NOW,
        adapter_factory=lambda _svc: adapter,
    )

    assert report.ran is True
    assert report.services[0]["decision"] == "attempt"
    saved = state_store.load()
    incident = saved["svc"].incident
    assert incident is not None
    assert incident.last_action_ok is False
    assert "recover boom" in incident.last_action_detail
