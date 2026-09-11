"""HTTP health-check adapter.

Read-only ``GET`` against a configured URL via :mod:`urllib.request`. A connection
error or timeout is a check that *could not run* — reported as ``UNKNOWN``, never a
service failure. A reachable response with the wrong status code or missing an
expected substring is ``FAILED``. Declares no recovery actions.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from keepalive.core.adapter import (
    Adapter,
    AdapterManifest,
    CheckContext,
    CheckResult,
    CheckSpec,
    DataSource,
    NotifyCondition,
    Status,
)

_DEFAULT_TIMEOUT_S = 5.0
_DEFAULT_EXPECT_STATUS = 200


class HttpAdapter(Adapter):
    """Reports OK/FAILED/UNKNOWN based on an HTTP GET against ``url``."""

    kind = "http"

    @classmethod
    def describe(cls) -> AdapterManifest:
        return AdapterManifest(
            kind=cls.kind,
            description="Polls an HTTP(S) URL; checks the status code and/or a response substring.",
            checks=(
                CheckSpec(
                    name="http_probe",
                    description="GET <url>; check status code and optional response substring.",
                ),
            ),
            reads=(DataSource(kind="http", target="<url>", read_only=True),),
            actions=(),
            notify_on=(
                NotifyCondition(event="incident_opened", description="Unexpected status or body"),
                NotifyCondition(event="recovered", description="Response healthy again"),
                NotifyCondition(event="check_unavailable", description="Repeated network failures"),
            ),
            treat_wake_as_transition=True,
            params_schema={
                "url": "URL to GET (required)",
                "timeout_s": "Request timeout in seconds (default 5)",
                "expect_status": "Expected HTTP status code (default 200; 0 = don't check)",
                "expect_substring": "Substring expected in the response body (empty = don't check)",
            },
        )

    def validate_params(self) -> list[str]:
        problems = super().validate_params()
        url = self.params.get("url")
        if not url or not isinstance(url, str):
            problems.append("'url' is required and must be a non-empty string")
        return problems

    def check(self, ctx: CheckContext) -> CheckResult:
        url = str(self.params.get("url", ""))
        timeout_s = float(self.params.get("timeout_s", _DEFAULT_TIMEOUT_S))
        expect_status = int(self.params.get("expect_status", _DEFAULT_EXPECT_STATUS))
        expect_substring = str(self.params.get("expect_substring", "") or "")

        if urlparse(url).scheme not in ("http", "https"):
            return CheckResult(
                status=Status.UNKNOWN,
                detail="url: unsupported scheme, skipped",
                metrics={},
            )

        request = urllib.request.Request(url, method="GET")  # noqa: S310 - scheme validated above
        try:
            # scheme validated above
            with urllib.request.urlopen(  # noqa: S310  # nosec B310
                request, timeout=timeout_s
            ) as response:
                status = response.status
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            # A reachable server returned an error status — the check itself ran fine.
            status = exc.code
            body = ""
        except (urllib.error.URLError, OSError) as exc:
            # Connection refused, DNS failure, timeout, etc.: the check could not run.
            return CheckResult(status=Status.UNKNOWN, detail="network", metrics={"error": str(exc)})

        metrics: dict[str, Any] = {"status": status}
        if expect_status and status != expect_status:
            return CheckResult(
                status=Status.FAILED,
                detail=f"unexpected status {status} (expected {expect_status})",
                metrics=metrics,
            )
        if expect_substring and expect_substring not in body:
            return CheckResult(
                status=Status.FAILED,
                detail="expected substring not found in response body",
                metrics=metrics,
            )
        return CheckResult(status=Status.OK, detail=f"status {status}", metrics=metrics)


__all__ = ["HttpAdapter"]
