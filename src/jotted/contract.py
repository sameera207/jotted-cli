"""The contract with wrappers (the desktop app, MCP, scripts): what `jotted --json` prints.

Every result is one envelope on stdout:

    {"v": 1, "ok": true, "data": {...}}
    {"v": 1, "ok": false, "error": {"code": "busy", "message": "...", "retry": true}}

`code` is for programs and `message` for people. Each code has its own exit status, so a
script can branch without parsing anything. Fields are only added within a version;
anything removed or renamed bumps `CONTRACT`, after a release that still accepts the old
one (`CONTRACT_MIN`).

This module is light on purpose: the CLI imports it before deciding whether to do the
work in this process or hand it to a running `jotted serve`.
"""

from __future__ import annotations

import os
import sys
from typing import Any

CONTRACT = 1  # the "v" in every output
CONTRACT_MIN = 1  # the oldest contract version this build still accepts

# code -> exit status
EXIT = {
    "invalid": 1,
    "not_found": 1,
    "conflict": 1,
    "internal": 1,  # a bug: the traceback goes to stderr
    "usage": 2,
    "config": 2,
    "not_set_up": 3,
    "busy": 4,
    "not_connected": 5,
    "model_error": 6,
    "interrupted": 130,
}


class Error(Exception):
    """An error raised with its contract code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class UsageError(Error):
    """Bad arguments, or a prompt that can't be shown (stdin isn't a terminal, or --json)."""

    def __init__(self, message: str):
        super().__init__("usage", message)


class Failed(Exception):
    """An error envelope that came back from a running `jotted serve`, passed on as it is."""

    def __init__(self, envelope: dict):
        super().__init__(envelope["error"]["message"])
        self.envelope = envelope


def release() -> str:
    """This build's release version (0.4.0)."""
    from importlib import metadata

    try:
        return metadata.version("jotted")
    except metadata.PackageNotFoundError:
        return "0+unknown"


BUNDLED_VAR = "JOTTED_BUNDLED"  # set by an app that ships its own copy of jotted


def bundled() -> bool:
    """Whether this copy belongs to an app, which updates it: the app says so, or this is a
    standalone build (frozen Python), which `uv` can't upgrade."""
    return bool(os.environ.get(BUNDLED_VAR)) or bool(getattr(sys, "frozen", False))


def ok(data: Any) -> dict:
    return {"v": CONTRACT, "ok": True, "data": data}


def fail(code: str, message: str, **extra: Any) -> dict:
    return {"v": CONTRACT, "ok": False, "error": {"code": code, "message": message, **extra}}


def exit_code(envelope: dict) -> int:
    return 0 if envelope.get("ok") else EXIT.get(envelope["error"]["code"], 1)


def error_of(e: BaseException) -> dict:
    """The envelope for an exception: from its `code` attribute, or by its type."""
    from .config import ConfigError
    from .core.ports import SourceError
    from .llm import ModelError
    from .locking import Busy
    from .plugins import PluginError

    if isinstance(e, Failed):
        return e.envelope
    message = str(e) or type(e).__name__
    code = getattr(e, "code", None)
    if isinstance(code, str) and code in EXIT:
        extra = {k: getattr(e, k) for k in ("retry", "step") if getattr(e, k, None) is not None}
        return fail(code, message, **extra)
    if isinstance(e, KeyboardInterrupt):
        return fail("interrupted", "Stopped.")
    if isinstance(e, Busy):
        return fail("busy", message, retry=True)
    if isinstance(e, (ConfigError, PluginError)):
        return fail("config", message)
    if isinstance(e, SourceError):
        return fail("not_connected", message)
    if isinstance(e, ModelError):
        return fail("model_error", message)
    if isinstance(e, FileNotFoundError):
        return fail("not_found", message)
    return fail("internal", f"Unexpected error: {message}")

