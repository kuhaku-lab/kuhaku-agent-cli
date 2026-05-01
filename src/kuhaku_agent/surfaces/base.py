"""Surface abstractions shared by all channel adapters.

Each concrete surface translates platform events into ``Inbound`` records and
produces ``Reply`` handles for streaming output back to the user.

Naming choices intentionally diverge from agentchannels' TypeScript types:

    Surface  — the place users interact (was: ChannelAdapter)
    Inbound  — one user-originated message     (was: ChannelMessage)
    Reply    — streaming output handle         (was: StreamHandle)
    Listener — message callback                (was: MessageHandler)
    Step     — plan/task indicator             (was: StreamTask)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional, Protocol


# ---------------------------------------------------------------------------
# Inbound message
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Attachment:
    """An image attached to an inbound message.

    The Surface downloads bytes (e.g. Slack's ``url_private`` requires auth
    headers); by the time it lands on ``Inbound`` it's raw bytes ready to be
    base64-encoded into an Anthropic Managed Agents image block.
    """

    mime: str
    data: bytes


@dataclass(slots=True)
class Inbound:
    """A normalized incoming message from any surface.

    Attributes
    ----------
    message_id:
        Surface-specific id (e.g. Slack's ``ts``).
    where:
        Channel / conversation identifier.
    thread:
        Stable thread identifier. Same thread → same Managed Agents session.
    sender:
        Surface user id.
    text:
        Cleaned message body (bot mention stripped).
    is_mention:
        ``True`` when this message explicitly @-mentioned the bot.
    is_dm:
        ``True`` for direct messages.
    attachments:
        Files the user attached alongside the message — currently images.
        The Surface downloads bytes before putting them here.
    raw:
        Original platform event for debug or advanced lookups.
    """

    message_id: str
    where: str
    thread: str
    sender: str
    text: str
    is_mention: bool = False
    is_dm: bool = False
    attachments: list[Attachment] = field(default_factory=list)
    raw: Any = None

    def thread_key(self, surface_name: str) -> str:
        """Compose a stable key used by ``ThreadStore``."""
        return f"{surface_name}::{self.where}::{self.thread}"


Listener = Callable[[Inbound], None]
"""Synchronous callback invoked once per inbound message."""


@dataclass(slots=True)
class ToolDecision:
    """Operator's decision on one paused tool use (see approval-flow.md)."""

    session_id: str
    tool_use_id: str
    result: Literal["allow", "deny"]
    deny_message: Optional[str] = None


ToolDecisionListener = Callable[[ToolDecision], None]
"""Callback the Coordinator registers to receive approval button clicks."""


# ---------------------------------------------------------------------------
# Reply (streaming output)
# ---------------------------------------------------------------------------


StepStatus = Literal["queued", "running", "done", "failed"]


@dataclass(slots=True)
class Step:
    """A plan/task indicator (e.g. tool invocation in progress)."""

    key: str
    label: str
    status: StepStatus = "queued"


class Reply(Protocol):
    """Streaming reply handle returned by ``Surface.open_reply()``.

    Lifecycle::

        reply = surface.open_reply(...)
        reply.write(delta)               # called many times
        reply.show_steps([...])          # optional plan view
        reply.seal(final_text)           # finalize once

    Implementations must be safe to call from any thread; the ``Coordinator``
    pushes deltas as soon as they arrive.
    """

    def write(self, delta: str) -> None: ...

    def show_steps(self, steps: list[Step]) -> None: ...

    def seal(
        self,
        final_text: Optional[str] = None,
        final_steps: Optional[list[Step]] = None,
    ) -> None: ...


# ---------------------------------------------------------------------------
# Surface base class
# ---------------------------------------------------------------------------


SurfaceState = Literal["idle", "starting", "running", "stopped", "errored"]


class Surface(ABC):
    """Abstract base class for messaging adapters.

    Concrete subclasses must set ``name`` to a short lowercase identifier
    (``"slack"``, ``"discord"``…) — it appears in thread keys and logs.
    """

    name: str = "abstract"

    state: SurfaceState = "idle"

    # -- lifecycle ----------------------------------------------------------
    @abstractmethod
    def start(self) -> None:
        """Open the platform connection and begin dispatching events."""

    @abstractmethod
    def stop(self) -> None:
        """Tear down the platform connection. Idempotent."""

    # -- event registration -------------------------------------------------
    @abstractmethod
    def listen(self, listener: Listener) -> None:
        """Register a listener invoked per inbound message."""

    # -- output -------------------------------------------------------------
    @abstractmethod
    def post(self, where: str, thread: str, text: str) -> None:
        """Send a complete (non-streaming) message."""

    @abstractmethod
    def open_reply(
        self, where: str, thread: str, sender: Optional[str] = None
    ) -> Reply:
        """Open a streaming reply and return a ``Reply`` handle."""

    # -- approval flow ------------------------------------------------------
    def listen_tool_decision(self, listener: ToolDecisionListener) -> None:
        """Register a callback invoked when the operator approves/denies a
        paused tool use. Default no-op for surfaces without an approval UI;
        Slack overrides this to wire a Bolt ``block_actions`` handler.
        """

    # -- optional UX helpers ------------------------------------------------
    def hint(self, where: str, thread: str, text: str) -> None:
        """Optional transient hint (e.g. typing indicator). No-op by default."""

    def clear_hint(self, where: str, thread: str) -> None:
        """Optional: clear the transient hint."""
