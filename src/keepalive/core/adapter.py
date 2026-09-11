"""The adapter contract. Core KeepAlive knows nothing about any application; every
application-specific fact is expressed through these types.

An adapter:

* ``describe()`` — declares its checks, the data it reads, its allowed recovery
  actions (with safety flags and default cooldowns), and its notification conditions;
* ``check(ctx)`` — a side-effect-free observation returning a :class:`CheckResult`;
* ``recover(action, ctx)`` — performs exactly ONE declared action, only when the
  engine's policy has approved it.

Adapters never decide policy. They report facts (``active_job``, ``instance_present``,
``lock_held``, ``handoff_until``) and the core enforces the rules.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"
    UNKNOWN = "unknown"  # the check itself could not run; never a service failure
    EXPECTED_TRANSITION = "expected_transition"  # planned restart / maintenance, with expiry


@dataclass(frozen=True, slots=True)
class CheckSpec:
    name: str
    description: str
    interval_s: int | None = None  # None = every cycle


@dataclass(frozen=True, slots=True)
class DataSource:
    """What an adapter reads — surfaced in the status view and the threat model."""

    kind: str  # "file" | "sqlite_ro" | "http" | "command" | "launchd" | "pmset"
    target: str
    read_only: bool = True


@dataclass(frozen=True, slots=True)
class ActionSpec:
    name: str
    description: str
    allowed_during_active_job: bool = False
    starts_instance: bool = False  # refused when instance_present or lock_held
    default_cooldown_s: int = 300
    default_max_attempts: int = 3
    destructive: bool = False  # never auto-selected; requires explicit allow-list entry


@dataclass(frozen=True, slots=True)
class NotifyCondition:
    event: str  # "incident_opened" | "recovered" | "circuit_open" | "check_unavailable"
    description: str


@dataclass(frozen=True, slots=True)
class AdapterManifest:
    kind: str
    description: str
    checks: tuple[CheckSpec, ...]
    reads: tuple[DataSource, ...]
    actions: tuple[ActionSpec, ...]
    notify_on: tuple[NotifyCondition, ...]
    treat_wake_as_transition: bool = True
    params_schema: dict[str, str] = field(default_factory=dict)  # param -> human description


@dataclass(slots=True)
class CheckResult:
    status: Status
    detail: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    active_job: bool = False  # a restart now would interrupt work
    instance_present: bool = False  # a live instance exists (healthy or not)
    lock_held: bool = False  # the application's own lock is held by a live process
    handoff_until: datetime | None = None  # planned restart/handoff in progress until then
    transition_until: datetime | None = None  # for EXPECTED_TRANSITION
    last_healthy_hint: datetime | None = None  # adapter-known last-good time, if any


@dataclass(slots=True)
class ActionResult:
    ok: bool
    detail: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CheckContext:
    """Everything an adapter may use. No config secrets — only env-var *names*."""

    service_name: str
    params: dict[str, Any]
    now: datetime
    on_ac_power: bool | None
    slept_since_last_cycle: bool
    last_cycle_at: datetime | None
    state_dir: str
    dry_run: bool = False


class Adapter(ABC):
    """Base class for every adapter. Subclasses set ``kind`` and implement the three
    methods. Constructors take only ``params`` (validated against ``params_schema``)."""

    kind: str = ""

    def __init__(self, params: dict[str, Any]) -> None:
        self.params = params

    @classmethod
    @abstractmethod
    def describe(cls) -> AdapterManifest: ...

    @abstractmethod
    def check(self, ctx: CheckContext) -> CheckResult: ...

    def recover(self, action: str, ctx: CheckContext) -> ActionResult:
        """Default: no actions. Adapters that declare actions override this."""
        return ActionResult(ok=False, detail=f"adapter {self.kind!r} declares no action {action!r}")

    def validate_params(self) -> list[str]:
        """Return human-readable problems with ``self.params`` (empty = valid)."""
        schema = self.describe().params_schema
        return [f"unknown param {k!r}" for k in self.params if k not in schema]


__all__ = [
    "ActionResult",
    "ActionSpec",
    "Adapter",
    "AdapterManifest",
    "CheckContext",
    "CheckResult",
    "CheckSpec",
    "DataSource",
    "NotifyCondition",
    "Status",
]
