"""
AI synthesizer：這是整個agent唯一會呼叫LLM的地方。

刻意把「呼叫AI」集中在這一個模組，而不是散落在各個collector裡——
這樣之後要換model、調整prompt、加token用量統計，都只要改這裡。

兩個對外函式：
- generate_daily_report()：每天固定時間跑一次，彙整當天所有新資料
- generate_alert_analysis()：Rule Engine觸發警報後才呼叫，只針對單一幣種、單一事件

輸出格式固定要求AI回傳純JSON（不要markdown框），這樣才能穩定parse，
不用每次都靠regex去猜AI這次想用什麼格式寫報告。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from app.config import Asset, Settings
from app.database import db
from app.utils.logger import get_logger

logger = get_logger(__name__)

MODEL = "claude-sonnet-5"
_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
_MAX_CONTENT_CHARS = 6000  # 單一則raw_content（例如逐字稿）超過這個長度就截斷，避免單支影片吃光token預算


def _load_prompt_template(filename: str) -> str:
    return (_PROMPTS_DIR / filename).read_text(encoding="utf-8")


def _call_claude(api_key: str, prompt: str, max_tokens: int = 4000) -> str:
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    logger.info(
        "Claude回應完成：stop_reason=%s, input_tokens=%s, output_tokens=%s",
        message.stop_reason, message.usage.input_tokens, message.usage.output_tokens,
    )
    return "".join(block.text for block in message.content if block.type == "text")


def _save_debug_response(raw_response: str, label: str) -> Path:
    debug_path = Path("logs") / f"debug_ai_{label}.txt"
    debug_path.parent.mkdir(parents=True, exist_ok=True)
    debug_path.write_text(raw_response, encoding="utf-8")
    return debug_path


def _parse_json_response(text: str) -> dict:
    """AI理論上會照要求輸出純JSON，但保險起見還是先去掉可能出現的```json框。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        cleaned = cleaned.rsplit("```", 1)[0]
    return json.loads(cleaned)


def _format_technical_data(technical_results: dict[str, dict[str, dict]]) -> str:
    return json.dumps(technical_results, ensure_ascii=False, indent=2)


def _format_derivative_data(conn: sqlite3.Connection, assets: list[Asset]) -> str:
    lines = []
    for asset in assets:
        asset_id = db.upsert_asset(conn, asset.symbol, asset.coingecko_id, asset.tier)
        row = db.get_latest_derivative(conn, asset_id)
        if row is None:
            lines.append(f"{asset.symbol}: 無衍生品數據")
        else:
            fr = row["funding_rate"]
            oi = row["open_interest"]
            lines.append(
                f"{asset.symbol}: funding_rate={fr if fr is not None else 'N/A'}, "
                f"open_interest={oi if oi is not None else 'N/A'} (來源:{row['source']})"
            )
    return "\n".join(lines)


