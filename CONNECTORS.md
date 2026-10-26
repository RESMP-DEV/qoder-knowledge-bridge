# Connectors

This plugin's connector is a local CLI named `qkb`. It is not an MCP server. It reads Qoder's local Repo Wiki, knowledge cards, repo state, and memory in read-only mode.

## Requirements

- macOS or Linux.
- Python 3.9+ with the standard-library `sqlite3` module.
- `git` for repository state and staleness checks.
- Optional: Codex CLI for the `codex-bridge` agent.
- Optional: Qoder Repo Wiki and knowledge cards. `qkb` degrades to repo and memory context when none exist.

## Put qkb on PATH

```bash
mkdir -p "$HOME/.local/bin"
ln -sfn "$HOME/.qoder/plugins/qoder-knowledge-bridge/bin/qkb" "$HOME/.local/bin/qkb"
qkb projects
```

If `$HOME/.local/bin` is not on `PATH`:

```bash
export PATH="$HOME/.qoder/plugins/qoder-knowledge-bridge/bin:$PATH"
```

For a team, each person installs the plugin under their own Qoder config root and creates their own symlink.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `QODER_HOME` | `~/.qoder` | Qoder config root containing `qoder-knowledge/` and `memory/`. |
| `QKB_PROVENANCE` | enabled | Set to `off`, `0`, `false`, or `no` to stop hook logging. |
| `QKB_CODEX_FLAGS` | `--sandbox workspace-write` | Flags appended to `codex exec` by the `codex-bridge` agent. |
| `QODER_PLUGIN_DATA` | Qoder-provided | Set by Qoder for plugin hooks; provenance is written here. |

`QKB_CODEX_FLAGS` is parsed as shell words. Do not put secrets in it. Examples:

```bash
export QKB_CODEX_FLAGS="--sandbox read-only"
export QKB_CODEX_FLAGS="--sandbox workspace-write --model gpt-5-codex"
```

Avoid `danger-full-access` and approval bypass unless you have an external sandbox and explicitly accept the risk.

## External agent setup

### Codex

Add the following to the repository's `AGENTS.md`:

```markdown
Before planning or editing, run:
  qkb context --task "<task>" --project "$PWD"
  qkb search "<key term>" --project "$PWD"

Cite page/card IDs and branch/baseline details.
Treat live code as implementation truth; Repo Wiki and cards describe intent.
Never write to ~/.qoder/qoder-knowledge.
```

Codex then gets the same cited context that Qoder agents use. Keep context packs as text; do not point external agents directly at the SQLite files, because they would lose citation and staleness handling.

### Other coding agents

Use the same instructions in the tool's project-level guidance (for example, `AGENTS.md` or `CLAUDE.md`). `qkb` is a normal executable and works with any agent that can run shell commands.

## QMind

QMind notebooks and sources are remote and authenticated. Qoder subagents inherit the session's MCP tools, including the QMind server when it is enabled, so `knowledge-scout` can query QMind and cite notebooks or sources.

`qkb` is deliberately offline and does not bridge QMind. External coding agents need their own QMind MCP configuration and credentials; this plugin does not bundle either. If QMind is unavailable, agents must say so rather than invent results.

## Provenance

Qoder hooks write one JSON object per line to:

```text
${QODER_PLUGIN_DATA}/provenance.jsonl
```

Fields include `ts`, `session_id`, `cwd`, `origin` (`qkb` or `mcp`), the qkb `command` or MCP `tool` plus input metadata, and `response_chars`. Tool responses and file contents are never stored.

Inspect recent entries:

```bash
jq -c 'select(.origin=="qkb")' "$QODER_PLUGIN_DATA/provenance.jsonl" | tail -20
```

Disable logging for a session with `QKB_PROVENANCE=off`.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `qkb: no knowledge bases found` | Generate a Repo Wiki or knowledge cards in Qoder Knowledge Center, or run `qodercli wiki --repo <path>`. |
| `no knowledge base matches <path>` | Use `--project <repo>`; run `qkb projects` to see registered owners. |
| `ambiguous knowledge base` | Use `--project` with a more specific repository path. |
| Stop hook reports stale knowledge | Run `qodercli wiki --update --repo <path>` and regenerate cards if needed. |
| `qkb` not found in Codex | Recreate the symlink or add the plugin `bin/` directory to `PATH`. |
| Hooks do not fire | Confirm the plugin is enabled in `~/.qoder/settings.json`, then start a new session. |
| `unable to open database file` | Check `QODER_HOME` and file permissions; the plugin only reads the current user's Qoder data. |
| QMind unavailable in an external CLI | Expected. QMind is exposed through Qoder MCP, not `qkb`. |

## Read-only guarantee

- SQLite databases are opened with `file:...?mode=ro` and `PRAGMA query_only=ON`.
- `qkb` does not create, modify, or delete anything under `qoder-knowledge/`.
- Provenance is written outside the knowledge directories, with owner-only permissions where the filesystem supports them.
- The only write-capable path is the `codex-bridge` agent's delegated coding work in the target repository, which follows the configured Codex sandbox.

## Uninstall

1. Disable the plugin in `~/.qoder/settings.json` (`enabledPlugins`) and remove its entry from `~/.qoder/plugins/installed_plugins_v2.json`.
2. Remove the `qkb` symlink from `$HOME/.local/bin`.
3. Optionally delete `${QODER_PLUGIN_DATA}/provenance.jsonl` and the plugin directory.
