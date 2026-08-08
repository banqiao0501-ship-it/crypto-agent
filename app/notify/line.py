"""
LINE Messaging API推播。

LINE的Push Message有兩個限制要注意：
1. 單則文字訊息長度上限約5000字，超過要切成多則
2. 一次push呼叫最多帶5則訊息

免費「輕用量」方案每月大概200~500則額度（依LINE官方公告的方案內容為準，
之後如果調整了要重新確認），對「每天一份日報+偶爾幾則警報」的用量來說非常足夠，
不用特別做流量控管。
"""
from __future__ import annotations

import httpx

from app.config import Asset
from app.utils.logger import get_logger

logger = get_logger(__name__)

_LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
_MAX_CHARS_PER_MESSAGE = 4800
_MAX_MESSAGES_PER_CALL = 5

_BIAS_EMOJI = {"bullish": "🟢", "bearish": "🔴", "neutral": "🟡"}


def _chunk_text(text: str, max_chars: int = _MAX_CHARS_PER_MESSAGE) -> list[str]:
    return [text[i : i + max_chars] for i in range(0, len(text), max_chars)] or [""]


def push_text(channel_access_token: str, user_id: str, text: str) -> None:
    chunks = _chunk_text(text)[:_MAX_MESSAGES_PER_CALL]  # 保險起見限制5則，理論上日報不會這麼長
    if len(_chunk_text(text)) > _MAX_MESSAGES_PER_CALL:
        logger.warning("報告內容過長，已截斷至前%d則訊息", _MAX_MESSAGES_PER_CALL)

    payload = {"to": user_id, "messages": [{"type": "text", "text": chunk} for chunk in chunks]}
    headers = {
        "Authorization": f"Bearer {channel_access_token}",
        "Content-Type": "application/json",
    }

    with httpx.Client() as client:
        resp = client.post(_LINE_PUSH_URL, json=payload, headers=headers, timeout=15)
        if resp.status_code != 200:
            logger.error("LINE推播失敗（status=%s）：%s", resp.status_code, resp.text)
            resp.raise_for_status()

    logger.info("LINE推播成功，共%d則訊息", len(chunks))


def format_daily_report(
    report: dict, assets: list[Asset], report_date: str, system_status: dict[str, bool],
    regime_result: dict | None = None,
) -> str:
    lines = [f"🧠 CRYPTO DAILY INTELLIGENCE", report_date, "━━━━━━━━━━━━━━━━"]

    if regime_result:
        regime_emoji = {
            "Bullish Trend": "🟢", "Bearish Trend": "🔴", "Range": "🟡",
            "High Volatility": "🟠", "Risk-off": "🔴",
        }.get(regime_result["regime"], "⚪")
        lines += [f"📊 Market Regime：{regime_emoji} {regime_result['regime']}", regime_result["reason"], ""]

    overview = report.get("market_overview", "")
    if overview:
        lines += ["🌐 市場總覽", overview, ""]

    key_events = report.get("key_events", [])
    if key_events:
        lines.append("🔥 今日關鍵事件")
        lines += [f"{i}. {event}" for i, event in enumerate(key_events, 1)]
        lines.append("")

    assets_data = report.get("assets", {})
    # 依tier排序顯示：Tier1（BTC/ETH）優先
    for asset in sorted(assets, key=lambda a: a.tier):
        data = assets_data.get(asset.symbol)
        if not data:
            continue

        lines.append(f"━━━━━━━━━━━━━━━━\n{asset.symbol}\n━━━━━━━━━━━━━━━━")

        news_summary = data.get("news_summary", "")
        if news_summary:
            lines.append(f"消息面：{news_summary}")

        bias = data.get("market_bias", {})
        if bias:
            short_e = _BIAS_EMOJI.get(bias.get("short_term", ""), "⚪")
            mid_e = _BIAS_EMOJI.get(bias.get("mid_term", ""), "⚪")
            long_e = _BIAS_EMOJI.get(bias.get("long_term", ""), "⚪")
            lines.append(
                f"Market Bias：短{short_e}{bias.get('short_term','-')} "
                f"中{mid_e}{bias.get('mid_term','-')} "
                f"長{long_e}{bias.get('long_term','-')}"
            )

        for factor in data.get("key_factors", []):
            lines.append(f"  + {factor}")
        for risk in data.get("risks", []):
            lines.append(f"  ⚠️ {risk}")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━\n⚙️ System Status")
    for name, ok in system_status.items():
        lines.append(f"{name}: {'🟢' if ok else '🔴'}")

    return "\n".join(lines)


def format_alert(alert: dict, analysis: dict) -> str:
    lines = [
        f"🚨 {alert['asset']} ALERT",
        "━━━━━━━━━━━━━━",
        f"觸發類型：{alert['trigger_type']}",
    ]
    for key, value in alert.items():
        if key in ("asset", "trigger_type"):
            continue
        lines.append(f"{key}：{value}")

    lines.append("━━━━━━━━━━━━━━")
    lines.append("可能原因：")
    for cause in analysis.get("possible_causes", []):
        lines.append(f"  • {cause}")

    lines.append(f"市場結構：{analysis.get('market_structure', '-')}")

    risks = analysis.get("risks", [])
    if risks:
        lines.append("風險：")
        for risk in risks:
            lines.append(f"  ⚠️ {risk}")

    return "\n".join(lines)