def _format_raw_contents(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "（本次沒有新的原始資料）"
    parts = []
    for row in rows:
        content = row["content"] or ""
        if len(content) > _MAX_CONTENT_CHARS:
            content = content[:_MAX_CONTENT_CHARS] + "...(內容過長，已截斷)"
        parts.append(
            f"--- 來源：{row['source_name']}（可信度權重{row['source_reliability']}）"
            f"｜發布時間：{row['published_at']}｜URL：{row['url']} ---\n"
            f"標題：{row['title']}\n內容：{content}"
        )
    return "\n\n".join(parts)


def _format_regime_data(regime_result: dict | None) -> str:
    if not regime_result:
        return "（Market Regime尚未計算成功，本次分析不包含大盤狀態判斷）"
    return (
        f"整體市場狀態：{regime_result['regime']}\n"
        f"判斷依據：{regime_result['reason']}\n"
        f"（依據幣種：{'/'.join(regime_result['based_on'])}；"
        f"趨勢={regime_result['trend']}、動能={regime_result['momentum']}、"
        f"波動度={regime_result['volatility']}、成交量={regime_result['volume']}）"
    )


def _format_indexed_contents(rows: list[sqlite3.Row]) -> str:
    parts = []
    for i, row in enumerate(rows):
        content = row["content"] or ""
        if len(content) > _MAX_CONTENT_CHARS:
            content = content[:_MAX_CONTENT_CHARS] + "...(內容過長，已截斷)"
        parts.append(
            f"[{i}] 來源：{row['source_name']}｜發布時間：{row['published_at']}\n"
            f"標題：{row['title']}\n內容：{content}"
        )
    return "\n\n".join(parts)


def _compute_reliability_score(content_ids: list[int], unprocessed_rows: list[sqlite3.Row]) -> float:
    """依這個事件底下所有來源的reliability加總，上限封頂在1.0。
    這樣「多個來源互相證實」會讓分數逼近滿分，「只有單一低可信度來源」分數就會偏低——
    是加總不是取平均，故意讓「多來源互相證實」這件事本身被看見，而不是被平均掉。"""
    by_id = {row["id"]: row["source_reliability"] for row in unprocessed_rows}
    total = sum(by_id.get(cid, 0.0) for cid in content_ids)
    return round(min(total, 1.0), 2)


def extract_events(
    conn: sqlite3.Connection, settings: Settings, unprocessed_rows: list[sqlite3.Row],
) -> list[dict]:
    """Event Clustering：把這批原始資料依「是不是同一件事」分組，存進events/event_sources，
    回傳結果（含content_ids、reliability_score，方便呼叫端知道這個事件是哪幾筆raw_contents合併出來的、
    可信度多高）。

    沒有原始資料時直接回傳空list，不用浪費一次AI呼叫。
    """
    if not unprocessed_rows:
        return []

    template = _load_prompt_template("event_extraction.txt")
    prompt = template.format(indexed_contents=_format_indexed_contents(unprocessed_rows))

    logger.info("呼叫Claude做事件去重（輸入%d筆原始資料）", len(unprocessed_rows))
    raw_response = _call_claude(settings.anthropic_api_key, prompt, max_tokens=6000)

    try:
        parsed = _parse_json_response(raw_response)
    except json.JSONDecodeError as exc:
        debug_path = _save_debug_response(raw_response, "event_extraction")
        logger.error("事件去重AI回應無法解析成JSON：%s\n完整原始回應已存到：%s", exc, debug_path)
        raise

    events = parsed.get("events", [])
    created_at = datetime.now(timezone.utc).isoformat()

    for event in events:
        indices = event.get("source_indices", [])
        content_ids = [
            unprocessed_rows[i]["id"] for i in indices if 0 <= i < len(unprocessed_rows)
        ]
        event["content_ids"] = content_ids
        event["reliability_score"] = _compute_reliability_score(content_ids, unprocessed_rows)

        event_id = db.insert_event(
            conn,
            event_key=event.get("event_key", ""),
            title=event.get("title", ""),
            summary=event.get("summary", ""),
            category=event.get("category", ""),
            impact=event.get("impact", ""),
            sentiment=event.get("sentiment", ""),
            related_assets=json.dumps(event.get("related_assets", []), ensure_ascii=False),
            reliability_score=event["reliability_score"],
            created_at=created_at,
        )
        for content_id in content_ids:
            db.insert_event_source(conn, event_id=event_id, content_id=content_id)

    logger.info("事件去重完成：%d筆原始資料 → %d個事件", len(unprocessed_rows), len(events))
    return events


def _format_events_data(events: list[dict]) -> str:
    if not events:
        return "（本次沒有新的事件）"
    # 可信度高的事件排前面，讓AI優先看到比較可靠的資訊
    sorted_events = sorted(events, key=lambda e: e.get("reliability_score", 0), reverse=True)
    parts = []
    for event in sorted_events:
        assets = "、".join(event.get("related_assets", [])) or "（無明確關聯幣種）"
        score = event.get("reliability_score", 0)
        confidence_label = "高（來源本身可信度高，或多方互相證實）" if score >= 0.8 else (
            "中" if score >= 0.5 else "低（僅單一低可信度來源，建議謹慎引用）"
        )
        parts.append(
            f"事件：{event.get('title', '')}\n"
            f"摘要：{event.get('summary', '')}\n"
            f"分類：{event.get('category', '')}｜重要程度：{event.get('impact', '')}｜"
            f"傾向：{event.get('sentiment', '')}｜關聯幣種：{assets}｜"
            f"來源數量：{len(event.get('content_ids', []))}｜"
            f"可信度分數：{score}（{confidence_label}）"
        )
    return "\n\n".join(parts)


def generate_daily_report(
    conn: sqlite3.Connection,
    settings: Settings,
    technical_results: dict[str, dict[str, dict]],
    regime_result: dict | None = None,
) -> dict:
    unprocessed = db.get_unprocessed_contents(conn)
    events = extract_events(conn, settings, unprocessed)

    template = _load_prompt_template("daily_report.txt")
    prompt = template.format(
        technical_data=_format_technical_data(technical_results),
        derivative_data=_format_derivative_data(conn, settings.assets),
        regime_data=_format_regime_data(regime_result),
        raw_contents=_format_events_data(events),
    )

    logger.info("呼叫Claude產生每日報告（納入%d個去重後的事件）", len(events))
    raw_response = _call_claude(settings.anthropic_api_key, prompt, max_tokens=16000)

    try:
        report = _parse_json_response(raw_response)
    except json.JSONDecodeError as exc:
        debug_path = _save_debug_response(raw_response, "daily_report")
        logger.error("AI回傳的內容無法解析成JSON：%s\n完整原始回應已存到：%s", exc, debug_path)
        raise

    if unprocessed:
        db.mark_contents_processed(conn, [row["id"] for row in unprocessed])

    return report


def generate_alert_analysis(
    conn: sqlite3.Connection,
    settings: Settings,
    alert: dict,
    technical_results: dict[str, dict[str, dict]],
) -> dict:
    symbol = alert["asset"]
    recent_rows = [
        row for row in db.get_unprocessed_contents(conn)
    ]  # V1簡化版：直接拿目前尚未彙整進日報的內容當作「最近相關資料」

    template = _load_prompt_template("alert_analysis.txt")
    prompt = template.format(
        asset=symbol,
        trigger_type=alert["trigger_type"],
        trigger_data=json.dumps({k: v for k, v in alert.items() if k not in ("asset", "trigger_type")}, ensure_ascii=False),
        technical_data=json.dumps(technical_results.get(symbol, {}), ensure_ascii=False, indent=2),
        recent_contents=_format_raw_contents(recent_rows),
    )

    logger.info("呼叫Claude分析警報：%s %s", symbol, alert["trigger_type"])
    raw_response = _call_claude(settings.anthropic_api_key, prompt, max_tokens=1500)

    try:
        return _parse_json_response(raw_response)
    except json.JSONDecodeError as exc:
        debug_path = _save_debug_response(raw_response, "alert_analysis")
        logger.error("警報分析AI回應無法解析成JSON：%s\n完整原始回應已存到：%s", exc, debug_path)
        raise
