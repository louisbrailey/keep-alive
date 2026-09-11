from __future__ import annotations

import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from keepalive.adapters.http import HttpAdapter
from keepalive.core.adapter import CheckContext, Status


def _ctx(tmp_path: Path) -> CheckContext:
    return CheckContext(
        service_name="svc",
        params={},
        now=datetime(2026, 9, 10, 12, 0, 0),
        on_ac_power=True,
        slept_since_last_cycle=False,
        last_cycle_at=None,
        state_dir=str(tmp_path),
    )


class _FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def test_describe_declares_no_actions() -> None:
    manifest = HttpAdapter.describe()
    assert manifest.kind == "http"
    assert manifest.actions == ()


def test_validate_params_requires_url() -> None:
    adapter = HttpAdapter({})
    problems = adapter.validate_params()
    assert any("url" in p for p in problems)


def test_healthy_response(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "keepalive.adapters.http.urllib.request.urlopen",
        lambda req, timeout: _FakeResponse(200, "all good"),
    )
    adapter = HttpAdapter({"url": "http://127.0.0.1:8787/health", "expect_status": 200})
    result = adapter.check(_ctx(tmp_path))
    assert result.status == Status.OK


def test_wrong_status_is_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "keepalive.adapters.http.urllib.request.urlopen",
        lambda req, timeout: _FakeResponse(500, "server error"),
    )
    adapter = HttpAdapter({"url": "http://127.0.0.1:8787/health", "expect_status": 200})
    result = adapter.check(_ctx(tmp_path))
    assert result.status == Status.FAILED
    assert result.metrics["status"] == 500


def test_missing_substring_is_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "keepalive.adapters.http.urllib.request.urlopen",
        lambda req, timeout: _FakeResponse(200, "nope"),
    )
    adapter = HttpAdapter(
        {"url": "http://127.0.0.1:8787/health", "expect_status": 200, "expect_substring": "healthy"}
    )
    result = adapter.check(_ctx(tmp_path))
    assert result.status == Status.FAILED


def test_http_error_status_checked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _raise(req: Any, timeout: float) -> Any:
        raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr("keepalive.adapters.http.urllib.request.urlopen", _raise)
    adapter = HttpAdapter({"url": "http://127.0.0.1:8787/health", "expect_status": 200})
    result = adapter.check(_ctx(tmp_path))
    assert result.status == Status.FAILED
    assert result.metrics["status"] == 503


def test_connection_refused_is_a_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _raise(req: Any, timeout: float) -> Any:
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    monkeypatch.setattr("keepalive.adapters.http.urllib.request.urlopen", _raise)
    adapter = HttpAdapter({"url": "http://127.0.0.1:8787/health"})
    result = adapter.check(_ctx(tmp_path))
    assert result.status == Status.FAILED  # nothing is listening: the service is down
    assert result.detail == "connection refused"


def test_loopback_timeout_is_a_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _raise(req: Any, timeout: float) -> Any:
        raise TimeoutError("timed out")

    monkeypatch.setattr("keepalive.adapters.http.urllib.request.urlopen", _raise)
    adapter = HttpAdapter({"url": "http://127.0.0.1:8787/health"})
    result = adapter.check(_ctx(tmp_path))
    assert result.status == Status.FAILED  # a local listener that never answers is down
    assert result.detail == "no response (timeout)"


def test_remote_timeout_is_unknown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _raise(req: Any, timeout: float) -> Any:
        raise TimeoutError("timed out")

    monkeypatch.setattr("keepalive.adapters.http.urllib.request.urlopen", _raise)
    adapter = HttpAdapter({"url": "http://example.invalid/health"})
    result = adapter.check(_ctx(tmp_path))
    assert result.status == Status.UNKNOWN
    assert result.detail == "network"


def test_unsupported_scheme_is_unknown_and_never_opens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _fail_if_called(req: Any, timeout: float) -> Any:
        raise AssertionError("urlopen must not be called for a non-http(s) url")

    monkeypatch.setattr("keepalive.adapters.http.urllib.request.urlopen", _fail_if_called)
    adapter = HttpAdapter({"url": "file:///etc/passwd"})
    result = adapter.check(_ctx(tmp_path))
    assert result.status == Status.UNKNOWN
    assert "scheme" in result.detail


def test_recover_declares_no_actions(tmp_path: Path) -> None:
    adapter = HttpAdapter({"url": "http://127.0.0.1:8787/health"})
    result = adapter.recover("anything", _ctx(tmp_path))
    assert result.ok is False
