"""Disk free-space adapter.

Read-only: ``shutil.disk_usage`` on a configured mount/path. Declares no recovery
actions — KeepAlive never frees disk space on its own.
"""

from __future__ import annotations

import os
import shutil

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

_REQUIRED_PARAMS = ("path", "warn_free_pct", "fail_free_pct")


class DiskAdapter(Adapter):
    """Reports OK/DEGRADED/FAILED based on free space percentage of ``path``."""

    kind = "disk"

    @classmethod
    def describe(cls) -> AdapterManifest:
        return AdapterManifest(
            kind=cls.kind,
            description="Observes free disk space on a filesystem path.",
            checks=(
                CheckSpec(
                    name="free_space",
                    description="Free space percentage of the filesystem containing `path`.",
                ),
            ),
            reads=(DataSource(kind="file", target="<path>", read_only=True),),
            actions=(),
            notify_on=(
                NotifyCondition(
                    event="incident_opened", description="Free space below fail_free_pct"
                ),
                NotifyCondition(event="recovered", description="Free space healthy again"),
                NotifyCondition(
                    event="check_unavailable", description="Repeated failures reading disk usage"
                ),
            ),
            treat_wake_as_transition=True,
            params_schema={
                "path": "Filesystem path to check (expanduser-expanded).",
                "warn_free_pct": "Free-space percentage below which status is DEGRADED.",
                "fail_free_pct": "Free-space percentage below which status is FAILED.",
            },
        )

    def validate_params(self) -> list[str]:
        problems = super().validate_params()
        for key in _REQUIRED_PARAMS:
            if key not in self.params:
                problems.append(f"missing required param {key!r}")
        warn = self.params.get("warn_free_pct")
        fail = self.params.get("fail_free_pct")
        if isinstance(warn, int | float) and isinstance(fail, int | float) and fail > warn:
            problems.append("fail_free_pct must be <= warn_free_pct")
        return problems

    def check(self, ctx: CheckContext) -> CheckResult:
        raw_path = self.params.get("path")
        warn_free_pct = self.params.get("warn_free_pct")
        fail_free_pct = self.params.get("fail_free_pct")
        if not isinstance(raw_path, str) or not raw_path:
            return CheckResult(status=Status.UNKNOWN, detail="disk adapter missing 'path' param")
        if not isinstance(warn_free_pct, int | float) or not isinstance(fail_free_pct, int | float):
            return CheckResult(
                status=Status.UNKNOWN,
                detail="disk adapter missing numeric 'warn_free_pct'/'fail_free_pct'",
            )

        path = os.path.expanduser(raw_path)
        try:
            usage = shutil.disk_usage(path)
        except OSError as exc:
            return CheckResult(
                status=Status.UNKNOWN,
                detail=f"could not read disk usage for {path!r}: {exc}",
            )

        if usage.total <= 0:
            return CheckResult(
                status=Status.UNKNOWN,
                detail=f"disk usage for {path!r} reported zero total size",
            )

        free_pct = (usage.free / usage.total) * 100
        metrics = {"free_pct": free_pct, "free_bytes": usage.free}

        if free_pct < fail_free_pct:
            status = Status.FAILED
        elif free_pct < warn_free_pct:
            status = Status.DEGRADED
        else:
            status = Status.OK

        detail = f"{path}: {free_pct:.1f}% free ({usage.free} bytes)"
        return CheckResult(status=status, detail=detail, metrics=metrics)


__all__ = ["DiskAdapter"]
