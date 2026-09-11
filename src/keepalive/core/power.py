"""AC/battery and sleep/wake observation (read-only) plus bounded caffeinate windows.

``PowerObserver`` only ever runs ``pmset -g batt`` / ``pmset -g log``. It is
tolerant of any failure (binary missing, non-zero exit, unparseable output)
and reports ``on_ac=None`` rather than raising or guessing.

``WakeWindow`` starts ``caffeinate -s -w <own pid>`` (so the hold dies with
this process) additionally bounded with ``-t <seconds>`` to
``min(minutes, max_wake_minutes)``. It refuses to start on battery unless
``allow_on_battery`` is set, and refuses entirely unless ``wake_windows`` is
enabled. KeepAlive never runs ``pmset -a ...`` or touches power policy.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from keepalive.core.audit import AuditLog
from keepalive.core.config import PowerSettings

_PMSET = "/usr/bin/pmset"
_CAFFEINATE = "/usr/bin/caffeinate"
_TIMEOUT_S = 10

_BATT_PCT_RE = re.compile(r"(\d+)%")
_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


@dataclass(slots=True)
class PowerState:
    on_ac: bool | None
    battery_pct: int | None
    slept_since_last: bool
    last_sleep_at: datetime | None
    last_wake_at: datetime | None
    detail: str = ""


def _run(args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _read_ac_state() -> tuple[bool | None, int | None]:
    output = _run([_PMSET, "-g", "batt"])
    if output is None:
        return None, None
    on_ac = "AC Power" in output
    match = _BATT_PCT_RE.search(output)
    pct = int(match.group(1)) if match else None
    return on_ac, pct


def _parse_log_timestamp(line: str) -> datetime | None:
    match = _LOG_TS_RE.match(line.strip())
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


class PowerObserver:
    def __init__(self, settings: PowerSettings, state_dir: Path) -> None:
        self.settings = settings
        self.state_dir = state_dir

    def observe(self, now: datetime, last_cycle_at: datetime | None) -> PowerState:
        if not self.settings.observe:
            return PowerState(
                on_ac=None,
                battery_pct=None,
                slept_since_last=False,
                last_sleep_at=None,
                last_wake_at=None,
                detail="power observation disabled",
            )
        on_ac, battery_pct = _read_ac_state()
        batt_detail = "" if on_ac is not None else "pmset -g batt unavailable"
        slept, last_sleep_at, last_wake_at, log_detail = self._read_log(last_cycle_at, now)
        detail = "; ".join(d for d in (batt_detail, log_detail) if d)
        return PowerState(
            on_ac=on_ac,
            battery_pct=battery_pct,
            slept_since_last=slept,
            last_sleep_at=last_sleep_at,
            last_wake_at=last_wake_at,
            detail=detail,
        )

    def _read_log(
        self, last_cycle_at: datetime | None, now: datetime
    ) -> tuple[bool, datetime | None, datetime | None, str]:
        output = _run([_PMSET, "-g", "log"])
        if output is None:
            return False, None, None, "pmset -g log unavailable"
        last_sleep: datetime | None = None
        last_wake: datetime | None = None
        for line in output.splitlines():
            ts = _parse_log_timestamp(line)
            if ts is None or ts > now:
                continue
            if last_cycle_at is not None and ts < last_cycle_at:
                continue
            is_sleep_line = "Sleep" in line and "DarkWake" not in line
            if is_sleep_line and (last_sleep is None or ts > last_sleep):
                last_sleep = ts
            elif "Wake" in line and (last_wake is None or ts > last_wake):
                last_wake = ts
        return last_sleep is not None, last_sleep, last_wake, ""


class WakeWindow:
    def __init__(self, settings: PowerSettings, audit: AuditLog) -> None:
        self.settings = settings
        self.audit = audit
        self._proc: subprocess.Popen[bytes] | None = None

    def hold(self, reason: str, minutes: int) -> bool:
        if not self.settings.wake_windows:
            self.audit.record("wake_window_refused", reason=reason, cause="wake_windows_disabled")
            return False
        if self._proc is not None and self._proc.poll() is None:
            self.audit.record("wake_window_refused", reason=reason, cause="already_held")
            return False

        on_ac, _pct = _read_ac_state()
        if on_ac is not True and not self.settings.allow_on_battery:
            self.audit.record("wake_window_refused", reason=reason, cause="on_battery_or_unknown")
            return False

        bounded_minutes = max(1, min(minutes, self.settings.max_wake_minutes))
        try:
            proc = subprocess.Popen(
                [_CAFFEINATE, "-s", "-w", str(os.getpid()), "-t", str(bounded_minutes * 60)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            self.audit.record(
                "wake_window_refused", reason=reason, cause=f"caffeinate_failed:{exc}"
            )
            return False

        self._proc = proc
        self.audit.record("wake_window_started", reason=reason, minutes=bounded_minutes)
        return True

    def release(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=_TIMEOUT_S)
            except (OSError, subprocess.TimeoutExpired):
                with contextlib.suppress(OSError):
                    self._proc.kill()
        self._proc = None
        self.audit.record("wake_window_released", reason="release")


__all__ = ["PowerObserver", "PowerState", "WakeWindow"]
