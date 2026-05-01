"""Coordinator — orchestrates the full lifecycle of one inbound message.

Phases (intentionally renamed vs agentchannels):

    resolve  — find or create the Managed Agents session for this thread
    open     — ask the surface for a streaming Reply handle
    stream   — pump beats from the SSE stream into the Reply
    seal     — finalize the Reply with the full text
    release  — post-flight (file uploads, logs)

A single thread runs at most one Coordinator at a time; concurrent inbounds
on the same thread are dropped with a hint message so the user sees feedback
instead of silent contention.

Tool approval flow:
    On ``RequiresAction``, ``_pump`` returns ``"paused"`` instead of
    ``"done"``. The gate is intentionally held, the reply is not sealed, and
    a ``_PendingApproval`` entry is parked in ``_pending`` keyed by
    ``session_id``. When the surface delivers a ``ToolDecision``, the
    coordinator sends ``user.tool_confirmation`` and — once every blocked
    ``tool_use_id`` is resolved — spawns a daemon thread that re-opens the
    SSE via ``Backend.converse_resume`` and pumps the resumed beats into the
    same reply. See ``.claude/skills/kuhaku-agent-dev/references/approval-flow.md``.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Literal, Optional

from .backend import Backend, StaleSessionError
from .events import Done, Hiccup, RequiresAction, Say, Stage, Tool
from .surfaces.base import Inbound, Reply, Step, Surface, ToolDecision
from .thread_store import ThreadStore
from .tool_labels import describe_tool

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


_PumpOutcome = Literal["done", "paused", "errored"]


@dataclass(slots=True)
class CoordinatorConfig:
    """Tweakables that don't change at runtime."""

    text_limit: int = 39_000
    """Cap final reply size (Slack: 40k char limit, leave a margin)."""

    busy_hint: str = "_Thinking ⠋_"
    """Initial placeholder shown while the first beat hasn't arrived. Used
    only when the surface's ``Reply`` doesn't expose ``begin_thinking``."""

    upload_outputs: bool = True
    """Whether to attach files written under /mnt/session/outputs/."""


# ---------------------------------------------------------------------------
# Diagnostic formatter (open hook so callers can override per surface)
# ---------------------------------------------------------------------------


Diagnoser = Callable[[Hiccup], str]
"""``Hiccup`` → user-visible text. See ``surfaces.slack.diagnostics``."""


def _default_diagnoser(h: Hiccup) -> str:
    extra = f" (server={h.server})" if h.server else ""
    return f":x: エージェントエラー [{h.kind}]{extra}: {h.detail}"


# ---------------------------------------------------------------------------
# Per-thread guard
# ---------------------------------------------------------------------------


