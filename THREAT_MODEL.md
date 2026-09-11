# KeepAlive — threat model

KeepAlive runs unattended, with the user's own privileges, and is capable of
restarting things and running local commands. This document states what it
protects, what it can touch, what bounds that, and what it explicitly cannot
protect against.

## 1. Assets

- **The user's Mac and its running services/jobs.** KeepAlive's purpose is to keep
  these healthy without silent, unbounded, or surprising intervention.
- **Local state**: `state.json` (per-service status, incident/backoff bookkeeping),
  `audit.jsonl` (append-only history), `keepalive.lock` (single-instance lock),
  `KILL` (kill switch marker) — all under `state_dir`, all local files, no secrets.
- **Configuration** (`~/.config/keepalive/config.toml` by default): what to watch,
  what recovery (if any) is allowed, and the *names* of environment variables that
  hold notification secrets. The file itself never holds a secret value.
- **Notification channel credentials** (a webhook URL): live only in the process
  environment at send time, sourced from whatever the user's own shell/launchd
  environment provides; never read from or written to disk by KeepAlive.

## 2. Trust boundaries

```
                     ┌─────────────────────────────┐
   config.toml  ───▶ │                              │
   (local file,      │        KeepAlive process      │ ── read-only ──▶ pmset, ps/pgrep,
    no secrets)      │  (user privileges, launchd-   │                  launchctl print,
                      │   or user-invoked)            │                  sqlite (?mode=ro),
   env var (by  ───▶ │                              │                  http GET/HEAD,
   name only)         │                              │                  file stat/mtime
                     │                              │
                     │                              │ ── bounded write ──▶ launchctl kickstart
                     │                              │    (only declared, allow-listed actions,
                     │                              │     only after policy approval)
                     │                              │
                     │                              │ ── local only ──▶ osascript notification,
                     └─────────────────────────────┘                  webhook POST (JSON, no
                                                                        secrets in body/logs)
```

- **Config is trusted input from the user**, but is still validated strictly:
  `version` must be `1`, unknown top-level keys, unknown adapter `kind`s, and
  unknown adapter params are hard errors (`ConfigError`), not silently ignored.
- **Adapters are trusted code** (built-in, or installed via the
  `keepalive.adapters` entry-point group) but are **not trusted with policy**.
  `core/policy.py` is the sole authority on whether an action runs; adapters only
  report facts (`active_job`, `instance_present`, `lock_held`, `handoff_until`) and
  the core enforces every precondition, threshold, backoff, cooldown, and circuit
  rule regardless of what an adapter "wants."
- **Data an adapter reads is untrusted for parsing purposes** (a health endpoint's
  JSON body, a database row, a process listing) but is never used to construct a
  shell command or file path outside what the adapter's own fixed logic expects.
- **Everything KeepAlive can *act* on is name-scoped by config**: an adapter can
  only ever affect the specific service instance identified by its own `params`
  (a launchd label, a PID/lock file, a URL) — never an arbitrary target supplied
  by check-time data.

## 3. What each built-in adapter reads

| Adapter | Reads | Read-only? |
|---|---|---|
| `process` | `pgrep -f <pattern>`, optional pid file contents, optional flock probe on `lock_file` | yes |
| `http` | HTTP GET/HEAD to a configured URL, with timeout | yes |
| `launchd` | `launchctl print gui/<uid>/<label>` | yes |
| `disk` | `os.statvfs` on a configured path | yes |
| `job_freshness` | file mtime, or a `SELECT max(...)` against a SQLite DB opened `?mode=ro`, or a fixed read-only command's output, depending on `kind` | yes |
| `power` | `pmset -g batt`, `pmset -g log` | yes |
| `signal_forge` (optional) | Signal Forge's SQLite DB opened `?mode=ro` (worker heartbeat, job-run table), the `/ops` HTTP endpoint, launchd status of its API/worker labels | yes |

`check()` on every adapter is contractually side-effect free (see
`core/adapter.py`); this is enforced by convention and by the fact that the engine
never calls `recover()` except after policy approval of one declared action.

## 4. Subprocess and argv discipline

Every subprocess invocation in KeepAlive:

- Uses **fixed, absolute binary paths**: `/usr/bin/pmset`, `/bin/launchctl`,
  `/usr/bin/osascript`, `/usr/bin/caffeinate`, `/usr/bin/pgrep`, `/bin/ps`. Never a
  bare command name resolved through `$PATH`.
- Is called as an **argument list**, never `shell=True` and never through a shell
  string built by interpolation. Values that come from config (a launchd label, a
  process pattern, a path) are passed as separate argv elements, not concatenated
  into a command string.
