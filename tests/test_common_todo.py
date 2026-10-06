"""The common to-do list: the core with fake adapters, the SQLite repository, the To-do
document layout and tick reading, and the web endpoints."""

import dataclasses
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import synth  # noqa: E402

from jotted import config  # noqa: E402
from jotted.plugins.remarkable import page, todo_document  # noqa: E402
from jotted.plugins.remarkable.settings import TemplateConfig  # noqa: E402
from jotted.adapters.sqlite_repo import SqliteRepository  # noqa: E402
from jotted.core import service  # noqa: E402
from jotted.core.model import (  # noqa: E402
    DocInfo, Judgment, PageInfo, PaperRead, Settings, SourceLine, TodoEntry, WrittenItem,
)
from jotted.ink.strokes import make_stroke  # noqa: E402

ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------- fakes


class FakeSource:
    name = "fake"

    def __init__(self):
        self.docs: dict[str, DocInfo] = {}
        self.pages_: dict[str, list[PageInfo]] = {}
        self.lines_: dict[tuple[str, str], list[SourceLine]] = {}
        self.reads: list[tuple[str, str]] = []

    def add(self, doc_id, name, folder, modified, pages):
        """pages: {page_id: [(anchor, key, text), ...]}; a key "k1+k2" is a line of strokes k1 and k2."""
        self.docs[doc_id] = DocInfo("fake", doc_id, name, folder, modified)
        self.pages_[doc_id] = []
        for i, (pid, lines) in enumerate(pages.items(), start=1):
            digest = str(hash(tuple(lines)))
            self.pages_[doc_id].append(PageInfo(doc_id, pid, i, digest))
            self.lines_[(doc_id, pid)] = [
                SourceLine(anchor=a, key=k, text=t, bbox=(0, 100 * n, 500, 100 * n + 40), marks=tuple(k.split("+")))
                for n, (a, k, t) in enumerate(lines)
            ]

    def list_documents(self):
        return list(self.docs.values())

    def folders(self):
        return sorted({d.folder for d in self.docs.values()})

    def pages(self, doc):
        return self.pages_[doc.id]

    def read_page(self, doc, page):
        self.reads.append((doc.id, page.id))
        return self.lines_[(doc.id, page.id)]

    def mark_ids(self, doc, page):
        return {s for ln in self.lines_[(doc.id, page.id)] for s in ln.marks}


class FakeJudge:
    """Lines containing 'TODO' are actions; '@name' makes them someone else's."""

    def __init__(self):
        self.judged: list[str] = []

    def judge(self, doc, page, lines, new):
        out = {}
        for ln in new:
            self.judged.append(ln.text)
            is_action = "TODO" in ln.text
            out[ln.anchor] = Judgment(p_action=0.95 if is_action else 0.1,
                                      owner="someone_else" if "@" in ln.text else "me")
        return out


class FakePublisher:
    def __init__(self, capacity=10):
        self.cap = capacity
        self.published: list[list[TodoEntry]] = []
        self.ticks: tuple[set[int], str] | None = None
        self.written: list[WrittenItem] = []
        self.occupied_seen: set[int] | None = None
        self.ink: set[int] = set()  # rows with other ink (old ticks, scribbles)
        self.pages_capacity: int | None = None  # what the tablet laid out; None = not opened yet
        self.deleted = 0

    def capacity(self):
        return self.cap

    def publish(self, entries):
        self.published.append([dataclasses.replace(e) for e in entries])

    def document_id(self):
        return "todo-doc" if self.ticks is not None else None

    def delete(self):
        self.deleted += 1
        self.ticks, self.written, self.ink, self.pages_capacity = None, [], set(), None

    def read_paper(self, occupied):
        self.occupied_seen = set(occupied)
        if self.ticks is None:
            return None
        slots, marker = self.ticks
        return PaperRead(doc_id="todo-doc", marker=marker, ticks=set(slots),
                         written=[w for w in self.written if w.slot not in occupied],
                         inked=set(slots) | {w.slot for w in self.written} | self.ink,
                         capacity=self.pages_capacity)


class FakePlugin:
    """A source plugin with no device behind it: the core needs nothing more."""
    NAME, LABEL, MARK, DEVICE, SECTIONS = "fake", "Fake notes", "F", "the fake device", {}

    def __init__(self, source, publisher=None, strokes=()):
        self._source, self._publisher, self.strokes = source, publisher, list(strokes)

    def source(self):
        return self._source

    def publisher(self, name, folder):
        return self._publisher

    def render_page(self, doc_id, page_id, highlight=None, crop=None):
        return page.render_svg(self.strokes, 1.0, highlight=highlight, crop=crop)

    def describe(self):
        return {"connected": True, "detail": "always"}


def fake_app(cfg, repo, source, judge, publisher=None, strokes=()):
    from jotted.app import App, lock_for

    plugin = FakePlugin(source, publisher, strokes)
    return App(cfg=cfg, repo=repo, plugin=plugin, source=source, judge=judge, lock=lock_for(cfg))


@pytest.fixture
def repo(tmp_path):
    r = SqliteRepository(tmp_path / "db.sqlite")
    r.save_settings(Settings(watch=["/Meetings"], action_threshold=0.7))
    return r


