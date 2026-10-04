"""`jotted claude connect | status | disconnect`: Claude Desktop runs `jotted mcp`.

Claude Desktop reads its MCP servers from `claude_desktop_config.json`. Only the `jotted`
entry under `mcpServers` is ever written; every other key and server is left as it was. The
file is backed up before each change and replaced in one step (a temporary file, then a
rename), and a file that isn't valid JSON is never touched.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from .. import config
from ..contract import BUNDLED_VAR, Error, bundled

NAME = "jotted"  # the key under mcpServers
PATH_VAR = "JOTTED_CLAUDE_CONFIG"  # use another config file (tests, a second profile)


def config_path() -> Path:
    if os.environ.get(PATH_VAR):
        return Path(os.environ[PATH_VAR]).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Claude" / \
            "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def current_command() -> str | None:
    """The absolute path of the `jotted` being run, or on the PATH; None if neither is found."""
    here = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if here and here.stem == "jotted" and here.is_file():
        return str(here.absolute())
    found = shutil.which("jotted")
    return str(Path(found).absolute()) if found else None


def _executable(path: str) -> bool:
    return Path(path).is_file() and os.access(path, os.X_OK)


def _same(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return a == b


def _read(path: Path) -> dict | None:
    """The config, {} if Claude Desktop hasn't written one yet, None if Claude Desktop isn't here."""
    if not path.is_file():
        return {} if path.parent.is_dir() else None
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError) as e:
        raise Error("config", f"{path} isn't valid JSON, so it was left alone: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise Error("config", f"{path} doesn't look like Claude Desktop's settings, so it was left alone")
    return data


def _write(path: Path, data: dict) -> str | None:
    """Back up the file (if there is one), then replace it in one step. Returns the backup's path."""
    backup = None
    if path.is_file():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        n = 1
        while backup.exists():  # two changes in one second: keep both backups
            n += 1
            backup = path.with_name(f"{path.name}.bak-{stamp}-{n}")
        shutil.copy2(path, backup)
    tmp = path.with_name(f".{path.name}.jotted-tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if path.is_file():
        shutil.copymode(path, tmp)  # it may be readable by its owner only
    os.replace(tmp, path)
    return str(backup) if backup else None


def entry(command: str, admin: bool = False) -> dict:
    """The mcpServers entry. Claude Desktop starts `jotted mcp` without this environment, so
    what decides which data it uses (JOTTED_CONFIG, JOTTED_HOME) and whether it is an app's
    bundled copy (JOTTED_BUNDLED) goes with it, when set."""
    out: dict = {"command": command, "args": ["mcp", "--admin"] if admin else ["mcp"]}
    env = {name: str(Path(os.environ[name]).expanduser().absolute())
           for name in (config.ENV_VAR, config.HOME_VAR) if os.environ.get(name)}
    if bundled():
        env[BUNDLED_VAR] = os.environ.get(BUNDLED_VAR) or "1"
    if env:
        out["env"] = env
    return out


def connect(command: str | None = None, admin: bool = False, dry_run: bool = False) -> dict:
    if command:
        command = str(Path(command).expanduser().absolute())
        if not _executable(command):
            raise Error("invalid", f"{command} isn't an executable file")
    else:
        command = current_command()
        if not command:
            raise Error("invalid", "Can't find the jotted command; pass its path with --command")
    path = config_path()
    data = _read(path)
    if data is None:
        raise Error("config", f"Claude Desktop's settings folder isn't there ({path.parent}). Install Claude "
                              "Desktop and open it once, then try again.")
    want = entry(command, admin)
    servers = data.get("mcpServers", {})
    changed = servers.get(NAME) != want
    backup = None
    if changed and not dry_run:
        backup = _write(path, {**data, "mcpServers": {**servers, NAME: want}})
    return {"changed": changed, "config_path": str(path), "backup_path": backup,
            "restart_required": changed, "entry": want, "dry_run": dry_run}


def status() -> dict:
    path = config_path()
    data = _read(path) if path.is_file() else None
    found = (data or {}).get("mcpServers", {}).get(NAME)
    command = found.get("command") if isinstance(found, dict) else None
    env = found.get("env") if isinstance(found, dict) else None
    return {"configured": found is not None, "config_path": str(path), "command": command,
            "command_exists": bool(command) and _executable(command),
            "matches_current": _same(command, current_command()),
            "admin": isinstance(found, dict) and "--admin" in (found.get("args") or []),
            "installed": path.parent.is_dir(),  # Claude Desktop's settings folder: it has been run here
            "env": env if isinstance(env, dict) else {}}


def disconnect() -> dict:
    path = config_path()
    data = _read(path) if path.is_file() else None
    servers = (data or {}).get("mcpServers", {})
    if NAME not in servers:
        return {"changed": False, "config_path": str(path), "backup_path": None, "restart_required": False}
    backup = _write(path, {**data, "mcpServers": {k: v for k, v in servers.items() if k != NAME}})
    return {"changed": True, "config_path": str(path), "backup_path": backup, "restart_required": True}
