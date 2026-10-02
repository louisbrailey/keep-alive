#!/bin/bash
#
# Offline tests for keep-tabs-alive.sh. Runs the watcher's functions under /bin/bash 3.2 with
# a throwaway HOME and stubbed osascript / open / curl / hostname, so no browser, app or
# Discord webhook is touched. Also compiles the generated AppleScript against the installed
# Safari and Google Chrome dictionaries (skipped if a browser is absent).
#
#   /bin/bash ~/.keep-alive/tests/keep-tabs-alive-test.sh [path/to/keep-tabs-alive.sh]

SCRIPT=${1:-$(cd "$(dirname "$0")/.." && pwd)/keep-tabs-alive.sh}
[ -f "$SCRIPT" ] || { echo "no script at $SCRIPT" >&2; exit 2; }
SCRIPT=$(cd "$(dirname "$SCRIPT")" && pwd)/$(basename "$SCRIPT")

TMP=$(mktemp -d /tmp/kta-test.XXXXXX)
trap 'rm -rf "$TMP"' EXIT
export HOME="$TMP/home"
export STUB="$TMP/stub"
mkdir -p "$HOME/.keep-alive" "$HOME/Library/Logs" "$STUB/bin"
LOG="$HOME/Library/Logs/keep-tabs-alive.log"

# ------------------------------------------------------------------ stubs
cat >"$STUB/bin/osascript" <<'EOF'
#!/bin/bash
# Records every call; answers from files in $STUB: inspect_result (yes|no|error),
# open_result (ok|error), app_<name> (true|false|error).
if [ "$1" = "-" ]; then
  script=$(cat)
  printf 'osascript - %s\n' "$2" >>"$STUB/calls"
  printf '%s\n' "$script" >"$STUB/last_script"
  case "$script" in
    *"repeat with w in windows"*)
      r=$(cat "$STUB/inspect_result" 2>/dev/null)
      case "$r" in
        yes|no) echo "$r"; exit 0 ;;
        *) echo "125:256: execution error: Google Chrome got an error: Can’t get every tab of item 1 of every window. (-1728)" >&2; exit 1 ;;
      esac ;;
    *"make new tab"*)
      echo open >>"$STUB/opens"
      # a successful open may flip what inspect returns next
      [ -f "$STUB/inspect_after_open" ] && cp "$STUB/inspect_after_open" "$STUB/inspect_result"
      if [ "$(cat "$STUB/open_result" 2>/dev/null)" = ok ]; then echo "tab id 7 of window id 3"; exit 0; fi
      echo "376:417: execution error: Google Chrome got an error: Can’t make class tab. (-2710)" >&2; exit 1 ;;
  esac
  echo "stub: unknown script" >&2; exit 1
fi
if [ "$1" = "-e" ]; then
  printf 'osascript -e %s\n' "$2" >>"$STUB/calls"
  case "$2" in
    *"is running"*)
      app=${2#application \"}; app=${app%%\"*}
      r=$(cat "$STUB/app_$app" 2>/dev/null)
      case "$r" in
        true|false) echo "$r"; exit 0 ;;
        *) echo "execution error: stub cannot see $app (-600)" >&2; exit 1 ;;
      esac ;;
    *"id of app"*) echo "com.example.stub"; exit 0 ;;
  esac
