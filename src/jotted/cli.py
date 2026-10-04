"""Jotted command line: the product's one public interface, over `api.Jotted`.

Every operation is a command here (a test keeps it so), and everything else (the desktop
app, `jotted mcp`, scripts) is a wrapper that runs `jotted --json …` and reads the
envelope `jotted.contract` defines. With --json, stdout carries exactly one JSON document;
logs and progress go to stderr. Prompts appear only in a terminal without --json; every
secret can come on stdin (--stdin) instead.

Most commands are one operation (`Call`): when `jotted serve` is running they are handed
to it (`jotted.fastpath`), else run here; the output is the same either way. Commands only
parse arguments and show results; the work happens in `jotted.api`.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from rich.console import Console
from rich.markup import escape

from . import contract
from .config import Config
from .contract import UsageError
from .ui import can_prompt, read_secret

log = logging.getLogger("jotted")
console = Console()
errors = Console(stderr=True)

BUNDLED_VAR = contract.BUNDLED_VAR  # set by the desktop app: it updates its own copy


def _setup_logging(level: str) -> None:
    from rich.logging import RichHandler

    logging.basicConfig(
        level=level.upper(),
        format="%(message)s",
        handlers=[RichHandler(console=errors, show_time=False, show_path=False)],
        force=True,
    )
    if level.upper() != "DEBUG":  # per-request lines from the HTTP clients are noise at INFO
        for name in ("httpx", "httpx2", "anthropic", "typesafe_sdk"):
            logging.getLogger(name).setLevel(logging.WARNING)
        # rmscene repeats "data not read" for every page from newer firmware; known and harmless.
        logging.getLogger("rmscene").setLevel(logging.ERROR)


# ---------------------------------------------------------------- what a command returns


@dataclass
class Call:
    """A command that is one api operation, run here or by a running `jotted serve`."""
    op: str
    kwargs: dict = field(default_factory=dict)
    render: Callable[[Any], None] | None = None  # show the result to a person
    then: Callable[[Any], Any] | None = None  # the operation's result -> the command's (runs here)
    status: str | None = None  # spinner text while it runs here; also turns on progress


@dataclass
class Done:
    """A command that did its work itself (setup, version, schema...)."""
    data: Any
    render: Callable[[Any], None] | None = None


def uses(*ops: str):
    """Mark a command with the api operations it offers (checked by the parity test)."""
    def mark(fn):
        fn.operations = ops
        return fn
    return mark


def _jotted(cfg: Config):
    from .api import Jotted

    return Jotted.open(cfg)


def _progress(args: argparse.Namespace, status) -> Callable[[str], None]:
    if args.json:
        return lambda m: print(json.dumps({"v": contract.CONTRACT, "progress": m}), file=sys.stderr, flush=True)
    return lambda m: status.update(escape(m)) if status else None


def _run_call(cfg: Config, args: argparse.Namespace, call: Call) -> Any:
    envelope = None
    if not args.local:
        from . import fastpath

        envelope = fastpath.send(cfg, call.op, call.kwargs)
    if envelope is not None:
        if not envelope.get("ok"):
            raise contract.Failed(envelope)
        data = envelope["data"]
    else:
        from .api import OPERATIONS

        jotted = _jotted(cfg)
        fn = getattr(jotted, OPERATIONS[call.op])
        if call.status and not args.json:
            with console.status(call.status) as status:
                data = fn(**call.kwargs, **({"progress": _progress(args, status)} if call.op == "collect" else {}))
        else:
            data = fn(**call.kwargs, **({"progress": _progress(args, None)} if call.op == "collect" else {}))
    return call.then(data) if call.then else data


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


# ---------------------------------------------------------------- showing results


def _label(item: dict) -> str:
    src = item["source"]
    if item["origin"] == "web":
        return "added here"
    if item["origin"] == "agent":
        return " › ".join(p for p in (src.get("kind"), src.get("title")) if p) or "added by an agent"
    if item["written"]:
        return f"{src['name']} · p{src['page']}"
    return f"{(src['folder'] or '/').strip('/') or 'Library'} › {src['name']} · p{src['page']}"


def _items_table(items: list[dict]) -> None:
    from rich.table import Table

    if not items:
        console.print("[dim]Nothing here.[/dim]")
        return
    table = Table(box=None, pad_edge=False, padding=(0, 2, 0, 0))
    for col in ("id", "", "owner", "item", "from"):
        table.add_column(col)
    colours = {"me": "green", "someone_else": "cyan", "unclear": "yellow"}
    marks = {"done": "✓", "proposed": "?", "dismissed": "✗"}
    for i in items:
        owner = i.get("owner_name") or i["owner"]
        table.add_row(str(i["id"]), marks.get(i["status"], "·"),
                      f"[{colours.get(i['owner'], 'white')}]{escape(owner)}[/]", escape(i["text"]),
                      f"[dim]{escape(_label(i))}[/dim]")
    console.print(table)


def _show_item(item: dict) -> None:
    console.print(f"#{item['id']} [{item['status']}] {escape(item['text'])}  [dim]{escape(_label(item))}[/dim]")
    if item["source"].get("excerpt"):
        console.print(f"  [dim]“{escape(item['source']['excerpt'])}”[/dim]")
    if item["source"].get("url"):
        console.print(f"  [dim]{escape(item['source']['url'])}[/dim]")


def _show_settings(settings: dict) -> None:
    width = max(len(k) for k in settings)
    for k, v in settings.items():
        console.print(f"{k:<{width}}  {json.dumps(v)}", highlight=False)


def _show_ai(ai: dict) -> None:
    m, j = ai["llm"], ai["jev"]
    key = m["key"]
    console.print(f"LLM:  {m['family']} by {m['label']} ({m['model']}), adapter “{m['provider']}”; key "
                  + (f"{key['hint']} ({key['source']})" if key["set"] else "[yellow]not set[/yellow]"))
    console.print(f"Jev:  {'on (' + j['model'] + '), judging actions and owners' if j['enabled'] else 'off'}")
    console.print(f"Judging actions: {'Jev' if ai['judge'] == 'jev' else m['family']}")


def _show_steps(status: dict) -> None:
    for s in status["steps"]:
        mark = "[green]✓[/green]" if s["done"] else ("[dim]·[/dim]" if s.get("optional") else "[yellow]✗[/yellow]")
        detail = f" [dim]{escape(s['detail'])}[/dim]" if s.get("detail") else ""
        todo = f"  → jotted {escape(s['command'])}" if not s["done"] and s.get("command") else ""
        console.print(f"{mark} {escape(s['title'])}{' (optional)' if s.get('optional') else ''}{detail}{todo}")
    console.print("\n[green]Set up.[/green]" if status["complete"] else "\n[yellow]Not set up yet.[/yellow]")


# ---------------------------------------------------------------- the to-do list


def _source_args(args: argparse.Namespace) -> dict | None:
    source = {"kind": args.source_kind, "key": args.source_key, "title": args.source_title,
              "url": args.source_url, "excerpt": args.excerpt}
    return {k: v for k, v in source.items() if v is not None} or None


def _batch(args: argparse.Namespace) -> list:
    """The JSON array of items on standard input."""
    if not args.stdin:
        raise UsageError("`jotted items add-batch` reads a JSON array of items from standard input: pass --stdin")
    try:
        items = json.loads(sys.stdin.read() or "null")
    except ValueError as e:
        raise contract.Error("invalid", f"standard input isn't JSON: {e}") from e
    if not isinstance(items, list):
        raise contract.Error("invalid", "standard input must be a JSON array of items")
    return items


@uses("items.list", "items.get", "items.add", "items.add_batch", "items.accept", "items.edit")
def cmd_items(cfg: Config, args: argparse.Namespace) -> Call:
    action = args.items_command or "list"
    if action == "list":
        return Call("items.list", {"status": None if args.status == "all" else args.status, "owner": args.owner,
                                   "folder": args.folder, "source_kind": args.source_kind,
                                   "source_key": args.source_key, "query": args.query, "limit": args.limit,
                                   "cursor": args.cursor}, _items_table)
    if action == "get":
        return Call("items.get", {"item_id": args.id}, _show_item)
    if action == "add":
        def added(item: dict) -> None:
            if not item["created"]:
                console.print(f"Already there: #{item['id']} [{item['status']}] {escape(item['text'])}")
            elif item["status"] == "proposed":
                console.print(f"Proposed #{item['id']}: {escape(item['text'])}. [dim]Accept it with "
                              f"`jotted items accept {item['id']}`.[/dim]")
            else:
                console.print(f"Added #{item['id']}: {escape(item['text'])}. [dim]It reaches the To-do document "
                              "on the next check (`jotted todo` now).[/dim]")
        return Call("items.add", {"text": " ".join(args.text), "owner": args.owner, "owner_name": args.owner_name,
                                  "source": _source_args(args), "propose": args.propose, "agent": args.agent}, added)
    if action == "add-batch":
        return Call("items.add_batch", {"items": _batch(args), "propose": args.propose, "agent": args.agent},
                    lambda r: console.print("\n".join(
                        f"{x['index']}: {x['outcome']}" + (f" #{x['id']}" if "id" in x else f" ({x['error']['message']})")
                        for x in r["results"]) or "Nothing to add."))
    if action == "accept":
        if args.all and args.ids:
            raise UsageError("`jotted items accept`: name items or pass --all, not both")
        return Call("items.accept", {"ids": args.ids or None, "every": args.all, "source_kind": args.source_kind},
                    lambda r: console.print(
                        f"Accepted {len(r['accepted'])}" + "".join(f"; #{x['id']} skipped ({x['reason']})"
                                                                  for x in r["skipped"])))
    if action == "edit":
        text = " ".join(args.text) or None
        if text is None and args.owner is None and args.owner_name is None:
            raise UsageError("`jotted items edit`: give new text, --owner or --owner-name")
        changes = {"text": text, "owner": args.owner, "owner_name": args.owner_name}
    else:
        changes = {"done": {"status": "done"}, "reopen": {"status": "open"}, "dismiss": {"dismissed": True}}[action]
    return Call("items.edit", {"item_id": args.id, **changes}, lambda item: console.print(
        f"#{args.id} removed from the list." if action == "dismiss" else
        f"#{item['id']} {'✓ ' if item['status'] == 'done' else ''}{escape(item['text'])}"))


# ---------------------------------------------------------------- what is read


def _value(text: str):
    """A setting's value as typed: JSON when it parses (true, 0.8, ["/A"]), else the text itself."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


