# Configuration reference

KeepAlive reads a single TOML file, `version = 1`. Default location:
`~/.config/keepalive/config.toml`, or the path named by the `KEEPALIVE_CONFIG`
environment variable. Use `keepalive config path` to see which one is active,
`keepalive config show-example` to print a starter file, and
`keepalive config validate` to check a file without running anything.

**Validation is strict.** Unknown top-level keys, unknown adapter `kind`s, and
unknown adapter `params` keys are all hard errors (`ConfigError`), reported with
the exact key path (e.g. `services[2].params.timeuot_s`), not silently ignored.
This is deliberate: a typo in a config key should never mean "that setting quietly
does nothing."

## `[keepalive]`

| Key | Type | Meaning |
|---|---|---|
| `mode` | `"observe"` \| `"recover"` | `"observe"` (default): every check and decision runs and is recorded, but no recovery action is ever taken, even if a service enables `recovery.enabled`. `"recover"`: recovery actions can run, but only for services that also set `recovery.enabled = true` — this is the *global* gate, not a per-service override. |
| `poll_interval_s` | int | Seconds between cycles when running via `keepalive run` (not `--once`). |
| `state_dir` | path | Directory for `state.json`, `audit.jsonl`, `keepalive.lock`, and the `KILL` kill-switch file. `~` is expanded. |
| `failure_threshold` | int | Consecutive `FAILED`/`DEGRADED` checks before an incident opens for a service. Can be overridden per-service (see below). |
| `unknown_threshold` | int | Consecutive `UNKNOWN` checks (the check itself couldn't run — e.g. network down, permission denied) before a `check_unavailable` incident opens. `UNKNOWN` is never treated as a service failure and never triggers recovery. |
| `audit_max_bytes` | int | Size at which `audit.jsonl` is rotated (renamed to `audit.jsonl.1`). |

## `[notifications]`

| Key | Type | Meaning |
|---|---|---|
| `macos` | bool | Send local notifications via `/usr/bin/osascript` (`display notification`). Off by default. |
| `webhook` | bool | POST a JSON notification to a URL. Off by default. |
| `webhook_url_env` | string | Name of the environment variable holding the webhook URL. The URL itself is **never** written to config — only read from `os.environ[webhook_url_env]` at send time, and only if `webhook = true` and the variable is actually set (a missing variable just means the webhook silently isn't sent). |
| `summary_daily_at` | string | Local time `"HH:MM"` for one daily summary notification. Empty string = never. |
| `notify_on` | list of string | Which event kinds actually produce a notification: any of `"incident_opened"`, `"recovered"`, `"circuit_open"`, `"check_unavailable"`. Events not listed are still audited, just not pushed as a notification. |

## `[power]`

| Key | Type | Meaning |
|---|---|---|
| `observe` | bool | Read `pmset -g batt` / `pmset -g log` each cycle (read-only; never disable unless you don't want power/sleep context at all). |
| `wake_windows` | bool | Allow KeepAlive to hold a bounded `caffeinate -s -w <keepalive pid>` window when a service/policy calls for one. Off by default. |
| `allow_on_battery` | bool | Wake windows are AC-only unless this is explicitly `true`. |
| `max_wake_minutes` | int | Hard upper bound on any single wake window, regardless of what requested it. |

KeepAlive never runs `pmset -a ...`, never edits System Settings or security
settings, and never changes power policy — these keys only bound a *temporary,
local, revocable* caffeinate hold.

## `[[services]]` (repeatable)

| Key | Type | Meaning |
|---|---|---|
| `name` | string | Unique service name — identifies it in state, audit, and `status`. |
| `adapter` | string | Adapter `kind` (see `keepalive adapters` for the installed set — built-ins plus anything registered via the `keepalive.adapters` entry-point group). |
| `enabled` | bool | If false, the service is skipped entirely (audited once per config load, not every cycle). |
| `[services.params]` | table | Adapter-specific parameters — validated against that adapter's `params_schema`; unknown keys are a config error. |
| `[services.recovery]` | table | See below. Optional; defaults to fully disabled (`enabled = false`, `allowed_actions = []`). |

### `[services.recovery]`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | bool | `false` | Per-service recovery gate. Also requires global `mode = "recover"`. |
| `allowed_actions` | list of string | `[]` | Action names this service may take — must be a subset of what the adapter's `describe().actions` declares. An action not listed here is never selected, no matter what the adapter can technically do. |
| `cooldown_s` | int | `300` | Minimum time after any attempt (success or failure) before another is considered. |
| `max_attempts` | int | `3` | Attempts allowed before the circuit opens for this service's incident. |
| `backoff_base_s` | int | `30` | Base for exponential backoff between attempts: `next_retry_at = now + min(backoff_base_s * 2**(attempts-1), backoff_max_s)`. |
| `backoff_max_s` | int | `900` | Cap on the backoff interval. |
| `circuit_open_s` | int | `3600` | How long the circuit stays open (no further attempts, "intervention needed" notification sent once) after `max_attempts` is reached. After this elapses, one half-open probe attempt is allowed. |
| `failure_threshold` | int \| unset | global value | Per-service override of `[keepalive] failure_threshold`. |

## Per-adapter params and declared actions

Run `keepalive adapters` for the live, authoritative list (checks, data read,
actions with cooldowns/attempt defaults, and notify conditions) for every
installed adapter. Summary of the built-ins:

| Adapter | Key params | Declared actions | Notes |
|---|---|---|---|
| `process` | `pattern` (matched via `pgrep -f`), `pid_file`, `lock_file` | none by default (adapters may declare `signal_term`, but it must be explicitly listed in `allowed_actions` to ever run) | Never starts anything; only observes and can optionally be allowed to terminate. |
| `http` | `url`, `timeout_s`, `expect_status` | none — observe-only by nature | |
| `launchd` | `label`, `expect_running`, `handoff_window_s` | `kickstart` | Refused while `instance_present`/`lock_held`/within `handoff_window_s` of a restart. |
| `disk` | `path`, `warn_free_pct`, `fail_free_pct` | none | |
| `job_freshness` | `kind` (`file_mtime` \| `sqlite_max` \| `command_timestamp`), `target`, `max_age_s`, `expected_windows`, `naive_tz` | none | `expected_windows` (e.g. `"17:30-18:30"`) are local wall-clock time-of-day ranges and suppress staleness alerts inside expected gaps. `naive_tz` (`"utc"`, the default, or `"local"`) says how to interpret a `sqlite_max`/`command_timestamp` timestamp that carries no UTC offset; a `file_mtime` age is always computed from an unambiguous epoch and ignores it. |
| `power` | `warn_on_battery`, `report_sleep` | none | Surfaces AC/battery and sleep/wake as a service, independent of the `[power]` section which drives KeepAlive's own behavior. |
| `signal_forge` (optional) | `repo_dir`, `db_path`, `ops_url`, `worker_label`, `api_label`, `heartbeat_stale_s`, `handoff_window_s` | `restart_worker` | Disabled (`enabled = false`) in the example config; read-only against Signal Forge's DB (`?mode=ro`) and `/ops` endpoint. See `ARCHITECTURE.md` §2.7. |

## Precedence and validation errors

- `KEEPALIVE_CONFIG` env var, if set, always wins over the default path.
- The config is re-read at the start of every cycle in `keepalive run` — edits
  take effect on the next cycle without restarting the process.
- `version` must equal `1`; any other value (including missing) is a
  `ConfigError`.
- Any unknown key at any level (top-level, `[keepalive]`, `[notifications]`,
  `[power]`, a service table, `services.params`, `services.recovery`) is a
  `ConfigError` naming the offending key path — it is never dropped silently.
- An unknown `adapter` kind is a `ConfigError` naming the service and the
  unrecognized kind.
- `keepalive config validate` runs this same validation without starting a
  cycle, and is the recommended pre-flight before `keepalive launchd install`.
