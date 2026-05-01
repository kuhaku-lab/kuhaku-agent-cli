# kuhaku-agent-cli

A Python bridge between Slack and Claude Managed Agents. When a user @-mentions the bot in Slack, the message becomes one turn of a Managed Agents session, and the response is streamed back via `chat.startStream` (with `chat.update` as a fallback). One Slack thread maps to one Managed Agents session.

Vault, OAuth, and MCP credential management are **out of scope** for this repository — they live in the [Anthropic Console](https://console.anthropic.com). This CLI only references existing Agents, Environments, and Vaults; Anthropic refreshes tokens server-side.

> 日本語版は [README.md](README.md) を参照してください。

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

## Install

```bash
uv sync
uv run kuhaku-agent --version
```

## Setup

### 1. Collect tokens

| Source | Value |
|---|---|
| https://console.anthropic.com | `ANTHROPIC_API_KEY` |
| https://api.slack.com/apps → your app → OAuth & Permissions | `SLACK_BOT_TOKEN` (`xoxb-...`) |
| Same app → Basic Information → App-Level Tokens (scope: `connections:write`) | `SLACK_APP_TOKEN` (`xapp-...`) |

### 2. Create `.env`

```bash
cp env.example .env   # if env.example exists; otherwise create manually
```

Minimum contents:

```dotenv
ANTHROPIC_API_KEY=sk-ant-...
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
```

### 3. Bootstrap Agent and Environment

`kuhaku-agent init` creates a Managed Agent and an Environment via the Anthropic API and appends the resulting IDs to your `.env`. It also runs a quick Slack `auth.test` at the end to catch obvious token issues.

```bash
uv run kuhaku-agent init
```

After it succeeds, `.env` gains two lines:

```dotenv
KUHAKU_AGENT_ID=agent_...
KUHAKU_ENVIRONMENT_ID=env_...
```

#### `init` options

```bash
# Create one resource at a time
uv run kuhaku-agent init agent
uv run kuhaku-agent init environment

# Override the agent's system prompt
uv run kuhaku-agent init agent --system-file prompts/my-system.md
uv run kuhaku-agent init agent --system "You are..."

# Open up the environment sandbox
uv run kuhaku-agent init environment \
    --allowed-host hooks.slack.com \
    --allowed-host api.notion.com \
    --pip pandas --pip openpyxl

# Print only — don't touch .env
uv run kuhaku-agent init --no-write-env

# Skip the Slack smoke test at the end
uv run kuhaku-agent init --skip-slack-check
```

`init` (no subcommand) runs the wizard. If `KUHAKU_AGENT_ID` already exists in `.env`, it's reused; only the missing resources are created. The wizard also writes the spec used to `agents/<name>.json` so you can review or version-control it later.

#### Manage Agent specs as JSON

For richer agents (MCP servers, tools, skills) you can drive creation from a JSON spec file:

```bash
# 1. Emit a default template
uv run kuhaku-agent init agent --template-out agents/0xbot.json

# 2. Edit agents/0xbot.json — add mcp_servers, tools, skills, metadata.

# 3. Create the Agent from the edited spec
uv run kuhaku-agent init agent --from-file agents/0xbot.json
```

Spec shape (mirrors the Anthropic SDK's `agents.create` payload):

```json
{
  "name": "0xbot",
  "description": "0X internal Slack assistant",
  "model": { "id": "claude-sonnet-4-6", "speed": "standard" },
  "system": "You are 0xbot, the internal Slack assistant for 0X...",
  "mcp_servers": [
    { "name": "slack",  "url": "https://mcp.slack.com/mcp",  "type": "url" },
    { "name": "notion", "url": "https://mcp.notion.com/mcp", "type": "url" }
  ],
  "tools": [
    { "type": "agent_toolset_20260401",
      "default_config": { "enabled": true, "permission_policy": { "type": "always_allow" } } },
    { "type": "mcp_toolset", "mcp_server_name": "slack",
      "default_config": { "enabled": true, "permission_policy": { "type": "always_ask" } } },
    { "type": "mcp_toolset", "mcp_server_name": "notion",
      "default_config": { "enabled": true, "permission_policy": { "type": "always_ask" } } }
  ],
  "skills": [
    { "skill_id": "skill_01ABC...", "type": "custom", "version": "latest" }
  ],
  "metadata": { "owner": "people-ops", "version": "0.1.0" }
}
```

To create with the simple flags but still keep the spec on disk, combine with `--save-spec`:

```bash
uv run kuhaku-agent init agent \
    --name 0xbot \
    --system-file prompts/0xbot.md \
    --save-spec agents/0xbot.json
```

### 4. Vaults (optional)

To use MCP (Slack, Notion, etc.), create a Vault in the Anthropic Console and add credentials via the in-Console OAuth flow. Then add the IDs to `.env`:

```dotenv
KUHAKU_VAULT_IDS=vault_a,vault_b
```

> Vault creation and credential authorization happen exclusively in the Anthropic Console because the OAuth flow is hosted there. This CLI never creates vaults.

### 5. Thread persistence

The Slack-thread ↔ Managed Agents session mapping is saved to a JSON file so the conversation continues across `serve` restarts. **Persistence is on by default** and the file lives at `.kuhaku/threads.json` in the current working directory — add it to `.gitignore`.

To change the location, set in `.env`:

```dotenv
KUHAKU_THREAD_STORE_PATH=~/.kuhaku-agent/threads.json
```

Delete the file to reset history; subsequent mentions will start from fresh sessions.

### 6. Verify

```bash
uv run kuhaku-agent doctor      # validate settings + API ping
uv run kuhaku-agent vaults      # list vaults and their credentials
```

## Run

```bash
uv run kuhaku-agent serve
```

Invite the bot to a Slack channel and mention it: `@kuhaku-agent your question`. Reply in-thread to continue the conversation as a multi-turn session.

## Enable the Slack Assistant feature (recommended)

To use `chat.startStream` (plan-mode) for smooth progress display and a heartbeat spinner, enable the Slack App as an **Agent / Assistant**:

1. https://api.slack.com/apps → your app → **Agents & AI Apps** tab → **Turn on**
2. Under **OAuth & Permissions**, add the `assistant:write` Bot Token Scope
3. **Reinstall to Workspace** and update `SLACK_BOT_TOKEN` in `.env` with the new `xoxb-...`
4. Restart `uv run kuhaku-agent serve`

Without this the bot still works, but falls back to `chat.update` and the spinner stops on the first text delta.

Confirm by looking for `chat.startStream(plan) ok ts=...` in the logs (success) versus `chat.startStream unavailable, using post+update: ...` (fallback).

## Tool approval flow

When an MCP toolset in the Agent spec uses `permission_policy.type = "always_ask"`, the agent pauses before executing the tool (`session.status_idle / requires_action`) and the bot posts a Block Kit **Approve / Deny** message in the Slack thread.

When the operator clicks, the bot sends a `user.tool_confirmation` event via the SDK and the session resumes — output streams into the same reply.

- During the wait, the plan area shows an "Awaiting approval" task in_progress; after approval it switches to "Running tool"
- Deny lets the agent try an alternative (or end the turn)
- Auto-expiry of pending approvals isn't implemented; restarting the bot loses pending state

If the approval UI gets in the way during development, switch the policy to `always_allow` in the spec. Full reference: `.claude/skills/kuhaku-agent-dev/references/approval-flow.md`.

## Image attachments

Attach an image (PNG / JPEG / GIF / WebP) to your Slack mention and the bot forwards it to the Agent alongside your text — useful for receipt OCR, screenshot explanations, diagram reading, etc.

### Required Slack scope

Add **`files:read`** to the Bot Token Scopes. Without it, `url_private` returns an HTML auth-error page and Anthropic responds with `Could not process image`.

1. https://api.slack.com/apps → your app → **OAuth & Permissions**
2. Add **`files:read`** to Bot Token Scopes
3. **Reinstall to Workspace** and update `SLACK_BOT_TOKEN` in `.env` with the new `xoxb-...`

### Required on the Anthropic side

- The Agent's `model.id` must support vision (Sonnet 4.x / Opus 4.x series)
- Sonnet 3 and earlier do not accept image content blocks

### Limits

- **20 MiB** per file (oversized attachments are dropped with a surface-level warning)
- Base64 inline encoding — large or numerous images bloat the request
- Magic-byte sniffing rejects non-image bytes (catches the missing-scope case automatically)

Full reference: `.claude/skills/kuhaku-agent-dev/references/image-attachments.md`.

## CLI commands

```
kuhaku-agent --version
kuhaku-agent doctor                    # validate config + reach the API
kuhaku-agent vaults                    # list vaults and credentials
kuhaku-agent init                      # create Agent + Environment (wizard)
kuhaku-agent init agent                # create only the Agent
kuhaku-agent init environment          # create only the Environment
kuhaku-agent serve                     # start the Slack listener (-v for verbose)
```

## Architecture

```
src/kuhaku_agent/
├── backend.py           # Anthropic SDK wrapper (sessions / vaults / files / agents / envs)
├── coordinator.py       # 1 inbound → 1 streaming reply across 5 phases
├── events.py            # SSE events → Beat (Say / Tool / Stage / Hiccup / Done / RequiresAction)
├── thread_store.py      # thread_key → session_id map (RLock-guarded, JSON-backed)
├── settings.py          # 3-source config: CLI > os.environ > .env
├── init_ops.py          # backend for `init` (upsert_env_line, defaults, …)
├── runner.py            # build_runtime + serve()
├── cli.py               # typer entry points
└── surfaces/
    ├── base.py          # Surface ABC, Inbound, Reply, Step, ToolDecision
    └── slack/
        ├── surface.py     # Bolt Socket Mode adapter + Block Kit approval UI
        ├── streamer.py    # SlackReply: chat.startStream + heartbeat + fallback
        └── diagnostics.py # Hiccup → user-facing Slack message
```

See `CLAUDE.md` for development conventions. Recipes for adding surfaces, troubleshooting MCP failures, the tool approval flow, and extending `init` live under `.claude/skills/kuhaku-agent-dev/references/`.

## Adding a new Surface

1. Subclass `kuhaku_agent.surfaces.base.Surface`.
2. Implement `start / stop / listen / post / open_reply`.
3. The returned `Reply` must be safe to call from any thread — `SlackReply`'s single-worker queue is the canonical example.
4. Wire it into `runner.py:build_runtime`.

Detailed walkthrough: `.claude/skills/kuhaku-agent-dev/references/adding-surface.md`.

## License

Apache License 2.0 — see [`LICENSE`](LICENSE).

### Attribution requirement for forks and derivative works

When forking, modifying, or redistributing this repository, you must — **in addition to the standard Apache 2.0 conditions** — clearly state that your work is derived from this project (`kuhaku-agent-cli`). See [`NOTICE`](NOTICE) for the full terms.

Concretely:

- Include "Forked from `kuhaku-agent-cli`" (with a link) in the README or equivalent top-level documentation of your fork
- Retain the original `NOTICE` file and add your own derivative info alongside it
- Continue to satisfy the regular Apache 2.0 §4 obligations (ship `LICENSE`, mark changed files, retain copyright notices)

Removing or hiding the attribution section of `NOTICE` violates Apache 2.0 §4(c) and the additional terms set forth by this project.
