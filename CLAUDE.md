# CLAUDE.md

This file briefs Claude Code on how `kuhaku-agent-cli` is laid out so it can navigate and modify the project without exploring from scratch every session.

## What this is

`kuhaku-agent-cli` bridges messaging surfaces (Slack first) to Claude Managed Agents. A user @-mentions the bot in Slack; the message becomes one turn in a streaming Managed Agents session keyed by Slack thread.

## Commands

```bash
uv sync                                    # install deps
uv run kuhaku-agent --version              # version check
uv run kuhaku-agent doctor                 # validate settings + API auth
uv run kuhaku-agent vaults                 # list available Anthropic Vaults
uv run kuhaku-agent serve                  # start the Slack listener
uv run kuhaku-agent serve -v               # verbose logs
```

## Architecture

Single Python package under `src/kuhaku_agent/`. Every module has one job:

```
backend.py        Anthropic SDK wrapper. Owns sessions / vaults / files.
coordinator.py    One inbound → one streaming reply. Phases: resolve / open
                  / stream / seal / release. Per-thread gate prevents races.
events.py         Translate raw SSE events into typed Beats (Say / Tool /
                  Stage / Hiccup / Done).
thread_store.py   In-memory thread_key → session_id map (RLock-guarded).
settings.py      Three-source config: CLI override > os.environ > .env file.
runner.py         Composition root: build Backend + Surface + Coordinator.
cli.py            typer app — entrypoints `serve`, `vaults`, `doctor`.

surfaces/
  base.py         Surface ABC, Inbound, Reply (Protocol), Step, Listener.
  slack/
    surface.py    Bolt Socket Mode adapter, mention stripping.
    streamer.py   SlackReply — chat.startStream + appendStream + stopStream
                  with chat.update fallback. Single worker thread per reply.
    diagnostics.py  Hiccup → Slack-flavored remediation text.
```

### Naming

The codebase deliberately avoids agentchannels' TypeScript names so future readers don't conflate the two:

| Concept | Here | agentchannels (for reference) |
|---|---|---|
| Channel adapter | `Surface` | `ChannelAdapter` |
| Incoming message | `Inbound` | `ChannelMessage` |
| Streaming output | `Reply` | `StreamHandle` |
| Per-message orchestrator | `Coordinator` | `StreamingBridge` |
| Thread → session map | `ThreadStore` | `SessionManager` |
| Anthropic SDK wrapper | `Backend` | `AgentClient` |
| SSE event union | `Beat` (Say/Tool/Stage/Hiccup/Done) | `AgentStreamEvent` |
| Lifecycle phases | resolve / open / stream / seal / release | session_resolve / stream_start / streaming / completing / cleanup |

### Vault management

**Out of scope here.** Vaults are created and OAuth credentials added via the Anthropic Console (https://console.anthropic.com). The bot only references vault IDs by passing them to `client.beta.sessions.create()`. Anthropic refreshes tokens server-side.

If a session emits `mcp_connection_failed_error`, `surfaces/slack/diagnostics.py` produces a Slack message that points the user back to the Console.

## Conventions

- **Python 3.12+** with `uv` for dependency management.
- **`src/` layout** so the package is importable only after install.
- **Sync code path**, no `async/await`. The Slack worker thread, plus a small per-reply worker thread for ordering Slack API calls, is enough; we don't need a full async runtime.
- **typer** for CLI, **rich** for terminal output.
- **No runtime mutation of `os.environ`** — settings flow through `Settings.load()`.
- Tests (none yet) belong under `tests/` mirroring `src/kuhaku_agent/`.

## Required environment variables

See `.env.example`. Minimum to run `serve`:

```
ANTHROPIC_API_KEY
KUHAKU_AGENT_ID
KUHAKU_ENVIRONMENT_ID
SLACK_BOT_TOKEN
SLACK_APP_TOKEN
KUHAKU_VAULT_IDS    # optional, comma-separated
```

## Adding a new surface

1. Subclass `kuhaku_agent.surfaces.base.Surface`.
2. Implement `start` / `stop` / `listen` / `post` / `open_reply`. The `Reply` returned should be safe to `write` from any thread.
3. Wire it into `runner.py` (or expose it through a CLI subcommand).
