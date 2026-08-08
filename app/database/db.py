"""
資料庫層：SQLite。

設計原則：
- V1先不做完整的Event/EventSource正規化表，那是V2 Event Clustering才需要的東西。
  現在raw_contents + 一個 is_processed 欄位就夠用：AI每天彙整時，直接抓「今天新進來、
  還沒處理過」的raw_contents，加上market/technical snapshot，一起丟給AI做每日報告。
- 所有「原始資料」都先存進raw_contents，就算之後AI分析邏輯改了，原始資料還在，
  可以重新跑分析，不用重新爬一次。
- alerts表用來做「冷卻機制」：同一個幣種同一種trigger，短時間內不要重複推播轟炸使用者。
"""
from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT UNIQUE NOT NULL,
    coingecko_id  TEXT NOT NULL,
    tier          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT UNIQUE NOT NULL,
    type          TEXT NOT NULL,          -- 'youtube' | 'news'
    handle_or_url TEXT NOT NULL,
    channel_id    TEXT,                   -- youtube解析出來的channel_id，快取用，避免每次都重新解析
    reliability   REAL NOT NULL DEFAULT 0.4
);

CREATE TABLE IF NOT EXISTS raw_contents (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id      INTEGER NOT NULL REFERENCES sources(id),
    external_id    TEXT NOT NULL,         -- video_id 或 jin10快訊的hash
    content_type   TEXT NOT NULL,         -- 'youtube_transcript' | 'jin10_news'
    title          TEXT,
    content        TEXT,
    url            TEXT,
    published_at   TEXT,                  -- ISO8601字串
    collected_at   TEXT NOT NULL,
    is_processed   INTEGER NOT NULL DEFAULT 0,  -- 是否已經被AI每日報告消化過
    UNIQUE(source_id, external_id)
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id   INTEGER NOT NULL REFERENCES assets(id),
    timestamp  TEXT NOT NULL,
    timeframe  TEXT NOT NULL,             -- '1h' | '4h' | '1d'
    open       REAL, high REAL, low REAL, close REAL, volume REAL,
    UNIQUE(asset_id, timeframe, timestamp)
);

CREATE TABLE IF NOT EXISTS derivative_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id       INTEGER NOT NULL REFERENCES assets(id),
    timestamp      TEXT NOT NULL,
    funding_rate   REAL,
    open_interest  REAL,
    source         TEXT NOT NULL,         -- 'binance' | 'bybit'
    UNIQUE(asset_id, timestamp, source)
);

CREATE TABLE IF NOT EXISTS technical_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id     INTEGER NOT NULL REFERENCES assets(id),
    timestamp    TEXT NOT NULL,
    timeframe    TEXT NOT NULL,
    rsi          REAL,
    ema20        REAL,
    ema50        REAL,
    ema200       REAL,
    trend        TEXT,                   -- 'bullish' | 'bearish' | 'neutral'
    support      TEXT,                   -- JSON array
    resistance   TEXT,                   -- JSON array
    UNIQUE(asset_id, timeframe, timestamp)
);

CREATE TABLE IF NOT EXISTS reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    report_type  TEXT NOT NULL,          -- 'daily' | 'alert'
    report_date  TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_text TEXT NOT NULL,
    sent_at      TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id      INTEGER NOT NULL REFERENCES assets(id),
    trigger_type  TEXT NOT NULL,         -- 'price_move' | 'volume_spike' | 'rsi_extreme'
    severity      TEXT NOT NULL,
    trigger_data  TEXT NOT NULL,         -- JSON
    triggered_at  TEXT NOT NULL,
    sent_at       TEXT
);