- Always has a **timeout**.
- Never receives **untrusted network or database content** as part of a command
  line — data read from an HTTP response or a SQLite row is used only for
  comparison/parsing in Python, never passed to `subprocess`.

## 5. Secrets policy

- Config holds only `webhook_url_env`: the *name* of an environment variable, e.g.
  `KEEPALIVE_WEBHOOK_URL`. The value is read with `os.environ[...]` at the moment
  a webhook notification is sent, and only if `notifications.webhook = true`.
- The audit log (`core/audit.py`) redacts any field whose **key** contains `url`,
  `token`, `secret`, or `password` — the webhook URL is recorded only as the env
  var name that held it, never its value.
- No credential, token, or URL is ever written to `state.json`, `config.toml`, or
  any log file KeepAlive produces.
- KeepAlive stores no credentials of its own kind (no auth to any local service —
  it observes via read-only local mechanisms: local sockets/HTTP, process
  inspection, file stat, `?mode=ro` SQLite, `pmset`/`launchctl` read subcommands).

## 6. Recovery-action blast radius and its bounds

An action can only be taken when **all** of the following hold simultaneously:

1. `mode = "recover"` globally **and** `services.recovery.enabled = true` for that
   service.
2. The action name appears in that service's `allowed_actions` — actions not
   listed are never selected, regardless of adapter capability.
3. `failure_threshold` consecutive `FAILED`/`DEGRADED` checks have already been
   observed (a single bad check never triggers anything).
4. The action's own `ActionSpec` preconditions hold:
   - `allowed_during_active_job` — if false (the default), an action is refused
     while the adapter reports `active_job = True`.
   - `starts_instance` — if true, the action is refused whenever
     `instance_present`, `lock_held`, or an active `handoff_until` is reported;
     KeepAlive **never starts a second instance of anything**.
5. The service isn't in cooldown (`cooldown_until`) or waiting for
   `next_retry_at` (exponential backoff between attempts).
6. The circuit for that service isn't open — after `max_attempts` failed
   recoveries the circuit opens for `circuit_open_s` and KeepAlive stops acting
   and instead sends an "intervention needed" notification; only a single
   half-open probe is allowed once the circuit period elapses.
7. The global kill switch (`<state_dir>/KILL`) is absent. If present, **every**
   action is suppressed immediately regardless of the above — checks and audit
   logging continue, nothing else does.
8. `ActionSpec.destructive` actions are never auto-selected by policy at all —
   only an operator explicitly listing a non-destructive action in
   `allowed_actions` enables automated use; the built-in adapters ship with
   `destructive = False` actions only (e.g. `kickstart`, not `rm`/`kill -9`
   against arbitrary targets).

The net effect: the worst a misconfigured or compromised adapter can do through
the recovery path is invoke one of its own explicitly-declared, explicitly
allow-listed, non-destructive actions (e.g. `launchctl kickstart` of one named
label), at most `max_attempts` times, with backoff and cooldown between attempts,
and only outside an active job and only when no live instance/lock already exists
— then it is cut off by the circuit breaker until a human looks at it.

## 7. What KeepAlive cannot protect against

- **A powered-off or unplugged machine.** No local software runs when the machine
  has no power. KeepAlive can only observe and report *after* the fact (via
  `pmset -g log` on the next successful cycle) that a sleep/shutdown happened.
- **A closed lid without clamshell mode.** A MacBook with its lid closed sleeps
  unless an external display and input device are attached; KeepAlive does not
  and will not change this behavior — it only reports on-battery/asleep state.
- **The user's own power policy.** KeepAlive never runs `pmset -a ...` or touches
  System Settings / security settings; if the user's power policy sleeps or shuts
  down the machine, that policy wins.
- **launchd itself being unavailable or misconfigured** (e.g. the KeepAlive agent
  plist is not loaded, or `launchd` itself is not running) — KeepAlive relies on
  launchd to keep *itself* alive; nothing in-process can restart KeepAlive if
  launchd never starts it.
- **Application-level bugs inside a service's own code path that don't manifest
  as any configured check failing** — KeepAlive only knows what its adapters
  observe.
- **A compromised local user account.** KeepAlive runs with the user's own
  privileges and assumes the local machine and account are already trusted; it is
  not a privilege boundary or a sandbox.
- **Network partition to a remote notification endpoint.** If the webhook host is
  unreachable, the notification silently fails to send (never raises, never
  retries indefinitely) — the audit log and macOS notification are the fallback.
