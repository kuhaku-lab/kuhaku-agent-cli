# kuhaku-agent-cli

Slack ↔ Claude Managed Agents をつなぐ Python 製ブリッジ。Slack で bot を @ メンションすると、そのメッセージが Managed Agents セッションの 1 ターンになり、応答が `chat.startStream`（非対応時は `chat.update` フォールバック）で Slack に流れます。スレッドごとに 1 セッションが対応します。

Vault / OAuth / MCP の credential 管理は **このリポジトリの責務外** です。すべて [Anthropic Console](https://console.anthropic.com) 側で行ってください。本 CLI は既存の Agent / Environment / Vault を参照するだけで、トークン更新は Anthropic 側が自動で行います。

> English version: [README.en.md](README.en.md)

## 必要環境

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

## インストール

```bash
uv sync
uv run kuhaku-agent --version
```

## セットアップ

### 1. 必要トークンを揃える

| 取得先 | 値 |
|---|---|
| https://console.anthropic.com | `ANTHROPIC_API_KEY` |
| https://api.slack.com/apps の対象 App → OAuth & Permissions | `SLACK_BOT_TOKEN`（`xoxb-...`） |
| 同 App → Basic Information → App-Level Tokens（scope: `connections:write`） | `SLACK_APP_TOKEN`（`xapp-...`） |

### 2. `.env` を作成

```bash
cp env.example .env   # ファイルがない場合は手動で作成
```

`.env` の中身（最低限）:

```dotenv
ANTHROPIC_API_KEY=sk-ant-...
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
```

### 3. Agent / Environment を初期化

`kuhaku-agent init` を実行すると、Managed Agent と Environment を自動作成し、出力された ID を `.env` に追記します。Slack トークンの疎通も最後にチェックします。

```bash
uv run kuhaku-agent init
```

実行後の `.env` には次の 2 行が増えています:

```dotenv
KUHAKU_AGENT_ID=agent_...
KUHAKU_ENVIRONMENT_ID=env_...
```

#### `init` のオプション

```bash
# 個別作成（ID をコピー貼付したい場合）
uv run kuhaku-agent init agent
uv run kuhaku-agent init environment

# システムプロンプトを差し替え
uv run kuhaku-agent init agent --system-file prompts/my-system.md
uv run kuhaku-agent init agent --system "あなたは…"

# Environment のサンドボックスを広げる
uv run kuhaku-agent init environment \
    --allowed-host hooks.slack.com \
    --allowed-host api.notion.com \
    --pip pandas --pip openpyxl

# .env に書かず標準出力だけにする
uv run kuhaku-agent init --no-write-env

# 末尾の Slack 認証チェックを省略
uv run kuhaku-agent init --skip-slack-check
```

`init`（サブコマンドなし）は wizard モードです。`.env` に既に `KUHAKU_AGENT_ID` がある場合は再利用し、足りない方だけ作成します。`agents/<name>.json` に作成時の spec を自動保存するので、後から内容を確認・差分管理できます。

#### Agent spec を JSON で管理する

複雑な Agent（MCP サーバー / ツール / Skill 入り）を再現可能な形で作りたい場合は、JSON spec ファイル経由で作成できます。

```bash
# 1. デフォルトテンプレートを出力
uv run kuhaku-agent init agent --template-out agents/0xbot.json

# 2. agents/0xbot.json を編集（mcp_servers / tools / skills / metadata を追記）

# 3. 編集した spec で Agent を作成
uv run kuhaku-agent init agent --from-file agents/0xbot.json
```

spec のフィールド構造（Anthropic SDK の `agents.create` payload 互換）:

```json
{
  "name": "0xbot",
  "description": "0X 株式会社の社内 Slack アシスタント",
  "model": { "id": "claude-sonnet-4-6", "speed": "standard" },
  "system": "あなたは 0X 株式会社の社内アシスタント Slack ボット…",
  "mcp_servers": [
    { "name": "slack",  "url": "https://mcp.slack.com/mcp",  "type": "url" },
    { "name": "notion", "url": "https://mcp.notion.com/mcp", "type": "url" }
  ],
  "tools": [
    { "type": "agent_toolset_20260401",
      "default_config": { "enabled": true, "permission_policy": { "type": "always_allow" } } },
    { "type": "mcp_toolset", "mcp_server_name": "slack",
      "default_config": { "enabled": true, "permission_policy": { "type": "always_ask" } } },
    { "type": "mcp_toolset", "mcp_server_name": "notion",
      "default_config": { "enabled": true, "permission_policy": { "type": "always_ask" } } }
  ],
  "skills": [
    { "skill_id": "skill_01ABC...", "type": "custom", "version": "latest" }
  ],
  "metadata": { "owner": "people-ops", "version": "0.1.0" }
}
```

簡易フラグで作成しつつ spec も残したい場合は `--save-spec` を併用:

```bash
uv run kuhaku-agent init agent \
    --name 0xbot \
    --system-file prompts/0xbot.md \
    --save-spec agents/0xbot.json
```

### 4. Vault（任意）

MCP（Slack / Notion など）を使う場合は、Anthropic Console で Vault を作成し、credential を OAuth で追加してください。発行された Vault ID を `.env` に:

```dotenv
KUHAKU_VAULT_IDS=vault_a,vault_b
```

> Vault の作成・credential の追加は Console 専用です。本 CLI からは作成できません（OAuth フローが Anthropic 側でホストされるため）。

### 5. スレッド永続化

Slack スレッド ↔ Managed Agents セッションのマッピングは JSON ファイルに保存され、`serve` を再起動しても同じスレッドで会話履歴が継続します。**デフォルトで有効** で、保存先は cwd 直下の `.kuhaku/threads.json` です。`.gitignore` に追加することを推奨します。

保存先を変えたい場合は `.env` に:

```dotenv
KUHAKU_THREAD_STORE_PATH=~/.kuhaku-agent/threads.json
```

履歴をリセットしたいときは該当ファイルを削除すれば、次回以降のメンションは新規セッションから始まります。

### 6. 動作確認

```bash
uv run kuhaku-agent doctor      # 設定 + API 疎通チェック
uv run kuhaku-agent vaults      # 利用可能な Vault と credential を一覧表示
```

## 起動

```bash
uv run kuhaku-agent serve
```

Slack で bot を招待したチャンネルから `@kuhaku-agent 質問内容` でメンションすると応答が始まります。スレッド内で続けて返信するとマルチターン会話になります。

## Slack App の Assistant 化（推奨）

`chat.startStream`（plan-mode）で滑らかな進捗表示と spinner を有効にするため、Slack App を **Agent / Assistant** として設定してください。

1. https://api.slack.com/apps → 対象 App → **Agents & AI Apps** タブ → **Turn on**
2. **OAuth & Permissions** の Bot Token Scopes に `assistant:write` を追加
3. **Reinstall to Workspace** で再インストール → 新しい `xoxb-...` を `.env` の `SLACK_BOT_TOKEN` に反映
4. `uv run kuhaku-agent serve` を再起動

未設定でも動きますが、フォールバック（`chat.update`）になり animation が初回 delta で止まります。

ログで `chat.startStream(plan) ok ts=...` が出れば成功、`chat.startStream unavailable, using post+update: ...` が出ていればフォールバック中です。

## ツール承認フロー

Agent spec で MCP ツールに `permission_policy.type = "always_ask"` を設定すると、ツール実行前にエージェントが一時停止 (`session.status_idle / requires_action`) し、Slack スレッドに **Block Kit の「承認 / 拒否」ボタン**が投稿されます。

操作者がクリックすると bot が `user.tool_confirmation` イベントを SDK 経由で送信、セッションが再開して同じ Reply にツール出力が流れます。

- 承認待ちの間も plan area に「Awaiting approval」タスクが in_progress で表示され、承認後は「Running tool」に切り替わります
- 拒否の場合はエージェントが代替案を試行（あるいは終了）
- 操作者が長時間放置した場合の自動失効は未実装（プロセス再起動で承認状態は失われます）

検証フェーズで承認 UI が邪魔なら spec 側で `always_allow` に切り替えてください。詳細は `.claude/skills/kuhaku-agent-dev/references/approval-flow.md`。

## 画像添付

Slack で bot をメンションする際に画像（PNG / JPEG / GIF / WebP）を添付すると、その内容を含めて Agent に渡されます。レシート OCR、スクショ解説、図面読み取り等に使えます。

### 必要な Slack scope

Bot Token Scopes に **`files:read`** が必須です。これが無いと `url_private` から HTML 認証エラーが返り、Anthropic が `Could not process image` で落ちます。

1. https://api.slack.com/apps → 対象 App → **OAuth & Permissions**
2. Bot Token Scopes に **`files:read`** を追加
3. **Reinstall to Workspace** → 新 `xoxb-...` を `.env` の `SLACK_BOT_TOKEN` に反映

### 必要な Anthropic 側

- Agent の `model.id` が **vision 対応**（Sonnet 4.x / Opus 4.x 系）
- Sonnet 3 系は image content block を受け付けません

### 制限

- 1 ファイル最大 **20 MiB**（超えると surface ログで warning + そのファイルだけスキップ）
- base64 inline 送信なので、巨大画像 / 多量画像はリクエストサイズが膨らみます
- magic byte 判定で本物の画像でなければ ERROR ログを出して破棄（誤検知の自動防御）

詳細は `.claude/skills/kuhaku-agent-dev/references/image-attachments.md`。

## CLI コマンド一覧

```
kuhaku-agent --version
kuhaku-agent doctor                    # 設定検証 + 接続確認
kuhaku-agent vaults                    # Vault と credential 一覧
kuhaku-agent init                      # Agent + Environment 作成 (wizard)
kuhaku-agent init agent                # Agent のみ作成
kuhaku-agent init environment          # Environment のみ作成
kuhaku-agent serve                     # Slack listener 起動 (-v で verbose)
```

## アーキテクチャ

```
src/kuhaku_agent/
├── backend.py           # Anthropic SDK ラッパー (sessions / vaults / files / agents / envs)
├── coordinator.py       # 1 メンション → 1 ストリーミング応答、5 phase
├── events.py            # SSE イベント → Beat (Say / Tool / Stage / Hiccup / Done / RequiresAction)
├── thread_store.py      # thread_key → session_id の map（RLock、JSON 永続化対応）
├── settings.py          # 3 段階設定: CLI > os.environ > .env
├── init_ops.py          # init コマンドのバックエンド (upsert_env_line ほか)
├── runner.py            # build_runtime + serve()
├── cli.py               # typer エントリポイント
└── surfaces/
    ├── base.py          # Surface ABC, Inbound, Reply, Step, ToolDecision
    └── slack/
        ├── surface.py     # Bolt Socket Mode + Block Kit 承認 UI
        ├── streamer.py    # SlackReply: chat.startStream + heartbeat + フォールバック
        └── diagnostics.py # Hiccup → ユーザー向け Slack メッセージ
```

開発時の規約は `CLAUDE.md` を参照してください。Surface 追加・MCP エラー対処・承認フロー・init 拡張などの詳細レシピは `.claude/skills/kuhaku-agent-dev/references/` 配下にあります。

## 新しい Surface を足すには

1. `kuhaku_agent.surfaces.base.Surface` を継承
2. `start / stop / listen / post / open_reply` を実装
3. 返す `Reply` は別スレッドからの呼び出しに耐える設計に（`SlackReply` の単一ワーカーキューが参考実装）
4. `runner.py:build_runtime` から組み込む

詳細は `.claude/skills/kuhaku-agent-dev/references/adding-surface.md`。

## License

Apache License 2.0 — see [`LICENSE`](LICENSE).

### Fork / 派生物の表記義務

本リポジトリを fork、改変、再配布する場合は **Apache 2.0 の条件に加えて、本プロジェクト（`kuhaku-agent-cli`）から派生したことを明示**してください。詳細は [`NOTICE`](NOTICE) を参照。

具体的には:

- README（または同等の最上位ドキュメント）に「Forked from `kuhaku-agent-cli`（リンク付き）」を記載
- 元の `NOTICE` ファイルを保持しつつ、自分側の派生情報を追記
- これらに加えて、Apache 2.0 §4 の通常義務（`LICENSE` 同梱、変更ファイルへの記載、著作権通知保持）を満たすこと

`NOTICE` の attribution セクションを削除または不可視にすることは Apache 2.0 §4(c) および本プロジェクトの追加条項違反になります。
