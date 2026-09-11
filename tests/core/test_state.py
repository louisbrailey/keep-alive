"""Tests for keepalive.core.state."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from keepalive.core.state import Incident, ServiceState, StateStore


def test_service_state_defaults() -> None:
    state = ServiceState(name="svc")
    assert state.status == "unknown"
    assert state.consecutive_failures == 0
    assert state.consecutive_unknown == 0
    assert state.incident is None


def test_incident_round_trip_through_dict() -> None:
    incident = Incident(
        kind="failure",
        opened_at=datetime(2026, 1, 1, 0, 0, 0),
        attempts=2,
        next_retry_at=datetime(2026, 1, 1, 0, 5, 0),
        cooldown_until=datetime(2026, 1, 1, 0, 10, 0),
        circuit_open_until=None,
        last_action="restart",
        last_action_at=datetime(2026, 1, 1, 0, 1, 0),
        last_action_ok=True,
        last_action_detail="ok",
        notified_opened=True,
    )
    restored = Incident.from_dict(incident.to_dict())
    assert restored == incident


def test_service_state_round_trip_through_dict() -> None:
    state = ServiceState(
        name="svc",
        status="failed",
        last_check_at=datetime(2026, 1, 1, 0, 0, 0),
        last_healthy_at=datetime(2025, 12, 31, 0, 0, 0),
        last_detail="down",
        consecutive_failures=3,
        consecutive_unknown=0,
        incident=Incident(kind="failure", opened_at=datetime(2026, 1, 1, 0, 0, 0)),
    )
    restored = ServiceState.from_dict(state.to_dict())
    assert restored == state


def test_store_load_on_missing_file_returns_empty(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    assert store.load() == {}
    assert store.meta == {}


def test_store_save_then_load_round_trips(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    states = {
        "svc-a": ServiceState(name="svc-a", status="ok"),
        "svc-b": ServiceState(
            name="svc-b",
            status="failed",
            consecutive_failures=2,
            incident=Incident(kind="failure", opened_at=datetime(2026, 1, 1)),
        ),
    }
    store.meta = {"last_cycle_at": "2026-01-01T00:00:00", "last_power": {"on_ac": True}}
    store.save(states)

    reloaded_store = StateStore(tmp_path / "state.json")
    loaded = reloaded_store.load()
    assert loaded == states
    assert reloaded_store.meta == {
        "last_cycle_at": "2026-01-01T00:00:00",
        "last_power": {"on_ac": True},
    }


def test_get_returns_default_state_for_unknown_service(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    state = store.get("nonexistent")
    assert state == ServiceState(name="nonexistent")


def test_get_returns_persisted_state(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.save({"svc": ServiceState(name="svc", status="ok")})
    assert store.get("svc").status == "ok"


def test_save_creates_parent_directories(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "state.json"
    store = StateStore(nested)
    store.save({"svc": ServiceState(name="svc")})
    assert nested.exists()


def test_corrupted_json_file_is_treated_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not valid json", encoding="utf-8")
    store = StateStore(path)
    assert store.load() == {}


def test_json_file_holding_a_list_is_treated_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    store = StateStore(path)
    assert store.load() == {}


def test_save_writes_atomically_no_leftover_tmp_file(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.save({"svc": ServiceState(name="svc")})
    assert path.exists()
    assert not (tmp_path / "state.json.tmp").exists()
