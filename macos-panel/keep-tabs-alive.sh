#!/bin/bash
#
# keep-tabs-alive.sh — keep chosen apps running, chosen browser tabs open, and the Mac
# awake while on AC power. Configured live from ~/.keep-alive/config.json (edited by the
# "Keep Alive" control panel); changes apply on the next check cycle, no restart needed.
#
# Usage:
#   keep-tabs-alive.sh              run in the foreground (Ctrl-C to stop)
#   keep-tabs-alive.sh install      run automatically at login via launchd
#   keep-tabs-alive.sh uninstall    remove the launchd job
#   keep-tabs-alive.sh status       show whether the launchd job is loaded and the log tail
#   keep-tabs-alive.sh notify MSG   send MSG to the configured Discord webhook
#
# Notes:
#   * A closed lid still sleeps the Mac unless an external display + keyboard/mouse are
#     attached (clamshell mode). Leave the lid open, or use clamshell mode.
#   * First run will ask for Automation permission to control the browser. Allow it.

CONF_DIR="$HOME/.keep-alive"
CONFIG="$CONF_DIR/config.json"
STATE="$CONF_DIR/state.json"
LOG_FILE="$HOME/Library/Logs/keep-tabs-alive.log"
LABEL="com.louisbrailey.keep-tabs-alive"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SCRIPT_PATH="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG_FILE"; }

cfg() { jq -r "$1" "$CONFIG" 2>/dev/null; }

on_ac_power() { pmset -g batt | grep -q "AC Power"; }

# ---------------------------------------------------------------- Discord
notify() {
  local msg="$1" webhook
  webhook=$(cfg '.discord.webhook // ""')
  [ -n "$webhook" ] || return 0
  jq -n --arg c "$(hostname -s): $msg" '{username:"Keep Alive", content:$c}' \
    | curl -s -m 10 -o /dev/null -H 'Content-Type: application/json' -d @- "$webhook" &
}

# Send only when event notifications are on.
notify_event() { [ "$(cfg '.discord.events')" = "true" ] && notify "$1"; }

# ---------------------------------------------------------------- browser
tab_open() {
  osascript - "$BROWSER" "$1" <<'EOF'
on run {browserName, target}
  using terms from application "Safari"
    tell application browserName
      repeat with w in windows
        repeat with t in tabs of w
          if (URL of t as text) starts with target then return "yes"
        end repeat
      end repeat
    end tell
  end using terms from
  return "no"
end run
EOF
}

open_tab() {
  osascript - "$BROWSER" "$1" <<'EOF'
on run {browserName, target}
  using terms from application "Safari"
    tell application browserName
      if (count of windows) is 0 then
        if browserName is "Safari" then
          make new document with properties {URL:target}
        else
          make new window
          set URL of tab 1 of window 1 to target
        end if
      else
        tell window 1 to make new tab with properties {URL:target}
      end if
    end tell
  end using terms from
end run
EOF
}

