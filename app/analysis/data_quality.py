"""
資料品質驗證（P0-3）。

這一層刻意放在「BingX抓到原始K線」跟「技術分析拿去算指標」中間，理由：
- BingX Kline的確切回應格式我沒辦法100%跟官方文件核對過（文件是動態網頁，我這邊讀不到），
  這一層可以在格式跟預期不符、或資料本身有問題時攔下來，而不是讓髒資料悄悄流進RSI/EMA計算，
  算出一個看起來正常、但其實是錯的技術指標。
- 就算端點格式正確，交易所API本身偶爾也會回傳缺漏、重複、或明顯異常的K棒（例如維護期間），
  這層驗證平常也用得到，不只是這次的過渡期用。

驗證項目：
1. 每根K棒內部OHLC關係要合理：high要是這根K棒裡最大的、low要是最小的
2. 不能有零或負數的價格
3. 時間戳必須嚴格遞增、不能重複
4. 相鄰K棒之間的時間間隔要符合預期的timeframe（例如1h K線，兩根之間不該差3小時，代表中間漏抓了）

不合格的K棒會被剔除並記錄警告，而不是讓整批資料直接失敗——單根K棒有問題，不代表其他都不能用。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.utils.logger import get_logger

logger = get_logger(__name__)

_TIMEFRAME_SECONDS = {"1h": 3600, "4h": 4 * 3600, "1d": 24 * 3600}
_GAP_TOLERANCE = 1.5  # 相鄰K棒間隔超過預期的1.5倍，就算異常缺口


@dataclass
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class ValidationResult:
    valid_candles: list[Candle]
    issues: list[str]

    @property
    def is_clean(self) -> bool:
        return len(self.issues) == 0


def _ohlc_consistent(c: Candle) -> bool:
    if c.open <= 0 or c.high <= 0 or c.low <= 0 or c.close <= 0:
        return False
    if c.high < max(c.open, c.close, c.low):
        return False
    if c.low > min(c.open, c.close, c.high):
        return False
    if c.high < c.low:
        return False
    return True


def validate_candles(candles: list[Candle], timeframe: str, symbol: str) -> ValidationResult:
    """依timeframe驗證一串K棒，回傳過濾後的乾淨資料+問題清單（問題清單只是記錄，不代表資料被丟棄）。"""
    issues: list[str] = []
    if not candles:
        return ValidationResult(valid_candles=[], issues=[f"{symbol} {timeframe}: 沒有任何K棒資料"])

    sorted_candles = sorted(candles, key=lambda c: c.timestamp)

    valid: list[Candle] = []
    seen_timestamps: set[datetime] = set()
    expected_gap = _TIMEFRAME_SECONDS.get(timeframe)

    prev: Candle | None = None
    for c in sorted_candles:
        if c.timestamp in seen_timestamps:
            issues.append(f"{symbol} {timeframe}: 發現重複時間戳 {c.timestamp}，已剔除")
            continue
        seen_timestamps.add(c.timestamp)

        if not _ohlc_consistent(c):
            issues.append(f"{symbol} {timeframe}: {c.timestamp} 這根K棒OHLC不合理（例如high小於close），已剔除")
            continue

        if prev is not None and expected_gap:
            actual_gap = (c.timestamp - prev.timestamp).total_seconds()
            if actual_gap > expected_gap * _GAP_TOLERANCE:
                issues.append(
                    f"{symbol} {timeframe}: {prev.timestamp} 到 {c.timestamp} 之間有異常缺口"
                    f"（間隔{actual_gap/3600:.1f}小時，預期約{expected_gap/3600:.1f}小時），可能中間漏抓了"
                )
            elif actual_gap < expected_gap / _GAP_TOLERANCE:
                issues.append(
                    f"{symbol} {timeframe}: {prev.timestamp} 到 {c.timestamp} 間隔異常過短"
                    f"（{actual_gap/3600:.2f}小時），可能是重複或時間戳錯誤"
                )

        valid.append(c)
        prev = c

    if issues:
        for issue in issues:
            logger.warning(issue)

    return ValidationResult(valid_candles=valid, issues=issues)


def validate_derivative_snapshot(
    symbol: str, funding_rate: float | None, open_interest: float | None,
    mark_price: float | None, index_price: float | None,
) -> list[str]:
    """檢查衍生品數據是否在合理範圍內，回傳問題清單（不剔除資料，只是記錄警告供之後排查）。"""
    issues: list[str] = []

    if funding_rate is not None and abs(funding_rate) > 0.05:
        issues.append(f"{symbol}: funding_rate={funding_rate} 數值異常大（一般在±1%以內），請人工核對")

    if open_interest is not None and open_interest <= 0:
        issues.append(f"{symbol}: open_interest={open_interest} 不合理（應該要大於0）")

    if mark_price is not None and index_price is not None and index_price > 0:
        deviation = abs(mark_price - index_price) / index_price
        if deviation > 0.05:
            issues.append(
                f"{symbol}: mark_price與index_price偏離超過5%（mark={mark_price}, index={index_price}），"
                f"這在正常市場很少見，可能其中一個數值有誤"
            )

    if issues:
        for issue in issues:
            logger.warning(issue)

    return issues
