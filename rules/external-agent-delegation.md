---
description: Rules for delegating coding work to the external Codex CLI with Qoder knowledge context.
alwaysApply: false
---

# External agent delegation

- Before delegating to Codex, build a context pack with `qkb context --task "<task>"` and include only the relevant excerpts.
- Give Codex a bounded goal, scope, non-goals, constraints, and acceptance checks. Ask for a summary of changed files and tests.
- Use the sandbox policy from `QKB_CODEX_FLAGS`; default to `--sandbox workspace-write`. Never use `danger-full-access` or approval bypass unless the user explicitly requests it.
- Never send secrets, environment variable values, tokens, or `.env` contents to an external agent.
- Do not commit, push, deploy, publish, or open pull requests from a delegated run unless the user explicitly asks.
- After Codex returns, inspect the full diff, check for scope creep and sensitive files, and run the documented tests or lint. Report what was not run.
- Stop after one delegation unless the user asks to iterate. If Codex fails, report the error instead of retrying with broader permissions.