fi
echo "stub: unexpected args $*" >&2; exit 1
EOF
cat >"$STUB/bin/open" <<'EOF'
#!/bin/bash
printf 'open %s\n' "$*" >>"$STUB/calls"
all="$*"; app=${all##*-a }
[ -f "$STUB/launch_makes_running" ] && echo true >"$STUB/app_$app"
exit 0
EOF
cat >"$STUB/bin/curl" <<'EOF'
#!/bin/bash
# The watcher pipes the JSON body via -d @-; record it, never send it.
printf 'curl\n' >>"$STUB/calls"
cat >>"$STUB/notifications"
printf '\n' >>"$STUB/notifications"
exit 0
EOF
printf '#!/bin/bash\necho testhost\n' >"$STUB/bin/hostname"
printf '#!/bin/bash\necho "AC Power"\n' >"$STUB/bin/pmset"
printf '#!/bin/bash\nexit 0\n' >"$STUB/bin/defaults"
chmod +x "$STUB/bin/"*
export PATH="$STUB/bin:$PATH"

cat >"$HOME/.keep-alive/config.json" <<'EOF'
{"enabled": true, "browser": "Google Chrome", "check_interval": 15,
 "sites": [{"url": "http://127.0.0.1:8787/", "match": "http://127.0.0.1:8787", "enabled": true},
           {"url": "https://example.com/off", "enabled": false}],
 "apps": [{"name": "Claude", "enabled": true}],
 "discord": {"webhook": "https://stub.invalid/webhook", "events": true, "heartbeat_hours": 0}}
EOF

# ------------------------------------------------------------------ harness
PASS=0; FAIL=0
ok()   { PASS=$((PASS + 1)); }
fail() { FAIL=$((FAIL + 1)); echo "FAIL: $*"; }
assert_eq() { [ "$2" = "$3" ] && ok || fail "$1: expected [$3] got [$2]"; }
assert_contains() { case "$2" in *"$3"*) ok ;; *) fail "$1: [$3] not found in: $2" ;; esac; }
assert_not_contains() { case "$2" in *"$3"*) fail "$1: [$3] unexpectedly found in: $2" ;; *) ok ;; esac; }
count() { if [ -f "$1" ]; then grep -c "$2" "$1"; else echo 0; fi; return 0; }
notes() { cat "$STUB/notifications" 2>/dev/null; }
# Background curl jobs belong to this shell, so wait here (not inside a $(...) subshell).
run() { "$@"; wait; }
reset_stub() {
  rm -f "$STUB/calls" "$STUB/opens" "$STUB/notifications" "$STUB/inspect_after_open" "$STUB/launch_makes_running" "$LOG"
  echo yes >"$STUB/inspect_result"; echo ok >"$STUB/open_result"; echo true >"$STUB/app_Claude"; echo true >"$STUB/app_Google Chrome"
  # forget per-target memory between scenarios
  for v in $(compgen -v MEM_); do unset "$v"; done
}

# ------------------------------------------------------------------ syntax + source
bash -n "$SCRIPT" && ok || fail "bash -n syntax check"
# shellcheck disable=SC1090
. "$SCRIPT" && ok || fail "sourcing the script must define functions without running the loop"
VERIFY_DELAY=0
BROWSER=$(cfg '.browser // "Safari"')
assert_eq "config browser" "$BROWSER" "Google Chrome"

# ------------------------------------------------------------------ AppleScript generation
s=$(browser_script inspect)
assert_contains "chrome inspect targets Chrome" "$s" 'tell application "Google Chrome"'
assert_not_contains "chrome inspect has no Safari terms" "$s" 'Safari'
assert_not_contains "no using-terms-from block" "$s" 'using terms from'
s=$(browser_script open)
assert_contains "chrome open makes a window" "$s" 'make new window'
assert_contains "chrome open sets active tab URL" "$s" 'set URL of active tab of window 1 to target'
assert_not_contains "chrome open has no Safari document" "$s" 'make new document'
BROWSER=Safari
s=$(browser_script open)
assert_contains "safari open uses document" "$s" 'make new document with properties {URL:target}'
assert_contains "safari open targets Safari" "$s" 'tell application "Safari"'
assert_eq "family Safari" "$(browser_family Safari)" safari
assert_eq "family Brave" "$(browser_family 'Brave Browser')" chromium
assert_eq "family Edge" "$(browser_family 'Microsoft Edge')" chromium
assert_eq "family unknown" "$(browser_family 'Firefox')" unknown
BROWSER='Evil"; do shell script "true'; browser_supported && fail "browser name with quotes must be rejected" || ok
BROWSER="Google Chrome"

# Compile against the real dictionaries (no browser is launched or scripted by osacompile).
for b in "Safari" "Google Chrome"; do
  if [ -d "/Applications/$b.app" ] || [ -d "/System/Applications/$b.app" ] || [ -d "/System/Cryptexes/App/System/Applications/$b.app" ]; then
    for action in inspect open; do
      BROWSER="$b" browser_script "$action" >"$TMP/$action.applescript"
      if /usr/bin/osacompile -o "$TMP/$action.scpt" "$TMP/$action.applescript" 2>"$TMP/compile.err"; then ok
      else fail "osacompile $b $action: $(cat "$TMP/compile.err")"; fi
    done
  else
    echo "skip: $b not installed, compile check skipped"
  fi
done
BROWSER="Google Chrome"

