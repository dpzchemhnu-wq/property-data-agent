"""Adapter and provenance marker for the upstream NCKU GESE Aspen Co-pilot."""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

REPOSITORY = "https://github.com/nckugese/Aspen_Co-pilot"
AGENT_NAME = "NCKU GESE Aspen Co-pilot / aspen-mcp"
AGENT_REF = os.getenv("ASPEN_COPILOT_COMMIT", "e705d9fba0dd5424f8dcf7afb99285e355b7abd9")
_loaded: tuple[Any, Any] | None = None
_manager: Any | None = None
_sessions: dict[str, str] = {}


def load() -> tuple[Any, Any] | None:
    global _loaded
    if _loaded is not None:
        return _loaded
    spec = importlib.util.find_spec("aspen_mcp")
    if spec is None:
        return None
    locations = list(spec.submodule_search_locations or [])
    package = Path(locations[0]) if locations else None
    if package is None:
        return None
    if str(package) not in sys.path:
        sys.path.insert(0, str(package))
    try:
        # The published wheel deliberately ships the upstream flat modules inside
        # ``aspen_mcp`` and its entry point puts that directory on sys.path. Keep
        # the same import contract here, while accepting a normal package layout
        # if the upstream project changes it later.
        manager_module = importlib.import_module("aspen_manager")
        server_module = importlib.import_module("server")
    except Exception:
        try:
            manager_module = importlib.import_module("aspen_mcp.aspen_manager")
            server_module = importlib.import_module("aspen_mcp.server")
        except Exception:
            return None
    if not callable(getattr(manager_module, "running_documents", None)):
        return None
    if not callable(getattr(manager_module, "AspenPlusManager", None)):
        return None
    _loaded = (manager_module, server_module)
    return _loaded


def available() -> bool:
    return load() is not None


def manager() -> Any | None:
    global _manager
    modules = load()
    if modules is None:
        return None
    if _manager is None:
        _manager = modules[0].AspenPlusManager()
    return _manager


def running_documents() -> list[dict[str, Any]]:
    modules = load()
    if modules is None:
        return []
    return list(modules[0].running_documents())


def discover_documents() -> list[dict[str, Any]]:
    return [item for item in running_documents() if item.get("path")]


def attach(path: str, pid: int | None = None) -> str | None:
    mgr = manager()
    if mgr is None:
        return None
    key = os.path.normcase(os.path.abspath(path))
    if key in _sessions:
        return _sessions[key]
    session = f"{Path(path).stem[:48]}-{pid or 'session'}"
    result = mgr.attach(path, session_name=session, pid=pid)
    if result is None:
        return None
    _sessions[key] = session
    return session


def app_for(path: str, pid: int | None = None) -> Any | None:
    session = attach(path, pid=pid)
    mgr = manager()
    if not session or mgr is None:
        return None
    return mgr.get_app(session)


def status() -> dict[str, Any]:
    return {"available": available(), "name": AGENT_NAME, "repository": REPOSITORY, "ref": AGENT_REF, "message": "网页侧使用上游 Aspen Co-pilot 的 Running Object Table 发现逻辑；网页工具仍限制为只读。"}


__all__ = ["AGENT_NAME", "REPOSITORY", "AGENT_REF", "available", "running_documents", "discover_documents", "attach", "app_for", "status"]
