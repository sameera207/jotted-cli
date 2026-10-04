"""Install and first run: where settings live, saved keys, rmapi download, guided setup."""

import hashlib
import io
import json
import os
import stat
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from jotted import cli, config, keys, onboarding, selfupdate  # noqa: E402
from jotted.plugins.remarkable import cloud, rmapi_install, setup as rm_setup  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fresh machine: no config anywhere, no keys in the environment."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(config.ENV_VAR, raising=False)
    monkeypatch.setenv(config.HOME_VAR, str(tmp_path / "home"))
    for name in ("ANTHROPIC_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "home"


# ---------------------------------------------------------------- settings location


def test_config_path_prefers_env_then_this_folder_then_the_app_home(home, tmp_path, monkeypatch):
    assert config.resolve_path() == home / "config.toml"
    (tmp_path / "config.toml").write_text("")
    assert config.resolve_path() == config.DEFAULT_PATH  # a checkout keeps working
    monkeypatch.setenv(config.ENV_VAR, str(tmp_path / "elsewhere.toml"))
    assert config.resolve_path() == tmp_path / "elsewhere.toml"


def test_created_config_has_every_default_and_paths_beside_it(home):
    cfg = config.load(config.create(home / "config.toml"))
    assert cfg.paths.cache_dir == (home / "cache").resolve() and cfg.server.db == (home / "data/jotted.db").resolve()
    assert cfg.rmapi.token_file == (home / ".secrets/rmapi.conf").resolve()


def test_set_value_edits_in_place_and_keeps_comments(home):
    path = config.create(home / "config.toml")
    config.set_value(path, "rmapi", "binary", "/opt/rm api/rmapi")
    text = path.read_text()
    assert 'binary     = "/opt/rm api/rmapi"' in text and "# name on PATH" in text
    assert config.load(path).rmapi.binary == "/opt/rm api/rmapi"
    config.set_value(path, "rmapi", "brand_new", "x")  # a key not there yet goes at the end of its section
    import tomllib
    raw = tomllib.loads(path.read_text())
    assert raw["rmapi"]["brand_new"] == "x" and raw["rmapi"]["timeout_s"] == 120 and "brand_new" not in raw["strokes"]


# ---------------------------------------------------------------- saved keys


def test_saved_keys_are_private_and_the_environment_wins(home, monkeypatch):
    cfg = config.load(config.create(home / "config.toml"))
    keys.save(cfg, "ANTHROPIC_API_KEY", "sk-saved")
    keys.save(cfg, "TYPESAFE_API_KEY", "ts-saved")
    assert stat.S_IMODE(keys.path(cfg).stat().st_mode) == 0o600
    assert stat.S_IMODE(cfg.paths.secrets_dir.stat().st_mode) == 0o700
    assert keys.saved(cfg) == {"ANTHROPIC_API_KEY": "sk-saved", "TYPESAFE_API_KEY": "ts-saved"}
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-from-shell")
    keys.load_into_env(cfg)
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-saved" and os.environ["TYPESAFE_API_KEY"] == "ts-from-shell"


# ---------------------------------------------------------------- rmapi download


def _archive(name="rmapi", body=b"#!/bin/sh\necho rmapi\n"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, body)
    return buf.getvalue()


def test_rmapi_install_checks_the_sum_and_unpacks_the_binary(tmp_path, monkeypatch):
    data = _archive()
    monkeypatch.setattr(rmapi_install, "ASSETS", {("darwin", "arm64"): ("rmapi-macos-arm64.zip", hashlib.sha256(data).hexdigest())})
    monkeypatch.setattr(rmapi_install, "_download", lambda url: data)
    binary = rmapi_install.install(tmp_path / "bin", "Darwin", "arm64")
    assert binary.read_bytes() == b"#!/bin/sh\necho rmapi\n" and os.access(binary, os.X_OK)

    monkeypatch.setattr(rmapi_install, "_download", lambda url: _archive(body=b"tampered"))
    with pytest.raises(rmapi_install.InstallError, match="checksum"):
        rmapi_install.install(tmp_path / "bin2", "Darwin", "arm64")
    assert not (tmp_path / "bin2").exists()


def test_rmapi_builds_are_known_for_common_machines():
    assert rmapi_install.asset_for("Darwin", "arm64")[0] == "rmapi-macos-arm64.zip"
    assert rmapi_install.asset_for("Darwin", "x86_64")[0] == "rmapi-macos-intel.zip"
    assert rmapi_install.asset_for("Linux", "x86_64")[0] == "rmapi-linux-amd64.tar.gz"
    assert rmapi_install.asset_for("Linux", "arm64")[0] == "rmapi-linux-arm64.tar.gz"
    assert rmapi_install.asset_for("Windows", "AMD64")[0] == "rmapi-win64.zip"
    assert rmapi_install.asset_for("Plan9", "mips") is None


# ---------------------------------------------------------------- guided setup


class ScriptedUI:
    """Answers prompts in order; records everything shown."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.shown: list[str] = []

    def _next(self, prompt):
        self.shown.append(f"? {prompt}")
        if not self.answers:
            raise AssertionError(f"unexpected prompt: {prompt}")
        return self.answers.pop(0)

    def step(self, t): self.shown.append(f"# {t}")
    def done(self, t): self.shown.append(f"✓ {t}")
    def info(self, t): self.shown.append(f"  {t}")
    def warn(self, t): self.shown.append(f"! {t}")
    def ask(self, p): return self._next(p)
    def secret(self, p): return self._next(p)
    def confirm(self, p, default=True): return self._next(p)


@pytest.fixture
def world(home, monkeypatch, tmp_path):
    """rmapi downloadable, the cloud reachable with code ABCD1234, keys starting 'good' valid."""
    calls = {"register": [], "verify": []}

    def install(dest, *a):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "rmapi").write_text("#!/bin/sh\n")
        (dest / "rmapi").chmod(0o755)
        return dest / "rmapi"

    def register(cfg, code):
        calls["register"].append(code)
        if code != "ABCD1234":
            raise cloud.CloudError("rmapi ls / failed (exit 1):\ncode rejected")
        cfg.rmapi.token_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.rmapi.token_file.write_text("token")

    def make_verify(label, error):
        def verify(self):
            key = os.environ[self.cfg.api_key_env]
            calls["verify"].append((label, key))
            if not key.startswith("good"):
                raise error(f"{label} rejected this key")
        return verify

    from jotted.adapters.anthropic_llm import AnthropicLLM
    from jotted.adapters.typesafe_judge import TypeSafeJudge
    from jotted.llm import ModelError

    monkeypatch.setattr(rmapi_install, "install", install)
    monkeypatch.setattr(rm_setup.shutil, "which", lambda b: b if os.path.isabs(b) and os.path.exists(b) else None)
    monkeypatch.setattr(cloud, "register", register)
    monkeypatch.setattr(cloud, "library", lambda cfg: (["a", "b", "c"], []))
    for name in ("ANTHROPIC_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(AnthropicLLM, "verify", make_verify("Anthropic", ModelError))
    monkeypatch.setattr(TypeSafeJudge, "verify", make_verify("TypeSafe", ModelError))
    return calls


def test_first_run_goes_from_nothing_to_ready(world, home):
    ui = ScriptedUI(True, "wrong123", "ABCD1234", "bad-key", "good-anthropic", True, "good-typesafe")
    cfg = onboarding.run(ui)

    assert (home / "config.toml").is_file() and cfg.rmapi.binary == str(home / "bin" / "rmapi")
    assert world["register"] == ["wrong123", "ABCD1234"]
    assert world["verify"] == [("Anthropic", "bad-key"), ("Anthropic", "good-anthropic"), ("TypeSafe", "good-typesafe")]
    assert "✓ Connected: 3 document(s) in your library" in ui.shown
    assert any(s.startswith("! Anthropic rejected this key") for s in ui.shown)
    assert keys.saved(cfg) == {"ANTHROPIC_API_KEY": "good-anthropic", "TYPESAFE_API_KEY": "good-typesafe"}
    assert "bad-key" not in json.dumps(keys.saved(cfg))


def test_second_run_asks_nothing(world, home, monkeypatch):
    onboarding.run(ScriptedUI(True, "ABCD1234", "good-a", True, "good-t"))
    for name in ("ANTHROPIC_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(name)  # a new shell: keys come back from the saved file
    ui = ScriptedUI()
    onboarding.run(ui)
    assert [s for s in ui.shown if s.startswith("✓")] == [
        f"✓ rmapi: {home / 'bin' / 'rmapi'}", "✓ reMarkable connected", "✓ Anthropic key",
        "✓ Jev plugin (TypeSafe key)"]


def test_setup_again_keeps_what_is_there_by_default(world, home):
    onboarding.run(ScriptedUI(True, "ABCD1234", "good-a", True, "good-t"))
    ui = ScriptedUI(False, "", True, "good-new-t")  # keep the tablet, keep Anthropic, replace TypeSafe
    cfg = onboarding.run(ui, redo=True)
    assert keys.saved(cfg) == {"ANTHROPIC_API_KEY": "good-a", "TYPESAFE_API_KEY": "good-new-t"}
    assert cfg.rmapi.token_file.read_text() == "token"
    cfg = onboarding.run(ScriptedUI(False, "", False), redo=True)  # stop using Jev
    assert keys.saved(cfg) == {"ANTHROPIC_API_KEY": "good-a"} and "TYPESAFE_API_KEY" not in os.environ


def test_jev_is_optional_and_not_asked_about_again(world, home):
    cfg = onboarding.run(ScriptedUI(True, "ABCD1234", "good-a", False))
    assert keys.saved(cfg) == {"ANTHROPIC_API_KEY": "good-a"}
    ui = ScriptedUI()  # a later start: no question about Jev
    onboarding.run(ui)
    assert not any("Jev" in line for line in ui.shown)


def test_declining_rmapi_stops_with_directions(world, home):
    with pytest.raises(onboarding.SetupError, match="github.com/ddvk/rmapi/releases"):
        onboarding.run(ScriptedUI(False))


def test_a_failed_reconnect_keeps_the_old_connection(world, home):
    onboarding.run(ScriptedUI(True, "ABCD1234", "good-a", True, "good-t"))
    with pytest.raises(onboarding.SetupError, match="previous connection is kept"):
        onboarding.run(ScriptedUI(True, "nope1234", "nope1234", "nope1234"), redo=True)
    cfg = config.load(home / "config.toml")
    assert cfg.rmapi.token_file.read_text() == "token"


# ---------------------------------------------------------------- start


def test_start_runs_without_the_browser_unless_asked_and_reuses_a_running_app(world, home, monkeypatch):
    opened, served, real_serve = [], [], cli._serve
    monkeypatch.setattr(cli, "can_prompt", lambda args: True)  # a person at a terminal
    monkeypatch.setattr("builtins.input", lambda prompt="": {"y": "y"}.get("y"))  # rmapi: yes
    monkeypatch.setattr(onboarding.ConsoleUI, "ask", lambda self, p: "ABCD1234")
    keys_iter = iter(["good-a", "good-t"])
    monkeypatch.setattr(onboarding.ConsoleUI, "secret", lambda self, p: next(keys_iter))
    monkeypatch.setattr(cli, "_serve", lambda cfg, host, port, **kw: served.append((port, kw["open_path"])) or 0)
    assert cli.main(["start"]) == 0
    assert served == [(8765, None)]  # the Jotted app is the way in: no browser
    assert cli.main(["start", "--browser"]) == 0
    assert served[-1] == (8765, "/#settings")  # nothing watched yet: Settings first

    import webbrowser
    monkeypatch.setattr(webbrowser, "open", opened.append)
    monkeypatch.setattr(cli, "_running_here", lambda url: True)

    class Busy:
        def __init__(self, *a): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def connect_ex(self, addr): return 0

    import socket
    monkeypatch.setattr(socket, "socket", Busy)
    cfg = config.load(home / "config.toml")
    assert real_serve(cfg, "127.0.0.1", 8765, open_path="/") == 0
    assert opened == ["http://127.0.0.1:8765/"]


# ---------------------------------------------------------------- self-update

GIT_INSTALL = json.dumps({"url": "https://github.com/someone/jotted",
                          "vcs_info": {"vcs": "git", "commit_id": "a" * 40}})


def test_only_an_unpinned_github_install_updates_itself():
    assert selfupdate.installed(GIT_INSTALL) == selfupdate.Install(repo="someone/jotted", commit="a" * 40)
    pinned = json.dumps({"url": "https://github.com/someone/jotted",
                         "vcs_info": {"vcs": "git", "commit_id": "a" * 40, "requested_revision": "v1"}})
    checkout = json.dumps({"url": "file:///src/jotted", "dir_info": {"editable": True}})
    assert selfupdate.installed(pinned) is None
    assert selfupdate.installed(checkout) is None


@pytest.fixture
def update_world(monkeypatch):
    """An install at commit aaa…; GitHub's head and the upgrade are scripted."""
    state = {"commit": "a" * 40, "head": "a" * 40, "upgrades": 0}
    monkeypatch.delenv(selfupdate.SKIP_VAR, raising=False)
    monkeypatch.delenv(selfupdate.DONE_VAR, raising=False)
    monkeypatch.setattr(selfupdate, "installed", lambda: selfupdate.Install("someone/jotted", state["commit"]))
    monkeypatch.setattr(selfupdate, "latest", lambda repo: state["head"])

    def upgrade():
        state["upgrades"] += 1
        state["commit"] = state["head"]

    monkeypatch.setattr(selfupdate, "upgrade", upgrade)
    return state


def test_start_updates_and_reruns_when_github_is_ahead(update_world, monkeypatch):
    update_world["head"] = "b" * 40
    monkeypatch.setattr(cli, "can_prompt", lambda args: True)
    reran = []
    monkeypatch.setattr(cli, "_rerun", lambda: reran.append(True) or (_ for _ in ()).throw(SystemExit(0)))
    with pytest.raises(SystemExit):
        cli.main(["start"])
    assert update_world["upgrades"] == 1 and reran == [True]


def test_no_update_when_current_offline_skipped_or_already_rerun(update_world, monkeypatch):
    console = cli.console
    assert selfupdate.check(console) is False  # up to date
    update_world["head"] = None  # offline
    assert selfupdate.check(console) is False
    update_world["head"] = "b" * 40
    monkeypatch.setenv(selfupdate.DONE_VAR, "1")  # the re-run after an update
    assert selfupdate.check(console) is False
    monkeypatch.delenv(selfupdate.DONE_VAR)
    monkeypatch.setenv(selfupdate.SKIP_VAR, "1")
    assert selfupdate.check(console) is False
    assert update_world["upgrades"] == 0
    assert selfupdate.check(console, force=True) is True  # `jotted update` ignores both
    assert update_world["upgrades"] == 1


def test_a_failed_upgrade_carries_on(update_world, monkeypatch):
    update_world["head"] = "b" * 40

    def fail():
        raise selfupdate.UpdateError("no network")

    monkeypatch.setattr(selfupdate, "upgrade", fail)
    assert selfupdate.check(cli.console) is False


def test_a_folder_from_the_old_name_is_moved(tmp_path, monkeypatch):
    monkeypatch.delenv(config.HOME_VAR, raising=False)
    monkeypatch.setattr(config.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    (tmp_path / "rmtasks").mkdir()
    (tmp_path / "rmtasks" / "config.toml").write_text("x")
    assert config.app_home() == tmp_path / "jotted"
    assert (tmp_path / "jotted" / "config.toml").read_text() == "x" and not (tmp_path / "rmtasks").exists()
    (tmp_path / "rmtasks").mkdir()  # both exist: the new one wins, the old is left alone
    assert config.app_home() == tmp_path / "jotted" and (tmp_path / "rmtasks").exists()
