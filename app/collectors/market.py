"""
市場數據 collector。

分兩塊，職責不同：
1. CoinGecko：抓現貨價格序列（不是完整OHLC，是逐點價格，technical.py會拿這個序列
   自己算EMA/RSI——技術指標本來就是用收盤價序列算的，不一定需要完整K棒）。
   如果CoinGecko的 free/demo tier 對 interval 參數的規則跟這裡假設的不一樣
   （官方時常調整免費方案的細節），實際跑起來如果發現抓到的區間/顆粒度怪怪的，
   去 https://docs.coingecko.com/reference/coins-id-market-chart 對一下最新規則。

2. Binance / Bybit：抓Funding Rate跟Open Interest（衍生品數據）。這兩家的市場數據
   端點都是公開的，不需要API key、不需要掛單權限。

這個檔案只負責「抓資料、存進market_snapshots / derivative_snapshots」，
不算技術指標——算指標是 app/analysis/technical.py 的工作。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import httpx

from app.config import Asset
from app.database import db
from app.utils.logger import get_logger

logger = get_logger(__name__)

_CG_BASE = "https://api.coingecko.com/api/v3"
_BINGX_BASE = "https://open-api.bingx.com"
_BINANCE_BASE = "https://fapi.binance.com"
_BYBIT_BASE = "https://api.bybit.com"


# ---------- CoinGecko：價格序列 ----------

def fetch_price_series(
    coingecko_id: str, days: int, api_key: str, client: httpx.Client,
) -> list[tuple[datetime, float, float]]:
    """回傳 [(timestamp, price, volume), ...]，由CoinGecko依days自動決定顆粒度
    （官方規則大致是：days<=1約5分鐘一點、days<=90約1小時一點、更長則約1天一點）。
    volume是market_chart回應裡的total_volumes，之前版本沒有取用，Market Regime要用到。"""
    headers = {"x-cg-demo-api-key": api_key} if api_key else {}
    resp = client.get(
        f"{_CG_BASE}/coins/{coingecko_id}/market_chart",
        params={"vs_currency": "usd", "days": days},
        headers=headers,
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    prices = data.get("prices", [])
    volumes = dict(data.get("total_volumes", []))  # {ts_ms: volume}
    return [
        (datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc), price, volumes.get(ts_ms, 0.0))
        for ts_ms, price in prices
    ]


def collect_prices(
    conn: sqlite3.Connection, asset_id: int, coingecko_id: str, api_key: str, client: httpx.Client,
) -> dict[str, list[tuple[datetime, float, float]]]:
    """抓兩段序列：近90天（給1h/4h技術分析用）+ 近250天（給1d技術分析，EMA200需要夠長的歷史）。
    回傳原始序列給 technical.py / market_regime.py 用，同時把資料存進market_snapshots方便之後回顧。"""
    series_short = fetch_price_series(coingecko_id, days=90, api_key=api_key, client=client)
    series_long = fetch_price_series(coingecko_id, days=250, api_key=api_key, client=client)

    for ts, price, volume in series_short:
        db.insert_market_snapshot(
            conn, asset_id=asset_id, timestamp=ts.isoformat(), timeframe="1h",
            open_=price, high=price, low=price, close=price, volume=volume,
        )
    for ts, price, volume in series_long:
        db.insert_market_snapshot(
            conn, asset_id=asset_id, timestamp=ts.isoformat(), timeframe="1d",
            open_=price, high=price, low=price, close=price, volume=volume,
        )

    return {"short": series_short, "long": series_long}


# ---------- BingX / Binance / Bybit：衍生品數據 ----------

def fetch_bingx_derivatives(symbol: str, client: httpx.Client) -> tuple[float | None, float | None]:
    """BingX是使用者實際交易的所在，優先用它的數據。symbol格式為「BTC-USDT」（注意有連字號，跟Binance不同）。
    這兩個quote端點屬於公開市場數據，不需要API key/簽名。"""
    funding_rate = open_interest = None
    try:
        resp = client.get(
            f"{_BINGX_BASE}/openApi/swap/v2/quote/premiumIndex", params={"symbol": symbol}, timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        raw_rate = data.get("lastFundingRate")
        funding_rate = float(raw_rate) if raw_rate is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("BingX funding rate 抓取失敗（%s）：%s", symbol, exc)

    try:
        resp = client.get(
            f"{_BINGX_BASE}/openApi/swap/v2/quote/openInterest", params={"symbol": symbol}, timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        raw_oi = data.get("openInterest")
        open_interest = float(raw_oi) if raw_oi is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("BingX open interest 抓取失敗（%s）：%s", symbol, exc)

    return funding_rate, open_interest


def fetch_binance_derivatives(symbol: str, client: httpx.Client) -> tuple[float | None, float | None]:
    """回傳 (funding_rate, open_interest)。任何一個失敗就回傳該欄位為None，不整個拋錯。"""
    funding_rate = open_interest = None
    try:
        resp = client.get(f"{_BINANCE_BASE}/fapi/v1/premiumIndex", params={"symbol": symbol}, timeout=15)
        resp.raise_for_status()
        funding_rate = float(resp.json()["lastFundingRate"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Binance funding rate 抓取失敗（%s）：%s", symbol, exc)

    try:
        resp = client.get(f"{_BINANCE_BASE}/fapi/v1/openInterest", params={"symbol": symbol}, timeout=15)
        resp.raise_for_status()
        open_interest = float(resp.json()["openInterest"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Binance open interest 抓取失敗（%s）：%s", symbol, exc)

    return funding_rate, open_interest


def fetch_bybit_derivatives(symbol: str, client: httpx.Client) -> tuple[float | None, float | None]:
    """Bybit v5 tickers端點一次就有fundingRate跟openInterest，比Binance省一次呼叫。"""
    try:
        resp = client.get(
            f"{_BYBIT_BASE}/v5/market/tickers", params={"category": "linear", "symbol": symbol}, timeout=15,
        )
        resp.raise_for_status()
        result_list = resp.json().get("result", {}).get("list", [])
        if not result_list:
            return None, None
        item = result_list[0]
        funding_rate = float(item["fundingRate"]) if item.get("fundingRate") else None
        open_interest = float(item["openInterest"]) if item.get("openInterest") else None
        return funding_rate, open_interest
    except Exception as exc:  # noqa: BLE001
        logger.warning("Bybit衍生品數據抓取失敗（%s）：%s", symbol, exc)
        return None, None


def collect_derivatives(conn: sqlite3.Connection, asset_id: int, asset: Asset, client: httpx.Client) -> None:
    funding_rate = open_interest = None
    source = None

    if asset.bingx_symbol:
        funding_rate, open_interest = fetch_bingx_derivatives(asset.bingx_symbol, client)
        source = "bingx"

    if funding_rate is None and open_interest is None and asset.binance_symbol:
        funding_rate, open_interest = fetch_binance_derivatives(asset.binance_symbol, client)
        source = "binance"

    if funding_rate is None and open_interest is None and asset.bybit_symbol:
        funding_rate, open_interest = fetch_bybit_derivatives(asset.bybit_symbol, client)
        source = "bybit"

    if funding_rate is None and open_interest is None:
        logger.info("%s 三個交易所的衍生品數據都抓不到，略過", asset.symbol)
        return

    db.insert_derivative_snapshot(
        conn, asset_id=asset_id, timestamp=datetime.now(timezone.utc).isoformat(),
        funding_rate=funding_rate, open_interest=open_interest, source=source,
    )


# ---------- 主入口 ----------

def collect(
    conn: sqlite3.Connection, assets: list[Asset], coingecko_api_key: str,
) -> dict[str, dict[str, list[tuple[datetime, float, float]]]]:
    """對每個asset抓價格序列+衍生品數據。回傳 {symbol: {"short":[...], "long":[...]}}，
    供 technical.py 直接拿去算指標，不用再重新查一次DB。"""
    price_series_by_symbol: dict[str, dict[str, list[tuple[datetime, float, float]]]] = {}

    with httpx.Client() as client:
        for asset in assets:
            asset_id = db.upsert_asset(conn, asset.symbol, asset.coingecko_id, asset.tier)

            try:
                series = collect_prices(conn, asset_id, asset.coingecko_id, coingecko_api_key, client)
                price_series_by_symbol[asset.symbol] = series
            except Exception as exc:  # noqa: BLE001
                logger.error("%s 價格序列抓取失敗：%s", asset.symbol, exc)
                price_series_by_symbol[asset.symbol] = {"short": [], "long": []}

            collect_derivatives(conn, asset_id, asset, client)

    return price_series_by_symbol