@uses("settings.get", "settings.update")
def cmd_settings(cfg: Config, args: argparse.Namespace) -> Call:
    if args.settings_command == "set":
        return Call("settings.update", {"changes": {args.key: _value(args.value)}}, _show_settings)
    return Call("settings.get", {}, _show_settings)


@uses("watch.add", "watch.remove", "watch.from_now")
def cmd_watch(cfg: Config, args: argparse.Namespace) -> Call:
    if args.action in ("add", "remove"):
        return Call(f"watch.{args.action}", {"path": args.path}, lambda s: console.print(
            "Watching: " + (", ".join(s["watch"]) or "[dim]nothing[/dim]")))

    def show(result: dict) -> None:
        if result["already_read"]:
            console.print(f"[dim]Already read in full, so nothing is skipped: {', '.join(result['already_read'])}[/dim]")
        console.print(f"{'New writing only' if args.action == 'from-now' else 'Everything is read'} in "
                      f"{len(result['documents'])} document(s)")
    return Call("watch.from_now", {"path": args.path, "on": args.action == "from-now"}, show,
                status="Listing the library…")


@uses("library")
def cmd_library(cfg: Config, args: argparse.Namespace) -> Call:
    def show(lib: dict) -> None:
        from rich.table import Table

        table = Table(box=None, pad_edge=False, padding=(0, 2, 0, 0))
        for col in ("folder", "documents", "watched"):
            table.add_column(col)
        top = [d for d in lib["documents"] if d["folder"] == "/"]
        table.add_row("/", str(len(top)), "")
        for f in lib["folders"]:
            table.add_row(f["path"], str(f["documents"]), "yes" if f["watched"] else "")
        console.print(table)
    return Call("library", {}, show, status="Listing the library…")


