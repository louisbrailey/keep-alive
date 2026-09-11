"""KeepAlive command-line interface.

Every command loads configuration fresh (config is re-read per invocation, same
as the engine re-reads it per cycle) and talks to the core only through the
public APIs in ``keepalive.core.*`` — this module holds no policy of its own.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from keepalive.core.audit import AuditLog
from keepalive.core.config import (
    Config,
    ConfigError,
    default_config_path,
    load_config,
)
from keepalive.core.engine import (
    CycleReport,
    kill_switch_path,
    run_cycle,
    run_loop,
    set_kill_switch,
)
from keepalive.core.notify import Notifier
from keepalive.core.power import PowerObserver
from keepalive.core.registry import list_adapters
from keepalive.core.state import Incident, ServiceState, StateStore

app = typer.Typer(
    name="keepalive",
    help="Local macOS reliability service: observe, decide, and (only when explicitly "
    "enabled) recover.",
    no_args_is_help=True,
    add_completion=False,
)
config_app = typer.Typer(
    name="config", help="Inspect and validate configuration.", no_args_is_help=True
)
launchd_app = typer.Typer(
    name="launchd", help="Render or (de)install the launchd agent.", no_args_is_help=True
)
kill_app = typer.Typer(
    name="kill",
    help="Control the kill switch (suppresses every recovery action; checks still run).",
    no_args_is_help=True,
)
app.add_typer(config_app)
app.add_typer(launchd_app)
app.add_typer(kill_app)

# A fixed, generous width: the status/adapters tables have many columns
# (ISO timestamps, action names) that Rich would otherwise wrap or truncate
# illegibly against the ~80-column width it falls back to when not attached
# to a real terminal (e.g. under CliRunner, or piped output).
console = Console(width=200)
err_console = Console(stderr=True, width=200)

_REPO_DIR = Path(__file__).resolve().parents[2]
_EXAMPLE_CONFIG_PATH = _REPO_DIR / "config" / "example.toml"
_PLIST_TEMPLATE_PATH = _REPO_DIR / "deploy" / "launchd" / "com.keepalive.agent.plist"
_PLIST_DEST = Path.home() / "Library" / "LaunchAgents" / "com.keepalive.agent.plist"
_LAUNCHD_LABEL = "com.keepalive.agent"
_LAUNCHCTL = "/bin/launchctl"
_SUBPROCESS_TIMEOUT_S = 10

_ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        "-c",
        help="Path to config.toml (default: $KEEPALIVE_CONFIG or ~/.config/keepalive/config.toml).",
    ),
]


def _load_config(config: Path | None) -> Config:
    path = config or default_config_path()
    try:
        return load_config(path)
    except ConfigError as exc:
        err_console.print(f"[red]config error[/red] ({path}): {exc}")
        raise typer.Exit(code=1) from exc


def _build_runtime(cfg: Config) -> tuple[StateStore, AuditLog, Notifier, PowerObserver | None]:
    state_store = StateStore(cfg.keepalive.state_dir / "state.json")
    audit = AuditLog(cfg.keepalive.state_dir / "audit.jsonl", cfg.keepalive.audit_max_bytes)
    notifier = Notifier(cfg.notifications, audit)
    power = PowerObserver(cfg.power, cfg.keepalive.state_dir) if cfg.power.observe else None
    return state_store, audit, notifier, power


# --------------------------------------------------------------------------- #
# check / run
# --------------------------------------------------------------------------- #


@app.command()
def check(
    config: _ConfigOption = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Run the cycle but never execute a recovery action.")
    ] = False,
) -> None:
    """Run a single observe -> decide -> (maybe recover) cycle and print a summary."""
    cfg = _load_config(config)
    state_store, audit, notifier, power = _build_runtime(cfg)
    report = run_cycle(
        cfg, state_store=state_store, audit=audit, notifier=notifier, power=power, dry_run=dry_run
    )
    _print_cycle_report(report)
    if not report.ran:
        raise typer.Exit(code=1)


def _print_cycle_report(report: CycleReport) -> None:
    if not report.ran:
        console.print(f"[yellow]cycle skipped[/yellow]: {report.skipped_reason}")
        return
    table = Table(title="cycle result")
    table.add_column("service")
    table.add_column("adapter")
    table.add_column("status")
    table.add_column("decision")
    table.add_column("reason")
    for service_report in report.services:
        table.add_row(
            str(service_report["name"]),
            str(service_report["adapter"]),
            str(service_report["status"]),
            str(service_report["decision"]),
            str(service_report["reason"]),
        )
    console.print(table)
    if report.power is not None:
        console.print(_format_power_line(report.power.on_ac, report.power.battery_pct))


@app.command()
def run(
    config: _ConfigOption = None,
    once: Annotated[
        bool, typer.Option("--once", help="Run a single cycle then exit instead of looping.")
    ] = False,
) -> None:
    """Run the poll loop (re-reading config each cycle) until interrupted."""
    run_loop(config, once=once)


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


def _fmt_dt(value: datetime | None) -> str:
    return value.isoformat(timespec="seconds") if value is not None else "-"


def _fmt_power_dict(power: dict[str, Any] | None) -> str:
    if power is None:
        return "power: not observed"
    return _format_power_line(power.get("on_ac"), power.get("battery_pct"))


def _format_power_line(on_ac: bool | None, battery_pct: int | None) -> str:
    ac_part = "unknown" if on_ac is None else ("AC power" if on_ac else "battery")
    pct_part = f", {battery_pct}%" if battery_pct is not None else ""
    return f"power: {ac_part}{pct_part}"


def _last_action_cell(incident: Incident | None) -> str:
    if incident is None or incident.last_action is None:
        return "-"
    result = "ok" if incident.last_action_ok else "failed"
    return f"{incident.last_action} ({result})"


def _service_status(name: str, adapter: str, state: ServiceState | None) -> dict[str, Any]:
    state = state or ServiceState(name=name)
    incident = state.incident
    return {
        "name": name,
        "adapter": adapter,
        "status": state.status,
        "last_check_at": state.last_check_at.isoformat() if state.last_check_at else None,
        "last_healthy_at": state.last_healthy_at.isoformat() if state.last_healthy_at else None,
        "incident_kind": incident.kind if incident else None,
        "incident_since": incident.opened_at.isoformat() if incident else None,
        "attempts": incident.attempts if incident else 0,
        "next_retry_at": (
            incident.next_retry_at.isoformat() if incident and incident.next_retry_at else None
        ),
        "cooldown_until": (
            incident.cooldown_until.isoformat() if incident and incident.cooldown_until else None
        ),
        "circuit_open_until": (
            incident.circuit_open_until.isoformat()
            if incident and incident.circuit_open_until
            else None
        ),
        "last_action": incident.last_action if incident else None,
        "last_action_ok": incident.last_action_ok if incident else None,
        "last_action_detail": incident.last_action_detail if incident else "",
    }


@app.command()
def status(
    config: _ConfigOption = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON instead of a table.")] = False,
) -> None:
    """Show the last known status of every configured service, plus power and the
    kill switch. Reads persisted state; runs no checks itself."""
    cfg = _load_config(config)
    state_store = StateStore(cfg.keepalive.state_dir / "state.json")
    states = state_store.load()
    kill_on = kill_switch_path(cfg.keepalive).exists()
    last_power = state_store.meta.get("last_power")
    last_cycle_at = state_store.meta.get("last_cycle_at")

    if as_json:
        payload = {
            "mode": cfg.keepalive.mode,
            "kill_switch": kill_on,
            "last_cycle_at": last_cycle_at,
            "power": last_power,
            "services": [
                _service_status(service.name, service.adapter, states.get(service.name))
                for service in cfg.services
            ],
        }
        console.print_json(json.dumps(payload, default=str))
        return

    table = Table(title=f"keepalive status (mode={cfg.keepalive.mode})")
    for column in (
        "service",
        "adapter",
        "state",
        "last check",
        "last healthy",
        "incident since",
        "attempts",
        "next retry",
        "cooldown until",
        "circuit until",
        "last action",
    ):
        table.add_column(column)

    for service in cfg.services:
        state = states.get(service.name)
        incident = state.incident if state is not None else None
        table.add_row(
            service.name,
            service.adapter,
            (state.status if state is not None else "unknown")
            + ("" if service.enabled else " (disabled)"),
            _fmt_dt(state.last_check_at if state is not None else None),
            _fmt_dt(state.last_healthy_at if state is not None else None),
            _fmt_dt(incident.opened_at) if incident else "-",
            str(incident.attempts) if incident else "-",
            _fmt_dt(incident.next_retry_at) if incident else "-",
            _fmt_dt(incident.cooldown_until) if incident else "-",
            _fmt_dt(incident.circuit_open_until) if incident else "-",
            _last_action_cell(incident),
        )
    console.print(table)
    console.print(_fmt_power_dict(last_power))
    console.print(f"last cycle: {last_cycle_at or 'never'}")
    console.print(f"kill switch: {'[red]ON[/red]' if kill_on else 'off'}")


# --------------------------------------------------------------------------- #
# summary
# --------------------------------------------------------------------------- #


def _build_summary_message(cfg: Config, states: dict[str, ServiceState]) -> tuple[str, str]:
    now = datetime.now()
    enabled = [service for service in cfg.services if service.enabled]
    status_counts: dict[str, int] = {}
    incidents_open = 0
    circuits_open = 0
    for service in enabled:
        state = states.get(service.name)
        status_name = state.status if state is not None else "unknown"
        status_counts[status_name] = status_counts.get(status_name, 0) + 1
        incident = state.incident if state is not None else None
        if incident is not None:
            incidents_open += 1
            if incident.circuit_open_until is not None and incident.circuit_open_until > now:
                circuits_open += 1

    parts = [f"{count} {name}" for name, count in sorted(status_counts.items())]
    message = f"{len(enabled)}/{len(cfg.services)} services enabled"
    if parts:
        message += " — " + ", ".join(parts)
    if incidents_open:
        message += f"; {incidents_open} incident(s) open"
    if circuits_open:
        message += f"; {circuits_open} circuit(s) open (needs intervention)"
    return "KeepAlive summary", message


def _send_summary_notification(notifier: Notifier, title: str, message: str) -> bool:
    """The `summary` command is an explicit, user-requested notification — unlike
    the policy-driven events in `notify_on` (incident_opened/recovered/circuit_open/
    check_unavailable), it always attempts delivery on whatever channels are
    enabled rather than being gated by that list."""
    settings = notifier.settings
    sent_macos = notifier._send_macos(title, message) if settings.macos else False
    sent_webhook = notifier._send_webhook("summary", title, message) if settings.webhook else False
    notifier.audit.record(
        "notify",
        notify_event="summary",
        title=title,
        macos_enabled=settings.macos,
        macos_sent=sent_macos,
        webhook_enabled=settings.webhook,
        webhook_sent=sent_webhook,
        webhook_env_var=settings.webhook_url_env,
    )
    return sent_macos or sent_webhook


@app.command()
def summary(config: _ConfigOption = None) -> None:
    """Send an explicit summary notification now, on whatever channels are enabled."""
    cfg = _load_config(config)
    state_store, audit, notifier, _power = _build_runtime(cfg)
    states = state_store.load()
    title, message = _build_summary_message(cfg, states)
    sent = _send_summary_notification(notifier, title, message)
    console.print(f"{title}: {message}")
    if not sent:
        console.print("[yellow]no notification channel is enabled; printed above only[/yellow]")
    audit.record("summary_requested", sent=sent)


# --------------------------------------------------------------------------- #
# audit
# --------------------------------------------------------------------------- #


@app.command()
def audit(
    config: _ConfigOption = None,
    tail: Annotated[
        int, typer.Option("--tail", "-n", help="Number of most recent records to show.")
    ] = 20,
    as_json: Annotated[bool, typer.Option("--json", help="Emit a JSON array.")] = False,
) -> None:
    """Show the most recent audit records."""
    cfg = _load_config(config)
    audit_log = AuditLog(cfg.keepalive.state_dir / "audit.jsonl", cfg.keepalive.audit_max_bytes)
    records = audit_log.tail(tail)
    if as_json:
        console.print_json(json.dumps(records, default=str))
        return
    if not records:
        console.print("no audit records")
        return
    for record in records:
        console.print(json.dumps(record, default=str, sort_keys=True))


# --------------------------------------------------------------------------- #
# adapters
# --------------------------------------------------------------------------- #


@app.command()
def adapters(
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON instead of a table.")] = False,
) -> None:
    """List every resolvable adapter (built-in and entry-point-registered) with
    its checks, data sources, allowed actions, and notification conditions."""
    manifests = list_adapters()
    if as_json:
        payload = [
            {
                "kind": manifest.kind,
                "description": manifest.description,
                "checks": [c.name for c in manifest.checks],
                "reads": [f"{d.kind}:{d.target}" for d in manifest.reads],
                "actions": [
                    {
                        "name": a.name,
                        "destructive": a.destructive,
                        "starts_instance": a.starts_instance,
                        "allowed_during_active_job": a.allowed_during_active_job,
                        "default_cooldown_s": a.default_cooldown_s,
                        "default_max_attempts": a.default_max_attempts,
                    }
                    for a in manifest.actions
                ],
                "notify_on": [n.event for n in manifest.notify_on],
            }
            for manifest in manifests
        ]
        console.print_json(json.dumps(payload))
        return

    table = Table(title="keepalive adapters")
    table.add_column("kind")
    table.add_column("checks")
    table.add_column("reads")
    table.add_column("actions")
    table.add_column("notify on")
    for manifest in manifests:
        table.add_row(
            manifest.kind,
            "\n".join(c.name for c in manifest.checks) or "-",
            "\n".join(f"{d.kind}:{d.target}" for d in manifest.reads) or "-",
            "\n".join(f"{a.name} (cooldown {a.default_cooldown_s}s)" for a in manifest.actions)
            or "-",
            "\n".join(n.event for n in manifest.notify_on) or "-",
        )
    console.print(table)


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


@config_app.command("validate")
def config_validate(config: _ConfigOption = None) -> None:
    """Load and validate a config file, reporting the first error found."""
    path = config or default_config_path()
    try:
        load_config(path)
    except ConfigError as exc:
        err_console.print(f"[red]invalid[/red]: {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]valid[/green]: {path}")


@config_app.command("show-example")
def config_show_example() -> None:
    """Print config/example.toml verbatim (a starting point to copy and edit)."""
    # typer.echo, not console.print: the example contains literal `[section]`
    # headers that rich would otherwise try to parse as markup.
    typer.echo(_EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"), nl=False)


@config_app.command("path")
def config_path() -> None:
    """Print the config path KeepAlive uses by default."""
    typer.echo(str(default_config_path()))


# --------------------------------------------------------------------------- #
# kill
# --------------------------------------------------------------------------- #


@kill_app.command("on")
def kill_on(
    config: _ConfigOption = None,
    reason: Annotated[
        str, typer.Option("--reason", help="Recorded in the kill-switch file.")
    ] = "manual",
) -> None:
    """Enable the kill switch: checks keep running, every action is suppressed."""
    cfg = _load_config(config)
    set_kill_switch(cfg.keepalive, True, reason)
    console.print(f"[red]kill switch ON[/red]: {kill_switch_path(cfg.keepalive)} ({reason})")


@kill_app.command("off")
def kill_off(
    config: _ConfigOption = None,
    reason: Annotated[
        str, typer.Option("--reason", help="Recorded for context; not persisted after removal.")
    ] = "manual",
) -> None:
    """Disable the kill switch."""
    cfg = _load_config(config)
    set_kill_switch(cfg.keepalive, False, reason)
    console.print(f"[green]kill switch off[/green]: {kill_switch_path(cfg.keepalive)}")


# --------------------------------------------------------------------------- #
# launchd
# --------------------------------------------------------------------------- #


def _render_plist(config_path: Path) -> str:
    template = _PLIST_TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        template.replace("__KEEPALIVE_REPO_DIR__", str(_REPO_DIR))
        .replace("__KEEPALIVE_CONFIG_PATH__", str(config_path))
        .replace("__HOME__", str(Path.home()))
    )


@launchd_app.command("render")
def launchd_render(config: _ConfigOption = None) -> None:
    """Print the launchd agent plist, rendered with this repo's real venv path."""
    config_path = config or default_config_path()
    typer.echo(_render_plist(config_path), nl=False)


