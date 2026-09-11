"""Tests for keepalive.core.config."""

from __future__ import annotations

from pathlib import Path

import pytest

from keepalive.core.config import (
    Config,
    ConfigError,
    default_config_path,
    load_config,
)

_MINIMAL = """
version = 1

[keepalive]
mode = "observe"
poll_interval_s = 60
state_dir = "~/state"
failure_threshold = 3
unknown_threshold = 5
audit_max_bytes = 1000000

[notifications]
macos = false
webhook = false
webhook_url_env = "KEEPALIVE_WEBHOOK_URL"
summary_daily_at = ""
notify_on = ["incident_opened", "recovered"]

[power]
observe = true
wake_windows = false
allow_on_battery = false
max_wake_minutes = 120
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_default_config_path_uses_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEEPALIVE_CONFIG", "~/somewhere/config.toml")
    assert default_config_path() == Path("~/somewhere/config.toml").expanduser()


def test_default_config_path_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KEEPALIVE_CONFIG", raising=False)
    assert default_config_path() == Path("~/.config/keepalive/config.toml").expanduser()


def test_loads_example_toml() -> None:
    cfg = load_config(Path(__file__).resolve().parents[2] / "config" / "example.toml")
    assert isinstance(cfg, Config)
    assert cfg.version == 1
    assert cfg.keepalive.mode == "observe"
    names = [s.name for s in cfg.services]
    assert "example-http" in names
    assert "signal-forge" in names
    sf = next(s for s in cfg.services if s.name == "signal-forge")
    assert sf.enabled is False
    assert sf.recovery.allowed_actions == ["restart_worker"]


def test_minimal_config_loads_with_no_services(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _MINIMAL))
    assert cfg.services == []
    assert cfg.keepalive.state_dir == Path("~/state").expanduser()
    assert cfg.notifications.notify_on == ["incident_opened", "recovered"]


def test_missing_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read config file"):
        load_config(tmp_path / "nope.toml")


def test_invalid_toml_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(_write(tmp_path, "this is not [ valid toml"))


def test_wrong_version_rejected(tmp_path: Path) -> None:
    text = _MINIMAL.replace("version = 1", "version = 2")
    with pytest.raises(ConfigError, match="version"):
        load_config(_write(tmp_path, text))


def test_unknown_top_level_key_rejected(tmp_path: Path) -> None:
    # Inserted before any [section] so it lands in the root table, not the
    # last-opened one (TOML has no block scoping otherwise).
    text = _MINIMAL.replace("version = 1", "version = 1\nbogus = true")
    with pytest.raises(ConfigError, match="unknown config key: bogus"):
        load_config(_write(tmp_path, text))


def test_missing_top_level_section_rejected(tmp_path: Path) -> None:
    text = _MINIMAL.replace("[power]\n", "").replace(
        "observe = true\nwake_windows = false\nallow_on_battery = false\nmax_wake_minutes = 120\n",
        "",
    )
    with pytest.raises(ConfigError, match="missing required key"):
        load_config(_write(tmp_path, text))


def test_unknown_keepalive_key_rejected(tmp_path: Path) -> None:
    text = _MINIMAL.replace('mode = "observe"', 'mode = "observe"\nbogus = 1')
    with pytest.raises(ConfigError, match=r"unknown config key: keepalive\.bogus"):
        load_config(_write(tmp_path, text))


def test_invalid_mode_rejected(tmp_path: Path) -> None:
    text = _MINIMAL.replace('mode = "observe"', 'mode = "sleep"')
    with pytest.raises(ConfigError, match=r"keepalive\.mode"):
        load_config(_write(tmp_path, text))


def test_unknown_notify_event_rejected(tmp_path: Path) -> None:
    text = _MINIMAL.replace(
        'notify_on = ["incident_opened", "recovered"]',
        'notify_on = ["incident_opened", "made_up_event"]',
    )
    with pytest.raises(ConfigError, match="unknown event"):
        load_config(_write(tmp_path, text))


def test_notify_on_must_be_list_of_strings(tmp_path: Path) -> None:
    text = _MINIMAL.replace('notify_on = ["incident_opened", "recovered"]', "notify_on = [1, 2]")
    with pytest.raises(ConfigError, match="notify_on"):
        load_config(_write(tmp_path, text))


def test_unknown_adapter_kind_rejected(tmp_path: Path) -> None:
    text = _MINIMAL + """
[[services]]
name = "svc"
adapter = "not_a_real_adapter"
enabled = true
"""
    with pytest.raises(ConfigError, match="unknown adapter kind"):
        load_config(_write(tmp_path, text))


def test_duplicate_service_name_rejected(tmp_path: Path) -> None:
    text = _MINIMAL + """
