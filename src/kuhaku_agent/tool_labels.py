"""Human-readable labels for built-in agent tools and MCP calls.

Used by the Coordinator to turn an ``agent.tool_use`` beat into a phrase
suitable for Slack's plan-mode task list. Keep entries short — Slack truncates
long task titles in the rendered plan area.
"""
from __future__ import annotations

from typing import Optional

from .events import Tool


# Built-in tools shipped with ``agent_toolset_*``. Map symbol → friendly verb
# phrase. Anything not listed falls back to ``Using <name>``.
_BUILTIN_LABELS = {
    "bash": "Running shell command",
    "read": "Reading file",
    "write": "Writing file",
    "edit": "Editing file",
    "glob": "Listing files",
    "grep": "Searching files",
    "web_fetch": "Fetching web page",
    "web_search": "Searching the web",
}


def describe_tool(beat: Tool) -> str:
    """Translate a ``Tool`` beat into a single-line label.

    Examples
    --------
    >>> describe_tool(Tool(name="read"))
    'Reading file'
    >>> describe_tool(Tool(name="search.messages", via_mcp=True, server="slack"))
    'Calling slack: search.messages'
    """
    if beat.via_mcp:
        if beat.server:
            return f"Calling {beat.server}: {beat.name}"
        return f"Calling MCP: {beat.name}"
    return _BUILTIN_LABELS.get(beat.name, f"Using {beat.name}")


def describe_tool_name(name: str, *, via_mcp: bool = False, server: Optional[str] = None) -> str:
    """String-arg variant for callers that don't have a ``Tool`` instance."""
    return describe_tool(Tool(name=name, via_mcp=via_mcp, server=server))
