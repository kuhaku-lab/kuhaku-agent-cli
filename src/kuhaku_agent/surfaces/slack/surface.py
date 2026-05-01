"""``SlackSurface`` — Bolt-for-Python Socket Mode adapter."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import requests

from ..base import (
    Attachment,
    Inbound,
    Listener,
    Reply,
    Surface,
    ToolDecision,
    ToolDecisionListener,
)
from .streamer import SlackReply


_MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
"""Cap per-image download size; Anthropic's image input limits are around
this bracket and oversized files just bloat the request anyway."""


def _sniff_image_mime(data: bytes) -> Optional[str]:
    """Return the canonical image MIME from magic bytes, or ``None`` if the
    payload is not one of Anthropic's supported formats. Used to catch
    Slack returning HTML-instead-of-image (the typical failure mode when
    the bot lacks the ``files:read`` scope)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


TOOL_CONFIRM_ACTION_PREFIX = "kuhaku.tool_confirm"
"""action_id prefix for approval buttons. Slack forbids duplicate action_ids
within one message, so each button uses ``<prefix>:<tool_use_id>:<decision>``.
The Bolt handler matches the prefix via regex."""

_TOOL_CONFIRM_ACTION_RE = re.compile(rf"^{re.escape(TOOL_CONFIRM_ACTION_PREFIX)}:")

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SlackSurfaceConfig:
    bot_token: str
    app_token: str
    char_limit: int = 39_000


