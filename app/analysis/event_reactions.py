"""
Event → Market Reaction Tracking（P1-1）。

設計原則（照使用者定案的版本）：
- 多時間窗追蹤：15m / 1h / 4h / 24h是標準窗口，重大事件（impact=high）額外加5m
- 一定要有baseline（事件發生前60分鐘的價格變化），不然沒辦法判斷「是事件造成的，
  還是市場本來就在動」——excess_return = 事件後報酬 - 事件前報酬，才是真正歸因給事件的部分
- Event-driven tracking，不是每個事件建一個cron：事件發生時只建立幾筆「reaction_jobs」
  （記錄「asset X在事件發生後Y分鐘要結算一次」），實際的結算工作由market-check每次執行時
  順便掃描「有沒有到期的任務」來處理，就算堆積到上百個事件也不會失控
- 全部計算都是deterministic（純數學公式，不靠AI），AI只在後面的每日報告階段負責「解讀」
  已經算好的數字，不負責算數——這樣系統才穩定、可重現，累積久了也才能拿來做統計

追蹤範圍：事件本身標記的related_assets，再加上BTC/ETH（不管事件是不是直接關於它們，
大盤兩大龍頭的反應都值得看）。更細緻的「Level 3關聯幣種」判斷（例如SOL相關新聞也該追蹤BTC/ETH
之外的哪些山寨幣）目前先不做，先用events表已經有的related_assets就好，避免範圍在V1就衝過大。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from app.config import Asset
from app.database import db
from app.utils.logger import get_logger

logger = get_logger(__name__)

# 分鐘數：每個window代表事件發生後多久要結算一次
_WINDOW_MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240, "24h": 1440}
_BASELINE_MINUTES = 60
_ALWAYS_TRACK = ("BTC", "ETH")  # 不管事件跟誰有關，這兩個永遠追蹤


def create_reaction_jobs(
    conn: sqlite3.Connection, *, event_id: int, event_time: datetime,
    related_assets: list[str], impact: str,
) -> None:
    """事件建立時呼叫。只建立追蹤任務（幾筆輕量的DB紀錄），不在這裡做任何運算。"""
    assets = set(related_assets or []) | set(_ALWAYS_TRACK)
    windows = list(_WINDOW_MINUTES.keys())
    if impact != "high":
        windows = [w for w in windows if w != "5m"]  # 5m只給重大事件，避免一般事件也追蹤過度密集

    created_at = datetime.now(timezone.utc).isoformat()
    for asset in assets:
        for window in windows:
            due_at = (event_time + timedelta(minutes=_WINDOW_MINUTES[window])).isoformat()
            db.insert_reaction_job(
                conn, event_id=event_id, asset=asset, window=window,
                due_at=due_at, created_at=created_at,
            )


def _classify_reaction(excess_return: float | None) -> str:
    """把excess_return分成幾個固定分類，方便之後統計「這類事件通常怎麼反應」，
    而不是每次都要重新解讀一段自由文字。門檻是概略估計值，之後有更多資料可以再校準。"""
    if excess_return is None:
        return "NO_REACTION"
    if excess_return >= 0.03:
        return "STRONG_POSITIVE"
    if excess_return >= 0.01:
        return "POSITIVE"
    if excess_return <= -0.03:
        return "STRONG_NEGATIVE"
    if excess_return <= -0.01:
        return "NEGATIVE"
    return "NEUTRAL"


def _compute_volume_ratio(conn: sqlite3.Connection, asset_id: int, near_iso: str) -> float | None:
    """拿最接近near_iso的1h K棒volume，跟前面20根1h K棒的平均volume比。"""
    snapshot = db.get_market_snapshot_near(conn, asset_id, "1h", near_iso)
    if snapshot is None:
        return None

    recent = conn.execute(
        """SELECT volume FROM market_snapshots WHERE asset_id = ? AND timeframe = '1h' AND timestamp < ?
           ORDER BY timestamp DESC LIMIT 20""",
        (asset_id, snapshot["timestamp"]),
    ).fetchall()
    volumes = [r["volume"] for r in recent if r["volume"]]
    if not volumes:
        return None
    avg = sum(volumes) / len(volumes)
    if avg == 0:
        return None
    return snapshot["volume"] / avg


def process_due_jobs(conn: sqlite3.Connection, assets: list[Asset]) -> list[dict]:
    """market-check每次執行時呼叫。掃描到期的追蹤任務，逐一做deterministic結算。
    回傳這次結算完成的清單，方便log記錄。"""
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    assets_by_symbol = {a.symbol: a for a in assets}

    due_jobs = db.get_due_reaction_jobs(conn, now_iso)
    if not due_jobs:
        return []

    completed: list[dict] = []
    for job in due_jobs:
        asset_cfg = assets_by_symbol.get(job["asset"])
        if asset_cfg is None:
            db.update_reaction_job_status(conn, job["id"], "failed")
            continue

        try:
            asset_id = db.upsert_asset(conn, asset_cfg.symbol, asset_cfg.coingecko_id, asset_cfg.tier)
            event_row = db.get_event(conn, job["event_id"])
            if event_row is None:
                db.update_reaction_job_status(conn, job["id"], "failed")
                continue

            event_time = datetime.fromisoformat(event_row["created_at"])
            baseline_time = event_time - timedelta(minutes=_BASELINE_MINUTES)

            event_candle = db.get_market_snapshot_near(conn, asset_id, "1h", event_time.isoformat())
            baseline_candle = db.get_market_snapshot_near(conn, asset_id, "1h", baseline_time.isoformat())
            reaction_candle = db.get_market_snapshot_near(conn, asset_id, "1h", now_iso)

            if not (event_candle and baseline_candle and reaction_candle):
                logger.warning(
                    "event_id=%s asset=%s window=%s 缺少K棒資料，無法結算，標記失敗",
                    job["event_id"], job["asset"], job["window"],
                )
                db.update_reaction_job_status(conn, job["id"], "failed")
                continue

            event_price = event_candle["close"]
            baseline_price = baseline_candle["close"]
            reaction_price = reaction_candle["close"]

            baseline_return = (
                (event_price - baseline_price) / baseline_price if baseline_price else None
            )
            price_return = (
                (reaction_price - event_price) / event_price if event_price else None
            )
            excess_return = (
                price_return - baseline_return
                if price_return is not None and baseline_return is not None
                else None
            )

            event_deriv = db.get_derivative_snapshot_near(conn, asset_id, event_time.isoformat())
            reaction_deriv = db.get_derivative_snapshot_near(conn, asset_id, now_iso)
            oi_change_pct = None
            if (
                event_deriv and reaction_deriv
                and event_deriv["open_interest"] and reaction_deriv["open_interest"]
            ):
                oi_change_pct = (
                    (reaction_deriv["open_interest"] - event_deriv["open_interest"])
                    / event_deriv["open_interest"]
                )

            volume_ratio = _compute_volume_ratio(conn, asset_id, now_iso)
            reaction_type = _classify_reaction(excess_return)

            db.insert_event_reaction(
                conn, event_id=job["event_id"], asset=job["asset"], window=job["window"],
                baseline_price=baseline_price, event_price=event_price, reaction_price=reaction_price,
                baseline_return=baseline_return, price_return=price_return, excess_return=excess_return,
                volume_ratio=volume_ratio, oi_change_pct=oi_change_pct,
                funding_rate=reaction_deriv["funding_rate"] if reaction_deriv else None,
                reaction_type=reaction_type, created_at=now_iso,
            )
            db.update_reaction_job_status(conn, job["id"], "done")

            completed.append({
                "event_id": job["event_id"], "asset": job["asset"], "window": job["window"],
                "excess_return": excess_return, "reaction_type": reaction_type,
            })
            logger.info(
                "事件反應結算：event_id=%s %s %s → excess_return=%s (%s)",
                job["event_id"], job["asset"], job["window"],
                f"{excess_return*100:.2f}%" if excess_return is not None else "N/A",
                reaction_type,
            )
        except Exception:  # noqa: BLE001 - 單一job失敗不該擋住其他job繼續處理
            logger.exception("結算reaction job失敗：job_id=%s", job["id"])
            db.update_reaction_job_status(conn, job["id"], "failed")

    return completed
