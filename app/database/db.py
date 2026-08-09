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
from datetime import datetime
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
    mark_price     REAL,
    index_price    REAL,
    source         TEXT NOT NULL,         -- 'bingx' | 'binance' | 'bybit'
    UNIQUE(asset_id, timestamp, source)
);

CREATE TABLE IF NOT EXISTS market_context_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id            INTEGER NOT NULL REFERENCES assets(id),
    timestamp           TEXT NOT NULL,
    market_cap_usd      REAL,
    market_cap_rank     INTEGER,
    circulating_supply  REAL,
    total_supply        REAL
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
    macd         REAL,
    macd_signal  REAL,
    macd_hist    REAL,
    atr          REAL,
    volume_ratio REAL,
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
    trigger_type  TEXT NOT NULL,         -- 'price_move' | 'volume_spike' | 'rsi_extreme' | 'derivatives_anomaly'
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

CREATE TABLE IF NOT EXISTS reaction_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL REFERENCES events(id),
    asset       TEXT NOT NULL,
    window      TEXT NOT NULL,           -- '5m' | '15m' | '1h' | '4h' | '24h'
    due_at      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'done' | 'failed'
    created_at  TEXT NOT NULL,
    UNIQUE(event_id, asset, window)
);

CREATE TABLE IF NOT EXISTS event_reactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        INTEGER NOT NULL REFERENCES events(id),
    asset           TEXT NOT NULL,
    window          TEXT NOT NULL,
    baseline_price  REAL,
    event_price     REAL,
    reaction_price  REAL,
    baseline_return REAL,               -- 事件發生前60分鐘的價格變化（排除既有趨勢用）
    price_return    REAL,               -- 事件發生後到這個window的價格變化
    excess_return   REAL,               -- price_return - baseline_return，扣除既有趨勢後的「真正反應」
    volume_ratio    REAL,
    oi_change_pct   REAL,
    funding_rate    REAL,
    reaction_type   TEXT,               -- STRONG_POSITIVE|POSITIVE|NEUTRAL|NEGATIVE|STRONG_NEGATIVE|NO_REACTION
    created_at      TEXT NOT NULL,
    UNIQUE(event_id, asset, window)
);