app_running() { [ "$(osascript -e "application \"$1\" is running" 2>/dev/null)" = "true" ]; }

# ---------------------------------------------------------------- checks
SITE_STATUS="[]"
APP_STATUS="[]"

ensure_sites() {
  local url match open
  SITE_STATUS="[]"
  while IFS=$'\t' read -r url match; do
    [ -n "$url" ] || continue
    [ -n "$match" ] || match="$url"
    if [ "$(tab_open "$match" 2>>"$LOG_FILE")" = "yes" ]; then
      open=true
    else
      log "reopening $url"
      notify_event "🔁 Reopened tab: $url"
      open_tab "$url" >/dev/null 2>>"$LOG_FILE"
      open=false
    fi
    SITE_STATUS=$(jq -c --arg u "$url" --argjson o "$open" '. + [{url:$u, open:$o}]' <<<"$SITE_STATUS")
  done < <(cfg '.sites[] | select(.enabled) | [.url, (.match // .url)] | @tsv')
}

ensure_apps() {
  local app running
  APP_STATUS="[]"
  while read -r app; do
    [ -n "$app" ] || continue
    if app_running "$app"; then
      running=true
    else
      log "$app not running; launching"
      notify_event "🚀 Relaunched app: $app"
      open -g -a "$app" 2>>"$LOG_FILE"
      running=false
    fi
    APP_STATUS=$(jq -c --arg a "$app" --argjson r "$running" '. + [{name:$a, running:$r}]' <<<"$APP_STATUS")
  done < <(cfg '.apps[] | select(.enabled) | .name')
}

# App Nap throttles apps that are not visible; turn it off for the watched apps so they keep
# working in the background. Takes effect the next time each app launches.
disable_app_nap() {
  local app bid
  while read -r app; do
    [ -n "$app" ] || continue
    bid=$(osascript -e "id of app \"$app\"" 2>/dev/null) || continue
    if [ "$(defaults read "$bid" NSAppSleepDisabled 2>/dev/null)" != "1" ]; then
      defaults write "$bid" NSAppSleepDisabled -bool YES && log "disabled App Nap for $app ($bid)"
    fi
  done < <(cfg '.apps[] | .name')
}

write_state() {
  local power=$1 enabled=$2 awake=$3
  jq -n --arg t "$(date '+%Y-%m-%d %H:%M:%S')" --arg p "$power" --argjson e "$enabled" \
        --argjson w "$awake" --argjson a "$APP_STATUS" --argjson s "$SITE_STATUS" \
        --arg st "$STARTED" \
        '{last_check:$t, power:$p, enabled:$e, awake:$w, apps:$a, sites:$s, started:$st}' \
    >"$STATE.tmp" && mv "$STATE.tmp" "$STATE"
}

# ---------------------------------------------------------------- main loop
CAFF_PID=""
hold_awake()    { [ -n "$CAFF_PID" ] && kill -0 "$CAFF_PID" 2>/dev/null && return; caffeinate -s -w $$ & CAFF_PID=$!; }
release_awake() { [ -n "$CAFF_PID" ] && kill "$CAFF_PID" 2>/dev/null; CAFF_PID=""; }

run_loop() {
  mkdir -p "$CONF_DIR" "$(dirname "$LOG_FILE")"
  [ -f "$CONFIG" ] || { echo "no config at $CONFIG" >&2; exit 1; }
  STARTED=$(date '+%Y-%m-%d %H:%M:%S')
  log "starting"
  notify_event "🟢 Keep Alive started"
  trap 'log "stopping"; notify_event "🔴 Keep Alive stopped"; release_awake; sleep 1; exit 0' INT TERM

  local last_power="" last_enabled="" last_heartbeat power enabled interval hb
  last_heartbeat=$(date +%s)

  while true; do
    enabled=$(cfg '.enabled')
    BROWSER=$(cfg '.browser // "Safari"')
    interval=$(cfg '.check_interval // 60')
    on_ac_power && power=ac || power=battery

    if [ "$enabled" != "$last_enabled" ]; then
      if [ -n "$last_enabled" ]; then
        log "enabled=$enabled"
        if [ "$enabled" = true ]; then notify_event "▶️ Keep Alive turned ON"; else notify_event "⏸️ Keep Alive turned OFF"; fi
      fi
      last_enabled=$enabled
    fi
    if [ "$power" != "$last_power" ]; then
      if [ -n "$last_power" ]; then
        log "power=$power"
        if [ "$power" = ac ]; then notify_event "🔌 Plugged in: watching resumed"; else notify_event "🔋 On battery: watching paused, Mac may sleep"; fi
      fi
      last_power=$power
    fi

    if [ "$enabled" = "true" ]; then
      hold_awake
      disable_app_nap
      if [ "$power" = "ac" ]; then
        if ! app_running "$BROWSER" && [ "$(cfg '[.sites[] | select(.enabled)] | length')" -gt 0 ]; then
          log "$BROWSER not running; launching"
          notify_event "🌐 Relaunched $BROWSER"
          open -g -a "$BROWSER"
          sleep 5
        fi
        ensure_sites
        ensure_apps
      fi
    else
      release_awake
      APP_STATUS="[]"; SITE_STATUS="[]"
    fi

    hb=$(cfg '.discord.heartbeat_hours // 0')
    if [ "$hb" -gt 0 ] 2>/dev/null && [ $(( $(date +%s) - last_heartbeat )) -ge $(( hb * 3600 )) ]; then
      notify "💓 Heartbeat: enabled=$enabled power=$power apps=$(jq -r '[.[]|select(.running)]|length' <<<"$APP_STATUS")/$(jq length <<<"$APP_STATUS") running, tabs=$(jq -r '[.[]|select(.open)]|length' <<<"$SITE_STATUS")/$(jq length <<<"$SITE_STATUS") open"
      last_heartbeat=$(date +%s)
    fi

    write_state "$power" "$enabled" "$([ -n "$CAFF_PID" ] && echo true || echo false)"
    sleep "$interval"
  done
}

install_agent() {
  mkdir -p "$HOME/Library/LaunchAgents" "$(dirname "$LOG_FILE")"
  chmod +x "$SCRIPT_PATH"
  cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$SCRIPT_PATH</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG_FILE</string>
  <key>StandardErrorPath</key><string>$LOG_FILE</string>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null
  launchctl bootstrap "gui/$(id -u)" "$PLIST" && echo "installed and started: $LABEL"
}

uninstall_agent() {
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null
  rm -f "$PLIST"
  echo "removed: $LABEL"
}

status_agent() {
  if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    echo "launchd job: loaded"
  else
    echo "launchd job: not loaded"
  fi
  on_ac_power && echo "power: AC" || echo "power: battery"
  [ -f "$STATE" ] && { echo "--- state ---"; jq . "$STATE"; }
  [ -f "$LOG_FILE" ] && { echo "--- last log lines ---"; tail -n 10 "$LOG_FILE"; }
}

case "${1:-run}" in
  run)       run_loop ;;
  install)   install_agent ;;
  uninstall) uninstall_agent ;;
  status)    status_agent ;;
  notify)    shift; notify "$*"; wait ;;
  *) echo "usage: $0 [run|install|uninstall|status|notify MSG]"; exit 1 ;;
esac
