"""Per-service persisted state.

Stored as JSON at ``<state_dir>/state.json``: ``{"services": {name: {...}},
"meta": {...}}``. ``meta`` carries cycle-level facts (``last_cycle_at``,
``last_power``) that don't belong to any one service; ``StateStore`` exposes
it as a plain attribute — ``load()`` populates it as a side effect, and
``save()`` writes back whatever it currently holds, so the engine sets
``store.meta = {...}`` before calling ``save()``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from keepalive.core.timeutil import ensure_aware


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_aware(value, "local")
    # A legacy naive-local ISO string, persisted before timestamps became
    # aware everywhere (see keepalive.core.timeutil), is assumed local.
    return ensure_aware(datetime.fromisoformat(str(value)), "local")


@dataclass(slots=True)
class Incident:
    kind: str
    opened_at: datetime
    attempts: int = 0
    next_retry_at: datetime | None = None
    cooldown_until: datetime | None = None
    circuit_open_until: datetime | None = None
    last_action: str | None = None
    last_action_at: datetime | None = None
    last_action_ok: bool | None = None
    last_action_detail: str = ""
    notified_opened: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "opened_at": _iso(self.opened_at),
            "attempts": self.attempts,
            "next_retry_at": _iso(self.next_retry_at),
            "cooldown_until": _iso(self.cooldown_until),
            "circuit_open_until": _iso(self.circuit_open_until),
            "last_action": self.last_action,
            "last_action_at": _iso(self.last_action_at),
            "last_action_ok": self.last_action_ok,
            "last_action_detail": self.last_action_detail,
            "notified_opened": self.notified_opened,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Incident:
        opened_at = _parse_dt(data["opened_at"])
        assert opened_at is not None
        return cls(
            kind=str(data["kind"]),
            opened_at=opened_at,
            attempts=int(data.get("attempts", 0)),
            next_retry_at=_parse_dt(data.get("next_retry_at")),
            cooldown_until=_parse_dt(data.get("cooldown_until")),
            circuit_open_until=_parse_dt(data.get("circuit_open_until")),
            last_action=data.get("last_action"),
            last_action_at=_parse_dt(data.get("last_action_at")),
            last_action_ok=data.get("last_action_ok"),
            last_action_detail=str(data.get("last_action_detail", "")),
            notified_opened=bool(data.get("notified_opened", False)),
        )


@dataclass(slots=True)
class ServiceState:
    name: str
    status: str = "unknown"
    last_check_at: datetime | None = None
    last_healthy_at: datetime | None = None
    last_detail: str = ""
    consecutive_failures: int = 0
    consecutive_unknown: int = 0
    incident: Incident | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "last_check_at": _iso(self.last_check_at),
            "last_healthy_at": _iso(self.last_healthy_at),
            "last_detail": self.last_detail,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_unknown": self.consecutive_unknown,
            "incident": self.incident.to_dict() if self.incident is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ServiceState:
        incident_data = data.get("incident")
        return cls(
            name=str(data["name"]),
            status=str(data.get("status", "unknown")),
            last_check_at=_parse_dt(data.get("last_check_at")),
            last_healthy_at=_parse_dt(data.get("last_healthy_at")),
            last_detail=str(data.get("last_detail", "")),
            consecutive_failures=int(data.get("consecutive_failures", 0)),
            consecutive_unknown=int(data.get("consecutive_unknown", 0)),
            incident=Incident.from_dict(incident_data) if incident_data else None,
        )


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.meta: dict[str, Any] = {}

    def load(self) -> dict[str, ServiceState]:
        raw = self._read_raw()
        meta = raw.get("meta", {})
        self.meta = dict(meta) if isinstance(meta, dict) else {}
        services = raw.get("services", {})
        if not isinstance(services, dict):
            return {}
        return {name: ServiceState.from_dict(data) for name, data in services.items()}

    def save(self, states: dict[str, ServiceState]) -> None:
        payload = {
            "services": {name: state.to_dict() for name, state in states.items()},
            "meta": self.meta,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self.path)

    def get(self, name: str) -> ServiceState:
        states = self.load()
        return states.get(name, ServiceState(name=name))

    def _read_raw(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}


__all__ = ["Incident", "ServiceState", "StateStore"]
