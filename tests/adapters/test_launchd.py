from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from keepalive.adapters.launchd import LaunchdAdapter
from keepalive.core.adapter import CheckContext, Status

LABEL = "com.example.worker"


def _ctx(tmp_path: Path, *, now: datetime, dry_run: bool = False) -> CheckContext:
    return CheckContext(
        service_name="svc",
        params={},
        now=now,
        on_ac_power=True,
        slept_since_last_cycle=False,
        last_cycle_at=None,
        state_dir=str(tmp_path),
        dry_run=dry_run,
    )


def _print_output(*, state: str, pid: int | None, last_exit_code: int = 0) -> str:
    pid_line = f"\tpid = {pid}\n" if pid is not None else ""
    return (
        f"gui/{os.getuid()}/{LABEL} = {{\n"
        f"\tactive count = 1\n"
        f"\tstate = {state}\n"
        f"{pid_line}"
        f"\tlast exit code = {last_exit_code}\n"
        f"}}\n"
    )


def _fake_run(
    *,
    print_returncode: int = 0,
    print_stdout: str = "",
    lstart: str | None = None,
    kickstart_returncode: int = 0,
) -> Any:
    def _run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["/bin/launchctl", "print"]:
            return subprocess.CompletedProcess(args, print_returncode, print_stdout, "")
        if args[0] == "/bin/ps":
            if lstart is None:
                return subprocess.CompletedProcess(args, 1, "", "ps: no such pid")
            return subprocess.CompletedProcess(args, 0, lstart + "\n", "")
        if args[:2] == ["/bin/launchctl", "kickstart"]:
            return subprocess.CompletedProcess(args, kickstart_returncode, "", "")
        raise AssertionError(f"unexpected subprocess args: {args}")

    return _run


def test_describe_declares_kickstart_starts_instance() -> None:
    manifest = LaunchdAdapter.describe()
    assert manifest.kind == "launchd"
    assert [a.name for a in manifest.actions] == ["kickstart"]
    assert manifest.actions[0].starts_instance is True


def test_validate_params_requires_label() -> None:
    adapter = LaunchdAdapter({})
    problems = adapter.validate_params()
    assert any("label" in p for p in problems)


def test_healthy_running_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    now = datetime(2026, 9, 10, 12, 0, 0)
    old_start = now - timedelta(seconds=500)
    monkeypatch.setattr(
        "keepalive.adapters.launchd.subprocess.run",
        _fake_run(
            print_stdout=_print_output(state="running", pid=555),
            lstart=old_start.strftime("%a %b %d %H:%M:%S %Y"),
        ),
    )
    adapter = LaunchdAdapter({"label": LABEL, "expect_running": True, "handoff_window_s": 90})
    result = adapter.check(_ctx(tmp_path, now=now))
    assert result.status == Status.OK
    assert result.instance_present is True


def test_crashed_job_not_loaded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    now = datetime(2026, 9, 10, 12, 0, 0)
    monkeypatch.setattr(
        "keepalive.adapters.launchd.subprocess.run",
        _fake_run(print_returncode=113, print_stdout=""),
    )
    adapter = LaunchdAdapter({"label": LABEL, "expect_running": True})
    result = adapter.check(_ctx(tmp_path, now=now))
    assert result.status == Status.FAILED


def test_loaded_but_no_pid_is_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    now = datetime(2026, 9, 10, 12, 0, 0)
    monkeypatch.setattr(
        "keepalive.adapters.launchd.subprocess.run",
        _fake_run(print_stdout=_print_output(state="not running", pid=None)),
    )
    adapter = LaunchdAdapter({"label": LABEL, "expect_running": True})
    result = adapter.check(_ctx(tmp_path, now=now))
    assert result.status == Status.FAILED
    assert result.instance_present is False


def test_handoff_window_is_expected_transition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    now = datetime(2026, 9, 10, 12, 0, 0)
    fresh_start = now - timedelta(seconds=30)  # younger than the 90s handoff window
    monkeypatch.setattr(
        "keepalive.adapters.launchd.subprocess.run",
        _fake_run(
            print_stdout=_print_output(state="running", pid=999),
            lstart=fresh_start.strftime("%a %b %d %H:%M:%S %Y"),
        ),
    )
    adapter = LaunchdAdapter({"label": LABEL, "expect_running": True, "handoff_window_s": 90})
    result = adapter.check(_ctx(tmp_path, now=now))
    assert result.status == Status.EXPECTED_TRANSITION
    assert result.instance_present is True
    assert result.handoff_until is not None
    assert result.handoff_until > now


def test_kickstart_runs_launchctl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    now = datetime(2026, 9, 10, 12, 0, 0)
    captured: dict[str, Any] = {}

    def _run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("keepalive.adapters.launchd.subprocess.run", _run)
    adapter = LaunchdAdapter({"label": LABEL})
    result = adapter.recover("kickstart", _ctx(tmp_path, now=now))
    assert result.ok is True
    assert captured["args"] == ["/bin/launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"]


def test_kickstart_dry_run_does_not_invoke_launchctl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    now = datetime(2026, 9, 10, 12, 0, 0)

    def _run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("subprocess.run should not be called on a dry run")

    monkeypatch.setattr("keepalive.adapters.launchd.subprocess.run", _run)
    adapter = LaunchdAdapter({"label": LABEL})
    result = adapter.recover("kickstart", _ctx(tmp_path, now=now, dry_run=True))
    assert result.ok is True


def test_recover_unknown_action_is_refused(tmp_path: Path) -> None:
    adapter = LaunchdAdapter({"label": LABEL})
    result = adapter.recover("restart", _ctx(tmp_path, now=datetime(2026, 9, 10)))
    assert result.ok is False


def test_launchctl_print_error_is_unknown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _raise(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise TimeoutError("launchctl hung")

    monkeypatch.setattr("keepalive.adapters.launchd.subprocess.run", _raise)
    adapter = LaunchdAdapter({"label": LABEL})
    result = adapter.check(_ctx(tmp_path, now=datetime(2026, 9, 10)))
    assert result.status == Status.UNKNOWN
