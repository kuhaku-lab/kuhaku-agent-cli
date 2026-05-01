"""Slack-side ``Reply`` implementation with a serialized worker.

Two output paths depending on what the workspace supports:

1. **Plan-mode streaming** via ``chat.startStream(task_display_mode="plan")``.
   Tasks are sent as ``task_update`` chunks; text deltas as ``markdown_text``
   chunks. Slack renders the plan area natively with spinners on
   ``in_progress`` tasks — that is the "thinking" animation.

2. **Fallback** via ``chat.postMessage`` + ``chat.update``. A small worker
   thread cycles a placeholder so the user still sees motion while the agent
   warms up.

All Web API calls run on a single FIFO worker thread per reply so that order
matches Slack's expectations (mixing ``appendStream`` and ``stopStream``
in parallel races and corrupts the rendered plan block).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ...surfaces.base import Reply, Step

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Thinking animation
# ---------------------------------------------------------------------------


_SPINNER_GLYPHS: tuple[str, ...] = (
    "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏",
)
"""Braille spinner characters reused by both the fallback animator and the
heartbeat. Single source of truth so the visual cue is identical across all
"still working" surfaces."""


THINKING_FRAMES: tuple[str, ...] = tuple(f"_Thinking {g}_" for g in _SPINNER_GLYPHS)
"""Fallback animation frames cycled every ``THINKING_INTERVAL`` seconds.

Slack mrkdwn italics keep the text low-key. The braille spinner gives the
same visual rhythm as the native plan-mode heartbeat below."""


THINKING_INTERVAL = 0.6
"""Seconds between frame updates. Slack rate-limits chat.update at ~1 req/sec
per user; 0.6s gives breathing room while still feeling animated."""


THINKING_DELAY_BEFORE_START = 0.4
"""Wait this long before starting the animation. If the first beat arrives
faster than this, no animation flicker is visible — only the static initial
text shows up."""


PLACEHOLDER_TEXT = "_Thinking ⠋_"
"""Initial chat.postMessage body before the animator takes over (fallback)."""


HEARTBEAT_FRAMES: tuple[str, ...] = tuple(f"Thinking {g}" for g in _SPINNER_GLYPHS)
"""Rotating frames pushed continuously throughout the reply lifetime so the
plan-area spinner keeps moving even after the first text delta lands. Without
this, Slack's plan-mode stops looking animated once body text starts and the
user can't tell whether the agent is still working between deltas."""


HEARTBEAT_INTERVAL = 0.3
"""Seconds between heartbeat ticks. 10 frames × 0.3s = 3s full cycle, which
reads as a brisk terminal-spinner cadence. Each tick pushes one task_update
chunk through the worker queue plus one assistant.threads.setStatus call —
chat.appendStream tolerates this on streaming sessions, and the status
endpoint silently rate-limits without breaking anything (caught in the
loop's try/except)."""


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


@dataclass
class _SlackJob:
    fn: Callable[[], Any]


