"""The CLI contract (specs/CLI-contract-spec.md): one envelope per --json run, error codes
with their exit statuses, no prompt a wrapper could hang on, setup as steps, events, the
fast path, `schema` and `mcp`."""

import io
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from jotted import api, cli, config, contract, fastpath, keys, mcp, schema  # noqa: E402
from jotted.plugins.remarkable import cloud, rmapi_install, setup as rm_setup  # noqa: E402

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import contract_docs  # noqa: E402


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    text = config.EXAMPLE.read_text().replace('db   = "./data/jotted.db"', f'db = "{tmp_path}/db.sqlite"')
    path = tmp_path / "config.toml"
    path.write_text(text)
    monkeypatch.setenv(config.ENV_VAR, str(path))
    monkeypatch.delenv(cli.BUNDLED_VAR, raising=False)
    for name in ("ANTHROPIC_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return config.load()


@pytest.fixture
def rmapi(monkeypatch):
    """rmapi installed; the cloud takes code ABCD1234."""
    monkeypatch.setattr(rm_setup.shutil, "which", lambda b: "/opt/bin/rmapi")

    def register(cfg, code):
        if code != "ABCD1234":
            raise cloud.CloudError("rmapi ls / failed (exit 1):\ncode rejected")
        cfg.rmapi.token_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.rmapi.token_file.write_text(f"token-{code}")

    monkeypatch.setattr(cloud, "register", register)
    monkeypatch.setattr(cloud, "library", lambda cfg: (["a", "b"], []))


def run(capsys, *argv, stdin: str | None = None, monkeypatch=None) -> tuple[int, dict]:
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = cli.main(["--json", "--local", *argv])
    out = capsys.readouterr().out
    return code, json.loads(out)


def data(capsys, *argv, **kw):
    code, envelope = run(capsys, *argv, **kw)
    assert code == 0 and envelope["ok"] and envelope["v"] == contract.CONTRACT, envelope
    return envelope["data"]


def error(capsys, *argv, **kw) -> tuple[int, dict]:
    code, envelope = run(capsys, *argv, **kw)
    assert not envelope["ok"] and code == contract.EXIT[envelope["error"]["code"]], envelope
    return code, envelope["error"]


# ---------------------------------------------------------------- shapes


def conforms(value, shape: dict, where: str = "data") -> None:
    """A small JSON Schema check: types, enums, required fields, array items, oneOf."""
    if "oneOf" in shape:
        failures = []
        for option in shape["oneOf"]:
            try:
                return conforms(value, option, where)
            except AssertionError as e:
                failures.append(str(e))
        raise AssertionError(f"{where}: matches no option: {failures}")
    if "enum" in shape:
        assert value in shape["enum"], f"{where}: {value!r} not in {shape['enum']}"
    types = shape.get("type")
    if types:
        names = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict,
                 "array": list, "null": type(None)}
        allowed = types if isinstance(types, list) else [types]
        assert any(isinstance(value, names[t]) and not (t in ("integer", "number") and isinstance(value, bool))
                   for t in allowed), f"{where}: {value!r} isn't {types}"
    if isinstance(value, dict):
        for k in shape.get("required", []):
            assert k in value, f"{where}: no {k!r}"
        for k, sub in shape.get("properties", {}).items():
            if k in value:
                conforms(value[k], sub, f"{where}.{k}")
    if isinstance(value, list) and "items" in shape:
        for i, v in enumerate(value):
            conforms(v, shape["items"], f"{where}[{i}]")


# ---------------------------------------------------------------- the envelope and error codes


