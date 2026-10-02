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
#   keep-tabs-alive.sh reload       restart the launchd job after a script update, without
#                                   the "stopped"/"started" Discord alerts
#   keep-tabs-alive.sh status       show whether the launchd job is loaded and the log tail
#   keep-tabs-alive.sh notify MSG   send MSG to the configured Discord webhook
#
# Notes:
#   * Browsers: Safari, or a Chromium-based browser (Google Chrome, Chromium, Brave, Edge,
#     Vivaldi, Arc, Opera). The AppleScript is compiled against the configured browser's own
#     dictionary — Safari terms do not work against Chrome (errors -1728 / -2710).
#   * A recovery (tab reopened, app relaunched) is reported only after it has been verified.
#     A target that keeps failing is retried RECOVER_ATTEMPTS times, then left alone for
#     RECOVER_BACKOFF seconds. Each distinct problem is logged/alerted once until it changes
#     or clears, so a broken browser cannot flood the log or Discord every cycle.
#   * A closed lid still sleeps the Mac unless an external display + keyboard/mouse are
#     attached (clamshell mode). Leave the lid open, or use clamshell mode.
#   * First run will ask for Automation permission to control the browser. Allow it.

CONF_DIR="$HOME/.keep-alive"
CONFIG="$CONF_DIR/config.json"
STATE="$CONF_DIR/state.json"
QUIET_FLAG="$CONF_DIR/.quiet-start"   # created by `reload`; consumed once at startup
LOG_FILE="$HOME/Library/Logs/keep-tabs-alive.log"
LABEL="com.louisbrailey.keep-tabs-alive"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SCRIPT_PATH="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

RECOVER_ATTEMPTS=3   # consecutive failed recoveries (or checks) before backing off / alerting
RECOVER_BACKOFF=600  # seconds to leave a failing target alone
VERIFY_TRIES=5       # how many times to look for the tab/app after a recovery action...
VERIFY_DELAY=2       # ...and how many seconds apart

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG_FILE"; }

cfg() { jq -r "$1" "$CONFIG" 2>/dev/null; }

on_ac_power() { pmset -g batt | grep -q "AC Power"; }

one_line() { printf '%s' "$*" | tr '\n' ' ' | cut -c1-240; }

# ---------------------------------------------------------------- Discord
notify() {
  local msg="$1" webhook
  webhook=$(cfg '.discord.webhook // ""')
  [ -n "$webhook" ] || return 0
  jq -n --arg c "$(hostname -s): $msg" '{username:"Keep Alive", content:$c}' \
    | curl -s -m 10 -o /dev/null -H 'Content-Type: application/json' -d @- "$webhook" &
}

# Send only when event notifications are on.
notify_event() { [ "$(cfg '.discord.events')" = "true" ] && notify "$1"; return 0; }

# ---------------------------------------------------------------- per-target memory
# bash 3.2 has no associative arrays; small facts live in MEM_<sanitised key> variables.
mem_var() { printf 'MEM_%s' "${1//[^A-Za-z0-9]/_}"; }
mem_get() { local v; v=$(mem_var "$1"); printf '%s' "${!v:-}"; }
mem_set() { printf -v "$(mem_var "$1")" '%s' "$2"; }

# Log a problem once; log it again only if its text changes.
log_problem() {  # key text
  [ "$(mem_get "problem:$1")" = "$2" ] && return 0
  mem_set "problem:$1" "$2"
  log "$2 (identical repeats suppressed)"
}
# Forget a problem; log TEXT only if one was outstanding.
clear_problem() {  # key text
  [ -n "$(mem_get "problem:$1")" ] || return 0
  mem_set "problem:$1" ""
  [ -n "$2" ] && log "$2"
  return 0
}
# Discord alert once per key; nothing more for that key until clear_alert.
alert_once() {  # key message
  [ "$(mem_get "alert:$1")" = 1 ] && return 0
  mem_set "alert:$1" 1
  notify_event "$2"
}
clear_alert() {  # key message-if-an-alert-was-outstanding
  [ "$(mem_get "alert:$1")" = 1 ] || return 0
  mem_set "alert:$1" 0
  [ -n "$2" ] && notify_event "$2"
  return 0
}
mark_healthy() {  # key label — reset retry state; log if we had given up on it
  mem_set "fails:$1" 0; mem_set "retry_after:$1" 0
  clear_problem "recover:$1" "$2 is back"
}

# ---------------------------------------------------------------- browser
browser_family() {
  case "$1" in
    Safari|"Safari Technology Preview") echo safari ;;
    "Google Chrome"*|Chromium|"Brave Browser"*|"Microsoft Edge"*|Vivaldi|Arc|Opera*) echo chromium ;;
    *) echo unknown ;;
  esac
}

browser_supported() {
  local re='^[A-Za-z0-9][A-Za-z0-9 ._-]*$'
  [[ "$BROWSER" =~ $re ]] && [ "$(browser_family "$BROWSER")" != unknown ]
}

