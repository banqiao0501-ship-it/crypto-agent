"""
技術分析引擎（V1範圍：RSI、EMA20/50/200、簡單趨勢判斷、簡單支撐壓力）。

刻意不做MACD/ATR/量能比這些（V2藍圖裡有提到，但先不做，避免V1範圍膨脹）。
指標故意只輸出「結構化的結論」（trend/rsi/support/resistance數值），
不直接輸出「買/賣」——這是延續我們先前討論的原則：
technical engine負責產出「素材」，真正綜合判斷交給AI synthesizer。

三個timeframe怎麼來的：
- 1h：直接用CoinGecko抓到的hourly價格序列
- 4h：把hourly序列resample成4小時一根
- 1d：直接用CoinGecko抓到的daily價格序列（另外抓的，因為hourly序列長度不夠算1d的EMA200）
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pandas as pd

from app.config import Asset
from app.database import db
from app.utils.logger import get_logger

logger = get_logger(__name__)

RESAMPLE_RULES = {"1h": None, "4h": "4h", "1d": None}  # None代表不用resample，直接用原始序列


def _series_to_df(series: list[tuple[datetime, float, float]]) -> pd.DataFrame:
    if not series:
        return pd.DataFrame(columns=["close", "volume"])
    df = pd.DataFrame(series, columns=["timestamp", "close", "volume"]).set_index("timestamp")
    return df.sort_index()


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """標準Wilder's RSI。"""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def _classify_trend(close: float, ema20: float, ema50: float) -> str:
    """簡化版趨勢判斷：現價相對短中期均線的排列。V1先不看EMA200排列，避免規則過度複雜。"""
    if pd.isna(ema20) or pd.isna(ema50):
        return "neutral"
    if close > ema20 > ema50:
        return "bullish"
    if close < ema20 < ema50:
        return "bearish"
    return "neutral"


def _support_resistance(df: pd.DataFrame, lookback: int = 50) -> tuple[list[float], list[float]]:
    """非常簡化的版本：抓近期高低點，不做真正的樞紐點/流動性分析（那是V2/V3的事）。"""
    recent = df.tail(lookback)
    if recent.empty:
        return [], []
    near = df.tail(min(20, len(df)))
    support = sorted({round(near["close"].min(), 4), round(recent["close"].min(), 4)})
    resistance = sorted({round(near["close"].max(), 4), round(recent["close"].max(), 4)})
    return support, resistance


def _compute_one_timeframe(df: pd.DataFrame) -> dict | None:
    if df.empty or len(df) < 20:
        return None

    df = df.copy()
    df["ema20"] = _ema(df["close"], 20)
    df["ema50"] = _ema(df["close"], 50)
    df["ema200"] = _ema(df["close"], 200)
    df["rsi"] = _rsi(df["close"])

    latest = df.iloc[-1]
    support, resistance = _support_resistance(df)

    return {
        "timestamp": df.index[-1].isoformat(),
        "close": float(latest["close"]),
        "rsi": None if pd.isna(latest["rsi"]) else round(float(latest["rsi"]), 2),
        "ema20": None if pd.isna(latest["ema20"]) else round(float(latest["ema20"]), 6),
        "ema50": None if pd.isna(latest["ema50"]) else round(float(latest["ema50"]), 6),
        "ema200": None if pd.isna(latest["ema200"]) else round(float(latest["ema200"]), 6),
        "trend": _classify_trend(latest["close"], latest["ema20"], latest["ema50"]),
        "support": support,
        "resistance": resistance,
    }


def run(
    conn: sqlite3.Connection,
    assets: list[Asset],
    price_series_by_symbol: dict[str, dict[str, list[tuple[datetime, float, float]]]],
) -> dict[str, dict[str, dict]]:
    """對每個asset、每個timeframe算指標，存進DB，同時回傳結果給AI synthesizer直接用。

    回傳格式：{symbol: {"1h": {...}, "4h": {...}, "1d": {...}}}
    """
    results: dict[str, dict[str, dict]] = {}

    for asset in assets:
        asset_id = db.upsert_asset(conn, asset.symbol, asset.coingecko_id, asset.tier)
        series = price_series_by_symbol.get(asset.symbol, {"short": [], "long": []})

        df_short = _series_to_df(series["short"])
        df_long = _series_to_df(series["long"])

        timeframe_dfs = {
            "1h": df_short,
            "4h": df_short.resample("4h").last().dropna() if not df_short.empty else df_short,
            "1d": df_long,
        }

        asset_results: dict[str, dict] = {}
        for timeframe, df in timeframe_dfs.items():
            snapshot = _compute_one_timeframe(df)
            if snapshot is None:
                logger.info("%s %s 資料不足，略過技術指標計算", asset.symbol, timeframe)
                continue

            db.insert_technical_snapshot(
                conn,
                asset_id=asset_id,
                timestamp=snapshot["timestamp"],
                timeframe=timeframe,
                rsi=snapshot["rsi"],
                ema20=snapshot["ema20"],
                ema50=snapshot["ema50"],
                ema200=snapshot["ema200"],
                trend=snapshot["trend"],
                support=json.dumps(snapshot["support"]),
                resistance=json.dumps(snapshot["resistance"]),
            )
            asset_results[timeframe] = snapshot

        results[asset.symbol] = asset_results

    return results
