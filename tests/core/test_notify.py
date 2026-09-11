"""Tests for keepalive.core.notify. No real osascript/network calls — subprocess
and urllib are monkeypatched."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from keepalive.core.audit import AuditLog
from keepalive.core.config import NotificationSettings
from keepalive.core.notify import Notifier


def _settings(**overrides: Any) -> NotificationSettings:
    base = dict(
        macos=False,
        webhook=False,
        webhook_url_env="KEEPALIVE_WEBHOOK_URL",
        summary_daily_at="",
        notify_on=["incident_opened", "recovered", "circuit_open", "check_unavailable"],
    )
    base.update(overrides)
    return NotificationSettings(**base)  # type: ignore[arg-type]


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def test_event_not_in_notify_on_is_skipped(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    settings = _settings(macos=True, notify_on=["recovered"])
    notifier = Notifier(settings, audit)
    sent = notifier.send("incident_opened", "title", "message")
    assert sent is False
    assert not (tmp_path / "audit.jsonl").exists()


def test_macos_disabled_and_webhook_disabled_sends_nothing(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    notifier = Notifier(_settings(), audit)
    sent = notifier.send("incident_opened", "title", "message")
    assert sent is False


def test_macos_notification_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0)

    monkeypatch.setattr("keepalive.core.notify.subprocess.run", fake_run)
    audit = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    notifier = Notifier(_settings(macos=True), audit)

    sent = notifier.send("incident_opened", "KeepAlive: svc", "down")
    assert sent is True
    assert calls[0][0] == "/usr/bin/osascript"
    assert calls[0][1] == "-e"
    assert "down" in calls[0][2]
    assert "KeepAlive: svc" in calls[0][2]


def test_macos_notification_failure_returncode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, returncode=1)

    monkeypatch.setattr("keepalive.core.notify.subprocess.run", fake_run)
    audit = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    notifier = Notifier(_settings(macos=True), audit)
    assert notifier.send("incident_opened", "t", "m") is False


def test_macos_notification_raises_oserror_is_caught(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise OSError("no osascript")

    monkeypatch.setattr("keepalive.core.notify.subprocess.run", fake_run)
    audit = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    notifier = Notifier(_settings(macos=True), audit)
    assert notifier.send("incident_opened", "t", "m") is False


def test_macos_notification_timeout_is_caught(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd=args, timeout=10)

    monkeypatch.setattr("keepalive.core.notify.subprocess.run", fake_run)
    audit = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    notifier = Notifier(_settings(macos=True), audit)
    assert notifier.send("incident_opened", "t", "m") is False


def test_webhook_disabled_never_reads_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KEEPALIVE_WEBHOOK_URL", "https://example.com/hook")

    def fail_urlopen(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not be called when webhook disabled")

    monkeypatch.setattr("keepalive.core.notify.urllib.request.urlopen", fail_urlopen)
    audit = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    notifier = Notifier(_settings(webhook=False), audit)
    assert notifier.send("incident_opened", "t", "m") is False


def test_webhook_success_posts_json_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KEEPALIVE_WEBHOOK_URL", "https://example.com/hook")
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(200)

    monkeypatch.setattr("keepalive.core.notify.urllib.request.urlopen", fake_urlopen)
    audit = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    notifier = Notifier(_settings(webhook=True), audit)

    sent = notifier.send("incident_opened", "title", "message")
    assert sent is True
    assert captured["url"] == "https://example.com/hook"
    assert captured["method"] == "POST"
    assert captured["body"]["event"] == "incident_opened"
    assert captured["body"]["title"] == "title"
    assert captured["body"]["message"] == "message"
    assert "ts" in captured["body"]


def test_webhook_env_var_unset_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("KEEPALIVE_WEBHOOK_URL", raising=False)

    def fail_urlopen(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not be called when env var unset")

    monkeypatch.setattr("keepalive.core.notify.urllib.request.urlopen", fail_urlopen)
    audit = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    notifier = Notifier(_settings(webhook=True), audit)
    assert notifier.send("incident_opened", "t", "m") is False


def test_webhook_rejects_non_http_scheme(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KEEPALIVE_WEBHOOK_URL", "file:///etc/passwd")

    def fail_urlopen(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not be called for a non-http(s) URL")

    monkeypatch.setattr("keepalive.core.notify.urllib.request.urlopen", fail_urlopen)
    audit = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    notifier = Notifier(_settings(webhook=True), audit)
    assert notifier.send("incident_opened", "t", "m") is False


def test_webhook_network_error_is_caught(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import urllib.error

    monkeypatch.setenv("KEEPALIVE_WEBHOOK_URL", "https://example.com/hook")

    def fake_urlopen(request: Any, timeout: float) -> Any:
        raise urllib.error.URLError("down")

    monkeypatch.setattr("keepalive.core.notify.urllib.request.urlopen", fake_urlopen)
    audit = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    notifier = Notifier(_settings(webhook=True), audit)
    assert notifier.send("incident_opened", "t", "m") is False


def test_webhook_non_2xx_status_is_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KEEPALIVE_WEBHOOK_URL", "https://example.com/hook")

    def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        return _FakeResponse(500)

    monkeypatch.setattr("keepalive.core.notify.urllib.request.urlopen", fake_urlopen)
    audit = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    notifier = Notifier(_settings(webhook=True), audit)
    assert notifier.send("incident_opened", "t", "m") is False


def test_audit_record_never_contains_webhook_url_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KEEPALIVE_WEBHOOK_URL", "https://example.com/super-secret-hook")

    def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        return _FakeResponse(200)

    monkeypatch.setattr("keepalive.core.notify.urllib.request.urlopen", fake_urlopen)
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditLog(audit_path, max_bytes=10_000_000)
    notifier = Notifier(_settings(webhook=True), audit)
    notifier.send("incident_opened", "t", "m")

    raw = audit_path.read_text(encoding="utf-8")
    assert "super-secret-hook" not in raw
    entry = json.loads(raw.splitlines()[0])
    assert entry["webhook_env_var"] == "KEEPALIVE_WEBHOOK_URL"


def test_both_channels_enabled_records_both_outcomes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KEEPALIVE_WEBHOOK_URL", "https://example.com/hook")

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, returncode=0)

    def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        return _FakeResponse(200)

    monkeypatch.setattr("keepalive.core.notify.subprocess.run", fake_run)
    monkeypatch.setattr("keepalive.core.notify.urllib.request.urlopen", fake_urlopen)
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditLog(audit_path, max_bytes=10_000_000)
    notifier = Notifier(_settings(macos=True, webhook=True), audit)

    assert notifier.send("incident_opened", "t", "m") is True
    entry = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert entry["macos_sent"] is True
    assert entry["webhook_sent"] is True