# ------------------------------------------------------------------ scenario: tab present
reset_stub
run ensure_sites
assert_eq "present: state open" "$(jq -c '.[0].open' <<<"$SITE_STATUS")" true
assert_eq "present: no opens" "$(count "$STUB/opens" open)" 0
assert_eq "present: no notifications" "$(notes)" ""
assert_eq "present: disabled site ignored" "$(jq length <<<"$SITE_STATUS")" 1

# ------------------------------------------------------------------ scenario: tab absent, reopen verified
reset_stub
echo no >"$STUB/inspect_result"; echo yes >"$STUB/inspect_after_open"
run ensure_sites
assert_eq "reopen: verified open" "$(jq -c '.[0].open' <<<"$SITE_STATUS")" true
assert_eq "reopen: one open" "$(count "$STUB/opens" open)" 1
n=$(notes)
assert_eq "reopen: exactly one notification" "$(printf '%s\n' "$n" | grep -c 'Reopened tab')" 1
assert_contains "reopen: notification names the url" "$n" 'http://127.0.0.1:8787/'
calls=$(cat "$STUB/calls")
first_open=$(grep -n '^osascript - http' "$STUB/calls" | sed -n 2p | cut -d: -f1)   # 2nd script call is the open
first_curl=$(grep -n '^curl' "$STUB/calls" | head -1 | cut -d: -f1)
[ -n "$first_open" ] && [ -n "$first_curl" ] && [ "$first_curl" -gt "$first_open" ] && ok || fail "reopen: notification must follow the open (open line $first_open, curl line $first_curl)"
assert_contains "reopen: log records verification" "$(cat "$LOG")" 'recovered (verified)'
run ensure_sites; run ensure_sites
assert_eq "reopen: stays quiet afterwards" "$(printf '%s\n' "$(notes)" | grep -c 'Reopened tab')" 1

# ------------------------------------------------------------------ scenario: inspection fails (-1728)
reset_stub
echo error >"$STUB/inspect_result"
run ensure_sites; run ensure_sites
assert_eq "inspect-fail: open is null" "$(jq -c '.[0].open' <<<"$SITE_STATUS")" null
assert_eq "inspect-fail: never opens a tab" "$(count "$STUB/opens" open)" 0
assert_eq "inspect-fail: no alert before threshold" "$(notes)" ""
assert_eq "inspect-fail: logged once" "$(count "$LOG" 'cannot check tab')" 1
assert_contains "inspect-fail: log carries the error" "$(cat "$LOG")" '(-1728)'
run ensure_sites   # third consecutive failure -> one alert
n=$(notes)
assert_eq "inspect-fail: one alert after threshold" "$(printf '%s\n' "$n" | grep -c 'Cannot check')" 1
assert_not_contains "inspect-fail: no false reopen alert" "$n" 'Reopened'
run ensure_sites; run ensure_sites
assert_eq "inspect-fail: alert not repeated" "$(printf '%s\n' "$(notes)" | grep -c 'Cannot check')" 1
assert_eq "inspect-fail: log not repeated" "$(count "$LOG" 'cannot check tab')" 1
echo yes >"$STUB/inspect_result"
run ensure_sites
assert_eq "inspect-fail: cleared -> open true" "$(jq -c '.[0].open' <<<"$SITE_STATUS")" true
assert_eq "inspect-fail: recovery logged" "$(count "$LOG" 'works again')" 1
assert_eq "inspect-fail: recovery alerted once" "$(printf '%s\n' "$(notes)" | grep -c 'works again')" 1

# ------------------------------------------------------------------ scenario: open fails (-2710), bounded retries
reset_stub
echo no >"$STUB/inspect_result"; echo error >"$STUB/open_result"
run ensure_sites; run ensure_sites; run ensure_sites
assert_eq "open-fail: three attempts" "$(count "$STUB/opens" open)" 3
n=$(notes)
assert_not_contains "open-fail: no success alert" "$n" 'Reopened'
assert_eq "open-fail: one failure alert" "$(printf '%s\n' "$n" | grep -c 'Could not recover')" 1
assert_contains "open-fail: alert carries the error" "$n" '(-2710)'
assert_eq "open-fail: giving-up logged once" "$(count "$LOG" 'giving up')" 1
run ensure_sites; run ensure_sites; run ensure_sites; run ensure_sites
assert_eq "open-fail: backoff stops further attempts" "$(count "$STUB/opens" open)" 3
assert_eq "open-fail: alert not repeated" "$(printf '%s\n' "$(notes)" | grep -c 'Could not recover')" 1
assert_eq "open-fail: state open false" "$(jq -c '.[0].open' <<<"$SITE_STATUS")" false
MEM_retry_after_site_http___127_0_0_1_8787_=0   # backoff elapsed
echo ok >"$STUB/open_result"; echo yes >"$STUB/inspect_after_open"
run ensure_sites
assert_eq "open-fail: retries after backoff and verifies" "$(jq -c '.[0].open' <<<"$SITE_STATUS")" true
assert_eq "open-fail: success alert after real recovery" "$(printf '%s\n' "$(notes)" | grep -c 'Reopened tab')" 1
assert_eq "open-fail: back logged" "$(count "$LOG" 'is back')" 1

