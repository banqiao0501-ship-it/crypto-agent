"""
jin10.com 快訊 collector。

【重要提醒】jin10首頁的快訊是伺服器端渲染的純文字內容（我事先確認過，不是像
coinglass那種完全靠JS動態載入的頁面），但我沒辦法直接看到它實際的HTML標籤/
class名稱（我這邊的網頁擷取工具只會回傳轉換過的文字內容，看不到原始HTML）。
所以下面的 _ITEM_SELECTOR / _TIME_SELECTOR 是「最可能」的猜測寫法，
第一次跑之前，你需要：
    1. 打開瀏覽器開發者工具（F12）看一下 jin10.com 首頁快訊區塊的實際HTML結構
    2. 對照下面選到的內容跟網頁上是否一致，不一致就照實際的class/tag調整

這個檔案的其他部分（去重、加密貨幣關鍵字過濾、存DB）都跟HTML結構無關，不用改。
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from app.config import CRYPTO_KEYWORDS, JIN10_URL
from app.database import db
from app.utils.logger import get_logger

logger = get_logger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# === 這兩行是最可能需要你依實際HTML調整的地方 ===
_ITEM_SELECTOR = "div.jin10-flash-item, div[class*='flash-item'], li[class*='flash']"
_TIME_SELECTOR = "[class*='time']"


def _is_crypto_relevant(text: str) -> bool:
    return any(keyword.lower() in text.lower() for keyword in CRYPTO_KEYWORDS)


def _content_hash(text: str) -> str:
    """用內容算hash當external_id，避免同一則快訊因為頁面改版重新抓到時被當成新資料。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def fetch_flash_items(client: httpx.Client) -> list[dict]:
    resp = client.get(JIN10_URL, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    items: list[dict] = []
    nodes = soup.select(_ITEM_SELECTOR)
    logger.info("jin10 selector找到%d個候選項目（selector=%s）", len(nodes), _ITEM_SELECTOR)

    if not nodes:
        debug_path = Path("logs") / "debug_jin10.html"
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        debug_path.write_text(resp.text, encoding="utf-8")
        logger.warning(
            "jin10快訊選不到任何項目，原始HTML已存到 %s，"
            "請用瀏覽器DevTools確認實際HTML結構並更新 _ITEM_SELECTOR", debug_path
        )
        return items

    for node in nodes:
        text = node.get_text(" ", strip=True)
        if not text:
            continue
        time_node = node.select_one(_TIME_SELECTOR)
        time_text = time_node.get_text(strip=True) if time_node else ""
        items.append({"time_text": time_text, "text": text})

    return items


def collect(conn: sqlite3.Connection) -> int:
    """抓jin10快訊，過濾出跟加密貨幣有關的項目，新項目才存進DB。回傳這次新增的筆數。"""
    source_id = db.upsert_source(
        conn, name="jin10快訊", type_="news", handle_or_url=JIN10_URL, reliability=0.8,
    )
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    today_str = now.strftime("%Y-%m-%d")

    new_count = 0
    with httpx.Client() as client:
        try:
            items = fetch_flash_items(client)
        except Exception as exc:  # noqa: BLE001
            logger.error("抓取jin10快訊失敗：%s", exc)
            return 0

        for item in items:
            text = item["text"]
            if not _is_crypto_relevant(text):
                continue

            external_id = _content_hash(text)
            inserted = db.insert_raw_content(
                conn,
                source_id=source_id,
                external_id=external_id,
                content_type="jin10_news",
                title=text[:60],
                content=text,
                url=JIN10_URL,
                published_at=f"{today_str} {item['time_text']}".strip(),
                collected_at=now_iso,
            )
            if inserted:
                new_count += 1
                logger.info("新jin10快訊入庫：%s", text[:40])

    return new_count
