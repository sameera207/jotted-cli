"""The architecture's promises: one application layer that every front end reaches in full,
sources as plugins outside the core, and one source job at a time across processes."""

import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from jotted import api, cli, config, plugins  # noqa: E402
from jotted.locking import SourceLock  # noqa: E402

SRC = Path(__file__).parent.parent / "src" / "jotted"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    text = config.EXAMPLE.read_text().replace('db   = "./data/jotted.db"', f'db = "{tmp_path}/db.sqlite"')
    path = tmp_path / "config.toml"
    path.write_text(text)
    monkeypatch.setenv(config.ENV_VAR, str(path))
    for name in ("ANTHROPIC_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return config.load()


# ---------------------------------------------------------------- one application layer


def _cli_operations() -> set[str]:
    return {op for name in dir(cli) if name.startswith("cmd_")
            for op in getattr(getattr(cli, name), "operations", ())}


def test_every_operation_is_a_cli_command():
    assert set(api.OPERATIONS) - _cli_operations() == set()
    assert _cli_operations() - set(api.OPERATIONS) == set()  # and the CLI names only real operations


def test_every_web_route_calls_an_operation(cfg):
    from jotted.server import create_app

    flask = create_app(cfg, background=False)
    routes = {rule.rule: flask.view_functions[rule.endpoint] for rule in flask.url_map.iter_rules()
              if rule.endpoint != "static" and rule.rule != "/"}
    unmapped = [r for r, fn in routes.items()
                if getattr(fn, "operation", None) not in api.OPERATIONS and r != "/api/op/<name>"]
    assert not unmapped, f"routes without an api operation: {unmapped}"


def test_front_ends_hold_no_product_logic():
    """The server and CLI reach the repository and the source only through the api."""
    for name in ("server.py", "cli.py"):
        text = (SRC / name).read_text()
        for needle in (".repo.", ".source.", "core.service", "keys.save", "keys.remove"):
            assert needle not in text, f"{name} uses {needle} directly"


# ---------------------------------------------------------------- sources are plugins


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            found |= {base} | {f"{base}.{a.name}" for a in node.names}
    return found


def test_nothing_outside_the_plugin_knows_remarkable():
    leaks = []
    for path in SRC.rglob("*.py"):
        if "plugins" in path.relative_to(SRC).parts:
            continue
        bad = [i for i in _imports(path) if "remarkable" in i or i.split(".")[-1] in ("cloud", "rmfile", "rmapi_install")]
        if bad:
            leaks.append(f"{path.relative_to(SRC)}: {bad}")
    assert not leaks, leaks


def test_the_core_imports_no_plugin_adapter_or_sdk():
    for path in (SRC / "core").glob("*.py"):
        for name in _imports(path):
            assert not any(word in name for word in ("plugins", "adapters", "flask", "anthropic", "typesafe",
                                                     "rmscene")), f"{path.name} imports {name}"


def test_the_remarkable_plugin_is_registered_and_owns_its_config(cfg):
    assert "remarkable" in plugins.available()
    cls = plugins.plugin_class("remarkable")
    assert set(cls.SECTIONS) == {"rmapi", "strokes", "template"}
    assert cfg.rmapi.binary == "rmapi" and cfg.section("template").scale == 1.0525  # plugin sections read like core ones
    with pytest.raises(plugins.PluginError, match="no source plugin"):
        plugins.plugin_class("nope")


def test_an_unknown_source_plugin_is_a_config_error(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[plugins]\nsource = "supernote"\n')
    with pytest.raises(config.ConfigError, match="no source plugin 'supernote'"):
        config.load(p)


def test_plugins_from_other_packages_are_found_by_entry_point(monkeypatch):
    class EP:
        name, value = "supernote", "jotted_supernote:SupernotePlugin"

    monkeypatch.setattr(plugins, "entry_points", lambda group: [EP()] if group == plugins.GROUP else [])
    assert plugins.available()["supernote"] == "jotted_supernote:SupernotePlugin"


def test_https_downloads_go_through_net():
    """Plain urllib can't verify certificates in the standalone build; net.urlopen can. Direct
    urlopen calls are for this machine's own server only, in files that name no https URL."""
    leaks = []
    for path in SRC.rglob("*.py"):
        if path.name == "net.py":
            continue
        tree = ast.parse(path.read_text())
        direct = any(isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", None)) == "urlopen"
                     and not (isinstance(n.func, ast.Attribute) and getattr(n.func.value, "id", None) == "net")
                     for n in ast.walk(tree))
        https = any(isinstance(n, ast.Constant) and isinstance(n.value, str) and "https://" in n.value
                    for n in ast.walk(tree))
        if direct and https:
            leaks.append(str(path.relative_to(SRC)))
    assert not leaks, f"call net.urlopen for https: {leaks}"


# ---------------------------------------------------------------- one source job at a time


def test_the_source_lock_holds_across_processes(tmp_path):
    lock_path = tmp_path / "x.lock"
    holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(SRC.parent)!r})
        from jotted.locking import SourceLock
        lock = SourceLock(__import__("pathlib").Path({str(lock_path)!r}))
        lock.acquire()
        print("held", flush=True)
        time.sleep(30)
    """)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        lock = SourceLock(lock_path)
        assert not lock.acquire(timeout=0.3)  # another process has it
        holder.kill()
        holder.wait()
        assert lock.acquire(timeout=5)  # free once that process is gone
        lock.release()
    finally:
        holder.kill()


def test_a_busy_source_is_a_conflict_not_a_hang(cfg, monkeypatch):
    jotted = api.Jotted.open(cfg)
    monkeypatch.setattr(api, "SOURCE_WAIT_S", 0.2)
    jotted.app.lock.acquire()
    try:
        with pytest.raises(api.Conflict, match="busy"):
            jotted._source_job("The library", lambda: None, wait=0.2)
    finally:
        jotted.app.lock.release()


# ---------------------------------------------------------------- the CLI as the whole product


def run(capsys, *argv) -> tuple[int, object]:
    code = cli.main(["--json", "--local", *argv])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else None


def data(capsys, *argv):
    code, envelope = run(capsys, *argv)
    assert code == 0 and envelope["ok"] and envelope["v"] == 1, envelope
    return envelope["data"]


def test_the_cli_runs_the_list_with_json(cfg, capsys):
    item = data(capsys, "items", "add", "Renew", "the", "passport")
    assert item["text"] == "Renew the passport" and item["origin"] == "web"
    assert data(capsys, "items")[0]["id"] == item["id"]
    assert data(capsys, "items", "done", str(item["id"]))["status"] == "done"
    assert data(capsys, "items") == []  # open only, by default
    assert len(data(capsys, "items", "--status", "all")) == 1
    assert data(capsys, "items", "edit", str(item["id"]), "Renew", "both", "passports")["text"] == "Renew both passports"
    code, err = run(capsys, "items", "done", "999")
    assert code == 1 and err == {"v": 1, "ok": False, "error": {"code": "not_found", "message": "no item 999"}}


def test_the_cli_changes_settings_and_reports_status(cfg, capsys):
    assert data(capsys, "settings", "set", "todo_enabled", "true")["todo_enabled"] is True
    assert data(capsys, "settings", "set", "watch", '["Meetings/"]')["watch"] == ["/Meetings"]
    code, err = run(capsys, "settings", "set", "action_threshold", "2")
    assert code == 1 and err["error"]["code"] == "invalid" and "between 0 and 1" in err["error"]["message"]
    status = data(capsys, "status")
    assert status["source"]["name"] == "remarkable" and status["watch"] == ["/Meetings"]
    assert status["judge"] == "llm" and status["todo"]["enabled"]
    ai = data(capsys, "ai")
    assert ai["llm"]["provider"] == "anthropic" and not ai["jev"]["enabled"]