def notes(source, modified="2026-10-01T10:00:00Z", extra=None):
    page1 = [("1:10", "k10", "Agenda for the weekly"), ("1:20", "k20", "TODO book the retro room"),
             ("1:30", "k30", "@Simon TODO send the deck")]
    if extra:
        page1 = page1 + extra
    source.add("doc-a", "Weekly sync", "/Meetings", modified, {"p1": page1, "p2": [("1:40", "k40", "Notes only")]})
    source.add("doc-b", "Diary", "/Personal", modified, {"p1": [("1:50", "k50", "TODO private thing")]})


# ---------------------------------------------------------------- core: collect


def test_settings_watch_matching():
    s = Settings(watch=["/Meetings", "/Work/Notes/Standup"])
    doc = lambda folder, name="x": DocInfo("fake", "1", name, folder, "m")  # noqa: E731
    assert s.watches(doc("/Meetings")) and s.watches(doc("/Meetings/2026"))
    assert not s.watches(doc("/Meetingsx")) and not s.watches(doc("/Personal"))
    assert s.watches(doc("/Work/Notes", "Standup"))  # a single document
    assert Settings(watch=["/"]).watches(doc("/anything"))


def test_collect_reads_only_watched_documents_and_creates_actions(repo):
    source, judge = FakeSource(), FakeJudge()
    notes(source)
    summary = service.collect(source, judge, repo)
    assert summary.docs_seen == 1 and summary.docs_changed == 1 and summary.pages_read == 2
    assert summary.actions_new == 2
    items = {i["text"]: i for i in repo.items()}
    assert set(items) == {"TODO book the retro room", "@Simon TODO send the deck"}
    assert items["@Simon TODO send the deck"]["owner"] == "someone_else"
    assert items["TODO book the retro room"]["source"]["folder"] == "/Meetings"
    assert "TODO private thing" not in judge.judged  # an unwatched folder is never read


def test_collect_is_incremental_at_every_level(repo):
    source, judge = FakeSource(), FakeJudge()
    notes(source)
    service.collect(source, judge, repo)
    first_judged = len(judge.judged)

    # Nothing changed: the document is skipped before its pages are even listed.
    source.reads.clear()
    summary = service.collect(source, judge, repo)
    assert summary.docs_changed == 0 and not source.reads and len(judge.judged) == first_judged

    # One line added on page 1: page 2 is skipped by hash, and only the new line is judged.
    notes(source, modified="2026-10-01T11:00:00Z", extra=[("1:60", "k60", "TODO renew the licence")])
    source.reads.clear()
    summary = service.collect(source, judge, repo)
    assert source.reads == [("doc-a", "p1")]
    assert summary.pages_skipped == 1 and summary.lines_judged == 1
    assert judge.judged[first_judged:] == ["TODO renew the licence"]


def test_erased_line_marks_its_action_missing(repo):
    source, judge = FakeSource(), FakeJudge()
    notes(source)
    service.collect(source, judge, repo)
    source.add("doc-a", "Weekly sync", "/Meetings", "2026-10-01T12:00:00Z",
               {"p1": [("1:10", "k10", "Agenda for the weekly"), ("1:30", "k30", "@Simon TODO send the deck")],
                "p2": [("1:40", "k40", "Notes only")]})
    summary = service.collect(source, judge, repo)
    assert summary.actions_missing == 1
    assert [i["text"] for i in repo.items()] == ["@Simon TODO send the deck"]


def test_failed_document_is_retried_next_run(repo):
    source, judge = FakeSource(), FakeJudge()
    notes(source)

    def boom(doc, page):
        raise RuntimeError("download failed")

    source.read_page = boom
    summary = service.collect(source, judge, repo)
    assert summary.errors and repo.doc_marker("fake", "doc-a") is None  # not marked as collected



# ---------------------------------------------------------------- core: "new writing only"


def from_now(repo, *doc_ids):
    s = repo.settings()
    s.from_now = sorted(set(s.from_now) | set(doc_ids))
    repo.save_settings(s)


def open_actions(repo):
    return sorted(i["text"] for i in repo.items())


def test_from_now_records_existing_writing_without_reading_it(repo):
    source, judge = FakeSource(), FakeJudge()
    notes(source)
    from_now(repo, "doc-a")
    summary = service.collect(source, judge, repo)
    assert summary.pages_baselined == 2 and summary.pages_read == 0
    assert source.reads == [] and judge.judged == [] and open_actions(repo) == []
    assert repo.doc_marker("fake", "doc-a") == "2026-10-01T10:00:00Z"  # done: next run skips it
    assert service.collect(source, judge, repo).docs_changed == 0


def test_from_now_reads_appended_pages_in_full(repo):
    source, judge = FakeSource(), FakeJudge()
    notes(source)
    from_now(repo, "doc-a")
    service.collect(source, judge, repo)
    page1 = [("1:10", "k10", "Agenda for the weekly"), ("1:20", "k20", "TODO book the retro room"),
             ("1:30", "k30", "@Simon TODO send the deck")]
    source.add("doc-a", "Weekly sync", "/Meetings", "2026-10-02T10:00:00Z",
               {"p1": page1, "p2": [("1:40", "k40", "Notes only")],
                "p3": [("1:60", "k60", "Decided on Friday"), ("1:70", "k70", "TODO email the agenda")]})
    summary = service.collect(source, judge, repo)
    assert source.reads == [("doc-a", "p3")] and summary.pages_baselined == 0
    assert judge.judged == ["Decided on Friday", "TODO email the agenda"]
    assert open_actions(repo) == ["TODO email the agenda"]