CREATE TABLE IF NOT EXISTS market_regime_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    TEXT NOT NULL,
    regime       TEXT NOT NULL,          -- 'Bullish Trend' | 'Bearish Trend' | 'Range' | 'High Volatility' | 'Risk-off'
    trend        TEXT NOT NULL,
    momentum     TEXT NOT NULL,
    volatility   TEXT NOT NULL,
    volume       TEXT NOT NULL,
    reason       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key       TEXT,
    title           TEXT NOT NULL,
    summary         TEXT,
    category        TEXT,
    impact          TEXT,
    sentiment       TEXT,
    related_assets  TEXT,                -- JSON array
    reliability_score REAL NOT NULL DEFAULT 0,  -- 依來源可信度加總算出，不是AI自己報的信心值
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_sources (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL REFERENCES events(id),
    content_id  INTEGER NOT NULL REFERENCES raw_contents(id),
    UNIQUE(event_id, content_id)
);
"""


@contextlib.contextmanager
def get_connection(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path) -> None:
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA)


def upsert_asset(conn: sqlite3.Connection, symbol: str, coingecko_id: str, tier: int) -> int:
    conn.execute(
        """INSERT INTO assets (symbol, coingecko_id, tier) VALUES (?, ?, ?)
           ON CONFLICT(symbol) DO UPDATE SET coingecko_id=excluded.coingecko_id, tier=excluded.tier""",
        (symbol, coingecko_id, tier),
    )
    row = conn.execute("SELECT id FROM assets WHERE symbol = ?", (symbol,)).fetchone()
    return row["id"]


def upsert_source(
    conn: sqlite3.Connection, name: str, type_: str, handle_or_url: str,
    reliability: float = 0.4, channel_id: str | None = None,
) -> int:
    conn.execute(
        """INSERT INTO sources (name, type, handle_or_url, reliability, channel_id)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET
               handle_or_url=excluded.handle_or_url,
               reliability=excluded.reliability,
               channel_id=COALESCE(excluded.channel_id, sources.channel_id)""",
        (name, type_, handle_or_url, reliability, channel_id),
    )
    row = conn.execute("SELECT id FROM sources WHERE name = ?", (name,)).fetchone()
    return row["id"]


def get_source_channel_id(conn: sqlite3.Connection, source_id: int) -> str | None:
    row = conn.execute("SELECT channel_id FROM sources WHERE id = ?", (source_id,)).fetchone()
    return row["channel_id"] if row else None


def insert_raw_content(
    conn: sqlite3.Connection, *, source_id: int, external_id: str, content_type: str,
    title: str, content: str, url: str, published_at: str, collected_at: str,
) -> bool:
    """回傳True代表是新資料，False代表這筆資料之前已經抓過了（重複，被UNIQUE擋掉）。"""
    try:
        conn.execute(
            """INSERT INTO raw_contents
               (source_id, external_id, content_type, title, content, url, published_at, collected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (source_id, external_id, content_type, title, content, url, published_at, collected_at),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def get_unprocessed_contents(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT raw_contents.*, sources.name AS source_name, sources.reliability AS source_reliability
           FROM raw_contents JOIN sources ON raw_contents.source_id = sources.id
           WHERE is_processed = 0
           ORDER BY published_at ASC"""
    ).fetchall()


def mark_contents_processed(conn: sqlite3.Connection, content_ids: list[int]) -> None:
    if not content_ids:
        return
    placeholders = ",".join("?" for _ in content_ids)
    conn.execute(f"UPDATE raw_contents SET is_processed = 1 WHERE id IN ({placeholders})", content_ids)


def insert_market_snapshot(
    conn: sqlite3.Connection, *, asset_id: int, timestamp: str, timeframe: str,
    open_: float, high: float, low: float, close: float, volume: float,
) -> None:
    conn.execute(
        """INSERT INTO market_snapshots (asset_id, timestamp, timeframe, open, high, low, close, volume)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(asset_id, timeframe, timestamp) DO UPDATE SET
               open=excluded.open, high=excluded.high, low=excluded.low,
               close=excluded.close, volume=excluded.volume""",
        (asset_id, timestamp, timeframe, open_, high, low, close, volume),
    )


def insert_derivative_snapshot(
    conn: sqlite3.Connection, *, asset_id: int, timestamp: str,
    funding_rate: float | None, open_interest: float | None, source: str,
) -> None:
    conn.execute(
        """INSERT INTO derivative_snapshots (asset_id, timestamp, funding_rate, open_interest, source)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(asset_id, timestamp, source) DO UPDATE SET
               funding_rate=excluded.funding_rate, open_interest=excluded.open_interest""",
        (asset_id, timestamp, funding_rate, open_interest, source),
    )


def insert_technical_snapshot(
    conn: sqlite3.Connection, *, asset_id: int, timestamp: str, timeframe: str,
    rsi: float | None, ema20: float | None, ema50: float | None, ema200: float | None,
    trend: str, support: str, resistance: str,
) -> None:
    conn.execute(
        """INSERT INTO technical_snapshots
           (asset_id, timestamp, timeframe, rsi, ema20, ema50, ema200, trend, support, resistance)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(asset_id, timeframe, timestamp) DO UPDATE SET
               rsi=excluded.rsi, ema20=excluded.ema20, ema50=excluded.ema50, ema200=excluded.ema200,
               trend=excluded.trend, support=excluded.support, resistance=excluded.resistance""",
        (asset_id, timestamp, timeframe, rsi, ema20, ema50, ema200, trend, support, resistance),
    )


def get_latest_derivative(conn: sqlite3.Connection, asset_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT * FROM derivative_snapshots WHERE asset_id = ?
           ORDER BY timestamp DESC LIMIT 1""",
        (asset_id,),
    ).fetchone()


def get_latest_technical(conn: sqlite3.Connection, asset_id: int, timeframe: str) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT * FROM technical_snapshots WHERE asset_id = ? AND timeframe = ?
           ORDER BY timestamp DESC LIMIT 1""",
        (asset_id, timeframe),
    ).fetchone()


def insert_report(
    conn: sqlite3.Connection, *, report_type: str, report_date: str,
    content_json: str, content_text: str, sent_at: str | None,
) -> int:
    cur = conn.execute(
        """INSERT INTO reports (report_type, report_date, content_json, content_text, sent_at)
           VALUES (?, ?, ?, ?, ?)""",
        (report_type, report_date, content_json, content_text, sent_at),
    )
    return cur.lastrowid


def recent_alert_exists(conn: sqlite3.Connection, asset_id: int, trigger_type: str, since_iso: str) -> bool:
    """冷卻機制：檢查同一幣種同一種trigger，在since_iso之後是否已經發過警報。"""
    row = conn.execute(
        """SELECT 1 FROM alerts WHERE asset_id = ? AND trigger_type = ? AND triggered_at >= ?
           LIMIT 1""",
        (asset_id, trigger_type, since_iso),
    ).fetchone()
    return row is not None


def insert_alert(
    conn: sqlite3.Connection, *, asset_id: int, trigger_type: str, severity: str,
    trigger_data: str, triggered_at: str, sent_at: str | None,
) -> int:
    cur = conn.execute(
        """INSERT INTO alerts (asset_id, trigger_type, severity, trigger_data, triggered_at, sent_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (asset_id, trigger_type, severity, trigger_data, triggered_at, sent_at),
    )
    return cur.lastrowid


def insert_market_regime_snapshot(
    conn: sqlite3.Connection, *, timestamp: str, regime: str, trend: str,
    momentum: str, volatility: str, volume: str, reason: str,
) -> int:
    cur = conn.execute(
        """INSERT INTO market_regime_snapshots (timestamp, regime, trend, momentum, volatility, volume, reason)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (timestamp, regime, trend, momentum, volatility, volume, reason),
    )
    return cur.lastrowid


def get_latest_market_regime(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM market_regime_snapshots ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()


def insert_event(
    conn: sqlite3.Connection, *, event_key: str, title: str, summary: str, category: str,
    impact: str, sentiment: str, related_assets: str, reliability_score: float, created_at: str,
) -> int:
    cur = conn.execute(
        """INSERT INTO events
           (event_key, title, summary, category, impact, sentiment, related_assets, reliability_score, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (event_key, title, summary, category, impact, sentiment, related_assets, reliability_score, created_at),
    )
    return cur.lastrowid


def insert_event_source(conn: sqlite3.Connection, *, event_id: int, content_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO event_sources (event_id, content_id) VALUES (?, ?)",
        (event_id, content_id),
    )
