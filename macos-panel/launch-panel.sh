#!/bin/bash
# Starts the Keep Alive control panel server if needed, then opens it in the default browser.
PY=$(ls -d "$HOME"/.local/share/uv/python/cpython-3.12*/bin/python3.12 2>/dev/null | head -1)
[ -x "$PY" ] || PY=$(command -v python3)
URL="http://127.0.0.1:47391/"
if ! curl -s -m 1 -o /dev/null "$URL/api/state"; then
  nohup "$PY" "$HOME/.keep-alive/panel.py" >/dev/null 2>&1 &
  for _ in $(seq 1 20); do curl -s -m 1 -o /dev/null "$URL/api/state" && break; sleep 0.25; done
fi
open "$URL"
