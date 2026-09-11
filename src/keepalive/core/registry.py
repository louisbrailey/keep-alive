"""Adapter discovery.

Core knows the *names* of the built-in adapters (so config validation and the
``keepalive adapters`` CLI can work without importing application code), but
never imports an adapter module until something actually asks for that kind.
Third-party / optional adapters (e.g. the Signal Forge integration) are found
through the ``keepalive.adapters`` entry-point group and are never hard-coded
here — that is what keeps core independent of any specific application.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.metadata

from keepalive.core.adapter import Adapter, AdapterManifest

#: kind -> "module:ClassName". Only the six general-purpose adapters that ship
#: with KeepAlive itself. Nothing application-specific belongs here.
BUILTIN: dict[str, str] = {
    "process": "keepalive.adapters.process:ProcessAdapter",
    "http": "keepalive.adapters.http:HttpAdapter",
    "launchd": "keepalive.adapters.launchd:LaunchdAdapter",
    "disk": "keepalive.adapters.disk:DiskAdapter",
    "job_freshness": "keepalive.adapters.job_freshness:JobFreshnessAdapter",
    "power": "keepalive.adapters.power:PowerAdapter",
}

_ENTRY_POINT_GROUP = "keepalive.adapters"


class AdapterNotFoundError(LookupError):
    """Raised when no adapter (built-in or entry point) matches a kind."""


def _load_target(target: str) -> type[Adapter]:
    module_name, _, class_name = target.partition(":")
    if not module_name or not class_name:
        raise AdapterNotFoundError(f"malformed adapter target {target!r}")
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name, None)
    if not (isinstance(cls, type) and issubclass(cls, Adapter)):
        raise AdapterNotFoundError(f"{target!r} does not resolve to an Adapter subclass")
    return cls


def get_adapter_class(kind: str) -> type[Adapter]:
    """Resolve an adapter kind to its class. Built-ins are checked first, then
    entry points registered under the ``keepalive.adapters`` group."""
    if kind in BUILTIN:
        return _load_target(BUILTIN[kind])
    for entry_point in importlib.metadata.entry_points(group=_ENTRY_POINT_GROUP):
        if entry_point.name == kind:
            cls = entry_point.load()
            if not (isinstance(cls, type) and issubclass(cls, Adapter)):
                raise AdapterNotFoundError(f"entry point {kind!r} is not an Adapter subclass")
            return cls
    raise AdapterNotFoundError(f"no adapter registered for kind {kind!r}")


def known_adapter_kinds() -> set[str]:
    """Every adapter kind name that can be resolved, without importing any of
    the underlying modules (entry-point *names* are metadata, not code)."""
    kinds = set(BUILTIN)
    with contextlib.suppress(Exception):  # metadata lookup must never break config loading
        kinds |= {ep.name for ep in importlib.metadata.entry_points(group=_ENTRY_POINT_GROUP)}
    return kinds


def list_adapters() -> list[AdapterManifest]:
    """Describe every resolvable adapter. Unlike ``known_adapter_kinds`` this
    does import each adapter module, since it needs ``describe()``."""
    kinds: dict[str, str] = dict(BUILTIN)
    for entry_point in importlib.metadata.entry_points(group=_ENTRY_POINT_GROUP):
        kinds.setdefault(entry_point.name, entry_point.value)
    return [get_adapter_class(kind).describe() for kind in sorted(kinds)]


__all__ = [
    "BUILTIN",
    "AdapterNotFoundError",
    "get_adapter_class",
    "known_adapter_kinds",
    "list_adapters",
]
