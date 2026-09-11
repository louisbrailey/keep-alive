"""Versioned, secret-free TOML configuration.

Unknown top-level keys, unknown keys within a known section, and unknown
adapter kinds are all validation errors that name the offending key path —
this is meant to catch typos in a config the user hand-edits, not to be
lenient about them.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from keepalive.core.registry import known_adapter_kinds


class ConfigError(ValueError):
    """Raised for structurally invalid or unrecognized configuration."""


@dataclass(slots=True)
class KeepAliveSettings:
    mode: str
    poll_interval_s: int
    state_dir: Path
    failure_threshold: int
    unknown_threshold: int
    audit_max_bytes: int


@dataclass(slots=True)
class NotificationSettings:
    macos: bool
    webhook: bool
    webhook_url_env: str
    summary_daily_at: str
    notify_on: list[str]


@dataclass(slots=True)
class PowerSettings:
    observe: bool
    wake_windows: bool
    allow_on_battery: bool
    max_wake_minutes: int


@dataclass(slots=True)
class RecoveryPolicy:
    enabled: bool = False
    allowed_actions: list[str] = field(default_factory=list)
    cooldown_s: int = 300
    max_attempts: int = 3
    backoff_base_s: int = 30
    backoff_max_s: int = 900
    circuit_open_s: int = 3600
    failure_threshold: int | None = None


@dataclass(slots=True)
class ServiceConfig:
    name: str
    adapter: str
    enabled: bool
    params: dict[str, Any]
    recovery: RecoveryPolicy


@dataclass(slots=True)
class Config:
    version: int
    keepalive: KeepAliveSettings
    notifications: NotificationSettings
    power: PowerSettings
    services: list[ServiceConfig]


_VALID_MODES = {"observe", "recover"}
_VALID_NOTIFY_EVENTS = {"incident_opened", "recovered", "circuit_open", "check_unavailable"}

_KEEPALIVE_KEYS = {
    "mode",
    "poll_interval_s",
    "state_dir",
    "failure_threshold",
    "unknown_threshold",
    "audit_max_bytes",
}
_NOTIFICATION_KEYS = {"macos", "webhook", "webhook_url_env", "summary_daily_at", "notify_on"}
_POWER_KEYS = {"observe", "wake_windows", "allow_on_battery", "max_wake_minutes"}
_SERVICE_KEYS = {"name", "adapter", "enabled", "params", "recovery"}
_SERVICE_REQUIRED_KEYS = {"name", "adapter", "enabled"}
_RECOVERY_KEYS = {
    "enabled",
    "allowed_actions",
    "cooldown_s",
    "max_attempts",
    "backoff_base_s",
    "backoff_max_s",
    "circuit_open_s",
    "failure_threshold",
}
_TOP_LEVEL_KEYS = {"version", "keepalive", "notifications", "power", "services"}


def default_config_path() -> Path:
    """``$KEEPALIVE_CONFIG`` if set, else ``~/.config/keepalive/config.toml``."""
    env_value = os.environ.get("KEEPALIVE_CONFIG")
    if env_value:
        return Path(env_value).expanduser()
    return Path("~/.config/keepalive/config.toml").expanduser()


def _key_path(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


def _check_unknown(table: dict[str, Any], allowed: set[str], prefix: str) -> None:
    unknown = sorted(k for k in table if k not in allowed)
    if unknown:
        raise ConfigError(f"unknown config key: {_key_path(prefix, unknown[0])}")


def _check_missing(table: dict[str, Any], required: set[str], prefix: str) -> None:
    missing = sorted(required - table.keys())
    if missing:
        raise ConfigError(f"{prefix}: missing required key(s): {', '.join(missing)}")


def _require_table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{key}: must be a table")
    return value


def _parse_keepalive(table: dict[str, Any]) -> KeepAliveSettings:
    _check_unknown(table, _KEEPALIVE_KEYS, "keepalive")
    _check_missing(table, _KEEPALIVE_KEYS, "keepalive")
    mode = table["mode"]
    if mode not in _VALID_MODES:
        raise ConfigError(f"keepalive.mode: must be one of {sorted(_VALID_MODES)}, got {mode!r}")
    return KeepAliveSettings(
        mode=mode,
        poll_interval_s=int(table["poll_interval_s"]),
        state_dir=Path(str(table["state_dir"])).expanduser(),
        failure_threshold=int(table["failure_threshold"]),
        unknown_threshold=int(table["unknown_threshold"]),
        audit_max_bytes=int(table["audit_max_bytes"]),
    )


def _parse_notifications(table: dict[str, Any]) -> NotificationSettings:
    _check_unknown(table, _NOTIFICATION_KEYS, "notifications")
    _check_missing(table, _NOTIFICATION_KEYS, "notifications")
    notify_on = table["notify_on"]
    if not isinstance(notify_on, list) or not all(isinstance(e, str) for e in notify_on):
        raise ConfigError("notifications.notify_on: must be a list of strings")
    unknown_events = sorted(set(notify_on) - _VALID_NOTIFY_EVENTS)
    if unknown_events:
        raise ConfigError(f"notifications.notify_on: unknown event(s): {', '.join(unknown_events)}")
    return NotificationSettings(
        macos=bool(table["macos"]),
        webhook=bool(table["webhook"]),
        webhook_url_env=str(table["webhook_url_env"]),
        summary_daily_at=str(table["summary_daily_at"]),
        notify_on=list(notify_on),
    )


def _parse_power(table: dict[str, Any]) -> PowerSettings:
    _check_unknown(table, _POWER_KEYS, "power")
    _check_missing(table, _POWER_KEYS, "power")
    return PowerSettings(
        observe=bool(table["observe"]),
        wake_windows=bool(table["wake_windows"]),
        allow_on_battery=bool(table["allow_on_battery"]),
        max_wake_minutes=int(table["max_wake_minutes"]),
    )


def _parse_recovery(table: dict[str, Any], prefix: str) -> RecoveryPolicy:
    _check_unknown(table, _RECOVERY_KEYS, prefix)
    kwargs: dict[str, Any] = {}
    if "enabled" in table:
        kwargs["enabled"] = bool(table["enabled"])
    if "allowed_actions" in table:
        actions = table["allowed_actions"]
        if not isinstance(actions, list) or not all(isinstance(a, str) for a in actions):
            raise ConfigError(f"{prefix}.allowed_actions: must be a list of strings")
        kwargs["allowed_actions"] = list(actions)
    for key in ("cooldown_s", "max_attempts", "backoff_base_s", "backoff_max_s", "circuit_open_s"):
        if key in table:
            kwargs[key] = int(table[key])
    if "failure_threshold" in table and table["failure_threshold"] is not None:
        kwargs["failure_threshold"] = int(table["failure_threshold"])
    return RecoveryPolicy(**kwargs)


def _parse_services(raw: Any) -> list[ServiceConfig]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError("services: must be a list of tables")
    known_kinds = known_adapter_kinds()
    seen_names: set[str] = set()
    services: list[ServiceConfig] = []
    for index, entry in enumerate(raw):
        prefix = f"services[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{prefix}: must be a table")
        _check_unknown(entry, _SERVICE_KEYS, prefix)
        _check_missing(entry, _SERVICE_REQUIRED_KEYS, prefix)

        name = str(entry["name"])
        if name in seen_names:
            raise ConfigError(f"{prefix}.name: duplicate service name {name!r}")
        seen_names.add(name)

        adapter = str(entry["adapter"])
        if adapter not in known_kinds:
            raise ConfigError(f"{prefix}.adapter: unknown adapter kind {adapter!r}")

        params = entry.get("params", {})
        if not isinstance(params, dict):
            raise ConfigError(f"{prefix}.params: must be a table")

        recovery_table = entry.get("recovery", {})
        if not isinstance(recovery_table, dict):
            raise ConfigError(f"{prefix}.recovery: must be a table")
        recovery = _parse_recovery(recovery_table, f"{prefix}.recovery")

        services.append(
            ServiceConfig(
                name=name,
                adapter=adapter,
                enabled=bool(entry["enabled"]),
                params=dict(params),
                recovery=recovery,
            )
        )
    return services


def load_config(path: Path | None = None) -> Config:
    cfg_path = path or default_config_path()
    try:
        raw_text = cfg_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {cfg_path}: {exc}") from exc

    try:
        data = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {cfg_path}: {exc}") from exc

    _check_unknown(data, _TOP_LEVEL_KEYS, "")
    _check_missing(data, _TOP_LEVEL_KEYS - {"services"}, "<config>")

    version = data["version"]
    if version != 1:
        raise ConfigError(f"version: expected 1, got {version!r}")

    return Config(
        version=version,
        keepalive=_parse_keepalive(_require_table(data, "keepalive")),
        notifications=_parse_notifications(_require_table(data, "notifications")),
        power=_parse_power(_require_table(data, "power")),
        services=_parse_services(data.get("services", [])),
    )


__all__ = [
    "Config",
    "ConfigError",
    "KeepAliveSettings",
    "NotificationSettings",
    "PowerSettings",
    "RecoveryPolicy",
    "ServiceConfig",
    "default_config_path",
    "load_config",
]
