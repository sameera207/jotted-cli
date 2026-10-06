"""`jotted schema`: every command, its arguments and options, and the shape of its `data`.

Commands and arguments come from the CLI's own parser and operations from `api.OPERATIONS`,
so they can't drift. The shapes of `data` are declared here (JSON Schema), and a test checks
real outputs against them. Shapes list the fields a wrapper can rely on; objects may carry
more (fields are only ever added within a contract version).
"""

from __future__ import annotations

import argparse
from typing import Any

from . import contract


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": list(props) if required is None else required}


def _arr(items: dict) -> dict:
    return {"type": "array", "items": items}


STR, INT, NUM, BOOL = {"type": "string"}, {"type": "integer"}, {"type": "number"}, {"type": "boolean"}
NSTR = {"type": ["string", "null"]}
ANY = {}

NINT = {"type": ["integer", "null"]}
ITEM = _obj({
    "id": INT, "origin": STR, "text": STR, "paper_text": STR, "written": BOOL,
    "status": {"enum": ["open", "done", "proposed", "dismissed"]},
    "owner": {"enum": ["me", "someone_else", "unclear"]}, "owner_name": NSTR, "p_action": NUM,
    "source": _obj({"doc_id": STR, "name": STR, "folder": STR, "page": INT, "anchor": STR,
                    "kind": NSTR, "key": NSTR, "title": NSTR, "url": NSTR, "excerpt": NSTR}),
    "page": {"oneOf": [{"type": "null"}, _obj({"doc_id": STR, "doc_name": STR, "page": INT, "page_count": NINT,
                                               "anchor": STR})]},
    "edited": BOOL, "slot": NINT, "created_at": STR,
})
ADDED = {**ITEM, "properties": {**ITEM["properties"], "created": BOOL}, "required": [*ITEM["required"], "created"],
         "description": "the item; `created` false when its source key was added before (nothing new was added)"}
CLAUDE_CHANGE = _obj({"changed": BOOL, "config_path": STR, "backup_path": NSTR, "restart_required": BOOL,
                      "entry": {"type": "object"}, "dry_run": BOOL},
                     required=["changed", "config_path", "backup_path", "restart_required"])
SETTINGS = _obj({
    "watch": _arr(STR), "action_threshold": NUM, "poll_interval_s": INT, "include_others": BOOL,
    "todo_enabled": BOOL, "todo_name": STR, "todo_folder": STR, "from_now": _arr(STR),
    "mcp_add_mode": {"enum": ["auto", "propose_all"]}, "proposed_limit": INT,
})
KEY = _obj({"set": BOOL, "source": {"enum": ["saved", "environment", None]}, "hint": NSTR})
AI = _obj({
    "llm": _obj({"provider": STR, "label": STR, "family": STR, "model": STR, "key_url": STR, "key": KEY,
                 "providers": _arr(_obj({"id": STR, "label": STR}))}),
    "jev": _obj({"name": STR, "by": STR, "model": STR, "key_url": STR, "key": KEY, "enabled": BOOL}),
    "judge": {"enum": ["llm", "jev"]},
})
SOURCE = _obj({"name": STR, "label": STR, "mark": STR, "device": STR, "connected": BOOL, "detail": STR})
STEPS = _obj({"complete": BOOL, "steps": _arr(_obj({
    "id": STR, "title": STR, "done": BOOL, "optional": BOOL, "command": STR, "detail": STR},
    required=["id", "title", "done"]))})
EVENT = _obj({"cursor": INT, "at": STR, "type": {"enum": [
    "item.added", "item.changed", "item.removed", "item.proposed", "item.accepted", "check.started", "check.finished", "todo.published",
    "settings.changed", "source.error"]}}, required=["cursor", "at", "type"])
SVG = {"oneOf": [_obj({"svg": STR}), _obj({"path": STR})]}

