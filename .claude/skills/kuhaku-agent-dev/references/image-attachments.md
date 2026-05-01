# Image attachments (Slack file → Anthropic image block)

When a Slack user mentions the bot with an image attached, the bridge has to:

1. Detect the file in the `app_mention` event
2. Download the bytes from Slack (auth required)
3. Verify they're really an image (not an HTML auth-error page)
4. Forward as a base64 image block on the next `user.message` event

Failures along this path surface to the user as the unhelpful Anthropic error
`unknown_error: Could not process image`. This file documents what the code
does, why each step exists, and how to debug.

## End-to-end shape

```
Slack files API ──► SlackSurface._fetch_image_attachments
                     │  validates mime + downloads bytes + sniffs magic
                     ▼
                 Inbound.attachments: list[Attachment(mime, data)]
                     │
                     ▼
               Coordinator._stream
                     │  images = [(a.mime, a.data) for a in inbound.attachments]
                     ▼
            Backend.converse(..., images=images)
                     │  base64-encodes each image into an image content block
                     ▼
            client.beta.sessions.events.send({
              "type": "user.message",
              "content": [
                {"type": "text", "text": user_text},
                {"type": "image", "source": {
                    "type": "base64", "media_type": mime, "data": <b64>}},
                ...
              ]
            })
```

`Attachment` lives in `surfaces/base.py` as a 2-field dataclass — `mime` and
`data`. It's intentionally small: only image is supported today, and YAGNI
forbids speculative `kind`/`name` fields. Add them back when a real second
attachment type lands.

## Slack-side download (`surfaces/slack/surface.py`)

`_fetch_image_attachments(files)` is the only place that touches Slack file
URLs. Three guarantees it provides:

1. **Skips non-image files cheaply.** The `mimetype` field on the Slack file
   object is checked before any HTTP call.
2. **Prefers `url_private_download`** with a fallback to `url_private`. Some
   workspaces serve an HTML viewer page from `url_private`.
3. **Magic-byte sniffs after download.** `_sniff_image_mime` returns `None`
   for anything that isn't PNG / JPEG / GIF / WebP. Non-`None` is the
   _canonical_ mime sent to Anthropic; the Slack-declared mime is logged but
   not trusted.

If sniff returns `None` (typical when Slack returned an HTML auth-error
page), the file is dropped with an ERROR log line that names the missing
scope. This is by far the most common failure mode and the log is the
single best diagnostic — never let it become a silent debug-level skip.

### Required Slack scopes

The bot needs:

| Scope | Why |
|---|---|
| `files:read` | Authenticated `url_private` / `url_private_download` access — without it Slack returns HTML |
| `chat:write` | The reply itself |
| `app_mentions:read` | Receiving the `app_mention` event |
| `assistant:write` | Plan-mode streaming via `chat.startStream` (separate concern) |

Adding `files:read` requires **Reinstall to Workspace** and a fresh
`xoxb-...` in `.env`. This is operator action, not a code change.

### Hard caps

`_MAX_ATTACHMENT_BYTES = 20 MiB` is enforced twice:

1. Pre-download: `if size > cap: skip` using Slack's metadata `size` field
2. Post-download: `if len(data) > cap: skip` because Slack `size` may be
   absent or wrong

The post-download check is not redundant — it's the reliable backstop. Keep
both.

## Backend-side encoding (`backend.py`)

`Backend.converse(session_id, user_text, *, images=())` accepts a sequence
of `(mime: str, data: bytes)` tuples. The shape is intentionally primitive
so `backend.py` doesn't import from `surfaces`.

For each tuple, it appends:

```python
{
  "type": "image",
  "source": {
    "type": "base64",
    "media_type": mime,
    "data": base64.standard_b64encode(data).decode("ascii"),
  },
}
```

This matches `BetaManagedAgentsImageBlockParam` /
`BetaManagedAgentsBase64ImageSourceParam` in the SDK. Anthropic's vision
models (Sonnet 4.x+, Opus 4.x+) accept this content shape directly. The
agent's `model.id` in `agents/<name>.json` must support vision — Sonnet 3
and earlier do not.

## Diagnosing "Could not process image"

The Anthropic Hiccup `unknown_error: Could not process image` always means
one of:

| Symptom | Root cause | Fix |
|---|---|---|
| Sniff log says `Slack returned non-image bytes ... Content-Type='text/html'` | Missing `files:read` scope | Add scope, reinstall, update `xoxb-` token |
| Sniff log shows `attached image '...png' (image/png, ... bytes)` and Anthropic still 400s | Agent model lacks vision | Switch `model.id` to a vision-capable Sonnet/Opus 4.x |
| Image is valid but >20 MiB | Capped before send | Resize at the source, or implement chunking via Files API |
| Multiple large images | Base64 inline bloats request | Switch to Files API upload + `{"source":{"type":"file","file_id":...}}` |

Always start by reading the Surface log lines. If you see `attached image`
the bytes were valid — the problem is downstream (model, size, format).

## Why we don't use the Anthropic Files API yet

The current path is base64-inline, simple and synchronous. Files API
(`client.beta.files.upload(...)`) would:

- Avoid resending the same image across multi-turn interactions
- Reduce request payload sizes for large images
- Need an extra round-trip per image upload

For typical receipt-screenshot use cases (single image, ~50 KB), inline
base64 is fine. Switch to Files API if you observe:

- Repeated re-sending of the same image across turns becoming slow
- Hitting Anthropic request-size limits (~32 MB)
- Wanting to attach the same image to multiple sessions

## Adding a new attachment type (e.g. PDF)

When the time comes, the minimal extension is:

1. Generalize `Attachment` to carry a `kind` discriminator (`Literal["image", "document"]`)
2. Add document magic-byte sniffing alongside `_sniff_image_mime`
3. Add a `documents=...` parameter on `Backend.converse` (mirroring images)
4. Coordinator partitions `inbound.attachments` by kind and passes each list separately

Don't generalize ahead of time — the current 2-field dataclass is exactly
the abstraction the code needs today. See `SKILL.md` style guardrails.
