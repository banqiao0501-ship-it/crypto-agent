"""
主程式入口。刻意切成幾個獨立的subcommand，讓cron可以用不同頻率各自排程：

    python -m app.main collect-youtube     # 抓YouTube逐字稿，建議一天排2~3次（晚上時段）
    python -m app.main collect-jin10       # 抓jin10快訊，建議30分鐘一次
    python -m app.main market-check        # 抓市場數據+算技術指標+檢查規則引擎告警，建議30分鐘一次
    python -m app.main daily-report        # 產生+推送每日報告，建議一天一次（例如早上8點）

每個subcommand都是獨立的一次性執行（跑完就結束），不是常駐程式，這樣才能用cron排程、
也才符合「不要Docker/Celery，先把複雜度壓到最低」的V1原則。

market-check 跟 daily-report 都會各自重新抓一次市場數據——這是刻意的：
每個subcommand都應該獨立可執行、獨立可測試，不依賴「前一個cron job有沒有跑成功」，
這樣任何一步失敗都不會連環壞掉。CoinGecko/Binance/Bybit這些呼叫都很輕量，
重複抓不會造成什麼負擔。
"""
from __future__ import annotations

import argparse
import re
import sys

import httpx

from app.ai import synthesizer
from app.analysis import event_detector, event_reactions, market_regime, prediction_evaluation, technical
from app.collectors import jin10, market, youtube
from app.config import Settings, load_settings
from app.database import db
from app.notify import line
from app.utils.logger import get_logger, setup_logging
from app.utils.timeutil import now_utc, taipei_today

logger = get_logger(__name__)


def _run_market_and_technical(conn, settings: Settings, status: dict[str, bool]) -> dict:
    try:
        klines_by_symbol = market.collect(conn, settings.assets, settings.coingecko_api_key)
        status["market_api"] = True
    except Exception:
        logger.exception("市場數據collector發生錯誤")
        status["market_api"] = False
        klines_by_symbol = {}

    technical_results = technical.run(conn, settings.assets, klines_by_symbol)

    try:
        regime_result = market_regime.compute(conn, settings.assets, klines_by_symbol, technical_results)
    except Exception:
        logger.exception("Market Regime計算發生錯誤")
        regime_result = None

    return {
        "technical_results": technical_results,
        "klines_by_symbol": klines_by_symbol,
        "regime_result": regime_result,
    }


def cmd_collect_youtube(settings: Settings) -> None:
    with db.get_connection(settings.database_path) as conn:
        try:
            count, new_videos = youtube.collect(conn, settings.youtube_channels)
            logger.info("YouTube collector完成，新增%d筆", count)
        except Exception:
            logger.exception("YouTube collector發生錯誤")
            return

        for video in new_videos:
            try:
                bullets = synthesizer.extract_youtube_claims(
                    conn, settings,
                    content_id=video["content_id"], source_name=video["channel_name"],
                    title=video["title"], transcript=video["transcript"],
                    published_at=video["published_at"],
                )
                text = line.format_youtube_notification(
                    video["channel_name"], video["title"], video["url"], bullets,
                )
                line.push_text(settings.line_channel_access_token, settings.line_user_id, text)
            except Exception:
                # 即時摘要/推播失敗不影響資料已經存進DB這件事——隔天8點日報一樣會統整到這支影片，
                # 只是少了這次的即時通知，不算嚴重錯誤，記log就好，不用中斷整個collector。
                logger.exception("影片即時摘要/推播失敗：%s", video.get("title"))


def cmd_collect_jin10(settings: Settings) -> None:
    with db.get_connection(settings.database_path) as conn:
        try:
            count = jin10.collect(conn)
            logger.info("jin10 collector完成，新增%d筆", count)
        except Exception:
            logger.exception("jin10 collector發生錯誤")