# command (as typed, without its arguments) -> the shape of `data`
DATA: dict[str, dict] = {
    "setup": {"description": "interactive; no JSON output"},
    "setup status": STEPS,
    "setup prepare": {**STEPS, "properties": {**STEPS["properties"], "prepared": _arr(_obj({"id": STR, "detail": NSTR}))},
                      "required": ["prepared", "complete", "steps"]},
    "connect": _obj({"connected": BOOL, "replaced": BOOL, "documents": INT}, required=["connected"]),
    "auth": _obj({"connected": BOOL, "replaced": BOOL, "documents": INT}, required=["connected"]),
    "ai": AI, "ai key": AI, "ai remove": AI, "ai provider": AI, "ai model": AI,
    "plugins": _obj({"chosen": NSTR, "installed": _arr(_obj({"name": STR, "label": STR, "module": STR,
                                                             "chosen": BOOL}, required=["name", "module", "chosen"]))}),
    "items": _arr(ITEM), "items list": _arr(ITEM), "items get": ITEM, "items add": ADDED,
    "items add-batch": _obj({"results": _arr(_obj({
        "index": INT, "outcome": {"enum": ["created", "existing", "dismissed", "invalid", "conflict", "internal"]},
        "id": INT, "error": _obj({"code": STR, "message": STR})}, required=["index", "outcome"]))}),
    "items accept": _obj({"accepted": _arr(INT), "skipped": _arr(_obj({
        "id": INT, "reason": {"enum": ["not_found", "not_proposed"]}}))}),
    "items edit": ITEM, "items done": ITEM,
    "items reopen": ITEM, "items dismiss": {"type": "object", "description": "empty: the item is gone"},
    "library": _obj({
        "folders": _arr(_obj({"path": STR, "documents": INT, "watched": BOOL})),
        "documents": _arr(_obj({"path": STR, "folder": STR, "id": STR, "own": BOOL, "read": BOOL,
                                "baseline_pages": INT, "watched": BOOL}))}),
    "watch": {"oneOf": [SETTINGS, _obj({"settings": SETTINGS, "documents": _arr(STR), "already_read": _arr(STR)})],
              "description": "add/remove: the settings; from-now/read-all: the documents affected"},
    "settings": SETTINGS, "settings set": SETTINGS,
    "collect": {"oneOf": [
        _obj({"docs_seen": INT, "docs_changed": INT, "pages_read": INT, "pages_skipped": INT, "pages_baselined": INT,
              "lines_judged": INT, "actions_new": INT, "actions_updated": INT, "actions_missing": INT,
              "errors": _arr(STR)}),
        _arr(_obj({"path": STR, "id": STR, "pages": _arr(INT)}))], "description": "--dry-run: the changed pages"},
    "todo": {**_obj({"enabled": BOOL, "unsupported": BOOL, "ticked": INT, "written": INT, "published": BOOL,
                     "items": INT, "overflow": INT, "rebuilt": BOOL}, required=[]),
             "description": "With --fresh the document is deleted and printed again from the top with open "
                            "items only, and rebuilt is true; if there was no document yet, a new one is just "
                            "published (rebuilt false)"},
    "check": _obj({"collect": ANY, "todo": ANY}, required=[]),
    "status": _obj({"source": SOURCE, "judge": STR, "watch": _arr(STR), "documents_read": INT,
                    "last_collected_at": NSTR, "items": _obj({"open": INT, "done": INT, "proposed": INT}),
                    "todo": _obj({"enabled": BOOL, "name": STR, "folder": STR, "published_at": NSTR}),
                    "background": ANY}),
    "image page": SVG, "image line": SVG,
    "events": {**_obj({"cursor": INT, "events": _arr(EVENT)}),
               "description": "--follow prints one event per line instead, each with \"v\""},
    "serve": {"description": "runs until stopped; no JSON output"},
    "start": {"description": "interactive; no JSON output"},
    "update": _obj({"updated": BOOL}),
    "version": _obj({"version": STR, "contract": INT, "contract_min": INT, "source_plugin": NSTR, "python": STR,
                     "bundled": BOOL}),
    "schema": {"type": "object", "description": "this document"},
    "mcp": {"description": "an MCP server on stdio; no JSON output"},
    "claude connect": CLAUDE_CHANGE, "claude disconnect": CLAUDE_CHANGE,
    "claude status": _obj({"configured": BOOL, "config_path": STR, "command": NSTR, "command_exists": BOOL,
                           "matches_current": BOOL, "admin": BOOL, "installed": BOOL,
                           "env": {"type": "object", "description": "the entry's environment variables"}}),
    "config check": _obj({"path": STR, "config": {"type": "object"}, "source": SOURCE, "ai": AI}),
}