# ---------------------------------------------------------------- doing the work now


@uses("collect", "pending")
def cmd_collect(cfg: Config, args: argparse.Namespace) -> Call:
    if args.dry_run:
        def show_pending(pending: list) -> None:
            if not pending:
                console.print("Nothing changed since the last collection.")
            for d in pending:
                console.print(f"[bold]{escape(d['path'])}[/bold]: {len(d['pages'])} changed page(s) "
                              + ", ".join(str(p) for p in d["pages"]))
        return Call("pending", {"fetch": True}, show_pending,
                    status="Checking what changed (downloads only, nothing is read)…")

    def show(summary: dict) -> None:
        console.print(f"{summary['docs_changed']} changed document(s), {summary['pages_read']} page(s) read, "
                      f"{summary['lines_judged']} new line(s) judged, {summary['actions_new']} new action(s).")
        for e in summary["errors"]:
            console.print(f"[red]{escape(e)}[/red]")
    return Call("collect", {}, show, status="Collecting…")


@uses("todo.sync")
def cmd_todo(cfg: Config, args: argparse.Namespace) -> Call:
    return Call("todo.sync", {"force": args.force}, lambda r: console.print(
        f"{r.get('ticked', 0)} ticked, {r.get('written', 0)} written on paper; "
        + ("published" if r.get("published") else "unchanged")
        + (f"; [yellow]{r['overflow']} item(s) didn't fit[/yellow]" if r.get("overflow") else "")),
        status="Reading ticks and publishing the To-do document…")


@uses("check")
def cmd_check(cfg: Config, args: argparse.Namespace) -> Call:
    return Call("check", {}, lambda r: console.print(
        "Nothing to check: no folders watched and the To-do document is off." if not r else
        "; ".join(f"{k}: {v}" for k, v in r.items())), status="Checking…")


@uses("status", "source")
def cmd_status(cfg: Config, args: argparse.Namespace) -> Call:
    def show(status: dict) -> None:
        src = status["source"]
        console.print(f"Source:   {src['label']} ({src['detail']})")
        console.print(f"Judge:    {'Jev' if status['judge'] == 'jev' else 'the LLM'}")
        console.print(f"Watching: {', '.join(status['watch']) or '[dim]nothing[/dim]'}")
        console.print(f"Read:     {status['documents_read']} document(s), last at {status['last_collected_at'] or 'never'}")
        console.print(f"Items:    {status['items']['open']} open, {status['items']['done']} done")
        t = status["todo"]
        console.print(f"To-do:    {'on' if t['enabled'] else 'off'}"
                      + (f", “{t['name']}” in {t['folder']}, published {t['published_at'] or 'never'}" if t["enabled"] else ""))
    return Call("status", {}, show)


# ---------------------------------------------------------------- AI


@uses("ai.get", "ai.set_key", "ai.remove_key", "ai.provider", "ai.model")
def cmd_ai(cfg: Config, args: argparse.Namespace) -> Call:
    if args.ai_command == "key":
        value = read_secret(args, f"The {args.which} key")
        return Call("ai.set_key", {"which": args.which, "value": value}, _show_ai, status="Checking the key…")
    if args.ai_command == "remove":
        return Call("ai.remove_key", {"which": args.which}, _show_ai)
    if args.ai_command in ("provider", "model"):
        return Call(f"ai.{args.ai_command}", {"name": args.name}, _show_ai)
    return Call("ai.get", {}, _show_ai)


