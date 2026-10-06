"""Core services: collect actions from sources, and keep the To-do document in step.

Reading is incremental at three levels:
1. documents whose change marker is unchanged are skipped (no download);
2. pages whose content hash is unchanged are skipped (no parsing);
3. on a changed page, only lines whose key changed are judged.

A document marked "from now" (`Settings.from_now`) starts with a baseline instead: until it is
first fully collected, every page not yet read has its marks recorded without being read.
Afterwards it is collected like any other document, except that a line made only of baseline
marks is never judged. So pages added later are read in full, and on an old page only lines
with new ink are judged (the old lines are still sent along as context).
"""

from __future__ import annotations

import logging

from .model import CollectSummary, DocInfo
from .ports import ActionJudge, DocumentSource, Progress, Repository, TodoPublisher

log = logging.getLogger("jotted.core")


def _quiet(_: str) -> None:
    pass


def collect(source: DocumentSource, judge: ActionJudge, repo: Repository,
            exclude: set[str] | None = None, progress: Progress = _quiet) -> CollectSummary:
    """Read what changed in the watched documents and update the action items."""
    settings = repo.settings()
    summary = CollectSummary()
    exclude = exclude or set()
    if not settings.watch:
        return summary
    docs = [d for d in source.list_documents() if settings.watches(d) and d.id not in exclude]
    summary.docs_seen = len(docs)
    baselined = repo.baselined_docs()
    for doc in docs:
        if doc.id in baselined and doc.id not in settings.from_now:
            repo.clear_baseline(doc.id)  # "from now" was turned off: read the earlier writing too
        marker = repo.doc_marker(source.name, doc.id)
        if marker == doc.modified:
            continue
        summary.docs_changed += 1
        progress(f"Reading {doc.path}")
        try:
            _collect_doc(source, judge, repo, doc, settings.action_threshold, summary, progress,
                         first=marker is None)
        except Exception as e:  # one bad document must not stop the rest
            log.exception("collecting %s failed", doc.path)
            summary.errors.append(f"{doc.path}: {e}")
    return summary


def _collect_doc(source: DocumentSource, judge: ActionJudge, repo: Repository, doc: DocInfo,
                 threshold: float, summary: CollectSummary, progress: Progress = _quiet,
                 first: bool = False) -> None:
    """first: the document has never been fully collected. If it is marked "from now", pages
    not read yet become baseline pages instead of being read. Settings are checked per page,
    so marking a large document while it is being read takes effect at its next page."""
    pages = source.pages(doc)
    for page in pages:
        stored = repo.page_hash(doc.id, page.id)
        if stored == page.content_hash:
            summary.pages_skipped += 1
            continue
        if first and stored is None and doc.id in repo.settings().from_now:
            progress(f"Recording existing writing in {doc.path} (page {page.index} of {len(pages)})")
            repo.save_baseline(doc, page, source.mark_ids(doc, page))
            summary.pages_baselined += 1
            continue
        progress(f"Reading {doc.path} (page {page.index} of {len(pages)})")
        lines = source.read_page(doc, page)
        known = repo.line_keys(doc.id, page.id)
        baseline = repo.baseline_marks(doc.id, page.id)
        new = [ln for ln in lines if known.get(ln.anchor) != ln.key and ln.text and not ln.drawing
               and (baseline is None or not set(ln.marks) <= baseline)]
        judgments = judge.judge(doc, page, lines, new) if new else {}
        added, updated, missing = repo.save_page(doc, page, lines, judgments, threshold)
        summary.pages_read += 1
        summary.lines_judged += len(new)
        summary.actions_new += added
        summary.actions_updated += updated
        summary.actions_missing += missing
    # Only now: a failure part-way leaves the old marker, so the next run retries this document.
    repo.save_doc(doc, len(pages))


def sync_todo(repo: Repository, publisher: TodoPublisher, force: bool = False, fresh: bool = False) -> dict:
    """Read the To-do document, then republish it if anything changed.

    Paper is read before publishing, so nothing done on paper is lost to a republish:
    1. new items written by hand in empty rows are added, in the rows they were written in;
    2. ticks mark items done;
    3. open items without a row get the first free rows with no ink (permanent), and the list is printed.

    Ink on the device is never erased, so a row with ink is never reused. When the rows run out,
    or the document has a different number of pages, it is deleted and rebuilt: a fresh document
    with the open items only. A document deleted on the device, or replaced by another one, is
    started afresh the same way.

    `fresh` rebuilds it now, rows left or not, so done items leave the paper (they stay done in
    the store). Paper is still read first. With no document yet, it is just published.
    """
    ticked = written = 0
    read = publisher.read_paper(repo.occupied_slots())
    known = repo.todo_doc_id()
    if read is None or (known is not None and read.doc_id != known):
        if known is not None or repo.occupied_slots():
            log.info("the To-do document is new: printing the list from the top")
        repo.reset_todo()
        read = None  # a replacement's ink was laid out by someone else: don't read it as ours
    else:
        repo.set_todo_doc_id(read.doc_id)
        written = repo.add_written(read.doc_id, read.written)
        ticked = repo.apply_ticks(read.ticks, read.marker)
    include_others = repo.settings().include_others
    capacity = publisher.capacity()
    rebuilt = read is not None and (fresh or (read.capacity is not None and read.capacity != capacity))
    if not rebuilt:
        entries, unplaced = repo.assign_slots(repo.todo_entries(include_others), capacity,
                                              read.inked if read else set())
        rebuilt = unplaced > 0 and read is not None  # an empty document can't fit more than this
    if rebuilt:
        log.info("rebuilding the To-do document with the open items only")
        publisher.delete()
        repo.reset_todo()
        entries, unplaced = repo.assign_slots(repo.todo_entries(include_others), capacity, set())
    if unplaced:
        log.warning("%d item(s) do not fit on the To-do document", unplaced)
    published = False
    if force or read is None or rebuilt or repo.todo_needs_publish(entries):
        publisher.publish(entries)
        repo.mark_todo_published(entries)
        published = True
    return {"ticked": ticked, "written": written, "published": published, "items": len(entries),
            "overflow": unplaced, "rebuilt": rebuilt}


def pending(source: DocumentSource, repo: Repository, exclude: set[str] | None = None,
            fetch: bool = False) -> list[tuple[DocInfo, list]]:
    """What a collect would read: changed documents and, with `fetch`, their changed pages.

    Nothing is transcribed, judged or stored. Without `fetch` nothing is downloaded either.
    """
    settings = repo.settings()
    exclude = exclude or set()
    out = []
    for doc in source.list_documents():
        marker = repo.doc_marker(source.name, doc.id)
        if not settings.watches(doc) or doc.id in exclude or marker == doc.modified:
            continue
        baseline_only = marker is None and doc.id in settings.from_now  # unread pages are only recorded
        pages = [p for p in source.pages(doc) if (h := repo.page_hash(doc.id, p.id)) != p.content_hash
                 and not (baseline_only and h is None)] if fetch else []
        out.append((doc, pages))
    return out
