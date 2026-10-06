"""Jotted's application layer: every operation the product offers, in one place.

The CLI, the local web server and any other front end (a desktop app, say) are thin
callers of `Jotted`: they parse input, call one method, and show the result. Nothing
here knows about HTTP or the terminal, and nothing outside knows how an operation is
done. Results are plain dicts and lists, ready for JSON: that is the contract front
ends rely on.

Each public operation is registered in `OPERATIONS` with `@operation`; a test checks
that every one is reachable from the CLI, and every web route maps to one.

Errors are `ApiError`s with a message for the person, a `code` for programs (the CLI's
--json contract, `jotted.contract`) and an HTTP-like status.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from urllib.parse import urlsplit
from dataclasses import asdict, fields, replace
from typing import Any, Callable

from . import classify, keys, llm
from .app import SYNC_ERRORS, App, Scheduler
from .config import Config
from .core import service
from .core.model import DocInfo, Settings
from .core.ports import StateConflict
from .locking import Busy

log = logging.getLogger("jotted")

OPERATIONS: dict[str, str] = {}  # operation name -> method name

SOURCE_WAIT_S = 60  # how long a request waits for the source to be free
RECENT_EVENTS = 50  # `events` without a cursor
STATUSES = ("open", "done", "all", "proposed", "dismissed", "any")  # `all` is open and done: the list
OWNERS = {"mine": "me", "others": "someone_else"}
MAX_LIST = 200  # items in one page of `items`
MAX_BATCH = 100  # items in one `items.add_batch`
KIND = re.compile(r"[a-z0-9_-]{1,32}")
LIMITS = {"text": 500, "owner_name": 80, "key": 200, "title": 200, "excerpt": 500, "url": 2000}


class ApiError(Exception):
    """An operation couldn't be done. `message` is for the person; `code` is for programs
    (see `jotted.contract`); `status` is the HTTP status the web server answers with."""
    status = 400
    code = "invalid"


class Invalid(ApiError):
    status = 400
    code = "invalid"


class NotFound(ApiError):
    status = 404
    code = "not_found"


class Conflict(ApiError):
    status = 409
    code = "conflict"


class SourceBusy(Conflict):
    """Another job (maybe another Jotted process) holds the device."""
    code = "busy"
    retry = True


class NotSetUp(ApiError):
    """A setup step is missing; `step` names it (`jotted setup status`)."""
    status = 409
    code = "not_set_up"

    def __init__(self, message: str, step: str):
        super().__init__(message)
        self.step = step


class Unavailable(ApiError):
    """The source or a model failed, or couldn't be reached."""
    status = 502
    code = "not_connected"


class ModelFailed(Unavailable):
    """No key, a rejected key, or the model failed."""
    code = "model_error"


class KeyRejected(ModelFailed):
    status = 400


def operation(name: str) -> Callable:
    def register(fn: Callable) -> Callable:
        OPERATIONS[name] = fn.__name__
        fn.operation = name
        return fn
    return register


def _settings_from(changes: dict, current: Settings) -> Settings:
    """Validated settings: `current` with `changes` applied."""
    names = {f.name for f in fields(Settings)}
    values = asdict(current)
    for k, v in changes.items():
        if k not in names:
            raise Invalid(f"unknown setting {k!r}")
        values[k] = v
    try:
        s = Settings(**values)
        s.action_threshold = float(s.action_threshold)
        s.poll_interval_s = int(s.poll_interval_s)
    except (TypeError, ValueError) as e:
        raise Invalid(str(e)) from e
    if not 0 <= s.action_threshold <= 1:
        raise Invalid("action_threshold must be between 0 and 1")
    if s.poll_interval_s < 15:
        raise Invalid("poll_interval_s must be at least 15 seconds")
    if s.mcp_add_mode not in ("auto", "propose_all"):
        raise Invalid("mcp_add_mode must be auto or propose_all")
    if isinstance(s.proposed_limit, bool) or not isinstance(s.proposed_limit, int) or not 10 <= s.proposed_limit <= 500:
        raise Invalid("proposed_limit must be a whole number from 10 to 500")
    if not isinstance(s.watch, list) or not all(isinstance(w, str) for w in s.watch):
        raise Invalid("watch must be a list of folder or document paths")
    s.watch = sorted({_path(w) for w in s.watch})
    if not isinstance(s.from_now, list) or not all(isinstance(d, str) for d in s.from_now):
        raise Invalid("from_now must be a list of document IDs")
    s.from_now = sorted(set(s.from_now))
    for flag in ("include_others", "todo_enabled"):
        if not isinstance(getattr(s, flag), bool):
            raise Invalid(f"{flag} must be true or false")
    if not isinstance(s.todo_name, str) or not s.todo_name.strip():
        raise Invalid("todo_name is empty")
    s.todo_name = s.todo_name.strip()
    s.todo_folder = _path(s.todo_folder) if isinstance(s.todo_folder, str) else "/"
    return s


