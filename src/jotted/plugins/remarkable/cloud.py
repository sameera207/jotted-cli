"""rmapi wrapper. All reMarkable Cloud access goes through this module.

rmapi is driven entirely from config.toml: each call gets RMAPI_CONFIG (the
token file) and, when enabled, RMAPI_TRACE. rmapi never reads ~/.rmapi.
Jotted holds its source lock around cloud work (`app.App.lock`): two rmapi processes
at once block each other.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from ...config import Config
from ...core.ports import SourceError

log = logging.getLogger(__name__)


class CloudError(SourceError):
    pass


class NotConnected(CloudError):
    """No rmapi token: setup isn't finished, so apps send the person to Connect."""
    code = "not_set_up"
    step = "remarkable.connect"

    def __init__(self, cfg: Config):
        super().__init__(f"Your reMarkable isn't connected yet (no rmapi token at {cfg.rmapi.token_file}). "
                         "Run `jotted connect`")


@dataclass(frozen=True)
class DocRef:
    id: str
    name: str
    version: int
    modified: str
    parent: str


def ensure_secrets_dir(cfg: Config) -> None:
    for d in {cfg.paths.secrets_dir, cfg.rmapi.token_file.parent}:
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)


def _binary(cfg: Config) -> str:
    b = cfg.rmapi.binary
    found = shutil.which(b)
    if not found:
        raise CloudError(f"rmapi binary not found: {b!r}. Install the ddvk fork or set rmapi.binary to its absolute path")
    return found


def _env(cfg: Config) -> dict[str, str]:
    env = dict(os.environ)
    env["RMAPI_CONFIG"] = str(cfg.rmapi.token_file)
    env.pop("RMAPI_TRACE", None)
    if cfg.rmapi.trace:
        env["RMAPI_TRACE"] = "1"
    return env


def _run(cfg: Config, args: list[str], *, cwd: Path | None = None, stdin: str | None = None) -> str:
    cmd = [_binary(cfg), *args]
    log.debug("running %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=_env(cfg),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=cfg.rmapi.timeout_s,
        )
    except subprocess.TimeoutExpired as e:
        raise CloudError(f"rmapi timed out after {cfg.rmapi.timeout_s}s: {' '.join(args)}") from e
    if proc.stderr.strip():
        log.debug("rmapi stderr:\n%s", proc.stderr.strip())
    if proc.returncode != 0:
        detail = (proc.stderr.strip() or proc.stdout.strip()).splitlines()[-5:]
        hint = ""
        if not cfg.rmapi.token_file.exists():
            hint = "\nNo token file yet: run `jotted connect` first."
        raise CloudError(f"rmapi {' '.join(args)} failed (exit {proc.returncode}):\n" + "\n".join(detail) + hint)
    return proc.stdout


def register(cfg: Config, code: str) -> None:
    """Register rmapi as a desktop app with a one-time code; stores the token."""
    code = code.strip()
    if len(code) != 8:
        raise CloudError("the one-time code should be 8 characters")
    ensure_secrets_dir(cfg)
    # Any online command triggers registration when the token file is missing;
    # rmapi reads the code from stdin. `ls /` is read-only.
    _run(cfg, ["ls", "/"], stdin=code + "\n")
    if not cfg.rmapi.token_file.exists():
        raise CloudError(f"rmapi finished but no token was written to {cfg.rmapi.token_file}")
    os.chmod(cfg.rmapi.token_file, 0o600)


def _parse_json_list(out: str) -> list[dict]:
    start, end = out.find("["), out.rfind("]")
    if start < 0 or end < start:
        raise CloudError("rmapi did not return JSON; is it the ddvk fork, v0.0.30 or newer?\n" + out[:500])
    try:
        return json.loads(out[start : end + 1])
    except json.JSONDecodeError as e:
        raise CloudError(f"could not parse rmapi JSON output: {e}") from e


@dataclass(frozen=True)
class LibraryEntry:
    """A document in the library, with its folder path ("/" for the root)."""
    doc: DocRef
    folder: str

    @property
    def path(self) -> str:
        return (self.folder.rstrip("/") + "/" + self.doc.name) if self.folder != "/" else "/" + self.doc.name


