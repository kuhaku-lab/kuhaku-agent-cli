"""Bootstrap operations behind ``kuhaku-agent init``.

Pure-ish helpers split out from ``cli.py`` so they're easy to test:

    upsert_env_line(path, key, value)       — idempotent .env editor
    default_system_prompt()                  — built-in agent system prompt
    default_environment_config(...)          — sensible Environment config dict
    default_agent_spec(name)                 — JSON-shaped Agent spec template
    load_agent_spec(path)                    — read JSON spec from disk
    save_agent_spec(path, spec)              — write JSON spec to disk

The Backend-driving helpers (``create_agent``, ``create_environment``) are also
here so the CLI can stay focused on argument parsing and presentation.
"""
from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from typing import Any, Optional, Sequence

from .backend import Backend


# ---------------------------------------------------------------------------
# .env editor
# ---------------------------------------------------------------------------


def upsert_env_line(path: Path, key: str, value: str) -> None:
    """Write ``KEY=value`` into ``.env``, replacing the existing line if any.

    Preserves comments, ordering, and unrelated entries. Creates the file with
    just the new line if it doesn't exist.
    """
    new_line = f"{key}={value}"
    if not path.exists():
        path.write_text(new_line + "\n", encoding="utf-8")
        return

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    replaced = False
    for i, raw in enumerate(lines):
        stripped = raw.lstrip()
        # only consider non-comment lines starting with KEY=
        if stripped.startswith(f"{key}="):
            lines[i] = new_line
            replaced = True
            break

    if not replaced:
        # ensure exactly one blank between existing content and the appended line
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append(new_line)

    out = "\n".join(lines)
    if not out.endswith("\n"):
        out += "\n"
    path.write_text(out, encoding="utf-8")


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def default_system_prompt() -> str:
    """Built-in starter prompt — small, friendly, easy for users to swap out."""
    return dedent(
        """
        あなたは社内アシスタントの Slack ボットです。

        - ユーザーの質問に簡潔に答えてください。絵文字は控えめに。
        - 不確かなときは推測せず、確認手段を提案してください。
        - 機密情報・ハラスメント・法的判断は安易に答えず、人間の担当者に振ってください。
        """
    ).strip()


def default_environment_config(
    *,
    allowed_hosts: Sequence[str] = ("hooks.slack.com",),
    allow_mcp: bool = True,
    allow_pkg: bool = True,
    pip: Sequence[str] = (),
) -> dict:
    """Minimal-but-useful Environment config dict.

    Ports stay closed unless the caller opens them: most workflows don't need
    pandas / openpyxl pre-installed and the cold start is faster without them.
    """
    return {
        "type": "cloud",
        "networking": {
            "type": "limited",
            "allowed_hosts": list(allowed_hosts),
            "allow_mcp_servers": allow_mcp,
            "allow_package_managers": allow_pkg,
        },
        "packages": {
            "type": "packages",
            "pip": list(pip),
        },
    }


# ---------------------------------------------------------------------------
# Backend-driving helpers
# ---------------------------------------------------------------------------


def make_agent(
    backend: Backend,
    *,
    name: str = "kuhaku-agent",
    model: str = "claude-sonnet-4-6",
    system: Optional[str] = None,
    system_file: Optional[Path] = None,
) -> str:
    """Create an Agent and return its id.

    Resolves the system prompt with this precedence: ``system_file`` → inline
    ``system`` → built-in default. For richer specs (mcp_servers, tools,
    skills, metadata) use ``make_agent_from_spec`` instead.
    """
    if system_file is not None:
        body = system_file.read_text(encoding="utf-8").strip()
    elif system:
        body = system
    else:
        body = default_system_prompt()
    return backend.create_agent(name=name, model=model, system=body)


# ---------------------------------------------------------------------------
# Agent spec (JSON-on-disk format)
# ---------------------------------------------------------------------------


def default_agent_spec(name: str = "kuhaku-agent") -> dict[str, Any]:
    """Return a minimal JSON-shaped Agent spec.

    Mirrors the Anthropic SDK's ``agents.create`` payload — fields users can
    edit directly: ``name`` / ``description`` / ``model`` / ``system`` /
    ``mcp_servers`` / ``tools`` / ``skills`` / ``metadata``.
    """
    return {
        "name": name,
        "description": "",
        "model": {"id": "claude-sonnet-4-6", "speed": "standard"},
        "system": default_system_prompt(),
        "mcp_servers": [],
        "tools": [],
        "skills": [],
        "metadata": {},
    }


def load_agent_spec(path: Path) -> dict[str, Any]:
    """Read a JSON Agent spec from ``path``.

    Raises ``FileNotFoundError`` if missing, ``json.JSONDecodeError`` if
    malformed. The caller is responsible for surfacing nice errors.
    """
    return json.loads(path.read_text(encoding="utf-8"))


def save_agent_spec(path: Path, spec: dict[str, Any]) -> None:
    """Write a spec to ``path`` as pretty-printed JSON. Creates parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(spec, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def make_agent_from_spec(backend: Backend, spec: dict[str, Any]) -> str:
    """Create an Agent from a full spec dict via ``Backend``.

    Trivial passthrough today; here so callers don't need to know whether to
    use the simple or spec-based Backend method.
    """
    return backend.create_agent_from_spec(spec)


def make_environment(
    backend: Backend,
    *,
    name: str = "kuhaku-agent-env",
    allowed_hosts: Sequence[str] = ("hooks.slack.com",),
    allow_mcp: bool = True,
    allow_pkg: bool = True,
    pip: Sequence[str] = (),
) -> str:
    """Create an Environment and return its id."""
    config = default_environment_config(
        allowed_hosts=allowed_hosts,
        allow_mcp=allow_mcp,
        allow_pkg=allow_pkg,
        pip=pip,
    )
    return backend.create_environment(name=name, config=config)