@launchd_app.command("install")
def launchd_install(
    config: _ConfigOption = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes", help="Actually write the plist and bootstrap the agent (default: print only)."
        ),
    ] = False,
) -> None:
    """Install the launchd agent. Without --yes, only prints what would happen."""
    config_path = config or default_config_path()
    plist_text = _render_plist(config_path)
    log_dir = Path.home() / "Library" / "Logs" / "keepalive"
    bootstrap_cmd = [_LAUNCHCTL, "bootstrap", f"gui/{os.getuid()}", str(_PLIST_DEST)]

    console.print(f"  log dir:    {log_dir}")
    console.print(f"  plist dest: {_PLIST_DEST}")
    console.print("This will write the rendered plist above, then run:")
    console.print(f"  {' '.join(bootstrap_cmd)}")

    if not yes:
        console.print("[yellow]Nothing changed. Re-run with --yes to execute.[/yellow]")
        return

    log_dir.mkdir(parents=True, exist_ok=True)
    _PLIST_DEST.parent.mkdir(parents=True, exist_ok=True)
    _PLIST_DEST.write_text(plist_text, encoding="utf-8")
    result = subprocess.run(
        bootstrap_cmd, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S, check=False
    )
    if result.returncode != 0:
        err_console.print(f"[red]launchctl bootstrap failed[/red]: {result.stderr.strip()}")
        raise typer.Exit(code=1)
    console.print("[green]installed and bootstrapped[/green]")


@launchd_app.command("uninstall")
def launchd_uninstall(
    yes: Annotated[
        bool,
        typer.Option(
            "--yes", help="Actually unload the agent and remove the plist (default: print only)."
        ),
    ] = False,
) -> None:
    """Uninstall the launchd agent. Without --yes, only prints what would happen."""
    bootout_cmd = [_LAUNCHCTL, "bootout", f"gui/{os.getuid()}/{_LAUNCHD_LABEL}"]

    console.print("This will run:")
    console.print(f"  {' '.join(bootout_cmd)}")
    console.print(f"  remove {_PLIST_DEST}")

    if not yes:
        console.print("[yellow]Nothing changed. Re-run with --yes to execute.[/yellow]")
        return

    subprocess.run(
        bootout_cmd, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S, check=False
    )
    _PLIST_DEST.unlink(missing_ok=True)
    console.print("[green]uninstalled[/green]")


__all__ = ["app"]
