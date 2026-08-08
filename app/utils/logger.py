"""
統一的logger設定。

每個模組開頭寫：
    from app.utils.logger import get_logger
    logger = get_logger(__name__)

這樣所有log會一起寫進同一個log檔，方便之後排查「昨天半夜到底哪一步掛了」。
"""
from __future__ import annotations

import logging
from pathlib import Path


def setup_logging(log_level: str, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),  # 同時印到終端機，方便手動跑的時候直接看
        ],
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
