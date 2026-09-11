#!/bin/sh
# KeepAlive launchd install helper.
#
# By default this script only PRINTS the commands it would run and what each
# one does — it makes no changes to your system. Pass --yes to actually
# execute them.
#
# This is a thin, transparent wrapper; `keepalive launchd render` /
# `keepalive launchd install --yes` (the CLI) is the primary, authoritative
# path and does the same thing. This script exists for reviewers who want to
# see the exact shell commands without invoking the CLI.
#
# It does NOT run `uv sync`, does NOT install dependencies, and does NOT
# create ~/.config/keepalive/config.toml for you — run
# `keepalive config show-example > ~/.config/keepalive/config.toml` and edit
# it first (see README.md).

set -eu

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_SRC="$REPO_DIR/deploy/launchd/com.keepalive.agent.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.keepalive.agent.plist"
CONFIG_PATH="${KEEPALIVE_CONFIG:-$HOME/.config/keepalive/config.toml}"
LOG_DIR="$HOME/Library/Logs/keepalive"
UID_NUM="$(id -u)"

DO_IT=0
if [ "${1:-}" = "--yes" ]; then
    DO_IT=1
fi

echo "KeepAlive launchd install"
echo "  repo dir:    $REPO_DIR"
echo "  config path: $CONFIG_PATH"
echo "  plist dest:  $PLIST_DST"
echo "  log dir:     $LOG_DIR"
echo

if [ ! -x "$REPO_DIR/.venv/bin/keepalive" ]; then
    echo "ERROR: $REPO_DIR/.venv/bin/keepalive not found." >&2
    echo "Run 'uv sync --python 3.12' in $REPO_DIR first." >&2
    exit 1
fi

if [ ! -f "$CONFIG_PATH" ]; then
    echo "WARNING: $CONFIG_PATH does not exist yet." >&2
    echo "Run: keepalive config show-example > \"$CONFIG_PATH\"" >&2
    echo "then edit it, before installing the agent." >&2
fi

echo "This script will run the following commands:"
echo
echo "  mkdir -p \"$LOG_DIR\""
echo "  sed -e \"s#__KEEPALIVE_REPO_DIR__#$REPO_DIR#g\" \\"
echo "      -e \"s#__KEEPALIVE_CONFIG_PATH__#$CONFIG_PATH#g\" \\"
echo "      -e \"s#__HOME__#$HOME#g\" \\"
echo "      \"$PLIST_SRC\" > \"$PLIST_DST\""
echo "  launchctl bootstrap gui/$UID_NUM \"$PLIST_DST\""
echo
echo "Each does the following:"
echo "  1. Creates the log directory the agent writes stdout/stderr to."
echo "  2. Renders the plist template into ~/Library/LaunchAgents, substituting"
echo "     this repo's path and your config path. No secrets are written."
echo "  3. Loads and starts the agent under your GUI launchd domain."
echo

if [ "$DO_IT" -ne 1 ]; then
    echo "Nothing has been changed. Re-run with --yes to execute the above."
    exit 0
fi

mkdir -p "$LOG_DIR"
sed -e "s#__KEEPALIVE_REPO_DIR__#$REPO_DIR#g" \
    -e "s#__KEEPALIVE_CONFIG_PATH__#$CONFIG_PATH#g" \
    -e "s#__HOME__#$HOME#g" \
    "$PLIST_SRC" > "$PLIST_DST"
launchctl bootstrap "gui/$UID_NUM" "$PLIST_DST"

echo "Done. Verify with:"
echo "  launchctl print gui/$UID_NUM/com.keepalive.agent"
echo "  keepalive status"
