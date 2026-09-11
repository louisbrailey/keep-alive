"""Tests for keepalive.core.lock.

Real fcntl.flock calls against tmp_path files — flock locks are scoped to the
open file description, not the process, so two independent ``open()`` calls
from this same test process (via two ``InstanceLock`` instances) correctly
exercise the "already held" contention path without needing a subprocess.
"""

from __future__ import annotations

import os
from pathlib import Path

from keepalive.core.lock import InstanceLock


def test_acquires_when_free(tmp_path: Path) -> None:
    lock_path = tmp_path / "keepalive.lock"
    with InstanceLock(lock_path) as lock:
        assert lock.acquired is True
        assert lock_path.read_text(encoding="utf-8") == str(os.getpid())


def test_releases_on_exit(tmp_path: Path) -> None:
    lock_path = tmp_path / "keepalive.lock"
    with InstanceLock(lock_path) as lock:
        assert lock.acquired is True
    assert lock.acquired is False


def test_second_lock_fails_while_first_held(tmp_path: Path) -> None:
    lock_path = tmp_path / "keepalive.lock"
    with InstanceLock(lock_path) as first:
        assert first.acquired is True
        with InstanceLock(lock_path) as second:
            assert second.acquired is False


def test_lock_available_again_after_first_released(tmp_path: Path) -> None:
    lock_path = tmp_path / "keepalive.lock"
    with InstanceLock(lock_path) as first:
        assert first.acquired is True

    with InstanceLock(lock_path) as second:
        assert second.acquired is True


def test_creates_parent_directory(tmp_path: Path) -> None:
    lock_path = tmp_path / "nested" / "keepalive.lock"
    with InstanceLock(lock_path) as lock:
        assert lock.acquired is True
    assert lock_path.exists()


def test_writes_own_pid(tmp_path: Path) -> None:
    lock_path = tmp_path / "keepalive.lock"
    with InstanceLock(lock_path):
        pass
    assert lock_path.read_text(encoding="utf-8") == str(os.getpid())


def test_reacquire_overwrites_stale_pid(tmp_path: Path) -> None:
    lock_path = tmp_path / "keepalive.lock"
    lock_path.write_text("999999", encoding="utf-8")
    with InstanceLock(lock_path) as lock:
        assert lock.acquired is True
        assert lock_path.read_text(encoding="utf-8") == str(os.getpid())