# ------------------------------------------------------------------ scenario: open "succeeds" but tab never verifies
reset_stub
echo no >"$STUB/inspect_result"; echo ok >"$STUB/open_result"
run ensure_sites
assert_eq "unverified: open false" "$(jq -c '.[0].open' <<<"$SITE_STATUS")" false
assert_eq "unverified: no success alert" "$(notes)" ""
assert_contains "unverified: log says still missing" "$(cat "$LOG")" 'still missing'

# ------------------------------------------------------------------ scenario: unsupported browser
reset_stub
BROWSER=Firefox
run ensure_sites
assert_eq "unsupported: open null" "$(jq -c '.[0].open' <<<"$SITE_STATUS")" null
assert_eq "unsupported: no osascript" "$(count "$STUB/calls" osascript)" 0
assert_contains "unsupported: logged" "$(cat "$LOG")" 'unsupported browser "Firefox"'
BROWSER="Google Chrome"

# ------------------------------------------------------------------ scenario: apps
reset_stub
run ensure_apps
assert_eq "app running: state" "$(jq -c '.[0].running' <<<"$APP_STATUS")" true
assert_eq "app running: quiet" "$(notes)" ""
reset_stub
echo false >"$STUB/app_Claude"; touch "$STUB/launch_makes_running"
run ensure_apps
assert_eq "app relaunch: verified running" "$(jq -c '.[0].running' <<<"$APP_STATUS")" true
assert_eq "app relaunch: launched once" "$(count "$STUB/calls" '^open -g -a Claude')" 1
assert_eq "app relaunch: one alert" "$(printf '%s\n' "$(notes)" | grep -c 'Relaunched app: Claude')" 1
reset_stub
echo false >"$STUB/app_Claude"
run ensure_apps
assert_eq "app launch unverified: running false" "$(jq -c '.[0].running' <<<"$APP_STATUS")" false
assert_eq "app launch unverified: no alert" "$(notes)" ""
reset_stub
echo error >"$STUB/app_Claude"
run ensure_apps
assert_eq "app check fails: running null" "$(jq -c '.[0].running' <<<"$APP_STATUS")" null
assert_eq "app check fails: not launched" "$(count "$STUB/calls" '^open ')" 0

# ------------------------------------------------------------------ scenario: browser relaunch
reset_stub
echo false >"$STUB/app_Google Chrome"; touch "$STUB/launch_makes_running"
run ensure_browser
assert_eq "browser relaunch: one alert after verification" "$(printf '%s\n' "$(notes)" | grep -c 'Relaunched Google Chrome')" 1
reset_stub
echo false >"$STUB/app_Google Chrome"
run ensure_browser
assert_eq "browser relaunch unverified: no alert" "$(notes)" ""

# ------------------------------------------------------------------ quiet restart flag
reset_stub
run announce_start
assert_eq "normal start: alert" "$(printf '%s\n' "$(notes)" | grep -c 'started')" 1
reset_stub
touch "$QUIET_FLAG"
run announce_start
assert_eq "quiet start: no alert" "$(notes)" ""
[ -e "$QUIET_FLAG" ] && fail "quiet start: flag must be consumed" || ok
assert_contains "quiet start: logged" "$(cat "$LOG")" 'quiet restart'

# ------------------------------------------------------------------ notifications never leak the webhook
reset_stub
echo no >"$STUB/inspect_result"; echo yes >"$STUB/inspect_after_open"
run ensure_sites
assert_not_contains "payload has no webhook url" "$(notes)" 'stub.invalid'
assert_contains "payload uses host prefix" "$(notes)" 'testhost:'

echo "passed=$PASS failed=$FAIL"
[ "$FAIL" -eq 0 ]
