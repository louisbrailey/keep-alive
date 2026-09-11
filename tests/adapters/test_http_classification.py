"""HTTP adapter: refused/loopback-timeout are failures; other network errors are UNKNOWN;
loopback probes bypass proxies."""

from __future__ import annotations

import urllib.error
from datetime import datetime
from types import SimpleNamespace

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


def _opener_raising(exc: Exception):
    def _open(*_a, **_k):
        raise exc

    def _build(*_handlers):
        return SimpleNamespace(open=_open)

    return _build


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
    monkeypatch.setattr(http_mod.urllib.request, "build_opener", _opener_raising(exc))
    r = HttpAdapter({"url": url, "timeout_s": 1, "expect_status": 200}).check(_ctx())
    assert r.status is expected


def test_loopback_bypasses_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple] = []

    class _Resp:
        status = 200

        def read(self) -> bytes:
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def _build(*handlers):
        seen.append(handlers)
        return SimpleNamespace(open=lambda *_a, **_k: _Resp())

    monkeypatch.setattr(http_mod.urllib.request, "build_opener", _build)
    HttpAdapter({"url": "http://127.0.0.1:1/x", "expect_status": 200}).check(_ctx())
    assert seen and isinstance(seen[0][0], http_mod.urllib.request.ProxyHandler)
    assert seen[0][0].proxies == {}