class _Worker:
    """A tiny FIFO worker that runs jobs on a dedicated thread."""

    def __init__(self, name: str = "slack-reply") -> None:
        self._queue: deque[_SlackJob] = deque()
        self._cv = threading.Condition()
        self._closed = False
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self._thread.start()

    def submit(self, fn: Callable[[], Any]) -> None:
        with self._cv:
            self._queue.append(_SlackJob(fn))
            self._cv.notify()

    def shutdown(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def _loop(self) -> None:
        while True:
            with self._cv:
                while not self._queue and not self._closed:
                    self._cv.wait()
                if not self._queue and self._closed:
                    return
                job = self._queue.popleft()
            try:
                job.fn()
            except Exception:
                log.exception("slack reply worker job failed")


# ---------------------------------------------------------------------------
# Reply
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Plan — in-memory task list rendered as Slack plan-mode chunks
# ---------------------------------------------------------------------------


_SLACK_STATUS = {
    "queued": "pending",
    "running": "in_progress",
    "done": "complete",
    "failed": "error",
}


def _to_chunk(step: Step) -> dict:
    """Render one ``Step`` as a Slack ``task_update`` chunk."""
    return {
        "type": "task_update",
        "id": step.key,
        "title": step.label,
        "status": _SLACK_STATUS.get(step.status, "in_progress"),
    }


class Plan:
    """In-memory plan state for a single Slack reply.

    The Coordinator pushes ``Step``s here as the agent emits ``Tool`` beats.
    During streaming the list is mutated only — sending intermediate
    ``task_update`` chunks races Slack's ``chat.appendStream`` ordering and
    can drop body text. The terminal state is flushed once via
    ``chat.stopStream`` (see ``SlackReply._close``).
    """

    INIT_KEY = "init"
    DEFAULT_INIT_LABEL = "Thinking ⠋"

    def __init__(self, init_label: str = DEFAULT_INIT_LABEL) -> None:
        self.tasks: list[Step] = []
        self.init_label = init_label

    # ------------------------------------------------------------ mutations
    def seed_init(self) -> bool:
        """Insert the seed task. Returns ``True`` only the first time."""
        if self.tasks and self.tasks[0].key == self.INIT_KEY:
            return False
        self.tasks.insert(
            0, Step(key=self.INIT_KEY, label=self.init_label, status="running")
        )
        return True

    def merge(self, steps: list[Step]) -> None:
        """Replace tasks identified by key, append unknown ones in order."""
        index = {t.key: t for t in self.tasks}
        for incoming in steps:
            existing = index.get(incoming.key)
            if existing is None:
                self.tasks.append(incoming)
            else:
                existing.label = incoming.label
                existing.status = incoming.status

    def complete_init(self) -> None:
        for t in self.tasks:
            if t.key == self.INIT_KEY and t.status == "running":
                t.status = "done"

    def complete_running(self) -> None:
        """Mark every in-progress task ``done``. Use before stopStream."""
        for t in self.tasks:
            if t.status == "running":
                t.status = "done"

    # --------------------------------------------------------- accessors
    def init_task(self) -> Optional[Step]:
        if self.tasks and self.tasks[0].key == self.INIT_KEY:
            return self.tasks[0]
        return None

    def slack_chunks(self) -> list[dict]:
        return [_to_chunk(t) for t in self.tasks]


# ---------------------------------------------------------------------------
# Reply
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Surface:
    """Slack-side rendering state for one reply."""

    message_ts: Optional[str] = None
    accumulated: str = ""
    used_native: bool = False


class SlackReply(Reply):
    """Streaming reply that picks the best available Slack surface.

    Plan-mode (``chat.startStream(task_display_mode="plan")``) renders a
    spinner on ``in_progress`` tasks natively — that is the "thinking"
    animation. If plan-mode isn't supported, we fall back to
    ``chat.postMessage`` + ``chat.update`` and a small worker thread cycles
    a placeholder so the user still sees motion.
    """

    def __init__(
        self,
        web_client,
        channel: str,
        thread_ts: str,
        *,
        char_limit: int = 39_000,
        thinking_frames: tuple[str, ...] = THINKING_FRAMES,
        thinking_interval: float = THINKING_INTERVAL,
        init_label: str = Plan.DEFAULT_INIT_LABEL,
    ):
        self._client = web_client
        self._channel = channel
        self._thread_ts = thread_ts
        self._char_limit = char_limit
        self._thinking_frames = thinking_frames
        self._thinking_interval = thinking_interval

        self._surface = _Surface()
        self._plan = Plan(init_label=init_label)
        self._worker = _Worker(name=f"slack-reply-{thread_ts}")
        self._opened = threading.Event()

        # Fallback animator only — in native plan-mode the spinner comes free.
        self._content_arrived = False
        self._animator_stop = threading.Event()
        self._animator_thread: Optional[threading.Thread] = None

        # Heartbeat keeps a visible "still working" cue alive for the full
        # reply lifetime, including between text deltas and tool calls.
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None
        # Coalesced single-slot pulse so worker backlog can't make the
        # animation feel stale.
        self._pending_pulse: Optional[str] = None
        self._pulse_lock = threading.Lock()

        self._worker.submit(self._open_message)

    # ============================================================ Reply API
    def begin_thinking(self) -> None:
        """Seed the plan area with an init task; idempotent."""
        if self._plan.seed_init():
            self._worker.submit(self._send_init_task)
        if self._animator_thread is None:
            self._animator_thread = threading.Thread(
                target=self._animate,
                name=f"slack-thinking-{self._thread_ts}",
                daemon=True,
            )
            self._animator_thread.start()
        self._start_heartbeat()

    def write(self, delta: str) -> None:
        if not delta:
            return
        self._plan.complete_init()
        self._mark_content_arrived()
        self._worker.submit(lambda: self._append(delta))

    def show_steps(self, steps: list[Step]) -> None:
        # Mutate in memory only; we flush the terminal state via stopStream.
        # Sending intermediate task_update chunks races chat.appendStream
        # ordering and can drop body text — same caveat agentchannels notes.
        self._plan.complete_init()
        self._plan.merge(steps)
        self._mark_content_arrived()

    def push_running(self, key: str, label: str) -> None:
        """Push an in_progress task chunk *now* so Slack keeps the spinner.

        Used by the Coordinator while a requires_action pause is in flight —
        between the operator clicking approve and the resumed agent emitting
        its first event there is no text to stream, so without a fresh
        in_progress task the plan-mode message can look frozen.

        Safe only when no text deltas are in flight; the Coordinator only
        calls this in idle gaps (pause start / resume start).
        """
        self._worker.submit(lambda: self._push_running(key, label))

    def _push_running(self, key: str, label: str) -> None:
        self._opened.wait()
        if self._surface.message_ts is None or not self._surface.used_native:
            return
        # Track the task in the plan so chat.stopStream's finalization marks
        # it complete instead of leaving a dangling in_progress entry.
        target: Optional[Step] = None
        for t in self._plan.tasks:
            if t.key == key:
                t.label = label
                t.status = "running"
                target = t
                break
        if target is None:
            target = Step(key=key, label=label, status="running")
            self._plan.tasks.append(target)
        try:
            self._client.chat_appendStream(
                channel=self._channel,
                ts=self._surface.message_ts,
                chunks=[_to_chunk(target)],
            )
        except Exception:
            log.debug("push_running failed", exc_info=True)

    def seal(
        self,
        final_text: Optional[str] = None,
        final_steps: Optional[list[Step]] = None,
    ) -> None:
        if final_steps is not None:
            self._plan.merge(final_steps)
        self._plan.complete_running()
        self._mark_content_arrived()
        self._heartbeat_stop.set()
        self._worker.submit(lambda: self._close(final_text))
        self._worker.shutdown()

    # ============================================================ internals
    def _mark_content_arrived(self) -> None:
        if not self._content_arrived:
            self._content_arrived = True
            self._animator_stop.set()

    # ------- heartbeat: keep a visible spinner for the full reply lifetime
    def _start_heartbeat(self) -> None:
        if self._heartbeat_thread is not None:
            return
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"slack-heartbeat-{self._thread_ts}",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        """Refresh the init task's label every ``HEARTBEAT_INTERVAL`` seconds.

        Refreshing the *seed* task (``init``) rather than a separate ``pulse``
        task keeps the spinner visually prominent — the init row is the first
        thing Slack renders in the plan area. The cadence is held by this
        thread; the actual API call is offloaded so a slow Slack response on
        one tick can't delay the next one.
        """
        self._opened.wait()
        if self._surface.message_ts is None:
            return
        idx = 0
        while not self._heartbeat_stop.wait(HEARTBEAT_INTERVAL):
            frame = HEARTBEAT_FRAMES[idx % len(HEARTBEAT_FRAMES)]
            idx += 1
            # Plan-area pulse via worker, coalesced so backlog can't build:
            # only one heartbeat job is queued at any time, and the worker
            # always consumes the latest frame when it picks the job up.
            if not self._surface.used_native:
                continue
            with self._pulse_lock:
                was_idle = self._pending_pulse is None
                self._pending_pulse = frame
            if was_idle:
                self._worker.submit(self._consume_pulse)

    def _consume_pulse(self) -> None:
        with self._pulse_lock:
            label = self._pending_pulse
            self._pending_pulse = None
        if label is None:
            return
        # Refresh the seed (`init`) task. Slack's plan area always shows the
        # init row first, so updating it gives the most visible spinner.
        self._push_running(Plan.INIT_KEY, label)

    # ------- fallback-mode animation thread (native plan-mode skips this)
    def _animate(self) -> None:
        self._opened.wait()
        if self._surface.message_ts is None or self._surface.used_native:
            return
        if self._animator_stop.wait(THINKING_DELAY_BEFORE_START):
            return

        idx = 0
        while not self._animator_stop.is_set():
            frame = self._thinking_frames[idx % len(self._thinking_frames)]
            try:
                self._client.chat_update(
                    channel=self._channel,
                    ts=self._surface.message_ts,
                    text=frame,
                )
            except Exception:
                log.debug("thinking animation update failed", exc_info=True)
                return
            idx += 1
            if self._animator_stop.wait(self._thinking_interval):
                return

    # ------- worker-thread operations
    def _open_message(self) -> None:
        try:
            resp = self._client.chat_startStream(
                channel=self._channel,
                thread_ts=self._thread_ts,
                task_display_mode="plan",
            )
            self._surface.message_ts = resp.get("ts")
            self._surface.used_native = True
            log.debug("chat.startStream(plan) ok ts=%s", self._surface.message_ts)
        except Exception as exc:
            log.info("chat.startStream unavailable, using post+update: %s", exc)
            resp = self._client.chat_postMessage(
                channel=self._channel,
                thread_ts=self._thread_ts,
                text=PLACEHOLDER_TEXT,
            )
            self._surface.message_ts = resp["ts"]
            self._surface.used_native = False
        finally:
            self._opened.set()

    def _send_init_task(self) -> None:
        """Push the seed task so the plan-area spinner appears immediately."""
        self._opened.wait()
        if self._surface.message_ts is None or not self._surface.used_native:
            return
        init = self._plan.init_task()
        if init is None:
            return
        try:
            self._client.chat_appendStream(
                channel=self._channel,
                ts=self._surface.message_ts,
                chunks=[_to_chunk(init)],
            )
        except Exception:
            log.debug("init task appendStream failed", exc_info=True)

    def _append(self, delta: str) -> None:
        self._opened.wait()
        if self._surface.message_ts is None:
            return

        # Always mirror the delta into accumulated so the chat.update fallback
        # in _close has something to render if chat.stopStream ever fails.
        self._surface.accumulated += delta

        if self._surface.used_native:
            try:
                self._client.chat_appendStream(
                    channel=self._channel,
                    ts=self._surface.message_ts,
                    chunks=[{"type": "markdown_text", "text": delta}],
                )
                return
            except Exception:
                log.debug(
                    "chat.appendStream failed, falling back to update",
                    exc_info=True,
                )
                self._surface.used_native = False  # one-way switch

        try:
            self._client.chat_update(
                channel=self._channel,
                ts=self._surface.message_ts,
                text=self._surface.accumulated[: self._char_limit],
            )
        except Exception:
            log.exception("chat.update fallback failed")

    def _close(self, final_text: Optional[str]) -> None:
        self._opened.wait()
        if self._surface.message_ts is None:
            return

        # Restore the init label so the final message doesn't freeze on the
        # last heartbeat frame (e.g. "Thinking ⠧").
        init = self._plan.init_task()
        if init is not None:
            init.label = self._plan.init_label

        if self._surface.used_native:
            chunks: list[dict] = self._plan.slack_chunks()
            if final_text:
                chunks.append({"type": "markdown_text", "text": final_text})
            kwargs: dict = {
                "channel": self._channel,
                "ts": self._surface.message_ts,
            }
            if chunks:
                kwargs["chunks"] = chunks
            try:
                self._client.chat_stopStream(**kwargs)
                return
            except Exception:
                log.debug(
                    "chat.stopStream failed, falling back to update",
                    exc_info=True,
                )

        body = final_text if final_text is not None else self._surface.accumulated
        body = (body or "（応答が空でした）")[: self._char_limit]
        try:
            self._client.chat_update(
                channel=self._channel, ts=self._surface.message_ts, text=body
            )
        except Exception:
            log.exception("chat.update on close failed")