# ---------------------------------------------------------------- where an item came from


@uses("page.image", "line.image")
def cmd_image(cfg: Config, args: argparse.Namespace) -> Call:
    def then(svg: str) -> dict:
        if args.out:
            Path(args.out).write_text(svg)
            return {"path": args.out}
        return {"svg": svg}

    def show(data: dict) -> None:
        if "svg" in data:
            sys.stdout.write(data["svg"])
        else:
            console.print(f"Written to {data['path']}")

    if args.image_command == "page":
        return Call("page.image", {"doc_id": args.doc_id, "page": args.page, "anchor": args.anchor,
                                   "width": args.width}, show, then)
    return Call("line.image", {"doc_id": args.doc_id, "anchor": args.anchor}, show, then)


# ---------------------------------------------------------------- live updates


def _event_line(e: dict) -> str:
    at = e["at"][11:19]
    if e["type"].startswith("item."):
        item = e["item"]
        what = f"#{item['id']}" + (f" {item['text']}" if "text" in item else "")
        if item.get("status") == "done":
            what += " ✓"
    elif e["type"] == "check.finished":
        what = f"{e['new']} new, {e['updated']} updated, {e['missing']} gone"
    elif e["type"] == "source.error":
        what = e["message"]
    else:
        what = ""
    return f"[dim]{at}[/dim] {e['type']:<16} {escape(what)}"


@uses("events")
def cmd_events(cfg: Config, args: argparse.Namespace) -> Call | int:
    if not args.follow:
        def show(data: dict) -> None:
            for e in data["events"]:
                console.print(_event_line(e), highlight=False)
            console.print(f"[dim]cursor {data['cursor']}[/dim]")
        return Call("events", {"since": args.since}, show)
    import time

    jotted = _jotted(cfg)  # reads the events table directly: sees every process's changes
    cursor = args.since if args.since is not None else jotted.events()["cursor"]
    try:
        while True:
            found = jotted.events(since=cursor)
            for e in found["events"]:
                if args.json:
                    print(json.dumps({"v": contract.CONTRACT, **e}, ensure_ascii=False, default=str), flush=True)
                else:
                    console.print(_event_line(e), highlight=False)
            cursor = found["cursor"]
            if not found["events"]:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        return 130


# ---------------------------------------------------------------- setting up


def cmd_setup(cfg: Config | None, args: argparse.Namespace) -> Done | int:
    from . import steps

    if args.setup_command == "status":
        return Done(steps.status(steps.load_config()), _show_steps)
    if args.setup_command == "prepare":
        def show(data: dict) -> None:
            for p in data["prepared"]:
                console.print(f"[green]✓[/green] {p['id']}: {escape(p['detail'] or 'done')}")
            if not data["prepared"]:
                console.print("Nothing to prepare.")
            _show_steps(data)
        return Done(steps.prepare(), show)
    if not can_prompt(args):
        raise UsageError("`jotted setup` asks questions; without a terminal, use `jotted setup status` "
                         "and run the command each step names")
    cfg = _onboard(redo=True)
    if cfg is None:
        return 1
    console.print("\n[green]All set.[/green] Run [bold]jotted start[/bold] to open the app.")
    return 0


def cmd_plugins(cfg: Config | None, args: argparse.Namespace) -> Done:
    from . import plugins, steps

    cfg = steps.load_config()
    chosen = cfg.plugins.source if cfg else None
    installed = []
    for name, target in sorted(plugins.available().items()):
        try:
            cls = plugins.plugin_class(name)
            installed.append({"name": name, "label": cls.LABEL, "module": target, "chosen": name == chosen})
        except Exception as e:  # a broken plugin is listed, not fatal
            installed.append({"name": name, "module": target, "chosen": name == chosen, "error": str(e)})

    def show(data: dict) -> None:
        for p in data["installed"]:
            console.print(f"{'[green]●[/green]' if p['chosen'] else '○'} {p['name']} "
                          f"[dim]{escape(p.get('label') or p.get('error', ''))} ({p['module']})[/dim]")
    return Done({"chosen": chosen, "installed": installed}, show)


def cmd_config_check(cfg: Config, args: argparse.Namespace) -> Done:
    jotted = _jotted(cfg)
    data = {"path": str(cfg.source), "config": cfg.as_dict(), "source": jotted.source(), "ai": jotted.ai()}

    def show(d: dict) -> None:
        console.print(f"[green]OK[/green] {d['path']}")
        for section, values in d["config"].items():
            console.print(f"\n[bold]\\[{section}][/bold]")
            width = max(len(k) for k in values)
            for k, v in values.items():
                console.print(f"  {k:<{width}} = {json.dumps(v)}", highlight=False)
        console.print()
        src, ai = d["source"], d["ai"]
        console.print(f"Source: {src['label']} ({src['detail']})")
        console.print(f"LLM: {ai['llm']['label']} {ai['llm']['model']}, key "
                      + ("set" if ai["llm"]["key"]["set"] else "[yellow]not set (run jotted setup)[/yellow]"))
        console.print(f"Jev plugin: {'on' if ai['jev']['enabled'] else 'off'}")
    return Done(data, show)


