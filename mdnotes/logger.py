"""全局日志系统。

日志输出到运行目录 logs/ 下，按天轮转。
同时输出到 stderr 以便调试。
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

from . import config as cfg


def _get_log_dir() -> Path:
    log_dir = cfg.get_project_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("mdnotes")
    logger.setLevel(logging.DEBUG)

    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 文件输出（按天）。目录不可写（打包到受保护目录等）时不能阻断应用启动，
    # 退化为仅控制台输出。
    try:
        log_dir = _get_log_dir()
        log_file = log_dir / f"mdnotes_{datetime.now().strftime('%Y%m%d')}.log"
        fh = logging.FileHandler(str(log_file), encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        _cleanup_old_logs(log_dir, keep_days=30)
    except OSError:
        pass

    # 控制台输出
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.WARNING)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


def _cleanup_old_logs(log_dir: Path, keep_days: int) -> None:
    cutoff = datetime.now().timestamp() - keep_days * 86400
    for f in log_dir.glob("mdnotes_*.log"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


# 模块级 logger 实例
log = _setup_logger()