def _path(p: str) -> str:
    return "/" + p.strip("/") if p.strip("/") else "/"


def _plain(value: Any, field: str, *, required: bool = False) -> str | None:
    """Text from a person or an agent, as Jotted keeps it: one line, no control characters,
    within its length limit. It is only ever shown as text."""
    if value is None or value == "":
        if required:
            raise Invalid(f"{field} is empty")
        return None
    if not isinstance(value, str):
        raise Invalid(f"{field} must be text")
    text = " ".join("".join(" " if unicodedata.category(ch)[0] in "CZ" else ch for ch in value).split())
    if not text:
        if required:
            raise Invalid(f"{field} is empty")
        return None
    limit = LIMITS[field.split(".")[-1]]
    if len(text) > limit:
        raise Invalid(f"{field} is longer than {limit} characters")
    return text


def _source(source: Any) -> dict | None:
    """A validated `source` ({kind, key, title, url, excerpt}); None if there is none."""
    if source is None:
        return None
    if not isinstance(source, dict):
        raise Invalid("source must be an object")
    unknown = set(source) - {"kind", "key", "title", "url", "excerpt"}
    if unknown:
        raise Invalid(f"source has unknown field(s) {', '.join(sorted(unknown))}")
    kind = source.get("kind")
    if kind is not None and (not isinstance(kind, str) or not KIND.fullmatch(kind)):
        raise Invalid("source.kind must be 1 to 32 of a-z, 0-9, _ and -")
    out = {"kind": kind, **{f: _plain(source.get(f), f"source.{f}") for f in ("key", "title", "excerpt")}}
    url = source.get("url")
    if url not in (None, ""):
        if not isinstance(url, str) or len(url) > LIMITS["url"]:
            raise Invalid("source.url must be a link of at most 2000 characters")
        parts = urlsplit(url.strip())
        if parts.scheme != "https" or not parts.netloc or any(ord(ch) < 33 for ch in url.strip()):
            raise Invalid("source.url must be an https link")
        out["url"] = url.strip()
    else:
        out["url"] = None
    if out["key"] and not kind:
        raise Invalid("source.key needs source.kind")
    return out if any(out.values()) else None


def _owner(owner: Any, owner_name: Any) -> tuple[str | None, str | None]:
    """(owner as stored, owner_name) from `mine`/`others` and a name, which means `others`."""
    if owner not in (None, "", *OWNERS):
        raise Invalid("owner must be mine or others")
    name = _plain(owner_name, "owner_name")
    if name and owner == "mine":
        raise Invalid("owner_name is for items someone else owns (owner others)")
    if name or owner == "others":
        return "someone_else", name
    return ("me", "") if owner == "mine" else (None, None)