# ---------------------------------------------------------------- running


def cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    host = args.host or cfg.server.host
    port = cfg.server.port if args.port is None else args.port
    if host not in ("127.0.0.1", "localhost", "::1"):
        errors.print(f"[yellow]Warning[/yellow]: listening on {host}; the app has no login, so anyone who can "
                     "reach this address can read and change your tasks.")
    return _serve(cfg, host, port, background=not args.no_background, dev=args.dev,
                  open_path=None if args.no_browser else "/")


def _running_here(url: str) -> bool:
    """Whether Jotted already answers at `url` (another start, or `jotted serve`)."""
    import urllib.request

    try:
        with urllib.request.urlopen(url + "/api/settings", timeout=2) as resp:  # noqa: S310 - local URL
            return "watch" in json.loads(resp.read())
    except (OSError, ValueError):
        return False


def _serve(cfg: Config, host: str, port: int, *, background: bool = True, dev: bool = False,
           open_path: str | None = None) -> int:
    """Serve the web app; with `open_path`, open the browser there once it is listening.
    Port 0 picks a free one. While it runs, serve.json lets CLI commands hand it their work."""
    import socket
    import webbrowser

    from . import fastpath, selfupdate
    from .server import create_app

    if port:
        with socket.socket() as probe:
            busy = probe.connect_ex((host, port)) == 0
        if busy:
            url = f"http://{host}:{port}"
            if _running_here(url):
                console.print(f"Jotted is already running at [bold]{url}[/bold]")
                if os.environ.get(selfupdate.DONE_VAR):
                    console.print("[yellow]That window still runs the old version:[/yellow] stop it with Ctrl+C, "
                                  "then run [bold]jotted start[/bold] again.")
                if open_path is not None:
                    webbrowser.open(url + open_path)
                return 0
            errors.print(f"[red]Port {port} is in use[/red] by another program. Try `--port {port + 1}`, or "
                         "`--port 0` for any free one.")
            return 1
    app = create_app(cfg, background=background)
    if dev:
        console.print(f"Jotted at [bold]http://{host}:{port}[/bold]  (store: {cfg.server.db})")
        app.run(host=host, port=port, debug=False, threaded=True)
        return 0
    from waitress import create_server

    # One process, many threads: the background scheduler must exist exactly once.
    server = create_server(app, host=host, port=port, threads=8, ident="jotted")
    port = int(server.effective_port)
    url = f"http://{host}:{port}"
    console.print(f"Jotted at [bold]{url}[/bold]  (store: {cfg.server.db})")
    fastpath.write(cfg, host, port, app.config["token"])
    import signal

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # how an app stops it: still clean up serve.json
    if open_path is not None:
        webbrowser.open(url + open_path)
    console.print("[dim]Leave this window open while you use Jotted. Press Ctrl+C to stop.[/dim]")
    try:
        server.run()
    except KeyboardInterrupt:
        console.print("Stopped.")
    finally:
        fastpath.remove(cfg)
        server.close()
    return 0


def _onboard(redo: bool) -> Config | None:
    from . import onboarding
    from .config import ConfigError

    try:
        return onboarding.run(onboarding.ConsoleUI(console), redo=redo)
    except (onboarding.SetupError, ConfigError) as e:
        console.print(f"\n[red]Setup stopped:[/red] {e}")
    except (EOFError, KeyboardInterrupt):
        console.print("\nSetup stopped. Run it again any time; finished steps are kept.")
    return None


def _rerun() -> None:
    """Start this command again in the copy just installed (once: DONE_VAR stops a second update)."""
    from . import selfupdate

    os.environ[selfupdate.DONE_VAR] = "1"
    os.execv(sys.argv[0], sys.argv)


def cmd_start(cfg: Config | None, args: argparse.Namespace) -> int:
    """Update from GitHub, set up whatever is missing, then run the web app and open it in the browser."""
    from . import selfupdate

    if not can_prompt(args):
        raise UsageError("`jotted start` is for people at a terminal; a wrapper runs `jotted setup status`, "
                         "then `jotted serve --no-browser`")
    if not args.no_update and not contract.bundled() and selfupdate.check(console):
        _rerun()
    cfg = _onboard(redo=False)
    if cfg is None:
        return 1
    _setup_logging(cfg.logging.level)
    from .adapters.sqlite_repo import SqliteRepository

    first_time = not SqliteRepository(cfg.server.db).settings().watch  # nothing watched yet: start in Settings
    console.print()
    return _serve(cfg, cfg.server.host, cfg.server.port if args.port is None else args.port,
                  open_path=None if args.no_browser else ("/#settings" if first_time else "/"))


def cmd_update(cfg: Config | None, args: argparse.Namespace) -> Done:
    """Update to the latest version on GitHub now."""
    from . import selfupdate

    if contract.bundled():
        raise contract.Error("conflict", "This copy of Jotted comes with the Jotted app, which updates it")
    updated = selfupdate.check(errors if args.json else console, force=True)  # says what it did
    return Done({"updated": updated}, lambda d: console.print(
        "Run [bold]jotted start[/bold] to use it (stop a running app first with Ctrl+C)." if d["updated"] else ""))


