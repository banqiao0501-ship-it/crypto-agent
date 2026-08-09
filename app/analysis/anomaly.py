"""
OI + Funding + Volume → Anomaly Score（P1-2）。

把三個獨立訊號（未平倉量變化、資金費率、成交量倍數）組合成一個綜合異常分數。
分數是deterministic加總（門檻式計分），不是AI判斷——這樣才能穩定地當作
Alert Engine（P1-4，event_detector.py）的觸發依據，也才能之後拿分數去做回測校準。

門檻數值目前是概略估計（沒有實際歷史資料回測過），如果實際跑起來發現太常/太少觸發，
回來調整 _OI_THRESHOLDS / _FUNDING_THRESHOLDS / _VOLUME_THRESHOLDS 這幾個常數就好，
不用動計分邏輯本身。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from app.database import db
from app.utils.logger import get_logger

logger = get_logger(__name__)

_OI_CHANGE_WINDOW_HOURS = 4
# (變動幅度門檻, 加分)，由大到小排序，符合第一個達到的門檻就用那個分數（不會疊加）
_OI_THRESHOLDS = [(0.10, 3), (0.05, 2), (0.03, 1)]
_FUNDING_THRESHOLDS = [(0.001, 2), (0.0005, 1)]
_VOLUME_THRESHOLDS = [(3.0, 3), (2.0, 2), (1.5, 1)]


def _score_by_thresholds(value: float, thresholds: list[tuple[float, int]]) -> int:
    for threshold, score in thresholds:
        if value >= threshold:
            return score
    return 0


def compute_anomaly_score(
    conn: sqlite3.Connection, asset_id: int, volume_ratio: float | None,
) -> dict:
    """回傳 {"score", "reasons", "oi_change_pct", "funding_rate", "volume_ratio"}。
    score是三個訊號各自門檻計分後的加總，reasons是給人看/給AI解讀用的文字說明。"""
    reasons: list[str] = []
    score = 0

    current = db.get_latest_derivative(conn, asset_id)
    oi_change_pct = None
    funding_rate = None

    if current:
        funding_rate = current["funding_rate"]
        past_iso = (
            datetime.fromisoformat(current["timestamp"]) - timedelta(hours=_OI_CHANGE_WINDOW_HOURS)
        ).isoformat()
        past = db.get_derivative_snapshot_near(conn, asset_id, past_iso)
        if past and past["open_interest"] and current["open_interest"]:
            oi_change_pct = (current["open_interest"] - past["open_interest"]) / past["open_interest"]

    if oi_change_pct is not None:
        s = _score_by_thresholds(abs(oi_change_pct), _OI_THRESHOLDS)
        if s:
            score += s
            direction = "增加" if oi_change_pct > 0 else "減少"
            reasons.append(f"OI在{_OI_CHANGE_WINDOW_HOURS}小時內{direction}{abs(oi_change_pct)*100:.1f}%")

    if funding_rate is not None:
        s = _score_by_thresholds(abs(funding_rate), _FUNDING_THRESHOLDS)
        if s:
            score += s
            reasons.append(f"funding_rate={funding_rate*100:.3f}%，偏離正常水位")

    if volume_ratio is not None:
        s = _score_by_thresholds(volume_ratio, _VOLUME_THRESHOLDS)
        if s:
            score += s
            reasons.append(f"成交量是近期平均的{volume_ratio:.1f}倍")

    return {
        "score": score,
        "reasons": reasons,
        "oi_change_pct": oi_change_pct,
        "funding_rate": funding_rate,
        "volume_ratio": volume_ratio,
    }
