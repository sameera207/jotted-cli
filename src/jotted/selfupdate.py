"""Keep an installed Jotted up to date with its GitHub releases. Checked by `jotted start` and
`jotted update`.

Only an install made with `uv tool install git+<repo>` updates itself, and only to a published
release: a push to main reaches nobody until it is released. uv records what it installed
(direct_url.json): unpinned (main), or pinned to a release tag (`@v0.2.0`, as this module
installs it). When GitHub's latest release is newer than this copy's version, it reinstalls at
that tag. An install pinned to anything else (a commit, a branch), a checkout run with
`uv run`, or a copy bundled in an app is never touched. Offline or rate-limited, the check is skipped.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from importlib import metadata

DIST = "jotted"
SKIP_VAR = "JOTTED_NO_UPDATE"  # set to 1 to never check
DONE_VAR = "JOTTED_UPDATED"  # set on the re-run after an update, so it happens once
RELEASE_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class Install:
    repo: str  # "owner/name" on GitHub
    version: str  # this copy's release version (0.1.0)


def release(tag_or_version: str | None) -> tuple[int, int, int] | None:
    """(0, 2, 0) for "v0.2.0" or "0.2.0"; None for anything that isn't a plain release."""
    m = RELEASE_TAG.fullmatch(tag_or_version or "") or RELEASE_TAG.fullmatch("v" + (tag_or_version or ""))
    return tuple(int(n) for n in m.groups()) if m else None  # type: ignore[return-value]


def installed(direct_url: str | None = None, version: str | None = None) -> Install | None:
    """The GitHub repo this copy was installed from, and its version; None for a checkout, a copy
    pinned to anything but a release tag, or one uv didn't install from GitHub."""
    try:
        if direct_url is None:
            direct_url = metadata.distribution(DIST).read_text("direct_url.json")
        if version is None:
            version = metadata.version(DIST)
    except metadata.PackageNotFoundError:
        return None
    if not direct_url:
        return None
    info = json.loads(direct_url)
    vcs = info.get("vcs_info") or {}
    url = info.get("url", "")
    if vcs.get("vcs") != "git" or "github.com/" not in url:
        return None
    pinned = vcs.get("requested_revision")
    if pinned and release(pinned) is None:
        return None  # pinned by hand to a commit or branch: leave it alone
    repo = url.split("github.com/", 1)[1].removesuffix(".git").strip("/")
    return Install(repo=repo, version=version)


def latest(repo: str, timeout: float = 4) -> str | None:
    """The tag of the latest published release on GitHub (v0.2.0); None if there is none yet
    or GitHub can't be reached. Drafts and pre-releases aren't "latest"."""
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": DIST})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
            tag = json.loads(resp.read()).get("tag_name")
    except (OSError, ValueError):
        return None
    return tag if release(tag) else None


def upgrade(repo: str, tag: str) -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise UpdateError(f"uv is not on PATH; run `uv tool install --force git+https://github.com/{repo}@{tag}`")
    done = subprocess.run([uv, "tool", "install", "--force", f"git+https://github.com/{repo}@{tag}"],
                          capture_output=True, text=True)
    if done.returncode != 0:
        raise UpdateError((done.stderr or done.stdout).strip() or f"uv exited with {done.returncode}")


def check(console, *, force: bool = False) -> bool:
    """Update if GitHub has a newer release. True if this copy was replaced (re-run to use it)."""
    if not force and (os.environ.get(SKIP_VAR) or os.environ.get(DONE_VAR)):
        return False
    have = installed()
    if have is None:
        if force:
            console.print("This copy isn't a `uv tool install` from GitHub, so it doesn't update itself.")
        return False
    tag = latest(have.repo)
    if tag is None:
        if force:
            console.print("[yellow]Couldn't find a release on GitHub to update to.[/yellow]")
        return False
    if release(have.version) is not None and release(tag) <= release(have.version):
        if force:
            console.print(f"Jotted is up to date ({have.version}).")
        return False
    console.print(f"Updating Jotted ({have.version} → {tag.removeprefix('v')})…")
    try:
        upgrade(have.repo, tag)
    except UpdateError as e:
        console.print(f"[yellow]Update failed, carrying on with this version:[/yellow] {e}")
        return False
    now = installed()
    if now is None or now.version == have.version:
        console.print("[yellow]uv didn't pick up the new version; carrying on with this one.[/yellow]")
        return False
    console.print(f"[green]Updated to {now.version}.[/green]")
    return True
