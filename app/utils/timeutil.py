"""
時區處理原則：資料庫一律存UTC（ISO8601格式，已經是這樣做），但「今天的日期」「顯示給使用者看的
時間」要明確地用台北時間，不要依賴作業系統的本地時區設定（date.today()這種寫法會受電腦時區影響，
如果之後把這個agent搬到用UTC時區的雲端主機，日期判斷就會跟預期不一樣）。

台灣不實施日光節約時間，所以台北時間就是穩定的UTC+8，用固定offset比用zoneinfo資料庫更簡單、
更不會有額外的套件依賴問題（zoneinfo在Windows上需要tzdata套件才能查IANA時區資料庫，
雖然pandas已經間接裝了tzdata，但既然台北時間本來就是固定offset，沒必要繞這條路）。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

TAIPEI_TZ = timezone(timedelta(hours=8))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_taipei() -> datetime:
    return now_utc().astimezone(TAIPEI_TZ)


def utc_to_taipei(dt: datetime) -> datetime:
    """把一個datetime轉成台北時間顯示。如果傳進來的dt沒有時區資訊（naive），假設它本來就是UTC。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TAIPEI_TZ)


def taipei_today() -> date:
    """今天的日期，以台北時間為準，不受執行程式的作業系統時區影響。"""
    return now_taipei().date()
