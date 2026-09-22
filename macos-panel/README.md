# Keep Alive

Keeps chosen apps running, chosen Safari tabs open, and the Mac awake while on AC power.
Posts alerts to Discord (#keep-alive-alerts).

- `keep-tabs-alive.sh` — the watcher daemon. Live copy runs from `~/bin/keep-tabs-alive.sh`
  via launchd (`com.louisbrailey.keep-tabs-alive`). After editing, copy to `~/bin` and run
  `~/bin/keep-tabs-alive.sh install`.
- `panel.py` / `panel.html` — local control panel on http://127.0.0.1:47391
- `launch-panel.sh` — starts the panel and opens it; called by `~/Desktop/Keep Alive/Keep Alive.app`
- `icon/` — SVG source, `KeepAlive.icns`, and `set-icon.js` (applies icons via NSWorkspace)
- `config.json` (ignored, holds the Discord webhook) and `state.json` (ignored, written by the daemon)
- Log: `~/Library/Logs/keep-tabs-alive.log`
