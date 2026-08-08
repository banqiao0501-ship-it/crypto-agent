"""
YouTube collector。

刻意不用官方 YouTube Data API（需要另外申請API key、還有quota限制），
改用兩個不需要金鑰的公開管道：

1. 頻道RSS feed（https://www.youtube.com/feeds/videos.xml?channel_id=...）
   → 取得該頻道最新影片清單（video_id、標題、發布時間）。
   官方就有提供這個RSS，不是爬蟲，很穩定。

2. youtube-transcript-api（非官方套件）
   → 用video_id抓逐字稿。這是目前免金鑰抓字幕最普遍的做法，
     但要注意：這套件偶爾會因為YouTube改版而壞掉，跑cron時如果連續失敗，
     去查 https://github.com/jdepoix/youtube-transcript-api 的issue通常能找到解法或更新版本。
     另外如果之後改成部署在雲端主機（而非自己電腦）跑，IP可能被YouTube限流，
     屆時再考慮加proxy或改用付費的逐字稿服務。

頻道RSS只用「@handle」是拿不到的，YouTube RSS需要channel_id（UCxxxx格式），
所以第一次遇到新頻道時，會先從頻道首頁HTML解析出channel_id，存進DB快取，
之後就不用每次都重新解析。
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

import httpx
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)

from app.config import YoutubeChannel
from app.database import db
from app.utils.logger import get_logger

logger = get_logger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
# 依序嘗試這幾種pattern，YouTube頁面結構偶爾會變，所以不只靠一種寫法
_CHANNEL_ID_PATTERNS = [
    re.compile(r'"channelId":"(UC[a-zA-Z0-9_-]{22})"'),
    re.compile(r'"externalId":"(UC[a-zA-Z0-9_-]{22})"'),
    re.compile(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[a-zA-Z0-9_-]{22})"'),
    re.compile(r'<meta itemprop="channelId" content="(UC[a-zA-Z0-9_-]{22})"'),
]


@dataclass
class VideoMeta:
    video_id: str
    title: str
    published_at: str  # ISO8601
    url: str


def resolve_channel_id(handle: str, client: httpx.Client) -> str:
    """從 https://www.youtube.com/@handle 的頁面HTML裡找出channel_id。"""
    resp = client.get(f"https://www.youtube.com/@{handle}", headers=_HEADERS, timeout=15)
    resp.raise_for_status()

    for pattern in _CHANNEL_ID_PATTERNS:
        match = pattern.search(resp.text)
        if match:
            return match.group(1)

    # 全部pattern都找不到：把原始HTML存檔，方便回頭檢查YouTube到底回傳了什麼內容
    debug_path = Path("logs") / f"debug_youtube_{handle}.html"
    debug_path.parent.mkdir(parents=True, exist_ok=True)
    debug_path.write_text(resp.text, encoding="utf-8")
    raise ValueError(
        f"無法從 @{handle} 的頁面解析出channel_id，YouTube可能改版了。"
        f"原始HTML已存到 {debug_path}，可以打開看看裡面實際內容（搜尋UC開頭的字串）。"
    )