def _type(action: argparse.Action) -> dict:
    if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
        return {"type": "boolean"}
    if action.choices:
        return {"enum": list(action.choices)}
    if action.type is int:
        return {"type": "integer"}
    if action.type is float:
        return {"type": "number"}
    return {"type": "string"}


def _arguments(parser: argparse.ArgumentParser) -> list[dict]:
    out = []
    for a in parser._actions:
        if isinstance(a, (argparse._HelpAction, argparse._SubParsersAction)) or a.help == argparse.SUPPRESS:
            continue
        arg: dict[str, Any] = {"name": a.dest, **_type(a)}
        if a.option_strings:
            arg["flags"] = a.option_strings
            if a.default not in (None, False, argparse.SUPPRESS):
                arg["default"] = a.default
        else:
            arg["positional"] = True
        if a.nargs in ("+", "*"):
            arg["many"] = True
        arg["required"] = bool(a.required) if a.option_strings else a.nargs not in ("?", "*")
        if a.help:
            arg["help"] = a.help
        out.append(arg)
    return out


def commands(parser: argparse.ArgumentParser, prefix: str = "", inherited: Any = None) -> list[dict]:
    """Every command in `parser`, depth first: a command with subcommands is listed too
    when it runs on its own (`items` lists, `ai` shows)."""
    found = []
    sub = next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
    helps = {c.dest: c.help for c in sub._choices_actions} if sub else {}
    for name, child in (sub.choices.items() if sub else []):
        path = f"{prefix} {name}".strip()
        grand = next((a for a in child._actions if isinstance(a, argparse._SubParsersAction)), None)
        func = child._defaults.get("func", inherited)  # `image page` runs `image`'s function
        if grand is None or (func is not None and not grand.required):
            entry: dict[str, Any] = {"command": path, "help": helps.get(name) or child.description or "",
                                     "arguments": _arguments(child),
                                     "operations": list(getattr(func, "operations", ())),
                                     "data": DATA.get(path, ANY)}
            if path.split()[0] not in helps and prefix == "":
                entry["hidden"] = True
            if child._defaults.get("deprecated"):
                entry["deprecated"] = f"use `{child._defaults['deprecated']}`"
            found.append(entry)
        if grand is not None:
            found.extend(commands(child, path, func))
    return found


def operations() -> dict[str, dict]:
    """The operation registry: what each takes (the fast path and MCP call these)."""
    import inspect

    from .api import OPERATIONS, Jotted

    out = {}
    for name, method in sorted(OPERATIONS.items()):
        fn = getattr(Jotted, method)
        params = {}
        for p in list(inspect.signature(fn).parameters.values())[1:]:
            if p.name in ("progress", "wait"):
                continue
            params[p.name] = {"type": str(p.annotation)} | ({} if p.default is p.empty else {"default": p.default})
        out[name] = {"params": params, "doc": inspect.getdoc(fn) or ""}
    return out


def build(parser: argparse.ArgumentParser) -> dict:
    return {
        "contract": contract.CONTRACT, "contract_min": contract.CONTRACT_MIN, "version": contract.release(),
        "envelope": {"ok": {"v": "integer", "ok": True, "data": "the command's data"},
                     "error": {"v": "integer", "ok": False, "error": {"code": "string", "message": "string",
                                                                     "retry": "boolean?", "step": "string?"}}},
        "errors": {code: {"exit": status} for code, status in contract.EXIT.items()},
        "global_options": _arguments(parser),
        "commands": commands(parser),
        "operations": operations(),
    }