def cmd_market_check(settings: Settings) -> None:
    with db.get_connection(settings.database_path) as conn:
        status: dict[str, bool] = {}
        result = _run_market_and_technical(conn, settings, status)
        technical_results = result["technical_results"]

        try:
            triggered = event_detector.detect(
                conn, settings.assets, result["klines_by_symbol"], technical_results
            )
        except Exception:
            logger.exception("event_detector發生錯誤")
            triggered = []

        for alert in triggered:
            try:
                analysis = synthesizer.generate_alert_analysis(conn, settings, alert, technical_results)
                text = line.format_alert(alert, analysis)
                line.push_text(settings.line_channel_access_token, settings.line_user_id, text)
            except Exception:
                logger.exception("警報分析/推播失敗：%s", alert)

        try:
            completed = event_reactions.process_due_jobs(conn, settings.assets)
            if completed:
                logger.info("本次結算了%d筆事件反應追蹤任務", len(completed))
        except Exception:
            logger.exception("事件反應追蹤(event_reactions)發生錯誤")

        try:
            completed_predictions = prediction_evaluation.process_due_predictions(conn, settings.assets)
            if completed_predictions:
                logger.info("本次結算了%d筆KOL預測", len(completed_predictions))
        except Exception:
            logger.exception("KOL預測評估(prediction_evaluation)發生錯誤")


def cmd_daily_report(settings: Settings) -> None:
    with db.get_connection(settings.database_path) as conn:
        status: dict[str, bool] = {}
        result = _run_market_and_technical(conn, settings, status)
        technical_results = result["technical_results"]
        regime_result = result["regime_result"]

        try:
            event_reactions.process_due_jobs(conn, settings.assets)
        except Exception:
            logger.exception("事件反應追蹤(event_reactions)發生錯誤")

        try:
            prediction_evaluation.process_due_predictions(conn, settings.assets)
        except Exception:
            logger.exception("KOL預測評估(prediction_evaluation)發生錯誤")

        try:
            report = synthesizer.generate_daily_report(conn, settings, technical_results, regime_result)
            status["ai"] = True
        except Exception:
            logger.exception("AI每日報告產生失敗")
            status["ai"] = False
            return  # 沒有報告內容就不用往下推播了

        report_date = taipei_today().isoformat()
        text = line.format_daily_report(report, settings.assets, report_date, status, regime_result)

        try:
            line.push_text(settings.line_channel_access_token, settings.line_user_id, text)
            sent_at = now_utc().isoformat()
        except Exception:
            logger.exception("每日報告LINE推播失敗")
            sent_at = None

        sync_payload = line.build_sync_payload(report, settings.assets, report_date)
        line.sync_to_webhook(settings.webhook_sync_url, settings.webhook_sync_secret, sync_payload)

        import json

        db.insert_report(
            conn, report_type="daily", report_date=report_date,
            content_json=json.dumps(report, ensure_ascii=False),
            content_text=text, sent_at=sent_at,
        )


_VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/)([a-zA-Z0-9_-]{11})")


def _extract_video_id(video_url_or_id: str) -> str:
    """接受完整網址（watch?v=、youtu.be/、shorts/都可以）或是直接給11碼的video_id。"""
    match = _VIDEO_ID_RE.search(video_url_or_id)
    if match:
        return match.group(1)
    return video_url_or_id.strip()


