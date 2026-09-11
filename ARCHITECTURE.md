# KeepAlive — architecture

KeepAlive is a general-purpose, local-only macOS reliability service. It observes
local services and workflows, detects actionable failures, records what happened,
applies only explicitly allowed recovery actions, and notifies the user when
intervention is needed.

It is an **observer and bounded recovery coordinator, not a scheduler**. It never
duplicates or competes with launchd, cron, application job queues, or application
locks, and it never starts a second instance of anything that already has a healthy
instance or a held lock.

## 1. Principles

1. **Observe by default.** `mode = "observe"` runs every check and records every
   decision but performs no recovery. Recovery must be enabled per service
   (`recovery.enabled = true`) *and* globally (`mode = "recover"`).
2. **Core knows nothing about applications.** Every application-specific fact lives
   in an adapter. The core only understands `CheckResult`, `ActionSpec`, policy, and
   state.
3. **Every action is bounded.** Failure threshold → incident; bounded attempts;
   exponential backoff; cooldown after an attempt; circuit breaker after repeated
   failed recoveries; global kill switch; per-service enable.
4. **Expected transitions are not failures.** An adapter can mark a result as an
   expected transition (planned restart handoff, maintenance window) with an expiry;
   the core treats it as "waiting", never as a failure, until it expires.
5. **Never act during an active job** unless the specific action declares
   `allowed_during_active_job = true`.
6. **Never start a second instance.** A `start`/`restart` action is refused when the
   adapter reports `instance_present = true` or `lock_held = true` (a healthy instance
   or an application lock exists), and when a launchd handoff is under way.
7. **Local and secret-free.** Configuration is a human-readable, versioned TOML file
   under the user's home directory. Secrets (webhook URLs, tokens) are never written
   into configuration, source, state, or logs; they are referenced by environment
   variable name and read at send time.
8. **Quiet.** Notifications only for: incident opened (after the failure threshold),
   recovery after failure, circuit open (intervention needed), explicit summaries.
   Healthy cycles produce audit records only.

## 2. Components

```
keepalive/
  core/
    adapter.py    the adapter contract (Adapter, CheckResult, ActionSpec, ActionResult…)
    config.py     TOML loading + validation (versioned; secrets by env-var name only)
    state.py      per-service persisted state (JSON) — status, incident, cooldowns
    policy.py     pure decision engine: given state + result + config → Decision
    engine.py     one cycle: lock → kill switch → checks → policy → actions → audit → notify
    audit.py      append-only JSONL audit log (every check, decision, attempt, result, suppression)
    lock.py       flock-based single-instance lock for KeepAlive itself
    notify.py     macOS notification (osascript) + optional webhook (env-var URL), both off by default
    power.py      AC/battery + sleep/wake observation (pmset, read-only) + bounded caffeinate windows
    registry.py   adapter discovery by name (built-ins + entry points)
  adapters/       process, http, launchd, disk, job_freshness, power
  integrations/signal_forge/   optional example adapter; KeepAlive works without it
  cli.py          keepalive check|run|status|summary|audit|config|kill|launchd
```

### 2.1 The adapter contract (`core/adapter.py`)

An adapter is a class with a stable `kind` and:

- `describe() -> AdapterManifest` — declares its **checks**, the **data it reads**
  (paths, sockets, commands, tables — for the threat model and the status view), the
  **allowed actions** with per-action safety flags and default cooldowns, and its
  **notification conditions**.
- `check(ctx) -> CheckResult` — read-only observation. Must be side-effect free.
- `recover(action, ctx) -> ActionResult` — performs exactly one declared action. Only
  called by the engine after policy approval.

`CheckResult.status` is one of `OK`, `DEGRADED`, `FAILED`, `UNKNOWN`,
`EXPECTED_TRANSITION`. `UNKNOWN` (e.g. a check could not run: network down, permission
denied) is **not** a failure of the service and never triggers recovery; it opens an
incident of kind `check_unavailable` only after its own threshold, and notifies once.

### 2.2 Policy (`core/policy.py`)

Pure function of `(ServiceState, CheckResult, ServicePolicy, now)`:

