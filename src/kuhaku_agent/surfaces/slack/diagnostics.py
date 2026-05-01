"""Translate ``Hiccup`` errors into Slack-friendly markdown text.

Slack-specific because we use mrkdwn formatting (``*bold*``, code fences) and
emoji shortcodes.
"""
from __future__ import annotations

from ...events import Hiccup


def slack_diagnoser(h: Hiccup) -> str:
    """Render a ``Hiccup`` into a multi-line Slack message.

    Special-cases the most common Managed Agents failures (MCP connect / token
    invalidation) with concrete remediation steps.
    """
    server = h.server or ""
    server_label = f"*{server}*" if server else "MCP サーバー"

    if h.kind == "mcp_connection_failed_error":
        return "\n".join(
            [
                f":lock: {server_label} の接続に失敗しました",
                f"```{h.detail}```",
                "復旧手順:",
                "1. <https://console.anthropic.com|Anthropic Console> で対象 Vault を開く",
                f"2. {server_label} の credential を再認可（OAuth フロー）",
                "3. このスレッドでもう一度メンション",
            ]
        )

    detail_lower = (h.detail or "").lower()
    if "credential" in detail_lower and (
        "invalid" in detail_lower or "expired" in detail_lower
    ):
        return (
            f":lock: 認証情報が失効しています ({server_label})\n"
            f"```{h.detail}```\n"
            "Anthropic Console で再認可してください: https://console.anthropic.com"
        )

    return f":x: エージェントエラー [{h.kind}]: {h.detail}"