def fetch_latest_videos(channel_id: str, client: httpx.Client, max_results: int = 5) -> list[VideoMeta]:
    """讀頻道RSS，回傳最新的影片清單（RSS本身就是照時間排序，通常回傳最新15支）。"""
    resp = client.get(
        "https://www.youtube.com/feeds/videos.xml",
        params={"channel_id": channel_id},
        headers=_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    root = ElementTree.fromstring(resp.text)

    videos: list[VideoMeta] = []
    for entry in root.findall("atom:entry", _ATOM_NS)[:max_results]:
        video_id = entry.findtext("yt:videoId", namespaces=_ATOM_NS)
        title = entry.findtext("atom:title", namespaces=_ATOM_NS) or ""
        published = entry.findtext("atom:published", namespaces=_ATOM_NS) or ""
        if not video_id:
            continue
        videos.append(
            VideoMeta(
                video_id=video_id,
                title=title.strip(),
                published_at=published,
                url=f"https://www.youtube.com/watch?v={video_id}",
            )
        )
    return videos


def is_short(video_id: str, client: httpx.Client) -> bool:
    """判斷這支影片是不是YouTube Shorts。

    YouTube沒有官方API可以直接查這件事，這裡用一個常見的非官方技巧：
    對 youtube.com/shorts/{id} 發HEAD請求，回應200代表是Shorts、303轉址代表是一般影片。
    這不是官方保證的行為，YouTube改版有可能讓它失效——失效或請求出錯時，
    保守判斷成「不是Shorts」（寧可多抓一支，也不要誤刪正常影片）。
    """
    try:
        resp = client.head(
            f"https://www.youtube.com/shorts/{video_id}",
            headers=_HEADERS, follow_redirects=False, timeout=10,
        )
        return resp.status_code == 200
    except Exception as exc:  # noqa: BLE001
        logger.info("判斷影片 %s 是否為Shorts時發生錯誤，保守當作不是Shorts：%s", video_id, exc)
        return False


def fetch_transcript(video_id: str) -> str | None:
    """抓逐字稿，優先中文，抓不到就退而求其次抓英文或自動翻譯。抓不到就回傳None（不要讓整個collector中斷）。

    注意：youtube-transcript-api在1.0版做過一次大改版，API從class的靜態方法（0.6.x）
    改成要先建立實例（1.x）。這裡是照1.x的寫法。如果你裝到的版本又不一樣，
    去 https://github.com/jdepoix/youtube-transcript-api 對一下目前版本的README範例。
    """
    try:
        ytt_api = YouTubeTranscriptApi()
        transcript_list = ytt_api.list(video_id)
        try:
            transcript = transcript_list.find_transcript(["zh-Hant", "zh-TW", "zh-Hans", "zh", "en"])
        except NoTranscriptFound:
            # 找不到偏好語言，就抓第一個可用的，並嘗試翻譯成繁中
            transcript = next(iter(transcript_list))
            if not transcript.language_code.startswith("zh"):
                try:
                    transcript = transcript.translate("zh-Hant")
                except Exception:  # noqa: BLE001 - 翻譯失敗就用原文，不要整支失敗
                    pass
        fetched = transcript.fetch()
        return " ".join(snippet.text for snippet in fetched if getattr(snippet, "text", None))
    except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable) as exc:
        logger.info("影片 %s 沒有可用逐字稿：%s", video_id, exc)
        return None
    except Exception as exc:  # noqa: BLE001 - collector要盡量不因單一影片而整批中斷
        logger.warning("抓取影片 %s 逐字稿時發生非預期錯誤：%s", video_id, exc)
        return None


def collect(conn: sqlite3.Connection, channels: list[YoutubeChannel]) -> int:
    """主入口：對每個頻道抓最新影片，新影片才抓逐字稿並存進DB。回傳這次新增的筆數。"""
    new_count = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    with httpx.Client() as client:
        for channel in channels:
            source_id = db.upsert_source(
                conn, name=channel.name, type_="youtube",
                handle_or_url=channel.handle, reliability=channel.reliability,
            )
            channel_id = db.get_source_channel_id(conn, source_id)
            if not channel_id:
                try:
                    channel_id = resolve_channel_id(channel.handle, client)
                except Exception as exc:  # noqa: BLE001
                    logger.error("解析頻道 %s 的channel_id失敗：%s", channel.name, exc)
                    continue
                db.upsert_source(
                    conn, name=channel.name, type_="youtube",
                    handle_or_url=channel.handle, reliability=channel.reliability,
                    channel_id=channel_id,
                )

            try:
                videos = fetch_latest_videos(channel_id, client)
            except Exception as exc:  # noqa: BLE001
                logger.error("抓取頻道 %s 最新影片清單失敗：%s", channel.name, exc)
                continue

            for video in videos:
                if is_short(video.video_id, client):
                    logger.info("影片 %s（%s）是Shorts，略過不處理", video.video_id, video.title)
                    continue

                transcript = fetch_transcript(video.video_id)
                if transcript is None:
                    continue
                inserted = db.insert_raw_content(
                    conn,
                    source_id=source_id,
                    external_id=video.video_id,
                    content_type="youtube_transcript",
                    title=video.title,
                    content=transcript,
                    url=video.url,
                    published_at=video.published_at,
                    collected_at=now_iso,
                )
                if inserted:
                    new_count += 1
                    logger.info("新影片入庫：%s - %s", channel.name, video.title)

    return new_count
