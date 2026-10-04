"""release.json: what a release holds, for apps that update jotted on their own (the Jotted app).

    python scripts/release_manifest.py DIR --tag v0.2.0

DIR holds the standalone builds and their .sha256 files (scripts/build_standalone.py), and
schema.json and cli-contract.md. Writes DIR/release.json:

    {"name": "jotted", "tag": "v0.2.0", "version": "0.2.0", "contract": 1, "contract_min": 1,
     "builds": {"macos-arm64": {"file": "jotted-0.2.0-macos-arm64.tar.gz", "sha256": "…", "size": 29…}, …},
     "files": {"schema.json": {"sha256": "…"}, "cli-contract.md": {"sha256": "…"}}}

CI signs it with minisign (release.json.minisig); an app checks that signature with the public
key in docs/release-key.pub before it trusts anything else in the release. It refuses to write a
manifest for a release that is missing a build, or whose checksums or version don't agree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

TARGETS = ("macos-arm64", "macos-x64")  # every release has a build for each
DOCS = ("schema.json", "cli-contract.md")
BUILD = re.compile(r"jotted-(?P<version>\d+\.\d+\.\d+)-(?P<target>[a-z0-9]+-[a-z0-9]+)\.tar\.gz")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest(folder: Path, tag: str) -> dict:
    version = tag.removeprefix("v")
    schema = json.loads((folder / "schema.json").read_text())
    if schema.get("version") != version:
        raise ValueError(f"schema.json is for {schema.get('version')}, the tag for {version}")
    builds = {}
    for archive in sorted(folder.glob("*.tar.gz")):
        m = BUILD.fullmatch(archive.name)
        if not m:
            raise ValueError(f"{archive.name} isn't named like a build")
        if m["version"] != version:
            raise ValueError(f"{archive.name} is a build of {m['version']}, not {version}")
        digest = sha256(archive)
        recorded = (folder / f"{archive.name}.sha256").read_text().split()[0]
        if recorded != digest:
            raise ValueError(f"{archive.name}: its .sha256 says {recorded}, the file is {digest}")
        builds[m["target"]] = {"file": archive.name, "sha256": digest, "size": archive.stat().st_size}
    missing = set(TARGETS) - set(builds)
    if missing:
        raise ValueError(f"no build for {', '.join(sorted(missing))}")
    return {"name": "jotted", "tag": tag, "version": version, "contract": schema["contract"],
            "contract_min": schema["contract_min"], "builds": builds,
            "files": {name: {"sha256": sha256(folder / name)} for name in DOCS}}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("folder", type=Path)
    p.add_argument("--tag", required=True)
    args = p.parse_args()
    try:
        data = manifest(args.folder, args.tag)
    except (ValueError, OSError, KeyError) as e:
        print(f"release.json not written: {e}", file=sys.stderr)
        return 1
    (args.folder / "release.json").write_text(json.dumps(data, indent=2) + "\n")
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
