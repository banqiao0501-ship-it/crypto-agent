"""
規則引擎（Rule Engine）：純程式規則判斷，不靠AI決定「要不要觸發警報」。
AI只有在規則觸發之後，才會被叫進來解釋「為什麼」（這塊在 app/ai/synthesizer.py 的
generate_alert_analysis）。

範圍：
1. 價格短時間內劇烈波動（1h / 4h變動幅度超過門檻）
2. RSI來到極端值（>75 或 <25）
3.（P1-4新增）OI+Funding+Volume異常分數超過門檻——這條規則不看價格本身，
   看的是「衍生品市場的槓桿/資金行為」是否異常，價格還沒明顯波動時可能就已經先示警
   （例如OI悄悄堆積、funding偏離、成交量放大，但價格還在盤整）

P0改版：現在直接用BingX原生的1h/4h K線比較漲跌幅（最新一根收盤價 vs 前一根收盤價），
不用再像以前那樣拿hourly序列往回數4個點去模擬4h波動——因為現在4h本身就是真實的K棒了。

完整的「Event Clustering / 跨來源事件比對」先不放在這裡，這裡只針對「單一幣種的市場數據異常」
做偵測，不去跟新聞/KOL意見做交叉比對（那是app/ai/synthesizer.py的extract_events在做的事）。

冷卻機制：同一幣種同一種trigger，COOLDOWN_HOURS 內不會重複觸發，避免LINE被同一件事轟炸。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from app.analysis import anomaly
from app.analysis.data_quality import Candle
from app.config import Asset
from app.database import db
from app.utils.logger import get_logger

logger = get_logger(__name__)

PRICE_MOVE_THRESHOLDS = {"1h": 0.03, "4h": 0.05}  # 3% / 5%
RSI_OVERBOUGHT = 75
RSI_OVERSOLD = 25
ANOMALY_SCORE_THRESHOLD = 4  # 異常分數達到這個門檻才觸發告警
COOLDOWN_HOURS = 4


def _price_move_pct(candles: list[Candle]) -> float | None:
    """最新一根K棒的收盤價，相對於前一根K棒收盤價的漲跌幅。"""
    if len(candles) < 2:
        return None
    latest_close = candles[-1].close
    prev_close = candles[-2].close
    if prev_close == 0:
        return None
    return (latest_close - prev_close) / prev_close


def detect(
    conn: sqlite3.Connection,
    assets: list[Asset],
    klines_by_symbol: dict[str, dict[str, list[Candle]]],
    technical_results: dict[str, dict[str, dict]],
) -> list[dict]:
    """回傳這次新觸發（沒被冷卻機制擋掉）的警報清單，每個元素是給AI synthesizer用的dict。"""
    triggered: list[dict] = []
    now = datetime.now(timezone.utc)
    cooldown_since = (now - timedelta(hours=COOLDOWN_HOURS)).isoformat()

    for asset in assets:
        asset_id = db.upsert_asset(conn, asset.symbol, asset.coingecko_id, asset.tier)
        candles_by_timeframe = klines_by_symbol.get(asset.symbol, {})

        # --- 規則1：價格劇烈波動 ---
        for timeframe, threshold in PRICE_MOVE_THRESHOLDS.items():
            candles = candles_by_timeframe.get(timeframe, [])
            pct = _price_move_pct(candles)
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

        # --- 規則3（P1-4）：OI+Funding+Volume異常分數 ---
        volume_ratio = technical_results.get(asset.symbol, {}).get("1h", {}).get("volume_ratio")
        anomaly_result = anomaly.compute_anomaly_score(conn, asset_id, volume_ratio)
        if anomaly_result["score"] >= ANOMALY_SCORE_THRESHOLD:
            trigger_type = "derivatives_anomaly"
            if db.recent_alert_exists(conn, asset_id, trigger_type, cooldown_since):
                continue

            trigger_data = {
                "score": anomaly_result["score"],
                "reasons": anomaly_result["reasons"],
                "oi_change_pct": (
                    round(anomaly_result["oi_change_pct"] * 100, 2)
                    if anomaly_result["oi_change_pct"] is not None else None
                ),
                "funding_rate": anomaly_result["funding_rate"],
                "volume_ratio": (
                    round(anomaly_result["volume_ratio"], 2)
                    if anomaly_result["volume_ratio"] is not None else None
                ),
            }
            db.insert_alert(
                conn, asset_id=asset_id, trigger_type=trigger_type,
                severity="high" if anomaly_result["score"] >= ANOMALY_SCORE_THRESHOLD + 2 else "medium",
                trigger_data=json.dumps(trigger_data, ensure_ascii=False),
                triggered_at=now.isoformat(), sent_at=None,
            )
            triggered.append({"asset": asset.symbol, "trigger_type": trigger_type, **trigger_data})
            logger.info(
                "觸發警報：%s 衍生品異常分數=%d（%s）",
                asset.symbol, anomaly_result["score"], "、".join(anomaly_result["reasons"]),
            )

    return triggered