# AppleScript for ACTION (inspect|open), compiled against $BROWSER's own dictionary. The
# target URL is passed as a run argument so it never needs quoting inside the script.
browser_script() {
  local first
  case "$1" in
    inspect) cat <<EOF
on run {target}
  with timeout of 30 seconds
    tell application "$BROWSER"
      repeat with w in windows
        try
          repeat with t in tabs of w
            set u to ""
            try
              set u to (URL of t as text)
            end try
            if u starts with target then return "yes"
          end repeat
        end try
      end repeat
    end tell
  end timeout
  return "no"
end run
EOF
    ;;
    open)
      if [ "$(browser_family "$BROWSER")" = safari ]; then
        first='make new document with properties {URL:target}'
      else
        first='make new window
        set URL of active tab of window 1 to target'
      fi
      cat <<EOF
on run {target}
  with timeout of 30 seconds
    tell application "$BROWSER"
      if (count of windows) is 0 then
        $first
      else
        tell window 1 to make new tab with properties {URL:target}
      end if
    end tell
  end timeout
end run
EOF
    ;;
  esac
}

# Print yes|no and return 0; or print why the check failed and return 1.
tab_state() {
  local out rc
  browser_supported || { printf 'unsupported browser "%s"' "$BROWSER"; return 1; }
  out=$(browser_script inspect | osascript - "$1" 2>&1); rc=$?
  if [ "$rc" -eq 0 ] && { [ "$out" = yes ] || [ "$out" = no ]; }; then printf '%s' "$out"; return 0; fi
  one_line "${out:-osascript exit $rc}"; return 1
}

# Return 0 once the browser accepted the request; or print the error and return 1.
open_tab() {
  local out rc
  browser_supported || { printf 'unsupported browser "%s"' "$BROWSER"; return 1; }
  out=$(browser_script open | osascript - "$1" 2>&1); rc=$?
  [ "$rc" -eq 0 ] && return 0
  one_line "${out:-osascript exit $rc}"; return 1
}

# Print yes|no for a running app and return 0; or print the error and return 1.
app_state() {
  local out rc
  out=$(osascript -e "application \"$1\" is running" 2>&1); rc=$?
  case "$rc:$out" in
    0:true)  echo yes; return 0 ;;
    0:false) echo no;  return 0 ;;
  esac
  one_line "${out:-osascript exit $rc}"; return 1
}

launch_app() { open -g -a "$1" 2>&1; }

# ---------------------------------------------------------------- recovery
# Run DO_FN ARG, then poll VERIFY_FN CHECK (prints yes|no; non-zero = could not check) until
# it says yes. Returns 0 only for a verified recovery. After RECOVER_ATTEMPTS failures in a
# row the target is left alone for RECOVER_BACKOFF seconds, so a target that never verifies
# cannot spawn a new tab or launch on every cycle.
recover() {  # key label do_fn verify_fn arg check
  local key="$1" label="$2" do_fn="$3" verify_fn="$4" arg="$5" check="$6"
  local now next fails out err="" verdict i
  now=$(date +%s)
  next=$(mem_get "retry_after:$key")
  [ "$now" -lt "${next:-0}" ] && return 1
  log "recovering $label"
  if out=$("$do_fn" "$arg"); then
    i=0
    while [ "$i" -lt "$VERIFY_TRIES" ]; do
      i=$((i + 1))
      sleep "$VERIFY_DELAY"
      if verdict=$("$verify_fn" "$check"); then
        if [ "$verdict" = yes ]; then
          log "$label recovered (verified)"
          mark_healthy "$key" "$label"
          return 0
        fi
      else
        err="could not verify: $verdict"
        break
      fi
    done
    [ -n "$err" ] || err="still missing $((VERIFY_TRIES * VERIFY_DELAY))s after the recovery action"
  else
    err=$(one_line "$out")
  fi
  fails=$(mem_get "fails:$key"); fails=$(( ${fails:-0} + 1 ))
  mem_set "fails:$key" "$fails"
  if [ "$fails" -ge "$RECOVER_ATTEMPTS" ]; then
    mem_set "fails:$key" 0
    mem_set "retry_after:$key" $((now + RECOVER_BACKOFF))
    log_problem "recover:$key" "giving up on $label for $((RECOVER_BACKOFF / 60)) min after $RECOVER_ATTEMPTS attempts: $err"
    alert_once "recover:$key" "⚠️ Could not recover $label: $err"
  else
    log "$label not recovered (attempt $fails/$RECOVER_ATTEMPTS): $err"
  fi
  return 1
}

# A check that keeps failing (e.g. AppleScript errors) is logged at once, alerted after
# RECOVER_ATTEMPTS consecutive failures, and both are cleared when the check works again.
check_failed() {  # key label error
  local n
  n=$(mem_get "checkfails:$1"); n=$(( ${n:-0} + 1 )); mem_set "checkfails:$1" "$n"
  log_problem "inspect:$1" "cannot check $2: $3"
  [ "$n" -ge "$RECOVER_ATTEMPTS" ] && alert_once "inspect:$1" "⚠️ Cannot check $2: $3"
  return 0
}
check_ok() {  # key label
  mem_set "checkfails:$1" 0
  clear_problem "inspect:$1" "check for $2 works again"
  clear_alert "inspect:$1" "✅ Check for $2 works again"
}

