"""Composition root: builds the configured source plugin, the repository and the judge,
and runs the background scheduler.

Everything that talks to the source holds `App.lock`, across processes too: `jotted
serve` and a CLI command never reach the device at the same time.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from . import plugins
from .adapters.action_judge import ModelActionJudge
from .adapters.sqlite_repo import SqliteRepository
from .aicache import AICache
from .config import Config, with_llm
from .core import service
from .core.ports import DocumentSource, SourceError, TodoPublisher
from .ink.reader import InkReader
from .llm import ModelError
from .locking import Busy, SourceLock

log = logging.getLogger("jotted")

SYNC_ERRORS = (SourceError, ModelError, Busy)


@dataclass
class App:
    cfg: Config
    repo: SqliteRepository  # items, settings, the To-do document
    plugin: plugins.SourcePlugin
    source: DocumentSource
    judge: ModelActionJudge
    lock: SourceLock
    base: Config | None = field(default=None, repr=False)  # config.toml as loaded, before `effective`

    @classmethod
    def build(cls, cfg: Config) -> "App":
        base = cfg
        repo = SqliteRepository(cfg.server.db)
        cfg = effective(cfg, repo)
        cache = AICache(cfg.paths.cache_dir / "ai")
        plugin = plugins.plugin_class(cfg.plugins.source)(cfg, plugins.Host(ink=InkReader(cfg, cache)))
        return cls(cfg=cfg, repo=repo, plugin=plugin, source=plugin.source(),
                   judge=ModelActionJudge(cfg, cache), lock=lock_for(cfg), base=base)

    def reload(self) -> None:
        """Pick up a new LLM choice: rebuild what holds the config, in place (the scheduler
        keeps this App) and keeping the lock."""
        fresh = App.build(self.base or self.cfg)
        for name in ("cfg", "repo", "plugin", "source", "judge"):
            setattr(self, name, getattr(fresh, name))

    def todo_document(self) -> TodoPublisher | None:
        s = self.repo.settings()
        return self.plugin.publisher(s.todo_name, s.todo_folder)

    def own_doc_ids(self) -> set[str]:
        """Documents we write ourselves (the To-do list): never collected."""
        doc_id = self.repo.todo_meta().get("doc_id")
        return {doc_id} if doc_id else set()

    def collect(self, progress=lambda _: None):
        """Read what changed, with check.started/check.finished events around it (and
        source.error for what failed), so `jotted events` sees checks from any process."""
        self.repo.add_event("check.started")
        try:
            summary = service.collect(self.source, self.judge, self.repo, exclude=self.own_doc_ids(),
                                      progress=progress)
        except SYNC_ERRORS as e:
            self.repo.add_event("source.error", message=str(e))
            raise
        for message in summary.errors:
            self.repo.add_event("source.error", message=message)
        self.repo.add_event("check.finished", new=summary.actions_new, updated=summary.actions_updated,
                            missing=summary.actions_missing, pages_read=summary.pages_read,
                            errors=len(summary.errors))
        return summary

    def sync_todo(self, force: bool = False, fresh: bool = False) -> dict:
        if not self.repo.settings().todo_enabled:
            return {"enabled": False}
        doc = self.todo_document()
        if doc is None:
            return {"enabled": False, "unsupported": True}
        try:
            result = service.sync_todo(self.repo, doc, force=force, fresh=fresh)
        except SYNC_ERRORS as e:
            self.repo.add_event("source.error", message=str(e))
            raise
        doc_id = doc.document_id()
        if doc_id:  # remember it so the collector never reads it
            self.repo.set_todo_doc_id(doc_id)
        return result


def effective(cfg: Config, repo: SqliteRepository | None = None) -> Config:
    """config.toml with the choices saved in the database applied (the LLM's provider and model)."""
    return with_llm(cfg, **(repo or SqliteRepository(cfg.server.db)).ai_choice())


def lock_for(cfg: Config) -> SourceLock:
    """Shared by every Jotted process using this database."""
    return SourceLock(cfg.server.db.parent / f".{cfg.plugins.source}.lock")


@dataclass
class Status:
    running: bool = False
    step: str = ""
    last_run_at: float | None = None
    last_summary: dict = field(default_factory=dict)
    last_error: str | None = None


class Scheduler:
    """One background loop for everything that reads from the cloud: the watched folders
    and the To-do document. `push_soon()` updates the To-do document a few seconds after
    a web edit."""

    def __init__(self, app: App, push_delay_s: float):
        self.app = app
        self.status = Status()
        self.push_status = Status()
        self._push_delay = push_delay_s
        self._cond = threading.Condition()
        self._push_due: float | None = None
        self._poll_now = False

    def start(self) -> "Scheduler":
        threading.Thread(target=self._poll_loop, name="poll", daemon=True).start()
        threading.Thread(target=self._push_loop, name="push", daemon=True).start()
        return self

    # ------------------------------------------------------------ reading

    def poll_now(self) -> None:
        with self._cond:
            self._poll_now = True
            self._cond.notify_all()

    def poll_once(self) -> dict:
        summary: dict = {}
        with self.app.lock:
            self.status.running = True
            try:
                self.status.step = "Checking watched folders"
                collected = self.app.collect(progress=lambda m: setattr(self.status, "step", m))
                summary["collect"] = collected.as_dict()
                self.status.step = "Updating the To-do document"
                try:
                    summary["todo"] = self.app.sync_todo()
                except SourceError as e:  # keep what was collected above
                    summary["todo"], summary["todo_error"] = {}, str(e)
            finally:
                self.status.running = False
                self.status.step = ""
        return summary

    def _poll_loop(self) -> None:
        while True:
            try:
                self.status.last_summary = self.poll_once()
                errors = self.status.last_summary.get("collect", {}).get("errors") or []
                s = self.status.last_summary
                self.status.last_error = "; ".join(errors + ([s["todo_error"]] if s.get("todo_error") else [])) or None
            except SYNC_ERRORS as e:
                log.error("background poll failed: %s", e)
                self.status.last_error = str(e)
            except Exception as e:  # keep the loop alive
                log.exception("background poll crashed")
                self.status.last_error = f"Unexpected error: {e}"
            self.status.last_run_at = time.time()
            interval = max(15, self.app.repo.settings().poll_interval_s)
            with self._cond:
                self._cond.wait_for(lambda: self._poll_now, timeout=interval)
                self._poll_now = False

    # ------------------------------------------------------------ writing

    def push_soon(self) -> None:
        with self._cond:
            self._push_due = time.monotonic() + self._push_delay
            self._cond.notify_all()

    def push_once(self) -> dict:
        with self.app.lock:
            self.push_status.running = True
            try:
                return {"todo": self.app.sync_todo()}
            finally:
                self.push_status.running = False

    def _push_loop(self) -> None:
        while True:
            with self._cond:
                while self._push_due is None or time.monotonic() < self._push_due:
                    self._cond.wait(None if self._push_due is None else max(0.05, self._push_due - time.monotonic()))
                self._push_due = None
            try:
                self.push_status.last_summary = self.push_once()
                self.push_status.last_error = None
            except SYNC_ERRORS as e:
                log.error("automatic push failed: %s", e)
                self.push_status.last_error = str(e)
            except Exception as e:
                log.exception("automatic push crashed")
                self.push_status.last_error = f"Unexpected error: {e}"
            self.push_status.last_run_at = time.time()

    def describe(self) -> dict:
        with self._cond:
            due = self._push_due
        return {
            "poll": {"running": self.status.running, "step": self.status.step,
                     "last_run_at": self.status.last_run_at, "last_error": self.status.last_error,
                     "last_summary": self.status.last_summary},
            "push": {"running": self.push_status.running, "scheduled": due is not None,
                     "in_s": max(0, round(due - time.monotonic())) if due is not None else None,
                     "last_error": self.push_status.last_error},
        }


__all__ = ["App", "Scheduler", "SYNC_ERRORS"]