def test_from_now_judges_only_new_ink_on_an_old_page(repo):
    source, judge = FakeSource(), FakeJudge()
    notes(source)
    from_now(repo, "doc-a")
    service.collect(source, judge, repo)
    # A new line under the old ones, and a tick added to an old line (its strokes grow).
    notes(source, modified="2026-10-02T10:00:00Z", extra=[("1:35", "k35", "TODO order the cake")])
    service.collect(source, judge, repo)
    assert source.reads == [("doc-a", "p1")]
    assert judge.judged == ["TODO order the cake"]  # the old lines go along as context only
    assert open_actions(repo) == ["TODO order the cake"]

    page1 = [("1:10", "k10", "Agenda for the weekly"), ("1:20", "k20+k21", "TODO book the retro room ✓"),
             ("1:30", "k30", "@Simon TODO send the deck"), ("1:35", "k35", "TODO order the cake")]
    source.add("doc-a", "Weekly sync", "/Meetings", "2026-10-03T10:00:00Z",
               {"p1": page1, "p2": [("1:40", "k40", "Notes only")]})
    service.collect(source, judge, repo)
    assert judge.judged[-1] == "TODO book the retro room ✓"  # an old line with new ink is new writing


def test_from_now_on_a_document_already_read_changes_nothing(repo):
    source, judge = FakeSource(), FakeJudge()
    notes(source)
    service.collect(source, judge, repo)
    from_now(repo, "doc-a")
    source.add("doc-a", "Weekly sync", "/Meetings", "2026-10-02T10:00:00Z",
               {"p1": [("1:20", "k20", "TODO book the retro room")], "p2": [("1:40", "k40", "Notes only")],
                "p3": [("1:70", "k70", "TODO email the agenda")]})
    summary = service.collect(source, judge, repo)
    assert summary.pages_baselined == 0 and ("doc-a", "p3") in source.reads
    assert "TODO email the agenda" in open_actions(repo)


def test_from_now_chosen_mid_read_skips_the_remaining_pages(repo):
    source = FakeSource()
    notes(source)

    class MarkingJudge(FakeJudge):
        def judge(self, doc, page, lines, new):  # the user ticks "new writing only" while page 1 is read
            from_now(repo, "doc-a")
            return super().judge(doc, page, lines, new)

    summary = service.collect(source, MarkingJudge(), repo)
    assert summary.pages_read == 1 and summary.pages_baselined == 1
    assert source.reads == [("doc-a", "p1")]
    assert open_actions(repo) == ["@Simon TODO send the deck", "TODO book the retro room"]


def test_turning_from_now_off_reads_the_skipped_writing(repo):
    source, judge = FakeSource(), FakeJudge()
    notes(source)
    from_now(repo, "doc-a")
    service.collect(source, judge, repo)
    notes(source, modified="2026-10-02T10:00:00Z", extra=[("1:35", "k35", "TODO order the cake")])
    service.collect(source, judge, repo)
    judge.judged.clear()

    s = repo.settings()
    s.from_now = []
    repo.save_settings(s)
    summary = service.collect(source, judge, repo)
    assert summary.pages_read == 2 and repo.baselined_docs() == set()
    assert sorted(judge.judged) == ["@Simon TODO send the deck", "Agenda for the weekly", "Notes only",
                                    "TODO book the retro room"]  # the cake was judged already
    assert open_actions(repo) == ["@Simon TODO send the deck", "TODO book the retro room", "TODO order the cake"]


def test_pending_leaves_out_pages_that_will_only_be_recorded(repo):
    source = FakeSource()
    notes(source)
    from_now(repo, "doc-a")
    assert [(d.id, pages) for d, pages in service.pending(source, repo, fetch=True)] == [("doc-a", [])]

def test_web_edits_win_over_older_paper_changes(repo):
    source, judge = FakeSource(), FakeJudge()
    notes(source)
    service.collect(source, judge, repo)
    action = next(i for i in repo.items() if i["text"] == "TODO book the retro room")
    repo.edit_action(action["id"], text="Book the big retro room", status="done")
    # The paper line is rewritten in an older version of the document: the web edit stays.
    source.add("doc-a", "Weekly sync", "/Meetings", "2026-10-01T09:00:00Z",
               {"p1": [("1:10", "k10", "Agenda for the weekly"), ("1:20", "k21", "TODO book a retro room"),
                       ("1:30", "k30", "@Simon TODO send the deck")], "p2": [("1:40", "k40", "Notes only")]})
    service.collect(source, judge, repo)
    item = next(i for i in repo.items() if i["id"] == action["id"])
    assert item["text"] == "Book the big retro room" and item["status"] == "done"
    assert item["paper_text"] == "TODO book a retro room"


def test_dismissed_actions_leave_the_list(repo):
    source, judge = FakeSource(), FakeJudge()
    notes(source)
    service.collect(source, judge, repo)
    action = repo.items()[0]
    repo.edit_action(action["id"], dismissed=True)
    assert action["id"] not in [i["id"] for i in repo.items()]


def test_filters(repo):
    source, judge = FakeSource(), FakeJudge()
    notes(source)
    service.collect(source, judge, repo)
    assert [i["text"] for i in repo.items(owner="others")] == ["@Simon TODO send the deck"]
    assert [i["text"] for i in repo.items(owner="mine")] == ["TODO book the retro room"]
    assert len(repo.items(folder="/Meetings")) == 2 and repo.items(folder="/Personal") == []


# ---------------------------------------------------------------- core: the To-do document


