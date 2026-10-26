<review-guidance repository="RESMP-DEV/qoder-knowledge-bridge">
# Review guidance for RESMP-DEV/qoder-knowledge-bridge

Kilo reads this file from the pull request base branch. Apply these repository-owned rules to changed behavior and its reachable callers. This file does not authorize weakening the read-only knowledge contract or the plugin's safety boundaries.

## What matters in this repository

- Read-only knowledge access is the core safety contract. Every SQLite connection must use a read-only URI and `PRAGMA query_only=ON`. Flag any write path, schema change, or code that could create, modify, or delete under `~/.qoder/qoder-knowledge`.
- Runtime data stays out of the repository: no knowledge databases, `MEMORY.md`, provenance logs, Qoder settings or registries, credentials, personal paths, or machine-specific endpoints.
- The plugin intentionally ships no skills. Flag additions of a `skills/` directory or a `skills` manifest field unless the user explicitly changes the product shape.
- Hooks must be non-blocking. A hook failure must not block a session; it should log a short diagnostic and exit 0. Provenance records metadata only, never responses or file contents.
- `qkb` must remain Python 3.9-compatible, standard-library only, with no shell injection, safe path handling, and a `QODER_HOME` override. JSON output must stay valid.
- Codex delegation defaults to `workspace-write`. Flag approval bypass, `danger-full-access`, secret exposure, commits, pushes, deploys, or unbounded retries.
- The manifest must remain valid: component paths inside the repository, agents discovered from `agents/`, and a real hooks path. The `rules` manifest-field warning is known.
- QMind stays behind Qoder MCP. Do not add QMind credentials or endpoints to `qkb`.

## Severity calibration

- **Critical:** knowledge data corruption or writes, credential or secret exposure, arbitrary command execution, hook-driven session blocking or data loss, or a delegated Codex run that can push or publish without approval.
- **Warning:** a reproducible correctness, validation, compatibility, safety, or observability defect with a reachable changed path, including stale citations presented as current, invalid manifest paths, broken hook JSON, or unhandled SQLite errors.
- **Suggestion:** a bounded maintainability improvement that reduces future defect risk without indicating the change is wrong.
- Do not flag formatting-only differences, personal style, generated-file churn without a concrete consequence, or speculative performance concerns without a hot path and mechanism.
- Do not duplicate one root cause across multiple comments. Prefer the highest-signal location.

## Verification expectations

- New or changed behavior needs a focused check that asserts the observable result and at least one material edge case. Use the commands in `AGENTS.md`; do not invent a test suite that is not checked in.
- Manifest changes: `qodercli plugin validate`, JSON parsing, component path existence, and a `qkb --version` probe.
- CLI changes: `qkb projects` plus either a real knowledge-base context/search/page/card run or a temporary-home fixture; confirm read-only behavior and valid JSON.
- Hook changes: simulate SessionStart, PostToolUse, and Stop with representative JSON; confirm non-blocking behavior, metadata-only provenance, and the `stop_hook_active` guard.
- Changes touching Codex delegation: verify default flags, secret handling, no commit or push, and diff and test reporting.
- Missing tests are a finding when changed behavior is unverified, not a blanket request for coverage on untouched code.

## Security, privacy, and performance

- Trace untrusted data from SQLite content and repository text into markdown and JSON output. Check for shell injection, path traversal, unbounded output, and unbounded scans.
- Never recommend committing secrets, tokens, knowledge data, provenance logs, or user-specific paths.
- Performance findings must identify the code path and scale. `qkb` should cap search results, context budgets, and page output.

## Review summary and comment style

- Lead with concrete findings ordered by severity. If there are none, say so and name residual risks or verification gaps.
- Each finding identifies the changed path, mechanism, impact, and a practical fix or verification direction.
- Separate confirmed defects from assumptions. Do not claim commands, hooks, or Qoder Desktop runs were exercised unless the evidence shows it.
</review-guidance>
