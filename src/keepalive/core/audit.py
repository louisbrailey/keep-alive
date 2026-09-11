"""Append-only JSONL audit log.

One record per check, decision, attempt/result, suppression, notification,
and power event. Any field whose *key* contains ``url``, ``token``,
``secret``, or ``password`` (case-insensitive) is redacted before it is
written — callers should still never pass an actual secret value in, but this
is the last line of defense.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REDACT_MARKERS = ("url", "token", "secret", "password")
_REDACTED = "<redacted>"


def _redact(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (_REDACTED if any(marker in key.lower() for marker in _REDACT_MARKERS) else value)
        for key, value in fields.items()
    }


class AuditLog:
    def __init__(self, path: Path, max_bytes: int) -> None:
        self.path = path
        self.max_bytes = max_bytes

    def record(self, event: str, **fields: Any) -> None:
        entry: dict[str, Any] = {"ts": datetime.now(UTC).isoformat(), "event": event}
        entry.update(_redact(fields))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_if_needed()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, default=str, sort_keys=True))
            handle.write("\n")

    def tail(self, n: int) -> list[dict[str, Any]]:
        if n <= 0 or not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        records: list[dict[str, Any]] = []
        for line in lines[-n:]:
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records

    def _rotate_if_needed(self) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size >= self.max_bytes:
            rotated = self.path.with_name(self.path.name + ".1")
            self.path.replace(rotated)


__all__ = ["AuditLog"]
