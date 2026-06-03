"""
jybot.log -- minimal logging shim.

Uses ``loguru`` when installed (nicer output + file rotation), otherwise falls
back to the standard library ``logging`` module so the bot never hard-depends
on loguru. The rest of the codebase imports ``from jybot.log import logger``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


def _build_logger():
    try:
        from loguru import logger as _logger  # type: ignore

        _logger.remove()
        _logger.add(
            sys.stderr,
            format=(
                "<green>{time:HH:mm:ss}</green> | "
                "<level>{level:<7}</level> | {message}"
            ),
            level="INFO",
            colorize=True,
        )
        return _logger, True
    except Exception:
        import logging

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)-7s | %(message)s",
            datefmt="%H:%M:%S",
        )
        return logging.getLogger("jybot"), False


logger, _IS_LOGURU = _build_logger()


def configure(level: str = "INFO", to_file: Optional[str] = None) -> None:
    """(Re)configure log level and optional file sink."""
    if _IS_LOGURU:
        logger.remove()
        logger.add(
            sys.stderr,
            format=(
                "<green>{time:HH:mm:ss}</green> | "
                "<level>{level:<7}</level> | {message}"
            ),
            level=level,
            colorize=True,
        )
        if to_file:
            _LOG_DIR.mkdir(exist_ok=True)
            logger.add(
                str(_LOG_DIR / to_file),
                format="{time:YYYY-MM-DD HH:mm:ss} | {level:<7} | {message}",
                level="DEBUG",
                rotation="50 MB",
                retention="14 days",
            )
    else:
        import logging

        logger.setLevel(getattr(logging, level.upper(), logging.INFO))
        if to_file:
            _LOG_DIR.mkdir(exist_ok=True)
            fh = logging.FileHandler(str(_LOG_DIR / to_file), encoding="utf-8")
            fh.setFormatter(
                logging.Formatter(
                    "%(asctime)s | %(levelname)-7s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            logger.addHandler(fh)
