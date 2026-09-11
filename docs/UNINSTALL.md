# Uninstalling KeepAlive

Removing the launchd agent (and, optionally, its state) is intentionally a few
explicit, reviewable steps — nothing here is destructive of your own
configuration or of any service KeepAlive was watching.

## 1. Stop and unload the launchd agent

```bash
launchctl bootout gui/$(id -u)/com.keepalive.agent
```

Or via the CLI, which prints the exact command and runs it only with `--yes`:

```bash
keepalive launchd uninstall           # prints the command
keepalive launchd uninstall --yes     # runs it
```

Verify it's gone:

```bash
launchctl print gui/$(id -u)/com.keepalive.agent
# expect: "Could not find service ... in domain for port"
```

## 2. Remove the plist

```bash
rm ~/Library/LaunchAgents/com.keepalive.agent.plist
```

## 3. Remove state (optional)

State is separate from config and holds only per-service status, incident
bookkeeping, the instance lock, the kill-switch marker, and the audit log —
never secrets, never anything about other applications beyond what you
configured KeepAlive to look at.

```bash
rm -rf "~/Library/Application Support/KeepAlive"
```

Skip this step if you plan to reinstall later and want to keep the audit
history and per-service status.

## 4. Config is left in place

`~/.config/keepalive/config.toml` (or whatever `KEEPALIVE_CONFIG` pointed at)
is **not** touched by any uninstall step above. Remove it yourself if you want
it gone:

```bash
rm ~/.config/keepalive/config.toml
```

## 5. Logs

launchd stdout/stderr logs under `~/Library/Logs/keepalive/` are not removed
by any of the above. Remove them if you want:

```bash
rm -rf ~/Library/Logs/keepalive
```

## 6. What is never touched by uninstall

Uninstalling KeepAlive never stops, restarts, or modifies any service it was
observing — those are independent of KeepAlive's own lifecycle. It also never
touches the unrelated `com.louisbrailey.keep-tabs-alive` launchd agent,
`Keep Alive.app`, `~/bin/keep-tabs-alive.sh`, or `~/.keep-alive/` — see the
coexistence note in `README.md`.