class Jotted:
    """The application. `on_change` runs after an edit that should reach the To-do document
    (the web server schedules an update; the CLI leaves it to `jotted todo` or the server)."""

    def __init__(self, app: App, scheduler: Scheduler | None = None, on_change: Callable[[], None] | None = None):
        self.app = app
        self.scheduler = scheduler
        self._on_change = on_change or (lambda: None)

    @property
    def cfg(self) -> Config:  # the App's, which `ai.provider`/`ai.model` replace in place
        return self.app.cfg

    @property
    def repo(self):
        return self.app.repo

    @classmethod
    def open(cls, cfg: Config, **kw: Any) -> "Jotted":
        return cls(App.build(cfg), **kw)

    def _source_job(self, what: str, fn: Callable[[], Any], wait: float | None = SOURCE_WAIT_S) -> Any:
        """Run `fn` holding the source lock; turn source and model failures into ApiErrors."""
        try:
            with self.app.lock.held(wait, what):
                self._require_source()
                return fn()
        except Busy as e:
            raise SourceBusy(str(e)) from e
        except llm.ModelError as e:
            raise ModelFailed(str(e)) from e
        except SYNC_ERRORS as e:
            if getattr(e, "code", None) == NotSetUp.code:  # the source found a step missing after all
                raise NotSetUp(str(e), step=e.step) from e
            raise Unavailable(str(e)) from e

    def _require_source(self) -> None:
        """NotSetUp, naming the step, if the source plugin's setup isn't finished."""
        for step in getattr(self.app.plugin, "setup_steps", lambda: [])():
            if not step.optional and not step.check(self.cfg)["done"]:
                raise NotSetUp(f"{step.title}: not set up yet. Run `jotted {step.command}`", step=step.id)

    # ------------------------------------------------------------ the to-do list

    @operation("items.list")
    def items(self, status: str | None = None, owner: str | None = None, folder: str | None = None,
              source_kind: str | None = None, source_key: str | None = None, query: str | None = None,
              limit: int | None = None, cursor: int | None = None) -> list[dict]:
        """The list (open and done items), or the items in `status`: proposed, dismissed, or any.
        With `limit`, one page: a full page may have more after it, from `cursor` = its last id."""
        if status not in (None, "", *STATUSES):
            raise Invalid("status must be " + ", ".join(STATUSES))
        if owner not in (None, "", *OWNERS):
            raise Invalid("owner must be mine or others")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIST):
            raise Invalid(f"limit must be from 1 to {MAX_LIST}")
        if cursor is not None and (isinstance(cursor, bool) or not isinstance(cursor, int)):
            raise Invalid("cursor must be the id of the last item already listed")
        try:
            return self.repo.items(status=None if status in (None, "", "all") else status, owner=owner or None,
                                   folder=folder or None, source_kind=source_kind or None,
                                   source_key=source_key or None, query=query or None, after=cursor, limit=limit)
        except KeyError:
            raise Invalid(f"no item {cursor} to continue after") from None

    @operation("items.get")
    def get_item(self, item_id: int) -> dict:
        """One item in any status, with where it came from."""
        try:
            return self.repo.item(item_id)
        except KeyError:
            raise NotFound(f"no item {item_id}") from None

    def _add(self, text: Any, owner: Any, owner_name: Any, source: Any, propose: bool, agent: bool) -> tuple[dict, str]:
        """One item added, or the one already there for its source key: (item, outcome)."""
        text = _plain(text, "text", required=True)
        owner, owner_name = _owner(owner, owner_name)
        source = _source(source)
        plugin = getattr(self.app.plugin, "NAME", None)
        agent = bool(agent) or bool(source and source["kind"] and source["kind"] != plugin)
        settings = self.repo.settings()
        proposed = bool(propose) or (agent and settings.mcp_add_mode == "propose_all")
        if propose and agent and not (source and source["key"]):
            raise Invalid("source.kind and source.key are required to propose an item: they say where it came "
                          "from, so it is proposed only once")
        try:
            item_id, outcome = self.repo.add_item(text, origin="agent" if agent else "web", owner=owner or "me",
                                                  owner_name=owner_name, source=source, proposed=proposed,
                                                  proposed_limit=settings.proposed_limit)
        except ValueError as e:
            raise Invalid(str(e)) from e
        except StateConflict as e:
            raise Conflict(str(e)) from e
        return self.repo.item(item_id, missing_too=True), outcome

    @operation("items.add")
    def add_item(self, text: str, owner: str | None = None, owner_name: str | None = None,
                 source: dict | None = None, propose: bool = False, agent: bool = False) -> dict:
        """Add an item; `propose` leaves it waiting for the person to accept it. With a `source`
        key seen before, nothing is added: the item already there comes back with `created`
        false (status dismissed if the person turned it down)."""
        item, outcome = self._add(text, owner, owner_name, source, propose, agent)
        if outcome != "existing":
            self._on_change()
        return {**item, "created": outcome == "created"}

    @operation("items.add_batch")
    def add_items(self, items: list, propose: bool = False, agent: bool = False) -> dict:
        """Up to 100 items, each as `items.add` takes it ({text, owner, owner_name, source, propose}).
        Each stands alone: one that fails doesn't stop the others. Returns an outcome per item:
        created, existing, dismissed, or invalid/conflict/internal with its error."""
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise Invalid("items must be a list of objects")
        if len(items) > MAX_BATCH:
            raise Invalid(f"at most {MAX_BATCH} items at a time")
        results = []
        for n, it in enumerate(items):
            unknown = set(it) - {"text", "owner", "owner_name", "source", "propose"}
            try:
                if unknown:
                    raise Invalid(f"unknown field(s) {', '.join(sorted(unknown))}")
                item, outcome = self._add(it.get("text"), it.get("owner"), it.get("owner_name"), it.get("source"),
                                          bool(propose or it.get("propose")), agent)
            except ApiError as e:
                results.append({"index": n, "outcome": e.code, "error": {"code": e.code, "message": str(e)}})
                continue
            except Exception as e:  # noqa: BLE001 - a bug in one item mustn't hide what happened to the others
                log.exception("items.add_batch: item %d failed", n)
                results.append({"index": n, "outcome": "internal",
                                "error": {"code": "internal", "message": f"Unexpected error: {e}"}})
                continue
            results.append({"index": n, "outcome": "existing" if outcome == "updated" else outcome, "id": item["id"]})
        if any(r["outcome"] == "created" for r in results):
            self._on_change()
        return {"results": results}

    @operation("items.accept")
    def accept_items(self, ids: list[int] | None = None, every: bool = False, source_kind: str | None = None) -> dict:
        """Proposed items onto the list, after the person has looked at them: `ids`, or `every`
        proposed one (of `source_kind`, if given). They reach the To-do document on its next update."""
        if every == bool(ids):
            raise Invalid("Name the items to accept, or accept them all")
        if ids and not (isinstance(ids, list) and all(isinstance(i, int) and not isinstance(i, bool) for i in ids)):
            raise Invalid("ids must be item ids")
        accepted, skipped = self.repo.accept(None if every else ids, source_kind or None)
        if ids and not accepted:
            reasons = {s["reason"] for s in skipped}
            if "not_proposed" in reasons:
                raise Conflict("Not proposed: " + ", ".join(f"#{s['id']}" for s in skipped
                                                          if s["reason"] == "not_proposed"))
            raise NotFound("no item " + ", ".join(str(s["id"]) for s in skipped))
        if accepted:
            self._on_change()
        return {"accepted": accepted, "skipped": skipped}

    @operation("items.edit")
    def edit_item(self, item_id: int, *, text: str | None = None, status: str | None = None,
                  dismissed: bool | None = None, owner: str | None = None, owner_name: str | None = None) -> dict:
        """Change an item's text, owner or status, or dismiss it (not an action, or not wanted).
        A dismissed item is kept, so its source never adds it again. Returns the item, or {} once
        dismissed."""
        text = _plain(text, "text")
        stored_owner, owner_name = _owner(owner, owner_name)
        try:
            item = self.repo.edit_action(item_id, text=text, status=status, dismissed=dismissed,
                                         owner=stored_owner, owner_name=owner_name)
        except KeyError:
            raise NotFound(f"no item {item_id}") from None
        except ValueError as e:
            raise Invalid(str(e)) from e
        except StateConflict as e:
            raise Conflict(str(e)) from e
        self._on_change()
        return item

    def overview(self, status: str | None = None, owner: str | None = None, folder: str | None = None) -> dict:
        """Everything the to-do page shows at once: `items` plus what explains them."""
        return {"items": self.items(status, owner, folder), "settings": self.settings(),
                "background": self.scheduler.describe() if self.scheduler else None,
                "todo": self.repo.todo_meta(), "sources": self.repo.source_docs(), "source": self.source()}

    # ------------------------------------------------------------ settings

    @operation("settings.get")
    def settings(self) -> dict:
        return asdict(self.repo.settings())

    @operation("settings.update")
    def update_settings(self, changes: dict) -> dict:
        if not isinstance(changes, dict):
            raise Invalid("settings must be an object")
        s = _settings_from(changes, self.repo.settings())
        self.repo.save_settings(s)
        if self.scheduler:
            self.scheduler.poll_now()  # pick up new folders without waiting for the next round
        return asdict(s)

    @operation("watch.add")
    def watch(self, path: str) -> dict:
        s = self.repo.settings()
        return self.update_settings({"watch": s.watch + [_path(path)]})

    @operation("watch.remove")
    def unwatch(self, path: str) -> dict:
        s = self.repo.settings()
        return self.update_settings({"watch": [w for w in s.watch if w != _path(path)]})

    @operation("watch.from_now")
    def from_now(self, path: str, on: bool = True) -> dict:
        """Skip (on) or read again (off) what is already written in the documents at `path`
        (a document, or every document in a folder now). Returns the settings, the documents
        affected, and those already read in full, where there is nothing left to skip."""
        target = _path(path)
        docs = [d for d in self._documents() if target == "/" or d.path == target or d.path.startswith(target + "/")]
        if not docs:
            raise NotFound(f"no documents at {target}")
        ids = {d.id for d in docs}
        s = self.repo.settings()
        late: list[str] = []
        if on:
            read = {d["id"] for d in self.repo.source_docs() if d["marker"]}
            late = [d.path for d in docs if d.id in read and d.id not in self.repo.baselined_docs()]
            settings = self.update_settings({"from_now": sorted(set(s.from_now) | ids)})
        else:
            settings = self.update_settings({"from_now": sorted(set(s.from_now) - ids)})
        return {"settings": settings, "documents": [d.path for d in docs], "already_read": late}

    # ------------------------------------------------------------ the source's library

    def _documents(self) -> list[DocInfo]:
        return self._source_job("The library", self.app.source.list_documents)

    @operation("library")
    def library(self) -> dict:
        """Folders and documents for the watch picker, with what each watch would cover."""
        docs, folders = self._source_job(
            "The library", lambda: (self.app.source.list_documents(), self.app.source.folders()))
        own = self.app.own_doc_ids()
        counts = {f: sum(1 for d in docs if (d.folder + "/").startswith(f.rstrip("/") + "/")) for f in folders}
        read = {d["id"] for d in self.repo.source_docs() if d["marker"]}  # fully collected at least once
        baseline = self.repo.baseline_pages()
        watched = self.repo.settings()
        return {
            "folders": [{"path": f, "documents": counts[f],
                         "watched": watched.watches(DocInfo("", "", "x", f, ""))} for f in folders],
            "documents": [{"path": d.path, "folder": d.folder, "id": d.id, "own": d.id in own,
                           "read": d.id in read, "baseline_pages": baseline.get(d.id, 0),
                           "watched": watched.watches(d)} for d in docs],
        }

    @operation("pending")
    def pending(self, fetch: bool = False) -> list[dict]:
        """What the next collection would read: changed documents and, with `fetch`, their
        changed pages (downloads only; nothing is read or judged)."""
        todo = self._source_job("The library", lambda: service.pending(
            self.app.source, self.repo, exclude=self.app.own_doc_ids(), fetch=fetch))
        return [{"path": d.path, "id": d.id, "pages": [p.index for p in pages]} for d, pages in todo]

    # ------------------------------------------------------------ work

    @operation("collect")
    def collect(self, progress: Callable[[str], None] = lambda _: None, wait: float | None = None) -> dict:
        """Read what changed in the watched documents now."""
        if not self.repo.settings().watch:
            raise Invalid("Nothing is watched yet. Watch a folder first (`jotted watch add PATH`, or Settings).")
        return self._source_job("The source", lambda: self.app.collect(progress=progress).as_dict(), wait)

    @operation("todo.sync")
    def sync_todo(self, force: bool = False, wait: float | None = None) -> dict:
        """Read ticks and new items from the To-do document, then republish it if needed."""
        if not self.repo.settings().todo_enabled:
            raise Invalid("The To-do document is off. Turn it on with `jotted settings set todo_enabled true` "
                          "or in Settings.")
        return self._source_job("The source", lambda: self.app.sync_todo(force=force), wait)

    @operation("check")
    def check(self) -> dict:
        """Check now: in the background when a scheduler runs (the web server), else right here."""
        self._require_source()
        if self.scheduler:
            self.scheduler.poll_now()
            return {"background": self.scheduler.describe()}
        out: dict = {}
        if self.repo.settings().watch:
            out["collect"] = self.collect(wait=None)
        if self.repo.settings().todo_enabled:
            out["todo"] = self.sync_todo(wait=None)
        return out

    @operation("status")
    def status(self) -> dict:
        s = self.repo.settings()
        items = self.repo.items()
        proposed = self.repo.items(status="proposed")
        docs = self.repo.source_docs()
        meta = self.repo.todo_meta()
        return {
            "source": self.source(),
            "judge": "jev" if classify.jev_enabled(self.cfg) else "llm",
            "watch": s.watch,
            "documents_read": sum(1 for d in docs if d["marker"]),
            "last_collected_at": max((d["collected_at"] for d in docs if d["collected_at"]), default=None),
            "items": {"open": sum(1 for i in items if i["status"] == "open"),
                      "done": sum(1 for i in items if i["status"] == "done"), "proposed": len(proposed)},
            "todo": {"enabled": s.todo_enabled, "name": s.todo_name, "folder": s.todo_folder,
                     "published_at": meta.get("published_at")},
            "background": self.scheduler.describe() if self.scheduler else None,
        }

    @operation("source")
    def source(self) -> dict:
        p = self.app.plugin
        return {"name": p.NAME, "label": p.LABEL, "mark": p.MARK, "device": p.DEVICE, **p.describe()}

    # ------------------------------------------------------------ what changed

    @operation("events")
    def events(self, since: int | None = None, limit: int = 1000) -> dict:
        """Changes after cursor `since` (without one: the last few), and the cursor to pass
        next time. Every process's changes are here: the server's, the CLI's, an agent's."""
        if since is not None and (not isinstance(since, int) or since < 0):
            raise Invalid("since must be a cursor from an earlier event")
        if since is None:
            since = max(0, self.repo.last_cursor() - RECENT_EVENTS)
        found = self.repo.events(since, limit)
        # Nothing new: the latest cursor, or a lower one if `since` came from a database since replaced.
        return {"cursor": found[-1]["cursor"] if found else min(since, self.repo.last_cursor()), "events": found}

    # ------------------------------------------------------------ where an item came from

    @operation("page.image")
    def page_image(self, doc_id: str, page: int, anchor: str | None = None, width: int | None = None) -> str:
        """A source page as SVG, with the line `anchor` highlighted; `width` in pixels (the height follows)."""
        if width is not None and (isinstance(width, bool) or not isinstance(width, int) or not 16 <= width <= 4000):
            raise Invalid("width must be from 16 to 4000 pixels")
        page_id = self.repo.page_id(doc_id, page)
        if not page_id:
            raise NotFound(f"no page {page} of {doc_id}")
        line = self.repo.source_line(doc_id, anchor) if anchor else None
        svg = self.app.plugin.render_page(doc_id, page_id, highlight=line["rows"] if line else None)
        if svg is None:
            raise NotFound("this source can't draw its pages")
        if width:
            svg = re.sub(r"<svg\b", f'<svg width="{width}"', svg, count=1)
        return svg

    @operation("line.image")
    def line_image(self, doc_id: str, anchor: str) -> str:
        """One handwritten line as SVG."""
        line = self.repo.source_line(doc_id, anchor)
        if not line:
            raise NotFound(f"no line {anchor} in {doc_id}")
        x0, y0, x1, y1 = line["bbox"]
        svg = self.app.plugin.render_page(doc_id, line["page_id"], crop=(x0 - 16, y0 - 12, x1 + 16, y1 + 12))
        if svg is None:
            raise NotFound("this source can't draw its pages")
        return svg

    # ------------------------------------------------------------ AI: the LLM and the Jev plugin

    @staticmethod
    def _jev_class() -> type:
        from .adapters.typesafe_judge import TypeSafeJudge

        return TypeSafeJudge

    def _ai_parts(self, which: str):
        if which == "llm":
            cls = llm.llm_class(self.cfg.llm)
            return self.cfg.llm.api_key_env, cls, lambda: cls(self.cfg.llm)
        if which == "jev":
            cls = self._jev_class()
            return self.cfg.jev.api_key_env, cls, lambda: cls(self.cfg.jev)
        raise NotFound(f"no AI part {which!r}; use llm or jev")

    @operation("ai.get")
    def ai(self) -> dict:
        cfg = self.cfg
        cls, jev = llm.llm_class(cfg.llm), self._jev_class()
        return {
            "llm": {"provider": cfg.llm.provider, "label": cls.LABEL, "family": cls.MODEL_FAMILY,
                    "model": cfg.llm.model, "key_url": cls.KEY_URL, "key": keys.describe(cfg, cfg.llm.api_key_env),
                    "providers": [{"id": p, "label": llm.llm_class(replace(cfg.llm, provider=p)).LABEL}
                                  for p in sorted(llm.PROVIDERS)]},
            "jev": {"name": jev.NAME, "by": jev.LABEL, "model": cfg.jev.model, "key_url": jev.KEY_URL,
                    "key": keys.describe(cfg, cfg.jev.api_key_env), "enabled": classify.jev_enabled(cfg)},
            "judge": "jev" if classify.jev_enabled(cfg) else "llm",
        }

    @operation("ai.provider")
    def set_provider(self, name: str) -> dict:
        """Choose the LLM adapter. Its model goes back to config.toml's until `ai.model` sets one."""
        if name not in llm.PROVIDERS:
            raise Invalid(f"no LLM provider {name!r}; available: {', '.join(sorted(llm.PROVIDERS))}")
        if name != self.cfg.llm.provider:
            self.repo.save_ai_choice(provider=name, model=None)
            self.app.reload()
        return self.ai()

    @operation("ai.model")
    def set_model(self, name: str) -> dict:
        """Choose the model. It reads handwriting, so it has to read images."""
        name = name.strip() if isinstance(name, str) else ""
        if not name:
            raise Invalid("Name a model")
        cls = llm.llm_class(self.cfg.llm)
        reads_images = getattr(cls, "reads_images", None)
        if reads_images and not reads_images(name):
            raise Invalid(f"{name} isn't a {cls.LABEL} model that reads images, which Jotted needs "
                          "to read handwriting")
        self.repo.save_ai_choice(model=name)
        self.app.reload()
        return self.ai()

    @operation("ai.set_key")
    def set_key(self, which: str, value: str) -> dict:
        """Check a key with its provider, then save it. Turning Jev on is adding its key."""
        name, cls, make = self._ai_parts(which)
        value = value.strip() if isinstance(value, str) else ""
        if not value:
            raise Invalid("Paste a key first")
        previous = os.environ.get(name)
        os.environ[name] = value
        try:
            make().verify()
        except llm.ModelError as e:
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous
            if "rejected" in str(e):
                raise KeyRejected(f"{e}. Check it was copied in full.") from e
            raise ModelFailed(f"{e}. The key wasn't saved.") from e
        keys.save(self.cfg, name, value)
        log.info("%s key saved", cls.LABEL)
        return self.ai()

    @operation("ai.remove_key")
    def remove_key(self, which: str) -> dict:
        """Turn the Jev plugin off. The LLM's key can be replaced but not removed: nothing works without it."""
        name, cls, _ = self._ai_parts(which)
        if which == "llm":
            raise Invalid(f"Jotted needs a {cls.LABEL} key to read handwriting; replace it instead")
        if keys.describe(self.cfg, name)["source"] == "environment":
            raise Conflict(f"{name} is exported in the shell that started Jotted. Remove it there, "
                           "then start Jotted again.")
        keys.remove(self.cfg, name)
        return self.ai()
