"""SQLite implementation of the core's Repository port.

Every item lives in `actions`, whatever its origin (`source`): the source plugin's name
(collected from a watched document), "todo" (written by hand in an empty row of the To-do
document), "web" (typed in the web app or the CLI) or "agent" (added by an agent, usually
from another document it read: `source_kind`/`source_key` say which line).

An item is open or done (`status`); `proposed` items wait for the person to accept them and
`dismissed` ones are kept, so their source line is never added again.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..core.model import DocInfo, Judgment, PageInfo, Settings, SourceLine, TodoEntry, WrittenItem
from ..core.ports import ListFull, StateConflict

BULLETS = "-–—•*·>"
WEB_DOC = "web"  # doc_id of items added in the web app
AGENT = "agent"  # origin, and doc_id, of items an agent added
TYPED = ("web", AGENT)  # origins with no handwriting behind them


def clean_text(text: str) -> str:
    """An item's text without the bullet it was written with: "- test prod" -> "test prod"."""
    return text.strip().lstrip(BULLETS).strip()


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def to_utc(stamp: str | None) -> str:
    """Normalise a cloud timestamp (RFC 3339, maybe nanoseconds) to our format; now() if unusable."""
    if not stamp:
        return now()
    s = stamp.strip().replace("Z", "+00:00")
    if "." in s:  # trim sub-second digits beyond microseconds
        head, _, rest = s.partition(".")
        digits = "".join(ch for ch in rest if ch.isdigit())
        tz = rest[len(digits):]
        s = f"{head}.{digits[:6]}{tz}"
    try:
        return datetime.fromisoformat(s).astimezone(UTC).isoformat(timespec="microseconds")
    except ValueError:
        return now()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_docs (
    source       TEXT NOT NULL,
    id           TEXT NOT NULL,
    name         TEXT NOT NULL,
    folder       TEXT NOT NULL,
    marker       TEXT,
    page_count   INTEGER NOT NULL DEFAULT 0,
    collected_at TEXT,
    PRIMARY KEY (source, id)
);
CREATE TABLE IF NOT EXISTS source_pages (
    doc_id  TEXT NOT NULL,
    page_id TEXT NOT NULL,
    idx     INTEGER NOT NULL,
    hash    TEXT NOT NULL,
    PRIMARY KEY (doc_id, page_id)
);
CREATE TABLE IF NOT EXISTS source_baseline (
    doc_id  TEXT NOT NULL,
    page_id TEXT NOT NULL,
    strokes TEXT NOT NULL,  -- JSON list of the mark IDs on the page when it was recorded
    PRIMARY KEY (doc_id, page_id)
);
CREATE TABLE IF NOT EXISTS source_lines (
    doc_id   TEXT NOT NULL,
    page_id  TEXT NOT NULL,
    anchor   TEXT NOT NULL,
    key      TEXT NOT NULL,
    text     TEXT NOT NULL,
    bbox     TEXT NOT NULL,
    rows     TEXT,
    drawing  INTEGER NOT NULL DEFAULT 0,
    p_action REAL,
    owner    TEXT,
    PRIMARY KEY (doc_id, page_id, anchor)
);
CREATE TABLE IF NOT EXISTS actions (
    id                INTEGER PRIMARY KEY,
    source            TEXT NOT NULL,
    doc_id            TEXT NOT NULL,
    doc_name          TEXT NOT NULL,
    folder            TEXT NOT NULL,
    page_id           TEXT NOT NULL,
    page_index        INTEGER NOT NULL,
    anchor            TEXT NOT NULL,
    bbox              TEXT,
    text              TEXT NOT NULL,
    paper_text        TEXT NOT NULL,
    owner             TEXT NOT NULL,
    p_action          REAL NOT NULL,
    status            TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
    text_changed_at   TEXT NOT NULL,
    status_changed_at TEXT NOT NULL,
    missing           INTEGER NOT NULL DEFAULT 0,
    dismissed         INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (doc_id, anchor)
);
CREATE TABLE IF NOT EXISTS todo_slots (
    slot    INTEGER PRIMARY KEY,
    kind    TEXT NOT NULL,  -- always 'action' now; 'task' was the retired Tasks notebook
    item_id INTEGER NOT NULL,
    ticked  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (kind, item_id)
);
CREATE TABLE IF NOT EXISTS todo_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS events (
    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
    at     TEXT NOT NULL,  -- 2026-10-03T04:12:09Z
    type   TEXT NOT NULL,  -- item.added, settings.changed... (`jotted events`)
    data   TEXT NOT NULL   -- JSON object, merged into the event
);
CREATE INDEX IF NOT EXISTS events_at ON events (at);
"""

# Columns added to `actions` since its first release, added to older databases on start.
ADDED_COLUMNS = {
    "written": "INTEGER NOT NULL DEFAULT 0",  # 1 = written by hand on the To-do document itself
    "proposed": "INTEGER NOT NULL DEFAULT 0",  # 1 = waiting to be accepted; never on the To-do document
    "owner_name": "TEXT",
    "source_kind": "TEXT",  # where the item came from: the plugin's name, "todo", "gdoc", "gmail"...
    "source_key": "TEXT",  # the line, message or ticket there; unique with its kind
    "source_title": "TEXT",
    "source_url": "TEXT",
    "excerpt": "TEXT",
}

EVENTS_KEPT_S = 7 * 24 * 3600  # a week
PRUNE_EVERY = 500  # events between prunes


def _bbox(b) -> str:
    return json.dumps([round(v, 1) for v in b])


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class _Changes:
    """Items a transaction touches: each gets an event for what changed, in that transaction.
    Call it with an item's id before changing it (`new(id)` after adding one)."""

    def __init__(self, repo: "SqliteRepository", db):
        self.repo, self.db = repo, db
        self.before: dict[int, dict | None] = {}

    def __call__(self, item_id: int) -> None:
        if item_id not in self.before:
            self.before[item_id] = self.repo._item_in(self.db, item_id)

    def new(self, item_id: int) -> None:
        self.before.setdefault(item_id, None)

    def emit(self) -> None:
        for item_id, before in self.before.items():
            after = self.repo._item_in(self.db, item_id)
            if before == after:
                continue
            if after is None:
                self.repo._event(self.db, "item.removed", item={"id": item_id})
            elif after["status"] == "proposed":  # not on the list yet: wrappers showing the list skip these
                self.repo._event(self.db, "item.proposed", item=after)
            elif before is None:
                self.repo._event(self.db, "item.added", item=after)
            elif before["status"] == "proposed":  # now on the list: added, for wrappers that don't know accepting
                self.repo._event(self.db, "item.accepted", item=after)
                self.repo._event(self.db, "item.added", item=after)
            else:
                self.repo._event(self.db, "item.changed", item=after)


