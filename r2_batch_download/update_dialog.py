"""Interactive update check and download for the Windows desktop client."""

from pathlib import Path
from collections.abc import Callable
import queue
import sys
import threading
from urllib.parse import urlsplit

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLineEdit, QPlainTextEdit, QProgressBar, QVBoxLayout

from .ui import button, label, size_text
from .update_feed import UpdateError, UpdateRelease, download_update, fetch_update, validate_feed_url


class UpdateDialog(QDialog):
    def __init__(self, parent, current: str, feed_url: str, stage_dir: Path, can_install: Callable[[], bool]):
        super().__init__(parent)
        self.setWindowTitle("检查更新")
        self.setMinimumWidth(510)
        self.current = current
        self.stage_dir = stage_dir
        self.can_install = can_install
        self.checked_url = ""
        self.release: UpdateRelease | None = None
        self.downloaded: Path | None = None
        self.cancel: threading.Event | None = None
        self.events: queue.Queue = queue.Queue()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 24, 26, 24)
        layout.setSpacing(12)
        layout.addWidget(label("软件更新", "title"))
        layout.addWidget(label(f"当前版本 {current}", "muted"))
        layout.addWidget(label("HTTPS 更新地址", "fieldLabel"))
        self.feed = QLineEdit(feed_url)
        self.feed.setAccessibleName("HTTPS 更新地址")
        self.feed.setPlaceholderText("输入发布方提供的更新清单地址")
        self.feed.textChanged.connect(self.reset_result)
        layout.addWidget(self.feed)
        layout.addWidget(label("请使用你信任的发布地址。", "helper"))
        self.status = label("", "helper", True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setMaximumHeight(130)
        self.notes.hide()
        layout.addWidget(self.notes)
        self.progress = QProgressBar()
        self.progress.hide()
        layout.addWidget(self.progress)

        actions = QHBoxLayout()
        actions.addStretch()
        self.close_button = button("关闭", callback=self.reject)
        actions.addWidget(self.close_button)
        self.check_button = button("检查更新", callback=self.check)
        actions.addWidget(self.check_button)
        self.install_button = button("下载并安装", kind="primary", callback=self.download)
        self.install_button.hide()
        actions.addWidget(self.install_button)
        layout.addLayout(actions)
        if feed_url:
            QTimer.singleShot(0, self.check)

    def reset_result(self) -> None:
        self.release = None
        self.notes.hide()
        self.install_button.hide()
        self.status.clear()

    def set_busy(self, busy: bool) -> None:
        self.feed.setEnabled(not busy)
        self.check_button.setEnabled(not busy)
        self.install_button.setEnabled(not busy)
        self.close_button.setText("取消下载" if self.cancel is not None else "关闭")
        if busy:
            self.timer.start(70)
        else:
            self.timer.stop()

    def check(self) -> None:
        if self.timer.isActive():
            return
        self.reset_result()
        try:
            address = validate_feed_url(self.feed.text())
        except UpdateError as exc:
            self.status.setText(str(exc))
            return
        self.status.setText("正在检查更新…")
        self.set_busy(True)

        def work():
            try:
                result = fetch_update(address, self.current)
                self.events.put(("checked", (address, result)))
            except (UpdateError, OSError) as exc:
                self.events.put(("error", str(exc)))

        threading.Thread(target=work, daemon=True, name="s3fm-update-check").start()

    def download(self) -> None:
        release = self.release
        if release is None or self.timer.isActive():
            return
        if not getattr(sys, "frozen", False):
            self.status.setText("请使用打包的 Windows 客户端安装更新。")
            return
        if not self.can_install():
            self.status.setText("请关闭此窗口，暂停或完成下载任务，再回来安装更新。")
            return
        self.status.setText("正在下载新版…")
        self.progress.setValue(0)
        self.progress.show()
        cancel = threading.Event()
        self.cancel = cancel
        self.set_busy(True)

        def work():
            percent = -1

            def progress(received, total):
                nonlocal percent
                value = received * 100 // total
                if value != percent:
                    percent = value
                    self.events.put(("progress", value))

            try:
                staged = download_update(release, self.stage_dir, on_progress=progress, cancel=cancel)
                self.events.put(("downloaded", staged))
            except (UpdateError, OSError) as exc:
                self.events.put(("error", str(exc)))

        threading.Thread(target=work, daemon=True, name="s3fm-update-download").start()

    def poll(self) -> None:
        while not self.events.empty():
            kind, value = self.events.get_nowait()
            if kind == "progress":
                self.progress.setValue(value)
                continue
            cancelled = self.cancel is not None and self.cancel.is_set()
            self.cancel = None
            self.set_busy(False)
            if cancelled:
                if kind == "downloaded":
                    try:
                        value.unlink(missing_ok=True)
                    except OSError:
                        pass
                super().reject()
            elif kind == "error":
                self.progress.hide()
                self.status.setText(value)
            elif kind == "checked":
                self.checked_url, self.release = value
                if self.release is None:
                    self.status.setText("已是当前更新源的最新版本。")
                else:
                    release = self.release
                    host = urlsplit(self.checked_url).hostname
                    self.status.setText(f"发现 {release.version} · {size_text(release.size)}\n发布源 {host} · 安装后将重启客户端")
                    self.notes.setPlainText(release.notes)
                    self.notes.setVisible(bool(release.notes))
                    self.install_button.show()
            elif kind == "downloaded":
                self.downloaded = value
                self.accept()
            return

    def reject(self) -> None:
        if self.cancel is not None:
            self.cancel.set()
            self.close_button.setEnabled(False)
            self.status.setText("正在取消下载…")
            return
        super().reject()