def test_slots_are_permanent_and_ticks_mark_items_done(repo):
    source, judge, pub = FakeSource(), FakeJudge(), FakePublisher()
    notes(source)
    service.collect(source, judge, repo)
    r = service.sync_todo(repo, pub)
    assert r["published"] and [e.slot for e in pub.published[-1]] == [0, 1]
    first = pub.published[-1][0]

    # Nothing changed: no republish.
    pub.ticks = (set(), "m1")
    assert not service.sync_todo(repo, pub)["published"]

    # A new action takes the next slot; existing ones keep theirs.
    notes(source, modified="2026-10-01T11:00:00Z", extra=[("1:60", "k60", "TODO renew the licence")])
    service.collect(source, judge, repo)
    service.sync_todo(repo, pub)
    assert [(e.text, e.slot) for e in pub.published[-1]][0] == (first.text, 0)
    assert pub.published[-1][-1].slot == 2

    # A tick on slot 0 marks that item done, once.
    pub.ticks = ({0}, "m2")
    r = service.sync_todo(repo, pub)
    assert r["ticked"] == 1 and pub.published[-1][0].done
    item = next(i for i in repo.items() if i["text"] == first.text)
    repo.edit_action(item["id"], status="open")  # re-opened on the web
    assert service.sync_todo(repo, pub)["ticked"] == 0  # the old ink does not tick it again
    assert not next(e for e in pub.published[-1] if e.slot == 0).done


def test_include_others_setting(repo):
    source, judge, pub = FakeSource(), FakeJudge(), FakePublisher()
    notes(source)
    service.collect(source, judge, repo)
    s = repo.settings()
    s.include_others = False
    repo.save_settings(s)
    service.sync_todo(repo, pub)
    assert [e.text for e in pub.published[-1]] == ["TODO book the retro room"]


def test_items_added_on_the_web_are_printed_and_ticked_like_any_other(repo):
    item_id, _ = repo.add_item("- Call the plumber")
    pub = FakePublisher()
    service.sync_todo(repo, pub)
    entry = next(e for e in pub.published[-1] if e.item_id == item_id)
    assert entry.text == "Call the plumber" and entry.source_label == "added in Jotted" and not entry.handwritten
    assert repo.item(item_id)["origin"] == "web" and repo.item(item_id)["owner"] == "me"
    pub.ticks = ({entry.slot}, "m")
    assert service.sync_todo(repo, pub)["ticked"] == 1
    assert repo.item(item_id)["status"] == "done"
    with pytest.raises(ValueError):
        repo.add_item("  - ")


OLD_TASKS_SCHEMA = """
CREATE TABLE notebooks (id TEXT PRIMARY KEY, name TEXT NOT NULL, file_type TEXT NOT NULL);
CREATE TABLE pages (notebook_id TEXT, page_index INTEGER, page_id TEXT, PRIMARY KEY (notebook_id, page_index));
CREATE TABLE tasks (id INTEGER PRIMARY KEY, notebook_id TEXT NOT NULL, origin TEXT NOT NULL, anchor_id TEXT,
    page_index INTEGER, text TEXT NOT NULL, paper_text TEXT, status TEXT NOT NULL DEFAULT 'open',
    text_changed_at TEXT NOT NULL, status_changed_at TEXT NOT NULL, rows TEXT, missing INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE todo_slots (slot INTEGER PRIMARY KEY, kind TEXT NOT NULL, item_id INTEGER NOT NULL,
    ticked INTEGER NOT NULL DEFAULT 0, UNIQUE (kind, item_id));
INSERT INTO notebooks VALUES ('nb', 'Tasks', 'pdf');
INSERT INTO pages VALUES ('nb', 1, 'nbp1');
INSERT INTO tasks VALUES (1, 'nb', 'paper', '1:14', 1, 'call Bob about invoices', 'call Bob', 'open', 't', 't',
    '[[-400, 200, 200, 280]]', 0, 't', 't');
INSERT INTO tasks VALUES (2, 'nb', 'web', NULL, NULL, 'from the web', NULL, 'done', 't', 't', NULL, 0, 't', 't');
INSERT INTO tasks VALUES (3, 'nb', 'paper', '1:99', 1, 'erased', 'erased', 'open', 't', 't', NULL, 1, 't', 't');
INSERT INTO todo_slots VALUES (4, 'task', 1, 0);
"""


