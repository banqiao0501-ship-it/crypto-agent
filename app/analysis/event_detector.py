"""
規則引擎（Rule Engine）：純程式規則判斷，不靠AI決定「要不要觸發警報」。
AI只有在規則觸發之後，才會被叫進來解釋「為什麼」（這塊在 app/ai/synthesizer.py 的
generate_alert_analysis）。

V1範圍先只做兩種規則：
1. 價格短時間內劇烈波動（1h / 4h變動幅度超過門檻）
2. RSI來到極端值（>75 或 <25）

完整的「Event Clustering / 跨來源事件比對」（V2藍圖提到的部分）先不做，
這裡只針對「單一幣種的市場數據異常」做偵測，不去跟新聞/KOL意見做交叉比對。

冷卻機制：同一幣種同一種trigger，COOLDOWN_HOURS 內不會重複觸發，
避免LINE被同一件事轟炸。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from app.config import Asset
from app.database import db
from app.utils.logger import get_logger

logger = get_logger(__name__)

PRICE_MOVE_THRESHOLDS = {"1h": 0.03, "4h": 0.05}  # 3% / 5%
RSI_OVERBOUGHT = 75
RSI_OVERSOLD = 25
COOLDOWN_HOURS = 4


def _price_move_pct(series: list[tuple[datetime, float, float]], lookback_points: int) -> float | None:
    """用最後一點跟往回數第lookback_points點比較漲跌幅（hourly序列裡，一點大約等於一小時）。"""
    if len(series) <= lookback_points:
        return None
    latest_price = series[-1][1]
    past_price = series[-1 - lookback_points][1]
    if past_price == 0:
        return None
    return (latest_price - past_price) / past_price


def detect(
    conn: sqlite3.Connection,
    assets: list[Asset],
    price_series_by_symbol: dict[str, dict[str, list[tuple[datetime, float, float]]]],
    technical_results: dict[str, dict[str, dict]],
) -> list[dict]:
    """回傳這次新觸發（沒被冷卻機制擋掉）的警報清單，每個元素是給AI synthesizer用的dict。"""
    triggered: list[dict] = []
    now = datetime.now(timezone.utc)
    cooldown_since = (now - timedelta(hours=COOLDOWN_HOURS)).isoformat()

    for asset in assets:
        asset_id = db.upsert_asset(conn, asset.symbol, asset.coingecko_id, asset.tier)
        hourly_series = price_series_by_symbol.get(asset.symbol, {}).get("short", [])

        # --- 規則1：價格劇烈波動 ---
        for timeframe, lookback in (("1h", 1), ("4h", 4)):
            threshold = PRICE_MOVE_THRESHOLDS[timeframe]
            pct = _price_move_pct(hourly_series, lookback)
            if pct is None or abs(pct) < threshold:
                continue

            trigger_type = f"price_move_{timeframe}"
            if db.recent_alert_exists(conn, asset_id, trigger_type, cooldown_since):
                continue

            trigger_data = {"timeframe": timeframe, "pct_change": round(pct * 100, 2)}
            db.insert_alert(
                conn, asset_id=asset_id, trigger_type=trigger_type,
                severity="high" if abs(pct) >= threshold * 1.5 else "medium",
                trigger_data=json.dumps(trigger_data), triggered_at=now.isoformat(), sent_at=None,
            )
            triggered.append({"asset": asset.symbol, "trigger_type": trigger_type, **trigger_data})
            logger.info("觸發警報：%s %s %.2f%%", asset.symbol, trigger_type, pct * 100)

        # --- 規則2：RSI極端值（用1h的RSI）---
        rsi = technical_results.get(asset.symbol, {}).get("1h", {}).get("rsi")
        if rsi is not None and (rsi >= RSI_OVERBOUGHT or rsi <= RSI_OVERSOLD):
            trigger_type = "rsi_extreme"
            if db.recent_alert_exists(conn, asset_id, trigger_type, cooldown_since):
                continue

            trigger_data = {"rsi": rsi, "zone": "overbought" if rsi >= RSI_OVERBOUGHT else "oversold"}
            db.insert_alert(
                conn, asset_id=asset_id, trigger_type=trigger_type, severity="medium",
                trigger_data=json.dumps(trigger_data), triggered_at=now.isoformat(), sent_at=None,
            )
            triggered.append({"asset": asset.symbol, "trigger_type": trigger_type, **trigger_data})
            logger.info("觸發警報：%s RSI極端值 %.1f", asset.symbol, rsi)

    return triggered
