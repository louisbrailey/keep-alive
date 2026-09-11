"""Tests for keepalive.adapters.disk.DiskAdapter."""

from __future__ import annotations

import shutil
from datetime import datetime

import pytest

from keepalive.adapters import disk as disk_module
from keepalive.adapters.disk import DiskAdapter
from keepalive.core.adapter import CheckContext, Status


class _FakeUsage:
    def __init__(self, total: int, used: int, free: int) -> None:
        self.total = total
        self.used = used
        self.free = free


def _ctx(path: str) -> CheckContext:
    return CheckContext(
        service_name="root-disk",
        params={},
        now=datetime(2024, 1, 1, 12, 0, 0),
        on_ac_power=True,
        slept_since_last_cycle=False,
        last_cycle_at=None,
        state_dir="/tmp/keepalive-state",
    )


def _adapter(**overrides: object) -> DiskAdapter:
    params = {"path": "/", "warn_free_pct": 15, "fail_free_pct": 5}
    params.update(overrides)
    return DiskAdapter(params)


def test_describe_manifest_has_no_actions() -> None:
    manifest = DiskAdapter.describe()
    assert manifest.kind == "disk"
    assert manifest.actions == ()
    assert manifest.checks


def test_describe_manifest_declares_notify_conditions() -> None:
    # No actions => no circuit_open, but incident_opened/recovered/check_unavailable
    # can all still fire through the generic policy pipeline.
    manifest = DiskAdapter.describe()
    events = {c.event for c in manifest.notify_on}
    assert events == {"incident_opened", "recovered", "check_unavailable"}


def test_validate_params_ok() -> None:
    assert _adapter().validate_params() == []


def test_validate_params_missing_required() -> None:
    problems = DiskAdapter({}).validate_params()
    assert any("path" in p for p in problems)
    assert any("warn_free_pct" in p for p in problems)
    assert any("fail_free_pct" in p for p in problems)


def test_validate_params_unknown_key() -> None:
    problems = _adapter(bogus=1).validate_params()
    assert any("bogus" in p for p in problems)


def test_healthy_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(disk_module.shutil, "disk_usage", lambda _path: _FakeUsage(100, 50, 50))
    result = _adapter().check(_ctx("/"))
    assert result.status == Status.OK
    assert result.metrics["free_pct"] == pytest.approx(50.0)
    assert result.metrics["free_bytes"] == 50


def test_disk_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    # 10% free: below warn_free_pct=15, above fail_free_pct=5.
    monkeypatch.setattr(disk_module.shutil, "disk_usage", lambda _path: _FakeUsage(100, 90, 10))
    result = _adapter().check(_ctx("/"))
    assert result.status == Status.DEGRADED
    assert result.metrics["free_pct"] == pytest.approx(10.0)


def test_disk_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    # 2% free: below fail_free_pct=5.
    monkeypatch.setattr(disk_module.shutil, "disk_usage", lambda _path: _FakeUsage(100, 98, 2))
    result = _adapter().check(_ctx("/"))
    assert result.status == Status.FAILED
    assert result.metrics["free_pct"] == pytest.approx(2.0)


def test_unreadable_path_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_path: str) -> _FakeUsage:
        raise OSError("no such path")

    monkeypatch.setattr(disk_module.shutil, "disk_usage", _raise)
    result = _adapter(path="/does/not/exist").check(_ctx("/does/not/exist"))
    assert result.status == Status.UNKNOWN


def test_real_disk_usage_smoke() -> None:
    # Sanity check against the real filesystem (no monkeypatch) so we know the
    # adapter's use of shutil.disk_usage matches the stdlib signature.
    usage = shutil.disk_usage("/")
    assert usage.total > 0
    result = _adapter(path="/", warn_free_pct=0, fail_free_pct=0).check(_ctx("/"))
    assert result.status == Status.OK
