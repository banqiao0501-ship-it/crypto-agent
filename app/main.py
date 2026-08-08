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
import sys
from datetime import date

from app.ai import synthesizer
from app.analysis import event_detector, market_regime, technical
from app.collectors import jin10, market, youtube
from app.config import Settings, load_settings
from app.database import db
from app.notify import line
from app.utils.logger import get_logger, setup_logging

logger = get_logger(__name__)


def _run_market_and_technical(conn, settings: Settings, status: dict[str, bool]) -> dict:
    try:
        price_series = market.collect(conn, settings.assets, settings.coingecko_api_key)
        status["market_api"] = True
    except Exception:
        logger.exception("市場數據collector發生錯誤")
        status["market_api"] = False
        price_series = {}

    technical_results = technical.run(conn, settings.assets, price_series)

    try:
        regime_result = market_regime.compute(conn, settings.assets, price_series, technical_results)
    except Exception:
        logger.exception("Market Regime計算發生錯誤")
        regime_result = None

    return {
        "technical_results": technical_results,
        "price_series": price_series,
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
                bullets = synthesizer.summarize_youtube_video(settings, video["title"], video["transcript"])
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
                conn, settings.assets, result["price_series"], technical_results
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


def cmd_daily_report(settings: Settings) -> None:
    with db.get_connection(settings.database_path) as conn:
        status: dict[str, bool] = {}
        result = _run_market_and_technical(conn, settings, status)
        technical_results = result["technical_results"]
        regime_result = result["regime_result"]

        try:
            report = synthesizer.generate_daily_report(conn, settings, technical_results, regime_result)
            status["ai"] = True
        except Exception:
            logger.exception("AI每日報告產生失敗")
            status["ai"] = False
            return  # 沒有報告內容就不用往下推播了

        report_date = date.today().isoformat()
        text = line.format_daily_report(report, settings.assets, report_date, status, regime_result)

        try:
            line.push_text(settings.line_channel_access_token, settings.line_user_id, text)
            sent_at = date.today().isoformat()
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Crypto Intelligence Agent")
    parser.add_argument(
        "command",
        choices=["collect-youtube", "collect-jin10", "market-check", "daily-report", "init-db"],
    )
    args = parser.parse_args()

    settings = load_settings()
    setup_logging(settings.log_level, settings.log_path)
    db.init_db(settings.database_path)

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
