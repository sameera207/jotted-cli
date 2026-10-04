"""Releases (.claude/commands/release.md, release.yml): the manifest apps trust, the version a
release suggests, and the notes it writes."""

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import contract_docs  # noqa: E402
import release  # noqa: E402
import release_manifest  # noqa: E402


@pytest.fixture
def dist(tmp_path):
    """What the publish job holds: both builds with their checksums, and the contract."""
    for target in release_manifest.TARGETS:
        archive = tmp_path / f"jotted-0.2.0-{target}.tar.gz"
        archive.write_bytes(f"build for {target}".encode())
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (tmp_path / f"{archive.name}.sha256").write_text(f"{digest}  {archive.name}\n")
    (tmp_path / "schema.json").write_text(json.dumps({"version": "0.2.0", "contract": 1, "contract_min": 1}))
    (tmp_path / "cli-contract.md").write_text("# contract\n")
    return tmp_path


def test_the_manifest_lists_every_build_with_its_checksum(dist):
    m = release_manifest.manifest(dist, "v0.2.0")
    assert m["version"] == "0.2.0" and m["contract"] == 1 and m["contract_min"] == 1
    assert set(m["builds"]) == {"macos-arm64", "macos-x64"}
    arm = m["builds"]["macos-arm64"]
    assert arm["file"] == "jotted-0.2.0-macos-arm64.tar.gz" and arm["size"] == len("build for macos-arm64")
    assert arm["sha256"] == hashlib.sha256(b"build for macos-arm64").hexdigest()
    assert set(m["files"]) == {"schema.json", "cli-contract.md"}


def test_the_manifest_refuses_a_release_that_doesnt_add_up(dist):
    with pytest.raises(ValueError, match="schema.json is for 0.2.0"):
        release_manifest.manifest(dist, "v0.3.0")
    (dist / "jotted-0.2.0-macos-x64.tar.gz").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="sha256"):
        release_manifest.manifest(dist, "v0.2.0")
    for f in dist.glob("jotted-0.2.0-macos-x64*"):
        f.unlink()
    with pytest.raises(ValueError, match="no build for macos-x64"):
        release_manifest.manifest(dist, "v0.2.0")


def test_the_suggested_version_follows_the_contract():
    nothing = {"contract": (1, 1), "commands": [], "by_command": {}, "errors": []}
    assert release.suggest(None, nothing, ["x"], "0.1.0") == ("0.1.0", "the first release")
    assert release.suggest("v0.1.0", nothing, [], "0.1.0")[0] is None
    assert release.suggest("v0.1.0", nothing, ["fix"], "0.1.0")[0] == "0.1.1"
    assert release.suggest("v0.1.3", {**nothing, "commands": ["items get"]}, ["x"], "0.1.3")[0] == "0.2.0"
    breaking = {**nothing, "contract": (1, 2)}
    version, why = release.suggest("v0.4.2", breaking, ["x"], "0.4.2")
    assert version == "0.5.0" and "BREAKING" in why
    assert release.suggest("v1.4.2", breaking, ["x"], "1.4.2")[0] == "2.0.0"


def test_notes_group_the_contract_additions_by_command():
    old = contract_docs.baseline(contract_docs.schema_data())
    new = json.loads(json.dumps(old))
    new["commands"]["items get"]["arguments"].append("full")
    new["commands"]["claude open"] = {"arguments": [], "data": {}}
    new["errors"]["paused"] = {"exit": 7}
    changes = release.contract_changes(old, new)
    assert changes["commands"] == ["claude open"] and changes["errors"] == ["paused"]
    assert changes["by_command"] == {"items get": ["option `full`"]}
    text = release.notes("0.2.0", "v0.1.0", changes, ["Add items get --full (abc1234)"])
    assert "Contract 1, unchanged: additions only." in text and "- `jotted claude open`" in text
    assert "- `jotted items get`: option `full`" in text and "New error codes: `paused`" in text
    assert text.rstrip().endswith("- Add items get --full (abc1234)")


def test_contract_docs_lists_additions():
    old = contract_docs.baseline(contract_docs.schema_data())
    new = json.loads(json.dumps(old))
    new["commands"]["status"]["data"]["properties"]["paused"] = {"type": "boolean"}
    assert contract_docs.added(old, new) == ["`jotted status` data field `paused`"]
    assert contract_docs.added(old, old) == []


def test_the_version_is_set_in_project_only():
    text = '[project]\nname = "jotted"\nversion = "0.1.0"\n\n[tool.x]\nversion = "9"\n'
    bumped = release.with_version(text, "0.2.0")
    assert release.project_version(bumped) == "0.2.0" and 'version = "9"' in bumped
