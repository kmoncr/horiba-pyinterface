"""Loguru file-sink setup shared by the GUI entrypoints.

Adds a per-launch timestamped log file under ``logs/`` next to the
scripts. The default loguru stderr sink stays in place, so logs still
appear in the terminal too — the file is just an artifact you can
read after the fact.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from loguru import logger


def setup_file_logging(component: str = "app",
                       log_dir: Path | None = None) -> Path:
    """Add a loguru file sink for this run and return the file path.

    Args:
        component: Short tag included in the filename (e.g. "horibagui",
            "rtc", "image"). Lets you tell logs apart at a glance.
        log_dir: Override the directory. Defaults to ``./logs/`` next
            to this file.

    Returns:
        Path to the freshly created log file. Callers usually print it
        to the user so they know where to look.
    """
    if log_dir is None:
        log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"{timestamp}_{component}.log"

    logger.add(
        log_path,
        level="DEBUG",
        rotation="10 MB",
        retention="7 days",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
               "{name}:{function}:{line} - {message}",
        backtrace=True,   # capture full stack on exceptions
        diagnose=True,    # include locals in tracebacks
        enqueue=True,     # thread-safe handoff (GUI + asyncio thread + worker thread)
    )
    return log_path
