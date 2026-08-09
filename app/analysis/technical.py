"""
技術分析引擎（RSI、EMA20/50/200、趨勢判斷、支撐壓力）。

P0改版重點：現在吃的是BingX的真實K棒（open/high/low/close/volume都是真的），
不再是CoinGecko價格點模擬出來的假OHLC。三個timeframe（1h/4h/1d）也都是BingX原生回傳的，
不用再自己resample——這樣支撐壓力可以用真正的high/low算，比之前只能用close價位準確。

指標故意只輸出「結構化的結論」（trend/rsi/support/resistance數值），
不直接輸出「買/賣」——technical engine負責產出「素材」，真正綜合判斷交給AI synthesizer。
"""
from __future__ import annotations

import json
import sqlite3

import pandas as pd

from app.analysis.data_quality import Candle
from app.config import Asset
from app.database import db
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _candles_to_df(candles: list[Candle]) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(
        [(c.timestamp, c.open, c.high, c.low, c.close, c.volume) for c in candles],
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    ).set_index("timestamp")
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


def _macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    """標準MACD：(MACD線, signal線, histogram)。"""
    macd_line = _ema(series, fast) - _ema(series, slow)
    signal_line = _ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range，衡量波動幅度。True Range取三者最大值：
    (今天high-low)、(今天high-昨天close的絕對值)、(今天low-昨天close的絕對值)。"""
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _volume_ratio(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """目前這根K棒的volume，相對於前面period根的平均volume，倍數表示（例如2.0代表是平常的兩倍）。"""
    avg_volume = df["volume"].rolling(period).mean()
    return df["volume"] / avg_volume.replace(0, pd.NA)


def _classify_trend(close: float, ema20: float, ema50: float) -> str:
    """簡化版趨勢判斷：現價相對短中期均線的排列。先不看EMA200排列，避免規則過度複雜。"""
    if pd.isna(ema20) or pd.isna(ema50):
        return "neutral"
    if close > ema20 > ema50:
        return "bullish"
    if close < ema20 < ema50:
        return "bearish"
    return "neutral"


def _support_resistance(df: pd.DataFrame, lookback: int = 50) -> tuple[list[float], list[float]]:
    """用真實的high/low算近期高低點（P0改版前只能用close，因為CoinGecko給的是價格點不是K棒）。
    仍然是簡化版本，不做真正的樞紐點/流動性分析（那是之後可以再加的事）。"""
    recent = df.tail(lookback)
    if recent.empty:
        return [], []
    near = df.tail(min(20, len(df)))
    support = sorted({round(near["low"].min(), 4), round(recent["low"].min(), 4)})
    resistance = sorted({round(near["high"].max(), 4), round(recent["high"].max(), 4)})
    return support, resistance


def _compute_one_timeframe(df: pd.DataFrame) -> dict | None:
    if df.empty or len(df) < 20:
        return None

    df = df.copy()
    df["ema20"] = _ema(df["close"], 20)
    df["ema50"] = _ema(df["close"], 50)
    df["ema200"] = _ema(df["close"], 200)
    df["rsi"] = _rsi(df["close"])
    macd_line, signal_line, histogram = _macd(df["close"])
    df["macd"], df["macd_signal"], df["macd_hist"] = macd_line, signal_line, histogram
    df["atr"] = _atr(df)
    df["volume_ratio"] = _volume_ratio(df)

    latest = df.iloc[-1]
    support, resistance = _support_resistance(df)

    def _safe_round(value, digits=6):
        return None if pd.isna(value) else round(float(value), digits)

    return {
        "timestamp": df.index[-1].isoformat(),
        "close": float(latest["close"]),
        "rsi": _safe_round(latest["rsi"], 2),
        "ema20": _safe_round(latest["ema20"]),
        "ema50": _safe_round(latest["ema50"]),
        "ema200": _safe_round(latest["ema200"]),
        "macd": _safe_round(latest["macd"]),
        "macd_signal": _safe_round(latest["macd_signal"]),
        "macd_hist": _safe_round(latest["macd_hist"]),
        "atr": _safe_round(latest["atr"]),
        "volume_ratio": _safe_round(latest["volume_ratio"], 2),
        "trend": _classify_trend(latest["close"], latest["ema20"], latest["ema50"]),
        "support": support,
        "resistance": resistance,
    }


def run(
    conn: sqlite3.Connection,
    assets: list[Asset],
    klines_by_symbol: dict[str, dict[str, list[Candle]]],
) -> dict[str, dict[str, dict]]:
    """對每個asset、每個timeframe（1h/4h/1d，都是BingX原生K線）算指標，存進DB，
    同時回傳結果給AI synthesizer直接用。

    回傳格式：{symbol: {"1h": {...}, "4h": {...}, "1d": {...}}}
    """
    results: dict[str, dict[str, dict]] = {}

    for asset in assets:
        asset_id = db.upsert_asset(conn, asset.symbol, asset.coingecko_id, asset.tier)
        candles_by_timeframe = klines_by_symbol.get(asset.symbol, {})

        asset_results: dict[str, dict] = {}
        for timeframe, candles in candles_by_timeframe.items():
            df = _candles_to_df(candles)
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
                macd=snapshot["macd"],
                macd_signal=snapshot["macd_signal"],
                macd_hist=snapshot["macd_hist"],
                atr=snapshot["atr"],
                volume_ratio=snapshot["volume_ratio"],
                trend=snapshot["trend"],
                support=json.dumps(snapshot["support"]),
                resistance=json.dumps(snapshot["resistance"]),
            )
            asset_results[timeframe] = snapshot

        results[asset.symbol] = asset_results

    return results
