# Troubleshooting

Recipes for the failure modes you'll actually hit. Each entry follows the pattern: symptom → likely cause → fix.

## `MCP server '<name>' initialize failed: HTTP 400`

**Surfaced as:** Slack message from `slack_diagnoser` saying "認証情報が失効しています" or a generic 400.

**Cause:** The vault credential for `<name>` is missing, expired, or was created for a different OAuth app than the MCP server expects.

**Fix:**

1. `uv run kuhaku-agent vaults` — find the vault id and inspect the credential's `status`.
2. Open https://console.anthropic.com → that vault → re-authorize the failing credential.
3. If the vault id changed (you re-created it), update `KUHAKU_VAULT_IDS` in `.env`.
4. No code changes. The bridge intentionally never re-creates vaults.

If `vaults` shows no credentials at all for the failing server, the user added the wrong `KUHAKU_VAULT_IDS`. Pick a vault that does contain that credential or add the credential in Console.

## `auth.expires_at must be in the future`

**Cause:** Older versions of this codebase wrote `expires_at` to vault credentials directly. The current code never does — if you see this error, you're running an old `setup.py` that was deleted. Re-pull the repo and use the Console.

## Stream stalls after the placeholder ":hourglass_flowing_sand: 考え中…"

**Cause:** The agent is taking a long time to emit its first text block (long thinking, tool use, slow MCP). The user only sees the placeholder until the first `Say` beat arrives.

**Fix options:**

- Promote `Tool` events to a visible status update earlier (already happens via `show_steps`).
- Lower the model latency by trimming the system prompt or moving heavy tool calls behind a faster intent classifier.
- This is rarely a code bug; it's almost always a slow agent.

## "Reply ends mid-sentence with no stop, agent reports awaiting approval"

**Symptom:** Slack message shows the agent narrating its plan ("Slack Canvas を作成します！" or similar) and then nothing more — Slack-side renders no Canvas / send / mutation. The session in the Anthropic Console reads "awaiting approval" / `requires_action`.

**Cause:** The toolset has `permission_policy.type = "always_ask"`. The agent emitted `agent.tool_use` (or `agent.mcp_tool_use`) and the session went `idle` with `stop_reason.type = "requires_action"`. Until a `user.tool_confirmation` is sent for every blocked `tool_use_id`, the agent does not execute the tool.

**Fix options:**

- Quick: change `permission_policy.type` to `always_allow` in `agents/<name>.json` and re-run `kuhaku-agent init agent --from-file agents/<name>.json`. Acceptable for dev/staging.
- Proper: implement the approval flow — see `references/approval-flow.md`. Adds a `RequiresAction` beat, Slack Block Kit buttons, and a `Backend.confirm_tool_use` helper.

If the bridge "feels" stuck but the Console says `running`, it's not this — investigate `_stream` instead.

## `mcp_authentication_failed_error: no credential is stored for this server URL`

**Cause:** The Agent's `mcp_servers[].url` does not match the URL stored on the Vault credential **byte-for-byte**. Common offenders: trailing slash, scheme (`http` vs `https`), subdomain (`mcp.notion.com` vs `api.notion.com`), or path component.

**Fix:**

1. `grep -n '"url"' agents/*.json` — read the Agent-side URL.
2. Anthropic Console → the relevant Vault → open the failing credential → copy the displayed Server URL.
3. Make them identical. Either edit `agents/<name>.json` and re-run `kuhaku-agent init agent --from-file ...`, or recreate the credential in Console with the URL the Agent expects.
4. `uv run kuhaku-agent vaults` to confirm the credential lists under the right vault id and status is `active`.

## `chat.startStream` always falls back to `chat.update`

**Cause:** The Slack workspace doesn't have the assistant streaming feature enabled, or the bot's scopes don't include it.

**Fix:**

1. Confirm with `uv run kuhaku-agent doctor` that auth is healthy.
2. Check the Bolt app's OAuth scopes — `chat.startStream` requires the assistant scopes.
3. If the workspace can't get those scopes, the fallback path is fine. Read `streamer.py:_open_message`'s catch path to confirm.

## `KuhakuKey not found` style settings errors

`Settings.load()` raises `SettingsError` listing every missing env var. The CLI prints them; the bot exits with code 2 from `cli.serve`. Re-check `.env` against `env.example`.

## "Bot doesn't reply at all in a channel"

Run through this checklist:

1. Bot user invited to the channel? `/invite @kuhaku-agent` in Slack.
2. Bot mentioned with `<@UXXXX>`? The bot only listens to `app_mention` events.
3. `SLACK_APP_TOKEN` starts with `xapp-` and has `connections:write` scope?
4. `kuhaku-agent serve` log shows "authenticated as user=…" on startup?
5. The Bolt thread is alive (no Python traceback in stderr)?

## Adding a new event type the SDK started emitting

If the SDK adds a new event type (say `agent.handoff`), you'll see it ignored — `parse_event` returns an empty `ParsedFrame` for unknown types by design. To surface it:

1. Add a new `Beat` variant in `events.py` (`Handoff`, etc.) and a case in `parse_event`.
2. Handle the new variant in `Coordinator._stream`. Avoid leaking SDK types into the Coordinator.
3. If the variant is purely informational, route it through `Stage` instead of inventing a new beat.

## Migrating away from `KUHAKU_*` env vars

Don't, unless the project is being rebranded. The prefix is the only signal that these settings belong to this CLI and not some other Claude tool.