[[services]]
name = "svc"
adapter = "http"
enabled = true
[services.params]
url = "http://x"

[[services]]
name = "svc"
adapter = "http"
enabled = true
[services.params]
url = "http://y"
"""
    with pytest.raises(ConfigError, match="duplicate service name"):
        load_config(_write(tmp_path, text))


def test_service_missing_required_key_rejected(tmp_path: Path) -> None:
    text = _MINIMAL + """
[[services]]
name = "svc"
adapter = "http"
"""
    with pytest.raises(ConfigError, match="missing required key"):
        load_config(_write(tmp_path, text))


def test_service_unknown_key_rejected(tmp_path: Path) -> None:
    text = _MINIMAL + """
[[services]]
name = "svc"
adapter = "http"
enabled = true
bogus = 1
"""
    with pytest.raises(ConfigError, match=r"unknown config key: services\[0\]\.bogus"):
        load_config(_write(tmp_path, text))


def test_service_recovery_defaults_and_overrides(tmp_path: Path) -> None:
    text = _MINIMAL + """
[[services]]
name = "svc"
adapter = "http"
enabled = true
[services.params]
url = "http://x"
[services.recovery]
enabled = true
allowed_actions = ["restart"]
cooldown_s = 10
max_attempts = 5
backoff_base_s = 1
backoff_max_s = 2
circuit_open_s = 3
failure_threshold = 7
"""
    cfg = load_config(_write(tmp_path, text))
    recovery = cfg.services[0].recovery
    assert recovery.enabled is True
    assert recovery.allowed_actions == ["restart"]
    assert recovery.cooldown_s == 10
    assert recovery.failure_threshold == 7


def test_service_recovery_table_optional_uses_defaults(tmp_path: Path) -> None:
    text = _MINIMAL + """
[[services]]
name = "svc"
adapter = "http"
enabled = true
[services.params]
url = "http://x"
"""
    cfg = load_config(_write(tmp_path, text))
    recovery = cfg.services[0].recovery
    assert recovery.enabled is False
    assert recovery.allowed_actions == []
    assert recovery.failure_threshold is None


def test_recovery_unknown_key_rejected(tmp_path: Path) -> None:
    text = _MINIMAL + """
[[services]]
name = "svc"
adapter = "http"
enabled = true
[services.params]
url = "http://x"
[services.recovery]
bogus = true
"""
    with pytest.raises(ConfigError, match="unknown config key"):
        load_config(_write(tmp_path, text))


def test_service_params_expand_home_left_to_adapter(tmp_path: Path) -> None:
    # config.py does not itself expanduser() inside params (adapters own their
    # own path params); it only expands keepalive.state_dir.
    text = _MINIMAL + """
[[services]]
name = "svc"
adapter = "disk"
enabled = true
[services.params]
path = "~/somewhere"
"""
    cfg = load_config(_write(tmp_path, text))
    assert cfg.services[0].params["path"] == "~/somewhere"


def test_state_dir_is_expanded(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _MINIMAL))
    assert "~" not in str(cfg.keepalive.state_dir)


def test_services_must_be_a_list(tmp_path: Path) -> None:
    # Inserted before any [section] so it lands in the root table.
    text = _MINIMAL.replace("version = 1", 'version = 1\nservices = "not-a-list"')
    with pytest.raises(ConfigError, match="services: must be a list of tables"):
        load_config(_write(tmp_path, text))


def test_service_entry_must_be_a_table(tmp_path: Path) -> None:
    text = _MINIMAL.replace("version = 1", "version = 1\nservices = [1, 2]")
    with pytest.raises(ConfigError, match=r"services\[0\]: must be a table"):
        load_config(_write(tmp_path, text))


def test_service_params_must_be_a_table(tmp_path: Path) -> None:
    text = _MINIMAL + """
[[services]]
name = "svc"
adapter = "http"
enabled = true
params = "not-a-table"
"""
    with pytest.raises(ConfigError, match=r"services\[0\].params: must be a table"):
        load_config(_write(tmp_path, text))


def test_service_recovery_must_be_a_table(tmp_path: Path) -> None:
    text = _MINIMAL + """
[[services]]
name = "svc"
adapter = "http"
enabled = true
recovery = "not-a-table"
"""
    with pytest.raises(ConfigError, match=r"services\[0\].recovery: must be a table"):
        load_config(_write(tmp_path, text))


def test_recovery_allowed_actions_must_be_list_of_strings(tmp_path: Path) -> None:
    text = _MINIMAL + """
[[services]]
name = "svc"
adapter = "http"
enabled = true
[services.recovery]
allowed_actions = [1, 2]
"""
    with pytest.raises(ConfigError, match="allowed_actions: must be a list of strings"):
        load_config(_write(tmp_path, text))