def test_tasks_from_the_retired_notebook_become_items_and_keep_their_rows(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(OLD_TASKS_SCHEMA)
    repo = SqliteRepository(path)
    items = {i["text"]: i for i in repo.items()}
    assert set(items) == {"call Bob about invoices", "from the web"}  # the erased task stays behind
    paper, web = items["call Bob about invoices"], items["from the web"]
    assert paper["origin"] == "remarkable" and paper["edited"] and paper["paper_text"] == "call Bob"
    assert paper["source"] == {"doc_id": "nb", "name": "Tasks", "folder": "/", "page": 1, "anchor": "1:14",
                               "kind": "remarkable", "key": "nb:1:14", "title": "Tasks", "url": None, "excerpt": None}
    assert paper["slot"] == 4  # still in its row on the To-do document
    assert repo.source_line("nb", "1:14")["bbox"] == [-400, 200, 200, 280]  # its handwriting can be shown
    assert web["origin"] == "web" and web["status"] == "done"
    SqliteRepository(path)  # once only
    assert len(repo.items()) == 2

    # Watching the old notebook later finds the same line instead of adding it again.
    source, judge = FakeSource(), FakeJudge()
    source.add("nb", "Tasks", "/", "2026-10-02T10:00:00Z", {"nbp1": [("1:14", "k14", "TODO call Bob")]})
    repo.save_settings(Settings(watch=["/"]))
    assert service.collect(source, judge, repo).actions_new == 0
    assert [i["id"] for i in repo.items()].count(paper["id"]) == 1


# ---------------------------------------------------------------- the To-do PDF


def test_todo_pdf_has_fixed_page_count(tmp_path):
    entries = [TodoEntry(i, f"item {i}", i % 2 == 0, "Meetings › Weekly · p1", i) for i in range(45)]
    pdf = todo_document.build_pdf(tmp_path / "To-do.pdf", entries, pages=todo_document.PAGES)
    import re

    assert len(re.findall(rb"/Type /Page[^s]", pdf.read_bytes())) == todo_document.PAGES


def test_tick_on_a_checkbox_is_read_back():
    scale = TemplateConfig().scale
    x0, y0, x1, y1 = todo_document.slot_box(3)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2  # pt from the top-left

    def to_tablet(x_pt, y_pt):
        return ((x_pt * page.UNITS_PER_PT) - page.RM_W / 2) * scale, y_pt * page.UNITS_PER_PT * scale

    a, b = to_tablet(cx - 4, cy), to_tablet(cx + 5, cy + 4)
    tick = make_stroke("1:1", "fineliner", [a, to_tablet(cx, cy + 3), b])
    far = make_stroke("1:2", "fineliner", [to_tablet(200, cy), to_tablet(260, cy)])  # in the text, not the box
    assert todo_document.ticked_rows([tick, far], scale) == {3}


# ---------------------------------------------------------------- web endpoints


def test_todo_endpoints(tmp_path, monkeypatch):
    text = config.EXAMPLE.read_text().replace('db   = "./data/jotted.db"', f'db = "{tmp_path}/db.sqlite"')
    path = tmp_path / "config.toml"
    path.write_text(text)
    monkeypatch.setenv(config.ENV_VAR, str(path))
    cfg = config.load()

    from jotted.server import create_app

    repo = SqliteRepository(cfg.server.db)
    repo.save_settings(Settings(watch=["/Meetings"]))
    source, judge = FakeSource(), FakeJudge()
    notes(source)
    service.collect(source, judge, repo)

    with repo.db() as db:  # line boxes for the source images
        db.execute("UPDATE source_lines SET bbox = '[0, 100, 400, 140]'")
    app = fake_app(cfg, repo, source, judge, strokes=[make_stroke("1:20", "fineliner", [(0, 100), (400, 140)])])
    flask = create_app(cfg, app_=app, background=False)
    client = flask.test_client()
    client.environ_base["HTTP_X_JOTTED_TOKEN"] = flask.config["token"]

    data = client.get("/api/todo").get_json()
    assert {i["text"] for i in data["items"]} == {"TODO book the retro room", "@Simon TODO send the deck"}
    assert client.get("/api/todo?owner=others").get_json()["items"][0]["owner"] == "someone_else"

    action = next(i for i in data["items"] if i["owner"] == "me")
    r = client.patch(f"/api/items/{action['id']}", json={"status": "done"}).get_json()
    assert next(i for i in r["items"] if i["id"] == action["id"])["status"] == "done"
    assert client.patch("/api/items/999", json={}).status_code == 404

    r = client.post("/api/items", json={"text": "Renew the passport"})
    assert r.status_code == 201
    added = r.get_json()["item"]
    assert added["origin"] == "web" and added["text"] == "Renew the passport"
    assert added["id"] in [i["id"] for i in r.get_json()["items"]]
    assert client.post("/api/items", json={"text": "   "}).status_code == 400
    assert client.post("/api/items", json={}).status_code == 400
    assert client.patch(f"/api/items/{added['id']}", json={"dismissed": True}).status_code == 200
    assert added["id"] not in [i["id"] for i in client.get("/api/todo").get_json()["items"]]

    line = client.get(f"/api/sources/doc-a/line/{action['source']['anchor']}.svg")
    assert line.status_code == 200 and line.data.startswith(b"<svg")
    page = client.get(f"/api/sources/doc-a/1/preview.svg?anchor={action['source']['anchor']}")
    assert page.status_code == 200 and b"#ffe066" in page.data  # the highlight

    assert client.put("/api/settings", json={"action_threshold": 2}).status_code == 400
    assert client.put("/api/settings", json={"bogus": 1}).status_code == 400
    s = client.put("/api/settings", json={"watch": ["Meetings/", "/Work"], "todo_enabled": True}).get_json()
    assert s["watch"] == ["/Meetings", "/Work"] and s["todo_enabled"]
    assert client.put("/api/settings", json={"from_now": "doc-a"}).status_code == 400
    assert client.put("/api/settings", json={"from_now": ["doc-b", "doc-a", "doc-a"]}).get_json()["from_now"] == \
        ["doc-a", "doc-b"]


# ---------------------------------------------------------------- writing on the To-do document


def written(slot, text, box_inked=False):
    return WrittenItem(slot=slot, page_id="tp1", page_index=1, text=text, anchor=f"9:{slot}", key=f"k{slot}",
                       bbox=(-500, 1000, -100, 1060), box_inked=box_inked)


def test_writing_in_an_empty_row_adds_an_item_in_that_row(repo):
    source, judge, pub = FakeSource(), FakeJudge(), FakePublisher()
    notes(source)
    service.collect(source, judge, repo)
    service.sync_todo(repo, pub)  # slots 0 and 1
    pub.ticks = (set(), "m1")
    pub.written = [written(2, "- Book flights to Sydney")]
    r = service.sync_todo(repo, pub)
    assert r["written"] == 1 and r["published"]
    entry = next(e for e in pub.published[-1] if e.slot == 2)
    assert entry.handwritten and entry.text == "Book flights to Sydney"  # bullet stripped
    assert entry.ink == (-500, 1000, -100, 1060)
    item = next(i for i in repo.items() if i["text"] == "Book flights to Sydney")
    assert item["owner"] == "me" and item["written"] and item["source"]["name"] == "To-do"
    assert repo.source_line("todo-doc", "9:2") is not None  # its handwriting image works

    # Read again: the row is now occupied, so nothing is added twice.
    assert service.sync_todo(repo, pub)["written"] == 0 and 2 in pub.occupied_seen

    # A newly collected action takes the next free slot, never the written one.
    notes(source, modified="2026-10-01T11:00:00Z", extra=[("1:60", "k60", "TODO renew the licence")])
    service.collect(source, judge, repo)
    service.sync_todo(repo, pub)
    assert next(e for e in pub.published[-1] if e.text == "TODO renew the licence").slot == 3


def test_a_box_drawn_with_a_new_item_is_not_a_tick(repo):
    pub = FakePublisher()
    pub.ticks = ({0}, "m1")
    pub.written = [written(0, "Call the bank", box_inked=True)]
    r = service.sync_todo(repo, pub)
    assert r["written"] == 1 and r["ticked"] == 0
    item = next(i for i in repo.items() if i["text"] == "Call the bank")
    assert item["status"] == "open"


def test_done_and_edited_handwritten_items(tmp_path, repo):
    pub = FakePublisher()
    pub.ticks = (set(), "m1")
    pub.written = [written(0, "Call the bank")]
    service.sync_todo(repo, pub)
    item = next(i for i in repo.items() if i["text"] == "Call the bank")
    repo.edit_action(item["id"], text="Call the bank about the card", status="done")
    service.sync_todo(repo, pub)
    e = pub.published[-1][0]
    assert e.handwritten and e.done and e.edited and e.text == "Call the bank about the card"
    pdf = todo_document.build_pdf(tmp_path / "t.pdf", pub.published[-1], scale=1.0525)
    assert pdf.read_bytes().startswith(b"%PDF")


def test_written_rows_groups_handwriting_by_row():
    scale = TemplateConfig().scale
    lines_cfg = config.LinesConfig()

    def tablet(x_pt, y_pt):
        return ((x_pt * page.UNITS_PER_PT) - page.RM_W / 2) * scale, y_pt * page.UNITS_PER_PT * scale

    top = todo_document.TOP + 4 * todo_document.ROW  # row 4
    letters = [make_stroke(f"1:{i}", "fineliner", [tablet(60 + 18 * i, top + 16), tablet(68 + 18 * i, top + 6),
                                                   tablet(74 + 18 * i, top + 17)]) for i in range(5)]
    descender = make_stroke("1:9", "fineliner", [tablet(80, top + 10), tablet(80, top + 30)])  # crosses into row 5
    box_tick = make_stroke("1:20", "fineliner", [tablet(30, top + 9), tablet(34, top + 13), tablet(38, top + 6)])
    rows = todo_document.written_rows(letters + [descender, box_tick], scale, lines_cfg)
    assert list(rows) == [4] and len(rows[4]) == 6  # the tick in the box is not writing


def test_a_deleted_todo_document_starts_again_from_the_top(repo):
    source, judge, pub = FakeSource(), FakeJudge(), FakePublisher()
    pub.ticks = (set(), "m1")
    pub.written = [written(5, "Call the bank")]
    notes(source)
    service.collect(source, judge, repo)
    service.sync_todo(repo, pub)  # written item in row 5; collected ones in the first free rows
    assert [e.slot for e in pub.published[-1]] == [0, 1, 5]
    assert pub.published[-1][-1].handwritten

    # Deleted on the tablet: the next document is printed from row 0, handwriting as text.
    pub.ticks, pub.written = None, []
    r = service.sync_todo(repo, pub)
    assert r["published"] and [e.slot for e in pub.published[-1]] == [0, 1, 2]
    bank = next(e for e in pub.published[-1] if e.text == "Call the bank")
    assert not bank.handwritten and bank.ink is None and bank.source_label == "written on an earlier To-do"

    # The new document is read as ours from then on: a tick on row 0 counts.
    pub.ticks = ({0}, "m2")
    assert service.sync_todo(repo, pub)["ticked"] == 1


def test_a_replaced_todo_document_is_not_read_with_the_old_layout(repo):
    pub = FakePublisher()
    pub.ticks = (set(), "m1")
    pub.written = [written(3, "Call the bank")]
    service.sync_todo(repo, pub)
    assert repo.todo_doc_id() == "todo-doc"
    repo.set_todo_doc_id("older-doc")  # as if the document found now isn't the one we laid out
    pub.ticks = ({3}, "m2")
    r = service.sync_todo(repo, pub)
    assert r["ticked"] == 0 and r["published"] and pub.published[-1][0].slot == 0


def test_new_items_take_the_first_rows_without_ink(repo):
    source, judge, pub = FakeSource(), FakeJudge(), FakePublisher()
    pub.ticks, pub.ink = (set(), "m1"), {0, 2}  # an old scribble in rows 0 and 2
    notes(source)
    service.collect(source, judge, repo)
    service.sync_todo(repo, pub)
    assert [e.slot for e in pub.published[-1]] == [1, 3]


def test_done_items_on_clean_rows_make_room_for_new_ones(repo):
    source, judge, pub = FakeSource(), FakeJudge(), FakePublisher(capacity=3)
    pub.ticks = (set(), "m1")
    notes(source)
    service.collect(source, judge, repo)
    service.sync_todo(repo, pub)  # rows 0 and 1
    first = next(i for i in repo.items() if i["source"].get("anchor") == "1:20")
    repo.edit_action(first["id"], status="done")  # done on the web: its row has no ink

    # One new item: row 2 is still free, so the done item stays, struck through.
    notes(source, modified="2026-10-01T11:00:00Z", extra=[("1:60", "k60", "TODO renew the licence")])
    service.collect(source, judge, repo)
    service.sync_todo(repo, pub)
    assert [(e.slot, e.done) for e in pub.published[-1]] == [(0, True), (1, False), (2, False)]

    # Another: the rows are full, so the done item leaves and its row is reused.
    notes(source, modified="2026-10-01T12:00:00Z",
          extra=[("1:60", "k60", "TODO renew the licence"), ("1:70", "k70", "TODO pay the invoice")])
    service.collect(source, judge, repo)
    r = service.sync_todo(repo, pub)
    assert not r["rebuilt"] and pub.deleted == 0
    assert [(e.slot, e.text) for e in pub.published[-1]][0] == (0, "TODO pay the invoice")


def test_full_document_is_rebuilt_with_the_open_items(repo):
    source, judge, pub = FakeSource(), FakeJudge(), FakePublisher(capacity=2)
    pub.ticks = (set(), "m1")
    notes(source)
    service.collect(source, judge, repo)
    service.sync_todo(repo, pub)  # rows 0 and 1
    pub.ticks = ({0}, "m2")  # ticked on paper: done, and the row has ink
    service.sync_todo(repo, pub)

    notes(source, modified="2026-10-01T11:00:00Z", extra=[("1:60", "k60", "TODO renew the licence")])
    service.collect(source, judge, repo)
    r = service.sync_todo(repo, pub)
    assert r["rebuilt"] and r["published"] and pub.deleted == 1
    assert [(e.slot, e.done) for e in pub.published[-1]] == [(0, False), (1, False)]
    assert "TODO renew the licence" in {e.text for e in pub.published[-1]}


def test_a_document_with_another_page_count_is_rebuilt(repo):
    source, judge, pub = FakeSource(), FakeJudge(), FakePublisher(capacity=10)
    pub.ticks, pub.pages_capacity = (set(), "m1"), 400  # an old 20-page To-do
    notes(source)
    service.collect(source, judge, repo)
    r = service.sync_todo(repo, pub)
    assert r["rebuilt"] and pub.deleted == 1 and [e.slot for e in pub.published[-1]] == [0, 1]
    pub.ticks = (set(), "m2")  # the new one, not yet opened on the tablet
    assert not service.sync_todo(repo, pub)["rebuilt"]


def test_fresh_rebuilds_with_rows_left_after_reading_the_paper(repo):
    source, judge, pub = FakeSource(), FakeJudge(), FakePublisher(capacity=10)
    pub.ticks = (set(), "m1")
    notes(source)
    service.collect(source, judge, repo)
    service.sync_todo(repo, pub)  # rows 0 and 1
    done_on_web = next(i for i in repo.items() if i["source"].get("anchor") == "1:30")
    repo.edit_action(done_on_web["id"], status="done")
    service.sync_todo(repo, pub)  # struck through in row 1; plenty of rows left
    ticked = next(e for e in pub.published[-1] if e.slot == 0)

    # Ticked and written on paper since: both count before the rebuild.
    pub.ticks, pub.written = ({0}, "m2"), [written(4, "Call the bank")]
    r = service.sync_todo(repo, pub, fresh=True)
    assert r["ticked"] == 1 and r["written"] == 1
    assert r["rebuilt"] and r["published"] and pub.deleted == 1
    assert repo.item(ticked.item_id)["status"] == "done" and repo.item(done_on_web["id"])["status"] == "done"
    assert [(e.slot, e.text, e.done) for e in pub.published[-1]] == [(0, "Call the bank", False)]
    assert r["items"] == 1


def test_fresh_with_no_document_yet_just_publishes(repo):
    source, judge, pub = FakeSource(), FakeJudge(), FakePublisher()
    notes(source)
    service.collect(source, judge, repo)
    r = service.sync_todo(repo, pub, fresh=True)
    assert r["published"] and not r["rebuilt"] and pub.deleted == 0
    assert [e.slot for e in pub.published[-1]] == [0, 1]


def test_todo_sync_fresh_through_the_fast_path(tmp_path, monkeypatch):
    text = config.EXAMPLE.read_text().replace('db   = "./data/jotted.db"', f'db = "{tmp_path}/db.sqlite"')
    path = tmp_path / "config.toml"
    path.write_text(text)
    monkeypatch.setenv(config.ENV_VAR, str(path))
    cfg = config.load()

    from jotted.server import create_app

    repo = SqliteRepository(cfg.server.db)
    repo.save_settings(Settings(watch=["/Meetings"]))
    source, judge, pub = FakeSource(), FakeJudge(), FakePublisher()
    notes(source)
    service.collect(source, judge, repo)
    flask = create_app(cfg, app_=fake_app(cfg, repo, source, judge, publisher=pub), background=False)
    client = flask.test_client()
    client.environ_base["HTTP_X_JOTTED_TOKEN"] = flask.config["token"]

    off = client.post("/api/op/todo.sync", json={"fresh": True}).get_json()
    assert off["error"]["code"] == "invalid"  # the To-do document is off
    client.put("/api/settings", json={"todo_enabled": True})
    pub.ticks = (set(), "m1")  # a document on the tablet already
    since = repo.events()[-1]["cursor"]
    r = client.post("/api/op/todo.sync", json={"fresh": True}).get_json()
    assert r["ok"] and r["data"]["rebuilt"] and r["data"]["published"] and pub.deleted == 1
    assert "todo.published" in [e["type"] for e in repo.events(since)]


def _entries(*docs):
    from jotted.plugins.remarkable import cloud

    return [cloud.LibraryEntry(cloud.DocRef(id=i, name=n, version=0, modified=m, parent=""), folder)
            for i, n, folder, m in docs], []


def test_two_todo_documents_with_the_same_name_stop_with_advice(monkeypatch):
    from jotted.plugins.remarkable import cloud

    both = _entries(("a", "To-do", "/", "2026-10-01T14:54:55Z"), ("b", "To-do", "/", "2026-10-01T14:55:17Z"))
    monkeypatch.setattr(cloud, "library", lambda cfg: both)
    doc = todo_document.TodoDocument(None, "To-do", "/", ink=None)
    with pytest.raises(cloud.CloudError, match="Delete the one you don't use"):
        doc.find()
    monkeypatch.setattr(cloud, "library", lambda cfg: (both[0][:1], []))
    assert doc.find().id == "a"


@pytest.fixture
def tablet(tmp_path, monkeypatch):
    """A fake reMarkable cloud holding one document called To-do, of whatever kind a test puts there."""
    import types
    import zipfile

    from jotted.plugins.remarkable import cloud

    cfg = types.SimpleNamespace(paths=types.SimpleNamespace(cache_dir=tmp_path),
                                template=TemplateConfig(), strokes=None)
    state = {"docs": [], "files": {}, "deleted": [], "uploads": []}

    def put(doc_id, folder, kind):
        path = tmp_path / f"remote-{doc_id}.rmdoc"
        if kind == "notebook":
            synth.rmdoc(path, [synth.rm_bytes([[(0, 0), (40, 40)]])], doc_id=doc_id, name="To-do")
        else:
            with zipfile.ZipFile(path, "w") as z:
                z.writestr(f"{doc_id}.metadata", "{}")  # all an interrupted upload leaves behind
                if kind == "pdf":
                    z.writestr(f"{doc_id}.content", '{"fileType": "pdf", "pages": []}')
                    z.writestr(f"{doc_id}.pdf", b"%PDF-1.4")
        state["docs"].append(("id-" + doc_id, "To-do", folder, "2026-10-01T14:54:55Z"))
        state["files"]["id-" + doc_id] = path

    monkeypatch.setattr(cloud, "library", lambda c: _entries(*state["docs"]))
    monkeypatch.setattr(cloud, "download", lambda c, ref: state["files"][ref.id])
    monkeypatch.setattr(cloud, "delete", lambda c, name, folder: state["deleted"].append((name, folder))
                        or state["docs"].clear())
    monkeypatch.setattr(cloud, "upload_pdf", lambda c, pdf, content_only, folder: state["uploads"].append(
        (pdf.name, content_only, folder)))
    state["put"] = put
    state["doc"] = todo_document.TodoDocument(cfg, "To-do", "/", ink=None)
    return state


def test_an_empty_todo_shell_is_replaced_with_a_fresh_document(tablet):
    tablet["put"]("shell", "/", "empty")
    assert tablet["doc"].read_paper(set()) is None  # nothing to read in it
    tablet["doc"].publish([])
    assert tablet["deleted"] == [("To-do", "/")]
    assert tablet["uploads"] == [("To-do.pdf", False, "/")]  # created anew, not swapped into the shell


def test_a_printed_todo_document_has_its_pdf_swapped(tablet):
    tablet["put"]("ours", "/", "pdf")
    tablet["doc"].publish([])
    assert tablet["deleted"] == [] and tablet["uploads"] == [("To-do.pdf", True, "/")]


def test_a_notebook_of_yours_called_todo_is_never_touched(tablet):
    from jotted.plugins.remarkable import cloud

    tablet["put"]("mine", "/", "notebook")
    with pytest.raises(cloud.CloudError, match="is a notebook, not Jotted's printed list"):
        tablet["doc"].publish([])
    with pytest.raises(cloud.CloudError, match="Rename it on the tablet"):
        tablet["doc"].read_paper(set())
    assert tablet["deleted"] == [] and tablet["uploads"] == []


def test_a_todo_in_another_folder_is_not_the_todo_document(tablet):
    tablet["put"]("elsewhere", "/Archive", "notebook")
    assert tablet["doc"].find() is None
    tablet["doc"].publish([])
    assert tablet["uploads"] == [("To-do.pdf", False, "/")]


def test_two_apps_starting_at_once_migrate_once(tmp_path):
    import sqlite3
    import threading

    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(OLD_TASKS_SCHEMA)
    errors = []

    def start():
        try:
            SqliteRepository(path)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=start) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(SqliteRepository(path).items()) == 2
