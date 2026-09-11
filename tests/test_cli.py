"""Tests for keepalive.cli using typer.testing.CliRunner.

No real launchctl or osascript ever runs: launchd tests monkeypatch
``keepalive.cli.subprocess.run`` and ``Path.home`` so nothing touches the
real system, and notification tests use a config with macos/webhook both
off so ``Notifier`` never shells out.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from keepalive.cli import _EXAMPLE_CONFIG_PATH, _REPO_DIR, app
from keepalive.core.config import default_config_path
from keepalive.core.engine import kill_switch_path

runner = CliRunner()

_CONFIG_TEMPLATE = """
version = 1

[keepalive]
mode = "{mode}"
poll_interval_s = 60
state_dir = "{state_dir}"
failure_threshold = 2
unknown_threshold = 2
audit_max_bytes = 1000000

[notifications]
macos = false
webhook = false
webhook_url_env = "KEEPALIVE_WEBHOOK_URL"
summary_daily_at = ""
notify_on = ["incident_opened", "recovered", "circuit_open", "check_unavailable"]

[power]
observe = false
wake_windows = false
allow_on_battery = false
max_wake_minutes = 120

[[services]]
name = "root-disk"
adapter = "disk"
enabled = true
[services.params]
path = "/"
warn_free_pct = 15
fail_free_pct = 5
"""


@pytest.fixture
def env(tmp_path: Path) -> tuple[Path, Path]:
    """A (config_path, state_dir) pair for a minimal, valid config."""
    state_dir = tmp_path / "state"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        _CONFIG_TEMPLATE.format(mode="observe", state_dir=str(state_dir)), encoding="utf-8"
    )
    return config_path, state_dir


# --------------------------------------------------------------------------- #
# check / run
# --------------------------------------------------------------------------- #


def test_check_runs_one_cycle_and_writes_state(env: tuple[Path, Path]) -> None:
    config_path, state_dir = env
    result = runner.invoke(app, ["check", "--config", str(config_path), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "root-disk" in result.output
    assert (state_dir / "state.json").exists()


def test_check_reports_lock_held(env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    config_path, state_dir = env
    state_dir.mkdir(parents=True)
    lock_path = state_dir / "keepalive.lock"
    lock_path.write_text("held", encoding="utf-8")

    # Hold the lock ourselves for the duration of the invocation.
    import fcntl

    handle = lock_path.open("a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = runner.invoke(app, ["check", "--config", str(config_path)])
        assert result.exit_code == 1
        assert "skipped" in result.output
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def test_check_bad_config_exits_nonzero(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("version = 2\n", encoding="utf-8")
    result = runner.invoke(app, ["check", "--config", str(config_path)])
    assert result.exit_code == 1
    assert "config error" in result.output or "version" in result.output


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


def test_status_json_before_any_check(env: tuple[Path, Path]) -> None:
    config_path, _state_dir = env
    result = runner.invoke(app, ["status", "--config", str(config_path), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "observe"
    assert payload["kill_switch"] is False
    assert payload["services"][0]["name"] == "root-disk"
    assert payload["services"][0]["status"] == "unknown"


def test_status_table_reflects_check(env: tuple[Path, Path]) -> None:
    config_path, _state_dir = env
    check_result = runner.invoke(app, ["check", "--config", str(config_path)])
    assert check_result.exit_code == 0, check_result.output

    result = runner.invoke(app, ["status", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    assert "root-disk" in result.output
    assert "disk" in result.output
    assert "kill switch" in result.output


def test_status_json_reflects_kill_switch(env: tuple[Path, Path]) -> None:
    config_path, _state_dir = env
    on_result = runner.invoke(app, ["kill", "on", "--config", str(config_path), "--reason", "t"])
    assert on_result.exit_code == 0, on_result.output

    result = runner.invoke(app, ["status", "--config", str(config_path), "--json"])
    payload = json.loads(result.output)
    assert payload["kill_switch"] is True


# --------------------------------------------------------------------------- #
# kill
# --------------------------------------------------------------------------- #


def test_kill_on_then_off(env: tuple[Path, Path]) -> None:
    config_path, _state_dir = env
    from keepalive.core.config import load_config

    cfg = load_config(config_path)
    kswitch = kill_switch_path(cfg.keepalive)

    on_result = runner.invoke(
        app, ["kill", "on", "--config", str(config_path), "--reason", "testing"]
    )
    assert on_result.exit_code == 0, on_result.output
    assert kswitch.exists()
    assert "testing" in kswitch.read_text(encoding="utf-8")

    off_result = runner.invoke(app, ["kill", "off", "--config", str(config_path)])
    assert off_result.exit_code == 0, off_result.output
    assert not kswitch.exists()


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


def test_config_validate_valid(env: tuple[Path, Path]) -> None:
    config_path, _state_dir = env
    result = runner.invoke(app, ["config", "validate", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    assert "valid" in result.output


def test_config_validate_invalid(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('version = 1\n[keepalive]\nmode = "bogus"\n', encoding="utf-8")
    result = runner.invoke(app, ["config", "validate", "--config", str(config_path)])
    assert result.exit_code == 1
    assert "invalid" in result.output


def test_config_show_example_matches_file() -> None:
    result = runner.invoke(app, ["config", "show-example"])
    assert result.exit_code == 0
    assert result.output == _EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8")


def test_config_path_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KEEPALIVE_CONFIG", raising=False)
    result = runner.invoke(app, ["config", "path"])
    assert result.exit_code == 0
    assert result.output.strip() == str(default_config_path())


# --------------------------------------------------------------------------- #
# adapters
# --------------------------------------------------------------------------- #


def test_adapters_json_lists_builtins() -> None:
    result = runner.invoke(app, ["adapters", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    kinds = {entry["kind"] for entry in payload}
    assert {"process", "http", "launchd", "disk", "job_freshness", "power"} <= kinds


def test_adapters_table_renders() -> None:
    result = runner.invoke(app, ["adapters"])
    assert result.exit_code == 0
    assert "disk" in result.output


# --------------------------------------------------------------------------- #
# audit
# --------------------------------------------------------------------------- #


def test_audit_tail_after_check(env: tuple[Path, Path]) -> None:
    config_path, _state_dir = env
    check_result = runner.invoke(app, ["check", "--config", str(config_path)])
    assert check_result.exit_code == 0, check_result.output

    result = runner.invoke(app, ["audit", "--config", str(config_path), "--tail", "5"])
    assert result.exit_code == 0, result.output
    assert "check" in result.output


def test_audit_empty_when_no_records(env: tuple[Path, Path]) -> None:
    config_path, _state_dir = env
    result = runner.invoke(app, ["audit", "--config", str(config_path)])
    assert result.exit_code == 0
    assert "no audit records" in result.output


# --------------------------------------------------------------------------- #
# summary
# --------------------------------------------------------------------------- #


def test_summary_prints_and_records_audit(env: tuple[Path, Path]) -> None:
    config_path, state_dir = env
    result = runner.invoke(app, ["summary", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    assert "KeepAlive summary" in result.output
    assert "no notification channel is enabled" in result.output

    audit_text = (state_dir / "audit.jsonl").read_text(encoding="utf-8")
    assert "summary_requested" in audit_text


# --------------------------------------------------------------------------- #
# launchd
# --------------------------------------------------------------------------- #


def test_launchd_render_substitutes_paths(env: tuple[Path, Path]) -> None:
    config_path, _state_dir = env
    result = runner.invoke(app, ["launchd", "render", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    assert str(_REPO_DIR) in result.output
    assert str(config_path) in result.output
    assert "__KEEPALIVE_REPO_DIR__" not in result.output
    assert "__KEEPALIVE_CONFIG_PATH__" not in result.output
    assert "__HOME__" not in result.output


def test_launchd_install_without_yes_makes_no_changes(
    env: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, _state_dir = env
    fake_dest = tmp_path / "LaunchAgents" / "com.keepalive.agent.plist"
    monkeypatch.setattr("keepalive.cli._PLIST_DEST", fake_dest)

    def _forbidden_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be called without --yes")

    monkeypatch.setattr("keepalive.cli.subprocess.run", _forbidden_run)

    result = runner.invoke(app, ["launchd", "install", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    assert "Nothing changed" in result.output
    assert not fake_dest.exists()


def test_launchd_install_with_yes_invokes_launchctl(
    env: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, _state_dir = env
    fake_home = tmp_path / "fake_home"
    fake_dest = fake_home / "Library" / "LaunchAgents" / "com.keepalive.agent.plist"
    monkeypatch.setattr("keepalive.cli._PLIST_DEST", fake_dest)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("keepalive.cli.subprocess.run", _fake_run)

    result = runner.invoke(app, ["launchd", "install", "--config", str(config_path), "--yes"])
    assert result.exit_code == 0, result.output
    assert fake_dest.exists()
    assert calls == [["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(fake_dest)]]
    assert (fake_home / "Library" / "Logs" / "keepalive").exists()


def test_launchd_install_reports_launchctl_failure(
    env: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, _state_dir = env
    fake_home = tmp_path / "fake_home"
    fake_dest = fake_home / "Library" / "LaunchAgents" / "com.keepalive.agent.plist"
    monkeypatch.setattr("keepalive.cli._PLIST_DEST", fake_dest)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    monkeypatch.setattr("keepalive.cli.subprocess.run", _fake_run)

    result = runner.invoke(app, ["launchd", "install", "--config", str(config_path), "--yes"])
    assert result.exit_code == 1
    assert "boom" in result.output


def test_launchd_uninstall_with_yes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_dest = tmp_path / "com.keepalive.agent.plist"
    fake_dest.write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr("keepalive.cli._PLIST_DEST", fake_dest)

    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("keepalive.cli.subprocess.run", _fake_run)

    result = runner.invoke(app, ["launchd", "uninstall", "--yes"])
    assert result.exit_code == 0, result.output
    assert not fake_dest.exists()
    assert len(calls) == 1
    assert calls[0][:2] == ["/bin/launchctl", "bootout"]


def test_launchd_uninstall_without_yes_leaves_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_dest = tmp_path / "com.keepalive.agent.plist"
    fake_dest.write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr("keepalive.cli._PLIST_DEST", fake_dest)

    def _forbidden_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be called without --yes")

    monkeypatch.setattr("keepalive.cli.subprocess.run", _forbidden_run)

    result = runner.invoke(app, ["launchd", "uninstall"])
    assert result.exit_code == 0, result.output
    assert fake_dest.exists()