CREATE TABLE IF NOT EXISTS kol_claims (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id          INTEGER REFERENCES raw_contents(id),
    source_name         TEXT NOT NULL,
    asset               TEXT,
    claim_type          TEXT,           -- market_outlook|price_target|support_resistance|breakout|
                                          -- breakdown|trend|entry_setup|risk_warning|macro_outlook|narrative
    direction           TEXT,           -- bullish|bearish|neutral
    time_horizon        TEXT,           -- short_term|mid_term|long_term|unspecified
    claim_text          TEXT,
    confidence          TEXT,           -- low|medium|high（KOL自己講話的語氣，不是AI的信心）
    entry_zone_low       REAL,
    entry_zone_high      REAL,
    invalidation_price   REAL,
    target_price         REAL,
    verifiable          INTEGER NOT NULL DEFAULT 0,
    unverifiable_reason  TEXT,
    source_timestamp     TEXT,          -- 影片內的時間戳記，方便回去對照原始發言
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kol_predictions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id            INTEGER NOT NULL REFERENCES kol_claims(id),
    asset               TEXT NOT NULL,
    direction           TEXT NOT NULL,
    target_price        REAL,
    invalidation_price  REAL,
    entry_zone_low      REAL,
    entry_zone_high     REAL,
    prediction_time      TEXT NOT NULL,  -- T0，一律用影片/內容的建立時間，不能事後用收盤價回推
    horizon_days         INTEGER NOT NULL,
    deadline             TEXT NOT NULL,  -- T0 + horizon_days
    status               TEXT NOT NULL DEFAULT 'pending',  -- pending | evaluated
    reference_price      REAL,           -- P0，prediction建立當下的價格（BingX為準）
    atr_value            REAL,           -- prediction建立當下的ATR絕對值，之後結算不會再改
    atr_percent          REAL,           -- atr_value / reference_price
    atr_timeframe        TEXT,           -- 這個ATR取自哪個timeframe（依horizon選擇：短天期用4h，長天期用1d）
    threshold            REAL,           -- 判斷方向預測對錯的門檻，V1直接=atr_percent，建立當下凍結
    max_price            REAL,
    min_price            REAL,
    close_price          REAL,           -- deadline當下的價格
    target_hit           INTEGER,
    invalidation_hit     INTEGER,
    directional_return   REAL,
    result                TEXT,          -- CORRECT|INCORRECT|PARTIAL|INCONCLUSIVE|UNSCORABLE
    evaluated_at          TEXT
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
        _migrate_missing_columns(conn)


# 表格schema演進時，如果是「幫既有表加欄位」（不是新增整張表），
# CREATE TABLE IF NOT EXISTS對已存在的表不會生效，要用ALTER TABLE補欄位。
# 這裡列出每次演進新增過的欄位，跑init_db時會自動檢查、補上缺少的，不會動到既有資料。
_COLUMN_MIGRATIONS = {
    "derivative_snapshots": {
        "mark_price": "REAL",
        "index_price": "REAL",
    },
    "technical_snapshots": {
        "macd": "REAL",
        "macd_signal": "REAL",
        "macd_hist": "REAL",
        "atr": "REAL",
        "volume_ratio": "REAL",
    },
}


def _migrate_missing_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _COLUMN_MIGRATIONS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for column, col_type in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


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
) -> int | None:
    """成功新增時回傳新的row id，這筆資料之前已經抓過（重複，被UNIQUE擋掉）時回傳None。
    呼叫端如果只是想判斷「有沒有新增」，直接用 if insert_raw_content(...): 就好——
    id一定是正整數，None是falsy，寫法不用改。"""
    try:
        cur = conn.execute(
            """INSERT INTO raw_contents
               (source_id, external_id, content_type, title, content, url, published_at, collected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (source_id, external_id, content_type, title, content, url, published_at, collected_at),
        )
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


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
    funding_rate: float | None, open_interest: float | None,
    mark_price: float | None, index_price: float | None, source: str,
) -> None:
    conn.execute(
        """INSERT INTO derivative_snapshots
           (asset_id, timestamp, funding_rate, open_interest, mark_price, index_price, source)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(asset_id, timestamp, source) DO UPDATE SET
               funding_rate=excluded.funding_rate, open_interest=excluded.open_interest,
               mark_price=excluded.mark_price, index_price=excluded.index_price""",
        (asset_id, timestamp, funding_rate, open_interest, mark_price, index_price, source),
    )


def insert_market_context_snapshot(
    conn: sqlite3.Connection, *, asset_id: int, timestamp: str,
    market_cap_usd: float | None, market_cap_rank: int | None,
    circulating_supply: float | None, total_supply: float | None,
) -> None:
    conn.execute(
        """INSERT INTO market_context_snapshots
           (asset_id, timestamp, market_cap_usd, market_cap_rank, circulating_supply, total_supply)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (asset_id, timestamp, market_cap_usd, market_cap_rank, circulating_supply, total_supply),
    )


