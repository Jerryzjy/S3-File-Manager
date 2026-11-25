"""Record unexpected failures locally without logging credentials or file payloads."""
from __future__ import annotations

import faulthandler
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import platform
import re
import sys
import tempfile
import threading
import traceback
from typing import Callable
import zipfile

from .client_store import state_path

_fault_stream = None


def export_diagnostics(destination: Path, version: str) -> None:
    folder = state_path().parent / "logs"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".zip", delete=False) as stream:
            temporary = Path(stream.name)
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("version.txt", f"S3 File Manager {version}\n{platform.system()} {platform.release()} {platform.machine()}\n")
            for name in ("errors.log", "errors.log.1", "errors.log.2", "errors.log.3"):
                source = folder / name
                if source.is_symlink():
                    continue
                try:
                    with source.open(encoding="utf-8", errors="replace") as stream:
                        lines = stream.read(1024 * 1024).splitlines()
                except FileNotFoundError:
                    continue
                # Include generated frame identifiers and exception types only.
                safe = [line for line in lines if re.fullmatch(
                    r"(?:\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} [A-Za-z_]\w*|[\w.-]+:\d+ in [\w<>.]+)", line)]
                if safe:
                    archive.writestr(name, "\n".join(safe) + "\n")
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
