# Adding a new Surface

Step-by-step recipe for plumbing a second messaging surface (e.g. Discord) into the bridge. Read this end-to-end before writing code — the small choices that look optional usually aren't.

## 1. Pick a name

Lowercase, single word, alphanumeric: `discord`, `teams`, `cli`. The string lands in two places:

- `Surface.name` (used as the prefix in `ThreadStore` keys → never change it after a release)
- The directory under `src/kuhaku_agent/surfaces/<name>/`

## 2. Scaffold the package

```
src/kuhaku_agent/surfaces/<name>/
├── __init__.py            # re-export the concrete Surface class
├── surface.py             # the Surface implementation
├── streamer.py            # the Reply implementation
└── diagnostics.py         # optional: Hiccup → user-visible text
```

Mirror the Slack layout. Even if `diagnostics.py` is trivial, keep it as its own module so the swap point is consistent across surfaces.

## 3. Implement `Surface`

Subclass `kuhaku_agent.surfaces.base.Surface` and implement the five required methods:

| Method | Responsibility |
|---|---|
| `start` | Open the platform connection. Block if the platform expects it (Slack does); otherwise return after the listener is registered. |
| `stop` | Idempotent close. |
| `listen(listener)` | Append the listener to a list; invoke listeners on each inbound event. |
| `post(where, thread, text)` | One-shot message; used for error fallbacks. |
| `open_reply(where, thread, sender)` | Return a `Reply` that is safe to call from any thread. |

The optional `hint` / `clear_hint` are nice-to-haves; ignore them on first pass.

### What `Inbound` should look like for a chat platform

```python
Inbound(
    message_id=event_id,
    where=channel_or_dm_id,
    thread=thread_or_message_root_id,
    sender=user_id,
    text=cleaned_text,        # bot mentions stripped
    is_mention=...,
    is_dm=...,
    raw=event_object,
)
```

`thread` is the most important field — it determines session reuse. Pick whatever the platform uses to group a multi-turn conversation. If the platform has no native threading (Twitter DMs, SMS), use the conversation id and accept that every message in the conversation appends to the same Managed Agents session.

## 4. Implement `Reply`

The Slack version (`surfaces/slack/streamer.py`) is the canonical reference. The shape you must preserve:

- A single FIFO worker per reply. All platform API calls happen on that worker.
- `write(delta)`, `show_steps(steps)`, `seal(text=None)` are the only public entry points the Coordinator calls.
- `seal` shuts the worker down; subsequent calls must be no-ops, not errors.

If the platform has native streaming (Slack `chat.startStream`, Discord token-by-token edits), use it. Otherwise: `post` a placeholder, then `edit` it on each delta with the accumulated text. The Slack module shows the pattern with a sticky fallback flag — copy that.

## 5. Wire into `runner.py`

`runner.build_runtime` constructs the runtime graph. To support multiple surfaces:

1. Add a `surface: str = "slack"` parameter to `Settings` (and a CLI flag in `cli.serve`).
2. Branch in `build_runtime` based on the value, instantiating the right `Surface` subclass with the right config.
3. The `Coordinator` and `ThreadStore` are surface-agnostic — leave them alone.

Until you have a second surface, keep the Slack default hardcoded so onboarding stays simple.

## 6. Diagnostics

The `Hiccup → str` formatter is per-surface because rendering rules differ (Slack uses mrkdwn + emoji shortcodes; Discord uses different markdown; CLI is plain text). The `Coordinator` accepts a `diagnose` callback in its constructor, so injecting your formatter is a one-liner:

```python
Coordinator(..., diagnose=my_diagnoser)
```

Always handle at least:

- `mcp_connection_failed_error` → tell the user which `mcp_server_name` failed and where to fix it (always: Anthropic Console).
- Generic fallback (`f"[{kind}] {detail}"`).

## 7. Things to avoid

- **Don't** import Bolt or Slack-specific types from your new surface. Keep platform deps inside the surface package.
- **Don't** reach into `Coordinator` internals. If the new surface needs different orchestration, fix `Coordinator` so all surfaces benefit.
- **Don't** make `Reply` async. Sync-with-worker is the model.
- **Don't** add per-user vault selection in the surface — that's a `Coordinator` (or above) concern.

## 8. Smoke test

Add a minimal manual test under `tests/surfaces/<name>/` that constructs the `Surface`, mocks the platform client, and asserts that an inbound triggers the registered listener with the expected `Inbound` shape. Real end-to-end tests come from running the bot.