def get_latest_market_context(conn: sqlite3.Connection, asset_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT * FROM market_context_snapshots WHERE asset_id = ?
           ORDER BY timestamp DESC LIMIT 1""",
        (asset_id,),
    ).fetchone()


def insert_technical_snapshot(
    conn: sqlite3.Connection, *, asset_id: int, timestamp: str, timeframe: str,
    rsi: float | None, ema20: float | None, ema50: float | None, ema200: float | None,
    macd: float | None, macd_signal: float | None, macd_hist: float | None,
    atr: float | None, volume_ratio: float | None,
    trend: str, support: str, resistance: str,
) -> None:
    conn.execute(
        """INSERT INTO technical_snapshots
           (asset_id, timestamp, timeframe, rsi, ema20, ema50, ema200,
            macd, macd_signal, macd_hist, atr, volume_ratio, trend, support, resistance)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(asset_id, timeframe, timestamp) DO UPDATE SET
               rsi=excluded.rsi, ema20=excluded.ema20, ema50=excluded.ema50, ema200=excluded.ema200,
               macd=excluded.macd, macd_signal=excluded.macd_signal, macd_hist=excluded.macd_hist,
               atr=excluded.atr, volume_ratio=excluded.volume_ratio,
               trend=excluded.trend, support=excluded.support, resistance=excluded.resistance""",
        (asset_id, timestamp, timeframe, rsi, ema20, ema50, ema200,
         macd, macd_signal, macd_hist, atr, volume_ratio, trend, support, resistance),
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


def get_event(conn: sqlite3.Connection, event_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()


def get_market_snapshot_near(
    conn: sqlite3.Connection, asset_id: int, timeframe: str, target_iso: str,
) -> sqlite3.Row | None:
    """找離target_iso時間點最近的一根K棒（不管在target之前或之後），
    用來在事件時間點附近沒有剛好對上的K棒時，找最接近的那根當作近似值。"""
    before = conn.execute(
        """SELECT * FROM market_snapshots WHERE asset_id = ? AND timeframe = ? AND timestamp <= ?
           ORDER BY timestamp DESC LIMIT 1""",
        (asset_id, timeframe, target_iso),
    ).fetchone()
    after = conn.execute(
        """SELECT * FROM market_snapshots WHERE asset_id = ? AND timeframe = ? AND timestamp > ?
           ORDER BY timestamp ASC LIMIT 1""",
        (asset_id, timeframe, target_iso),
    ).fetchone()

    if before is None:
        return after
    if after is None:
        return before

    target_dt = datetime.fromisoformat(target_iso)
    before_diff = abs((target_dt - datetime.fromisoformat(before["timestamp"])).total_seconds())
    after_diff = abs((datetime.fromisoformat(after["timestamp"]) - target_dt).total_seconds())
    return before if before_diff <= after_diff else after


def get_derivative_snapshot_near(
    conn: sqlite3.Connection, asset_id: int, target_iso: str,
) -> sqlite3.Row | None:
    """跟get_market_snapshot_near邏輯一樣，但用在derivative_snapshots（找最接近某時間點的衍生品數據）。"""
    before = conn.execute(
        """SELECT * FROM derivative_snapshots WHERE asset_id = ? AND timestamp <= ?
           ORDER BY timestamp DESC LIMIT 1""",
        (asset_id, target_iso),
    ).fetchone()
    after = conn.execute(
        """SELECT * FROM derivative_snapshots WHERE asset_id = ? AND timestamp > ?
           ORDER BY timestamp ASC LIMIT 1""",
        (asset_id, target_iso),
    ).fetchone()

    if before is None:
        return after
    if after is None:
        return before

    target_dt = datetime.fromisoformat(target_iso)
    before_diff = abs((target_dt - datetime.fromisoformat(before["timestamp"])).total_seconds())
    after_diff = abs((datetime.fromisoformat(after["timestamp"]) - target_dt).total_seconds())
    return before if before_diff <= after_diff else after


def insert_reaction_job(
    conn: sqlite3.Connection, *, event_id: int, asset: str, window: str, due_at: str, created_at: str,
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO reaction_jobs (event_id, asset, window, due_at, status, created_at)
           VALUES (?, ?, ?, ?, 'pending', ?)""",
        (event_id, asset, window, due_at, created_at),
    )


