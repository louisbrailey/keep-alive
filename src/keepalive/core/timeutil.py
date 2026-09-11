"""Single timezone convention for KeepAlive.

Every datetime that crosses a module boundary — engine to adapter, adapter to
an external timestamp (a file mtime, a `ps`/`pmset` command, a sqlite column,
a persisted state field), core to persisted state — is timezone-**aware**.

Ad-hoc `datetime.now()` / `datetime.fromisoformat()` / `datetime.fromtimestamp()`
calls silently produce *naive* datetimes. Two naive datetimes compare and
subtract without error even when they secretly represent different zones (one
local, one UTC), producing a wrong-but-plausible-looking answer instead of a
crash — that is what turned a naive-local `engine.now` compared against an
aware-UTC heartbeat timestamp into a fresh heartbeat reporting an age of
roughly *minus* the local UTC offset. Mixing a naive and an aware datetime,
when it doesn't produce a silently wrong answer, raises `TypeError` on
subtraction or ordering.

Use the helpers below anywhere a datetime is created from, or compared
against, something from outside the current function.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

#: What a naive datetime should be assumed to mean when it must be made aware:
#: "local" (system wall-clock — `ps lstart`, `pmset -g log`, a pre-fix legacy
#: naive-local persisted timestamp) or "utc" (a naive-UTC source, e.g. Signal
#: Forge's own timestamp columns).
Assumption = Literal["local", "utc"]


def now_aware() -> datetime:
    """The current time, timezone-aware, in the system's local zone."""
    return datetime.now().astimezone()


def ensure_aware(dt: datetime, assume: Assumption = "local") -> datetime:
    """Return `dt` unchanged if it already carries a UTC offset; otherwise
    attach one under the stated assumption about what naive `dt` means.

    `assume="local"`: `dt` is system local wall-clock time. A naive
    datetime's `.astimezone()` is documented to presume it represents local
    time and attaches the correct (DST-aware) offset for that instant without
    changing the wall-clock numbers.

    `assume="utc"`: `dt` is UTC wall-clock time with the offset/`Z` stripped
    — attach `UTC` directly (also without changing the wall-clock numbers).
    """
    if dt.tzinfo is not None and dt.utcoffset() is not None:
        return dt
    if assume == "utc":
        return dt.replace(tzinfo=UTC)
    return dt.astimezone()


def age_seconds(now: datetime, then: datetime) -> float:
    """`(now - then)` in seconds.

    Both arguments must already be timezone-aware. Mixing a naive and an
    aware datetime here is a bug at the call site, not something to paper
    over silently — wrap the offending value in `ensure_aware(...)` (with the
    correct assumption for *where it came from*) before calling this.
    """
    return (now - then).total_seconds()


__all__ = ["Assumption", "age_seconds", "ensure_aware", "now_aware"]
