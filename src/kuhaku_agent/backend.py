"""Wrapper around the Anthropic Managed Agents SDK.

Encapsulates everything the bridge needs: opening sessions, sending the
``user.message`` event, iterating the SSE stream, listing existing vaults so
the operator can pick one, and surfacing failures as plain Python exceptions.
"""
from __future__ import annotations

import base64
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional, Sequence

import anthropic

from .events import ParsedFrame, parse_event


class StaleSessionError(RuntimeError):
    """The cached session_id no longer accepts events (archived / deleted).

    Raised by ``converse`` so the Coordinator can drop the ``ThreadStore``
    entry and retry with a fresh session for the same thread.
    """

    def __init__(self, session_id: str, original: Exception) -> None:
        self.session_id = session_id
        self.original = original
        super().__init__(f"session {session_id} is archived/gone: {original}")

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BackendBindings:
    """Identifiers required to open a session."""

    agent_id: str
    environment_id: str
    vault_ids: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class Backend:
    """Stateless-ish helper around ``anthropic.Anthropic.beta`` resources."""

    def __init__(self, api_key: str, *, bindings: Optional[BackendBindings] = None) -> None:
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is required to construct Backend")
        self._client = anthropic.Anthropic(api_key=api_key)
        self._bindings = bindings

    # ------------------------------------------------------------------ misc
    @property
    def raw(self) -> anthropic.Anthropic:
        """Escape hatch — direct access to the underlying SDK client."""
        return self._client

    def ping(self) -> None:
        """Cheap call to confirm the API key is valid."""
        self._client.beta.agents.list(limit=1)

    # ------------------------------------------------------------- bootstrap
    def create_agent(self, *, name: str, model: str, system: str) -> str:
        """Create a Managed Agent (minimal form) and return its id."""
        agent = self._client.beta.agents.create(name=name, model=model, system=system)
        log.info("created agent: id=%s name=%r", agent.id, name)
        return agent.id

    def create_agent_from_spec(self, spec: dict) -> str:
        """Create a Managed Agent from a full spec dict and return its id.

        The dict shape mirrors the SDK's ``agents.create`` arguments:
        ``name``, ``description``, ``model``, ``system``, ``tools``,
        ``mcp_servers``, ``skills``, ``metadata``. Unknown keys are passed
        through so future SDK fields work without changes here.
        """
        agent = self._client.beta.agents.create(**spec)
        log.info("created agent from spec: id=%s name=%r", agent.id, spec.get("name"))
        return agent.id

    def create_environment(self, *, name: str, config: dict) -> str:
        """Create an Environment with the given ``config`` dict and return its id."""
        env = self._client.beta.environments.create(name=name, config=config)
        log.info("created environment: id=%s name=%r", env.id, name)
        return env.id

    # ------------------------------------------------------------- sessions
    def open_thread(
        self,
        *,
        title: Optional[str] = None,
        bindings: Optional[BackendBindings] = None,
    ) -> str:
        """Create a new Managed Agents session and return its id."""
        b = bindings or self._bindings
        if b is None:
            raise RuntimeError("Backend has no bindings configured")

        kwargs: dict = {
            "agent": b.agent_id,
            "environment_id": b.environment_id,
        }
        if b.vault_ids:
            kwargs["vault_ids"] = list(b.vault_ids)
        if title:
            kwargs["title"] = title

        session = self._client.beta.sessions.create(**kwargs)
        log.info("opened session: id=%s title=%r", session.id, title)
        return session.id

    def confirm_tool_use(
        self,
        session_id: str,
        *,
        tool_use_id: str,
        result: str,
        deny_message: Optional[str] = None,
    ) -> None:
        """Send a ``user.tool_confirmation`` event to resolve one paused tool.

        ``result`` must be ``"allow"`` or ``"deny"``. ``deny_message`` is only
        meaningful for deny. The session resumes once every event id from the
        previous ``requires_action`` has been resolved.
        """
        if result not in {"allow", "deny"}:
            raise ValueError(f"result must be 'allow' or 'deny', got {result!r}")
        payload: dict = {
            "type": "user.tool_confirmation",
            "tool_use_id": tool_use_id,
            "result": result,
        }
        if result == "deny" and deny_message:
            payload["deny_message"] = deny_message
        try:
            self._client.beta.sessions.events.send(session_id, events=[payload])
        except anthropic.BadRequestError as exc:
            if _is_stale_session_error(exc):
                raise StaleSessionError(session_id, exc) from exc
            raise

    @contextmanager
    def converse_resume(self, session_id: str) -> Iterator[Iterator[ParsedFrame]]:
        """Open a fresh SSE stream on an already-running session.

        Use this after ``confirm_tool_use`` for every paused tool: the agent
        resumes server-side and emits its remaining output. Unlike
        ``converse``, no ``user.message`` is sent — we are reading the tail of
        the previous turn, not starting a new one.
        """
        try:
            stream_ctx = self._client.beta.sessions.events.stream(session_id)
        except anthropic.BadRequestError as exc:
            if _is_stale_session_error(exc):
                raise StaleSessionError(session_id, exc) from exc
            raise
        stream = stream_ctx.__enter__()
        try:
            def _frames() -> Iterator[ParsedFrame]:
                for raw in stream:
                    frame = parse_event(raw)
                    yield frame
                    if frame.terminal:
                        return

            yield _frames()
        finally:
            stream_ctx.__exit__(None, None, None)

    @contextmanager
    def converse(
        self,
        session_id: str,
        user_text: str,
        *,
        images: Sequence[tuple[str, bytes]] = (),
    ) -> Iterator[Iterator[ParsedFrame]]:
        """Send the user message and yield an iterator of parsed frames.

        Usage::

            with backend.converse(sid, "hello") as frames:
                for frame in frames:
                    for beat in frame.beats:
                        ...

        ``images`` is an optional sequence of ``(mime_type, bytes)`` tuples;
        each one is base64-encoded and appended to the ``user.message`` as an
        image block, so the agent can see attachments alongside the prompt.

        Wrapping the SDK's two-step protocol (``stream`` opened → ``send``)
        in a context manager keeps callers from forgetting to close the
        underlying SSE connection.
        """
        content: list[dict] = [{"type": "text", "text": user_text}]
        for mime, data in images:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime,
                        "data": base64.standard_b64encode(data).decode("ascii"),
                    },
                }
            )
        stream_ctx = self._client.beta.sessions.events.stream(session_id)
        stream = stream_ctx.__enter__()
        try:
            try:
                self._client.beta.sessions.events.send(
                    session_id,
                    events=[{"type": "user.message", "content": content}],
                )
            except anthropic.BadRequestError as exc:
                if _is_stale_session_error(exc):
                    raise StaleSessionError(session_id, exc) from exc
                raise

            def _frames() -> Iterator[ParsedFrame]:
                for raw in stream:
                    frame = parse_event(raw)
                    yield frame
                    if frame.terminal:
                        return

            yield _frames()
        finally:
            stream_ctx.__exit__(None, None, None)

    # ----------------------------------------------------------------- vault
    def list_vaults(self, limit: int = 20) -> list[dict]:
        """Return ``[{"id":..., "name":..., "credentials":[...]}, ...]``."""
        out: list[dict] = []
        for v in self._client.beta.vaults.list(limit=limit):
            creds = []
            try:
                for c in self._client.beta.vaults.credentials.list(vault_id=v.id):
                    creds.append(
                        {
                            "id": c.id,
                            "display_name": getattr(c, "display_name", ""),
                            "type": getattr(getattr(c, "auth", None), "type", "?"),
                            "status": getattr(c, "status", "?"),
                        }
                    )
            except Exception:  # noqa: BLE001
                log.warning("failed to list credentials for vault %s", v.id, exc_info=True)
            out.append(
                {
                    "id": v.id,
                    "name": getattr(v, "display_name", "") or getattr(v, "name", ""),
                    "credentials": creds,
                }
            )
        return out

    # --------------------------------------------------------- session files
    def session_outputs(self, session_id: str) -> list:
        """List files scoped to ``session_id``.

        Uses the SDK's ``scope_id`` query param. The deployed API may not
        accept it yet (returns 400 ``unknown field``); when that happens we
        log a single warning line and return ``[]`` so the run completes
        normally — outputs just don't get attached.
        """
        try:
            return list(self._client.beta.files.list(scope_id=session_id))
        except anthropic.BadRequestError as exc:
            log.warning(
                "session_outputs: API rejected scope_id (skipping outputs for %s): %s",
                session_id,
                exc,
            )
            return []
        except Exception:  # noqa: BLE001
            log.exception("session_outputs: list failed for %s", session_id)
            return []

    def download_session_file(self, session_id: str, file_id: str) -> bytes:
        """Fetch the bytes of a session output. ``session_id`` is unused now
        but kept in the signature for callers that pass it positionally."""
        del session_id  # SDK download is keyed only by file_id
        resp = self._client.beta.files.download(file_id)
        return resp.read()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_STALE_SESSION_MARKERS = (
    "archived session",
    "session not found",
    "deleted session",
    "no such session",
)


def _is_stale_session_error(exc: anthropic.BadRequestError) -> bool:
    """Decide whether a 400 from Anthropic means the cached session is gone.

    The error message text is the only signal Anthropic exposes today, so we
    match on a short list of known phrasings. Update if Anthropic changes
    the wording.
    """
    msg = str(exc).lower()
    return any(marker in msg for marker in _STALE_SESSION_MARKERS)
