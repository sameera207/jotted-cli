# Standalone builds: HTTPS certificates

Status: milestones 1–3 done, release pending · Oct 6, 2026 · @Sam
Target repo: `jotted-cli`
Related: `scripts/build_standalone.py`, `src/jotted/plugins/remarkable/rmapi_install.py`,
`src/jotted/selfupdate.py`, jotted-app `docs/bundling.md`

## 1. The problem

The standalone build in release v0.1.0 (`jotted-0.1.0-macos-*.tar.gz`, PyInstaller `--onedir`)
can't verify HTTPS certificates when it uses Python's own `urllib`. On a fresh Mac, the first
setup step fails:

```text
$ jotted --json setup prepare        # fresh JOTTED_HOME, PATH=/usr/bin:/bin:/usr/sbin:/sbin
{"ok": false, "error": {"code": "not_connected", "step": "remarkable.rmapi",
 "message": "could not download rmapi: <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED]
             certificate verify failed: unable to get local issuer certificate (_ssl.c:1006)>"}}
```

rmapi is never installed, so `jotted connect` can't run, and nobody can set up Jotted from the
desktop app (which bundles this build). Found installing the app on a second Mac; a `uv run` or
`uv tool install` copy doesn't show it, because uv's Python finds certificates.

Why: the frozen Python's OpenSSL looks for CA certificates at the path it was built with, which
doesn't exist on the user's Mac, and the build doesn't ship any. `certifi` isn't in the bundle
(`_internal/` has no `cacert.pem`).

What already works in the same build: the model and plugin calls. `ai key llm --stdin` and
`ai key jev --stdin` with a made-up key reach Anthropic and TypeSafe and answer "rejected this
key", so their HTTP clients bring their own trust. **Verify** how (the build has
`httpx2-2.13.1.dist-info`), so the fix below doesn't change them by accident.

| HTTPS call | File | Affected in a standalone build |
| --- | --- | --- |
| rmapi download (`setup prepare`) | `plugins/remarkable/rmapi_install.py` `_download` | **Yes**: setup is blocked |
| Self-update check (GitHub API) | `selfupdate.py` | No: never runs in a bundled copy (`JOTTED_BUNDLED`) or a standalone one |
| Local server / fast path | `cli.py`, `fastpath.py` | No: plain HTTP on this machine |
| Anthropic, TypeSafe | the SDKs | No (see above) |
| rmapi itself (Go) | — | No: Go uses the system trust store |

The desktop app works around it today by setting `SSL_CERT_FILE=/etc/ssl/cert.pem` for every
`jotted` it starts (jotted-app `src-tauri/src/bin.rs`). That keeps the app working but doesn't
help anyone else who runs the standalone build, and it misses certificates that only live in the
macOS keychain.

## 2. Decisions

| Question | Decision |
| --- | --- |
| Which certificates | The operating system's trust store, through [`truststore`](https://pypi.org/project/truststore/) (the macOS keychain via Security.framework; Windows' store later). It includes certificates an employer installs for a TLS-inspecting proxy, which a bundled `cacert.pem` would not: the app is going onto work laptops |
| Fallback | `certifi`'s bundle when `truststore` can't be used (no Security.framework: Linux builds, an import error) |
| `SSL_CERT_FILE` / `SSL_CERT_DIR` | When either is set, they win: `ssl.create_default_context()` reads them. Someone who sets them means it |
| Scope | One helper for every `urllib` HTTPS call Jotted makes. No global `truststore.inject_into_ssl()`: it would also change the SDKs' TLS, which work and aren't Jotted's to change |
| Contract | Unchanged. No new command, field or error code |

## 3. The change

### A helper: `src/jotted/net.py`

```python
def ssl_context() -> ssl.SSLContext:
    """The TLS context for Jotted's own HTTPS downloads.

    SSL_CERT_FILE / SSL_CERT_DIR when set; else the OS trust store (truststore); else certifi."""

def urlopen(url_or_request, *, timeout: float):
    """urllib.request.urlopen with ssl_context(); https only for anything not on this machine."""
```

- `rmapi_install._download` and `selfupdate` call `net.urlopen`. The local calls in `cli.py` and
  `fastpath.py` stay as they are (plain HTTP to `127.0.0.1`).
- A test in the suite fails if `urllib.request.urlopen` is called with an `https://` URL anywhere
  else in `src/` (a grep, like the other architecture tests in `AGENTS.md`).

### Dependencies

- Add `truststore` and `certifi` to `pyproject.toml` `dependencies` (both pure Python, no
  network at import).
- `scripts/build_standalone.py`: `--collect-data certifi` (its `cacert.pem`) and
  `--hidden-import truststore`, so PyInstaller keeps both even though they're only imported
  inside a function.

### The error, when it still fails

`could not download rmapi: <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] …>` gives a person
nothing to do. For a certificate failure, `rmapi_install` says:

> Couldn't download rmapi: this Mac didn't trust github.com's certificate. If you're on a work
> network, it may inspect HTTPS; ask IT, or install rmapi yourself and set `rmapi.binary`.

Still `not_connected` with `step: remarkable.rmapi`, so apps keep handling it as today.

## 4. Testing

- **Unit:** `ssl_context()` uses `SSL_CERT_FILE` when set, `truststore` otherwise, and `certifi`
  when `truststore` can't be imported (monkeypatched).
- **Unit:** the "only `net.urlopen` for https" grep test.
- **Standalone smoke test** (`build_standalone.py smoke_test`): it never touches the network
  today, which is why this shipped. Add one HTTPS request made the way the CLI makes it: the
  frozen build runs `jotted --json setup prepare` in its scratch home, with `SSL_CERT_FILE`,
  `SSL_CERT_DIR` and `REQUESTS_CA_BUNDLE` unset and the minimal `PATH`, and the
  `remarkable.rmapi` step must be done afterwards. CI's macOS runners have network. Keep
  `--skip-tests` for building offline.
- **Manual, per release:** on a Mac that has never had Python or Jotted, open the Jotted app
  (or run the standalone `jotted --json setup prepare`) and check rmapi downloads.

## 5. Related, found at the same time

These came up while tracing the same report. They're small and could ship in the same release.

1. **"no rmapi token at …; run `jotted connect` first"** (`plugins/remarkable/cloud.py`,
   `library()` and `put()`). When the token is missing this is a `CloudError`, so a
   `SourceError`, so `not_connected`. It means setup isn't done, so it should be `not_set_up`
   with `step: "remarkable.connect"`, as `library` already answers before setup. Apps then send
   the person to Connect instead of showing a CLI instruction in a window.
2. **`check` before setup is complete answers `{"ok": true, "data": {}}`.** It should fail with
   `not_set_up` and the first missing step, like the other commands that need setup.

## 6. Milestones

1. `net.py`, the two call sites, the dependencies and the build flags; unit tests.
2. The smoke test's HTTPS step; confirm it fails on v0.1.0's build and passes on the new one.
3. The friendlier certificate error, and the two related fixes (§5).
4. A release. The desktop app moves its pin and drops its `SSL_CERT_FILE` workaround in the same
   change: since `SSL_CERT_FILE` wins (§2), keeping it would replace the keychain with
   `/etc/ssl/cert.pem` and lose a work proxy's certificates.
