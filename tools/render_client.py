"""Capture production empty/connection states without accessing user data."""
from __future__ import annotations

import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from r2_batch_download.client import ClientWindow, ConnectionDialog, VERSION
from r2_batch_download.ui import app_style


def main() -> None:
    output = Path(__file__).resolve().parents[1] / "design-reference" / ("release-" + VERSION)
    output.mkdir(parents=True, exist_ok=True)
    application = QApplication([])
    for filename in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf"):
        QFontDatabase.addApplicationFont(str(Path("C:/Windows/Fonts") / filename))
    application.setStyle("Fusion")
    application.setFont(QFont("Microsoft YaHei UI", 10))
    application.setStyleSheet(app_style())
    state = {"profiles": [], "tasks": [], "preferences": {"workers": 4, "skip_existing": True}}
    with patch("r2_batch_download.client.load_state", return_value=state), patch("r2_batch_download.client.save_state"):
        window = ClientWindow()
        window.show()
        window.timer.stop()
        QTest.qWait(60)
        window.grab().save(str(output / "01-clean-start.png"))
        dialog = ConnectionDialog(window)
        dialog.show()
        application.processEvents()
        dialog.grab().save(str(output / "02-add-connection.png"))
        dialog.close()
        window.switch_page(1)
        application.processEvents()
        window.grab().save(str(output / "03-clean-tasks.png"))
        window.resize(1060, 720)
        QTest.qWait(60)
        window.grab().save(str(output / "04-clean-small.png"))
        window.tasks = [{"id": "render-running", "key": "传输测试文件.bin", "name": "传输测试文件.bin", "bucket": "仅用于截图的测试状态", "status": "downloading", "size": 1048576, "transferred": 524288}, {"id": "render-paused", "key": "暂停测试文件.bin", "name": "暂停测试文件.bin", "bucket": "仅用于截图的测试状态", "status": "paused", "size": 1048576, "transferred": 262144}]
        window._task_structure_dirty = True
        window.render_tasks()
        application.processEvents()
        window.grab().save(str(output / "05-pause-controls.png"))
        window.tasks.clear()
        window.close()
        window.writer.flush(3)
        window.writer.close(3)
        window._closing_saved = True
        window.close()
    print(output)


if __name__ == "__main__":
    main()