class SlackSurface(Surface):
    """Slack adapter using Bolt for Python in Socket Mode."""

    name = "slack"

    def __init__(self, config: SlackSurfaceConfig) -> None:
        self._config = config
        self._app = App(token=config.bot_token)
        self._handler: Optional[SocketModeHandler] = None
        self._listeners: list[Listener] = []
        self._tool_decision_listeners: list[ToolDecisionListener] = []
        self._self_user_id: Optional[str] = None
        self._mention_pattern: Optional[re.Pattern[str]] = None

        self._app.event("app_mention")(self._on_mention)
        self._app.action(_TOOL_CONFIRM_ACTION_RE)(self._on_tool_confirm)

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self.state == "running":
            return
        self.state = "starting"
        identity = self._app.client.auth_test()
        self._self_user_id = identity["user_id"]
        self._mention_pattern = re.compile(rf"<@{re.escape(self._self_user_id)}>\s*")
        log.info(
            "SlackSurface authenticated as user=%s (%s)",
            identity.get("user"),
            self._self_user_id,
        )
        self._handler = SocketModeHandler(self._app, self._config.app_token)
        self.state = "running"
        # Blocking call — owner of the surface is expected to run this in a
        # thread or as the program's main loop.
        try:
            self._handler.start()
        finally:
            self.state = "stopped"

    def stop(self) -> None:
        if self._handler is not None:
            try:
                self._handler.close()
            except Exception:  # noqa: BLE001
                log.debug("SocketModeHandler close failed", exc_info=True)
        self.state = "stopped"

    # ---------------------------------------------------------- registration
    def listen(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def listen_tool_decision(self, listener: ToolDecisionListener) -> None:
        self._tool_decision_listeners.append(listener)

    # ------------------------------------------------------------ output
    def post(self, where: str, thread: str, text: str) -> None:
        self._app.client.chat_postMessage(channel=where, thread_ts=thread, text=text)

    def open_reply(
        self, where: str, thread: str, sender: Optional[str] = None
    ) -> Reply:
        return SlackReply(
            self._app.client, where, thread, char_limit=self._config.char_limit
        )

    # ----------------------------------------------------- transient notices
    def post_busy_notice(
        self, where: str, thread: str, label: str, *, hold_seconds: float = 2.5
    ) -> None:
        """Show a plan-mode notice with a spinner that resolves after a beat.

        Used when the per-thread gate rejects a concurrent inbound: we want
        the rejection to feel like the screenshot — pulldown container with a
        spinning task — instead of a flat ``chat.postMessage`` line.
        """
        reply = SlackReply(
            self._app.client,
            where,
            thread,
            char_limit=self._config.char_limit,
            init_label=label,
        )
        reply.begin_thinking()

        def _close_later() -> None:
            time.sleep(hold_seconds)
            try:
                reply.seal(None)
            except Exception:  # noqa: BLE001
                log.debug("busy-notice seal failed", exc_info=True)

        threading.Thread(
            target=_close_later, name="slack-busy-notice", daemon=True
        ).start()

    # ----------------------------------------------------- approval flow
    def post_approval(
        self,
        where: str,
        thread: str,
        session_id: str,
        tool_uses: list[dict],
    ) -> None:
        """Post a Block Kit message with Approve/Deny buttons per pending tool.

        ``tool_uses`` items must carry ``tool_use_id``, ``name``, and may
        include ``input`` (rendered as a JSON preview) and ``server`` for MCP.
        """
        blocks: list[dict] = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": ":lock: *ツール実行の承認が必要です*",
                },
            }
        ]
        for t in tool_uses:
            tool_use_id = t["tool_use_id"]
            name = t.get("name", "tool")
            server = t.get("server")
            label = f"`{name}`" + (f" via *{server}*" if server else "")
            preview = ""
            if t.get("input"):
                try:
                    raw = json.dumps(t["input"], ensure_ascii=False, indent=2)
                except Exception:  # noqa: BLE001
                    raw = str(t["input"])
                preview = "\n```" + raw[:1500] + "```"
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"{label}{preview}"},
                }
            )
            blocks.append(
                {
                    "type": "actions",
                    "block_id": f"kuhaku_confirm_{tool_use_id}",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": f"{TOOL_CONFIRM_ACTION_PREFIX}:{tool_use_id}:allow",
                            "text": {"type": "plain_text", "text": "承認"},
                            "style": "primary",
                            "value": json.dumps(
                                {
                                    "session_id": session_id,
                                    "tool_use_id": tool_use_id,
                                    "decision": "allow",
                                }
                            ),
                        },
                        {
                            "type": "button",
                            "action_id": f"{TOOL_CONFIRM_ACTION_PREFIX}:{tool_use_id}:deny",
                            "text": {"type": "plain_text", "text": "拒否"},
                            "style": "danger",
                            "value": json.dumps(
                                {
                                    "session_id": session_id,
                                    "tool_use_id": tool_use_id,
                                    "decision": "deny",
                                }
                            ),
                        },
                    ],
                }
            )
        try:
            self._app.client.chat_postMessage(
                channel=where,
                thread_ts=thread,
                text=":lock: ツール実行の承認が必要です",
                blocks=blocks,
            )
        except Exception:  # noqa: BLE001
            log.exception("post_approval failed")

    def _on_tool_confirm(self, ack, body, action, client) -> None:
        ack()
        try:
            payload = json.loads(action.get("value", "{}"))
        except Exception:  # noqa: BLE001
            log.exception("tool_confirm: malformed action value")
            return
        session_id = payload.get("session_id")
        tool_use_id = payload.get("tool_use_id")
        decision = payload.get("decision")
        if not (session_id and tool_use_id and decision in {"allow", "deny"}):
            log.warning("tool_confirm: incomplete payload %r", payload)
            return

        decision_obj = ToolDecision(
            session_id=session_id,
            tool_use_id=tool_use_id,
            result=decision,  # type: ignore[arg-type]
        )

        # Mark the message so the operator sees the click landed.
        try:
            channel = body.get("channel", {}).get("id")
            ts = body.get("message", {}).get("ts")
            user = (body.get("user") or {}).get("id", "?")
            mark = "✅ 承認" if decision == "allow" else "🚫 拒否"
            if channel and ts:
                client.chat_update(
                    channel=channel,
                    ts=ts,
                    text=f"{mark} 済み（<@{user}> による）",
                    blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"{mark} 済み（<@{user}> による）\n`{tool_use_id}`",
                            },
                        }
                    ],
                )
        except Exception:  # noqa: BLE001
            log.debug("tool_confirm: chat_update failed", exc_info=True)

        for listener in list(self._tool_decision_listeners):
            try:
                listener(decision_obj)
            except Exception:  # noqa: BLE001
                log.exception("tool_decision listener raised")

    # -------------------------------------------------- transient hint UX
    def hint(self, where: str, thread: str, text: str) -> None:
        """Show a "thinking…" indicator using ``assistant.threads.setStatus``.

        Best-effort — workspaces without the assistant API will silently fail
        and the SlackReply's plan-mode (or fallback animator) still gives the
        user feedback.
        """
        try:
            self._app.client.assistant_threads_setStatus(
                channel_id=where, thread_ts=thread, status=text
            )
        except Exception:  # noqa: BLE001
            log.debug("assistant_threads_setStatus failed", exc_info=True)

    def clear_hint(self, where: str, thread: str) -> None:
        self.hint(where, thread, "")

    # -------------------------------------------------------------- handler
    def _on_mention(self, event, client) -> None:
        # Skip bot's own messages and other bots
        if self._self_user_id and event.get("user") == self._self_user_id:
            return
        if event.get("bot_id"):
            return

        raw_text: str = event.get("text", "")
        cleaned = self._strip_mention(raw_text)

        attachments = self._fetch_image_attachments(event.get("files") or [])

        inbound = Inbound(
            message_id=event["ts"],
            where=event["channel"],
            thread=event.get("thread_ts") or event["ts"],
            sender=event.get("user", "unknown"),
            text=cleaned,
            is_mention=True,
            is_dm=event.get("channel_type") == "im",
            attachments=attachments,
            raw=event,
        )
        for listener in list(self._listeners):
            try:
                listener(inbound)
            except Exception:  # noqa: BLE001
                log.exception("listener raised")

    def _strip_mention(self, text: str) -> str:
        if not self._mention_pattern:
            return text.strip()
        return self._mention_pattern.sub("", text).strip()

    def _fetch_image_attachments(self, files: list[dict]) -> list[Attachment]:
        """Download image files from Slack and return them as ``Attachment``s.

        Slack's ``url_private`` / ``url_private_download`` URLs require a
        Bearer token; without ``files:read`` scope the API returns an HTML
        login page instead of the image. We sniff magic bytes after download
        to catch that case before forwarding garbage to Anthropic (which
        responds with the unhelpful "Could not process image").
        """
        out: list[Attachment] = []
        for f in files:
            mime = (f.get("mimetype") or "").lower()
            # Prefer the dedicated download URL — some workspaces serve an
            # HTML viewer page from `url_private` instead of binary content.
            url = f.get("url_private_download") or f.get("url_private")
            name = f.get("name") or f.get("title") or ""
            if not mime.startswith("image/") or not url:
                log.debug("skipping non-image attachment: name=%r mime=%r", name, mime)
                continue
            size = f.get("size")
            if isinstance(size, int) and size > _MAX_ATTACHMENT_BYTES:
                log.warning(
                    "skipping oversized attachment %r (%d bytes > %d cap)",
                    name, size, _MAX_ATTACHMENT_BYTES,
                )
                continue
            try:
                resp = requests.get(
                    url,
                    headers={"Authorization": f"Bearer {self._config.bot_token}"},
                    timeout=15,
                    allow_redirects=True,
                )
                resp.raise_for_status()
            except Exception:  # noqa: BLE001
                log.exception("failed to download Slack file %r", name)
                continue
            data = resp.content
            if len(data) > _MAX_ATTACHMENT_BYTES:
                log.warning(
                    "downloaded attachment %r exceeded cap (%d > %d), skipping",
                    name, len(data), _MAX_ATTACHMENT_BYTES,
                )
                continue
            sniffed = _sniff_image_mime(data)
            if sniffed is None:
                # Not a recognized image — almost always Slack returning an
                # auth-error HTML page. Surface a clear log line so the
                # operator can grant `files:read` instead of debugging
                # "Could not process image" from the agent.
                log.error(
                    "Slack returned non-image bytes for %r (Content-Type=%r, "
                    "first 16 bytes=%s). Likely missing `files:read` scope or "
                    "the bot is not in the channel where the file lives.",
                    name,
                    resp.headers.get("Content-Type"),
                    data[:16].hex(),
                )
                continue
            if sniffed != mime:
                log.info(
                    "attachment %r: declared mime=%s but sniffed=%s (using sniffed)",
                    name, mime, sniffed,
                )
            out.append(Attachment(mime=sniffed, data=data))
            log.info("attached image %r (%s, %d bytes)", name, sniffed, len(data))
        return out
