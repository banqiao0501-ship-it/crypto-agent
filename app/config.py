"""
集中管理所有設定：環境變數、關注幣種清單、YouTube頻道清單。

之後如果要新增/移除幣種或頻道，只需要改這個檔案，不用去每個collector裡面找。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# 專案根目錄（這個檔案的上一層）
BASE_DIR = Path(__file__).resolve().parent.parent

# 載入 .env（本機開發用；正式在cron上跑時環境變數也可以直接由系統注入）
load_dotenv(BASE_DIR / ".env")


def _require(name: str) -> str:
    """讀取一個「一定要有值」的環境變數，沒有就直接報錯，而不是讓程式帶著空字串跑到一半才炸。"""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"環境變數 {name} 沒有設定，請檢查 .env 檔案（可以參考 .env.example）。"
        )
    return value


def _optional(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Asset:
    """一個關注的幣種。"""
    symbol: str          # 幣種代號，例如 BTC
    coingecko_id: str    # CoinGecko用的id，例如 bitcoin
    bingx_symbol: str    # BingX合約代號，格式為「BTC-USDT」（使用者實際交易所，優先使用）
    binance_symbol: str  # Binance合約代號，例如 BTCUSDT（BingX抓不到時的備援）
    bybit_symbol: str    # Bybit合約代號，通常跟binance一樣（再往下一層備援）
    tier: int            # 分層：1=BTC/ETH主流幣，2=主流山寨幣，3=敘事/題材幣


# 關注幣種清單（依照 tier 分層，分析時給予不同重點）
ASSETS: list[Asset] = [
    Asset("BTC", "bitcoin", "BTC-USDT", "BTCUSDT", "BTCUSDT", tier=1),
    Asset("ETH", "ethereum", "ETH-USDT", "ETHUSDT", "ETHUSDT", tier=1),
    Asset("SOL", "solana", "SOL-USDT", "SOLUSDT", "SOLUSDT", tier=2),
    Asset("BNB", "binancecoin", "BNB-USDT", "BNBUSDT", "BNBUSDT", tier=2),
    Asset("XRP", "ripple", "XRP-USDT", "XRPUSDT", "XRPUSDT", tier=2),
    Asset("LINK", "chainlink", "LINK-USDT", "LINKUSDT", "LINKUSDT", tier=3),
    Asset("HYPE", "hyperliquid", "HYPE-USDT", "", "HYPEUSDT", tier=3),
    Asset("ONDO", "ondo-finance", "ONDO-USDT", "ONDOUSDT", "ONDOUSDT", tier=3),
]


@dataclass(frozen=True)
class YoutubeChannel:
    """一個要追蹤的YouTube頻道。"""
    name: str     # 顯示用名稱
    handle: str   # @handle（不含@），用來解析channel_id
    reliability: float = 0.4  # 來源可信度權重，KOL類預設0.4


YOUTUBE_CHANNELS: list[YoutubeChannel] = [
    YoutubeChannel("腦哥Chill塊鏈", "brainbrocrypto"),
    YoutubeChannel("墨山貓MØC", "CryptoMOC"),
    YoutubeChannel("大漂亮的K線日記", "GiantCutie-K"),
    YoutubeChannel("邦妮區塊鏈Bonnie Blockchain", "BonnieBlockchain"),
]

JIN10_URL = "https://www.jin10.com/index.html"

# jin10快訊裡用來判斷「是否跟加密貨幣有關」的關鍵字（避免把一堆總經/地緣政治新聞也塞進報告）
CRYPTO_KEYWORDS = [
    "比特幣", "以太坊", "加密貨幣", "虛擬貨幣", "數位貨幣", "穩定幣",
    "BTC", "ETH", "SOL", "BNB", "XRP", "SEC", "ETF", "Coinbase", "幣安",
    "區塊鏈", "crypto", "bitcoin", "ethereum",
]


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str
    coingecko_api_key: str
    line_channel_access_token: str
    line_user_id: str
    database_path: Path
    log_level: str
    log_path: Path
    assets: list[Asset] = field(default_factory=lambda: ASSETS)
    youtube_channels: list[YoutubeChannel] = field(default_factory=lambda: YOUTUBE_CHANNELS)


def load_settings() -> Settings:
    """統一入口：main.py 或任何模組要用設定時，呼叫這個函式拿到 Settings 物件。"""
    return Settings(
        anthropic_api_key=_require("ANTHROPIC_API_KEY"),
        coingecko_api_key=_optional("COINGECKO_API_KEY"),
        line_channel_access_token=_require("LINE_CHANNEL_ACCESS_TOKEN"),
        line_user_id=_require("LINE_USER_ID"),
        database_path=BASE_DIR / _optional("DATABASE_PATH", "data/crypto.db"),
        log_level=_optional("LOG_LEVEL", "INFO"),
        log_path=BASE_DIR / _optional("LOG_PATH", "logs/crypto-agent.log"),
    )
