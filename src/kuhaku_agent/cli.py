"""CLI entrypoint — ``kuhaku-agent`` command."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .backend import Backend
from .banner import print_banner
from .init_ops import (
    default_agent_spec,
    default_system_prompt,
    load_agent_spec,
    make_agent,
    make_agent_from_spec,
    make_environment,
    save_agent_spec,
    upsert_env_line,
)
from .settings import ENV_KEYS, Settings, SettingsError

app = typer.Typer(
    name="kuhaku-agent",
    help="Bridge messaging surfaces to Claude Managed Agents.",
    invoke_without_command=True,
)
console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


# ---------------------------------------------------------------------------
# version / no-arg landing
# ---------------------------------------------------------------------------


@app.callback()
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Print version and exit."),
) -> None:
    if version:
        console.print(f"kuhaku-agent {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        # No subcommand: show banner + a one-line cheat sheet, exit cleanly.
        print_banner(console)
        console.print()
        console.print("[bold]Commands[/bold]")
        console.print("  [cyan]doctor[/cyan]    validate settings + ping the API")
        console.print("  [cyan]vaults[/cyan]    list available Vaults")
        console.print("  [cyan]init[/cyan]      bootstrap Agent + Environment")
        console.print("  [cyan]serve[/cyan]     start the Slack listener")
        console.print()
        console.print("Run [bold]kuhaku-agent --help[/bold] for full options.")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


@app.command()
def serve(
    agent_id: Optional[str] = typer.Option(None, "--agent-id"),
    environment_id: Optional[str] = typer.Option(None, "--environment-id"),
    vault_ids: Optional[str] = typer.Option(
        None, "--vault-ids", help="Comma-separated vault IDs."
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Run the Slack ↔ Managed Agents bridge."""
    _setup_logging(verbose)

    overrides: dict[str, str] = {}
    if agent_id:
        overrides["agent_id"] = agent_id
    if environment_id:
        overrides["environment_id"] = environment_id
    if vault_ids:
        overrides["vault_ids"] = vault_ids

    try:
        settings = Settings.load(overrides=overrides)
    except SettingsError as e:
        console.print(f"[red]設定が不足しています:[/red] {', '.join(e.missing)}")
        console.print("→ `.env` を見直すか、--agent-id / --environment-id を指定してください")
        raise typer.Exit(code=2)

    print_banner(
        console,
        agent_id=settings.agent_id,
        environment_id=settings.environment_id,
        vault_ids=settings.vault_ids,
    )

    # Local import to avoid pulling Slack deps for unrelated commands.
    from .runner import serve as _serve

    _serve(settings)


# ---------------------------------------------------------------------------
# vaults
# ---------------------------------------------------------------------------