def get_due_reaction_jobs(conn: sqlite3.Connection, now_iso: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM reaction_jobs WHERE status = 'pending' AND due_at <= ?
           ORDER BY due_at ASC""",
        (now_iso,),
    ).fetchall()


def update_reaction_job_status(conn: sqlite3.Connection, job_id: int, status: str) -> None:
    conn.execute("UPDATE reaction_jobs SET status = ? WHERE id = ?", (status, job_id))


def insert_event_reaction(
    conn: sqlite3.Connection, *, event_id: int, asset: str, window: str,
    baseline_price: float | None, event_price: float | None, reaction_price: float | None,
    baseline_return: float | None, price_return: float | None, excess_return: float | None,
    volume_ratio: float | None, oi_change_pct: float | None, funding_rate: float | None,
    reaction_type: str, created_at: str,
) -> None:
    conn.execute(
        """INSERT INTO event_reactions
           (event_id, asset, window, baseline_price, event_price, reaction_price,
            baseline_return, price_return, excess_return, volume_ratio, oi_change_pct,
            funding_rate, reaction_type, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(event_id, asset, window) DO UPDATE SET
               baseline_price=excluded.baseline_price, event_price=excluded.event_price,
               reaction_price=excluded.reaction_price, baseline_return=excluded.baseline_return,
               price_return=excluded.price_return, excess_return=excluded.excess_return,
               volume_ratio=excluded.volume_ratio, oi_change_pct=excluded.oi_change_pct,
               funding_rate=excluded.funding_rate, reaction_type=excluded.reaction_type""",
        (event_id, asset, window, baseline_price, event_price, reaction_price,
         baseline_return, price_return, excess_return, volume_ratio, oi_change_pct,
         funding_rate, reaction_type, created_at),
    )


def get_recent_event_reactions(conn: sqlite3.Connection, since_iso: str) -> list[sqlite3.Row]:
    """給每日報告用：把最近完成的事件反應資料抓出來，交給AI解讀。"""
    return conn.execute(
        """SELECT event_reactions.*, events.title AS event_title, events.category AS event_category
           FROM event_reactions JOIN events ON event_reactions.event_id = events.id
           WHERE event_reactions.created_at >= ?
           ORDER BY event_reactions.created_at DESC""",
        (since_iso,),
    ).fetchall()


def insert_kol_claim(
    conn: sqlite3.Connection, *, content_id: int | None, source_name: str, asset: str,
    claim_type: str, direction: str, time_horizon: str, claim_text: str, confidence: str,
    entry_zone_low: float | None, entry_zone_high: float | None,
    invalidation_price: float | None, target_price: float | None,
    verifiable: bool, unverifiable_reason: str, source_timestamp: str, created_at: str,
) -> int:
    cur = conn.execute(
        """INSERT INTO kol_claims
           (content_id, source_name, asset, claim_type, direction, time_horizon, claim_text,
            confidence, entry_zone_low, entry_zone_high, invalidation_price, target_price,
            verifiable, unverifiable_reason, source_timestamp, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (content_id, source_name, asset, claim_type, direction, time_horizon, claim_text,
         confidence, entry_zone_low, entry_zone_high, invalidation_price, target_price,
         1 if verifiable else 0, unverifiable_reason, source_timestamp, created_at),
    )
    return cur.lastrowid


def insert_kol_prediction(
    conn: sqlite3.Connection, *, claim_id: int, asset: str, direction: str,
    target_price: float | None, invalidation_price: float | None,
    entry_zone_low: float | None, entry_zone_high: float | None,
    prediction_time: str, horizon_days: int, deadline: str,
    reference_price: float | None, atr_value: float | None, atr_percent: float | None,
    atr_timeframe: str | None, threshold: float | None,
) -> int:
    cur = conn.execute(
        """INSERT INTO kol_predictions
           (claim_id, asset, direction, target_price, invalidation_price,
            entry_zone_low, entry_zone_high, prediction_time, horizon_days, deadline, status,
            reference_price, atr_value, atr_percent, atr_timeframe, threshold)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)""",
        (claim_id, asset, direction, target_price, invalidation_price,
         entry_zone_low, entry_zone_high, prediction_time, horizon_days, deadline,
         reference_price, atr_value, atr_percent, atr_timeframe, threshold),
    )
    return cur.lastrowid


def get_recent_kol_claims(conn: sqlite3.Connection, since_iso: str) -> list[sqlite3.Row]:
    """給每日報告/敘事追蹤用：抓最近一段時間內的KOL claims。"""
    return conn.execute(
        "SELECT * FROM kol_claims WHERE created_at >= ? ORDER BY created_at DESC",
        (since_iso,),
    ).fetchall()


def get_market_snapshot_range_stats(
    conn: sqlite3.Connection, asset_id: int, timeframe: str, start_iso: str, end_iso: str,
) -> dict | None:
    """給預測結算用：某段時間範圍內的最高價、最低價、範圍內最後一根的收盤價。
    找不到任何資料時回傳None。"""
    rows = conn.execute(
        """SELECT high, low, close, timestamp FROM market_snapshots
           WHERE asset_id = ? AND timeframe = ? AND timestamp >= ? AND timestamp <= ?
           ORDER BY timestamp ASC""",
        (asset_id, timeframe, start_iso, end_iso),
    ).fetchall()
    if not rows:
        return None
    return {
        "max_price": max(r["high"] for r in rows),
        "min_price": min(r["low"] for r in rows),
        "close_price": rows[-1]["close"],
    }


def get_pending_kol_predictions_due(conn: sqlite3.Connection, now_iso: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM kol_predictions WHERE status = 'pending' AND deadline <= ? ORDER BY deadline ASC",
        (now_iso,),
    ).fetchall()


def update_kol_prediction_result(
    conn: sqlite3.Connection, *, prediction_id: int, max_price: float | None,
    min_price: float | None, close_price: float | None, target_hit: bool | None,
    invalidation_hit: bool | None, directional_return: float | None,
    result: str, evaluated_at: str,
) -> None:
    conn.execute(
        """UPDATE kol_predictions SET
               status = 'evaluated', max_price = ?, min_price = ?, close_price = ?,
               target_hit = ?, invalidation_hit = ?, directional_return = ?,
               result = ?, evaluated_at = ?
           WHERE id = ?""",
        (max_price, min_price, close_price,
         None if target_hit is None else int(target_hit),
         None if invalidation_hit is None else int(invalidation_hit),
         directional_return, result, evaluated_at, prediction_id),
    )
