"""launchctl print parsing: the job's top-level state wins over nested blocks."""

from __future__ import annotations

from keepalive.adapters.launchd import LaunchdAdapter

RAW = """gui/501/com.example.worker = {
\tactive count = 1
\tpath = /Users/x/Library/LaunchAgents/com.example.worker.plist
\tstate = running

\tprogram = /usr/bin/true
\tpid = 4242
\tlast exit code = (never exited)

\tspawn type = daemon (3)
\tinstances = {
\t\tstate = active
\t\tpid = 9999
\t}
}
"""


def test_top_level_state_and_pid_win() -> None:
    info = LaunchdAdapter._parse_print(RAW)
    assert info["state"] == "running"
    assert info["pid"] == "4242"
    assert info["last exit code"] == "(never exited)"