| Situation | Decision |
|---|---|
| kill switch on | `SUPPRESSED(kill_switch)` for every action; checks still run and are audited |
| service disabled | skipped entirely, audited once per config load |
| `OK` | close open incident (notify "recovered" only if an incident had been *opened*), reset counters |
| `EXPECTED_TRANSITION` with future `until` | `WAIT`, no failure counted |
| `FAILED`/`DEGRADED` below `failure_threshold` | count, no action |
| threshold reached, mode observe or recovery disabled | open incident, notify once, `SUPPRESSED(observe_only)` |
| threshold reached, recovery enabled | choose the first allowed action whose preconditions hold |
| action precondition fails (active job, instance present, lock held, handoff) | `SUPPRESSED(<reason>)`, re-evaluate next cycle, never counted as an attempt |
| within cooldown / before `next_retry_at` | `WAIT` |
| attempts ≥ `max_attempts` | open circuit for `circuit_open_s`, notify "intervention needed", no further attempts until it closes |
| circuit half-open (after `circuit_open_s`) | one probe attempt allowed |

Backoff: `next_retry_at = now + min(backoff_base_s * 2**(attempts-1), backoff_max_s)`;
cooldown after every attempt regardless of result: `cooldown_until = now + cooldown_s`.

### 2.3 Engine cycle (`core/engine.py`)

1. Acquire the KeepAlive lock (non-blocking). If held → exit 0, audit `overlap_skipped`.
2. Load config (re-read each cycle so edits apply without restart), read kill switch.
3. Observe power (AC/battery, slept since last cycle). A detected sleep marks the
   *previous* interval as `EXPECTED_TRANSITION` for adapters that opt in
   (`treat_wake_as_transition = true`), so a stale heartbeat right after wake is not a
   failure.
4. For each enabled service: `check()` → policy → optional `recover()` → persist state
   → audit record for the check, the decision, and any attempt/suppression.
5. Notify per policy. Release lock.

`keepalive run` loops with `poll_interval_s`; launchd keeps *that* process alive.
KeepAlive itself is the only thing launchd restarts; it never uses launchd to restart
other services except through an adapter's declared `launchd_kickstart` action.

### 2.4 Power (`core/power.py`)

Read-only: `pmset -g batt` (AC vs battery, charge), `pmset -g log` (sleep/wake events
since the last cycle). Optional **wakefulness window**: while a configured condition
holds (e.g. a service reports an active job, or a daily window), KeepAlive holds a
`caffeinate -s -w <own pid>` child, bounded by `max_wake_minutes`, and **only on AC
power** unless `allow_on_battery = true` is set explicitly. KeepAlive never runs
`pmset -a …`, never edits System Settings, security settings, or power policy.

No software can keep a powered-off, unplugged-and-drained, or physically disconnected
computer running. A closed lid on a MacBook sleeps the machine unless an external
display and input device are attached (clamshell) — KeepAlive can only report this.

### 2.5 Audit (`core/audit.py`)

Append-only JSONL at `<state_dir>/audit.jsonl`, one record per check, decision,
attempt, result, suppression, notification, and power event. Rotated by size. Secrets
are never written; webhook URLs are logged as the env-var *name*.

### 2.6 Configuration (`config/example.toml`)

TOML, `version = 1`, one `[[services]]` table per observed thing. Unknown keys and
unknown adapter kinds are validation errors. Secrets only as `*_env` names.

### 2.7 Signal Forge integration (`integrations/signal_forge/`)

Optional example adapter. Reads, read-only: the worker heartbeat and job-run tables
of the Signal Forge SQLite database (`?mode=ro`), the `/ops` page's health state over
HTTP, daily-bar freshness, and launchd status of the API/worker agents. Its single
allowed action is `restart_worker` (launchd kickstart), permitted only when: heartbeat
stale beyond the configured window **and** no job is `running` **and** the worker
process is not younger than the handoff window (~90 s after a planned restart is
expected and is reported as `EXPECTED_TRANSITION`) **and** the worker lock is not
held by a live process. It never touches settings, alerts, Discord, probation,
auto-simulation, strategies, trades, provider keys, or the kill switch, and has no
brokerage capability of any kind.

## 3. What KeepAlive is not

- Not a scheduler, job queue, or supervisor of arbitrary child processes.
- Not a replacement for launchd `KeepAlive`; it complements it by observing what
  launchd cannot see (stale heartbeats, frozen loops, stale data).
- Not a secrets manager.
- Not able to prevent sleep on battery by default, override user power policy, or
  keep a machine running that has no power.