def cmd_test_claim(settings: Settings, video_url_or_id: str) -> None:
    """手動測試用：直接指定一支YouTube影片，跑一次完整的claim抽取流程（不用等自然排程）。
    不會動到你原本4個頻道的正常抓取邏輯，用的是獨立的「手動測試」來源。"""
    video_id = _extract_video_id(video_url_or_id)
    logger.info("手動測試影片：%s（解析出的video_id：%s）", video_url_or_id, video_id)

    with db.get_connection(settings.database_path) as conn:
        with httpx.Client() as client:
            if youtube.is_short(video_id, client):
                logger.warning("這支影片被判斷為Shorts，不會抓逐字稿，測試中止：%s", video_id)
                return
            transcript = youtube.fetch_transcript(video_id)

        if transcript is None:
            logger.error("抓不到這支影片的逐字稿（很可能是字幕被關閉），測試中止：%s", video_id)
            return

        logger.info("逐字稿抓取成功，長度%d字，開始呼叫Claude做結構化claim抽取", len(transcript))

        source_id = db.upsert_source(
            conn, name="手動測試", type_="youtube", handle_or_url="manual-test", reliability=0.4,
        )
        now_iso = now_utc().isoformat()
        content_id = db.insert_raw_content(
            conn, source_id=source_id, external_id=video_id, content_type="youtube_transcript",
            title=f"手動測試影片 {video_id}", content=transcript,
            url=f"https://www.youtube.com/watch?v={video_id}",
            published_at=now_iso, collected_at=now_iso,
        )
        if content_id is None:
            # 這支影片之前測試過了，UNIQUE擋掉插入——查出既有的content_id繼續往下測
            row = conn.execute(
                "SELECT id FROM raw_contents WHERE source_id = ? AND external_id = ?",
                (source_id, video_id),
            ).fetchone()
            content_id = row["id"]
            logger.info("這支影片之前測試過了，沿用既有的content_id=%d繼續測試", content_id)

        bullets = synthesizer.extract_youtube_claims(
            conn, settings, content_id=content_id, source_name="手動測試",
            title=f"手動測試影片 {video_id}", transcript=transcript, published_at=now_iso,
        )

        print("\n=== Bullets（即時推播摘要）===")
        for b in bullets:
            print(f"- {b}")

        try:
            text = line.format_youtube_notification(
                "手動測試", f"手動測試影片 {video_id}",
                f"https://www.youtube.com/watch?v={video_id}", bullets,
            )
            line.push_text(settings.line_channel_access_token, settings.line_user_id, text)
            print("\n（已推播到LINE，跟正常collect-youtube流程收到的通知長一樣）")
        except Exception:
            logger.exception("test-claim推播LINE失敗（不影響上面終端機印出的測試結果）")

        claims = conn.execute("SELECT * FROM kol_claims WHERE content_id = ?", (content_id,)).fetchall()
        print(f"\n=== Claims（存進kol_claims，共{len(claims)}筆）===")
        for c in claims:
            print(dict(c))

        predictions = conn.execute(
            """SELECT kol_predictions.* FROM kol_predictions
               JOIN kol_claims ON kol_predictions.claim_id = kol_claims.id
               WHERE kol_claims.content_id = ?""",
            (content_id,),
        ).fetchall()
        print(f"\n=== Predictions（存進kol_predictions，共{len(predictions)}筆）===")
        for p in predictions:
            print(dict(p))


def main() -> None:
    parser = argparse.ArgumentParser(description="Crypto Intelligence Agent")
    parser.add_argument(
        "command",
        choices=[
            "collect-youtube", "collect-jin10", "market-check", "daily-report",
            "init-db", "test-claim",
        ],
    )
    parser.add_argument(
        "video", nargs="?", default=None,
        help="test-claim指令專用：YouTube影片網址或video_id",
    )
    args = parser.parse_args()

    settings = load_settings()
    setup_logging(settings.log_level, settings.log_path)
    db.init_db(settings.database_path)

    if args.command == "test-claim":
        if not args.video:
            logger.error("test-claim指令需要指定影片網址或video_id，例如：python -m app.main test-claim <網址>")
            sys.exit(1)
        cmd_test_claim(settings, args.video)
        return

    commands = {
        "collect-youtube": cmd_collect_youtube,
        "collect-jin10": cmd_collect_jin10,
        "market-check": cmd_market_check,
        "daily-report": cmd_daily_report,
        "init-db": lambda s: logger.info("資料庫已初始化：%s", s.database_path),
    }
    commands[args.command](settings)


if __name__ == "__main__":
    sys.exit(main())