def _ref(n: dict) -> DocRef:
    return DocRef(
        id=n["id"],
        name=n["name"],
        version=int(n.get("version") or 0),
        modified=n.get("modifiedClient", ""),
        parent=n.get("parent", ""),
    )


def library(cfg: Config) -> tuple[list[LibraryEntry], list[str]]:
    """Every document (with its folder path) and every folder path in the library. Trash excluded."""
    if not cfg.rmapi.token_file.exists():
        raise NotConnected(cfg)
    nodes = _parse_json_list(_run(cfg, ["-ni", "-json", "find", "/"]))
    folders = {n["id"]: n for n in nodes
               if n.get("type") == "CollectionType" and n.get("id") and n["id"] != "trash" and n.get("parent") != "trash"}

    def folder_path(fid: str, depth: int = 0) -> str:
        if not fid or fid not in folders or depth > 50:
            return "/"
        parent = folder_path(folders[fid].get("parent", ""), depth + 1)
        return (parent.rstrip("/") + "/" + folders[fid]["name"]) if parent != "/" else "/" + folders[fid]["name"]

    docs = [LibraryEntry(_ref(n), folder_path(n.get("parent", "")))
            for n in nodes if n.get("type") == "DocumentType" and n.get("parent") != "trash"]
    return docs, sorted(folder_path(fid) for fid in folders)


def find_documents(cfg: Config, name: str, folder: str = "/") -> list[DocRef]:
    """Every document called `name` under `folder` (searched recursively)."""
    nodes = _parse_json_list(_run(cfg, ["-ni", "-json", "find", folder]))
    return [_ref(n) for n in nodes if n.get("name") == name and n.get("type") == "DocumentType"]


def find_document(cfg: Config, name: str, folder: str = "/") -> DocRef | None:
    """The document called `name` under `folder` (searched recursively); None if absent."""
    matches = find_documents(cfg, name, folder)
    if len(matches) > 1:
        ids = ", ".join(m.id for m in matches)
        raise CloudError(f"{len(matches)} documents named {name!r} ({ids}); narrow the folder")
    return matches[0] if matches else None


def download(cfg: Config, doc: DocRef) -> Path:
    """Download the document into the cache as <id>.rmdoc, with a <id>.json sidecar."""
    cache = cfg.paths.cache_dir
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache, prefix=".dl-") as tmp:
        root = Path(tmp).resolve()
        # rmapi saves to "<name>.rmdoc" in its working folder, so a "/" in the name ("Ana / Sameera")
        # reads as a subfolder: make it first, or the download fails. Only ever inside `root`.
        target = (root / (doc.name.lstrip("/") + ".rmdoc")).resolve()
        if target.parent != root and root in target.parents:
            target.parent.mkdir(parents=True, exist_ok=True)
        _run(cfg, ["-ni", "get", "--id", doc.id], cwd=root)
        files = [p for p in root.rglob("*") if p.is_file()]
        if len(files) != 1:
            raise CloudError(f"expected one downloaded file, found {[str(p.relative_to(root)) for p in files]}")
        dest = cache / f"{doc.id}.rmdoc"
        shutil.move(files[0], dest)
    sidecar = cache / f"{doc.id}.json"
    sidecar.write_text(json.dumps(asdict(doc), indent=2))
    return dest


def delete(cfg: Config, name: str, folder: str = "/") -> None:
    """Delete the document called `name` in `folder`."""
    _run(cfg, ["-ni", "rm", folder.rstrip("/") + "/" + name])


def upload_pdf(cfg: Config, pdf: Path, *, content_only: bool, folder: str = "/") -> None:
    """Upload a PDF into `folder`; the document is named after the file.

    content_only=True swaps only the PDF inside an existing document: its page list
    and handwriting (.rm files) are left as they are. Without it, a new document is
    created and rmapi refuses if one with that name already exists.
    """
    if not cfg.rmapi.token_file.exists():
        raise NotConnected(cfg)
    args = ["-ni", "put"] + (["--content-only"] if content_only else []) + [str(pdf), folder]
    out = _run(cfg, args)
    log.debug("rmapi put: %s", out.strip())
