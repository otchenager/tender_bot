"""Общая настройка логирования для всех модулей проекта."""

import logging
import sys
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
    logger.addHandler(console_handler)

    file_handler = logging.FileHandler(LOG_DIR / "tender_bot.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
    logger.addHandler(file_handler)

    return logger
