"""Runtime settings — three-source precedence: CLI args > os.environ > .env file.

The dataclass below is the single source of truth for every knob the program
needs. ``Settings.load()`` walks the three sources, validates required keys,
and reports human-readable errors.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from dotenv import dotenv_values


# ---------------------------------------------------------------------------
# Variable names (keep one place to grep)
# ---------------------------------------------------------------------------


ENV_KEYS = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "agent_id": "KUHAKU_AGENT_ID",
    "environment_id": "KUHAKU_ENVIRONMENT_ID",
    "vault_ids": "KUHAKU_VAULT_IDS",  # comma-separated
    "slack_bot_token": "SLACK_BOT_TOKEN",
    "slack_app_token": "SLACK_APP_TOKEN",
    "thread_store_path": "KUHAKU_THREAD_STORE_PATH",  # optional
}

OPTIONAL_KEYS = {"vault_ids", "thread_store_path"}
"""Fields whose absence is fine — vault_ids defaults to empty, and a missing
thread_store_path falls back to ``.kuhaku/threads.json`` under the cwd."""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SettingsError(RuntimeError):
    """Raised when required settings are missing or malformed."""

    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__(
            "missing required settings: " + ", ".join(missing)
            if missing
            else "settings invalid"
        )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Settings:
    anthropic_api_key: str
    agent_id: str
    environment_id: str
    slack_bot_token: str
    slack_app_token: str
    vault_ids: tuple[str, ...] = ()
    thread_store_path: Optional[Path] = None

    # ------------------------------------------------------------ loading
    @classmethod
    def load(
        cls,
        *,
        overrides: Optional[Mapping[str, str]] = None,
        cwd: Optional[Path] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> "Settings":
        """Resolve settings from CLI overrides, OS env, and .env file.

        Parameters
        ----------
        overrides:
            High-priority values, typically CLI flags. Keyed by Settings field
            name (``"agent_id"``, not the env var name).
        cwd:
            Directory that contains ``.env``. Defaults to ``Path.cwd()``.
        env:
            Mapping standing in for ``os.environ`` (handy in tests).
        """
        overrides = dict(overrides or {})
        env_map = dict(env if env is not None else os.environ)
        dot_env = _load_dotenv(cwd or Path.cwd())

        def pick(field_name: str) -> Optional[str]:
            if field_name in overrides and overrides[field_name]:
                return overrides[field_name]
            env_var = ENV_KEYS[field_name]
            if env_var in env_map and env_map[env_var]:
                return env_map[env_var]
            return dot_env.get(env_var) or None

        missing: list[str] = []
        values: dict[str, object] = {}
        for field_name in ENV_KEYS:
            v = pick(field_name)
            if field_name == "vault_ids":
                values[field_name] = tuple(_split_csv(v) if v else ())
                continue
            if field_name == "thread_store_path":
                values[field_name] = Path(v).expanduser() if v else None
                continue
            if not v and field_name not in OPTIONAL_KEYS:
                missing.append(ENV_KEYS[field_name])
            values[field_name] = v or ""

        if missing:
            raise SettingsError(missing)

        return cls(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_dotenv(cwd: Path) -> dict[str, str]:
    path = cwd / ".env"
    if not path.exists():
        return {}
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]
