# Jotted: notes for agents

Jotted turns handwritten notes (reMarkable today) into one to-do list. Python 3.11+, run with uv.

```bash
uv run pytest -q                               # all tests (synthetic pages, a fake rmapi; no tablet or key needed)
uv run jotted --json <command>                 # the CLI
uv run python scripts/contract_docs.py         # regenerate the contract documents (see below)
uv run --with pyinstaller python scripts/build_standalone.py   # the standalone build apps bundle (build/release/)
```

A release is a `vX.Y.Z` tag matching `version` in `pyproject.toml`. CI then attaches the contract documents
(`contract.yml`) and a standalone build per macOS architecture with its checksum (`standalone.yml`).

## Building something on Jotted (an app, an MCP connector, a script)?

Read [`docs/cli-contract.md`](docs/cli-contract.md) and use only the `jotted` command with `--json`. Exact arguments and data shapes: [`docs/schema.json`](docs/schema.json). Don't import Jotted's Python, read its database or call its local HTTP API.

## Working on Jotted itself

The architecture, and the tests that keep it:

- **The CLI is the product's one public interface.** Everything else (the desktop app in `jotted-desktop`, `jotted mcp`, scripts) wraps `jotted --json …`. `specs/CLI-contract-spec.md` explains why.
- **Every operation lives once, in `src/jotted/api.py`** (`@operation`), and is a CLI command (`@uses` in `cli.py`). `cli.py` and `server.py` parse input, call one operation and show its result; no product logic.
- **Sources are plugins** (`src/jotted/plugins/`). Nothing outside `plugins/remarkable/` may know about reMarkable. `src/jotted/core/` imports no plugin, adapter, Flask or AI SDK.
- **Errors carry a contract code** (`code` on `ApiError`, `SetupError`, `contract.Error`): see `src/jotted/contract.py`. Pick an existing code; a new one needs an exit status in `contract.EXIT`.
- **No prompts a wrapper could hang on.** Read secrets with `ui.read_secret(args, …)`, which takes `--stdin`; only prompt when `ui.can_prompt(args)`.
- **Every change to the repository writes an event** in the same transaction (`sqlite_repo._tracked`, `_event`), so `jotted events` sees it.

### Changing a command, option or output

1. Change the code. A new command returns a `Call` (one operation) or `Done` (data) from its `cmd_` function.
2. Declare or update its `data` shape in `DATA` in `src/jotted/schema.py`. A test fails for a command without one, and checks real outputs against the shapes.
3. Regenerate the documents: `uv run python scripts/contract_docs.py`. It rewrites `docs/schema.json`, the GENERATED parts of `docs/cli-contract.md`, and `tests/contract/schema-v<N>.json`. The pre-commit hook runs it for you once enabled (`git config core.hooksPath .githooks`); CI and `tests/test_contract_docs.py` fail if they are stale.
4. If the change affects how a wrapper should use the CLI (a new flow, a new event type, a new environment variable), update the hand-written parts of `docs/cli-contract.md` too.

### The contract only grows

Within a contract version (`contract.CONTRACT`, the `"v"` in every output), commands, options, fields, operations and error codes can be added, never removed, renamed or given a new meaning. `tests/contract/schema-v<N>.json` records what is promised; the script refuses to write, and tests fail, if something in it disappears.

To remove or rename something: keep the old name working for a release with a warning on stderr (like `auth` → `connect`, via `set_defaults(deprecated=...)`), then raise `contract.CONTRACT` (and keep `CONTRACT_MIN` at the old one for a release). Never edit the baseline file by hand to make a removal pass.

### Style

Match the surrounding code: short docstrings that say why, plain English in messages (they are shown to people as they are), comments only where the code can't say it.
