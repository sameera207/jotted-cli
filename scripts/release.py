"""Cutting a release: the steps the /release command (.claude/commands/release.md) runs.

    uv run python scripts/release.py plan           # is it safe, what changed, which version
    uv run python scripts/release.py prepare X.Y.Z  # set the version, regenerate the docs, write the notes

`plan` checks that main is clean, pushed and green in CI, compares the contract with the last
release's (tests/contract/schema-v<N>.json at that tag), and suggests a version:

- before 1.0: a new contract version → minor, with a warning (it breaks apps); additions to the
  contract → minor; anything else → patch;
- from 1.0: a new contract version → major; additions → minor; anything else → patch.

`prepare` writes build/release-notes.md, which becomes the tag's message and so the release
notes (release.yml). Committing, tagging and pushing are left to the person (or the command,
once they agree).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
NOTES = ROOT / "build" / "release-notes.md"
VERSION_LINE = re.compile(r'^version = "(?P<v>[^"]+)"$', re.M)
SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)")

Version = tuple[int, int, int]


def git(*args: str, check: bool = True) -> str:
    done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if check and done.returncode != 0:
        raise SystemExit(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout.strip() if done.returncode == 0 else ""


def parse(v: str) -> Version:
    m = SEMVER.fullmatch(v.removeprefix("v"))
    if not m:
        raise ValueError(f"{v!r} isn't a version like 0.2.0")
    return tuple(int(n) for n in m.groups())  # type: ignore[return-value]


def fmt(v: Version) -> str:
    return ".".join(map(str, v))


def project_version(text: str | None = None) -> str:
    text = (ROOT / "pyproject.toml").read_text() if text is None else text
    return VERSION_LINE.search(text)["v"]


def with_version(text: str, version: str) -> str:
    """pyproject.toml's text with [project]'s version set (the first `version = ` line)."""
    return VERSION_LINE.sub(f'version = "{version}"', text, count=1)


def last_tag() -> str | None:
    return git("describe", "--tags", "--abbrev=0", "--match", "v[0-9]*", check=False) or None


# ---------------------------------------------------------------- what changed


def contract_at(ref: str) -> dict | None:
    """The contract baseline a release promised (tests/contract/schema-v<N>.json at `ref`)."""
    names = [n for n in git("ls-tree", "--name-only", ref, "tests/contract/", check=False).splitlines()
             if re.fullmatch(r"tests/contract/schema-v\d+\.json", n)]
    if not names:
        return None
    newest = max(names, key=lambda n: int(re.search(r"v(\d+)", n)[1]))
    return json.loads(git("show", f"{ref}:{newest}"))


def contract_changes(old: dict | None, new: dict) -> dict:
    """{"contract": (old N, new N), "commands": [new commands], "by_command": {command: [what was added]},
    "errors": [new codes]}, from two baselines (contract_docs.baseline)."""
    import contract_docs

    out: dict = {"contract": (old["contract"] if old else None, new["contract"]), "commands": [],
                 "by_command": {}, "errors": []}
    if old is None:
        return out
    out["errors"] = [c for c in new["errors"] if c not in old["errors"]]
    for name, c in new["commands"].items():
        was = old["commands"].get(name)
        if was is None:
            out["commands"].append(name)
            continue
        options = [a for a in c["arguments"] if a not in was["arguments"]]
        fields = sorted({p.lstrip(".").removeprefix("[].").removeprefix("|0.").removeprefix("|1.")
                         for p in contract_docs._gone_fields(c["data"], was["data"], "")})
        parts = ([("option" if len(options) == 1 else "options") + " " + ", ".join(f"`{o}`" for o in options)]
                 if options else []) + \
                ([("field" if len(fields) == 1 else "fields") + " " + ", ".join(f"`{f}`" for f in fields)]
                 if fields else [])
        if parts:
            out["by_command"][name] = parts
    return out


def suggest(last: str | None, changes: dict, commits: list[str], current: str) -> tuple[str | None, str]:
    """The next version and why."""
    if last is None:
        return current, "the first release"
    if not commits:
        return None, f"nothing has changed since {last}"
    major, minor, patch = parse(last)
    old_n, new_n = changes["contract"]
    if old_n is not None and new_n != old_n:
        if major == 0:
            return fmt((0, minor + 1, 0)), f"contract {old_n} → {new_n}: BREAKING for apps (minor, before 1.0)"
        return fmt((major + 1, 0, 0)), f"contract {old_n} → {new_n}: BREAKING for apps"
    if changes["commands"] or changes["by_command"] or changes["errors"]:
        return fmt((major, minor + 1, 0)), "additions to the contract"
    return fmt((major, minor, patch + 1)), "fixes and changes that don't touch the contract"


def notes(version: str, last: str | None, changes: dict, commits: list[str]) -> str:
    old_n, new_n = changes["contract"]
    lines = [f"Jotted {version}", ""]
    if last is None:
        lines += ["The first release.", "", f"Contract {new_n}."]
    elif old_n is not None and old_n != new_n:
        lines += [f"**Contract {new_n}** (was {old_n}): a breaking change. Apps built for contract {old_n} "
                  "must update; see docs/cli-contract.md."]
    else:
        lines += [f"Contract {new_n}, unchanged" + (": additions only." if changes["by_command"] or
                                                     changes["commands"] or changes["errors"] else ".")]
    if changes["commands"]:
        lines += ["", "New commands:", ""] + [f"- `jotted {c}`" for c in changes["commands"]]
    if changes["by_command"]:
        lines += ["", "Added to existing commands:", ""] + \
                 [f"- `jotted {c}`: {'; '.join(parts)}" for c, parts in changes["by_command"].items()]
    if changes["errors"]:
        lines += ["", "New error codes: " + ", ".join(f"`{c}`" for c in changes["errors"])]
    if commits:
        lines += ["", "## Changes", ""] + [f"- {c}" for c in commits]
    return "\n".join(lines) + "\n"


def commits_since(tag: str | None) -> list[str]:
    log = git("log", "--format=%s (%h)", f"{tag}..HEAD" if tag else "HEAD")
    return [c for c in log.splitlines() if c and not c.startswith("Release v")]


# ---------------------------------------------------------------- is it safe


def checks() -> list[tuple[bool, str]]:
    out = []
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    out.append((branch == "main", f"on main (on {branch})" if branch != "main" else "on main"))
    dirty = git("status", "--porcelain", "--untracked-files=no")
    out.append((not dirty, "no uncommitted changes" if not dirty else "uncommitted changes:\n" + dirty))
    git("fetch", "--quiet", "--tags", "origin", check=False)
    head, remote = git("rev-parse", "HEAD"), git("rev-parse", "origin/main", check=False)
    out.append((head == remote, "pushed: main is the same as origin/main" if head == remote else
                "main and origin/main differ: push or pull first"))
    try:
        done = subprocess.run(["gh", "run", "list", "--workflow", "contract.yml", "--commit", head, "--limit", "1",
                               "--json", "status,conclusion"], cwd=ROOT, capture_output=True, text=True, timeout=30)
        runs = json.loads(done.stdout or "[]") if done.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        runs = None
    if runs is None:
        out.append((False, "couldn't ask GitHub about CI (is `gh` installed and logged in?)"))
    elif not runs:
        out.append((False, "CI hasn't run on this commit yet"))
    elif runs[0]["status"] != "completed":
        out.append((False, "CI is still running on this commit"))
    else:
        out.append((runs[0]["conclusion"] == "success", f"CI on this commit: {runs[0]['conclusion']}"))
    return out


# ---------------------------------------------------------------- commands


def plan() -> int:
    import contract_docs

    for ok, what in checks():
        print(f"{'✓' if ok else '✗'} {what}")
    last = last_tag()
    commits = commits_since(last)
    changes = contract_changes(contract_at(last) if last else None, contract_docs.baseline(contract_docs.schema_data()))
    version, why = suggest(last, changes, commits, project_version())
    print(f"\nLast release: {last or 'none'}. pyproject.toml: {project_version()}.")
    if version is None:
        print(f"Nothing to release: {why}.")
        return 1
    print(f"Suggested version: {version} ({why}).\n")
    print(notes(version, last, changes, commits))
    return 0


def prepare(version: str) -> int:
    import contract_docs

    parse(version)
    last = last_tag()
    if last and parse(version) <= parse(last):
        raise SystemExit(f"{version} isn't after the last release, {last}")
    if git("tag", "-l", f"v{version}") or git("ls-remote", "--tags", "origin", f"v{version}", check=False):
        raise SystemExit(f"v{version} already exists")
    pyproject = ROOT / "pyproject.toml"
    pyproject.write_text(with_version(pyproject.read_text(), version))
    # uv.lock records the project's version; the docs record it too (schema.json).
    subprocess.run(["uv", "lock"], cwd=ROOT, check=True, capture_output=True)
    subprocess.run(["uv", "run", "python", "scripts/contract_docs.py"], cwd=ROOT, check=True)
    commits = commits_since(last)
    changes = contract_changes(contract_at(last) if last else None, contract_docs.baseline(contract_docs.schema_data()))
    NOTES.parent.mkdir(exist_ok=True)
    NOTES.write_text(notes(version, last, changes, commits))
    print(f"\nVersion {version} set; the docs are regenerated; the notes are in {NOTES.relative_to(ROOT)}.")
    print("Next, once the diff looks right:")
    print(f'  git commit -am "Release v{version}"')
    print(f"  git tag -a v{version} -F {NOTES.relative_to(ROOT)}")
    print(f"  git push --atomic origin main v{version}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="step", required=True)
    sub.add_parser("plan", help="checks, what changed since the last release, the version to suggest")
    sub.add_parser("prepare", help="set the version, regenerate the docs, write the notes").add_argument("version")
    args = p.parse_args()
    return plan() if args.step == "plan" else prepare(args.version)


if __name__ == "__main__":
    sys.exit(main())
