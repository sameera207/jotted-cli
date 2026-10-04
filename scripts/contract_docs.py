"""Regenerate the contract documents for downstream agents and apps from the CLI itself.

    uv run python scripts/contract_docs.py           # write them
    uv run python scripts/contract_docs.py --check   # exit 1 if they're out of date

Writes:
- docs/schema.json: `jotted --json schema`, the machine-readable contract.
- docs/cli-contract.md: the parts between the GENERATED markers (command reference, error
  codes, MCP tools). Everything outside the markers is written by hand and kept as is.
- tests/contract/schema-v<N>.json: what contract N promises (commands, arguments, data
  fields, operations, error codes). It only grows: additions are recorded, and a removal or
  rename is refused until the contract version goes up (contract.CONTRACT), which starts a
  new file.

Run by the pre-commit hook (.githooks/pre-commit) and checked by tests/test_contract_docs.py,
so the documents can't fall behind the code.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from jotted import cli, contract, mcp, schema  # noqa: E402

DOCS = ROOT / "docs"
SCHEMA_JSON = DOCS / "schema.json"
GUIDE = DOCS / "cli-contract.md"
BASELINE = ROOT / "tests" / "contract" / f"schema-v{contract.CONTRACT}.json"
BLOCK = re.compile(r"(<!-- BEGIN GENERATED: (?P<name>[\w-]+) -->\n).*?(<!-- END GENERATED: (?P=name) -->)", re.S)


def schema_data() -> dict:
    return schema.build(cli.build_parser())


# ---------------------------------------------------------------- JSON Schema -> one readable line


def shape(s: dict, depth: int = 0) -> str:
    if not s:
        return "any"
    if "oneOf" in s:
        return " | ".join(shape(o, depth) for o in s["oneOf"])
    if "enum" in s:
        return " | ".join(json.dumps(v) for v in s["enum"])
    t = s.get("type")
    if t == "array":
        return f"{shape(s.get('items', {}), depth)}[]"
    if t == "object" or "properties" in s:
        props = s.get("properties", {})
        if not props:
            return "object"
        if depth >= 1:
            return "{" + ", ".join(props) + "}"
        req = set(s.get("required", props))
        return "{" + ", ".join(f"{k}{'' if k in req else '?'}: {shape(v, depth + 1)}" for k, v in props.items()) + "}"
    if isinstance(t, list):
        return " | ".join(t)
    return t or "any"


def usage(c: dict) -> str:
    parts = ["jotted", c["command"]]
    for a in c["arguments"]:
        if a.get("positional"):
            name = ("|".join(a["enum"]) if "enum" in a else a["name"].upper()) + ("..." if a.get("many") else "")
            parts.append(name if a["required"] else f"[{name}]")
        else:
            flag = a["flags"][-1]
            value = "" if a.get("type") == "boolean" else " " + ("|".join(a["enum"]) if "enum" in a else a["name"].upper())
            parts.append(f"[{flag}{value}]")
    return " ".join(parts)


def _sentence(text: str) -> str:
    text = text[:1].upper() + text[1:]
    return text if not text or text.endswith(".") else text + "."


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def commands_block(d: dict) -> str:
    out = [f"Generated from `jotted schema` (release {d['version']}, contract {d['contract']}, "
           f"accepts {d['contract_min']}+). Exact argument types and data shapes: `docs/schema.json`.", ""]
    out += ["| Command | What it does |", "| --- | --- |"]
    for c in d["commands"]:
        if c.get("hidden"):
            continue
        out.append(f"| `{_cell(usage(c))}` | {_cell(_sentence(c['help']))} |")
    out.append("")
    for c in d["commands"]:
        title = f"### `jotted {c['command']}`"
        if c.get("hidden"):
            title += " (hidden" + (f", deprecated: {c['deprecated']}" if c.get("deprecated") else "") + ")"
        out += [title, "", _sentence(c["help"]), "",
                f"```text\n{usage(c)}\n```", ""]
        if c["arguments"]:
            out += ["| Argument | Type | Notes |", "| --- | --- | --- |"]
            for a in c["arguments"]:
                name = ", ".join(a.get("flags") or [a["name"].upper()])
                notes = [a.get("help", "")]
                if "default" in a:
                    notes.append(f"default `{a['default']}`")
                if a["required"]:
                    notes.append("required")
                if a.get("many"):
                    notes.append("one or more words")
                out.append(f"| `{name}` | `{_cell(shape(a))}` | {_cell('; '.join(n for n in notes if n))} |")
            out.append("")
        data = c["data"]
        out += [f"`data`: `{_cell(shape(data))}`" if data.get("type") or "oneOf" in data or "properties" in data
                else f"`data`: {data.get('description', 'any')}.", ""]
        if data.get("description") and (data.get("type") or "oneOf" in data):
            out += [_sentence(data["description"]), ""]
    return "\n".join(out).rstrip() + "\n"


def errors_block(d: dict) -> str:
    meaning = {
        "invalid": "Bad input: unknown item, bad setting value",
        "not_found": "No such item, document or page",
        "conflict": "Can't do that in this state",
        "internal": "A bug in Jotted; details on stderr",
        "usage": "Bad arguments, or a prompt that can't be shown",
        "config": "`config.toml` is invalid",
        "not_set_up": "A setup step is missing; `error.step` names it",
        "busy": "Another job holds the device; `retry: true`",
        "not_connected": "The device or its cloud can't be reached, or the connection was revoked",
        "model_error": "No key, a rejected key, or the model failed",
        "interrupted": "Ctrl+C",
    }
    rows = ["| `code` | Exit | Meaning |", "| --- | --- | --- |", "| (success) | 0 | Done |"]
    for code, e in sorted(d["errors"].items(), key=lambda kv: (kv[1]["exit"], kv[0])):
        rows.append(f"| `{code}` | {e['exit']} | {meaning.get(code, '')} |")
    return "\n".join(rows) + "\n"


def mcp_block(d: dict) -> str:
    rows = ["| Tool | Runs | What it does |", "| --- | --- | --- |"]
    for name, tool in mcp.TOOLS.items():
        runs = tool.runs or " ".join(["jotted", tool.command, *tool.fixed])
        notes = (" Only with `jotted mcp --admin`." if tool.admin else "") + \
            (" For the widget only (`visibility: [\"app\"]`)." if tool.app_only else "") + \
            (f" Opens the widget (`{mcp.mcp_ui.URI}`)." if tool.widget else "")
        rows.append(f"| `{name}` | `{runs}` | {_cell(tool.description)}{notes} |")
    rows += ["", "Never exposed (they take a secret, or rewire an app): "
             + ", ".join(f"`{c}`" for c in sorted(mcp.NEVER)) + "."]
    return "\n".join(rows) + "\n"


BLOCKS = {"commands": commands_block, "errors": errors_block, "mcp-tools": mcp_block}


# ---------------------------------------------------------------- the promise: only grows


def baseline(d: dict) -> dict:
    """What a wrapper may rely on, from a schema."""
    return {"contract": d["contract"], "errors": d["errors"],
            "commands": {c["command"]: {"arguments": sorted(a["name"] for a in c["arguments"]), "data": c["data"]}
                         for c in d["commands"]},
            "operations": {k: sorted(v["params"]) for k, v in d["operations"].items()}}


def _gone_fields(old, new, where: str) -> list[str]:
    if not isinstance(old, dict) or not isinstance(new, dict):
        return []
    gone = []
    for k, v in old.get("properties", {}).items():
        if k not in new.get("properties", {}):
            gone.append(f"{where}.{k}")
        else:
            gone += _gone_fields(v, new["properties"][k], f"{where}.{k}")
    if "items" in old:
        gone += _gone_fields(old["items"], new.get("items", {}), f"{where}[]")
    for i, option in enumerate(old.get("oneOf", [])):
        if i < len(new.get("oneOf", [])):
            gone += _gone_fields(option, new["oneOf"][i], f"{where}|{i}")
    return gone


def removed(old: dict, new: dict) -> list[str]:
    """What `old` promised that `new` no longer has (both from `baseline`). Additions are fine."""
    gone = [f"error {code}" for code, e in old["errors"].items() if new["errors"].get(code) != e]
    for name, c in old["commands"].items():
        if name not in new["commands"]:
            gone.append(f"command `{name}`")
            continue
        gone += [f"`{name}` argument {a}" for a in c["arguments"] if a not in new["commands"][name]["arguments"]]
        gone += [f"`{name}` data{p}" for p in _gone_fields(c["data"], new["commands"][name]["data"], "")]
    for op, params in old["operations"].items():
        if op not in new["operations"]:
            gone.append(f"operation {op}")
        else:
            gone += [f"{op}({p})" for p in params if p not in new["operations"][op]]
    return gone


def added(old: dict, new: dict) -> list[str]:
    """What `new` promises that `old` didn't (both from `baseline`): for release notes."""
    found = [f"error code `{code}`" for code in new["errors"] if code not in old["errors"]]
    for name, c in new["commands"].items():
        if name not in old["commands"]:
            found.append(f"command `jotted {name}`")
            continue
        was = old["commands"][name]
        found += [f"`jotted {name}` option or argument `{a}`" for a in c["arguments"] if a not in was["arguments"]]
        found += [f"`jotted {name}` data field `{p.lstrip('.')}`" for p in _gone_fields(c["data"], was["data"], "")]
    for op, params in new["operations"].items():
        if op not in old["operations"]:
            found.append(f"operation `{op}`")
        else:
            found += [f"operation `{op}` parameter `{p}`" for p in params if p not in old["operations"][op]]
    return found


