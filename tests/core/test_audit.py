"""Tests for keepalive.core.audit."""

from __future__ import annotations

import json
from pathlib import Path

from keepalive.core.audit import AuditLog


def test_record_appends_jsonl_line(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    log.record("check", service="svc", status="ok")
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["event"] == "check"
    assert entry["service"] == "svc"
    assert entry["status"] == "ok"
    assert "ts" in entry


def test_record_creates_parent_directory(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "nested" / "audit.jsonl", max_bytes=10_000_000)
    log.record("check")
    assert (tmp_path / "nested" / "audit.jsonl").exists()


def test_redacts_fields_whose_key_contains_marker(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    log.record(
        "notify",
        webhook_url="https://example.com/secret-hook",
        webhook_env_var="KEEPALIVE_WEBHOOK_URL",
        api_token="abc123",
        account_password="hunter2",
        plain_field="visible",
    )
    entry = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert entry["webhook_url"] == "<redacted>"
    assert entry["api_token"] == "<redacted>"
    assert entry["account_password"] == "<redacted>"
    assert entry["plain_field"] == "visible"
    # The *name* of the env var (not a secret itself) is not redacted, since
    # the key itself contains "env_var" not one of the secret markers... but
    # it does contain "url" via webhook_env_var? no: "webhook_env_var" does
    # not contain "url". Confirm it passes through untouched.
    assert entry["webhook_env_var"] == "KEEPALIVE_WEBHOOK_URL"


def test_redaction_is_case_insensitive(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    log.record("notify", WebHook_URL="https://example.com")
    entry = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert entry["WebHook_URL"] == "<redacted>"


def test_tail_returns_last_n_records(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    for i in range(5):
        log.record("check", i=i)
    tail = log.tail(2)
    assert [entry["i"] for entry in tail] == [3, 4]


def test_tail_on_missing_file_returns_empty(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    assert log.tail(10) == []


def test_tail_zero_or_negative_returns_empty(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    log.record("check")
    assert log.tail(0) == []
    assert log.tail(-1) == []


def test_tail_skips_corrupted_lines(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, max_bytes=10_000_000)
    log.record("check", i=1)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
        handle.write("\n")
    log.record("check", i=2)
    tail = log.tail(10)
    assert [entry["i"] for entry in tail] == [1, 2]


def test_rotates_when_over_max_bytes(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, max_bytes=1)  # any write will exceed this
    log.record("check", detail="first")
    assert path.exists()
    log.record("check", detail="second")
    rotated = tmp_path / "audit.jsonl.1"
    assert rotated.exists()
    # The rotated file holds the first record; the live file holds only the second.
    rotated_lines = rotated.read_text(encoding="utf-8").splitlines()
    live_lines = path.read_text(encoding="utf-8").splitlines()
    assert len(rotated_lines) == 1
    assert len(live_lines) == 1
    assert json.loads(rotated_lines[0])["detail"] == "first"
    assert json.loads(live_lines[0])["detail"] == "second"


def test_no_rotation_below_max_bytes(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, max_bytes=10_000_000)
    log.record("check", detail="first")
    log.record("check", detail="second")
    assert not (tmp_path / "audit.jsonl.1").exists()
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_non_serializable_value_falls_back_to_str(tmp_path: Path) -> None:
    class Weird:
        def __str__(self) -> str:
            return "weird-value"

    log = AuditLog(tmp_path / "audit.jsonl", max_bytes=10_000_000)
    log.record("check", thing=Weird())
    entry = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert entry["thing"] == "weird-value"