class SqliteRepository:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript(SCHEMA)
            cols = {r["name"] for r in db.execute("PRAGMA table_info(actions)")}
            for name, decl in ADDED_COLUMNS.items():
                if name in cols:
                    continue
                try:
                    db.execute(f"ALTER TABLE actions ADD COLUMN {name} {decl}")
                except sqlite3.OperationalError as e:  # another app starting at the same time added it
                    if "duplicate column" not in str(e):
                        raise
            self._migrate_tasks(db)
            # Handwritten items are keyed by their line, so one rule finds a duplicate whatever its origin.
            db.execute("UPDATE actions SET source_kind = source, source_key = doc_id || ':' || anchor "
                       "WHERE source_key IS NULL AND source NOT IN (?, ?)", TYPED)
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS actions_source ON actions (source_kind, source_key) "
                       "WHERE source_key IS NOT NULL")

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _migrate_tasks(db) -> None:
        """Once: turn the retired Tasks notebook's tasks into items.

        A handwritten task keeps its notebook and anchor stroke, so if that notebook is
        watched later its lines match these items instead of adding them again. A row it
        held on the To-do document stays its row. The old tables are left in place.
        """
        if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks'").fetchone() is None:
            return
        db.commit()
        db.execute("BEGIN IMMEDIATE")  # two apps starting at once: the second waits, then sees it done
        if db.execute("SELECT 1 FROM todo_meta WHERE key = 'tasks_migrated'").fetchone():
            return
        books = {r["id"]: r["name"] for r in db.execute("SELECT id, name FROM notebooks")}
        page_ids = {(r["notebook_id"], r["page_index"]): r["page_id"] for r in db.execute("SELECT * FROM pages")}
        stamp = now()
        for t in db.execute("SELECT * FROM tasks WHERE missing = 0 ORDER BY id").fetchall():
            paper = t["origin"] == "paper" and t["anchor_id"]
            if paper:
                doc_id, anchor, name = t["notebook_id"], t["anchor_id"], books.get(t["notebook_id"], "Tasks")
                page_index = t["page_index"] or 1
                page_id = page_ids.get((doc_id, page_index), "")
                rows = json.loads(t["rows"]) if t["rows"] else []
                bbox = [min(r[0] for r in rows), min(r[1] for r in rows),
                        max(r[2] for r in rows), max(r[3] for r in rows)] if rows else None
            else:
                doc_id, anchor, name, page_index, page_id, rows, bbox = WEB_DOC, uuid.uuid4().hex, "", 0, "", [], None
            cur = db.execute(
                """INSERT OR IGNORE INTO actions (source, doc_id, doc_name, folder, page_id, page_index, anchor, bbox,
                     text, paper_text, owner, p_action, status, text_changed_at, status_changed_at, created_at,
                     updated_at) VALUES (?, ?, ?, '/', ?, ?, ?, ?, ?, ?, 'me', 1.0, ?, ?, ?, ?, ?)""",
                ("remarkable" if paper else "web", doc_id, name, page_id, page_index, anchor,
                 _bbox(bbox) if bbox else None, t["text"], t["paper_text"] or t["text"], t["status"],
                 t["text_changed_at"], t["status_changed_at"], t["created_at"], stamp),
            )
            if cur.rowcount:
                item_id = cur.lastrowid
                if paper and page_id and bbox:  # so the web app can show the page and the handwritten line
                    db.execute("INSERT OR IGNORE INTO source_pages (doc_id, page_id, idx, hash) VALUES (?, ?, ?, '')",
                               (doc_id, page_id, page_index))
                    db.execute(
                        """INSERT OR IGNORE INTO source_lines (doc_id, page_id, anchor, key, text, bbox, rows)
                           VALUES (?, ?, ?, '', ?, ?, ?)""",  # key '': judged afresh if the notebook is watched
                        (doc_id, page_id, anchor, t["paper_text"] or t["text"], _bbox(bbox), json.dumps(rows)),
                    )
            else:  # already collected from that notebook: the more recent tick or untick wins
                row = db.execute("SELECT id, status_changed_at FROM actions WHERE doc_id = ? AND anchor = ?",
                                 (doc_id, anchor)).fetchone()
                item_id = row["id"]
                if t["status_changed_at"] > row["status_changed_at"]:
                    db.execute("UPDATE actions SET status = ?, status_changed_at = ?, updated_at = ? WHERE id = ?",
                               (t["status"], t["status_changed_at"], stamp, item_id))
            if db.execute("SELECT 1 FROM todo_slots WHERE kind = 'action' AND item_id = ?", (item_id,)).fetchone():
                db.execute("DELETE FROM todo_slots WHERE kind = 'task' AND item_id = ?", (t["id"],))
            else:
                db.execute("UPDATE todo_slots SET kind = 'action', item_id = ? WHERE kind = 'task' AND item_id = ?",
                           (item_id, t["id"]))
        db.execute("DELETE FROM todo_slots WHERE kind = 'task'")  # tasks gone from their notebook
        db.execute("INSERT INTO todo_meta (key, value) VALUES ('tasks_migrated', ?)", (stamp,))

    # ------------------------------------------------------------ settings

    AI_KEYS = {"provider": "llm.provider", "model": "llm.model"}  # beside the settings, not in them

    def ai_choice(self) -> dict:
        """The LLM provider and model chosen with `jotted ai provider`/`ai model`, if any."""
        with self.db() as db:
            rows = {r["key"]: json.loads(r["value"]) for r in db.execute(
                "SELECT * FROM settings WHERE key IN (?, ?)", tuple(self.AI_KEYS.values()))}
        return {k: rows.get(v) for k, v in self.AI_KEYS.items()}

    def save_ai_choice(self, **choice: str | None) -> None:
        with self.db() as db:
            for k, v in choice.items():
                db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                           "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (self.AI_KEYS[k], json.dumps(v)))

    def settings(self) -> Settings:
        with self.db() as db:
            stored = {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM settings")}
        if "tablet_include_others" in stored:  # its name before sources became plugins
            stored.setdefault("include_others", stored.pop("tablet_include_others"))
        known = {f.name for f in fields(Settings)}
        return Settings(**{k: v for k, v in stored.items() if k in known})

    def save_settings(self, settings: Settings) -> None:
        with self.db() as db:
            for k, v in asdict(settings).items():
                db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                           "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (k, json.dumps(v)))
            self._event(db, "settings.changed", settings=asdict(settings))

    # ------------------------------------------------------------ collection state

    def doc_marker(self, source: str, doc_id: str) -> str | None:
        with self.db() as db:
            row = db.execute("SELECT marker FROM source_docs WHERE source = ? AND id = ?", (source, doc_id)).fetchone()
        return row["marker"] if row else None

    def save_doc(self, doc: DocInfo, page_count: int) -> None:
        with self._tracked() as (db, changes):
            for r in db.execute("SELECT id FROM actions WHERE doc_id = ? AND (doc_name != ? OR folder != ?)",
                                (doc.id, doc.name, doc.folder)).fetchall():
                changes(r["id"])  # renamed or moved on the device
            db.execute(
                """INSERT INTO source_docs (source, id, name, folder, marker, page_count, collected_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (source, id) DO UPDATE SET name = excluded.name, folder = excluded.folder,
                     marker = excluded.marker, page_count = excluded.page_count, collected_at = excluded.collected_at""",
                (doc.source, doc.id, doc.name, doc.folder, doc.modified, page_count, now()),
            )
            db.execute("UPDATE actions SET doc_name = ?, folder = ? WHERE doc_id = ?", (doc.name, doc.folder, doc.id))

    def page_hash(self, doc_id: str, page_id: str) -> str | None:
        with self.db() as db:
            row = db.execute("SELECT hash FROM source_pages WHERE doc_id = ? AND page_id = ?",
                             (doc_id, page_id)).fetchone()
        return row["hash"] if row else None

    def save_baseline(self, doc: DocInfo, page: PageInfo, marks: set[str]) -> None:
        with self.db() as db:
            db.execute(
                """INSERT INTO source_pages (doc_id, page_id, idx, hash) VALUES (?, ?, ?, ?)
                   ON CONFLICT (doc_id, page_id) DO UPDATE SET idx = excluded.idx, hash = excluded.hash""",
                (doc.id, page.id, page.index, page.content_hash),
            )
            db.execute("INSERT OR REPLACE INTO source_baseline (doc_id, page_id, strokes) VALUES (?, ?, ?)",
                       (doc.id, page.id, json.dumps(sorted(marks))))

    def baseline_marks(self, doc_id: str, page_id: str) -> set[str] | None:
        with self.db() as db:
            row = db.execute("SELECT strokes FROM source_baseline WHERE doc_id = ? AND page_id = ?",
                             (doc_id, page_id)).fetchone()
        return set(json.loads(row["strokes"])) if row else None

    def baselined_docs(self) -> set[str]:
        with self.db() as db:
            return {r["doc_id"] for r in db.execute("SELECT DISTINCT doc_id FROM source_baseline")}

    def baseline_pages(self) -> dict[str, int]:
        """doc id -> how many of its pages were recorded as a baseline."""
        with self.db() as db:
            return {r["doc_id"]: r["n"] for r in db.execute(
                "SELECT doc_id, COUNT(*) AS n FROM source_baseline GROUP BY doc_id")}

    def clear_baseline(self, doc_id: str) -> None:
        """Baseline pages are read again in full; lines already judged on them keep their judgments."""
        with self.db() as db:
            pages = [r["page_id"] for r in db.execute("SELECT page_id FROM source_baseline WHERE doc_id = ?",
                                                      (doc_id,))]
            for pid in pages:
                db.execute("DELETE FROM source_pages WHERE doc_id = ? AND page_id = ?", (doc_id, pid))
                db.execute("DELETE FROM source_lines WHERE doc_id = ? AND page_id = ? AND p_action IS NULL",
                           (doc_id, pid))
            db.execute("DELETE FROM source_baseline WHERE doc_id = ?", (doc_id,))
            db.execute("UPDATE source_docs SET marker = NULL WHERE id = ?", (doc_id,))

    def line_keys(self, doc_id: str, page_id: str) -> dict[str, str]:
        with self.db() as db:
            return {r["anchor"]: r["key"] for r in db.execute(
                "SELECT anchor, key FROM source_lines WHERE doc_id = ? AND page_id = ?", (doc_id, page_id))}

    def save_page(self, doc: DocInfo, page: PageInfo, lines: list[SourceLine],
                  judgments: dict[str, Judgment], threshold: float) -> tuple[int, int, int]:
        stamp, paper_at = now(), to_utc(doc.modified)
        added = updated = missing = 0
        with self._tracked() as (db, changes):
            db.execute(
                """INSERT INTO source_pages (doc_id, page_id, idx, hash) VALUES (?, ?, ?, ?)
                   ON CONFLICT (doc_id, page_id) DO UPDATE SET idx = excluded.idx, hash = excluded.hash""",
                (doc.id, page.id, page.index, page.content_hash),
            )
            present = {ln.anchor for ln in lines}
            for ln in lines:
                j = judgments.get(ln.anchor)
                prev = db.execute("SELECT * FROM source_lines WHERE doc_id = ? AND page_id = ? AND anchor = ?",
                                  (doc.id, page.id, ln.anchor)).fetchone()
                p_action = j.p_action if j else (prev["p_action"] if prev and prev["key"] == ln.key else None)
                owner = j.owner if j else (prev["owner"] if prev and prev["key"] == ln.key else None)
                db.execute(
                    """INSERT INTO source_lines (doc_id, page_id, anchor, key, text, bbox, rows, drawing, p_action, owner)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT (doc_id, page_id, anchor) DO UPDATE SET key = excluded.key, text = excluded.text,
                         bbox = excluded.bbox, rows = excluded.rows, drawing = excluded.drawing,
                         p_action = excluded.p_action, owner = excluded.owner""",
                    (doc.id, page.id, ln.anchor, ln.key, ln.text, _bbox(ln.bbox),
                     json.dumps([[round(v, 1) for v in r] for r in (ln.rows or [ln.bbox])]), int(ln.drawing),
                     p_action, owner),
                )
                action = db.execute("SELECT * FROM actions WHERE doc_id = ? AND anchor = ?",
                                    (doc.id, ln.anchor)).fetchone()
                text = clean_text(ln.text)
                is_action = p_action is not None and p_action >= threshold and text and not ln.drawing
                if action is None:
                    if is_action:
                        cur = db.execute(
                            """INSERT INTO actions (source, doc_id, doc_name, folder, page_id, page_index, anchor, bbox,
                                 text, paper_text, owner, p_action, text_changed_at, status_changed_at, created_at,
                                 updated_at, source_kind, source_key)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (doc.source, doc.id, doc.name, doc.folder, page.id, page.index, ln.anchor, _bbox(ln.bbox),
                             text, text, owner or "unclear", p_action, paper_at, paper_at, stamp, stamp,
                             doc.source, f"{doc.id}:{ln.anchor}"),
                        )
                        changes.new(cur.lastrowid)
                        added += 1
                    continue
                fields_: dict = {"page_id": page.id, "page_index": page.index, "bbox": _bbox(ln.bbox), "missing": 0}
                if j is not None:  # the line changed and was judged again
                    fields_.update(p_action=p_action, owner=owner or action["owner"])
                    if not is_action:
                        fields_["missing"] = 1  # no longer reads as an action
                    if text and text != action["paper_text"]:
                        fields_["paper_text"] = text
                        if paper_at > action["text_changed_at"]:
                            fields_.update(text=text, text_changed_at=paper_at)
                    updated += 1
                changes(action["id"])
                self._update(db, "actions", action["id"], fields_, stamp)

            stored = db.execute("SELECT anchor FROM source_lines WHERE doc_id = ? AND page_id = ?",
                                (doc.id, page.id)).fetchall()
            for row in stored:
                if row["anchor"] in present:
                    continue
                db.execute("DELETE FROM source_lines WHERE doc_id = ? AND page_id = ? AND anchor = ?",
                           (doc.id, page.id, row["anchor"]))
                gone = db.execute("SELECT id FROM actions WHERE doc_id = ? AND anchor = ? AND missing = 0",
                                  (doc.id, row["anchor"])).fetchone()
                if gone:
                    changes(gone["id"])
                    self._update(db, "actions", gone["id"], {"missing": 1}, stamp)
                    missing += 1
        return added, updated, missing

    @staticmethod
    def _update(db, table: str, row_id: int, values: dict, stamp: str) -> None:
        values = {**values, "updated_at": stamp}
        cols = ", ".join(f"{k} = ?" for k in values)
        db.execute(f"UPDATE {table} SET {cols} WHERE id = ?", (*values.values(), row_id))

    # ------------------------------------------------------------ the combined list

    # Items with the page count of their document (None when it isn't one that was read).
    SELECT_ITEMS = ("SELECT a.*, (SELECT MAX(page_count) FROM source_docs d WHERE d.id = a.doc_id) AS doc_pages "
                    "FROM actions a")

    def items(self, status: str | None = None, owner: str | None = None, folder: str | None = None, *,
              source_kind: str | None = None, source_key: str | None = None, query: str | None = None,
              after: int | None = None, limit: int | None = None) -> list[dict]:
        """Items in list order. `status` is open, done, proposed, dismissed or "any"; without one,
        the list itself (open and done). `after` is the id of the last item already seen."""
        with self.db() as db:
            slots = {r["item_id"]: r["slot"] for r in db.execute("SELECT * FROM todo_slots")}
            out = [self._item_dict(r, slots.get(r["id"])) for r in db.execute(
                self.SELECT_ITEMS + " WHERE a.missing = 0 ORDER BY a.created_at, a.id")]
            if after is not None:
                mark = db.execute("SELECT created_at, id FROM actions WHERE id = ?", (after,)).fetchone()
                if mark is None:
                    raise KeyError(after)
        if status != "any":
            wanted = (status,) if status else ("open", "done")
            out = [i for i in out if i["status"] in wanted]
        if owner == "mine":
            out = [i for i in out if i["owner"] in ("me", "unclear")]
        elif owner == "others":
            out = [i for i in out if i["owner"] == "someone_else"]
        if folder:
            f = "/" + folder.strip("/")
            out = [i for i in out if (i["source"]["folder"] + "/").startswith(f.rstrip("/") + "/")]
        if source_kind:
            out = [i for i in out if i["source"]["kind"] == source_kind]
        if source_key:
            out = [i for i in out if i["source"]["key"] == source_key]
        if query:
            q = query.casefold()
            out = [i for i in out if q in i["text"].casefold() or q in (i["source"]["title"] or "").casefold()]
        if after is not None:
            out = [i for i in out if (i["created_at"], i["id"]) > (mark["created_at"], mark["id"])]
        return out[:limit] if limit else out

    @staticmethod
    def _item_dict(r, slot: int | None) -> dict:
        typed = r["source"] in TYPED
        status = "dismissed" if r["dismissed"] else "proposed" if r["proposed"] else r["status"]
        return {
            "id": r["id"], "origin": r["source"], "text": r["text"], "paper_text": r["paper_text"],
            "written": bool(r["written"]), "bbox": json.loads(r["bbox"]) if r["bbox"] else None,
            "status": status, "owner": r["owner"], "owner_name": r["owner_name"], "p_action": round(r["p_action"], 2),
            "source": {"doc_id": r["doc_id"], "name": r["doc_name"], "folder": r["folder"],
                       "page": r["page_index"], "anchor": r["anchor"],
                       "kind": r["source_kind"], "key": r["source_key"],
                       "title": r["source_title"] or (None if typed else r["doc_name"]),
                       "url": r["source_url"], "excerpt": r["excerpt"]},
            "page": None if typed or not r["page_id"] else {
                "doc_id": r["doc_id"], "doc_name": r["doc_name"], "page": r["page_index"],
                "page_count": r["doc_pages"], "anchor": r["anchor"]},
            "edited": not typed and r["text"] != r["paper_text"], "slot": slot,
            "created_at": r["created_at"],
        }

    def _item_in(self, db, item_id: int) -> dict | None:
        """An item inside a transaction, for its events; None if it isn't (or is no longer) an item:
        dismissed, or its line gone."""
        r = db.execute(self.SELECT_ITEMS + " WHERE a.id = ? AND a.dismissed = 0 AND a.missing = 0",
                       (item_id,)).fetchone()
        if r is None:
            return None
        slot = db.execute("SELECT slot FROM todo_slots WHERE item_id = ?", (item_id,)).fetchone()
        return self._item_dict(r, slot["slot"] if slot else None)

    def item(self, item_id: int, *, missing_too: bool = False) -> dict:
        """One item in any status, dismissed ones included; KeyError if there is none. An item
        whose line is gone from its page counts only with `missing_too` (a source key still finds it)."""
        with self.db() as db:
            r = db.execute(self.SELECT_ITEMS + " WHERE a.id = ? AND (a.missing = 0 OR ?)",
                           (item_id, missing_too)).fetchone()
            slot = db.execute("SELECT slot FROM todo_slots WHERE item_id = ?", (item_id,)).fetchone()
        if r is None:
            raise KeyError(item_id)
        return self._item_dict(r, slot["slot"] if slot else None)

    def add_item(self, text: str, *, origin: str = "web", owner: str = "me", owner_name: str | None = None,
                 source: dict | None = None, proposed: bool = False,
                 proposed_limit: int | None = None) -> tuple[int, str]:
        """An item typed in the web app or the CLI, or added by an agent. With a source key it is
        added once: a second add finds it. Returns (id, outcome): "created"; "updated" (a proposed
        item's text changed); "existing"; "dismissed" (the person turned it down: left alone)."""
        text = clean_text(text)
        if not text:
            raise ValueError("The item is empty")
        source = source or {}
        kind, key = source.get("kind"), source.get("key")
        stamp = now()
        with self._tracked() as (db, changes):
            # Look and add under the write lock: another process adding the same key waits, then finds it.
            db.execute("BEGIN IMMEDIATE")
            if key:
                row = db.execute("SELECT * FROM actions WHERE source_kind = ? AND source_key = ?",
                                 (kind, key)).fetchone()
                if row is not None:
                    if row["dismissed"]:
                        return row["id"], "dismissed"
                    if row["proposed"] and text != row["text"]:
                        changes(row["id"])
                        self._update(db, "actions", row["id"], {"text": text, "paper_text": text,
                                                                "text_changed_at": stamp}, stamp)
                        return row["id"], "updated"
                    return row["id"], "existing"
            if proposed and proposed_limit is not None:
                waiting = db.execute("SELECT COUNT(*) FROM actions WHERE proposed = 1 AND dismissed = 0 "
                                     "AND missing = 0").fetchone()[0]
                if waiting >= proposed_limit:
                    raise ListFull(f"There are already {waiting} proposed items; accept or dismiss some first")
            cur = db.execute(
                """INSERT INTO actions (source, doc_id, doc_name, folder, page_id, page_index, anchor, text,
                     paper_text, owner, owner_name, p_action, text_changed_at, status_changed_at, created_at,
                     updated_at, proposed, source_kind, source_key, source_title, source_url, excerpt)
                   VALUES (?, ?, ?, '', '', 0, ?, ?, ?, ?, ?, 1.0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (origin, AGENT if origin == AGENT else WEB_DOC, source.get("title") or "", uuid.uuid4().hex,
                 text, text, owner, owner_name, stamp, stamp, stamp, stamp, int(proposed), kind, key,
                 source.get("title"), source.get("url"), source.get("excerpt")),
            )
            changes.new(cur.lastrowid)
            if not proposed:
                self._web_changed(db, stamp)
        return int(cur.lastrowid), "created"

    @staticmethod
    def _web_changed(db, stamp: str) -> None:
        db.execute("INSERT INTO todo_meta (key, value) VALUES ('web_changed_at', ?) "
                   "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (stamp,))

    def edit_action(self, action_id: int, *, text: str | None = None, status: str | None = None,
                    dismissed: bool | None = None, owner: str | None = None, owner_name: str | None = None) -> dict:
        """Returns the item, or {} once dismissed. A dismissed item can't change; a proposed one
        can be reworded or dismissed, but is ticked only once accepted."""
        stamp = now()
        with self._tracked() as (db, changes):
            row = db.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
            if row is None:
                raise KeyError(action_id)
            changes(action_id)
            values: dict = {}
            if text is not None and text.strip() and text.strip() != row["text"]:
                values.update(text=text.strip(), text_changed_at=stamp)
            if status is not None and status != row["status"]:
                if status not in ("open", "done"):
                    raise ValueError("status must be 'open' or 'done'")
                values.update(status=status, status_changed_at=stamp)
            if owner is not None and owner != row["owner"]:
                values["owner"] = owner
            if owner_name is not None and owner_name != row["owner_name"]:
                values["owner_name"] = owner_name or None
            if values and row["dismissed"]:
                raise StateConflict(f"Item {action_id} was dismissed")
            if row["proposed"] and ("status" in values or (status == "open" and not row["dismissed"])):
                raise StateConflict(f"Item {action_id} is proposed: accept it first")
            if dismissed is not None:
                values["dismissed"] = int(dismissed)
            if dismissed and not row["written"]:
                # Its row on the To-do document is blank now: free it, unless it has ink (a tick),
                # so a later tick there can't mark the dismissed item done.
                db.execute("DELETE FROM todo_slots WHERE item_id = ? AND kind = 'action' AND ticked = 0",
                           (action_id,))
            if values:
                self._update(db, "actions", action_id, values, stamp)
                self._web_changed(db, stamp)
        return self.item(action_id) if not dismissed else {}

    def accept(self, ids: list[int] | None, source_kind: str | None = None) -> tuple[list[int], list[dict]]:
        """Proposed items onto the list: `ids`, or (None) every proposed one, of `source_kind` if given.
        Returns the ids accepted and those skipped, with why (not_found, not_proposed)."""
        stamp = now()
        accepted, skipped = [], []
        with self._tracked() as (db, changes):
            if ids is None:
                ids = [r["id"] for r in db.execute(
                    "SELECT id FROM actions WHERE proposed = 1 AND dismissed = 0 AND missing = 0 "
                    "AND (? IS NULL OR source_kind = ?) ORDER BY created_at, id", (source_kind, source_kind))]
            for item_id in dict.fromkeys(ids):  # once each: a repeated id isn't a second, failed accept
                row = db.execute("SELECT * FROM actions WHERE id = ? AND missing = 0", (item_id,)).fetchone()
                if row is None:
                    skipped.append({"id": item_id, "reason": "not_found"})
                elif not row["proposed"] or row["dismissed"]:
                    skipped.append({"id": item_id, "reason": "not_proposed"})
                else:
                    changes(item_id)
                    self._update(db, "actions", item_id, {"proposed": 0, "status": "open",
                                                          "status_changed_at": stamp}, stamp)
                    accepted.append(item_id)
            if accepted:
                self._web_changed(db, stamp)
        return accepted, skipped

    def source_line(self, doc_id: str, anchor: str) -> dict | None:
        with self.db() as db:
            row = db.execute("SELECT l.*, p.idx FROM source_lines l JOIN source_pages p "
                             "ON p.doc_id = l.doc_id AND p.page_id = l.page_id WHERE l.doc_id = ? AND l.anchor = ?",
                             (doc_id, anchor)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["bbox"] = json.loads(d["bbox"])
        d["rows"] = json.loads(d["rows"]) if d["rows"] else [d["bbox"]]
        return d

    def source_docs(self) -> list[dict]:
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM source_docs ORDER BY folder, name")]

    def page_id(self, doc_id: str, index: int) -> str | None:
        with self.db() as db:
            row = db.execute("SELECT page_id FROM source_pages WHERE doc_id = ? AND idx = ?", (doc_id, index)).fetchone()
        return row["page_id"] if row else None

    # ------------------------------------------------------------ the To-do document

    def todo_doc_id(self) -> str | None:
        return self.todo_meta().get("doc_id")

    def set_todo_doc_id(self, doc_id: str) -> None:
        with self.db() as db:
            db.execute("INSERT INTO todo_meta (key, value) VALUES ('doc_id', ?) "
                       "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (doc_id,))

    def reset_todo(self) -> None:
        with self.db() as db:
            db.execute("DELETE FROM todo_slots")
            db.execute("DELETE FROM todo_meta WHERE key IN ('doc_id', 'fingerprint', 'paper_marker')")

    def todo_entries(self, include_others: bool) -> list[TodoEntry]:
        current = self.todo_doc_id()
        entries = []
        for i in self.items():
            if not include_others and i["owner"] == "someone_else":
                continue
            src = i["source"]
            # Handwriting is the item's text only while it is on the current document.
            on_paper = i["written"] and src["doc_id"] == current
            if on_paper:
                label = "written here"
            elif i["written"]:
                label = "written on an earlier To-do"
            elif i["origin"] == "web":
                label = "added in Jotted"
            elif i["origin"] == AGENT:
                kind, title = i["source"]["kind"], i["source"]["title"]
                label = f"{kind} › {title}" if kind and title else (title or kind or "added by an agent")
            else:
                folder = src["folder"].strip("/") or "Library"
                label = f"{folder} › {src['name']} · p{src['page']}"
                if i["owner"] == "someone_else":
                    label = "others · " + label
            entries.append(TodoEntry(item_id=i["id"], text=i["text"],
                                     done=i["status"] == "done", source_label=label, slot=i["slot"],
                                     handwritten=on_paper,
                                     ink=tuple(i["bbox"]) if on_paper and i.get("bbox") else None,
                                     edited=bool(i.get("edited"))))
        return entries

    def assign_slots(self, entries: list[TodoEntry], capacity: int, inked: set[int]) -> tuple[list[TodoEntry], int]:
        waiting = [e for e in entries if e.slot is None and not e.done]  # a done item never joins the document
        placed = [e for e in entries if e.slot is not None]
        taken = {e.slot for e in placed} | self.occupied_slots()
        free = [s for s in range(capacity) if s not in taken and s not in inked]
        # Short of rows: done items on rows without ink leave, and their rows are reused.
        releasable = [e for e in placed if e.done and e.slot not in inked]
        released = releasable[:max(0, len(waiting) - len(free))]
        with self._tracked() as (db, changes):
            for e in released:
                changes(e.item_id)
                db.execute("DELETE FROM todo_slots WHERE slot = ?", (e.slot,))
                free.append(e.slot)
                e.slot = None
            free.sort()
            for e, slot in zip(waiting, free):
                changes(e.item_id)
                db.execute("INSERT INTO todo_slots (slot, kind, item_id) VALUES (?, 'action', ?)", (slot, e.item_id))
                e.slot = slot
        return sorted((e for e in entries if e.slot is not None), key=lambda e: e.slot), max(0, len(waiting) - len(free))

    def occupied_slots(self) -> set[int]:
        with self.db() as db:
            return {r["slot"] for r in db.execute("SELECT slot FROM todo_slots")}

    def add_written(self, doc_id: str, items: list[WrittenItem]) -> int:
        """New items written by hand in empty rows of the To-do document: they keep that row,
        so the handwriting stays next to its checkbox."""
        if not items:
            return 0
        settings = self.settings()
        stamp = now()
        added = 0
        with self._tracked() as (db, changes):
            for w in items:
                if db.execute("SELECT 1 FROM todo_slots WHERE slot = ?", (w.slot,)).fetchone():
                    continue  # taken meanwhile
                text = clean_text(w.text)
                cur = db.execute(
                    """INSERT OR IGNORE INTO actions (source, doc_id, doc_name, folder, page_id, page_index, anchor,
                         bbox, text, paper_text, owner, p_action, text_changed_at, status_changed_at, created_at,
                         updated_at, written, source_kind, source_key)
                       VALUES ('todo', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'me', 1.0, ?, ?, ?, ?, 1, 'todo', ?)""",
                    (doc_id, settings.todo_name, settings.todo_folder, w.page_id, w.page_index, w.anchor,
                     _bbox(w.bbox), text, text, stamp, stamp, stamp, stamp, f"{doc_id}:{w.anchor}"),
                )
                if not cur.rowcount:
                    continue
                changes.new(cur.lastrowid)
                # Its line, so the web app can show the handwriting like any collected action.
                db.execute("INSERT OR IGNORE INTO source_pages (doc_id, page_id, idx, hash) VALUES (?, ?, ?, '')",
                           (doc_id, w.page_id, w.page_index))
                db.execute(
                    """INSERT OR REPLACE INTO source_lines (doc_id, page_id, anchor, key, text, bbox, rows, drawing,
                         p_action, owner) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1.0, 'me')""",
                    (doc_id, w.page_id, w.anchor, w.key, text, _bbox(w.bbox), json.dumps([list(w.bbox)])),
                )
                # A box that already had ink when the item was written isn't a tick.
                db.execute("INSERT INTO todo_slots (slot, kind, item_id, ticked) VALUES (?, 'action', ?, ?)",
                           (w.slot, cur.lastrowid, int(w.box_inked)))
                added += 1
        return added

    def apply_ticks(self, ticked_slots: set[int], marker: str) -> int:
        """A slot that gains ink marks its item done. Ink stays on paper, so each slot's tick
        counts once: re-opening the item on the web afterwards is not undone by the old tick."""
        stamp = now()
        changed = 0
        with self._tracked() as (db, changes):
            for slot in sorted(ticked_slots):
                row = db.execute("SELECT * FROM todo_slots WHERE slot = ? AND ticked = 0", (slot,)).fetchone()
                if row is None:
                    continue
                changes(row["item_id"])
                db.execute("UPDATE todo_slots SET ticked = 1 WHERE slot = ?", (slot,))
                db.execute("UPDATE actions SET status = 'done', status_changed_at = ?, updated_at = ? "
                           "WHERE id = ? AND status = 'open'", (stamp, stamp, row["item_id"]))
                changed += 1
            db.execute("INSERT INTO todo_meta (key, value) VALUES ('paper_marker', ?) "
                       "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (marker,))
        return changed

    @staticmethod
    def _fingerprint(entries: list[TodoEntry]) -> str:
        data = json.dumps([(e.slot, e.text, e.done, e.source_label, e.handwritten, e.edited) for e in entries])
        return hashlib.sha256(data.encode()).hexdigest()

    def todo_needs_publish(self, entries: list[TodoEntry]) -> bool:
        with self.db() as db:
            row = db.execute("SELECT value FROM todo_meta WHERE key = 'fingerprint'").fetchone()
        return row is None or row["value"] != self._fingerprint(entries)

    def mark_todo_published(self, entries: list[TodoEntry]) -> None:
        with self.db() as db:
            for k, v in (("fingerprint", self._fingerprint(entries)), ("published_at", now())):
                db.execute("INSERT INTO todo_meta (key, value) VALUES (?, ?) "
                           "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (k, v))
            self._event(db, "todo.published", items=len(entries))

    def todo_meta(self) -> dict:
        with self.db() as db:
            return {r["key"]: r["value"] for r in db.execute("SELECT * FROM todo_meta")}

    # ------------------------------------------------------------ events

    @contextmanager
    def _tracked(self):
        """A transaction whose item changes are written as events in it."""
        with self.db() as db:
            changes = _Changes(self, db)
            yield db, changes
            changes.emit()

    def _event(self, db, type_: str, **data) -> None:
        cur = db.execute("INSERT INTO events (at, type, data) VALUES (?, ?, ?)",
                         (_stamp(datetime.now(UTC)), type_, json.dumps(data, default=str)))
        if cur.lastrowid % PRUNE_EVERY == 0:
            cutoff = _stamp(datetime.now(UTC) - timedelta(seconds=EVENTS_KEPT_S))
            db.execute("DELETE FROM events WHERE at < ?", (cutoff,))

    def add_event(self, type_: str, **data) -> None:
        """An event with no change of its own here (a check started, the source failed)."""
        with self.db() as db:
            self._event(db, type_, **data)

    def events(self, since: int | None = None, limit: int = 1000) -> list[dict]:
        """Events after cursor `since`, oldest first: {cursor, at, type, **data}."""
        with self.db() as db:
            rows = db.execute("SELECT * FROM events WHERE cursor > ? ORDER BY cursor LIMIT ?",
                              (since or 0, limit)).fetchall()
        return [{"cursor": r["cursor"], "at": r["at"], "type": r["type"], **json.loads(r["data"])} for r in rows]

    def last_cursor(self) -> int:
        with self.db() as db:
            row = db.execute("SELECT MAX(cursor) AS c FROM events").fetchone()
        return row["c"] or 0