def breaking() -> list[str]:
    """Removals since the recorded promise for this contract version."""
    if not BASELINE.is_file():
        return []
    return removed(json.loads(BASELINE.read_text()), baseline(schema_data()))


def render() -> dict[Path, str]:
    d = schema_data()
    guide = GUIDE.read_text()

    def fill(m: re.Match) -> str:
        name = m.group("name")
        if name not in BLOCKS:
            raise SystemExit(f"{GUIDE}: unknown generated block {name!r}")
        return m.group(1) + BLOCKS[name](d) + m.group(3)

    found = {m.group("name") for m in BLOCK.finditer(guide)}
    missing = set(BLOCKS) - found
    if missing:
        raise SystemExit(f"{GUIDE}: missing GENERATED markers for {sorted(missing)}")
    return {SCHEMA_JSON: json.dumps(d, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n",
            GUIDE: BLOCK.sub(fill, guide),
            BASELINE: json.dumps(baseline(d), indent=1, sort_keys=True, default=str) + "\n"}


def stale() -> list[Path]:
    return [p for p, text in render().items() if not p.is_file() or p.read_text() != text]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--check", action="store_true", help="don't write; exit 1 if anything is out of date")
    args = p.parse_args()
    gone = breaking()
    if gone:
        print(f"Breaking changes to contract {contract.CONTRACT}: " + "; ".join(gone), file=sys.stderr)
        print("Put them back (deprecate first: keep them working for a release), or raise contract.CONTRACT "
              "for a new contract version. Nothing was written.", file=sys.stderr)
        return 1
    if args.check:
        out = stale()
        for path in out:
            print(f"out of date: {path.relative_to(ROOT)}", file=sys.stderr)
        if out:
            print("run: uv run python scripts/contract_docs.py", file=sys.stderr)
        return 1 if out else 0
    for path, text in render().items():
        if not path.is_file() or path.read_text() != text:
            path.write_text(text)
            print(f"updated {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