@app.command()
def vaults(
    limit: int = typer.Option(20, "--limit"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """List Anthropic Vaults available to the configured API key."""
    _setup_logging(verbose)
    try:
        settings = Settings.load()
    except SettingsError as e:
        # API key alone may be enough; allow partial settings.
        if "ANTHROPIC_API_KEY" in e.missing:
            console.print("[red]ANTHROPIC_API_KEY が見つかりません[/red]")
            raise typer.Exit(code=2)
        # other missing fields are fine here
        import os

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        backend = Backend(api_key=api_key)
    else:
        backend = Backend(api_key=settings.anthropic_api_key)

    rows = backend.list_vaults(limit=limit)
    if not rows:
        console.print("[yellow]Vault が 1 つも見つかりませんでした[/yellow]")
        console.print("→ https://console.anthropic.com で Vault を作成してください")
        return

    table = Table(title="Vaults")
    table.add_column("vault_id", overflow="fold")
    table.add_column("name")
    table.add_column("credentials")
    for v in rows:
        creds_text = "\n".join(
            f"{c['display_name']} ({c['type']}) [{c['status']}]"
            for c in v["credentials"]
        ) or "(none)"
        table.add_row(v["id"], v["name"], creds_text)
    console.print(table)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@app.command()
def doctor() -> None:
    """Quick health-check of settings and Anthropic auth."""
    try:
        settings = Settings.load()
    except SettingsError as e:
        console.print("[red]設定エラー[/red]")
        for k in e.missing:
            console.print(f"  - {k}")
        raise typer.Exit(code=2)

    console.print(":white_check_mark: settings loaded")
    backend = Backend(api_key=settings.anthropic_api_key)
    try:
        backend.ping()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Anthropic API への接続に失敗:[/red] {exc}")
        raise typer.Exit(code=1)
    console.print(":white_check_mark: anthropic api reachable")
    console.print(f"  agent_id = {settings.agent_id}")
    console.print(f"  environment_id = {settings.environment_id}")
    console.print(f"  vault_ids = {','.join(settings.vault_ids) or '(none)'}")


# ---------------------------------------------------------------------------
# init — Anthropic resource bootstrap
# ---------------------------------------------------------------------------


init_app = typer.Typer(
    name="init",
    help="Bootstrap Anthropic resources (Agent, Environment) for this bot.",
    no_args_is_help=False,
    invoke_without_command=True,
)
app.add_typer(init_app, name="init")


def _real_or_none(value: Optional[str], *, prefix: str) -> Optional[str]:
    """Return ``value`` if it looks like a real ID, else ``None``.

    Treats empty strings, ``replace-me`` placeholders, and values that don't
    start with the expected resource prefix as missing — so the wizard creates
    the resource instead of falsely "reusing" a stub like ``agent_replace_me``.
    """
    if not value:
        return None
    v = value.strip()
    if not v:
        return None
    lower = v.lower()
    if "replace" in lower or lower.endswith("replace_me") or lower.endswith("-replace-me"):
        return None
    if not v.startswith(prefix):
        return None
    return v


def _backend_from_env() -> Backend:
    """Build a Backend from ANTHROPIC_API_KEY only (other settings optional)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        # fall back to .env via Settings; ignore other missing keys
        try:
            from .settings import _load_dotenv  # type: ignore[attr-defined]

            api_key = _load_dotenv(Path.cwd()).get("ANTHROPIC_API_KEY", "")
        except Exception:  # noqa: BLE001
            api_key = ""
    if not api_key:
        console.print("[red]ANTHROPIC_API_KEY が見つかりません[/red]")
        raise typer.Exit(code=2)
    return Backend(api_key=api_key)


def _print_id_table(rows: list[tuple[str, str]]) -> None:
    table = Table(title="Created", show_header=False)
    table.add_column("key", style="bold")
    table.add_column("value", overflow="fold")
    for k, v in rows:
        table.add_row(k, v)
    console.print(table)


@init_app.command("agent")
def init_agent_cmd(
    name: str = typer.Option("kuhaku-agent", "--name"),
    model: str = typer.Option("claude-sonnet-4-6", "--model"),
    system: Optional[str] = typer.Option(None, "--system", help="Inline system prompt."),
    system_file: Optional[Path] = typer.Option(
        None, "--system-file", exists=True, dir_okay=False, readable=True
    ),
    from_file: Optional[Path] = typer.Option(
        None,
        "--from-file",
        "-f",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Load a full Agent spec (JSON) and create from it. Other flags are ignored.",
    ),
    save_spec: Optional[Path] = typer.Option(
        None,
        "--save-spec",
        help="After creation, persist the spec used to this path (JSON).",
    ),
    template_out: Optional[Path] = typer.Option(
        None,
        "--template-out",
        help="Write a default Agent spec template to this path and exit (no API call).",
    ),
    write_env: bool = typer.Option(
        True, "--write-env/--no-write-env", help="Append to .env (replacing existing key)."
    ),
    env_path: Path = typer.Option(Path(".env"), "--env-file"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Create a Managed Agent and (optionally) write its id to .env.

    Three creation modes:

    1. Defaults from flags (``--name`` / ``--model`` / ``--system``).
    2. From a JSON spec file: ``--from-file agents/myagent.json``.
    3. Emit a template to edit later: ``--template-out agents/myagent.json``.
    """
    _setup_logging(verbose)

    # Mode 3: emit a template and exit without calling the API.
    if template_out is not None:
        spec = default_agent_spec(name=name)
        save_agent_spec(template_out, spec)
        console.print(f"[green]Wrote template[/green] {template_out}")
        console.print("Edit it, then re-run with [bold]--from-file " + str(template_out) + "[/bold]")
        raise typer.Exit()

    backend = _backend_from_env()

    # Mode 2: load spec from disk.
    if from_file is not None:
        try:
            spec = load_agent_spec(from_file)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]spec ファイルを読み込めません:[/red] {exc}")
            raise typer.Exit(code=2)
        try:
            agent_id = make_agent_from_spec(backend, spec)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Agent 作成に失敗:[/red] {exc}")
            raise typer.Exit(code=1)
        console.print(f"[dim]spec source:[/dim] {from_file}")

    # Mode 1: build a spec from --name / --model / --system flags.
    else:
        if system_file is not None:
            system_body = system_file.read_text(encoding="utf-8").strip()
        elif system:
            system_body = system
        else:
            system_body = default_system_prompt()

        spec = default_agent_spec(name=name)
        spec["model"] = {"id": model, "speed": "standard"}
        spec["system"] = system_body

        try:
            agent_id = make_agent_from_spec(backend, spec)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Agent 作成に失敗:[/red] {exc}")
            raise typer.Exit(code=1)

    _print_id_table([(ENV_KEYS["agent_id"], agent_id)])

    if save_spec is not None:
        save_agent_spec(save_spec, spec)
        console.print(f"[green]Saved spec[/green] {save_spec}")

    if write_env:
        upsert_env_line(env_path, ENV_KEYS["agent_id"], agent_id)
        console.print(f"[green].env updated[/green] ({env_path})")
    else:
        console.print("[yellow]Add this to your .env manually.[/yellow]")
    console.print("Next: [bold]uv run kuhaku-agent doctor[/bold]")


@init_app.command("environment")
def init_environment_cmd(
    name: str = typer.Option("kuhaku-agent-env", "--name"),
    allowed_host: list[str] = typer.Option(
        ["hooks.slack.com"],
        "--allowed-host",
        help="Repeatable. Hostnames the sandbox may reach.",
    ),
    allow_mcp: bool = typer.Option(True, "--allow-mcp/--no-allow-mcp"),
    allow_pkg: bool = typer.Option(True, "--allow-pkg/--no-allow-pkg"),
    pip: list[str] = typer.Option(
        [], "--pip", help="Repeatable. Pre-installed pip packages."
    ),
    write_env: bool = typer.Option(True, "--write-env/--no-write-env"),
    env_path: Path = typer.Option(Path(".env"), "--env-file"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Create an Environment and (optionally) write its id to .env."""
    _setup_logging(verbose)
    backend = _backend_from_env()
    try:
        env_id = make_environment(
            backend,
            name=name,
            allowed_hosts=tuple(allowed_host),
            allow_mcp=allow_mcp,
            allow_pkg=allow_pkg,
            pip=tuple(pip),
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Environment 作成に失敗:[/red] {exc}")
        raise typer.Exit(code=1)

    _print_id_table([(ENV_KEYS["environment_id"], env_id)])
    if write_env:
        upsert_env_line(env_path, ENV_KEYS["environment_id"], env_id)
        console.print(f"[green].env updated[/green] ({env_path})")
    else:
        console.print("[yellow]Add this to your .env manually.[/yellow]")


@init_app.callback()
def init_root(
    ctx: typer.Context,
    write_env: bool = typer.Option(True, "--write-env/--no-write-env"),
    env_path: Path = typer.Option(Path(".env"), "--env-file"),
    skip_slack_check: bool = typer.Option(
        False, "--skip-slack-check", help="Skip the Slack auth.test smoke test at the end."
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Wizard: create both Agent and Environment, then sanity-check Slack auth.

    If a subcommand was invoked (``init agent`` / ``init environment``) this
    callback exits early.
    """
    if ctx.invoked_subcommand is not None:
        return

    _setup_logging(verbose)
    backend = _backend_from_env()

    # 1. Existing values?  Reuse if .env already has them.
    from dotenv import dotenv_values

    existing = dotenv_values(env_path) if env_path.exists() else {}

    rows: list[tuple[str, str]] = []

    agent_id = _real_or_none(existing.get(ENV_KEYS["agent_id"]), prefix="agent_")
    if agent_id:
        console.print(f"[blue]Reusing[/blue] {ENV_KEYS['agent_id']} = {agent_id}")
    else:
        spec = default_agent_spec()
        try:
            agent_id = make_agent_from_spec(backend, spec)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Agent 作成に失敗:[/red] {exc}")
            raise typer.Exit(code=1)
        rows.append((ENV_KEYS["agent_id"], agent_id))
        # Persist the spec so the user can edit + re-create later.
        try:
            save_agent_spec(Path("agents") / f"{spec['name']}.json", spec)
            console.print(
                f"[dim]Saved spec to agents/{spec['name']}.json — edit and re-run "
                f"with `init agent --from-file` to recreate.[/dim]"
            )
        except Exception:  # noqa: BLE001
            pass  # best-effort

    env_id = _real_or_none(existing.get(ENV_KEYS["environment_id"]), prefix="env_")
    if env_id:
        console.print(f"[blue]Reusing[/blue] {ENV_KEYS['environment_id']} = {env_id}")
    else:
        try:
            env_id = make_environment(backend)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Environment 作成に失敗:[/red] {exc}")
            raise typer.Exit(code=1)
        rows.append((ENV_KEYS["environment_id"], env_id))

    if rows:
        _print_id_table(rows)
        if write_env:
            for k, v in rows:
                upsert_env_line(env_path, k, v)
            console.print(f"[green].env updated[/green] ({env_path})")
        else:
            console.print("[yellow]Add these to your .env manually.[/yellow]")

    # 2. Optional Slack smoke test.
    if skip_slack_check:
        console.print("[dim]Slack 認証チェックをスキップ[/dim]")
    else:
        _slack_smoke_test(env_path, existing)

    console.print()
    console.print("Next: [bold]uv run kuhaku-agent doctor[/bold]")


def _slack_smoke_test(env_path: Path, dotenv_values_cache: dict) -> None:
    """Best-effort `auth.test` to catch obviously bad Slack tokens early."""
    bot_token = (
        os.environ.get(ENV_KEYS["slack_bot_token"])
        or dotenv_values_cache.get(ENV_KEYS["slack_bot_token"])
        or ""
    )
    if not bot_token:
        console.print(
            "[yellow]SLACK_BOT_TOKEN 未設定 — Slack チェックをスキップ[/yellow]"
        )
        return
    try:
        import requests

        r = requests.post(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {bot_token}"},
            timeout=10,
        )
        body = r.json()
        if body.get("ok"):
            console.print(
                f":white_check_mark: Slack auth ok — team={body.get('team')!r} "
                f"user={body.get('user')!r}"
            )
        else:
            console.print(
                f"[red]Slack auth.test failed:[/red] {body.get('error', 'unknown')}"
            )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Slack チェックでエラー:[/yellow] {exc}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
