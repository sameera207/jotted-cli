"""Build the standalone `jotted` for this machine: Python inside, for apps that bundle it.

    uv run --with pyinstaller python scripts/build_standalone.py

Writes build/release/jotted-<version>-macos-<arch>.tar.gz (a `jotted/` folder: the `jotted`
executable and `_internal/`) and a .sha256 next to it. This is what the Jotted app pins in its
jotted.lock (jotted-app/docs/bundling.md). CI builds one per architecture on each release tag
(.github/workflows/contract.yml).

PyInstaller --onedir, not --onefile: a single file unpacks Python into a temporary folder on
every run, which is slow and can't be signed with the app's identity. The app signs and
notarizes every file in the folder itself.

Before packaging, the build is run as an app would run it, with no Python on hand: version,
schema, the MCP widget, and a command against a scratch app folder.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
OUT = BUILD / "release"
ARCHES = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}


def version() -> str:
    import tomllib

    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def target() -> str:
    if sys.platform != "darwin":
        raise SystemExit("Standalone builds are macOS-only for now")
    arch = ARCHES.get(platform.machine().lower())
    if arch is None:
        raise SystemExit(f"Unknown architecture {platform.machine()!r}")
    return f"macos-{arch}"


def freeze() -> Path:
    """PyInstaller --onedir. Jotted's own modules are all collected, since plugins and adapters
    are imported by name, and its metadata is copied, since `version` reads it."""
    work = BUILD / "pyinstaller"
    shutil.rmtree(work, ignore_errors=True)
    subprocess.run([
        sys.executable, "-m", "PyInstaller", str(ROOT / "src" / "jotted" / "__main__.py"),
        "--name", "jotted", "--onedir", "--noconfirm", "--clean", "--log-level", "WARN",
        "--distpath", str(work / "dist"), "--workpath", str(work / "work"), "--specpath", str(work),
        "--collect-submodules", "jotted", "--collect-data", "jotted", "--copy-metadata", "jotted",
        "--exclude-module", "pytest",
    ], check=True, cwd=ROOT)
    folder = work / "dist" / "jotted"
    for dev in folder.rglob("mcp_ui/dev"):  # the widget's fake host: development only
        shutil.rmtree(dev)
    return folder


def run(exe: Path, *args: str, stdin: str = "", env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([str(exe), *args], input=stdin, capture_output=True, text=True, timeout=120,
                          env=env, check=False)


def smoke_test(folder: Path, expected_version: str) -> None:
    """The build works with nothing from this checkout or its Python: a scratch home and config,
    Python variables pointing nowhere useful, and a minimal PATH."""
    exe = folder / "jotted"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        poison = tmp / "poison"
        poison.mkdir()
        (poison / "rich.py").write_text("raise ImportError('PYTHONPATH reached the frozen build')\n")
        config = tmp / "config.toml"
        config.write_text((ROOT / "src" / "jotted" / "config.example.toml").read_text().replace(
            'db   = "./data/jotted.db"', f'db = "{tmp}/db.sqlite"'))
        env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp), "JOTTED_HOME": str(tmp / "home"),
               "JOTTED_CONFIG": str(config),
               "JOTTED_NO_UPDATE": "1", "JOTTED_CLAUDE_CONFIG": str(tmp / "claude" / "config.json"),
               "PYTHONPATH": str(poison), "PYTHONHOME": str(tmp / "nowhere")}

        def check(*args: str, stdin: str = "") -> dict:
            done = run(exe, "--json", *args, stdin=stdin, env=env)
            try:
                envelope = json.loads(done.stdout)
            except ValueError:
                raise SystemExit(f"`jotted {' '.join(args)}` printed no envelope:\n{done.stdout}\n{done.stderr}")
            return envelope

        v = check("version")
        assert v["ok"] and v["data"]["version"] == expected_version, v
        assert v["data"]["bundled"], "a standalone build must never update itself"
        schema = check("schema")["data"]
        published = json.loads((ROOT / "docs" / "schema.json").read_text())
        assert [c["command"] for c in schema["commands"]] == [c["command"] for c in published["commands"]], \
            "the build's commands differ from docs/schema.json"
        assert check("setup", "status")["ok"]
        added = check("items", "add", "Smoke", "test")
        assert added["ok"] and added["data"]["text"] == "Smoke test", added
        assert check("plugins")["data"]["installed"][0]["name"] == "remarkable"

        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": "ui://jotted/list"}}]
        mcp = run(exe, "mcp", stdin="".join(json.dumps(m) + "\n" for m in messages), env=env)
        replies = [json.loads(line) for line in mcp.stdout.splitlines()]
        html = replies[1]["result"]["contents"][0]["text"]
        assert "const Bridge" in html and "/* SCRIPTS */" not in html, "the widget isn't in the build"
    print(f"smoke test passed: {exe}")


def package(folder: Path, name: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    archive = OUT / f"{name}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(folder, arcname="jotted")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (OUT / f"{archive.name}.sha256").write_text(f"{digest}  {archive.name}\n")
    size = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
    print(f"{archive.relative_to(ROOT)}  {archive.stat().st_size / 1e6:.1f} MB packed, "
          f"{size / 1e6:.1f} MB unpacked  sha256 {digest}")
    return archive


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--skip-tests", action="store_true", help="package without the smoke test")
    args = p.parse_args()
    v = version()
    name = f"jotted-{v}-{target()}"
    folder = freeze()
    if not args.skip_tests:
        smoke_test(folder, v)
    package(folder, name)
    return 0


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(main())
