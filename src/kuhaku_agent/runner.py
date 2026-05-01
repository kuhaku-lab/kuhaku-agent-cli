"""Top-level wiring: build a Backend + Surface + Coordinator and start serving."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import requests

from .backend import Backend, BackendBindings
from .coordinator import Coordinator, CoordinatorConfig
from .settings import Settings
from .surfaces.base import Inbound
from .surfaces.slack import SlackSurface, slack_diagnoser
from .surfaces.slack.surface import SlackSurfaceConfig
from .thread_store import ThreadStore

log = logging.getLogger(__name__)


def build_runtime(settings: Settings) -> tuple[SlackSurface, Coordinator, Backend]:
    """Construct the full runtime graph from validated settings."""
    backend = Backend(
        api_key=settings.anthropic_api_key,
        bindings=BackendBindings(
            agent_id=settings.agent_id,
            environment_id=settings.environment_id,
            vault_ids=settings.vault_ids,
        ),
    )
    surface = SlackSurface(
        SlackSurfaceConfig(
            bot_token=settings.slack_bot_token,
            app_token=settings.slack_app_token,
        )
    )
    threads_path = settings.thread_store_path or (Path.cwd() / ".kuhaku" / "threads.json")
    threads = ThreadStore(persist_path=threads_path)

    def on_outputs(session_id: str, inbound: Inbound) -> None:
        files = backend.session_outputs(session_id)
        for f in files:
            name = getattr(f, "filename", None) or getattr(f, "name", "output")
            if getattr(f, "downloadable", True) is False:
                continue
            try:
                content = backend.download_session_file(session_id, f.id)
                surface._app.client.files_upload_v2(  # type: ignore[attr-defined]
                    channel=inbound.where,
                    thread_ts=inbound.thread,
                    filename=name,
                    content=content,
                    initial_comment=f"成果物: `{name}`",
                )
            except Exception:  # noqa: BLE001
                log.exception("failed to upload output file=%s", name)

    coordinator = Coordinator(
        backend=backend,
        surface=surface,
        threads=threads,
        config=CoordinatorConfig(),
        diagnose=slack_diagnoser,
        on_outputs=on_outputs,
    )
    surface.listen(coordinator.handle)
    return surface, coordinator, backend


def serve(settings: Settings) -> None:
    """Blocking run loop. Returns when the surface is stopped."""
    surface, coordinator, backend = build_runtime(settings)
    log.info(
        "kuhaku-agent ready: agent=%s env=%s vaults=%s",
        settings.agent_id,
        settings.environment_id,
        ",".join(settings.vault_ids) if settings.vault_ids else "(none)",
    )
    backend.ping()
    surface.start()