def cmd_version(cfg: Config | None, args: argparse.Namespace) -> Done:
    import platform

    from . import steps

    cfg = steps.load_config()
    data = {"version": contract.release(), "contract": contract.CONTRACT, "contract_min": contract.CONTRACT_MIN,
            "source_plugin": cfg.plugins.source if cfg else None, "python": platform.python_version(),
            "bundled": contract.bundled()}
    return Done(data, lambda d: console.print(
        f"jotted {d['version']} (contract {d['contract']}, accepts {d['contract_min']}+; "
        f"source {d['source_plugin'] or 'not set up'}; Python {d['python']})", highlight=False))


def cmd_schema(cfg: Config | None, args: argparse.Namespace) -> Done:
    from . import schema

    return Done(schema.build(build_parser()), lambda d: _print_json(d))


def cmd_mcp(cfg: Config | None, args: argparse.Namespace) -> int:
    from . import mcp

    return mcp.serve(admin=args.admin, ui_dir=args.ui_dir)


def cmd_claude(cfg: Config | None, args: argparse.Namespace) -> Done:
    """Claude Desktop runs `jotted mcp` (jotted.integrations.claude_desktop)."""
    from .integrations import claude_desktop

    def changed(d: dict) -> None:
        if not d["changed"]:
            console.print("Nothing to change.")
            return
        console.print(("Would change " if d.get("dry_run") else "Changed ") + escape(d["config_path"])
                      + (f" [dim](backup: {escape(d['backup_path'])})[/dim]" if d.get("backup_path") else ""))
        if not d.get("dry_run"):
            console.print("Quit and reopen Claude Desktop to use it.")

    def show_status(d: dict) -> None:
        if not d["configured"]:
            console.print(f"Not connected [dim]({escape(d['config_path'])})[/dim]. Run `jotted claude connect`.")
        elif not d["command_exists"]:
            console.print(f"[yellow]Needs repair[/yellow]: {escape(d['command'] or '')} isn't there any more. "
                          "Run `jotted claude connect`.")
        else:
            console.print(f"Connected: Claude Desktop runs {escape(d['command'])}"
                          + ("" if d["matches_current"] else " [dim](another copy of jotted)[/dim]"))

    if args.claude_command == "connect":
        return Done(claude_desktop.connect(args.command_path, admin=args.admin, dry_run=args.dry_run), changed)
    if args.claude_command == "disconnect":
        return Done(claude_desktop.disconnect(), changed)
    return Done(claude_desktop.status(), show_status)


# ---------------------------------------------------------------- the command line


class Parser(argparse.ArgumentParser):
    """Bad arguments raise UsageError, so --json still prints an envelope."""

    def error(self, message: str):
        raise UsageError(f"{self.prog}: {message}")


