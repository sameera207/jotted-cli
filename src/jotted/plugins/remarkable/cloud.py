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
import re
import shutil
import subprocess
import tempfile
import time
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


# How rmapi (Go) words a network failure that may pass if tried again, and what to tell the person.
TRANSIENT = [
    (re.compile(r"connection reset|broken pipe|unexpected EOF|\bEOF\b|TLS handshake|connection refused|"
                r"server closed|stream error", re.I),
     "the connection to reMarkable cloud dropped"),
    (re.compile(r"timeout|timed out|deadline exceeded", re.I), "reMarkable cloud didn't answer in time"),
    (re.compile(r"no such host|temporary failure in name resolution|network is unreachable|no route to host", re.I),
     "couldn't reach reMarkable cloud (is this computer online?)"),
    (re.compile(r"status\D{0,8}50[0234]\b|bad gateway|service unavailable|gateway timeout|internal server error", re.I),
     "reMarkable cloud had a problem on its side"),
]
RETRY_DELAYS_S = (2, 5)  # two retries, for read-only calls; see _run


def _transient(text: str) -> str | None:
    """The person's words for a network failure worth retrying; None for any other failure."""
    return next((said for pattern, said in TRANSIENT if pattern.search(text)), None)


def _plain(text: str) -> str:
    """rmapi's last error line without its log prefix ("ERROR: 2026/10/11 11:09:04 main.go:86: Error: ")."""
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    line = lines[-1] if lines else "no detail"
    line = re.sub(r"^(ERROR|WARNING|INFO):\s*(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d\s*)?(\S+\.go:\d+:\s*)?", "", line)
    line = re.sub(r"^Error:\s*", "", line)
    line = line.rstrip(" .")
    return line if len(line) <= 200 else line[:197] + "..."


def _run(cfg: Config, args: list[str], what: str, *, cwd: Path | None = None, stdin: str | None = None,
         retry: bool = False) -> str:
    """Run rmapi. `what` is what it was for, as in "Couldn't <what>" ("list your documents").

    retry=True (read-only calls) tries again after a network failure, up to twice, but stops at
    the second time rmapi.timeout_s runs out: a worst case of two timeouts and a few seconds, not
    three. Writes are not retried: a reset upload may have landed. Messages are short, for people;
    rmapi's own output goes to the log (INFO when it fails, the full stderr at DEBUG)."""
    cmd = [_binary(cfg), *args]
    timeouts = 0
    for attempt in range(len(RETRY_DELAYS_S) + 1):
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
        except subprocess.TimeoutExpired:
            timeouts += 1
            raw = f"timed out after {cfg.rmapi.timeout_s}s"
            said = "reMarkable cloud didn't answer in time"
        else:
            if proc.stderr.strip():
                log.debug("rmapi stderr:\n%s", proc.stderr.strip())
            if proc.returncode == 0:
                return proc.stdout
            raw = proc.stderr.strip() or proc.stdout.strip()
            said = _transient(raw)
        log.info("rmapi %s failed (attempt %d): %s", " ".join(args), attempt + 1, raw)
        if said is None:  # not a network failure: trying again won't help
            hint = "" if cfg.rmapi.token_file.exists() else " No token file yet: run `jotted connect` first."
            raise CloudError(f"Couldn't {what}: {_plain(raw)}.{hint}")
        if not retry or attempt == len(RETRY_DELAYS_S) or timeouts == 2:
            raise CloudError(f"Couldn't {what}: {said}.")
        time.sleep(RETRY_DELAYS_S[attempt])
    raise AssertionError("unreachable")


def register(cfg: Config, code: str) -> None:
    """Register rmapi as a desktop app with a one-time code; stores the token."""
    code = code.strip()
    if len(code) != 8:
        raise CloudError("the one-time code should be 8 characters")
    ensure_secrets_dir(cfg)
    # Any online command triggers registration when the token file is missing;
    # rmapi reads the code from stdin. `ls /` is read-only.
    _run(cfg, ["ls", "/"], "connect", stdin=code + "\n")
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
    nodes = _parse_json_list(_run(cfg, ["-ni", "-json", "find", "/"], "list your documents", retry=True))
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
    nodes = _parse_json_list(_run(cfg, ["-ni", "-json", "find", folder], "list your documents", retry=True))
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
        _run(cfg, ["-ni", "get", "--id", doc.id], f"download {doc.name}", cwd=root, retry=True)
        files = [p for p in root.rglob("*") if p.is_file()]
        if len(files) != 1:
            raise CloudError(f"Couldn't download {doc.name}: expected one downloaded file, found {[str(p.relative_to(root)) for p in files]}")
        dest = cache / f"{doc.id}.rmdoc"
        shutil.move(files[0], dest)
    sidecar = cache / f"{doc.id}.json"
    sidecar.write_text(json.dumps(asdict(doc), indent=2))
    return dest


def delete(cfg: Config, name: str, folder: str = "/") -> None:
    """Delete the document called `name` in `folder`."""
    _run(cfg, ["-ni", "rm", folder.rstrip("/") + "/" + name], f"delete {name}")


def upload_pdf(cfg: Config, pdf: Path, *, content_only: bool, folder: str = "/") -> None:
    """Upload a PDF into `folder`; the document is named after the file.

    content_only=True swaps only the PDF inside an existing document: its page list
    and handwriting (.rm files) are left as they are. Without it, a new document is
    created and rmapi refuses if one with that name already exists.
    """
    if not cfg.rmapi.token_file.exists():
        raise NotConnected(cfg)
    args = ["-ni", "put"] + (["--content-only"] if content_only else []) + [str(pdf), folder]
    out = _run(cfg, args, f"upload {pdf.stem}")
    log.debug("rmapi put: %s", out.strip())
