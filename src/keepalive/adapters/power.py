"""Power-state adapter.

Reports on AC/battery power and sleep/wake transitions. Unlike other adapters this
one runs no subprocess of its own — the engine's ``PowerObserver`` already samples
``pmset`` once per cycle (read-only) and hands the result down via
``CheckContext.on_ac_power`` / ``CheckContext.slept_since_last_cycle``, so every
service can see the same power sample without each adapter re-shelling out. Declares
no recovery actions.
"""

from __future__ import annotations

from keepalive.core.adapter import (
    Adapter,
    AdapterManifest,
    CheckContext,
    CheckResult,
    CheckSpec,
    DataSource,
    NotifyCondition,
    Status,
)


class PowerAdapter(Adapter):
    """Surfaces AC/battery and sleep/wake state as a service in its own right."""

    kind = "power"

    @classmethod
    def describe(cls) -> AdapterManifest:
        return AdapterManifest(
            kind=cls.kind,
            description="Observes AC/battery power and sleep/wake transitions.",
            checks=(
                CheckSpec(
                    name="power_state",
                    description=(
                        "Current AC/battery state and whether the system slept since "
                        "the last cycle."
                    ),
                ),
            ),
            reads=(
                DataSource(
                    kind="pmset",
                    target="ctx.on_ac_power / ctx.slept_since_last_cycle",
                    read_only=True,
                ),
            ),
            actions=(),
            notify_on=(
                NotifyCondition(
                    event="incident_opened", description="On battery power (warn_on_battery)"
                ),
                NotifyCondition(event="recovered", description="Back on AC power"),
                NotifyCondition(
                    event="check_unavailable",
                    description="Repeated failures determining AC/battery state",
                ),
            ),
            # This adapter's own subject *is* wake/sleep, so a wake transition must
            # not mask what it reports.
            treat_wake_as_transition=False,
            params_schema={
                "warn_on_battery": "If true, report DEGRADED while running on battery power.",
                "report_sleep": (
                    "If true, include sleep-since-last-cycle information in the detail."
                ),
            },
        )

    def check(self, ctx: CheckContext) -> CheckResult:
        warn_on_battery = bool(self.params.get("warn_on_battery", False))
        report_sleep = bool(self.params.get("report_sleep", False))

        metrics: dict[str, object] = {
            "on_ac_power": ctx.on_ac_power,
            "slept_since_last_cycle": ctx.slept_since_last_cycle,
        }

        if ctx.on_ac_power is None:
            return CheckResult(
                status=Status.UNKNOWN,
                detail="unable to determine AC/battery state",
                metrics=metrics,
            )

        if ctx.on_ac_power:
            status = Status.OK
            detail = "on AC power"
        elif warn_on_battery:
            status = Status.DEGRADED
            detail = "running on battery power"
        else:
            status = Status.OK
            detail = "running on battery power (not configured to warn)"

        if report_sleep and ctx.slept_since_last_cycle:
            detail += "; system slept since last cycle"

        return CheckResult(status=status, detail=detail, metrics=metrics)


__all__ = ["PowerAdapter"]