class _ThreadGate:
    """Allows at most one in-flight coordinator per thread key."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy: set[str] = set()

    def acquire(self, key: str) -> bool:
        with self._lock:
            if key in self._busy:
                return False
            self._busy.add(key)
            return True

    def release(self, key: str) -> None:
        with self._lock:
            self._busy.discard(key)


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _RunState:
    pieces: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    tools_by_id: dict[str, Tool] = field(default_factory=dict)


@dataclass(slots=True)
class _PendingApproval:
    """Live state for a paused run awaiting human approval."""

    session_id: str
    key: str
    inbound: Inbound
    reply: Reply
    state: _RunState
    remaining: set[str]


class Coordinator:
    """Wires together a Backend and a Surface to handle one inbound at a time."""

    def __init__(
        self,
        *,
        backend: Backend,
        surface: Surface,
        threads: ThreadStore,
        config: Optional[CoordinatorConfig] = None,
        diagnose: Diagnoser = _default_diagnoser,
        on_outputs: Optional[Callable[[str, Inbound], None]] = None,
    ) -> None:
        self.backend = backend
        self.surface = surface
        self.threads = threads
        self.config = config or CoordinatorConfig()
        self.diagnose = diagnose
        self.on_outputs = on_outputs
        self._gate = _ThreadGate()
        self._pending: dict[str, _PendingApproval] = {}
        self._pending_lock = threading.Lock()

        try:
            surface.listen_tool_decision(self._on_tool_decision)
        except Exception:  # noqa: BLE001
            log.debug("surface has no tool-decision channel", exc_info=True)

    # ----------------------------------------------------------- public API
    def handle(self, inbound: Inbound) -> None:
        """Drive one inbound through every phase. Safe under thread spam."""
        key = inbound.thread_key(self.surface.name)

        if not self._gate.acquire(key):
            log.info("dropping inbound: thread already running (%s)", key)
            label = "前のリクエストを処理中です。完了後にもう一度どうぞ。"
            notice = getattr(self.surface, "post_busy_notice", None)
            if callable(notice):
                try:
                    notice(inbound.where, inbound.thread, label)
                    return
                except Exception:  # noqa: BLE001
                    log.debug("post_busy_notice failed, falling back to post", exc_info=True)
            self.surface.post(
                inbound.where,
                inbound.thread,
                f":hourglass_flowing_sand: {label}",
            )
            return

        reply: Optional[Reply] = None
        paused = False
        try:
            session_id = self._resolve(key, inbound)
            reply = self._open(inbound)
            state = _RunState()
            try:
                outcome = self._stream(session_id, key, inbound, reply, state)
            except StaleSessionError as exc:
                # Cached session was archived/deleted server-side. Drop the
                # mapping, open a fresh session, and retry once with a clean
                # _RunState so leftover Tool beats don't bleed across.
                log.warning(
                    "stale session %s; opening new session for thread=%s",
                    exc.session_id, key,
                )
                self.threads.forget(key)
                session_id = self._resolve(key, inbound)
                state = _RunState()
                outcome = self._stream(session_id, key, inbound, reply, state)
            if outcome == "paused":
                paused = True
                return
            if outcome == "done":
                self._seal(reply)
            self._release(session_id, inbound)
        except Exception as exc:  # noqa: BLE001
            log.exception("coordinator failure")
            if reply is not None:
                try:
                    reply.seal(f":fire: 予期しないエラー: `{type(exc).__name__}: {exc}`")
                except Exception:
                    log.exception("failed to seal reply after error")
            else:
                self.surface.post(
                    inbound.where,
                    inbound.thread,
                    f":fire: 予期しないエラー: `{type(exc).__name__}: {exc}`",
                )
        finally:
            if not paused:
                self._gate.release(key)

    # ----------------------------------------------------------- phase: resolve
    def _resolve(self, key: str, inbound: Inbound) -> str:
        existing = self.threads.lookup(key)
        if existing is not None:
            log.info("resolve: reusing session=%s thread=%s", existing, key)
            return existing
        sid = self.backend.open_thread(title=f"{self.surface.name} {inbound.thread}")
        self.threads.remember(key, sid)
        return sid

    # -------------------------------------------------------------- phase: open
    def _open(self, inbound: Inbound) -> Reply:
        reply = self.surface.open_reply(inbound.where, inbound.thread, inbound.sender)
        # Best-effort: ask the surface to show a "thinking" status indicator.
        try:
            self.surface.hint(inbound.where, inbound.thread, "Thinking ⠋")
        except Exception:  # noqa: BLE001
            log.debug("surface.hint failed", exc_info=True)

        # If the reply supports an animated placeholder (Slack does — via
        # plan-mode tasks or a fallback animator), use it. Otherwise drop a
        # static busy hint so the user sees something immediately.
        animator = getattr(reply, "begin_thinking", None)
        if callable(animator):
            try:
                animator()
            except Exception:  # noqa: BLE001
                log.debug("begin_thinking failed, falling back to static hint", exc_info=True)
                self._safe_write(reply, self.config.busy_hint)
        else:
            self._safe_write(reply, self.config.busy_hint)
        return reply

    @staticmethod
    def _safe_write(reply: Reply, text: str) -> None:
        try:
            reply.write(text)
        except Exception:  # noqa: BLE001
            log.debug("placeholder write failed", exc_info=True)

    @staticmethod
    def _tick_running(reply: Reply, key: str, label: str) -> None:
        """Best-effort: push an in_progress task so the plan-mode spinner stays
        animated during requires_action pauses and resume gaps. Surfaces that
        don't expose ``push_running`` get nothing and that's fine."""
        push = getattr(reply, "push_running", None)
        if not callable(push):
            return
        try:
            push(key, label)
        except Exception:  # noqa: BLE001
            log.debug("push_running failed", exc_info=True)

    # ----------------------------------------------------------- phase: stream
    def _stream(
        self,
        session_id: str,
        key: str,
        inbound: Inbound,
        reply: Reply,
        state: _RunState,
    ) -> _PumpOutcome:
        log.info(
            "stream: session=%s text=%r images=%d",
            session_id,
            inbound.text[:80],
            len(inbound.attachments),
        )
        images = [(a.mime, a.data) for a in inbound.attachments]
        with self.backend.converse(
            session_id, inbound.text or "（空メンション）", images=images
        ) as frames:
            return self._pump(frames, session_id, key, inbound, reply, state)

    def _pump(
        self,
        frames,
        session_id: str,
        key: str,
        inbound: Inbound,
        reply: Reply,
        state: _RunState,
    ) -> _PumpOutcome:
        """Iterate parsed frames, dispatch beats. Shared by initial + resume."""
        for frame in frames:
            for beat in frame.beats:
                if isinstance(beat, Say):
                    state.pieces.append(beat.text)
                    try:
                        reply.write(beat.text)
                    except Exception:  # noqa: BLE001
                        log.debug("reply.write failed", exc_info=True)

                elif isinstance(beat, Tool):
                    if beat.id:
                        state.tools_by_id[beat.id] = beat
                    for prev in state.steps:
                        if prev.status == "running" and prev.key.startswith("tool_"):
                            prev.status = "done"
                    step = Step(
                        key=f"tool_{len(state.steps)}",
                        label=describe_tool(beat),
                        status="running",
                    )
                    state.steps.append(step)
                    try:
                        reply.show_steps(state.steps)
                    except Exception:  # noqa: BLE001
                        log.debug("reply.show_steps failed", exc_info=True)

                elif isinstance(beat, Stage):
                    log.debug("stage=%s", beat.label)

                elif isinstance(beat, Hiccup):
                    log.error("session hiccup: %r", beat)
                    try:
                        reply.seal(self.diagnose(beat))
                    except Exception:  # noqa: BLE001
                        log.exception("seal-on-hiccup failed")
                    return "errored"

                elif isinstance(beat, RequiresAction):
                    self._handle_requires_action(
                        beat, session_id, key, inbound, reply, state
                    )
                    return "paused"

                elif isinstance(beat, Done):
                    return "done"

        return "done"

    def _handle_requires_action(
        self,
        beat: RequiresAction,
        session_id: str,
        key: str,
        inbound: Inbound,
        reply: Reply,
        state: _RunState,
    ) -> None:
        """Park pending state and post the approval UI to the surface."""
        remaining = set(beat.event_ids)
        log.info(
            "requires_action: session=%s tools=%s", session_id, sorted(remaining)
        )

        with self._pending_lock:
            self._pending[session_id] = _PendingApproval(
                session_id=session_id,
                key=key,
                inbound=inbound,
                reply=reply,
                state=state,
                remaining=remaining,
            )

        tool_uses: list[dict] = []
        for tu_id in beat.event_ids:
            t = state.tools_by_id.get(tu_id)
            tool_uses.append(
                {
                    "tool_use_id": tu_id,
                    "name": t.name if t else "tool",
                    "input": t.input if t else None,
                    "server": t.server if t else None,
                    "via_mcp": bool(t and t.via_mcp),
                }
            )

        # Keep the plan-mode spinner visible while we wait for the operator.
        self._tick_running(reply, "await_approval", "Awaiting approval ⠋")

        poster = getattr(self.surface, "post_approval", None)
        if callable(poster):
            try:
                poster(inbound.where, inbound.thread, session_id, tool_uses)
                return
            except Exception:  # noqa: BLE001
                log.exception("post_approval failed; falling back to plain text")

        # Fallback: plain text. The operator has to use Console / SDK to confirm.
        names = ", ".join(f"`{tu['name']}`" for tu in tool_uses) or "tool"
        try:
            self.surface.post(
                inbound.where,
                inbound.thread,
                f":lock: ツール承認待ち: {names}（承認 UI 未対応の Surface のため、Console から実行してください）",
            )
        except Exception:  # noqa: BLE001
            log.exception("fallback post_approval text failed")

    # --------------------------------------------------- approval callbacks
    def _on_tool_decision(self, decision: ToolDecision) -> None:
        """Called by the Surface when the operator approves/denies a tool."""
        with self._pending_lock:
            pending = self._pending.get(decision.session_id)
        if pending is None:
            log.info(
                "tool decision for unknown/expired session: %s", decision.session_id
            )
            return

        try:
            self.backend.confirm_tool_use(
                pending.session_id,
                tool_use_id=decision.tool_use_id,
                result=decision.result,
                deny_message=decision.deny_message,
            )
        except Exception:  # noqa: BLE001
            log.exception("confirm_tool_use failed")
            return

        with self._pending_lock:
            entry = self._pending.get(decision.session_id)
            if entry is None:
                return
            entry.remaining.discard(decision.tool_use_id)
            if entry.remaining:
                log.info(
                    "decision recorded; %d more pending for session=%s",
                    len(entry.remaining),
                    decision.session_id,
                )
                return
            self._pending.pop(decision.session_id, None)

        log.info("all confirmations sent; resuming session=%s", decision.session_id)
        threading.Thread(
            target=self._resume_run,
            args=(pending,),
            name=f"kuhaku-resume-{decision.session_id[:12]}",
            daemon=True,
        ).start()

    def _resume_run(self, pending: _PendingApproval) -> None:
        """Open a fresh SSE on the resumed session and pump remaining beats."""
        outcome: _PumpOutcome = "errored"
        # Keep the spinner alive between approval and the first resumed event.
        self._tick_running(pending.reply, "await_approval", "Running tool ⠋")
        try:
            with self.backend.converse_resume(pending.session_id) as frames:
                outcome = self._pump(
                    frames,
                    pending.session_id,
                    pending.key,
                    pending.inbound,
                    pending.reply,
                    pending.state,
                )
            if outcome == "done":
                self._seal(pending.reply)
            if outcome != "paused":
                self._release(pending.session_id, pending.inbound)
        except Exception as exc:  # noqa: BLE001
            log.exception("resume run failed for session=%s", pending.session_id)
            try:
                pending.reply.seal(
                    f":fire: 再開中にエラー: `{type(exc).__name__}: {exc}`"
                )
            except Exception:
                log.exception("seal after resume failure failed")
        finally:
            if outcome != "paused":
                self._gate.release(pending.key)

    # ------------------------------------------------------------- phase: seal
    def _seal(self, reply: Reply) -> None:
        try:
            reply.seal(None)
        except Exception:  # noqa: BLE001
            log.exception("reply.seal failed")

    # --------------------------------------------------------- phase: release
    def _release(self, session_id: str, inbound: Inbound) -> None:
        # Always clear the surface-level "thinking" indicator, regardless of
        # whether outputs are configured.
        try:
            self.surface.clear_hint(inbound.where, inbound.thread)
        except Exception:  # noqa: BLE001
            log.debug("surface.clear_hint failed", exc_info=True)

        if not self.config.upload_outputs:
            return
        if self.on_outputs is None:
            return
        try:
            self.on_outputs(session_id, inbound)
        except Exception:  # noqa: BLE001
            log.exception("on_outputs hook failed")
