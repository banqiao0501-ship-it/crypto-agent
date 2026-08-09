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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic

from app.analysis import event_reactions
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
            fr, oi = row["funding_rate"], row["open_interest"]
            mark, index = row["mark_price"], row["index_price"]
            lines.append(
                f"{asset.symbol}: funding_rate={fr if fr is not None else 'N/A'}, "
                f"open_interest={oi if oi is not None else 'N/A'}, "
                f"mark_price={mark if mark is not None else 'N/A'}, "
                f"index_price={index if index is not None else 'N/A'} (來源:{row['source']})"
            )
    return "\n".join(lines)


def _format_market_context(conn: sqlite3.Connection, assets: list[Asset]) -> str:
    """CoinGecko的市場背景資料（市值、排名、流通量），純粹是額外脈絡，不是技術分析依據。"""
    lines = []
    for asset in assets:
        asset_id = db.upsert_asset(conn, asset.symbol, asset.coingecko_id, asset.tier)
        row = db.get_latest_market_context(conn, asset_id)
        if row is None:
            lines.append(f"{asset.symbol}: 無市場背景資料")
            continue
        cap = row["market_cap_usd"]
        rank = row["market_cap_rank"]
        circ = row["circulating_supply"]
        cap_text = f"${cap/1e9:.1f}B" if cap is not None else "N/A"
        lines.append(
            f"{asset.symbol}: Market Cap={cap_text}, Rank=#{rank if rank is not None else 'N/A'}, "
            f"流通量={circ if circ is not None else 'N/A'}"
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

        event_reactions.create_reaction_jobs(
            conn, event_id=event_id, event_time=datetime.fromisoformat(created_at),
            related_assets=event.get("related_assets", []), impact=event.get("impact", ""),
        )

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


def _format_event_reactions(conn: sqlite3.Connection) -> str:
    """把過去24小時內完成的事件反應結算資料，整理給AI「解讀」——AI只負責讀懂這些已經算好的
    數字代表什麼，不負責重新計算。"""
    since_iso = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    rows = db.get_recent_event_reactions(conn, since_iso)
    if not rows:
        return "（過去24小時沒有事件反應結算資料）"

    by_event: dict[int, list] = {}
    for row in rows:
        by_event.setdefault(row["event_id"], []).append(row)

    parts = []
    for event_id, reactions in by_event.items():
        title = reactions[0]["event_title"]
        lines = [f"事件：{title}"]
        for r in sorted(reactions, key=lambda x: x["window"]):
            excess = r["excess_return"]
            excess_text = f"{excess*100:+.2f}%" if excess is not None else "N/A"
            vol = r["volume_ratio"]
            vol_text = f"{vol:.1f}x" if vol is not None else "N/A"
            lines.append(
                f"  {r['asset']} {r['window']}：excess_return={excess_text}, "
                f"volume={vol_text}, 分類={r['reaction_type']}"
            )
        parts.append("\n".join(lines))
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
        market_context=_format_market_context(conn, settings.assets),
        regime_data=_format_regime_data(regime_result),
        event_reactions=_format_event_reactions(conn),
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


_YOUTUBE_TRANSCRIPT_MAX_CHARS = 8000  # 即時摘要用，比日報的_MAX_CONTENT_CHARS更小，讓即時推播更快回來


_HORIZON_DAYS = {"short_term": 3, "mid_term": 14, "long_term": 60}


def _select_atr_timeframe(horizon_days: int) -> str:
    """依照predict的時間長度選擇要用哪個timeframe的ATR當基準——短天期預測用短timeframe的波動度，
    長天期預測用長timeframe的波動度，比全部都用同一個timeframe合理（跟使用者定案的V1設計一致）。"""
    return "4h" if horizon_days <= 3 else "1d"


def _get_reference_and_atr(conn: sqlite3.Connection, asset_id: int, horizon_days: int) -> dict:
    """在prediction建立當下，把「現在的價格」跟「現在的ATR」凍結下來，之後結算永遠用這組數字，
    不會因為之後ATR變了就跟著變——這是避免look-ahead bias的關鍵。"""
    timeframe = _select_atr_timeframe(horizon_days)
    tech_snapshot = db.get_latest_technical(conn, asset_id, timeframe)
    price_snapshot = db.get_market_snapshot_near(
        conn, asset_id, timeframe, datetime.now(timezone.utc).isoformat(),
    )

    if tech_snapshot is None or tech_snapshot["atr"] is None or price_snapshot is None:
        return {
            "reference_price": None, "atr_value": None, "atr_percent": None,
            "atr_timeframe": timeframe, "threshold": None,
        }

    reference_price = price_snapshot["close"]
    atr_value = tech_snapshot["atr"]
    atr_percent = atr_value / reference_price if reference_price else None
    return {
        "reference_price": reference_price, "atr_value": atr_value,
        "atr_percent": atr_percent, "atr_timeframe": timeframe, "threshold": atr_percent,
    }


def _safe_parse_iso(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


def extract_youtube_claims(
    conn: sqlite3.Connection, settings: Settings, *,
    content_id: int, source_name: str, title: str, transcript: str, published_at: str,
) -> list[str]:
    """對單一支新影片做結構化claim抽取（P2-1）。

    回傳bullets給即時LINE推播用；claims全部存進kol_claims（給Daily Report/敘事追蹤參考）；
    其中verifiable=true且time_horizon有明確值的，另外存一筆進kol_predictions，
    等P2-2的評估引擎（還沒做）到期時去BingX撈價格結算。
    """
    content = transcript
    if len(content) > _YOUTUBE_TRANSCRIPT_MAX_CHARS:
        content = content[:_YOUTUBE_TRANSCRIPT_MAX_CHARS] + "...(內容過長，已截斷)"

    template = _load_prompt_template("youtube_claims.txt")
    prompt = template.format(title=title, transcript=content)

    logger.info("呼叫Claude做結構化claim抽取：%s", title)
    raw_response = _call_claude(settings.anthropic_api_key, prompt, max_tokens=16000)

    try:
        parsed = _parse_json_response(raw_response)
    except json.JSONDecodeError as exc:
        debug_path = _save_debug_response(raw_response, "youtube_claims")
        logger.error("Claim抽取AI回應無法解析成JSON：%s\n完整原始回應已存到：%s", exc, debug_path)
        raise

    created_at = published_at.strip() if published_at else datetime.now(timezone.utc).isoformat()
    prediction_time = _safe_parse_iso(created_at)

    for claim in parsed.get("claims", []):
        conditions = claim.get("conditions") or {}
        claim_id = db.insert_kol_claim(
            conn,
            content_id=content_id, source_name=source_name,
            asset=claim.get("asset", ""), claim_type=claim.get("claim_type", ""),
            direction=claim.get("direction", ""), time_horizon=claim.get("time_horizon", "unspecified"),
            claim_text=claim.get("claim_text", ""), confidence=claim.get("confidence", ""),
            entry_zone_low=conditions.get("entry_zone_low"), entry_zone_high=conditions.get("entry_zone_high"),
            invalidation_price=conditions.get("invalidation"), target_price=conditions.get("target"),
            verifiable=bool(claim.get("verifiable")), unverifiable_reason=claim.get("unverifiable_reason", ""),
            source_timestamp=claim.get("source_timestamp", ""), created_at=created_at,
        )

        horizon = claim.get("time_horizon")
        if claim.get("verifiable") and horizon in _HORIZON_DAYS:
            horizon_days = _HORIZON_DAYS[horizon]
            deadline = prediction_time + timedelta(days=horizon_days)

            asset_symbol = claim.get("asset", "")
            asset_cfg = next((a for a in settings.assets if a.symbol == asset_symbol), None)
            atr_info = {
                "reference_price": None, "atr_value": None, "atr_percent": None,
                "atr_timeframe": None, "threshold": None,
            }
            if asset_cfg is not None:
                asset_id = db.upsert_asset(conn, asset_cfg.symbol, asset_cfg.coingecko_id, asset_cfg.tier)
                atr_info = _get_reference_and_atr(conn, asset_id, horizon_days)

            db.insert_kol_prediction(
                conn, claim_id=claim_id, asset=asset_symbol, direction=claim.get("direction", ""),
                target_price=conditions.get("target"), invalidation_price=conditions.get("invalidation"),
                entry_zone_low=conditions.get("entry_zone_low"), entry_zone_high=conditions.get("entry_zone_high"),
                prediction_time=prediction_time.isoformat(), horizon_days=horizon_days,
                deadline=deadline.isoformat(), **atr_info,
            )

    return parsed.get("bullets", [])


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
