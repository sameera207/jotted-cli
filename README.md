# Jotted

Turns your handwritten notes into a to-do list. It works with reMarkable today. It reads the notebooks you choose through reMarkable Cloud, works out which lines are tasks and whose they are, and keeps one list in a local web app. Tick items there or on the tablet.

It runs on your computer. Nothing goes through a server of ours: images of new lines go to your language model (Claude, from Anthropic) to be read and judged, with your own API key. The optional Jev plugin, from TypeSafe, can do the judging instead.

## Quick start

You need a reMarkable with cloud sync (a Connect subscription), an [Anthropic API key](https://console.anthropic.com/settings/keys), and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
uv tool install git+https://github.com/sameera207/jotted-cli
jotted start
```

`jotted start` walks you through the rest, then runs Jotted in that window and says so:

1. It creates a folder for settings, data and keys (`~/Library/Application Support/jotted` on a Mac, `~/.local/share/jotted` on Linux).
2. It downloads [rmapi](https://github.com/ddvk/rmapi), which talks to reMarkable Cloud, and checks the download against its published checksum.
3. It connects your reMarkable with a one-time code from my.remarkable.com.
4. It asks for your API keys and checks them. They're saved in that folder, readable by your user only. Keys exported in your shell take precedence.
5. It starts Jotted and prints what it is doing. Choose the folders whose notes should feed your list in the Jotted app (or `jotted watch add PATH`; `jotted start --browser` opens the web app).

Run `jotted start` again whenever you want Jotted running; finished steps are skipped. `jotted setup` goes through the steps again, to reconnect the tablet or change a key. Leave the terminal window open while Jotted runs.

### Updates

`jotted start` checks GitHub first. When there is a newer release than the version you have, it reinstalls at that release (`uv tool install --force git+…@vX.Y.Z`) and starts again in it. If GitHub can't be reached, or the upgrade fails, it carries on with the version you have.

- An app that is already running keeps its old code: stop it with Ctrl+C, then `jotted start` again.
- `jotted update` updates without starting the app.
- `jotted start --no-update`, or `JOTTED_NO_UPDATE=1`, skips the check.

Only an install from GitHub updates itself, and only to releases. A checkout run with `uv run`, or an install pinned by hand to a commit or branch, never does.

So shipping a fix is: commit it to `main`, push, then cut a release (`/release` in Claude Code; see `AGENTS.md`). Each install picks it up the next time it starts, and the Jotted app downloads it.

## Development setup

To work on Jotted itself. A `config.toml` in the folder you run from takes precedence over the app folder.

### 1. Get the code and dependencies

```bash
git clone <your-repo-url> jotted
cd jotted
uv sync
```

### 2. Install rmapi

Download the binary for your OS from the [ddvk/rmapi releases](https://github.com/ddvk/rmapi/releases), then put it on your `PATH`:

```bash
chmod +x rmapi
mv rmapi /usr/local/bin/        # or ~/.local/bin
command -v rmapi                # should print its path
```

Or build it from source with Go:

```bash
git clone https://github.com/ddvk/rmapi && cd rmapi && go install
```

If you don't put it on your `PATH`, set `rmapi.binary` in the config to its absolute path.

### 3. Create your config

```bash
cp src/jotted/config.example.toml config.toml
```

Edit `config.toml` if you need to (the defaults work for a first run), then check it:

```bash
uv run jotted config check
```

Every setting lives in this one file, including rmapi's token location. You never set rmapi environment variables yourself. To keep the config somewhere else, point `JOTTED_CONFIG` at it.

### 4. Connect to reMarkable Cloud (once)

```bash
uv run jotted connect
```

1. The command asks for a one-time code (or pass it on standard input with `--stdin`).
2. Sign in at my.remarkable.com and open the page for connecting a desktop app to get the code.
3. Paste the code. The token is saved to `.secrets/rmapi.conf`.

That token grants full access to your library. Never commit it. If it leaks, revoke the device on my.remarkable.com and run `jotted connect --replace`. (`jotted auth`, its old name, still works for one release.)

### 5. Run it

```bash
uv run jotted serve
```

Then open http://127.0.0.1:8765, tick a folder in Settings, and write a line like "book the retro room" in a document in it. `uv run jotted collect --dry-run` shows what changed without reading anything, and `uv run jotted collect` reads it.

Each page's new lines are sent to the LLM as images in one request, and their transcripts to the judge (the LLM, or Jev when it's on) in one request. Both results are cached under `cache/ai/`, keyed by the line's stroke IDs, so reading a page again makes no API calls. Diagrams are never actions. A line that wraps onto a second line is merged into one item: close spacing proposes the merge, and the judge can veto it.

Run the tests with `uv run pytest`. They use synthetic pages, plus a fake `rmapi` for the cloud wrapper.

## The common to-do list

`jotted serve` runs a local web app at http://127.0.0.1:8765. It collects action items from the folders you choose and keeps them in one list. It also keeps that list on the tablet as a To-do document you can tick with the pen.

1. Open **Settings**, load your folders, tick the ones to watch (for example `/Meeting Notes`), and save.
2. In the background, the app checks the tablet every minute. Only documents whose cloud copy changed are downloaded; only pages whose content changed are parsed; only lines with new strokes are read. The judge decides whether each line is an action and who owns it.
3. **To-do** shows everything: collected actions, with an image of the handwritten line and a link to its page, and items you type into the empty line at the bottom. Filter by open/done, mine/others and source. Mark "×" on a line that isn't an action, or to delete an item you typed.
4. Turn on **To-do document on the tablet** in Settings. Each item gets a fixed slot with a printed checkbox. Tick a box with the pen and the item is marked done on the next check. Write in an empty row and it becomes a new item. The document has two pages. When its rows run out, Jotted deletes it and prints a fresh one with only the open items.

There used to be a separate Tasks notebook as well. It's gone: write tasks in any watched notebook, in an empty row of the To-do document, or in the web app. The first time a new version starts, the old notebook's tasks become items on the list and keep their rows on the To-do document. To keep using that notebook, watch it like any other; its lines match the items they already became.

### The language model and plugins

Settings shows the **language model**: its adapter, model and key. It reads handwriting, and judges which lines are actions unless a plugin does. You can replace its key there. `config.toml` sets the default adapter and model (`[llm] provider` and `model`); `jotted ai provider NAME` and `jotted ai model NAME` choose others, saved with the settings.

**Jev**, from TypeSafe, is an optional plugin. Add its key in Settings (or during `jotted setup`) and Jev judges actions and owners instead, with its own threshold slider. Remove the key and the LLM judges again. Keys typed in Settings are checked with the provider first and saved like the ones from setup; a key exported in your shell wins, and can only be removed there.

## The command line

The CLI is the product's one public interface: everything the web app does is a command, and every other front end (the desktop app, `jotted mcp`, your scripts) only runs `jotted --json …`. Building on it? Read [`docs/cli-contract.md`](docs/cli-contract.md); [`docs/schema.json`](docs/schema.json) has every command's arguments and data. Both are regenerated from the code (`scripts/contract_docs.py`).

| Command | What it does |
| --- | --- |
| `jotted setup status` | Every setup step, done or not, and the command that does it |
| `jotted setup prepare` | The steps that need no answer: the app folder, downloading rmapi |
| `jotted connect [--stdin] [--replace]` | Connect your reMarkable with a one-time code (from the reMarkable plugin) |
| `jotted ai` / `ai key llm\|jev [--stdin]` / `ai remove jev` | The language model and the Jev plugin |
| `jotted ai provider NAME` / `ai model NAME` | Choose the LLM adapter and model |
| `jotted plugins` | Installed source plugins, and which one is chosen |
| `jotted items [--status open\|done\|all] [--owner mine\|others] [--folder F]` | List the to-do list |
| `jotted items add TEXT` / `edit ID TEXT` / `done ID` / `reopen ID` / `dismiss ID` | Change it |
| `jotted watch add\|remove\|from-now\|read-all PATH` | Choose what is read |
| `jotted settings` / `jotted settings set KEY VALUE` | Show or change settings (`todo_enabled true`, `action_threshold 0.8`) |
| `jotted library` | Your device's folders, and which are watched |
| `jotted collect [--dry-run]` | Read what changed now (`--dry-run`: only show what would be read) |
| `jotted todo [--force]` | Read ticks from the To-do document and republish it |
| `jotted check` | Both of the above |
| `jotted status` | What is read, judged and published, and when |
| `jotted image page DOC PAGE` / `image line DOC ANCHOR` | Where an item came from, as SVG |
| `jotted events [--since CURSOR] [--follow]` | What changed; `--follow` prints changes as they happen |
| `jotted serve [--browser] [--port 0]` | The web app, background checking and the CLI's fast path |
| `jotted version` / `jotted schema` | Release and contract versions; every command and the shape of its data |
| `jotted mcp` | Jotted's operations as MCP tools, for agents |
| `jotted claude connect` | Add Jotted to Claude Desktop (`claude status`, `claude disconnect`) |

### For scripts and apps

With `--json` (before or after the command), stdout carries exactly one JSON envelope:

```json
{"v": 1, "ok": true, "data": {}}
{"v": 1, "ok": false, "error": {"code": "busy", "message": "…", "retry": true}}
```

Branch on `code`, never on `message`. Each code has its own exit status: `invalid`, `not_found`, `conflict` 1; `usage`, `config` 2; `not_set_up` 3 (`error.step` names the step); `busy` 4; `not_connected` 5; `model_error` 6; `interrupted` 130. Logs and progress go to stderr. Nothing prompts unless stdin is a terminal and `--json` is off; keys and codes can always come on stdin with `--stdin`.

`jotted --json events --follow` prints one JSON line per change (items added, changed or removed, checks, the To-do document published, settings), from any process. Keep the last `cursor` and pass it as `--since` after a restart to miss nothing.

While `jotted serve` runs, CLI commands are handed to it instead of starting the work themselves (it writes `serve.json` next to the database); the output is the same. `--local` runs a command in its own process anyway.

For agents: `jotted mcp` serves the to-do list, status and setup status as MCP tools on stdio; `jotted claude connect` adds it to Claude Desktop. What an agent finds in other documents (meeting docs, mail, tickets) it proposes, and proposals wait for you to accept them before they reach the list or the tablet. The library, watching, settings and checks are offered only with `jotted mcp --admin`. Keys never go through it. An agent with a shell can also run `jotted --json …` directly; `jotted schema` describes every command.

`jotted serve` (what `jotted start` runs) does the same work in the background and serves the web app. A CLI command that needs your device while the server is checking it waits for it: one job at a time, across processes.

## How it fits together

```text
jotted/core/           model, ports and services; imports no plugin, adapter, Flask or AI SDK
jotted/api.py          every operation, once: the CLI, the web server and any other app call it
jotted/cli.py          the command line over api.py: the public interface
jotted/contract.py     what `--json` prints: the envelope, error codes and exit statuses
jotted/steps.py        setup as steps a wrapper can check and run one at a time
jotted/fastpath.py     handing CLI commands to a running `jotted serve`
jotted/schema.py       `jotted schema`; mcp.py: `jotted mcp`
jotted/server.py       the web app and its JSON API over api.py (internal)
jotted/plugins/        source plugins: where notes come from (SDK and registry)
jotted/plugins/remarkable/   the reMarkable cloud: rmapi, .rm pages, the To-do PDF, setup, `connect`
jotted/ink/            handwriting for every plugin: strokes, line clustering, reading with the LLM
jotted/llm.py          the LLM port (Claude in adapters/anthropic_llm.py); the Jev plugin judges instead when on
jotted/adapters/       SQLite, and the AI providers
```

- **Front ends hold no logic.** `cli.py` and `server.py` parse input, call one `api.Jotted` method and show its result. Tests check that every operation is a CLI command and every web route calls an operation.
- **Sources are plugins.** Nothing outside `jotted/plugins/remarkable/` knows about reMarkable, and a test checks it. A plugin returns the core's ports, owns its config sections, setup steps and CLI commands, and hands handwriting to `jotted.ink` as strokes. Another package can add one through the `jotted.sources` entry point group; choose it with `[plugins] source` in `config.toml`. `jotted/plugins/__init__.py` describes the interface.
- **The web server's API is internal.** Other apps use the `jotted` command, not this API. Read requests are only answered on this computer's own address, so a web page can't reach it through DNS rebinding. Requests that change something need the install's token in `X-Jotted-Token`; it is in `<secrets_dir>/server-token`, readable by your user only.

See `specs/Common-todo-spec.md` for the to-do list's design.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `connect` or `collect` fails with an auth error | Run `jotted connect --replace` |
| "busy with another job" | `jotted serve` is checking your device; try again in a moment |
| Need to see what rmapi is doing | Set `rmapi.trace = true` in `config.toml` |
| Warnings about unreadable blocks | Newer firmware than rmscene knows: `uv lock --upgrade-package rmscene && uv sync` |
| A page's latest writing is missing | The tablet hadn't finished syncing; wait for the next check, or click **Check now** |

## Layout

```text
src/jotted/config.example.toml   every setting with its default; `jotted start` copies it
config.toml           your settings (gitignored)
src/jotted/           see "How it fits together"
.secrets/             API keys, the rmapi token and the server token (gitignored)
cache/                downloaded documents and cached AI answers (gitignored)
```
