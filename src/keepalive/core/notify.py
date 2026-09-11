"""macOS notification (osascript) and optional webhook, both off by default.

The webhook URL is never read from config or passed around as a value — it
is looked up from the environment variable *named* in
``NotificationSettings.webhook_url_env`` at send time, and only that name
(never the value) is ever written to the audit log.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from datetime import UTC, datetime

from keepalive.core.audit import AuditLog
from keepalive.core.config import NotificationSettings

_OSASCRIPT = "/usr/bin/osascript"
_TIMEOUT_S = 10


class Notifier:
    def __init__(self, settings: NotificationSettings, audit: AuditLog) -> None:
        self.settings = settings
        self.audit = audit

    def send(self, event: str, title: str, message: str) -> bool:
        """Never raises. Returns whether at least one channel reported success."""
        if event not in self.settings.notify_on:
            return False

        sent_macos = self._send_macos(title, message) if self.settings.macos else False
        sent_webhook = self._send_webhook(event, title, message) if self.settings.webhook else False
        sent = sent_macos or sent_webhook

        self.audit.record(
            "notify",
            notify_event=event,
            title=title,
            macos_enabled=self.settings.macos,
            macos_sent=sent_macos,
            webhook_enabled=self.settings.webhook,
            webhook_sent=sent_webhook,
            webhook_env_var=self.settings.webhook_url_env,
        )
        return sent

    def _send_macos(self, title: str, message: str) -> bool:
        script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
        try:
            result = subprocess.run(
                [_OSASCRIPT, "-e", script],
                capture_output=True,
                timeout=_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def _send_webhook(self, event: str, title: str, message: str) -> bool:
        url = os.environ.get(self.settings.webhook_url_env)
        if not url or not url.startswith(("http://", "https://")):
            return False
        payload = json.dumps(
            {
                "event": event,
                "title": title,
                "message": message,
                "ts": datetime.now(UTC).isoformat(),
            }
        ).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - scheme validated above
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            # scheme validated above
            with urllib.request.urlopen(  # noqa: S310  # nosec B310
                request, timeout=_TIMEOUT_S
            ) as response:
                return bool(200 <= response.status < 300)
        except (urllib.error.URLError, OSError, ValueError):
            return False


__all__ = ["Notifier"]