# ---------------------------------------------------------------- checks
SITE_STATUS="[]"
APP_STATUS="[]"

ensure_sites() {
  local url match state open
  SITE_STATUS="[]"
  while IFS=$'\t' read -r url match; do
    [ -n "$url" ] || continue
    [ -n "$match" ] || match="$url"
    if state=$(tab_state "$match"); then
      check_ok "site:$url" "tab $url"
      if [ "$state" = yes ]; then
        open=true
        mark_healthy "site:$url" "tab $url"
        clear_alert "recover:site:$url" "✅ Tab open again: $url"
      elif recover "site:$url" "tab $url" open_tab tab_state "$url" "$match"; then
        open=true
        notify_event "🔁 Reopened tab: $url"
        clear_alert "recover:site:$url" ""
      else
        open=false
      fi
    else
      open=null   # unknown: the browser could not be inspected, so do not touch it
      check_failed "site:$url" "tab $url in $BROWSER" "$state"
    fi
    SITE_STATUS=$(jq -c --arg u "$url" --argjson o "$open" '. + [{url:$u, open:$o}]' <<<"$SITE_STATUS")
  done < <(cfg '.sites[] | select(.enabled) | [.url, (.match // .url)] | @tsv')
}

ensure_apps() {
  local app state running
  APP_STATUS="[]"
  while read -r app; do
    [ -n "$app" ] || continue
    if state=$(app_state "$app"); then
      check_ok "app:$app" "app $app"
      if [ "$state" = yes ]; then
        running=true
        mark_healthy "app:$app" "app $app"
        clear_alert "recover:app:$app" "✅ App running again: $app"
      elif recover "app:$app" "app $app" launch_app app_state "$app" "$app"; then
        running=true
        notify_event "🚀 Relaunched app: $app"
        clear_alert "recover:app:$app" ""
      else
        running=false
      fi
    else
      running=null
      check_failed "app:$app" "app $app" "$state"
    fi
    APP_STATUS=$(jq -c --arg a "$app" --argjson r "$running" '. + [{name:$a, running:$r}]' <<<"$APP_STATUS")
  done < <(cfg '.apps[] | select(.enabled) | .name')
}

# Launch the browser when tabs are watched and it is not running; tabs are checked after.
ensure_browser() {
  local state
  [ "$(cfg '[.sites[] | select(.enabled)] | length')" -gt 0 ] 2>/dev/null || return 0
  if state=$(app_state "$BROWSER"); then
    check_ok "browser" "browser $BROWSER"
    if [ "$state" = yes ]; then
      mark_healthy "browser" "browser $BROWSER"
      clear_alert "recover:browser" "✅ $BROWSER running again"
    elif recover "browser" "browser $BROWSER" launch_app app_state "$BROWSER" "$BROWSER"; then
      notify_event "🌐 Relaunched $BROWSER"
      clear_alert "recover:browser" ""
    fi
  else
    check_failed "browser" "browser $BROWSER" "$state"
  fi
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

# Announce startup, unless `reload` asked for a quiet restart.
announce_start() {
  if [ -e "$QUIET_FLAG" ]; then
    rm -f "$QUIET_FLAG"
    log "starting (quiet restart, no start alert)"
  else
    log "starting"
    notify_event "🟢 Keep Alive started"
  fi
}

run_loop() {
  mkdir -p "$CONF_DIR" "$(dirname "$LOG_FILE")"
  [ -f "$CONFIG" ] || { echo "no config at $CONFIG" >&2; exit 1; }
  STARTED=$(date '+%Y-%m-%d %H:%M:%S')
  announce_start
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
        ensure_browser
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

# Restart the running job to pick up a script change, with no Discord stop/start alerts:
# SIGKILL skips the old process's "stopped" trap, launchd (KeepAlive) starts the script
# again, and the flag makes the new process skip its "started" alert.
reload_agent() {
  local pid
  pid=$(launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | awk '$1 == "pid" {print $3; exit}')
  [ -n "$pid" ] || { echo "launchd job not running; use: $0 install" >&2; return 1; }
  touch "$QUIET_FLAG"
  kill -KILL "$pid" && echo "restarted quietly: $LABEL (old pid $pid)"
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

# When sourced (tests), define everything and run nothing.
[ "${BASH_SOURCE[0]}" = "$0" ] || return 0

case "${1:-run}" in
  run)       run_loop ;;
  install)   install_agent ;;
  uninstall) uninstall_agent ;;
  reload)    reload_agent ;;
  status)    status_agent ;;
  notify)    shift; notify "$*"; wait ;;
  *) echo "usage: $0 [run|install|uninstall|reload|status|notify MSG]"; exit 1 ;;
esac
