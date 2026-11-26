"""Record unexpected failures locally without logging credentials or file payloads."""
from __future__ import annotations

import faulthandler
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import threading
import traceback
from typing import Callable

from .client_store import state_path

_fault_stream = None


def install_diagnostics(notify: Callable[[str], None]) -> None:
    global _fault_stream
    folder = state_path().parent / "logs"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger("r2-client-errors")
        logger.setLevel(logging.ERROR)
        if not logger.handlers:
            handler = RotatingFileHandler(folder / "errors.log", maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            logger.addHandler(handler)
        fault_path = folder / "native-fault.log"
        if fault_path.exists() and fault_path.stat().st_size > 2 * 1024 * 1024:
            fault_path.replace(folder / "native-fault.previous.log")
        _fault_stream = fault_path.open("a", encoding="utf-8")
        faulthandler.enable(_fault_stream, all_threads=True)
    except OSError:
        return

    def record(exception_type: type, exception: BaseException, trace: object) -> None:
        # Avoid exception text and source lines: either may contain a pasted key.
        frames = "\n".join(f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}" for frame in traceback.extract_tb(trace))
        logger.error("%s\n%s", exception_type.__name__, frames)
        notify("检测到异常，详情已记录在本机 logs 目录；请重试当前操作。")

    sys.excepthook = record
    threading.excepthook = lambda args: record(args.exc_type, args.exc_value, args.exc_traceback)
