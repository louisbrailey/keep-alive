# Operations

## Reading `keepalive status`

`keepalive status` (add `--json` for machine-readable output) prints one row
per configured service plus a power line and a kill-switch line.

| Field | Meaning |
|---|---|
| **service** | The `name` from config. |
| **adapter** | The adapter `kind` handling this service. |
| **state** | Current `ServiceState.status`: `ok`, `degraded`, `failed`, `unknown`, or `expected_transition`. This is the last observed `CheckResult.status`, not a summary judgment — `unknown` means the check itself couldn't run (network down, permission denied), not that the service is unhealthy. |
| **last check** | Timestamp of the most recent `check()` call for this service, whether or not it succeeded. |
| **last healthy** | Timestamp of the most recent `OK` result. If this is far behind "last check," the service has been failing, degraded, or unknown for a while. |
| **incident since** | Empty if no incident is open. Otherwise the time the current incident opened — i.e. the check that pushed consecutive failures past `failure_threshold`. |
| **attempts** | How many recovery attempts have been made against the *current* incident. Reset when the incident closes (service returns to `OK`). |
| **next retry** | If waiting in backoff, when the next attempt is eligible (`next_retry_at`). Empty otherwise. |
| **cooldown until** | Set after *every* attempt (success or failure) — no new attempt is considered before this time even if backoff would otherwise allow one sooner. |
| **circuit until** | If the circuit is open (attempts reached `max_attempts` without resolving), the time it will move to half-open and allow exactly one probe attempt. Empty if the circuit is closed. |
| **last action + result** | The most recent recovery action taken for this service (if any), whether it reported `ok=True`, and its one-line detail. Empty if recovery was never attempted (observe mode, recovery disabled, or no incident has ever reached the action stage). |

The **power line** shows `on_ac` (`True`/`False`/`None` if `pmset` couldn't be
read), battery percentage if known, and whether a sleep was detected since the
last cycle.

The **kill-switch line** shows whether `<state_dir>/KILL` is present. When
present, every service's decisions will show as `suppressed(kill_switch)` in
the audit log even though checks continue to run normally.

## Notification policy

Notifications are sent only for the event kinds listed in
`notifications.notify_on` (default: `incident_opened`, `recovered`,
`circuit_open`, `check_unavailable`), and only through channels you've
enabled (`notifications.macos`, `notifications.webhook`). A healthy cycle for
a service that stays `OK` produces **no notification at all** — only an audit
record. Concretely:

- **`incident_opened`** — sent once, when a service crosses
  `failure_threshold` consecutive `FAILED`/`DEGRADED` checks (or
  `unknown_threshold` consecutive `UNKNOWN` checks, as a `check_unavailable`
  incident). Not sent again for the same open incident.
- **`recovered`** — sent when a service that had an *open* incident returns
  to `OK`. A service that briefly blips below threshold and recovers before
  an incident ever opened produces no notification (nothing to recover
  *from*, from the user's point of view).
- **`circuit_open`** — sent once, when `max_attempts` recovery attempts have
  been exhausted without resolving the incident. This is the "KeepAlive has
  given up automatically, please look" signal.
- **`check_unavailable`** — sent once, when a service's *check itself*
  (not the service) has failed to run `unknown_threshold` times in a row —
  e.g. the machine lost network access to an HTTP-checked endpoint.
- **Daily summary** — if `summary_daily_at` is set (e.g. `"08:00"`), one
  additional notification per day summarizing overall state, independent of
  the per-event notifications above. `keepalive summary` sends this
  on demand.

Every notification attempt (sent, skipped-by-policy, or failed to send) is
recorded in the audit log regardless of channel configuration.

## Kill switch

`keepalive kill on --reason "..."` creates `<state_dir>/KILL` and immediately
suppresses **every** recovery action across **every** service, from whatever
state it's currently in — no restart, no config change needed. Checks and
audit logging continue unaffected, so you keep visibility while acting is
paused. `keepalive kill off --reason "..."` removes the marker. Both the
reason and the on/off transition are recorded in the audit log.

Use this before doing manual maintenance on a watched service, or any time
you want KeepAlive to stop touching things while you investigate.

## Power caveats

KeepAlive's power observation (`pmset -g batt`, `pmset -g log`) and its
optional, bounded `caffeinate -s -w <keepalive pid>` wake windows are the
limit of what it can do about power. Concretely:

- **No software can make a powered-off or disconnected computer run.**
  KeepAlive can only report, on the next cycle after power returns, that a
  gap happened (via `pmset -g log`) — it cannot act while the machine has no
  power.
- **A closed lid without clamshell mode sleeps the machine.** Attaching an
  external display and keyboard/mouse (clamshell mode) is the only thing that
  changes this; KeepAlive does not and will not alter lid-close behavior.
- **The user's own power policy (System Settings) always wins.** KeepAlive
  never runs `pmset -a ...` and never touches System Settings or security
  settings, so a system configured to sleep aggressively on battery will
  still sleep aggressively on battery regardless of `[power]` settings in
  KeepAlive's own config.
- **Wake windows are AC-only unless `allow_on_battery = true`**, and are
  always capped by `max_wake_minutes` regardless of what requested the
  window — KeepAlive will let a wake window expire rather than hold it
  indefinitely.
