"""
市場數據 collector（P0改版）。

分工原則（跟使用者確認過的架構）：
- BingX：交易數據主力，包含K線OHLCV、Funding Rate、Open Interest、Mark Price、Index Price。
  技術分析（RSI/EMA/趨勢）全部改用BingX的真實K棒計算，不再用CoinGecko的價格點模擬OHLC——
  舊版用同一個價格點假裝open=high=low=close，會讓ATR、真實高低點、波動率這些分析失真。
- Binance / Bybit：BingX抓不到時的備援，一樣能拿到funding rate/OI/mark price/index price。
- CoinGecko：改為只負責「市場背景資料」（Market Cap、市場排名、流通量/總供給量），
  不再是技術分析或主要價格的資料來源。

【重要提醒】BingX Kline端點的確切回應格式，我沒辦法直接連網跟官方文件核對（文件是動態網頁，
我這邊讀不到內容），是照第三方SDK的原始碼交叉比對寫的（路徑：/openApi/swap/v3/quote/klines）。
下面的解析邏輯故意寫得比較有彈性（同時處理「陣列包陣列」跟「陣列包物件」兩種可能格式），
如果實際跑起來發現解析不出東西，把debug log裡印出的原始回應內容貼回來，我們再調整。

這個檔案只負責「抓資料、驗證、存進DB」，不算技術指標——算指標是 app/analysis/technical.py 的工作。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import httpx

from app.analysis.data_quality import Candle, validate_candles, validate_derivative_snapshot
from app.config import Asset
from app.database import db
from app.utils.logger import get_logger

logger = get_logger(__name__)

_CG_BASE = "https://api.coingecko.com/api/v3"
_BINGX_BASE = "https://open-api.bingx.com"
_BINANCE_BASE = "https://fapi.binance.com"
_BYBIT_BASE = "https://api.bybit.com"

# 技術分析要用的三個timeframe，跟各自要抓幾根K棒（留足夠算EMA200的餘裕；1h特別抓多一點，
# 是因為Market Regime的波動度基準窗口需要30天歷史，500根只有約21天不夠用）
_KLINE_TIMEFRAMES = {"1h": 1000, "4h": 500, "1d": 300}


# ---------- BingX：K線 OHLCV ----------

def _parse_bingx_kline_item(item, symbol: str, timeframe: str) -> Candle | None:
    """K線的單一項目可能是陣列（[openTime, open, high, low, close, volume, closeTime, ...]）
    也可能是物件（{"time"/"openTime": ..., "open":..., "high":..., "low":..., "close":..., "volume":...}），
    這裡兩種都試，哪種格式實際符合就用哪種。"""
    try:
        if isinstance(item, dict):
            ts_ms = item.get("time") or item.get("openTime")
            open_, high, low, close = item.get("open"), item.get("high"), item.get("low"), item.get("close")
            volume = item.get("volume", 0)
        elif isinstance(item, (list, tuple)) and len(item) >= 6:
            ts_ms, open_, high, low, close, volume = item[:6]
        else:
            return None

        if ts_ms is None or open_ is None or high is None or low is None or close is None:
            return None

        return Candle(
            timestamp=datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc),
            open=float(open_), high=float(high), low=float(low), close=float(close),
            volume=float(volume) if volume is not None else 0.0,
        )
    except (ValueError, TypeError) as exc:
        logger.warning("%s %s 有一根K棒解析失敗，已略過：%s（原始資料：%s）", symbol, timeframe, exc, item)
        return None


def fetch_bingx_klines(symbol: str, interval: str, limit: int, client: httpx.Client) -> list[Candle]:
    resp = client.get(
        f"{_BINGX_BASE}/openApi/swap/v3/quote/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=20,
    )
    resp.raise_for_status()
    raw_items = resp.json().get("data", [])

    candles = []
    for item in raw_items:
        candle = _parse_bingx_kline_item(item, symbol, interval)
        if candle:
            candles.append(candle)
    return candles


def collect_klines(
    conn: sqlite3.Connection, asset_id: int, asset: Asset, client: httpx.Client,
) -> dict[str, list[Candle]]:
    """對一個asset抓1h/4h/1d三個timeframe的K線，驗證後存進market_snapshots，回傳驗證過的資料。"""
    result: dict[str, list[Candle]] = {}

    for timeframe, limit in _KLINE_TIMEFRAMES.items():
        try:
            raw_candles = fetch_bingx_klines(asset.bingx_symbol, timeframe, limit, client)
        except Exception as exc:  # noqa: BLE001
            logger.error("%s %s K線抓取失敗：%s", asset.symbol, timeframe, exc)
            result[timeframe] = []
            continue

        validation = validate_candles(raw_candles, timeframe, asset.symbol)
        result[timeframe] = validation.valid_candles

        for c in validation.valid_candles:
            db.insert_market_snapshot(
                conn, asset_id=asset_id, timestamp=c.timestamp.isoformat(), timeframe=timeframe,
                open_=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume,
            )

    return result


# ---------- CoinGecko：市場背景資料（不再用於技術分析）----------

def fetch_coingecko_context(coingecko_id: str, api_key: str, client: httpx.Client) -> dict | None:
    """market cap、市場排名、流通量這些「背景資訊」，給AI寫報告時當作額外脈絡用，
    不影響技術指標計算。"""
    headers = {"x-cg-demo-api-key": api_key} if api_key else {}
    try:
        resp = client.get(
            f"{_CG_BASE}/coins/{coingecko_id}",
            params={
                "localization": "false", "tickers": "false", "market_data": "true",
                "community_data": "false", "developer_data": "false",
            },
            headers=headers, timeout=20,
        )
        resp.raise_for_status()
        market_data = resp.json().get("market_data", {})
        return {
            "market_cap_usd": market_data.get("market_cap", {}).get("usd"),
            "market_cap_rank": market_data.get("market_cap_rank"),
            "circulating_supply": market_data.get("circulating_supply"),
            "total_supply": market_data.get("total_supply"),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s CoinGecko市場背景資料抓取失敗：%s", coingecko_id, exc)
        return None


def collect_market_context(
    conn: sqlite3.Connection, asset_id: int, asset: Asset, api_key: str, client: httpx.Client,
) -> None:
    context = fetch_coingecko_context(asset.coingecko_id, api_key, client)
    if context is None:
        return
    db.insert_market_context_snapshot(
        conn, asset_id=asset_id, timestamp=datetime.now(timezone.utc).isoformat(),
        market_cap_usd=context["market_cap_usd"], market_cap_rank=context["market_cap_rank"],
        circulating_supply=context["circulating_supply"], total_supply=context["total_supply"],
    )


# ---------- BingX / Binance / Bybit：衍生品數據（Funding Rate / OI / Mark Price / Index Price）----------

def fetch_bingx_derivatives(symbol: str, client: httpx.Client) -> dict:
    """回傳 {funding_rate, open_interest, mark_price, index_price}，任何一項失敗就是None，不整個拋錯。"""
    result = {"funding_rate": None, "open_interest": None, "mark_price": None, "index_price": None}
    try:
        resp = client.get(
            f"{_BINGX_BASE}/openApi/swap/v2/quote/premiumIndex", params={"symbol": symbol}, timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        result["funding_rate"] = float(data["lastFundingRate"]) if data.get("lastFundingRate") is not None else None
        result["mark_price"] = float(data["markPrice"]) if data.get("markPrice") is not None else None
        result["index_price"] = float(data["indexPrice"]) if data.get("indexPrice") is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("BingX funding/mark/index price 抓取失敗（%s）：%s", symbol, exc)

    try:
        resp = client.get(
            f"{_BINGX_BASE}/openApi/swap/v2/quote/openInterest", params={"symbol": symbol}, timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        result["open_interest"] = float(data["openInterest"]) if data.get("openInterest") is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("BingX open interest 抓取失敗（%s）：%s", symbol, exc)

    return result


def fetch_binance_derivatives(symbol: str, client: httpx.Client) -> dict:
    result = {"funding_rate": None, "open_interest": None, "mark_price": None, "index_price": None}
    try:
        resp = client.get(f"{_BINANCE_BASE}/fapi/v1/premiumIndex", params={"symbol": symbol}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        result["funding_rate"] = float(data["lastFundingRate"])
        result["mark_price"] = float(data["markPrice"])
        result["index_price"] = float(data["indexPrice"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Binance funding/mark/index price 抓取失敗（%s）：%s", symbol, exc)

    try:
        resp = client.get(f"{_BINANCE_BASE}/fapi/v1/openInterest", params={"symbol": symbol}, timeout=15)
        resp.raise_for_status()
        result["open_interest"] = float(resp.json()["openInterest"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Binance open interest 抓取失敗（%s）：%s", symbol, exc)

    return result


def fetch_bybit_derivatives(symbol: str, client: httpx.Client) -> dict:
    result = {"funding_rate": None, "open_interest": None, "mark_price": None, "index_price": None}
    try:
        resp = client.get(
            f"{_BYBIT_BASE}/v5/market/tickers", params={"category": "linear", "symbol": symbol}, timeout=15,
        )
        resp.raise_for_status()
        result_list = resp.json().get("result", {}).get("list", [])
        if not result_list:
            return result
        item = result_list[0]
        result["funding_rate"] = float(item["fundingRate"]) if item.get("fundingRate") else None
        result["open_interest"] = float(item["openInterest"]) if item.get("openInterest") else None
        result["mark_price"] = float(item["markPrice"]) if item.get("markPrice") else None
        result["index_price"] = float(item["indexPrice"]) if item.get("indexPrice") else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Bybit衍生品數據抓取失敗（%s）：%s", symbol, exc)
    return result


def collect_derivatives(conn: sqlite3.Connection, asset_id: int, asset: Asset, client: httpx.Client) -> None:
    result = {"funding_rate": None, "open_interest": None, "mark_price": None, "index_price": None}
    source = None

    if asset.bingx_symbol:
        result = fetch_bingx_derivatives(asset.bingx_symbol, client)
        source = "bingx"

    if all(v is None for v in result.values()) and asset.binance_symbol:
        result = fetch_binance_derivatives(asset.binance_symbol, client)
        source = "binance"

    if all(v is None for v in result.values()) and asset.bybit_symbol:
        result = fetch_bybit_derivatives(asset.bybit_symbol, client)
        source = "bybit"

    if all(v is None for v in result.values()):
        logger.info("%s 三個交易所的衍生品數據都抓不到，略過", asset.symbol)
        return

    validate_derivative_snapshot(
        asset.symbol, result["funding_rate"], result["open_interest"],
        result["mark_price"], result["index_price"],
    )

    db.insert_derivative_snapshot(
        conn, asset_id=asset_id, timestamp=datetime.now(timezone.utc).isoformat(),
        funding_rate=result["funding_rate"], open_interest=result["open_interest"],
        mark_price=result["mark_price"], index_price=result["index_price"], source=source,
    )


# ---------- 主入口 ----------

def collect(
    conn: sqlite3.Connection, assets: list[Asset], coingecko_api_key: str,
) -> dict[str, dict[str, list[Candle]]]:
    """對每個asset抓K線+衍生品數據+市場背景資料。回傳 {symbol: {"1h":[...], "4h":[...], "1d":[...]}}，
    供 technical.py / event_detector.py / market_regime.py 直接拿去用，不用再重新查一次DB。"""
    klines_by_symbol: dict[str, dict[str, list[Candle]]] = {}

    with httpx.Client() as client:
        for asset in assets:
            asset_id = db.upsert_asset(conn, asset.symbol, asset.coingecko_id, asset.tier)

            try:
                klines_by_symbol[asset.symbol] = collect_klines(conn, asset_id, asset, client)
            except Exception as exc:  # noqa: BLE001
                logger.error("%s K線collector發生非預期錯誤：%s", asset.symbol, exc)
                klines_by_symbol[asset.symbol] = {tf: [] for tf in _KLINE_TIMEFRAMES}

            collect_derivatives(conn, asset_id, asset, client)
            collect_market_context(conn, asset_id, asset, coingecko_api_key, client)

    return klines_by_symbol
