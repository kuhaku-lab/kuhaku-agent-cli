"""Thread-key → Managed Agents session-id index.

In-memory by default. If ``persist_path`` is given, the index is mirrored to a
JSON file so that a process restart picks up the same thread→session mapping
and the conversation history on the Anthropic side continues to be reused.

Naming note: this module replaces what agentchannels calls ``SessionManager``.
We use ``ThreadStore`` because the index is keyed by thread, not session.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Iterator, Optional

log = logging.getLogger(__name__)


@dataclass(slots=True)
class _Slot:
    session_id: str
    born_at: float
    touched_at: float


class ThreadStore:
    """Mapping of thread keys to session ids.

    Parameters
    ----------
    idle_ttl:
        If set, entries not touched for ``idle_ttl`` seconds are evicted on
        the next lookup. ``None`` (default) disables expiry.
    persist_path:
        If set, every mutation is written to this JSON file (atomically) and
        the file is read at construction. ``None`` keeps behaviour purely
        in-memory.
    """

    def __init__(
        self,
        *,
        idle_ttl: Optional[int] = None,
        persist_path: Optional[Path] = None,
    ) -> None:
        self._slots: dict[str, _Slot] = {}
        self._idle_ttl = idle_ttl
        self._guard = RLock()
        self._path = persist_path
        if self._path is not None:
            self._load()

    # ------------------------------------------------------------------ ops
    def lookup(self, key: str) -> Optional[str]:
        with self._guard:
            slot = self._slots.get(key)
            if slot is None:
                return None
            if self._is_expired(slot):
                self._slots.pop(key, None)
                self._flush_locked()
                return None
            slot.touched_at = time.time()
            # No flush on touch — saves disk churn; touched_at drifts at
            # most by one process lifetime, which is harmless for TTL.
            return slot.session_id

    def remember(self, key: str, session_id: str) -> None:
        now = time.time()
        with self._guard:
            self._slots[key] = _Slot(session_id, now, now)
            self._flush_locked()

    def forget(self, key: str) -> None:
        with self._guard:
            if self._slots.pop(key, None) is not None:
                self._flush_locked()

    def items(self) -> Iterator[tuple[str, str]]:
        """Snapshot view of (key, session_id) pairs."""
        with self._guard:
            return iter([(k, v.session_id) for k, v in self._slots.items()])

    # ----------------------------------------------------------------- info
    def __len__(self) -> int:
        with self._guard:
            return len(self._slots)

    def __contains__(self, key: str) -> bool:
        with self._guard:
            return key in self._slots

    # --------------------------------------------------------------- helper
    def _is_expired(self, slot: _Slot) -> bool:
        if self._idle_ttl is None:
            return False
        return (time.time() - slot.touched_at) > self._idle_ttl

    # --------------------------------------------------------- persistence
    def _load(self) -> None:
        assert self._path is not None
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            log.warning("thread store at %s is unreadable; starting empty", self._path)
            return
        slots = data.get("slots") if isinstance(data, dict) else None
        if not isinstance(slots, dict):
            return
        for key, raw in slots.items():
            if not isinstance(raw, dict):
                continue
            sid = raw.get("session_id")
            if not isinstance(sid, str) or not sid:
                continue
            born = float(raw.get("born_at", 0.0) or 0.0)
            touched = float(raw.get("touched_at", born) or born)
            self._slots[key] = _Slot(sid, born, touched)
        log.info("thread store loaded %d entries from %s", len(self._slots), self._path)

    def _flush_locked(self) -> None:
        if self._path is None:
            return
        payload = {
            "version": 1,
            "slots": {
                k: {
                    "session_id": s.session_id,
                    "born_at": s.born_at,
                    "touched_at": s.touched_at,
                }
                for k, s in self._slots.items()
            },
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                delete=False,
            ) as tmp:
                json.dump(payload, tmp, ensure_ascii=False)
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp_path = tmp.name
            os.replace(tmp_path, self._path)
        except Exception:  # noqa: BLE001
            log.exception("failed to persist thread store to %s", self._path)
