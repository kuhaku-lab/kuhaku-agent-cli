# Architecture (deep dive)

This is the long-form companion to `SKILL.md`. Read it when you need to understand why the modules are split the way they are, or when you're about to change behaviour at a layer boundary.

## Table of contents

- [Lifecycle of a single inbound](#lifecycle-of-a-single-inbound)
- [Why phases have those names](#why-phases-have-those-names)
- [`events.py` — the SSE → `Beat` translator](#eventspy--the-sse--beat-translator)
- [`backend.py` — boundary with the Anthropic SDK](#backendpy--boundary-with-the-anthropic-sdk)
- [`thread_store.py` — keyed by thread, not session](#thread_storepy--keyed-by-thread-not-session)
- [`SlackReply` — the worker queue, in detail](#slackreply--the-worker-queue-in-detail)
- [Concurrency model](#concurrency-model)

## Lifecycle of a single inbound

```
Slack mention event
        │
        ▼
SlackSurface._on_mention(event)        ← Bolt thread
        │
        │  build Inbound, dispatch to listeners
        ▼
Coordinator.handle(inbound)            ← still on Bolt thread
        │
        │  ─── resolve ───────────────────────────────
        │   ThreadStore.lookup(key)
        │   ├─ hit  → reuse session_id
        │   └─ miss → Backend.open_thread() then remember
        │
        │  ─── open ───────────────────────────────────
        │   reply = surface.open_reply(...)
        │   reply.write(busy_hint)        ← initial placeholder
        │
        │  ─── stream ─────────────────────────────────
        │   with backend.converse(sid, text) as frames:
        │       for frame in frames:
        │           dispatch beats:
        │             Say   → reply.write
        │             Tool  → append to plan, reply.show_steps
        │             Hiccup → reply.seal(diagnose(beat)); return
        │             Done   → break out
        │
        │  ─── seal ───────────────────────────────────
        │   reply.seal(None)  (final text already streamed)
        │
        │  ─── release ────────────────────────────────
        │   on_outputs(session_id, inbound) hook
```

The whole chain runs on the Bolt event handler thread because the per-thread gate already prevents two messages from racing. If a worker pool is ever needed, it lives between `_on_mention` and `Coordinator.handle`, never inside `Coordinator`.

## Why phases have those names

agentchannels uses `session_resolve / stream_start / streaming / completing / cleanup`. Those names embed two concepts (session + stream) and one is opinionated about completion. We use shorter, neutral verbs:

- `resolve` — find or open a session
- `open` — open the user-visible output channel
- `stream` — pump output
- `seal` — finalize the output (more evocative than "complete" of "no more writes after this")
- `release` — release any session-scoped resources

`seal` matters: Slack's `chat.stopStream` is rejected if `chat.appendStream` is still in flight. The verb reminds the reader that it's a terminal, ordered operation.

## `events.py` — the SSE → `Beat` translator

The Anthropic Managed Agents SSE stream emits many event types — most of which the bridge does not care about (`message_start`, `span.*`, etc.). `parse_event` collapses the stream into a small, exhaustive union:

```
Beat = Say | Tool | Stage | Hiccup | Done | RequiresAction
```

Why a `ParsedFrame` (with both `beats` and `terminal`) and not just `Beat | None`?

Some events emit zero beats but still terminate the stream (`session.deleted`). Others emit several beats from one event (a `agent.message` with multiple text blocks). Returning a frame keeps both signals on every iteration without extra plumbing.

`session.status_idle` is a discriminated union over `stop_reason.type`:

| `stop_reason.type` | Beat | terminal |
|---|---|---|
| `end_turn` | `Done(why="end_turn")` | `True` |
| `requires_action` | `RequiresAction(event_ids=...)` | `True` (close the SSE; we'll reopen after `user.tool_confirmation`) |
| `retries_exhausted` | `Hiccup(kind="retries_exhausted", ...)` | `True` |

The `requires_action` branch is the pause-and-resume path covered in `references/approval-flow.md`. The Coordinator must not seal the `Reply` on this beat — the conversation continues after the human decides.

Add a new event type by extending `parse_event`. Do **not** sprinkle `if etype == ...` in `Coordinator` — that's the layering invariant that lets us evolve event handling without touching orchestration.

## `backend.py` — boundary with the Anthropic SDK

`Backend` is the only file allowed to import `anthropic`. Everything else talks to it through:

- `Backend.ping()` — cheap auth check
- `Backend.open_thread(...)` — `client.beta.sessions.create`
- `Backend.converse(session_id, text)` — context manager that opens the SSE stream, sends `user.message`, and yields `ParsedFrame`s
- `Backend.list_vaults()` — for the `kuhaku-agent vaults` CLI
- `Backend.session_outputs / download_session_file` — file attach support

`converse` is a context manager because the SDK's stream needs an explicit close. Wrapping the two-step "open stream / send message" protocol behind a single `with` removes the easiest footgun in the codebase.

## `thread_store.py` — keyed by thread, not session

The map is `thread_key → session_id`, not the reverse. We never need to look up "which thread does this session belong to" — sessions are an implementation detail. Naming the type after its key makes the intent visible at every callsite (`ThreadStore.lookup(thread_key)` is self-documenting; `SessionManager.get(thread_key)` is not).

Optional `idle_ttl` evicts entries on lookup. Eviction is lazy on purpose: a background reaper would need its own thread for behaviour we mostly don't need (process restarts already drop everything).

## `SlackReply` — the worker queue, in detail

Three things are happening concurrently on the Slack side:

1. The agent stream emits text deltas one after another.
2. The Slack Web API requires ordered calls — `appendStream` after `appendStream`, `stopStream` last.
3. Slack rate-limits aggressively; transient failures must not poison subsequent calls.

`SlackReply` solves this with a single-thread FIFO worker per reply. The `write` / `show_steps` / `seal` methods enqueue closures; the worker runs them in order. If `chat.startStream` is unavailable (older workspace), the worker falls back to `chat.postMessage` + `chat.update` for the rest of the reply lifetime. The fallback flag flips once and is sticky — a partial fallback (some appendStream succeeded, some didn't) would leave the message half-baked.

Editing this module:

- The worker thread is daemonic. Don't block on shutdown — `seal` enqueues the close, calls `_Worker.shutdown`, and lets the main thread move on.
- `_opened` is an `Event` so deltas enqueued before the message is created wait politely instead of racing.
- All Slack API calls run inside the worker, never on the Bolt thread.

## Concurrency model

| Concern | Where it's solved |
|---|---|
| Two inbounds on the same thread | `Coordinator._ThreadGate` |
| Streaming output ordering | `SlackReply` worker queue |
| Session map mutation | `ThreadStore` `RLock` |
| Anthropic SDK access | `anthropic.Anthropic` is thread-safe per docs |

There is no `asyncio` anywhere. We picked threads because Bolt's Socket Mode is already thread-based and the bot's per-message work is short. If we ever need to coordinate hundreds of concurrent threads, switching to asyncio is a large rewrite — at that point also re-examine whether the in-memory `ThreadStore` is still the right answer.
