"""Shared test fixtures. No real subprocess/network calls happen from here —
individual test modules monkeypatch ``subprocess``/``urllib`` themselves."""

from __future__ import annotations

from typing import Any

from keepalive.core.adapter import (
    ActionResult,
    ActionSpec,
    Adapter,
    AdapterManifest,
    CheckContext,
    CheckResult,
    CheckSpec,
    DataSource,
    NotifyCondition,
    Status,
)


class FakeAdapter(Adapter):
    """A reusable Adapter test double.

    ``results`` is a scripted list of ``CheckResult`` returned in order by
    successive ``check()`` calls (the last entry repeats once exhausted; an
    empty list means "always OK"). Every ``recover()`` call is recorded in
    ``recover_calls`` as ``(action, ctx)``, and returns
    ``recover_results[call_index]`` if set, else ``recover_result``.
    """

    kind = "fake"

    def __init__(self, params: dict[str, Any]) -> None:
        super().__init__(params)
        self.results: list[CheckResult] = []
        self._check_index = 0
        self.recover_calls: list[tuple[str, CheckContext]] = []
        self.recover_result = ActionResult(ok=True, detail="recovered")
        self.recover_results: list[ActionResult] = []
        self.manifest_override: AdapterManifest | None = None

    @classmethod
    def describe(cls) -> AdapterManifest:
        return AdapterManifest(
            kind="fake",
            description="test double adapter",
            checks=(CheckSpec(name="fake_check", description="scripted result"),),
            reads=(DataSource(kind="command", target="fake"),),
            actions=(
                ActionSpec(
                    name="restart",
                    description="restart the fake thing",
                    allowed_during_active_job=False,
                    starts_instance=True,
                ),
                ActionSpec(
                    name="signal",
                    description="send a signal",
                    allowed_during_active_job=True,
                    starts_instance=False,
                ),
            ),
            notify_on=(
                NotifyCondition(event="incident_opened", description="an incident opened"),
                NotifyCondition(event="recovered", description="recovered after an incident"),
                NotifyCondition(event="circuit_open", description="circuit opened"),
                NotifyCondition(event="check_unavailable", description="check could not run"),
            ),
            treat_wake_as_transition=True,
        )

    def check(self, ctx: CheckContext) -> CheckResult:
        if not self.results:
            return CheckResult(status=Status.OK, detail="no script; default OK")
        index = min(self._check_index, len(self.results) - 1)
        self._check_index += 1
        return self.results[index]

    def recover(self, action: str, ctx: CheckContext) -> ActionResult:
        self.recover_calls.append((action, ctx))
        if self.recover_results:
            idx = min(len(self.recover_calls) - 1, len(self.recover_results) - 1)
            return self.recover_results[idx]
        return self.recover_result
