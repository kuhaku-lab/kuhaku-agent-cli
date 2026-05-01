"""Normalize Managed Agents SSE events into a small typed union.

The SDK yields pydantic objects with many fields; downstream code only cares
about a handful of cases. ``parse_event`` extracts those cases and returns a
single ``Beat`` (musical metaphor — distinct from agentchannels' ``ParseResult``)
plus a flag indicating whether the stream is finished.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Optional, Union


# ---------------------------------------------------------------------------
# Public event union
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Say:
    """A piece of agent-authored prose to render to the user."""

    text: str


@dataclass(slots=True)
class Tool:
    """The agent invoked a tool (built-in or MCP).

    ``id`` and ``input`` are filled when the underlying SDK event carries them
    (``agent.tool_use``/``agent.mcp_tool_use``/``content_block_start``); the
    Coordinator looks them up by ``id`` when ``RequiresAction`` arrives.
    """

    name: str
    via_mcp: bool = False
    server: Optional[str] = None
    id: Optional[str] = None
    input: Optional[dict] = None


@dataclass(slots=True)
class Stage:
    """Lifecycle marker (running, idle, terminated…)."""

    label: str


@dataclass(slots=True)
class Hiccup:
    """A surfaced error from the session itself."""

    kind: str
    detail: str
    server: Optional[str] = None
    raw: Any = None


@dataclass(slots=True)
class Done:
    """The agent has nothing more to say (terminal)."""

    why: str = "idle"


@dataclass(slots=True)
class RequiresAction:
    """The agent paused awaiting human approval for one or more tool uses.

    ``event_ids`` lists the ``agent.tool_use`` / ``agent.mcp_tool_use`` event
    ids the session is blocked on. Resolve every one with
    ``Backend.confirm_tool_use(...)`` to resume the session. See
    ``.claude/skills/kuhaku-agent-dev/references/approval-flow.md``.
    """

    event_ids: tuple[str, ...]


Beat = Union[Say, Tool, Stage, Hiccup, Done, RequiresAction]
"""One discrete output extracted from the SSE stream."""


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ParsedFrame:
    beats: list[Beat] = field(default_factory=list)
    terminal: bool = False


def parse_event(event: Any) -> ParsedFrame:
    """Translate a raw SSE event from the Managed Agents stream.

    Accepts both the SDK's pydantic event objects and plain ``dict`` payloads
    (handy in tests). Unknown event types collapse to an empty frame.
    """
    etype = _attr(event, "type", "")
    if not etype:
        return ParsedFrame()

    # ---- incremental content streaming ----------------------------------
    # The SDK delivers most assistant text as content_block_delta with a
    # text_delta payload. Missing this is what made the second turn render
    # empty in plan-mode.
    if etype == "content_block_delta":
        delta = _attr(event, "delta", None)
        dtype = _attr(delta, "type", "")
        if dtype == "text_delta":
            t = _attr(delta, "text", "")
            if isinstance(t, str) and t:
                return ParsedFrame(beats=[Say(t)])
        # thinking_delta is not user-visible
        return ParsedFrame()

    if etype == "content_block_start":
        block = _attr(event, "content_block", None)
        if _attr(block, "type", "") == "tool_use":
            return ParsedFrame(
                beats=[
                    Tool(
                        name=_attr(block, "name", "tool"),
                        id=_attr(block, "id", None),
                        input=_attr(block, "input", None),
                    )
                ]
            )
        return ParsedFrame()

    if etype == "content_block_stop":
        return ParsedFrame()

    # ---- assistant prose (full-message fallback) ------------------------
    if etype == "agent.message":
        beats: list[Beat] = []
        for block in _attr(event, "content", []) or []:
            if _attr(block, "type", "") == "text":
                t = _attr(block, "text", "")
                if t:
                    beats.append(Say(t))
        return ParsedFrame(beats=beats)

    # ---- thinking is not user-visible ------------------------------------
    if etype == "agent.thinking":
        return ParsedFrame()

    # ---- tool invocations -------------------------------------------------
    if etype in {"agent.tool_use", "agent.custom_tool_use"}:
        return ParsedFrame(
            beats=[
                Tool(
                    name=_attr(event, "name", "tool"),
                    id=_attr(event, "id", None),
                    input=_attr(event, "input", None),
                )
            ]
        )
    if etype == "agent.mcp_tool_use":
        return ParsedFrame(
            beats=[
                Tool(
                    name=_attr(event, "name", "?"),
                    via_mcp=True,
                    server=_attr(event, "mcp_server_name", None),
                    id=_attr(event, "id", None),
                    input=_attr(event, "input", None),
                )
            ]
        )
    if etype in {"agent.tool_result", "agent.mcp_tool_result"}:
        # Tool result events are observability only — Coordinator already
        # marks the running tool task complete when the next Tool/Done arrives.
        return ParsedFrame()

    # ---- session lifecycle -----------------------------------------------
    # NOTE: idle MUST be checked before the prefix branch — agentchannels
    # treats idle as "this turn is done, close the stream". Without it, the
    # SSE loop never returns and the next turn's reply renders empty.
    if etype == "session.status_idle":
        stop_reason = _attr(event, "stop_reason", None)
        sr_type = _attr(stop_reason, "type", "end_turn")
        if sr_type == "requires_action":
            ids_raw = _attr(stop_reason, "event_ids", []) or []
            return ParsedFrame(
                beats=[RequiresAction(event_ids=tuple(ids_raw))],
                terminal=True,
            )
        if sr_type == "retries_exhausted":
            return ParsedFrame(
                beats=[
                    Hiccup(
                        kind="retries_exhausted",
                        detail=str(_attr(stop_reason, "reason", "retries exhausted")),
                        raw=stop_reason,
                    )
                ],
                terminal=True,
            )
        return ParsedFrame(beats=[Done(why=sr_type or "end_turn")], terminal=True)

    if etype.startswith("session.status_"):
        label = etype.removeprefix("session.status_")
        terminal = label == "terminated"
        return ParsedFrame(beats=[Stage(label=label)], terminal=terminal)

    if etype == "session.error":
        err = _attr(event, "error", None)
        kind = _attr(err, "type", "session_error") or "session_error"
        detail = _attr(err, "message", "") or "unknown error"
        server = _attr(err, "mcp_server_name", None)
        return ParsedFrame(
            beats=[Hiccup(kind=kind, detail=detail, server=server, raw=err)],
            terminal=True,
        )

    if etype == "session.deleted":
        return ParsedFrame(beats=[Done(why="deleted")], terminal=True)

    # everything else (span.*, message_*, etc.) is informational
    return ParsedFrame()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _attr(obj: Any, name: str, default: Any) -> Any:
    """Read an attribute from either a dataclass-like object or a dict."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def iter_beats(frames: Iterable[ParsedFrame]) -> Iterable[Beat]:
    """Flatten an iterable of frames into beats. Convenience for callers."""
    for frame in frames:
        yield from frame.beats
        if frame.terminal:
            return
