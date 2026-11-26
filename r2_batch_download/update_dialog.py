"""Software update page used by desktop settings."""

from collections.abc import Callable
from pathlib import Path
import platform
import queue
import sys
import threading

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPlainTextEdit, QProgressBar, QVBoxLayout, QWidget

from . import DEFAULT_UPDATE_FEED
from .ui import button, label, size_text
from .update_feed import MacUpdateRelease, UpdateError, UpdateRelease, download_update, fetch_mac_update, fetch_update


class UpdatePanel(QWidget):
    ready_to_install = Signal(object, object)

    def __init__(self, current: str, stage_dir: Path, can_install: Callable[[], bool]):
        super().__init__()
        self.current = current
        self.stage_dir = stage_dir
        self.can_install = can_install
        self.is_mac = sys.platform == "darwin"
        self.architecture = platform.machine()
        self.release: UpdateRelease | MacUpdateRelease | None = None
        self.cancel: threading.Event | None = None
        self.events: queue.Queue = queue.Queue()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        layout.addWidget(label("软件更新", "title"))
        layout.addWidget(label("检查 S3 File Manager 的官方新版本。", "muted", True))

        card = QFrame()
        card.setObjectName("settingsCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 16, 18, 16)
        card_layout.setSpacing(12)
        header = QHBoxLayout()
        version = QVBoxLayout()
        version.setSpacing(5)
        version.addWidget(label("当前版本", "muted"))
        version.addWidget(label(f"v{current}", "subheading"))
        header.addLayout(version, 1)
        self.check_button = button("检查更新", "update", kind="primary", callback=self.check)
        header.addWidget(self.check_button)
        card_layout.addLayout(header)
        self.status = label("", "muted", True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        card_layout.addWidget(self.status)
        self.notes = QPlainTextEdit()
        self.notes.setAccessibleName("更新说明")
        self.notes.setReadOnly(True)
        self.notes.setFixedHeight(112)
        self.notes.hide()
        card_layout.addWidget(self.notes)
        self.progress = QProgressBar()
        self.progress.setAccessibleName("更新下载进度")
        self.progress.setTextVisible(False)
        self.progress.hide()
        card_layout.addWidget(self.progress)

        actions = QHBoxLayout()
        actions.addStretch()
        self.cancel_button = button("取消下载", callback=self.cancel_download)
        self.cancel_button.hide()
        actions.addWidget(self.cancel_button)
        action_text = "下载 macOS 安装包" if self.is_mac else "下载并安装"
        self.install_button = button(action_text, "download-outline", callback=self.download)
        self.install_button.hide()
        actions.addWidget(self.install_button)
        card_layout.addLayout(actions)
        layout.addWidget(card)
        layout.addStretch()
        self.status.setText("发现新版后，可下载安装包并手动替换应用。" if self.is_mac else "发现新版后，可在这里下载安装。")

    def check(self) -> None:
        if self.timer.isActive():
            return
        self.release = None
        self.notes.hide()
        self.install_button.hide()
        self.status.setText("正在检查更新…")
        self.check_button.setEnabled(False)
        self.timer.start(70)

        def work() -> None:
            try:
                if self.is_mac:
                    result = fetch_mac_update(self.current, self.architecture)
                else:
                    result = fetch_update(DEFAULT_UPDATE_FEED, self.current)
                self.events.put(("checked", result))
            except (UpdateError, OSError) as exc:
                self.events.put(("error", str(exc)))

        threading.Thread(target=work, daemon=True, name="s3fm-update-check").start()

    def download(self) -> None:
        release = self.release
        if release is None or self.timer.isActive():
            return
        if isinstance(release, MacUpdateRelease):
            opened = QDesktopServices.openUrl(QUrl(release.url))
            if opened:
                self.status.setText("已在浏览器中打开下载。打开 DMG 后，将新版拖入「应用程序」。")
            else:
                self.status.setText("无法打开安装包下载。请检查默认浏览器后重试。")
            return
        if not getattr(sys, "frozen", False):
            self.status.setText("请使用打包的 Windows 客户端安装更新。")
            return
        if not self.can_install():
            self.status.setText("请先暂停或完成传输，再到这里安装更新。")
            return
        self.status.setText("正在下载新版…")
        self.progress.setValue(0)
        self.progress.show()
        self.install_button.hide()
        self.cancel_button.setEnabled(True)
        self.cancel_button.show()
        cancel = threading.Event()
        self.cancel = cancel
        self.check_button.setEnabled(False)
        self.timer.start(70)

        def work() -> None:
            percent = -1

            def progress(received: int, total: int) -> None:
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
                if self.cancel is not None and not self.cancel.is_set():
                    self.status.setText(f"正在下载新版… {value}%")
                continue
            cancelled = self.cancel is not None and self.cancel.is_set()
            self.cancel = None
            self.timer.stop()
            self.progress.hide()
            self.cancel_button.hide()
            self.check_button.setEnabled(True)
            if cancelled:
                if kind == "downloaded":
                    try:
                        value.unlink(missing_ok=True)
                    except OSError:
                        pass
                self.status.setText("已取消下载。")
                self.install_button.show()
            elif kind == "error":
                self.status.setText(value)
                self.install_button.setVisible(self.release is not None)
            elif kind == "checked":
                self.release = value
                if value is None:
                    self.status.setText("当前已是此平台的最新版本。")
                else:
                    installation = "下载后请手动安装" if self.is_mac else "安装后将自动重启"
                    self.status.setText(f"发现新版 v{value.version} · {size_text(value.size)}\n{installation}")
                    self.notes.setPlainText(value.notes)
                    self.notes.setVisible(bool(value.notes))
                    self.install_button.show()
            elif kind == "downloaded":
                self.ready_to_install.emit(value, self.release)
            return

    def cancel_download(self) -> bool:
        if self.cancel is None:
            return False
        self.cancel.set()
        self.cancel_button.setEnabled(False)
        self.status.setText("正在取消下载…")
        return True
