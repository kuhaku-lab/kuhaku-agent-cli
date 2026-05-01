# `kuhaku-agent init` — design notes

Spec for the `init` subcommand family. Implementation lives in `cli.py` once approved.

## Goal

Replace the ad-hoc one-liner Python scripts users run to create an Agent and Environment with a first-class CLI command. The user should be able to go from a fresh `.env` (only API key + Slack tokens) to a working bot without ever writing code or visiting the Console.

Vault creation stays out of scope — that's still Console-only because of OAuth flow complexity. The bot does not require a vault to start, so this is fine.

## Commands

```
kuhaku-agent init                   # interactive wizard, runs both subcommands below
kuhaku-agent init agent             # create only the Agent
kuhaku-agent init environment       # create only the Environment
kuhaku-agent init --print-only      # show what would be created, exit without API calls
```

### `init agent` — flags

| Flag | Default | Notes |
|---|---|---|
| `--name` | `kuhaku-agent` | Agent display name |
| `--model` | `claude-sonnet-4-6` | Latest at time of writing; bump in code, not in user config |
| `--system-file` | `None` | Path to a markdown file used as the system prompt; falls back to `--system` or built-in default |
| `--system` | (built-in default) | Inline system prompt |
| `--write-env / --no-write-env` | `--write-env` | If true, append `KUHAKU_AGENT_ID=` to `.env`. Idempotent — replace any existing value |

### `init environment` — flags

| Flag | Default | Notes |
|---|---|---|
| `--name` | `kuhaku-agent-env` | Environment display name |
| `--allowed-host` | `hooks.slack.com` (repeatable) | Adds to `networking.allowed_hosts` |
| `--allow-mcp / --no-allow-mcp` | `--allow-mcp` | `networking.allow_mcp_servers` |
| `--allow-pkg / --no-allow-pkg` | `--allow-pkg` | `networking.allow_package_managers` |
| `--pip` | `[]` (repeatable) | pre-installed pip packages |
| `--write-env / --no-write-env` | `--write-env` | Append `KUHAKU_ENVIRONMENT_ID=` to `.env` |

### `init` (no subcommand) — interactive wizard

Order: API auth check → agent → environment → vault selection (existing only) → final summary. Skips creation for any value already present in `.env` and prints "reusing X" instead.

## Defaults

### Built-in default system prompt

Short, generic, and friendly. Living spec — keep it under 1 KB so first-time users don't get distracted by content they didn't write themselves:

```
あなたは社内アシスタントの Slack ボットです。

- ユーザーの質問に簡潔に答えてください。絵文字は控えめに。
- 不確かなときは推測せず、確認手段を提案してください。
- 機密情報・ハラスメント・法的判断は安易に答えず、人間の担当者に振ってください。
```

User overrides via `--system-file path/to/prompt.md` once they want something custom.

### Default Environment config

```python
{
    "type": "cloud",
    "networking": {
        "type": "limited",
        "allowed_hosts": ["hooks.slack.com"],
        "allow_mcp_servers": True,
        "allow_package_managers": True,
    },
    "packages": {"type": "packages", "pip": []},
}
```

We start from a minimal sandbox and let the user expand via flags. Don't pre-install pandas / openpyxl by default — most workflows don't need them and the cold start gets longer.

## .env writing rules

- Read the file as text, parse it line by line (don't use `dotenv_values` which loses comments).
- Locate any line matching `^<KEY>=` and replace it. If absent, append.
- Preserve a trailing newline. Don't reorder unrelated lines.
- If `.env` doesn't exist, create it with the new key only (no template content).

A small helper `_upsert_env_line(path, key, value)` lives next to the init code; same helper used by `init agent` and `init environment`.

## Idempotency

`init agent` always creates a *new* agent. We don't try to detect duplicates by name because Console allows duplicates and the user might have multiple test agents. The interactive `init` wizard does the duplicate avoidance: it checks `.env` first and asks "reuse `agent_xxx`?" before creating.

Same logic for environment.

## Output

Use `rich` to print a small confirmation table:

```
Created
┏━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ KUHAKU_AGENT_ID         ┃ agent_01ABC...                          ┃
┃ KUHAKU_ENVIRONMENT_ID   ┃ env_01XYZ...                            ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━┻━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
.env updated.
Next: uv run kuhaku-agent doctor
```

If `--no-write-env`, swap the trailing line for `Add these to your .env manually.`

## Errors

- API auth failure → exit 2 with the same hint as `doctor` (`uv run kuhaku-agent doctor`).
- API permission failure ("agents.create not allowed") → print the SDK error verbatim plus a pointer: "Your API key may not have Managed Agents access. Check console.anthropic.com → Settings."
- Network / 5xx → suggest retry. No automatic retry inside the command (a single API call, easier to reason about if it fails fast).

## Out of scope

- Vault creation. Direct user to Console with a printed link if they ask for it.
- Agent skill management (attaching `0x-handbook` etc.). Skill IDs change per workspace; let the user attach via Console after `init agent` succeeds.
- Multiple environments. Always create one. If the user wants more, they can run `init environment --name foo` repeatedly.

## Implementation notes (for the actual edit)

- New file: `src/kuhaku_agent/init_ops.py`
  - `create_agent(backend, name, model, system) -> str`
  - `create_environment(backend, name, allowed_hosts, allow_mcp, allow_pkg, pip) -> str`
  - `upsert_env(path, key, value) -> None`
- New typer sub-app under `cli.py`:
  ```python
  init_app = typer.Typer(help="Bootstrap Anthropic resources.")
  app.add_typer(init_app, name="init")
  init_app.command("agent")(...)
  init_app.command("environment")(...)
  ```
- Wizard mode (no subcommand) calls both functions in order. Use `typer.prompt` for confirmations.
- Tests (later, not now):
  - `_upsert_env_line` round-trip on a tempfile
  - Default config dict shape (frozen snapshot)
- Wire `Backend` to expose `create_agent` / `create_environment` thin wrappers so `init_ops` doesn't reach into `backend._client` directly.

## Open questions

- Should `init` prompt to *also* call Slack `auth.test` and verify Bolt tokens before exiting? Probably yes — the typical user runs `init` then `serve` and should learn about a bad Slack token sooner. Add as a final step in the wizard, optional via `--skip-slack-check`.
- Do we need a `kuhaku-agent reset` to delete the created agent/env? Probably not — Console handles deletion fine. Don't add until requested.
