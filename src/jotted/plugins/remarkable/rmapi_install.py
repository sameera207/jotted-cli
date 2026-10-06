"""Download rmapi (the ddvk fork) for this machine, checked against pinned SHA-256 sums.

rmapi is a separate program, so it is fetched from its own GitHub release rather than
bundled. Each archive holds a single binary. The sums are the ones GitHub publishes for
the release; a download that doesn't match is refused.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import tarfile
import tempfile
import zipfile
from pathlib import Path

from ... import net

VERSION = "v0.0.35"
URL = "https://github.com/ddvk/rmapi/releases/download/{version}/{asset}"

# (system, machine) -> (asset, sha256)
ASSETS = {
    ("darwin", "arm64"): ("rmapi-macos-arm64.zip", "fb55b6c782b83dc0ccbb236359d738727c4c600378feae920fcab505862f624a"),
    ("darwin", "x86_64"): ("rmapi-macos-intel.zip", "1afc2c4d73ddde60e22e0ff8d2bc737450dfcaeaa723eb6a3b863ac96cb2102c"),
    ("linux", "x86_64"): ("rmapi-linux-amd64.tar.gz", "117616151d11937446ead6972b0934f97155443087f8406141db38fd1ac8fb25"),
    ("linux", "aarch64"): ("rmapi-linux-arm64.tar.gz", "645c170d8119b4dcb652cf79612e362fcb032dc0e5869ff520eec1324da39637"),
    ("windows", "amd64"): ("rmapi-win64.zip", "f8132c68044f054b2cca14c30d4525ea0fd78b2af175998d2aad5b3c8afcb05b"),
    ("windows", "arm64"): ("rmapi-win-arm64.zip", "a8c540f72a17228b2a5027d8c26332db9718de3d049742788c0cdd9e30d20268"),
}
ALIASES = {"amd64": "x86_64", "arm64": "aarch64"}  # linux reports either spelling


class InstallError(Exception):
    pass


def asset_for(system: str | None = None, machine: str | None = None) -> tuple[str, str] | None:
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    return ASSETS.get((system, machine)) or ASSETS.get((system, ALIASES.get(machine, machine)))


def _download(url: str) -> bytes:
    with net.urlopen(url, timeout=120) as resp:
        return resp.read()


def install(dest_dir: Path, system: str | None = None, machine: str | None = None) -> Path:
    """Download, verify and unpack rmapi into `dest_dir`; returns the binary's path."""
    found = asset_for(system, machine)
    if found is None:
        raise InstallError(f"no rmapi build for {platform.system()} {platform.machine()}; "
                           "download one from https://github.com/ddvk/rmapi/releases")
    asset, sha256 = found
    try:
        data = _download(URL.format(version=VERSION, asset=asset))
    except OSError as e:
        if net.untrusted(e):
            machine = "this Mac" if platform.system() == "Darwin" else "this computer"
            raise InstallError(f"Couldn't download rmapi: {machine} didn't trust github.com's certificate. "
                               "If you're on a work network, it may inspect HTTPS; ask IT, or install rmapi "
                               "yourself and set `rmapi.binary`.") from e
        raise InstallError(f"could not download rmapi: {e}") from e
    if hashlib.sha256(data).hexdigest() != sha256:
        raise InstallError(f"{asset} did not match its published checksum; not installed")

    name = "rmapi.exe" if asset.startswith("rmapi-win") else "rmapi"
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            member = next((m for m in z.namelist() if m.rsplit("/", 1)[-1] == name), None)
            binary = z.read(member) if member else None
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            member = next((m for m in t.getmembers() if m.isfile() and m.name.rsplit("/", 1)[-1] == name), None)
            binary = t.extractfile(member).read() if member else None
    if binary is None:
        raise InstallError(f"{asset} has no {name} inside")

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    with tempfile.NamedTemporaryFile(dir=dest_dir, delete=False) as f:
        f.write(binary)
    os.chmod(f.name, 0o755)
    os.replace(f.name, dest)
    return dest
