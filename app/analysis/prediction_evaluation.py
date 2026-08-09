"""
KOL Prediction Evaluation Engine（P2-2）。

V1設計（跟使用者定案的版本一致）：
- Direction prediction（沒有明確target_price）：用ATR-normalized threshold做三區間判斷。
  threshold是prediction建立當下就凍結的atr_percent（=ATR/當時價格），不是結算當下重新算的，
  避免市場波動變化影響到「當初這個預測是不是合理」的判斷。
    - Bullish：return >= +threshold → CORRECT；return <= -threshold → INCORRECT；其餘 → INCONCLUSIVE
    - Bearish：return <= -threshold → CORRECT；return >= +threshold → INCORRECT；其餘 → INCONCLUSIVE
- Target prediction（有明確target_price）：不用ATR threshold，直接判斷horizon內有沒有觸價。
    - 觸價 → CORRECT
    - 沒觸價但方向對且幅度超過threshold → PARTIAL（方向對，只是沒到目標價）
    - 沒觸價且方向相反超過threshold → INCORRECT
    - 其餘 → INCONCLUSIVE
- 缺資料（沒有reference_price、沒有K線資料可查）→ UNSCORABLE
- invalidation_hit會記錄，但V1先不影響最終result判斷，只是附加資訊，之後有需要再納入判斷邏輯

不做「conditional/breakout」預測的特殊處理（使用者文件裡提到的第三類），V1先只處理
Direction跟Target這兩類，佔KOL發言的大多數，breakout類的先讓它落在INCONCLUSIVE/UNSCORABLE，
之後有需要再另外處理。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.config import Asset
from app.database import db
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _evaluate_one(prediction: sqlite3.Row, market_stats: dict) -> dict:
    direction = prediction["direction"]
    reference_price = prediction["reference_price"]
    target = prediction["target_price"]
    invalidation = prediction["invalidation_price"]
    threshold = prediction["threshold"]

    max_price = market_stats["max_price"]
    min_price = market_stats["min_price"]
    close_price = market_stats["close_price"]

    directional_return = (
        (close_price - reference_price) / reference_price if reference_price else None
    )

    target_hit = None
    if target is not None:
        if direction == "bullish":
            target_hit = max_price >= target
        elif direction == "bearish":
            target_hit = min_price <= target

    invalidation_hit = None
    if invalidation is not None:
        if direction == "bullish":
            invalidation_hit = min_price <= invalidation
        elif direction == "bearish":
            invalidation_hit = max_price >= invalidation

    if directional_return is None:
        result = "UNSCORABLE"
    elif target is not None:
        if target_hit:
            result = "CORRECT"
        elif threshold is None:
            result = "INCONCLUSIVE"
        elif direction == "bullish" and directional_return >= threshold:
            result = "PARTIAL"
        elif direction == "bearish" and directional_return <= -threshold:
            result = "PARTIAL"
        elif direction == "bullish" and directional_return <= -threshold:
            result = "INCORRECT"
        elif direction == "bearish" and directional_return >= threshold:
            result = "INCORRECT"
        else:
            result = "INCONCLUSIVE"
    elif threshold is None:
        result = "UNSCORABLE"
    elif direction == "bullish":
        result = (
            "CORRECT" if directional_return >= threshold
            else "INCORRECT" if directional_return <= -threshold
            else "INCONCLUSIVE"
        )
    elif direction == "bearish":
        result = (
            "CORRECT" if directional_return <= -threshold
            else "INCORRECT" if directional_return >= threshold
            else "INCONCLUSIVE"
        )
    else:
        result = "UNSCORABLE"

    return {
        "max_price": max_price, "min_price": min_price, "close_price": close_price,
        "target_hit": target_hit, "invalidation_hit": invalidation_hit,
        "directional_return": directional_return, "result": result,
    }


def _mark_unscorable(conn: sqlite3.Connection, prediction_id: int, now_iso: str) -> None:
    db.update_kol_prediction_result(
        conn, prediction_id=prediction_id, max_price=None, min_price=None, close_price=None,
        target_hit=None, invalidation_hit=None, directional_return=None,
        result="UNSCORABLE", evaluated_at=now_iso,
    )


def process_due_predictions(conn: sqlite3.Connection, assets: list[Asset]) -> list[dict]:
    """market-check/daily-report每次執行時呼叫，掃描到期的KOL prediction並結算。
    回傳這次結算完成的清單，方便log記錄。"""
    now_iso = datetime.now(timezone.utc).isoformat()
    assets_by_symbol = {a.symbol: a for a in assets}

    due = db.get_pending_kol_predictions_due(conn, now_iso)
    if not due:
        return []

    completed: list[dict] = []
    for prediction in due:
        try:
            asset_cfg = assets_by_symbol.get(prediction["asset"])
            if asset_cfg is None or prediction["reference_price"] is None:
                _mark_unscorable(conn, prediction["id"], now_iso)
                continue

            asset_id = db.upsert_asset(conn, asset_cfg.symbol, asset_cfg.coingecko_id, asset_cfg.tier)
            timeframe = prediction["atr_timeframe"] or "4h"
            stats = db.get_market_snapshot_range_stats(
                conn, asset_id, timeframe, prediction["prediction_time"], prediction["deadline"],
            )
            if stats is None:
                logger.warning(
                    "prediction_id=%s 找不到%s到%s之間的%s K線資料，標記UNSCORABLE",
                    prediction["id"], prediction["prediction_time"], prediction["deadline"], timeframe,
                )
                _mark_unscorable(conn, prediction["id"], now_iso)
                continue

            eval_result = _evaluate_one(prediction, stats)
            db.update_kol_prediction_result(conn, prediction_id=prediction["id"], evaluated_at=now_iso, **eval_result)

            completed.append({
                "prediction_id": prediction["id"], "asset": prediction["asset"],
                "result": eval_result["result"], "directional_return": eval_result["directional_return"],
            })
            ret_text = (
                f"{eval_result['directional_return']*100:.2f}%"
                if eval_result["directional_return"] is not None else "N/A"
            )
            logger.info(
                "KOL預測結算：prediction_id=%s %s → %s (return=%s)",
                prediction["id"], prediction["asset"], eval_result["result"], ret_text,
            )
        except Exception:  # noqa: BLE001 - 單一prediction結算失敗不該擋住其他的繼續處理
            logger.exception("結算KOL prediction失敗：prediction_id=%s", prediction["id"])
            _mark_unscorable(conn, prediction["id"], now_iso)

    return completed
