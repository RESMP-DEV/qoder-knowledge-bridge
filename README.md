# Qoder Knowledge Bridge

Bridges Qoder's local knowledge into Qoder subagents and external coding agents such as Codex. It ships **no skills** by design: the capability is delivered through two agents, two rule files, three hooks, and a read-only CLI connector.

Repository standards for agents: [AGENTS.md](AGENTS.md). Kilo review guidance: [REVIEW.md](REVIEW.md).

## What it does

| Surface | Purpose |
| --- | --- |
| `qkb` CLI | Read-only access to Qoder Repo Wiki pages, knowledge cards, repo state, and memory. Works offline, no MCP server, no credentials. |
| `knowledge-scout` agent | Gathers architecture, conventions, prior decisions, and citations before planning or coding. Read-only. |
| `codex-bridge` agent | Builds a cited context pack, delegates a bounded task to `codex exec`, then verifies the diff and tests. |
| Rules | Citation/staleness guardrails and external-delegation safety rules. |
| Hooks | SessionStart knowledge index, PostToolUse provenance logging, Stop summary with stale-wiki warning. |

QMind notebooks and sources are available to Qoder subagents through Qoder's MCP tools. `qkb` itself is offline and does not access QMind; see [CONNECTORS.md](CONNECTORS.md).

## Requirements

- macOS or Linux with Python 3.9+ and the standard-library `sqlite3` module.
- `git` for repo state and staleness checks.
- Qoder Desktop or Qoder CLI with plugin support.
- Optional: the Codex CLI (`codex`) for the `codex-bridge` agent.
- Optional: a generated Repo Wiki or knowledge cards. Without one, `qkb` still provides repo and memory context.

## Quick start

```bash
qkb projects
qkb index --project "$PWD"
qkb search "routing failover" --project "$PWD"
qkb context --task "explain the routing failover path" --project "$PWD"
qkb page <page-id>
qkb card <card-id>
qkb repo --project "$PWD"
qkb memory --project "$PWD" --search "failover"
```

If `qkb` is not on `PATH`:

```bash
export PATH="$HOME/.qoder/plugins/qoder-knowledge-bridge/bin:$PATH"
# or
ln -sfn "$HOME/.qoder/plugins/qoder-knowledge-bridge/bin/qkb" "$HOME/.local/bin/qkb"
```

## Using the agents in Qoder

The agents are selected automatically from their descriptions, and can also be invoked directly as `/knowledge-scout` and `/codex-bridge`.

- `knowledge-scout` is read-only. It runs `qkb context`, drills into pages/cards, checks live code, and returns a cited context pack.
- `codex-bridge` checks for `codex`, builds a context pack, writes a bounded delegation prompt, runs `codex exec`, and then verifies the diff and documented tests. It does not commit, push, deploy, or pass secrets.

## Using with Codex externally

Add this to a repository's `AGENTS.md` so Codex pulls Qoder knowledge before planning:

```markdown
Before planning or editing, run:
  qkb context --task "<task>" --project "$PWD"
  qkb search "<key term>" --project "$PWD"

Cite page/card IDs and branch/baseline details in plans and reviews.
Treat live code as implementation truth; Repo Wiki and cards describe intent.
Never write to ~/.qoder/qoder-knowledge.
```

Then run Codex normally. For the delegated path, `QKB_CODEX_FLAGS` controls the sandbox (default `--sandbox workspace-write`).

## Data sources

| Source | Location | Access |
| --- | --- | --- |
| Repo Wiki | `~/.qoder/qoder-knowledge/knowledge-*/wiki/wiki.v1.db` (`section`) | Read-only SQLite |
| Knowledge cards | `~/.qoder/qoder-knowledge/knowledge-*/cards/cards.v1.db` (`card`, `module`, `module_card`, `relation`) | Read-only SQLite |
| Repo state | Git plus `AGENTS.md` / `README` | Read-only files and `git` commands |
| Qoder memory | `~/.qoder/memory/MEMORY.md` and project memory indexes | Read-only files |
| QMind | Remote QMind service | Qoder MCP tools only |

Every SQLite connection uses a `mode=ro` URI and `PRAGMA query_only=ON`. `qkb` never creates, updates, or deletes knowledge data.

## Hooks

| Event | Behavior |
| --- | --- |
| `SessionStart` | Injects a short index for the matching knowledge base, including branch, page/card counts, and staleness vs. HEAD. Silent when no KB matches. |
| `PostToolUse` | Logs `qkb` commands and knowledge/QMind MCP reads to `${QODER_PLUGIN_DATA}/provenance.jsonl`. Commands and input metadata only; responses are not stored. |
| `Stop` | Summarizes knowledge consulted in the session and warns when the wiki baseline differs from HEAD. Never blocks. |

Hook failures exit `0` so they cannot interrupt a session. Set `QKB_PROVENANCE=off` to disable logging.

## Rules

- `rules/knowledge-provenance.md`: cite knowledge, prefer code over stale docs, flag baseline drift.
- `rules/external-agent-delegation.md`: bounded delegation, no secrets, sandbox policy, verification.

The current Qoder CLI reports the manifest `rules` field as unknown and ignores it. The same guardrails are injected by the SessionStart hook. To apply the rule files as project rules, symlink or copy them into `<repo>/.qoder/rules/`.

## Security

- Knowledge databases are opened read-only; writes are impossible through `qkb`.
- No credentials, tokens, or account-specific endpoints are bundled.
- Provenance logs contain command/input metadata, are created with owner-only permissions, and never store tool responses.
- The Codex bridge defaults to the `workspace-write` sandbox and refuses commits, pushes, deploys, and secret sharing unless the user explicitly asks.

## Layout

```text
qoder-knowledge-bridge/
  .qoder-plugin/plugin.json
  agents/knowledge-scout.md
  agents/codex-bridge.md
  rules/knowledge-provenance.md
  rules/external-agent-delegation.md
  hooks/hooks.json
  scripts/qkb_core.py
  scripts/session_index.py
  scripts/provenance_hook.py
  scripts/stop_summary.py
  bin/qkb
  README.md
  CONNECTORS.md
```

## Validation

Validated on 2026-10-25:

- Bundled plugin validator: `ok: true`, one warning — `plugin.json does not declare skills`. This is intentional; the plugin ships agents, rules, hooks, and a connector, no skills.
- `qodercli plugin validate`: `valid: true`, 6 components, 0 errors, 1 warning — the current CLI ignores the `rules` manifest field. The rule files ship, and their guardrails are injected by the SessionStart hook.
- Discovery: `qodercli plugin list` shows `qoder-knowledge-bridge@local` enabled at user scope; `qodercli agents list` shows `qoder-knowledge-bridge:knowledge-scout` and `qoder-knowledge-bridge:codex-bridge`.
- Read-only proof: a `CREATE TABLE` attempt through the CLI's SQLite helper fails with `attempt to write a readonly database`.
- Hook simulations: SessionStart context, PostToolUse provenance JSONL, and Stop summary/stale detection all behave as documented.

See [CONNECTORS.md](CONNECTORS.md) for setup and troubleshooting.

## License

MIT. See [LICENSE](LICENSE).
