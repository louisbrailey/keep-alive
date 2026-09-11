# Deployment — running KeepAlive under launchd

KeepAlive itself is kept alive by launchd; it never uses launchd to restart
anything else except through an adapter's own declared action (e.g.
`launchctl kickstart` on one named label, and only after policy approval — see
`ARCHITECTURE.md` §2.3 and `THREAT_MODEL.md` §6).

**Installation is never performed automatically by KeepAlive.** `keepalive
launchd install` only *prints* the exact commands it would run, and executes
them itself only when you pass `--yes`. Review the rendered plist and the
printed commands before agreeing to either.

## 1. Prerequisites

- `uv sync --python 3.12` has been run in the repo (creates `.venv/`).
- `~/.config/keepalive/config.toml` exists and passes `keepalive config
  validate`.
- You've run `keepalive check --dry-run` at least once and reviewed
  `keepalive status` so you know what it currently sees before it runs
  unattended.

## 2. Render the plist

```bash
cd ~/keepalive
keepalive launchd render
```

This prints the exact plist KeepAlive would install, built from:

- `Label`: `com.keepalive.agent`
- `ProgramArguments`: `["<repo>/.venv/bin/keepalive", "run"]` — the venv's own
  interpreter, no `uv run` indirection, so the agent doesn't depend on `uv`
  being on launchd's `PATH`.
- `RunAtLoad`: `true`, `KeepAlive`: `true` — launchd restarts the KeepAlive
  process itself if it exits; `ThrottleInterval`: `30` seconds between restarts
  so a crash loop doesn't spin.
- `StandardOutPath` / `StandardErrorPath`: under `~/Library/Logs/keepalive/`.
- `EnvironmentVariables`: only `KEEPALIVE_CONFIG` (pointing at your config
  path) — **no secrets**. A webhook URL, if you use one, belongs in your own
  shell/launchd environment under the variable name given by
  `notifications.webhook_url_env` in config, set however you already manage
  per-agent environment (e.g. a separate, git-ignored
  `~/Library/LaunchAgents/environment.plist`-style mechanism, or `launchctl
  setenv` in a login item you control) — KeepAlive's own plist never carries
  it.

Save it to disk yourself once you're satisfied with it:

```bash
keepalive launchd render > ~/Library/LaunchAgents/com.keepalive.agent.plist
```

## 3. Install (review first)

```bash
# Prints the exact commands, does NOT run them:
keepalive launchd install

# Only after reviewing the output above, actually run them:
keepalive launchd install --yes
```

What `--yes` executes is exactly this, nothing more:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.keepalive.agent.plist
```

## 4. Verify

```bash
launchctl print gui/$(id -u)/com.keepalive.agent
```

Look for `state = running` and check the `last exit code`. Then:

```bash
keepalive status
tail -f ~/Library/Logs/keepalive/keepalive.out.log
tail -f ~/Library/Logs/keepalive/keepalive.err.log
```

The audit trail (every check, decision, attempt, suppression, notification) is
separate from these process logs — see `docs/OPERATIONS.md` and `keepalive
audit --tail N`, at `<state_dir>/audit.jsonl` (default
`~/Library/Application Support/KeepAlive/audit.jsonl`).

## 5. Log locations

| What | Where |
|---|---|
| launchd stdout | `~/Library/Logs/keepalive/keepalive.out.log` |
| launchd stderr | `~/Library/Logs/keepalive/keepalive.err.log` |
| Audit log (JSONL, one record per check/decision/attempt/notify) | `<state_dir>/audit.jsonl` (rotated to `.1` by `audit_max_bytes`) |
| Per-service state | `<state_dir>/state.json` |
| Instance lock | `<state_dir>/keepalive.lock` |
| Kill switch marker | `<state_dir>/KILL` (present = every recovery action suppressed) |

## 6. Running without launchd (manual / foreground)

For testing, or if you prefer not to install a launchd agent at all:

```bash
cd ~/keepalive
.venv/bin/keepalive run          # loops with poll_interval_s until interrupted
.venv/bin/keepalive run --once   # single cycle, then exit
```

Nothing about KeepAlive requires launchd — it's how you get "runs continuously
and restarts itself if it crashes" without babysitting a terminal.

## 7. Updating the agent

After changing the plist template (a code change, not a config change — config
edits take effect on the next cycle with no restart needed):

```bash
launchctl bootout gui/$(id -u)/com.keepalive.agent
keepalive launchd render > ~/Library/LaunchAgents/com.keepalive.agent.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.keepalive.agent.plist
```

See `docs/UNINSTALL.md` for full removal.