def build_parser() -> argparse.ArgumentParser:
    p = Parser(prog="jotted", description="Turn handwritten notes into a to-do list.")
    p.add_argument("--json", action="store_true", help="print one JSON envelope (for scripts and other apps)")
    p.add_argument("--local", action="store_true", help="do the work in this process, even when `jotted serve` runs")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def command(name: str, func, help: str, no_config: bool = False, **kw) -> argparse.ArgumentParser:
        c = sub.add_parser(name, help=help, **kw)
        c.set_defaults(func=func, no_config=no_config)
        return c

    # setting up
    st = command("setup", cmd_setup, "set-up steps: status, prepare; alone, the interactive walkthrough",
                 no_config=True)
    st_sub = st.add_subparsers(dest="setup_command", metavar="STEP")
    st_sub.add_parser("status", help="every setup step, done or not, and the command that does it")
    st_sub.add_parser("prepare", help="the steps that need no answer: the app folder, the source's tools")
    ai = command("ai", cmd_ai, "the language model and the Jev plugin: status, keys, provider, model")
    ai_sub = ai.add_subparsers(dest="ai_command", metavar="ACTION")
    k = ai_sub.add_parser("key", help="check and save a key (adding Jev's turns the plugin on)")
    k.add_argument("which", choices=["llm", "jev"])
    k.add_argument("--stdin", action="store_true", help="read the key from standard input instead of asking")
    ai_sub.add_parser("remove", help="turn the Jev plugin off").add_argument("which", choices=["jev"])
    ai_sub.add_parser("provider", help="choose the LLM adapter (anthropic)").add_argument("name")
    ai_sub.add_parser("model", help="choose the model; it has to read images").add_argument("name")
    command("plugins", cmd_plugins, "installed source plugins, and which one is chosen", no_config=True)

    # the to-do list
    it = command("items", cmd_items, "the to-do list: list, add, propose, accept, edit, tick, dismiss")
    it_sub = it.add_subparsers(dest="items_command", metavar="ACTION")
    for c in (it, it_sub.add_parser("list", help="list items (the default)")):
        c.add_argument("--status", choices=["open", "done", "all", "proposed", "dismissed", "any"], default="open",
                       help="all: open and done (the list); any: every item, proposed and dismissed too")
        c.add_argument("--owner", choices=["mine", "others"])
        c.add_argument("--folder", help="only items from documents in this folder")
        c.add_argument("--source-kind", help="only items from this kind of source (gdoc, gmail...)")
        c.add_argument("--source-key", help="only the item from this source line, message or ticket")
        c.add_argument("--query", help="only items whose text or source title contains this")
        c.add_argument("--limit", type=int, help="at most this many (1 to 200); a full page may have more")
        c.add_argument("--cursor", type=int, help="the next page: the id of the last item of the one before")
    it_sub.add_parser("get", help="one item, in any status, with where it came from").add_argument("id", type=int)
    ad = it_sub.add_parser("add", help="add an item (once per --source-key)")
    ad.add_argument("text", nargs="+")
    ab = it_sub.add_parser("add-batch", help="add up to 100 items from a JSON array on standard input")
    ab.add_argument("--stdin", action="store_true", help="read the items from standard input (required)")
    for c in (ad, ab):
        c.add_argument("--propose", action="store_true", help="wait for the person to accept it before it is listed")
        c.add_argument("--agent", action="store_true", help="added by an agent, not typed by the person "
                                                           "(`jotted mcp` sets it)")
    ac = it_sub.add_parser("accept", help="put proposed items on the list")
    ac.add_argument("ids", type=int, nargs="*", help="the items to accept")
    ac.add_argument("--all", action="store_true", help="every proposed item")
    ac.add_argument("--source-kind", help="with --all: only those from this kind of source")
    ed = it_sub.add_parser("edit", help="change an item's text or owner")
    ed.add_argument("id", type=int)
    ed.add_argument("text", nargs="*")
    for c in (ad, ed):
        c.add_argument("--owner", choices=["mine", "others"])
        c.add_argument("--owner-name", help="who owns it, when that's someone else (at most 80 characters)")
    ad.add_argument("--source-kind", help="where it came from: gdoc, gmail, confluence, jira, gcal, chat, other...")
    ad.add_argument("--source-key", help="the line, message or ticket it came from: the item is added only once")
    ad.add_argument("--source-title", help="the source's title, as it is shown")
    ad.add_argument("--source-url", help="a link to the source (https only; Jotted never opens it)")
    ad.add_argument("--excerpt", help="the text the item was taken from (at most 500 characters)")
    for name, help in (("done", "mark an item done"), ("reopen", "mark an item open again"),
                       ("dismiss", "not an action, or not wanted: take it off the list; its source never "
                                   "adds it again")):
        it_sub.add_parser(name, help=help).add_argument("id", type=int)

    # what is read
    command("library", cmd_library, "list your device's folders and which are watched")
    wa = command("watch", cmd_watch, "watch or stop watching a folder (also in the web app)",
                 description="from-now: skip what is already written in the documents at PATH (a "
                             "folder means the documents in it now); read-all undoes it.")
    wa.add_argument("action", choices=["add", "remove", "from-now", "read-all"])
    wa.add_argument("path", help="folder or document path, e.g. '/Meeting notes'")
    se = command("settings", cmd_settings, "show or change settings (also in the web app)")
    se_sub = se.add_subparsers(dest="settings_command", metavar="ACTION")
    ss = se_sub.add_parser("set", help="set one: e.g. todo_enabled true, action_threshold 0.8")
    ss.add_argument("key")
    ss.add_argument("value", help="JSON (true, 0.8, [\"/A\"]) or plain text")

    # doing the work now (`jotted serve` does it in the background)
    co = command("collect", cmd_collect, "read what changed in watched folders and update the to-do list")
    co.add_argument("--dry-run", action="store_true", help="only show which documents and pages changed")
    td = command("todo", cmd_todo, "read ticks from, and republish, the To-do document")
    td.add_argument("--force", action="store_true", help="republish even if nothing changed")
    command("check", cmd_check, "collect and update the To-do document now")
    command("status", cmd_status, "what Jotted reads, judges and publishes, and when it last did")

    # where an item came from
    im = command("image", cmd_image, "draw a source page or line as SVG")
    im_sub = im.add_subparsers(dest="image_command", required=True, metavar="WHAT")
    pg = im_sub.add_parser("page", help="a page, with a line highlighted")
    pg.add_argument("doc_id")
    pg.add_argument("page", type=int)
    pg.add_argument("--anchor", "--highlight", help="the line to highlight (an item's page.anchor)")
    pg.add_argument("--width", type=int, help="width in pixels; the height follows")
    ln = im_sub.add_parser("line", help="one handwritten line")
    ln.add_argument("doc_id")
    ln.add_argument("anchor")
    for c in (pg, ln):
        c.add_argument("-o", "--out", help="write to this file instead of standard output")

    # live updates
    ev = command("events", cmd_events, "changes since a cursor; --follow keeps printing them")
    ev.add_argument("--since", type=int, help="the cursor of the last event you saw")
    ev.add_argument("--follow", action="store_true", help="keep printing changes as they happen, one per line")
    ev.add_argument("--interval", type=float, default=1.0, help=argparse.SUPPRESS)

    # running and the rest
    sv = command("serve", cmd_serve, "run the web app, background checking and the CLI's fast path")
    sv.add_argument("--host", help="default: server.host")
    sv.add_argument("--port", type=int, help="default: server.port; 0 picks a free one")
    sv.add_argument("--no-browser", action="store_true", help="don't open the browser")
    sv.add_argument("--dev", action="store_true", help="use Flask's development server")
    sv.add_argument("--no-background", action="store_true", help="don't check or write to the device in the background")
    sa = command("start", cmd_start, "set up anything missing, then open the app (start here)", no_config=True)
    sa.add_argument("--port", type=int, help="default: server.port")
    sa.add_argument("--no-browser", action="store_true", help="don't open the browser")
    sa.add_argument("--no-update", action="store_true", help="don't check GitHub for a newer version")
    command("update", cmd_update, "update Jotted to the latest version on GitHub", no_config=True)
    command("version", cmd_version, "release and contract versions", no_config=True)
    command("schema", cmd_schema, "every command: its arguments, options and the shape of its data",
            no_config=True)
    mc = command("mcp", cmd_mcp, "serve Jotted's operations as MCP tools on stdio (for agents)", no_config=True)
    mc.add_argument("--admin", action="store_true", help="also offer the tools that change what Jotted reads "
                                                         "(settings, watch, library, collect, check)")
    mc.add_argument("--ui-dir", help=argparse.SUPPRESS)  # development: serve the widget from these files
    cl = command("claude", cmd_claude, "connect Claude Desktop to Jotted (it runs `jotted mcp`)", no_config=True)
    cl_sub = cl.add_subparsers(dest="claude_command", required=True, metavar="ACTION")
    cc = cl_sub.add_parser("connect", help="add Jotted to Claude Desktop's MCP servers (backs up its settings first)")
    cc.add_argument("--command", dest="command_path", help="the jotted to run (default: this one)")
    cc.add_argument("--admin", action="store_true", help="let Claude change settings and watched folders too")
    cc.add_argument("--dry-run", action="store_true", help="say what would change; write nothing")
    cl_sub.add_parser("status", help="whether Claude Desktop runs Jotted, and whether that copy still exists")
    cl_sub.add_parser("disconnect", help="remove Jotted from Claude Desktop's MCP servers (backs up first)")
    cfg_p = sub.add_parser("config", help="configuration commands")
    cfg_sub = cfg_p.add_subparsers(dest="config_command", required=True, metavar="ACTION")
    cfg_sub.add_parser("check", help="validate config.toml and print resolved values").set_defaults(
        func=cmd_config_check, no_config=False)

    # the source plugins' own commands (reMarkable: connect)
    from . import plugins

    for name in plugins.available():
        try:
            plugins.plugin_class(name).cli(sub)
        except Exception as e:  # a broken plugin must not take the CLI down
            print(f"warning: source plugin {name!r} failed to load: {e}", file=sys.stderr)

    _json_anywhere(p)
    return p


