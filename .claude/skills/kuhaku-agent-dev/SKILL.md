---
name: kuhaku-agent-dev
description: Use this skill whenever you are about to read, edit, or extend code in the kuhaku-agent-cli repository — adding a messaging Surface, debugging the Coordinator's resolve/open/stream/seal/release phases, tracing a Slack streaming reply that hangs, fixing event parser bugs, extending the typer CLI, or investigating Managed Agents session errors. Trigger on any mention of the Surface / Inbound / Reply / Coordinator / Backend / ThreadStore types, the `kuhaku_agent` package, files under `src/kuhaku_agent/`, the `kuhaku-agent` CLI, or any task involving Slack Bolt + Anthropic Managed Agents. Load this skill before touching any file in this codebase so you do not accidentally re-introduce agentchannels' TypeScript names (`ChannelAdapter`, `StreamingBridge`, `SessionManager`, `AgentClient`, `ChunkParser`) or break the per-thread Coordinator gate that prevents concurrent stream races.
---

# kuhaku-agent-dev

Engineering aide for the `kuhaku-agent-cli` repository. The codebase intentionally diverges from agentchannels' naming so a reader never has to wonder which is which — keeping that divergence clean is the most important thing this skill does.

## Mental model

```
Slack ──► SlackSurface ──► Coordinator ──► Backend ──► Anthropic Managed Agents
                ▲              │   ▲              │
                │              │   └── ThreadStore (thread_key → session_id)
                │              │
                └── SlackReply ◄┘  (worker queue serializes Slack API calls)
```

`Coordinator.handle(inbound)` walks five named phases. The names are intentional and worth memorizing:

| Phase | What happens |
|---|---|
| `resolve` | `ThreadStore.lookup`; on miss, `Backend.open_thread` |
| `open` | `Surface.open_reply` returns a `Reply` |
| `stream` | Pump `Beat`s from `Backend.converse` into the `Reply` |
| `seal` | `Reply.seal(...)` — atomic close so `chat.stopStream` ordering holds |
| `release` | Post-flight (e.g. attach `/mnt/session/outputs/` files) |

A per-thread gate (`_ThreadGate` inside `Coordinator`) rejects a second inbound on the same thread with a hint, so you never have two streams racing on one Slack message.

## Naming guardrail

Do **not** introduce these TypeScript names from the sister project. The Python codebase deliberately uses different ones so cross-references in PRs are unambiguous:

| Don't write | Use this instead |
|---|---|
| `ChannelAdapter` | `Surface` |
| `ChannelMessage` | `Inbound` |
| `StreamHandle` | `Reply` |
| `MessageHandler` | `Listener` |
| `StreamTask` | `Step` |
| `SessionManager` | `ThreadStore` |
| `StreamingBridge` | `Coordinator` |
| `AgentClient` | `Backend` |
| `chunk-parser` / `parseSSEEvent` | `events.py` / `parse_event` |
| `AgentStreamEvent` | `Beat` (one of `Say` / `Tool` / `Stage` / `Hiccup` / `Done` / `RequiresAction`) |
| Phase: `session_resolve` etc. | `resolve / open / stream / seal / release` |
| Env: `CLAUDE_AGENT_ID` etc. | `KUHAKU_AGENT_ID` etc. |

If you find yourself reaching for one of the left-hand names, stop and re-check — it's the strongest signal that you're solving the problem in the wrong abstraction layer.

## Where to look first

```
src/kuhaku_agent/
├── backend.py          # Anthropic SDK wrapper (ping / converse / list_vaults / files)
├── coordinator.py      # 5-phase orchestration + per-thread gate
├── events.py           # Beat union + parse_event
├── thread_store.py     # in-memory mapping with optional idle TTL
├── settings.py         # 3-source loader (CLI > os.environ > .env)
├── runner.py           # build_runtime + serve()
├── cli.py              # typer commands: serve / vaults / doctor
└── surfaces/
    ├── base.py         # Surface ABC, Inbound, Reply, Step, Listener, Attachment, ToolDecision
    └── slack/
        ├── surface.py     # SlackSurface (Bolt Socket Mode + Block Kit approval + image download)
        ├── streamer.py    # SlackReply (single-worker queue + heartbeat)
        └── diagnostics.py # Hiccup → Slack mrkdwn
```

## Common workflows

For each of these the detailed recipe lives under `references/`. Read the matching file before changing code — they encode why the design is the way it is.

- **Adding a new Surface** (Discord, Teams, …) → `references/adding-surface.md`
- **Diagnosing MCP / Vault errors** → `references/troubleshooting.md`
- **Reproducing a streaming bug** or **changing event parsing** → `references/architecture.md`
- **Implementing or extending `kuhaku-agent init`** → `references/init-command.md`
- **Tool approval flow (`always_ask` / `requires_action` / `user.tool_confirmation`)** → `references/approval-flow.md`
- **Image attachments (Slack file → Anthropic image block)** → `references/image-attachments.md`
- **Releasing / running the bot in development** → `scripts/doctor.sh`

## Style guardrails

- **Sync only**, no `async`/`await`. Worker threads where ordering matters (`SlackReply` is the canonical example).
- `from __future__ import annotations` at the top of every module.
- `@dataclass(slots=True)` for hot-path types; protocols for duck-typed interfaces (`Reply`).
- Public functions are type-annotated. `Optional[T]` is fine; prefer `T | None` only when refactoring full files.
- No new top-level dependencies without updating `pyproject.toml` and refreshing `uv.lock`.
- Don't mutate `os.environ`. All settings flow through `Settings.load()`.

## Hard rules

These exist because the behaviour is non-obvious from the code alone:

1. **Vault lifecycle lives in the Anthropic Console**, not in this repo. Do not call `client.beta.vaults.create()` or `credentials.create()` from any module. The repo references vaults by id and lets Anthropic refresh tokens server-side.
2. **The Bolt event handler thread must not block on long work.** Push into `Coordinator.handle`, which itself is synchronous but guarded so concurrent inbounds on the same Slack thread are dropped with a hint instead of stacking.
3. **`Reply.seal` must be called exactly once per `open_reply`.** The Slack worker shuts down on seal; calling write/seal afterwards is a no-op but a sign of a logic error upstream.
4. **Do not seal on `RequiresAction`.** When `session.status_idle` arrives with `stop_reason.type == "requires_action"`, the agent is paused awaiting human approval — not finished. The `Reply` must stay live until every `tool_use_id` is resolved via `user.tool_confirmation` and the resumed run reaches `end_turn`. See `references/approval-flow.md`.
5. **Vault credential URL must match the Agent's `mcp_servers[].url` byte-for-byte.** Trailing slash, scheme, and subdomain count. Mismatch surfaces as `mcp_authentication_failed_error: no credential is stored for this server URL` — see `references/troubleshooting.md`.
6. **Slack image attachments require the `files:read` scope.** Without it, `url_private` returns an HTML auth page that we forward as base64 to Anthropic, which then 400s with `Could not process image`. The `_sniff_image_mime` magic-byte check catches this and logs a clear ERROR — but the only fix is adding the scope and reinstalling. See `references/image-attachments.md`.

## Tests

`tests/` is empty as of this skill's creation. When you add a test, mirror the package layout (`tests/core/test_events.py`, `tests/surfaces/slack/test_streamer.py`). Pure logic (`events.parse_event`, `thread_store`) is the cheapest place to start.
