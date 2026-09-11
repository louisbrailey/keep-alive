"""Tests for keepalive.core.timeutil — the single timezone convention every
other module in KeepAlive relies on (see that module's docstring for the bug
this exists to prevent: a naive-local `now` silently compared against an
aware-UTC timestamp)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from keepalive.core.timeutil import age_seconds, ensure_aware, now_aware


def test_now_aware_is_timezone_aware() -> None:
    value = now_aware()
    assert value.tzinfo is not None
    assert value.utcoffset() is not None


def test_now_aware_is_close_to_system_clock() -> None:
    before = datetime.now(UTC)
    value = now_aware()
    after = datetime.now(UTC)
    assert before <= value.astimezone(UTC) <= after + timedelta(seconds=1)


def test_ensure_aware_naive_assume_utc_attaches_utc_without_shifting_wall_clock() -> None:
    naive = datetime(2026, 9, 10, 12, 0, 0)
    result = ensure_aware(naive, "utc")
    assert result.tzinfo is not None
    assert result.utcoffset() == timedelta(0)
    # Wall-clock digits are unchanged -- only the offset was attached.
    assert result.replace(tzinfo=None) == naive


def test_ensure_aware_naive_assume_local_attaches_local_without_shifting_wall_clock() -> None:
    naive = datetime(2026, 9, 10, 12, 0, 0)
    result = ensure_aware(naive, "local")
    assert result.tzinfo is not None
    # .astimezone() on a naive datetime is documented to presume it already
    # represents local time and attach the correct offset for that instant --
    # so the wall-clock digits must survive unchanged.
    assert result.replace(tzinfo=None) == naive
    assert result == naive.astimezone()


def test_ensure_aware_default_assumption_is_local() -> None:
    naive = datetime(2026, 9, 10, 12, 0, 0)
    assert ensure_aware(naive) == ensure_aware(naive, "local")


@pytest.mark.parametrize("assume", ["utc", "local"])
def test_ensure_aware_aware_input_is_returned_unchanged(assume: str) -> None:
    fixed_zone = timezone(timedelta(hours=-7))
    aware = datetime(2026, 9, 10, 12, 0, 0, tzinfo=fixed_zone)
    result = ensure_aware(aware, assume)  # type: ignore[arg-type]
    assert result is aware or result == aware
    assert result.tzinfo == fixed_zone


def test_ensure_aware_aware_utc_input_is_not_reinterpreted_as_local() -> None:
    # A datetime already carrying an offset must never be treated as though
    # it were naive-and-meaning-something-else, regardless of `assume`.
    aware_utc = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    assert ensure_aware(aware_utc, "local") == aware_utc


def test_age_seconds_same_instant_different_zones_is_zero() -> None:
    utc_now = datetime(2026, 9, 10, 19, 0, 0, tzinfo=UTC)
    same_instant_minus_seven = utc_now.astimezone(timezone(timedelta(hours=-7)))
    assert age_seconds(utc_now, same_instant_minus_seven) == pytest.approx(0.0)


def test_age_seconds_positive_gap_across_zones() -> None:
    # 60 seconds "ago", expressed in a completely different offset than "now" --
    # age_seconds must reduce to the true elapsed time, not the difference in
    # the two datetimes' wall-clock digits.
    then_local = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone(timedelta(hours=-7)))
    now_different_zone = (then_local + timedelta(seconds=60)).astimezone(UTC)
    assert age_seconds(now_different_zone, then_local) == pytest.approx(60.0)


def test_age_seconds_negative_when_then_is_in_the_future() -> None:
    now = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    future = now + timedelta(seconds=30)
    assert age_seconds(now, future) == pytest.approx(-30.0)


def test_age_seconds_mixing_naive_and_aware_raises_type_error() -> None:
    # This is the exact class of bug keepalive.core.timeutil exists to
    # prevent from being silent: mixing naive and aware must be a loud
    # TypeError at the call site (fixed by wrapping the naive value in
    # ensure_aware(..., <correct assumption>)), never a wrong-but-plausible
    # number.
    aware = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    naive = datetime(2026, 9, 10, 12, 0, 0)
    with pytest.raises(TypeError):
        age_seconds(aware, naive)
    with pytest.raises(TypeError):
        age_seconds(naive, aware)
