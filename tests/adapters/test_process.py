from __future__ import annotations

import fcntl
import os
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from keepalive.adapters.process import ProcessAdapter
from keepalive.core.adapter import CheckContext, Status


def _ctx(tmp_path: Path, *, dry_run: bool = False) -> CheckContext:
    return CheckContext(
        service_name="svc",
        params={},
        now=datetime(2026, 9, 10, 12, 0, 0),
        on_ac_power=True,
        slept_since_last_cycle=False,
        last_cycle_at=None,
        state_dir=str(tmp_path),
        dry_run=dry_run,
    )


def _fake_run(returncode: int, stdout: str = "", stderr: str = "") -> Any:
    def _run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    return _run


def test_describe_declares_signal_term_only() -> None:
    manifest = ProcessAdapter.describe()
    assert manifest.kind == "process"
    action_names = [a.name for a in manifest.actions]
    assert action_names == ["signal_term"]
    action = manifest.actions[0]
    assert action.destructive is True
    assert action.starts_instance is False


def test_validate_params_requires_pattern() -> None:
    adapter = ProcessAdapter({})
    problems = adapter.validate_params()
    assert any("pattern" in p for p in problems)


def test_healthy_process_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("keepalive.adapters.process.subprocess.run", _fake_run(0, stdout="4242\n"))
    adapter = ProcessAdapter({"pattern": "my-daemon --serve"})
    result = adapter.check(_ctx(tmp_path))
    assert result.status == Status.OK
    assert result.instance_present is True
    assert result.lock_held is False
    assert result.metrics["matched_pids"] == [4242]


def test_crashed_process_not_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("keepalive.adapters.process.subprocess.run", _fake_run(1, stdout=""))
    adapter = ProcessAdapter({"pattern": "my-daemon --serve"})
    result = adapter.check(_ctx(tmp_path))
    assert result.status == Status.FAILED
    assert result.instance_present is False


def test_unexpected_pgrep_error_is_unknown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "keepalive.adapters.process.subprocess.run", _fake_run(2, stderr="usage: pgrep ...")
    )
    adapter = ProcessAdapter({"pattern": "x"})
    result = adapter.check(_ctx(tmp_path))
    assert result.status == Status.UNKNOWN


def test_duplicate_instance_lock_held(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # pgrep finds nothing itself, but another process holds the application's lock file.
    monkeypatch.setattr("keepalive.adapters.process.subprocess.run", _fake_run(1, stdout=""))

    lock_path = tmp_path / "app.lock"
    lock_path.write_text("")
    holder_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT)
    fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        adapter = ProcessAdapter({"pattern": "x", "lock_file": str(lock_path)})
        result = adapter.check(_ctx(tmp_path))
        assert result.lock_held is True
        assert result.instance_present is True
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)


def test_lock_not_held_when_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("keepalive.adapters.process.subprocess.run", _fake_run(1, stdout=""))
    lock_path = tmp_path / "app.lock"
    adapter = ProcessAdapter({"pattern": "x", "lock_file": str(lock_path)})
    result = adapter.check(_ctx(tmp_path))
    assert result.lock_held is False
    assert result.instance_present is False


def test_pattern_with_shell_metacharacters_is_single_argv_element(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    def _run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 1, "", "")

    monkeypatch.setattr("keepalive.adapters.process.subprocess.run", _run)
    dangerous = "foo; rm -rf / #$(whoami)`id`"
    adapter = ProcessAdapter({"pattern": dangerous})
    adapter.check(_ctx(tmp_path))

    args = captured["args"]
    assert args == ["/usr/bin/pgrep", "-f", "--", dangerous]
    assert args[-1] == dangerous  # unexpanded, a single argv element
    assert captured["kwargs"].get("shell", False) is False


def test_signal_term_sends_sigterm_to_matched_pid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("keepalive.adapters.process.subprocess.run", _fake_run(0, stdout="777\n"))
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        "keepalive.adapters.process.os.kill", lambda pid, sig: killed.append((pid, sig))
    )
    adapter = ProcessAdapter({"pattern": "x"})
    result = adapter.recover("signal_term", _ctx(tmp_path))
    assert result.ok is True
    assert killed == [(777, signal.SIGTERM)]


def test_signal_term_prefers_pid_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("keepalive.adapters.process.subprocess.run", _fake_run(0, stdout="777\n"))
    pid_file = tmp_path / "app.pid"
    pid_file.write_text("999\n")
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        "keepalive.adapters.process.os.kill", lambda pid, sig: killed.append((pid, sig))
    )
    adapter = ProcessAdapter({"pattern": "x", "pid_file": str(pid_file)})
    result = adapter.recover("signal_term", _ctx(tmp_path))
    assert result.ok is True
    assert killed == [(999, signal.SIGTERM)]


def test_signal_term_dry_run_does_not_kill(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("keepalive.adapters.process.subprocess.run", _fake_run(0, stdout="777\n"))
    called: list[Any] = []
    monkeypatch.setattr("keepalive.adapters.process.os.kill", lambda *a: called.append(a))
    adapter = ProcessAdapter({"pattern": "x"})
    result = adapter.recover("signal_term", _ctx(tmp_path, dry_run=True))
    assert result.ok is True
    assert called == []


def test_signal_term_no_process_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("keepalive.adapters.process.subprocess.run", _fake_run(1, stdout=""))
    adapter = ProcessAdapter({"pattern": "x"})
    result = adapter.recover("signal_term", _ctx(tmp_path))
    assert result.ok is False


def test_recover_unknown_action_is_refused(tmp_path: Path) -> None:
    adapter = ProcessAdapter({"pattern": "x"})
    result = adapter.recover("restart", _ctx(tmp_path))
    assert result.ok is False
