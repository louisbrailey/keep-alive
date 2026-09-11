"""HTTP adapter: refused/loopback-timeout are failures; other network errors are UNKNOWN."""

from __future__ import annotations

import urllib.error
from datetime import datetime

import pytest

from keepalive.adapters import http as http_mod
from keepalive.adapters.http import HttpAdapter
from keepalive.core.adapter import CheckContext, Status


def _ctx() -> CheckContext:
    return CheckContext(
        service_name="t",
        params={},
        now=datetime.now().astimezone(),
        on_ac_power=True,
        slept_since_last_cycle=False,
        last_cycle_at=None,
        state_dir="/tmp",
    )


def _raise(exc: Exception):
    def _open(*_a, **_k):
        raise exc

    return _open


@pytest.mark.parametrize(
    ("url", "exc", "expected"),
    [
        (
            "http://127.0.0.1:1/x",
            urllib.error.URLError(ConnectionRefusedError(61, "refused")),
            Status.FAILED,
        ),
        ("http://localhost:1/x", TimeoutError("timed out"), Status.FAILED),
        ("http://example.invalid/x", TimeoutError("timed out"), Status.UNKNOWN),
        (
            "http://example.invalid/x",
            urllib.error.URLError("nodename nor servname provided"),
            Status.UNKNOWN,
        ),
    ],
)
def test_network_error_classification(
    monkeypatch: pytest.MonkeyPatch, url: str, exc: Exception, expected: Status
) -> None:
    monkeypatch.setattr(http_mod.urllib.request, "urlopen", _raise(exc))
    r = HttpAdapter({"url": url, "timeout_s": 1, "expect_status": 200}).check(_ctx())
    assert r.status is expected
