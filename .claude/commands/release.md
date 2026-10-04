---
description: Cut a jotted-cli release (version, notes, tag); CI builds and publishes it
---

Cut a release of jotted-cli. A release reaches every `uv` install (they update to the latest
release) and every Jotted app (it downloads new releases), so go step by step and stop at
anything unexpected. $ARGUMENTS may name the version; otherwise suggest one.

1. **Plan.** Run `uv run python scripts/release.py plan`. It checks that you are on `main`, that
   nothing is uncommitted, that `main` is pushed and that CI passed on it, then shows what changed
   since the last release and suggests a version with draft notes.
   - If any check failed (✗), stop and tell the person what to fix. Never release over a red check.
   - If it says the contract version changed, say plainly that this breaks apps built for the old
     contract, and ask whether that is intended.
2. **Agree the version.** Show the suggested version, why, and the draft notes. Ask the person to
   confirm it or give another. Don't go on without a clear yes.
3. **Prepare.** Run `uv run python scripts/release.py prepare X.Y.Z`. It sets the version in
   `pyproject.toml` and `uv.lock`, regenerates the contract documents and writes
   `build/release-notes.md`. Read the notes: tighten the "Changes" list into a few lines a person
   would want to read (keep the contract sections as they are), and save it. Run `uv run pytest -q`.
   Show `git diff --stat` and the notes.
4. **Commit, tag, push**, once the person agrees:
   ```
   git commit -am "Release vX.Y.Z"
   git tag -a vX.Y.Z -F build/release-notes.md
   git push --atomic origin main vX.Y.Z
   ```
5. **Watch CI.** The tag starts `release.yml`: tests, a build per architecture, then one job that
   creates the release as a draft with every file, checks them, and publishes it. Find the run with
   `gh run list --workflow release.yml --limit 1` and follow it with `gh run watch <id> --exit-status`.
   - On success, check the release holds eight files (`gh release view vX.Y.Z --json assets`):
     two `.tar.gz` builds, their `.sha256`, `schema.json`, `cli-contract.md`, `release.json` and
     `release.json.minisig`. Give the person its link.
   - On failure, show the failing step's log (`gh run view <id> --log-failed`). The tag is pushed
     but nothing was published (at most a draft). Then:
     - a passing glitch (a runner, the network): re-run the failed jobs, `gh run rerun <id> --failed`;
     - a missing secret or key: tell the person (AGENTS.md, Releases), then re-run;
     - a real fix: commit and push it to `main` first. Then delete the draft, if one was made
       (`gh release delete vX.Y.Z --yes`), and the tag (`git push --delete origin vX.Y.Z && git tag -d vX.Y.Z`),
       and tag the new head with the same notes (`git tag -a vX.Y.Z -F build/release-notes.md && git push origin vX.Y.Z`).
     Never move or delete the tag of a release that was published.
