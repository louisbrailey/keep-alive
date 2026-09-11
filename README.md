# KeepAlive

A standalone, general-purpose, local-only macOS reliability service. KeepAlive
observes local services and workflows, detects actionable failures, records what
happened, applies only explicitly allowed and tightly bounded recovery actions, and
notifies you when it can't fix something itself.

Full design: [ARCHITECTURE.md](ARCHITECTURE.md). Threat model:
[THREAT_MODEL.md](THREAT_MODEL.md).

## What it is

- A small, local process that periodically checks the things you tell it to check
  (a process, an HTTP health endpoint, a launchd job, disk free space, a file/DB
  freshness signal, AC power) via pluggable **adapters**.
- Observe-only by default. Recovery is opt-in per service, and even then limited to
  a short list of actions the adapter explicitly declares (e.g. `kickstart` a launchd
  job) — never arbitrary commands.
- Bounded: failure thresholds before it does anything, capped retries with backoff,
  a cooldown after every attempt, and a circuit breaker that stops it from hammering
  a broken service and instead asks you to intervene.
- Auditable: every check, decision, attempt, and notification is appended to a local
  JSONL log. Nothing phones home.

## What it is NOT

- Not a scheduler, job queue, or process supervisor for arbitrary child processes —
  it never launches new long-running work, only restarts things you've explicitly
  named and enabled.
- Not a replacement for launchd's own `KeepAlive` — it complements it, watching for
  things launchd can't see (a process that's alive but stuck, stale data, a stale
  heartbeat).
- Not a secrets manager. Webhook URLs and tokens are never stored in config, source,
  state, or logs — only referenced by environment variable *name*, read at send
  time.
- Not tied to any one application. Signal Forge is one optional adapter under
  `integrations/signal_forge/`; the core has no knowledge of it or any other app.
- Not able to keep a powered-off, unplugged, or physically disconnected machine
  running, override your power policy, or force a MacBook to stay awake with the
  lid closed and no external display attached.

## Quick start

```bash
cd ~/keepalive
uv sync --python 3.12

# Write a starter config to the default location
keepalive config show-example > ~/.config/keepalive/config.toml
# Edit ~/.config/keepalive/config.toml: point services at what you actually run

# Validate it
keepalive config validate

# Run one check cycle and see what KeepAlive would do (no recovery in observe mode anyway)
keepalive check --dry-run

# See current state
keepalive status

# Run continuously (foreground; for a real deployment see docs/DEPLOYMENT.md)
keepalive run
```

By default `mode = "observe"` in `[keepalive]`: KeepAlive checks everything and
records every decision it *would* make, but performs no recovery action, ever,
regardless of what individual services declare.

## Enabling recovery for a service

Recovery requires **both**:

1. `[keepalive] mode = "recover"` globally, and
2. `[services.recovery] enabled = true` on that specific service, with
   `allowed_actions` listing only the action name(s) you want it allowed to take
   (see `keepalive adapters` for what each adapter can do).

Example — allow KeepAlive to kickstart a stuck launchd job, nothing else:

```toml
[[services]]
name = "example-launchd-job"
adapter = "launchd"
enabled = true
[services.params]
label = "com.example.worker"
expect_running = true
[services.recovery]
enabled = true
allowed_actions = ["kickstart"]
cooldown_s = 600
max_attempts = 2
```

Even then, an action only runs when its preconditions hold — never during an active
job (unless the action explicitly allows it), never when a healthy instance or lock
is already present, never during a handoff window, and never more than
`max_attempts` times before the circuit opens and KeepAlive just notifies you
instead.

A global kill switch (`keepalive kill on`) suppresses every recovery action
immediately, from any state, while checks keep running and logging.

## Coexistence with your existing "Keep Alive" setup

If you already run **`Keep Alive.app`** / `~/bin/keep-tabs-alive.sh` (launchd label
`com.louisbrailey.keep-tabs-alive`, config under `~/.keep-alive/`) — that is a
**separate, unrelated tool** that keeps specific browser tabs/apps open. This
project, KeepAlive, does not read, write, manage, restart, or otherwise touch that
tool, its launchd job, or its config directory in any way. The two can run side by
side: different launchd labels, different config locations
(`~/.config/keepalive/` vs `~/.keep-alive/`), different state directories.

## Commands

```
keepalive check [--dry-run]      run one cycle now
keepalive run [--once]           run the poll loop (or one cycle and exit)
keepalive status [--json]        current state of every service, power, kill switch
keepalive summary                send an explicit summary notification now
keepalive audit [--tail N]       show recent audit log entries
keepalive adapters               list built-in and installed adapters and what they can do
keepalive config validate|show-example|path
keepalive kill on|off --reason "..."
keepalive launchd render|install|uninstall [--yes]
```

See [docs/OPERATIONS.md](docs/OPERATIONS.md) for what the `status` fields mean,
[docs/CONFIGURATION.md](docs/CONFIGURATION.md) for every config key, and
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) / [docs/UNINSTALL.md](docs/UNINSTALL.md)
for running it under launchd.

## Requirements

Python 3.12, macOS. Dependencies: `typer`, `rich`, stdlib otherwise (`tomllib`,
`sqlite3`, `urllib`, `fcntl`, `subprocess` with fixed binaries and argument lists
only — see the threat model).
