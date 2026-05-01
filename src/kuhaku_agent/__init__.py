"""kuhaku-agent-cli — bridge messaging surfaces to Claude Managed Agents.

Public API:

    from kuhaku_agent import Backend, Coordinator, ThreadStore
    from kuhaku_agent.surfaces.slack import SlackSurface

The library is organized as small, single-purpose modules:

    backend.py     — Anthropic Managed Agents SDK wrapper
    coordinator.py — orchestrates one inbound message → streaming reply
    thread_store.py— in-memory thread → session-id index
    events.py      — typed event union + parser for the SSE stream
    settings.py    — runtime settings (CLI flag > env > .env)
    surfaces/      — pluggable channel implementations
"""

__version__ = "0.1.0"

from .backend import Backend
from .coordinator import Coordinator
from .thread_store import ThreadStore

__all__ = ["Backend", "Coordinator", "ThreadStore", "__version__"]
