# Keep Alive

Keeps chosen apps running, chosen Safari or Chromium-browser tabs open, and the Mac awake while on AC power.
Posts alerts to Discord (#keep-alive-alerts).

- `keep-tabs-alive.sh` — the watcher daemon. Live copy runs from `~/bin/keep-tabs-alive.sh`
  via launchd (`com.louisbrailey.keep-tabs-alive`). After editing, copy to `~/bin` and run
  `~/bin/keep-tabs-alive.sh reload` for an already-running watcher. Use `install` only for initial setup.
- `panel.py` / `panel.html` — local control panel on http://127.0.0.1:47391
- `launch-panel.sh` — starts the panel and opens it; called by `~/Desktop/Keep Alive/Keep Alive.app`
- `icon/` — SVG source, `KeepAlive.icns`, and `set-icon.js` (applies icons via NSWorkspace)
- `config.json` (ignored, holds the Discord webhook) and `state.json` (ignored, written by the daemon)
- Log: `~/Library/Logs/keep-tabs-alive.log`

## Verified browser recovery

Browser scripts use the configured browser’s own AppleScript dictionary. Failed
inspection is reported as unknown and never triggers a tab-opening attempt. A
reopened-tab notification requires a successful follow-up inspection. Failed
recoveries back off for ten minutes after three attempts, and repeated failure
notifications are suppressed until recovery.

Run the offline regression suite (temporary HOME, stubbed browser and Discord):

```bash
bash macos-panel/tests/keep-tabs-alive-test.sh
```

The installed script and canonical source are separate files; copying source does
not reload an existing process. `reload` suppresses stop/start notifications, but
normal configured event notifications resume afterward. Never commit local
configuration, webhook values, or state.