def _json_anywhere(parser: argparse.ArgumentParser) -> None:
    """Let --json come after the command too (`jotted items --json`)."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in set(action.choices.values()):
                child.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
                _json_anywhere(child)


def _load_config() -> Config:
    from . import keys
    from .config import load

    cfg = load()
    keys.load_into_env(cfg)
    _setup_logging(cfg.logging.level)
    return cfg


def _execute(args: argparse.Namespace) -> int:
    if getattr(args, "deprecated", None):
        errors.print(f"[yellow]`jotted {args.command}` is now `jotted {args.deprecated}`[/yellow]; "
                     "the old name goes in the next release.")
    cfg = None if getattr(args, "no_config", False) else _load_config()
    out = args.func(cfg, args)
    if isinstance(out, int):  # serve, start, events --follow, mcp: they print their own output
        return out
    if isinstance(out, Call):
        data, render = _run_call(cfg, args, out), out.render
    elif isinstance(out, Done):
        data, render = out.data, out.render
    else:  # a plugin's command: its data, and render(data, console) from its parser
        plugin_render = getattr(args, "render", None)
        data, render = out, (lambda d: plugin_render(d, console)) if plugin_render else None
    if args.json:
        _print_json(contract.ok(data))
    elif render:
        render(data)
    else:
        _print_json(data)
    return 0


def _show_error(envelope: dict, json_out: bool) -> int:
    if json_out:
        _print_json(envelope)
    else:
        err = envelope["error"]
        errors.print(f"[red]Error:[/red] {escape(err['message'])}")
        if err["code"] == "config":
            from .config import resolve_path

            errors.print(f"[dim](config path: {resolve_path()}; set JOTTED_CONFIG to use another)[/dim]")
    return contract.exit_code(envelope)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    json_out = "--json" in argv
    try:
        args = build_parser().parse_args(argv)
        json_out = args.json
        return _execute(args)
    except SystemExit:  # --help
        raise
    except BaseException as e:  # noqa: BLE001 - every failure becomes an envelope and an exit code
        envelope = contract.error_of(e)
        if envelope["error"]["code"] == "internal":
            traceback.print_exc(file=sys.stderr)
        return _show_error(envelope, json_out)


def invoke(argv: list[str], stdin: str | None = None) -> dict:
    """Run a command with --json in this process and return its envelope (`jotted mcp`, tests).
    `stdin` is what the command reads from standard input (--stdin)."""
    out = io.StringIO()
    real_stdin = sys.stdin
    sys.stdin = io.StringIO(stdin or "")
    try:
        with contextlib.redirect_stdout(out):
            main(["--json", *argv])
    finally:
        sys.stdin = real_stdin
    return json.loads(out.getvalue())


if __name__ == "__main__":
    sys.exit(main())
