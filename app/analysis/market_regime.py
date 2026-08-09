"""
Market Regime Detection。

判斷邏輯：分別算出Trend / Momentum / Volatility / Volume四個訊號，再組合成一個整體市場狀態標籤。
這裡刻意只用BTC+ETH（使用者指定），不是全部8個幣種——因為Market Regime的用途是描述「大盤氣氛」，
用市值最大、最能代表整體幣圈情緒的兩個幣就夠了，納入太多小幣反而會稀釋訊號。

五種狀態：
- Bullish Trend：趨勢向上，非高波動
- Bearish Trend：趨勢向下，非高波動、非恐慌性放量
- Range：沒有明確趨勢、波動度正常
- High Volatility：波動度異常放大，但還不到恐慌性下跌
- Risk-off：下跌+高波動+放量同時出現，最極端的狀態

P0改版：資料來源從CoinGecko價格點改成BingX 1h K線（真實OHLCV），邏輯本身沒有變，
只是把輸入資料結構從(datetime,price,volume) tuple改成Candle物件。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from statistics import mean, stdev

from app.analysis.data_quality import Candle
from app.config import Asset
from app.database import db
from app.utils.logger import get_logger

logger = get_logger(__name__)

REGIME_ASSETS = ("BTC", "ETH")  # 用哪些幣種來判斷整體市場狀態

_VOL_LOOKBACK_HOURS = 48       # 「近期波動度」的觀察窗口
_VOL_BASELINE_HOURS = 24 * 30  # 「基準波動度」的觀察窗口（近30天）
_VOLUME_LOOKBACK_HOURS = 24
_VOLUME_BASELINE_HOURS = 24 * 20


def _hourly_returns(closes: list[float]) -> list[float]:
    return [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(1, len(closes))
        if closes[i - 1] != 0
    ]


def _volatility_signal(hourly_candles: list[Candle]) -> tuple[str, float]:
    """比較近48小時的報酬率標準差 vs 再往前30天（不含近48小時）的標準差，回傳(signal, ratio)。
    baseline故意不包含recent窗口，不然一次真正的異常波動會被自己稀釋掉、偵測不出來。"""
    closes = [c.close for c in hourly_candles]
    needed = _VOL_BASELINE_HOURS + _VOL_LOOKBACK_HOURS
    if len(closes) < needed // 2:
        return "unknown", 1.0

    recent = closes[-_VOL_LOOKBACK_HOURS:]
    baseline_end = -_VOL_LOOKBACK_HOURS
    baseline_start = -min(needed, len(closes))
    baseline = closes[baseline_start:baseline_end]

    recent_returns = _hourly_returns(recent)
    baseline_returns = _hourly_returns(baseline)
    if len(recent_returns) < 5 or len(baseline_returns) < 5:
        return "unknown", 1.0

    recent_vol = stdev(recent_returns)
    baseline_vol = stdev(baseline_returns)
    if baseline_vol == 0:
        return "unknown", 1.0

    ratio = recent_vol / baseline_vol
    if ratio >= 1.5:
        return "high", ratio
    if ratio <= 0.7:
        return "low", ratio
    return "normal", ratio


def _volume_signal(hourly_candles: list[Candle]) -> tuple[str, float]:
    """比較近24小時成交量 vs 再往前20天（不含近24小時）的每小時平均成交量，回傳(signal, ratio)。
    同樣故意排除recent窗口，理由跟volatility一樣。"""
    volumes = [c.volume for c in hourly_candles if c.volume]
    needed = _VOLUME_BASELINE_HOURS + _VOLUME_LOOKBACK_HOURS
    if len(volumes) < needed // 2:
        return "unknown", 1.0

    recent_avg = mean(volumes[-_VOLUME_LOOKBACK_HOURS:])
    baseline_end = -_VOLUME_LOOKBACK_HOURS
    baseline_start = -min(needed, len(volumes))
    baseline_slice = volumes[baseline_start:baseline_end]
    if not baseline_slice:
        return "unknown", 1.0
    baseline_avg = mean(baseline_slice)
    if baseline_avg == 0:
        return "unknown", 1.0

    ratio = recent_avg / baseline_avg
    if ratio >= 1.3:
        return "above_average", ratio
    if ratio <= 0.7:
        return "below_average", ratio
    return "average", ratio


def _momentum_signal(rsi_4h: float | None) -> str:
    if rsi_4h is None:
        return "neutral"
    if rsi_4h >= 60:
        return "strong_bullish"
    if rsi_4h <= 40:
        return "strong_bearish"
    return "neutral"


def _combine_trend(trends: list[str]) -> str:
    """多個幣種的trend要一致才算數，任何分歧就視為mixed（保守判斷，避免單一幣種訊號誤導整體結論）。"""
    unique = set(trends)
    if unique == {"bullish"}:
        return "bullish"
    if unique == {"bearish"}:
        return "bearish"
    return "mixed"


def classify_regime(
    trend: str, momentum: str, volatility_signal: str, volume_signal: str,
) -> tuple[str, str]:
    """回傳(regime標籤, 判斷理由)，理由是給AI/使用者看的，不是黑盒結論。"""
    if volatility_signal == "high" and trend == "bearish" and volume_signal == "above_average":
        return "Risk-off", "下跌趨勢＋波動度異常放大＋成交量放大同時出現，屬於恐慌性拋售訊號"
    if volatility_signal == "high":
        return "High Volatility", "波動度明顯高於近期基準，方向尚未明朗，不適合追價"
    if trend == "bullish":
        return "Bullish Trend", "BTC與ETH皆呈多頭排列且波動度正常"
    if trend == "bearish":
        return "Bearish Trend", "BTC與ETH皆呈空頭排列，但尚未出現恐慌性放量"
    return "Range", "BTC與ETH趨勢不一致或呈中性，波動度也在正常範圍，判斷為區間盤整"


def compute(
    conn: sqlite3.Connection,
    assets: list[Asset],
    klines_by_symbol: dict[str, dict[str, list[Candle]]],
    technical_results: dict[str, dict[str, dict]],
) -> dict:
    """主入口：算出目前的Market Regime，存進DB，並回傳結果給AI synthesizer/LINE報告使用。"""
    relevant_assets = [a for a in assets if a.symbol in REGIME_ASSETS]

    trends: list[str] = []
    rsi_values: list[float] = []
    volatility_signals: list[str] = []
    volume_signals: list[str] = []

    for asset in relevant_assets:
        hourly_candles = klines_by_symbol.get(asset.symbol, {}).get("1h", [])
        tech_4h = technical_results.get(asset.symbol, {}).get("4h", {})

        trend = tech_4h.get("trend", "neutral")
        trends.append(trend)

        rsi = tech_4h.get("rsi")
        if rsi is not None:
            rsi_values.append(rsi)

        vol_signal, _ = _volatility_signal(hourly_candles)
        volatility_signals.append(vol_signal)

        volu_signal, _ = _volume_signal(hourly_candles)
        volume_signals.append(volu_signal)

    combined_trend = _combine_trend(trends)
    combined_momentum = _momentum_signal(mean(rsi_values) if rsi_values else None)
    # 波動度/成交量訊號：只要有一個幣種顯示high/above_average就採計（寧可提早示警，不要漏掉）
    combined_volatility = "high" if "high" in volatility_signals else (
        "low" if volatility_signals and all(s == "low" for s in volatility_signals) else "normal"
    )
    combined_volume = "above_average" if "above_average" in volume_signals else (
        "below_average" if volume_signals and all(s == "below_average" for s in volume_signals) else "average"
    )

    regime, reason = classify_regime(combined_trend, combined_momentum, combined_volatility, combined_volume)

    result = {
        "regime": regime,
        "reason": reason,
        "trend": combined_trend,
        "momentum": combined_momentum,
        "volatility": combined_volatility,
        "volume": combined_volume,
        "based_on": list(REGIME_ASSETS),
    }

    db.insert_market_regime_snapshot(
        conn,
        timestamp=datetime.now(timezone.utc).isoformat(),
        regime=regime,
        trend=combined_trend,
        momentum=combined_momentum,
        volatility=combined_volatility,
        volume=combined_volume,
        reason=reason,
    )
    logger.info("Market Regime：%s（%s）", regime, reason)

    return result
