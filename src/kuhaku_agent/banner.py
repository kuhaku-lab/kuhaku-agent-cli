"""Startup banner shown by long-running commands (mainly ``serve``).

Kept separate from ``cli.py`` so it's easy to test the rendering and to swap
the styling without touching CLI wiring.
"""
from __future__ import annotations

from typing import Optional

from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__


# Japanese corner brackets 「」 rendered with block characters.
# 「 sits top-left, 」 sits bottom-right — so reading 「  …  」 across the
# diagonal gives the recognisable Japanese quotation pair.
# Each line is normalised to _LOGO_WIDTH cells via NBSP padding (U+00A0) —
# regular trailing spaces would be stripped by rich, breaking the centering.
_LOGO_WIDTH = 18
_NBSP = " "


def _pad(line: str) -> str:
    return line + _NBSP * (_LOGO_WIDTH - len(line))


_LOGO_LINES = tuple(
    _pad(line)
    for line in (
        "████████",
        "██",
        "██",
        "██",
        _NBSP * 16 + "██",
        _NBSP * 16 + "██",
        _NBSP * 16 + "██",
        _NBSP * 10 + "████████",
    )
)
_TAGLINE = "Slack ↔ Claude Managed Agents bridge"


def render_banner(
    *,
    agent_id: Optional[str] = None,
    environment_id: Optional[str] = None,
    vault_ids: tuple[str, ...] = (),  # accepted for backward compat; not displayed
) -> Panel:
    """Build the startup panel as a rich renderable.

    ``agent_id`` / ``environment_id`` are shown as a small footer when set.
    ``vault_ids`` is intentionally not rendered (kept in the signature so older
    callers don't break).
    """
    del vault_ids  # not displayed by design

    # Every logo line is exactly _LOGO_WIDTH cells (NBSP-padded) so that when
    # ``Align.center`` centers each line individually, they all start at the
    # same column — effectively centering the bracket block as a unit.
    logo = Text("\n".join(_LOGO_LINES), style="bold white", no_wrap=True)
    sub = Text(f"agent  ·  v{__version__}", style="dim")
    tag = Text(_TAGLINE, style="white")

    body = Table.grid(padding=(0, 0))
    body.add_column()
    body.add_row(Align.center(logo))
    body.add_row("")
    body.add_row(Align.center(sub))
    body.add_row("")
    body.add_row(Align.center(tag))

    if agent_id or environment_id:
        body.add_row("")
        meta = Table.grid(padding=(0, 1))
        meta.add_column(style="dim", justify="right")
        meta.add_column()
        if agent_id:
            meta.add_row("agent", Text(agent_id, style="green"))
        if environment_id:
            meta.add_row("env", Text(environment_id, style="green"))
        body.add_row(Align.center(meta))

    return Panel(
        Align.center(body),
        border_style="white",
        padding=(1, 2),
    )


def print_banner(console: Console, **kwargs) -> None:
    """Convenience: render and print to ``console``."""
    console.print(render_banner(**kwargs))
