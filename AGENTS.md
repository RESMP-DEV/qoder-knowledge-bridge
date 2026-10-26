# qoder-knowledge-bridge agent instructions

`CLAUDE.md` is a relative symlink to this file.

## Scope

This repository owns the Qoder Knowledge Bridge plugin: the `knowledge-scout` and `codex-bridge` subagents, the provenance and delegation rule documents, three lifecycle hooks, and the read-only `qkb` connector. It does not own Qoder's knowledge generation, the Qoder application or MCP runtime, Codex configuration, credentials, or any target repository's source code.

The plugin intentionally ships no skills. Do not add a `skills/` directory or a `skills` manifest field. The capability is delivered through `agents/`, `rules/`, `hooks/`, and `qkb`.

The canonical repository is `RESMP-DEV/qoder-knowledge-bridge` and is public under the MIT license. Keep it free of secrets, credentials, personal data, knowledge data, and runtime state. Visibility changes require explicit user approval.

Global machine instructions in `~/AGENTS.md` remain authoritative. This file adds repository-specific scope, contracts, and verification. Read `README.md` first for usage and `CONNECTORS.md` for setup and troubleshooting.

## Layout and contracts

`.qoder-plugin/plugin.json` is the Qoder manifest. Component paths must exist and stay inside the repository. `agents/` is discovered by convention and `hooks/hooks.json` is declared explicitly. The current Qoder CLI ignores the `rules` manifest field, so the same guardrails are injected by the SessionStart hook.

`bin/qkb` is a thin wrapper. `scripts/qkb_core.py` owns knowledge access, formatting, and the hook subcommands. Keep it Python 3.9-compatible and standard-library only.

Every SQLite connection uses a `mode=ro` URI and `PRAGMA query_only=ON`. `qkb` must never create, modify, or delete anything under `~/.qoder/qoder-knowledge`. No feature may weaken this contract.

Knowledge databases, `MEMORY.md`, provenance logs, Qoder settings, and plugin registries are runtime data. They stay outside this repository.

Hooks must never block a session. Catch errors, print a short diagnostic to stderr, and exit 0. Provenance records metadata only, never tool responses or file contents.

`codex-bridge` delegates to `codex exec`. The default sandbox is `workspace-write` through `QKB_CODEX_FLAGS`. Never add `danger-full-access` or approval bypass unless the user explicitly asks. Never send secrets, commit, push, deploy, or publish from a delegated run.

QMind is accessed through Qoder MCP in Qoder agents. `qkb` is offline and does not bridge QMind. Do not add credentials or QMind endpoints to this repository.

No personal paths, usernames, tokens, or machine-specific endpoints belong in plugin content. Use `$HOME`, `QODER_HOME`, or documented environment variables.

README is the human entry point, CONNECTORS is the specialized setup document, and this file carries agent contracts. Do not add parallel roadmap, status, or component-summary markdown.

## Verification

Run these from the repository root before claiming a change complete:

- Syntax: `python3 -c 'import ast, pathlib; [ast.parse(p.read_text(), filename=str(p)) for p in pathlib.Path(".").rglob("*.py")]'`
- JSON: `python3 -m json.tool .qoder-plugin/plugin.json >/dev/null` and `python3 -m json.tool hooks/hooks.json >/dev/null`
- Manifest and components: `qodercli plugin validate .` is valid with the known `rules` warning; when the bundled validator is available, its only warning should be the intentional absence of skills.
- CLI smoke: `qkb --version`, `qkb projects`, and either `qkb context --task "..." --project <repo>` on a repo with a generated knowledge base or the documented no-KB path. Quote exact output in the change record.
- Hook smoke: pipe representative SessionStart, PostToolUse, and Stop JSON into `scripts/session_index.py`, `scripts/provenance_hook.py`, and `scripts/stop_summary.py` with `QODER_PLUGIN_DATA` set to a temporary directory. Confirm valid JSON, a provenance entry, a Stop summary, and no output when `stop_hook_active` is true.
- Read-only proof: attempt `CREATE TABLE` through `qkb_core.connect_ro` on a disposable copy and confirm the database rejects the write.
- Hygiene: no `__pycache__`, `.pyc`, or `.DS_Store` files; no absolute home-directory paths in plugin content; `git diff --check` is clean.

Do not add GitHub Actions. Run checks locally and record commands and results in the pull request or a durable receipt.

A change is not complete because it was written. State implemented, exercised, measured, or proposed separately. Live path probes outrank documentation and self-report.

## Review and output

`REVIEW.md` is read by Kilo from the base branch. Keep it current when invariants, severity guidance, or verification expectations change. Address every actionable review thread with evidence and do not silently ignore findings.

Keep interactive output sparse during PR and review work. Do not paste full reviewer reports, raw PR JSON, or bot payloads into the terminal. Put complete evidence in the pull request or a durable receipt; report the pull request URL, exact head, reviewer status, unresolved findings, required gates, and next action.

## Git and repository hygiene

Use conventional commits and focused branches. Do not push feature work directly to a default branch; use a branch and a pull request. Preserve unrelated dirty work. Default new repositories to private under RESMP-DEV; public requires explicit approval and a license.

Keep `.gitignore` covering `.sindexer/`, `.venv/`, Python caches, `.env`, `.worktrees/`, `build/`, `runtime/`, and `.DS_Store`. No runtime state, knowledge data, provenance logs, generated bytecode, or Qoder configuration belongs in the repository.

## Known gaps and non-claims

Automated tests and a receipt-producing verification dispatcher are not yet in this repository. Verification is command-based as listed above. Do not claim a test suite, CI, or a live Qoder Desktop exercise exists until it does. The current workstation installs the plugin in place at `~/.qoder/plugins/qoder-knowledge-bridge` and registers it locally; teammates should install from this repository with `qodercli plugin install` and create their own `qkb` PATH link as documented in `CONNECTORS.md`.