def test_success_and_failure_are_envelopes_with_their_exit_codes(cfg, capsys):
    item = data(capsys, "items", "add", "Call", "the", "plumber")
    conforms(item, schema.DATA["items add"])
    code, err = error(capsys, "items", "done", "999")
    assert code == 1 and err == {"code": "not_found", "message": "no item 999"}
    code, err = error(capsys, "items", "done", "nine")  # argparse's own complaint, as an envelope
    assert code == 2 and err["code"] == "usage" and "invalid int value" in err["message"]
    code, err = error(capsys, "settings", "set", "nope", "1")
    assert code == 1 and err["code"] == "invalid"
    code, err = error(capsys, "frobnicate")
    assert code == 2 and err["code"] == "usage"


def test_json_can_come_after_the_command(cfg, capsys):
    assert cli.main(["--local", "items", "add", "Buy", "milk", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["text"] == "Buy milk"


def test_config_errors_and_a_missing_config(cfg, capsys, tmp_path, monkeypatch):
    bad = tmp_path / "bad.toml"
    bad.write_text("[nonsense]\n")
    monkeypatch.setenv(config.ENV_VAR, str(bad))
    code, err = error(capsys, "status")
    assert code == 2 and err["code"] == "config" and "unknown section" in err["message"]
    monkeypatch.setenv(config.ENV_VAR, str(tmp_path / "none.toml"))
    code, err = error(capsys, "items")
    assert code == 3 and err["code"] == "not_set_up" and err["step"] == "app_folder"


def test_a_missing_setup_step_is_named(cfg, capsys, monkeypatch):
    data(capsys, "watch", "add", "/Meetings")
    monkeypatch.setattr(rm_setup.shutil, "which", lambda b: None)
    code, err = error(capsys, "collect")
    assert code == 3 and err["step"] == "remarkable.rmapi" and "jotted setup prepare" in err["message"]
    monkeypatch.setattr(rm_setup.shutil, "which", lambda b: "/opt/bin/rmapi")
    code, err = error(capsys, "library")
    assert code == 3 and err["step"] == "remarkable.connect" and "jotted connect --stdin" in err["message"]


def test_a_busy_device_says_retry(cfg, capsys, monkeypatch, rmapi):
    from jotted import locking

    def busy(self):
        raise locking.Busy("The library is busy with another job; try again in a moment")

    monkeypatch.setattr(locking._Held, "__enter__", busy)
    code, err = error(capsys, "library")
    assert code == 4 and err["code"] == "busy" and err["retry"] is True


def test_source_and_model_failures_have_their_own_codes(cfg, capsys, monkeypatch, rmapi):
    from jotted.llm import ModelError

    cfg.rmapi.token_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.rmapi.token_file.write_text("token")
    data(capsys, "watch", "add", "/Meetings")

    def unreachable(*a, **kw):
        raise cloud.CloudError("rmapi ls / failed: the cloud can't be reached")

    monkeypatch.setattr(cloud, "library", unreachable)
    monkeypatch.setattr(cloud, "documents", unreachable, raising=False)
    from jotted.plugins.remarkable.library import RemarkableLibrary

    monkeypatch.setattr(RemarkableLibrary, "list_documents", unreachable)
    code, err = error(capsys, "collect")
    assert code == 5 and err["code"] == "not_connected"

    def no_key(*a, **kw):
        raise ModelError("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr(RemarkableLibrary, "list_documents", no_key)
    code, err = error(capsys, "collect")
    assert code == 6 and err["code"] == "model_error"


def test_ctrl_c_is_130(cfg, capsys, monkeypatch):
    def interrupted(self, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(api.Jotted, "items", interrupted)
    code, err = error(capsys, "items")
    assert code == 130 and err["code"] == "interrupted"


# ---------------------------------------------------------------- no prompts under --json


def _sample(arg: dict) -> str:
    if "enum" in arg:
        return str(arg["enum"][0])
    return {"integer": "1", "number": "1"}.get(arg.get("type"), "x")


def _argv(command: dict) -> list[str]:
    argv = command["command"].split()
    for a in command["arguments"]:
        if a.get("positional") and a["required"]:
            argv.append(_sample(a))
    return argv


# Commands that run until stopped, or are the MCP server reading stdin.
LONG_RUNNING = {"serve", "mcp"}


def test_every_command_with_json_and_no_stdin_answers_with_an_envelope(cfg, tmp_path):
    """Run as a wrapper would: stdin closed, no terminal. Each command succeeds or fails with
    a code; none waits on a prompt."""
    commands = [c for c in schema.commands(cli.build_parser()) if c["command"] not in LONG_RUNNING]
    env = {**os.environ, "JOTTED_NO_UPDATE": "1", "JOTTED_HOME": str(tmp_path / "home")}
    env.pop("ANTHROPIC_API_KEY", None)

    def one(command: dict) -> tuple[str, int, str, str]:
        argv = [sys.executable, "-m", "jotted.cli", "--json", "--local", *_argv(command)]
        try:
            done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
                                  env=env, cwd=tmp_path, start_new_session=True)  # no controlling terminal
        except subprocess.TimeoutExpired:
            return command["command"], -1, "", "timed out: waiting on a prompt?"
        return command["command"], done.returncode, done.stdout, done.stderr

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(one, commands))
    for name, code, out, err in results:
        assert code != -1, f"{name}: {err}"
        try:
            envelope = json.loads(out)
        except ValueError:
            raise AssertionError(f"{name}: stdout isn't one JSON document: {out!r} (stderr: {err[-400:]})") from None
        assert envelope["v"] == contract.CONTRACT and isinstance(envelope["ok"], bool), name
        if envelope["ok"]:
            assert code == 0, name
        else:
            assert envelope["error"]["code"] in contract.EXIT and envelope["error"]["message"], name
            assert code == contract.EXIT[envelope["error"]["code"]], (name, code, envelope)
            assert envelope["error"]["code"] != "internal", (name, envelope, err[-800:])


def test_secrets_never_prompt_without_a_terminal(cfg, capsys, monkeypatch, rmapi):
    code, err = error(capsys, "ai", "key", "llm")
    assert code == 2 and err["code"] == "usage" and "--stdin" in err["message"]
    code, err = error(capsys, "connect")
    assert code == 2 and "--stdin" in err["message"]
    code, err = error(capsys, "setup")
    assert code == 2 and "setup status" in err["message"]
    code, err = error(capsys, "start")
    assert code == 2


# ---------------------------------------------------------------- connect


def test_connect_takes_the_code_on_stdin_and_replaces_only_when_asked(cfg, capsys, monkeypatch, rmapi):
    code, err = error(capsys, "connect", "--stdin", stdin="wrong123\n", monkeypatch=monkeypatch)
    assert code == 1 and err["code"] == "invalid" and "didn't work" in err["message"]
    assert data(capsys, "connect", "--stdin", stdin="ABCD1234\n", monkeypatch=monkeypatch) == {
        "connected": True, "replaced": True, "documents": 2}
    code, err = error(capsys, "connect", "--stdin", stdin="ABCD1234\n", monkeypatch=monkeypatch)
    assert err["code"] == "conflict" and "--replace" in err["message"]
    code, err = error(capsys, "connect", "--stdin", "--replace", stdin="nope\n", monkeypatch=monkeypatch)
    assert err["code"] == "invalid" and cfg.rmapi.token_file.read_text() == "token-ABCD1234"  # the old one is kept
    assert data(capsys, "connect", "--stdin", "--replace", stdin="ABCD1234\n", monkeypatch=monkeypatch)["connected"]


def test_auth_still_works_with_a_warning(cfg, capsys, monkeypatch, rmapi):
    monkeypatch.setattr(sys, "stdin", io.StringIO("ABCD1234\n"))
    assert cli.main(["--json", "auth", "--stdin"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["data"]["connected"] and "jotted connect" in captured.err


# ---------------------------------------------------------------- setup as steps


def test_setup_status_from_nothing_then_prepare(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv(config.ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(config.HOME_VAR, str(tmp_path / "home"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(rm_setup.shutil, "which", lambda b: b if os.path.isabs(b) and os.path.exists(b) else None)

    status = data(capsys, "setup", "status")
    conforms(status, schema.DATA["setup status"])
    assert [s["id"] for s in status["steps"]] == [
        "app_folder", "remarkable.rmapi", "remarkable.connect", "llm", "jev", "folders"]
    assert not status["complete"] and not any(s["done"] for s in status["steps"])
    by_id = {s["id"]: s for s in status["steps"]}
    assert by_id["remarkable.connect"]["command"] == "connect --stdin"
    assert by_id["llm"]["command"] == "ai key llm --stdin" and by_id["llm"]["provider"] == "anthropic"
    assert by_id["jev"]["optional"] and by_id["folders"]["command"] == "watch add PATH"

    def install(dest, *a):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "rmapi").write_text("#!/bin/sh\n")
        (dest / "rmapi").chmod(0o755)
        return dest / "rmapi"

    monkeypatch.setattr(rmapi_install, "install", install)
    prepared = data(capsys, "setup", "prepare")
    assert [p["id"] for p in prepared["prepared"]] == ["app_folder", "remarkable.rmapi"]
    by_id = {s["id"]: s for s in prepared["steps"]}
    assert by_id["app_folder"]["done"] and by_id["remarkable.rmapi"]["done"]
    assert "checksum verified" in by_id["remarkable.rmapi"]["detail"] and "command" not in by_id["app_folder"]
    assert data(capsys, "setup", "prepare")["prepared"] == []  # nothing left that needs no answer


def test_ai_key_on_stdin_then_provider_and_model(cfg, capsys, monkeypatch):
    from jotted.adapters.anthropic_llm import AnthropicLLM
    from jotted.llm import ModelError

    def verify(self):
        if not os.environ[self.cfg.api_key_env].startswith("good"):
            raise ModelError("Anthropic rejected this key")

    monkeypatch.setattr(AnthropicLLM, "verify", verify)
    code, err = error(capsys, "ai", "key", "llm", "--stdin", stdin="bad\n", monkeypatch=monkeypatch)
    assert code == 6 and err["code"] == "model_error" and "rejected" in err["message"]
    ai = data(capsys, "ai", "key", "llm", "--stdin", stdin="good-key-1234\n", monkeypatch=monkeypatch)
    assert ai["llm"]["key"]["set"] and keys.saved(cfg) == {"ANTHROPIC_API_KEY": "good-key-1234"}
    assert {s["id"]: s["done"] for s in data(capsys, "setup", "status")["steps"]}["llm"]

    code, err = error(capsys, "ai", "model", "gpt-4o")
    assert err["code"] == "invalid" and "images" in err["message"]
    assert data(capsys, "ai", "model", "claude-sonnet-5-5")["llm"]["model"] == "claude-sonnet-5-5"
    assert data(capsys, "ai")["llm"]["model"] == "claude-sonnet-5-5"  # saved, over config.toml's
    assert config.load().llm.model != "claude-sonnet-5-5"  # config.toml itself is untouched
    code, err = error(capsys, "ai", "provider", "openai")
    assert err["code"] == "invalid" and "anthropic" in err["message"]
    assert data(capsys, "ai", "provider", "anthropic")["llm"]["model"] == "claude-sonnet-5-5"  # same: kept


# ---------------------------------------------------------------- version, plugins, update


def test_version_reports_both_versions(cfg, capsys, monkeypatch):
    v = data(capsys, "version")
    conforms(v, schema.DATA["version"])
    assert v["contract"] == contract.CONTRACT and v["contract_min"] <= v["contract"]
    assert v["source_plugin"] == "remarkable" and not v["bundled"]
    plugins = data(capsys, "plugins")
    assert plugins["chosen"] == "remarkable" and plugins["installed"][0]["label"] == "reMarkable"


def test_a_bundled_copy_never_updates_itself(cfg, capsys, monkeypatch):
    monkeypatch.setenv(cli.BUNDLED_VAR, "1")
    code, err = error(capsys, "update")
    assert err["code"] == "conflict" and "app" in err["message"]
    assert data(capsys, "version")["bundled"]


def test_a_standalone_build_counts_as_bundled(cfg, capsys, monkeypatch, tmp_path):
    """A frozen build (scripts/build_standalone.py) can't be upgraded by uv: it never tries, and
    Claude Desktop runs it as the app's copy."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    code, err = error(capsys, "update")
    assert err["code"] == "conflict"
    assert data(capsys, "version")["bundled"]
    (tmp_path / "Claude").mkdir()
    monkeypatch.setenv("JOTTED_CLAUDE_CONFIG", str(tmp_path / "Claude" / "claude_desktop_config.json"))
    entry = data(capsys, "claude", "connect", "--command", sys.executable, "--dry-run")["entry"]
    assert entry["env"]["JOTTED_BUNDLED"] == "1"


# ---------------------------------------------------------------- schema


def test_schema_covers_every_command_and_operation(cfg, capsys):
    s = data(capsys, "schema")
    assert s["contract"] == contract.CONTRACT and set(s["operations"]) == set(api.OPERATIONS)
    named = {op for c in s["commands"] for op in c["operations"]}
    assert named == set(api.OPERATIONS)  # every operation is some command's
    missing = [c["command"] for c in s["commands"] if c["command"] not in schema.DATA]
    assert not missing, f"commands without a data shape: {missing}"
    items_list = next(c for c in s["commands"] if c["command"] == "items list")
    status = next(a for a in items_list["arguments"] if a["name"] == "status")
    assert status["enum"][:3] == ["open", "done", "all"] and status["default"] == "open"
    assert next(c for c in s["commands"] if c["command"] == "auth")["deprecated"]


def test_the_schema_only_grows_within_a_contract_version():
    """tests/contract/schema-v<N>.json records what contract N promised. Removing or renaming a
    command, argument, operation, error code or data field needs a new contract version."""
    gone = contract_docs.breaking()
    assert not gone, f"breaking changes without a new contract version: {gone}"


def test_outputs_match_their_declared_shapes(cfg, capsys):
    item = data(capsys, "items", "add", "Renew", "passport")
    for argv, shape in ((["items"], "items"), (["items", "done", str(item["id"])], "items done"),
                        (["settings"], "settings"), (["status"], "status"), (["ai"], "ai"),
                        (["events"], "events"), (["config", "check"], "config check")):
        conforms(data(capsys, *argv), schema.DATA[shape], " ".join(argv))


# ---------------------------------------------------------------- events


def test_every_change_writes_an_event(cfg, capsys):
    start = data(capsys, "events")["cursor"]
    item = data(capsys, "items", "add", "Book the venue")
    data(capsys, "items", "done", str(item["id"]))
    data(capsys, "items", "edit", str(item["id"]), "Book", "the", "big", "venue")
    data(capsys, "items", "dismiss", str(item["id"]))
    data(capsys, "watch", "add", "/Meetings")
    found = data(capsys, "events", "--since", str(start))
    assert [e["type"] for e in found["events"]] == [
        "item.added", "item.changed", "item.changed", "item.removed", "settings.changed"]
    added, done, edited, removed, settings = found["events"]
    assert added["item"]["text"] == "Book the venue" and done["item"]["status"] == "done"
    assert edited["item"]["text"] == "Book the big venue" and removed["item"] == {"id": item["id"]}
    assert settings["settings"]["watch"] == ["/Meetings"]
    assert found["cursor"] == settings["cursor"] and added["at"].endswith("Z")
    conforms(found, schema.DATA["events"])
    assert data(capsys, "events", "--since", str(found["cursor"]))["events"] == []
    data(capsys, "items", "add", "Unrelated")
    assert data(capsys, "items", "done", str(item["id"] + 1)) and all(  # a no-op edit writes nothing
        e["type"] != "item.changed" or e["item"]["id"] != item["id"]
        for e in data(capsys, "events", "--since", str(found["cursor"]))["events"])


def test_events_follow_sees_changes_from_another_process(cfg, tmp_path):
    env = {**os.environ, config.ENV_VAR: str(cfg.source)}
    follower = subprocess.Popen([sys.executable, "-m", "jotted.cli", "--json", "events", "--follow",
                                 "--interval", "0.1"], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                text=True, env=env)
    try:
        import time

        time.sleep(1.5)  # it starts from the latest cursor
        subprocess.run([sys.executable, "-m", "jotted.cli", "--json", "--local", "items", "add", "From", "elsewhere"],
                       env=env, capture_output=True, check=True)
        line = json.loads(follower.stdout.readline())
        assert line["v"] == 1 and line["type"] == "item.added" and line["item"]["text"] == "From elsewhere"
    finally:
        follower.kill()
        follower.wait()


def test_old_events_are_pruned(cfg, monkeypatch):
    from jotted.adapters import sqlite_repo

    repo = sqlite_repo.SqliteRepository(cfg.server.db)
    with repo.db() as db:
        db.execute("INSERT INTO events (at, type, data) VALUES ('2020-01-01T00:00:00Z', 'item.added', '{}')")
    monkeypatch.setattr(sqlite_repo, "PRUNE_EVERY", 1)
    repo.add_event("check.started")
    assert [e["type"] for e in repo.events()] == ["check.started"]


# ---------------------------------------------------------------- the fast path


@pytest.fixture
def server(cfg):
    """`jotted serve`'s app, really listening, with serve.json written as serve writes it."""
    from werkzeug.serving import make_server

    from jotted.server import create_app

    app = create_app(cfg, background=False)
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    fastpath.write(cfg, "127.0.0.1", srv.server_port, app.config["token"])
    yield srv
    fastpath.remove(cfg)
    srv.shutdown()
    thread.join()


def test_commands_go_to_a_running_server_with_the_same_output(cfg, capsys, server, monkeypatch):
    assert fastpath.server(cfg)["port"] == server.server_port
    item = data(capsys, "items", "add", "Water", "the", "plants")  # --local: in this process

    def both(*argv):
        local = run(capsys, *argv)
        code = cli.main(["--json", *argv])  # through the server
        remote = json.loads(capsys.readouterr().out)
        return local, (code, remote)

    real_open = cli._jotted
    calls = []
    monkeypatch.setattr(cli, "_jotted", lambda c: calls.append(1) or real_open(c))
    for argv in (["items"], ["items", "--status", "all"], ["settings"], ["status"], ["ai"],
                 ["items", "done", "999"], ["settings", "set", "action_threshold", "2"]):
        local, remote = both(*argv)
        assert local == remote, argv
    assert len(calls) == 7  # only the --local runs opened Jotted here

    code = cli.main(["--json", "items", "done", str(item["id"])])
    assert code == 0 and json.loads(capsys.readouterr().out)["data"]["status"] == "done"
    assert data(capsys, "items", "--status", "done")[0]["id"] == item["id"]  # the server changed the same store
    assert len(calls) == 8


def test_the_fast_path_steps_aside_for_a_stale_or_other_version_server(cfg, server, monkeypatch):
    info = fastpath.read(cfg)
    assert fastpath.server(cfg)
    monkeypatch.setattr(fastpath, "release", lambda: "9.9.9")
    assert fastpath.server(cfg) is None  # another version: run here
    monkeypatch.undo()
    fastpath.path(cfg).write_text(json.dumps({**info, "pid": 999999}))
    assert fastpath.server(cfg) is None  # gone without cleaning up
    assert oct(os.stat(fastpath.write(cfg, "127.0.0.1", 1, "t")).st_mode & 0o777) == "0o600"


def test_the_op_endpoint_needs_the_token(cfg):
    from jotted.server import create_app

    flask = create_app(cfg, background=False)
    c = flask.test_client()
    assert c.post("/api/op/items.list", json={}).status_code == 403
    token = {"X-Jotted-Token": flask.config["token"]}
    assert c.post("/api/op/items.list", json={}, headers=token).get_json() == {"v": 1, "ok": True, "data": []}
    r = c.post("/api/op/items.list", json={"colour": "red"}, headers=token)
    assert r.status_code == 400 and r.get_json()["error"]["code"] == "usage"
    assert c.post("/api/op/nope", json={}, headers=token).status_code == 404


# ---------------------------------------------------------------- MCP


def rpc(lines: list[dict]) -> list[dict]:
    out = io.StringIO()
    mcp.serve(io.StringIO("".join(json.dumps(m) + "\n" for m in lines)), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_mcp_serves_the_operations_as_tools_without_secrets(cfg):
    init, listed = rpc([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ])
    assert init["result"]["serverInfo"]["name"] == "jotted" and "tools" in init["result"]["capabilities"]
    names = {t["name"] for t in listed["result"]["tools"]}
    assert names == {"items_list", "items_get", "items_add", "items_propose", "items_edit", "items_done",
                     "items_reopen", "items_dismiss", "items_accept", "show_list", "status", "setup_status"}
    assert not any("key" in n or "connect" in n for n in names)
    add = next(t for t in listed["result"]["tools"] if t["name"] == "items_add")
    assert add["inputSchema"]["required"] == ["text"] and "propose" not in add["inputSchema"]["properties"]
    assert "agent" not in add["inputSchema"]["properties"] and "source" in add["inputSchema"]["properties"]
    done = next(t for t in listed["result"]["tools"] if t["name"] == "items_done")
    assert done["inputSchema"]["properties"]["id"]["type"] == "integer"
    assert next(t for t in listed["result"]["tools"] if t["name"] == "items_list")["annotations"]["readOnlyHint"]
    dismiss = next(t for t in listed["result"]["tools"] if t["name"] == "items_dismiss")
    assert dismiss["annotations"]["destructiveHint"]


def test_mcp_offers_settings_and_watching_only_with_admin(cfg):
    out = io.StringIO()
    mcp.serve(io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"), out, admin=True)
    names = {t["name"] for t in json.loads(out.getvalue())["result"]["tools"]}
    assert {"library", "watch", "settings_get", "settings_set", "collect", "check"} <= names
    (refused,) = rpc([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "settings_set", "arguments": {"key": "watch", "value": "[\"/\"]"}}}])
    assert refused["result"]["isError"] and "--admin" in refused["result"]["content"][0]["text"]


def test_mcp_tools_run_the_cli_commands(cfg):
    added, listed, done, missing, wrong, steps = rpc([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "items_add", "arguments": {"text": "Send the minutes"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "items_list", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "items_list", "arguments": {"status": "done", "owner": "mine"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "items_done", "arguments": {"id": 999}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "items_done", "arguments": {"x": 1}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "setup_status", "arguments": {}}},
    ])
    item = json.loads(added["result"]["content"][0]["text"])
    assert not added["result"]["isError"] and item["text"] == "Send the minutes"
    assert item["origin"] == "agent" and item["status"] == "open" and item["created"]  # asked for: straight on
    assert "bbox" not in item and "p_action" not in item  # text fields only
    page = json.loads(listed["result"]["content"][0]["text"])
    assert page["items"][0]["id"] == item["id"] and page["next_cursor"] is None
    assert not done["result"]["isError"] and json.loads(done["result"]["content"][0]["text"])["items"] == []
    assert missing["result"]["isError"] and json.loads(missing["result"]["content"][0]["text"])["code"] == "not_found"
    assert wrong["result"]["isError"]
    assert any(s["id"] == "llm" for s in json.loads(steps["result"]["content"][0]["text"])["steps"])
