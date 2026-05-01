# Tool approval flow (`requires_action`)

When an MCP toolset or agent tool is configured with `permission_policy.type = "always_ask"`, the agent will pause before invoking the tool and wait for a human decision. The bridge has to surface that pause to the user, collect the answer, and feed it back to the SDK. This file is the authoritative reference for that round-trip.

## SDK shapes

These are the only types involved. Read them from the SDK if anything below looks stale:

- `anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event.BetaManagedAgentsSessionStatusIdleEvent`
- `anthropic.types.beta.sessions.beta_managed_agents_session_requires_action.BetaManagedAgentsSessionRequiresAction`
- `anthropic.types.beta.sessions.beta_managed_agents_agent_tool_use_event.BetaManagedAgentsAgentToolUseEvent`
- `anthropic.types.beta.sessions.beta_managed_agents_user_tool_confirmation_event_params.BetaManagedAgentsUserToolConfirmationEventParams`

### The pause signal

`session.status_idle` is no longer a single "turn ended" marker — it's a discriminated union over `stop_reason.type`:

| `stop_reason.type` | Meaning | Bridge action |
|---|---|---|
| `end_turn` | Agent finished this turn naturally | Seal the reply; stream is done |
| `requires_action` | Agent paused awaiting human decision (tool confirmation) | **Do not seal.** Render approval UI, send `user.tool_confirmation` |
| `retries_exhausted` | Agent gave up after retries | Surface as `Hiccup`, seal |

`requires_action.event_ids[]` lists the tool-use events the agent is blocked on. Each id matches the `id` of a previously-emitted `agent.tool_use` or `agent.mcp_tool_use` event — that's where the tool name and input live. **Resolving fewer than all event_ids re-emits `session.status_idle` with the remainder**, so a sloppy partial-confirm just loops.

### The decision payload

To resume, send a `user.tool_confirmation` event per pending tool_use_id:

```python
client.beta.sessions.events.send(
    session_id,
    events=[{
        "type": "user.tool_confirmation",
        "tool_use_id": "<id from agent.tool_use>",
        "result": "allow",            # or "deny"
        "deny_message": "...",        # only when result == "deny"
    }],
)
```

You can batch multiple confirmations in one `events=[...]` call. After the last one, the session transitions back to `running` and emits the resumed agent output on the **same** SSE stream — until you closed it. See "Stream lifetime" below.

## Bridge design

### Beat union extension

Add a new beat alongside the existing five:

```python
Beat = Say | Tool | Stage | Hiccup | Done | RequiresAction

@dataclass(slots=True)
class RequiresAction:
    event_ids: tuple[str, ...]  # agent.tool_use ids the session is blocked on
```

`parse_event` for `session.status_idle`:

| `stop_reason.type` | Beat | terminal |
|---|---|---|
| `end_turn` | `Done(why="end_turn")` | `True` |
| `requires_action` | `RequiresAction(event_ids=...)` | `True` (close the SSE; we'll reopen after confirmations land) |
| `retries_exhausted` | `Hiccup(kind="retries_exhausted", ...)` | `True` |

We close the SSE stream on `requires_action` even though the SDK *can* keep it open. Two reasons:

1. Humans can take hours to click. An SSE connection held open across that span is fragile (proxies time out, the worker thread can't service other requests).
2. Reopening with a fresh `client.beta.sessions.events.stream(session_id)` after `events.send(user.tool_confirmation, ...)` cleanly delivers all resumed events. The SDK does not require the original stream to be alive.

To enrich the approval UI we also need the tool name + input. The Coordinator already accumulates `agent.tool_use` events as `Tool` beats during streaming; remember the most recent ones keyed by their `id`, then look them up by the `event_ids` from `RequiresAction`. **Do not** add a new SDK call to fetch tool inputs — they are already in the stream.

> Implementation note: extend `Tool` with the SDK event's `id` and `input`, or add a side-table `pending_tool_uses: dict[str, Tool]` in `_RunState`. The side-table is less invasive.

### Coordinator behaviour

When `_stream` sees `RequiresAction`:

1. **Do not** call `reply.seal`. The reply must stay live.
2. Render the approval UI via a new `reply.ask_confirmation(tool_uses, on_decision)` method (Slack Block Kit buttons). The reply tracks the pending tool_use_ids internally.
3. Return from `_stream` with a new sentinel (e.g., `_StreamOutcome.PAUSED`) so `handle` knows not to call `seal` or `release`.
4. Persist the pause state in a process-local map: `pending: dict[session_id, _PendingApproval]` where `_PendingApproval` holds the `Reply`, the `Inbound`, and the unresolved `tool_use_ids`.

When the Slack `block_actions` handler fires:

1. Look up the `_PendingApproval` by `session_id` (encoded in the button's `value`).
2. Call `Backend.confirm_tool_use(session_id, tool_use_id, result, deny_message=None)`.
3. Reduce the pending set. If empty, **resume**: open a fresh `Backend.converse_resume(session_id)` (a new helper that opens the SSE without sending a `user.message`) and pump events into the same `Reply`.

The per-thread gate must remain held during the pause so a fresh inbound on the same thread doesn't race the resumption. Keep `_ThreadGate.acquire(key)` outside the `_stream` boundary; release it only when the resumed run finally seals.

### Backend additions

Two new methods on `Backend`:

```python
def confirm_tool_use(
    self,
    session_id: str,
    *,
    tool_use_id: str,
    result: Literal["allow", "deny"],
    deny_message: Optional[str] = None,
) -> None:
    payload = {"type": "user.tool_confirmation", "tool_use_id": tool_use_id, "result": result}
    if result == "deny" and deny_message:
        payload["deny_message"] = deny_message
    self._client.beta.sessions.events.send(session_id, events=[payload])

@contextmanager
def converse_resume(self, session_id: str) -> Iterator[Iterator[ParsedFrame]]:
    """Open a stream on an already-running session without sending user.message."""
    stream_ctx = self._client.beta.sessions.events.stream(session_id)
    stream = stream_ctx.__enter__()
    try:
        yield (parse_event(raw) for raw in stream)
    finally:
        stream_ctx.__exit__(None, None, None)
```

`converse_resume` is just `converse` minus the `events.send(user.message)`. Resist the temptation to merge them with a flag — clarity at the call site beats DRY here.

### Slack-side UI

`SlackReply.ask_confirmation(tool_uses, session_id)` sends a `chat.postMessage` (in the same thread) with Block Kit:

- Section block: "🔐 ツール実行の承認が必要です: `<tool_name>` — input preview"
- Actions block with two buttons: `承認` (style: `primary`) and `拒否` (style: `danger`).
- Each button's `value` encodes `f"{session_id}:{tool_use_id}:{decision}"`.
- `action_id`: `"kuhaku.tool_confirm"` so the surface can route it.

`SlackSurface` registers a Bolt `action("kuhaku.tool_confirm")` handler that:

1. Decodes the `value`.
2. Calls a new `Surface.on_tool_decision(session_id, tool_use_id, decision)` listener that the `Coordinator` registered at startup.
3. ACKs the action quickly (Bolt requires <3s) and updates the message to "✅ 承認済み" / "🚫 拒否済み".

### Threading

| Step | Where it runs |
|---|---|
| `_stream` sees `RequiresAction`, posts approval UI | Bolt event-handler thread (initial inbound) |
| Operator clicks button | Slack worker thread / Bolt action handler |
| `Backend.confirm_tool_use` → `Backend.converse_resume` → resumed `_stream` loop | A **new daemon thread** spawned by the action handler, *not* the Bolt thread |

Don't do the resumption work on the Bolt action thread — Bolt requires a fast ack and we may stream for a while.

## Failure modes

- **Operator never clicks**: the pending entry leaks. Add an idle TTL to the `pending` map (default 1h) that auto-denies with a deny_message and logs.
- **Operator clicks twice**: the second click should look up by `session_id+tool_use_id`, find the entry already resolved, ack the Slack action, and exit. Idempotent.
- **Stream errors during resume**: surface via the existing `Hiccup` path. The user-facing message lives in `slack_diagnoser` — extend it if a new kind shows up.
- **Coordinator restart while paused**: the pending map is in-memory and dies. The Slack message still has the buttons but clicking them returns "no longer awaiting" because the `pending` lookup misses. This is acceptable; persisting confirmation state is a non-goal for now.

## Quick path for "I just want it to run"

If you don't need an approval UX (dev / staging / a tool that's safe to auto-allow):

1. Edit `agents/<name>.json`.
2. Change the offending toolset's `default_config.permission_policy.type` from `always_ask` to `always_allow`.
3. `uv run kuhaku-agent init agent --from-file agents/<name>.json` to upsert.

This bypasses everything in this document. Do this in dev; do **not** do it for tools that mutate user-visible state in production (Canvas creation, message sending, file deletion).
