"""Interactive desktop design preview. All files, connections and tasks are samples."""
from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QSize, QTimer, Signal
from PySide6.QtGui import QFont, QIcon, QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QLabel, QPushButton, QToolButton,
    QVBoxLayout, QHBoxLayout, QGridLayout, QStackedWidget, QScrollArea,
    QLineEdit, QCheckBox, QDialog, QDialogButtonBox, QFileDialog,
    QComboBox, QProgressBar, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QMessageBox, QSizePolicy,
)
import qtawesome as qta

from .preview_data import BUCKETS, FILES, TASKS, immediate_children

GREEN = "#07845F"
INK = "#172B27"
MUTED = "#76847F"

STYLE = """
QWidget { color: #263B35; font-family: 'Microsoft YaHei UI'; font-size: 13px; }
QMainWindow, QWidget#workspace, QScrollArea, QScrollArea>QWidget>QWidget { background: #FFFFFF; }
QFrame#sidebar { background: #F8FAFA; border-right: 1px solid #E9EFEC; }
QFrame#topbar { border-bottom: 1px solid #ECF0EE; background: #FFFFFF; }
QLabel#brand { font-size: 17px; font-weight: 700; color: #172B27; }
QLabel#section { color: #5F7265; font-size: 11px; font-weight: 600; }
QLabel#muted { color: #5F7265; font-size: 12px; }
QLabel#tiny { color: #5F7265; font-size: 11px; }
QLabel#title { color: #1B3029; font-size: 22px; font-weight: 700; }
QLabel#subheading { color: #243A31; font-size: 15px; font-weight: 600; }
QLabel#previewBadge { color: #526F5F; background: #EEF5F1; border-radius: 5px; padding: 4px 9px; font-size: 11px; }
QPushButton, QToolButton { background: #FFFFFF; color: #5D7067; border: 1px solid #E5ECE8; border-radius: 7px; padding: 8px 12px; }
QPushButton:hover, QToolButton:hover { background: #F0F7F3; color: #1C6953; border-color: #CCDED4; }
QPushButton:pressed, QToolButton:pressed { background: #E7F4ED; }
QPushButton:focus, QToolButton:focus, QLineEdit:focus, QComboBox:focus { border: 1px solid #0AAA7B; }
QPushButton:disabled, QToolButton:disabled { background: #F6F8F7; color: #A7B3AC; border-color: #EDF0EE; }
QPushButton#primary { color: #FFFFFF; background: #07845F; border: 1px solid #07845F; font-weight: 600; padding: 9px 16px; }
QPushButton#primary:hover { background: #066E50; border-color: #066E50; }
QPushButton#primary:disabled { background: #D4EDE1; color: #8BB6A0; border-color: #D4EDE1; }
QPushButton#ghost { border-color: transparent; background: transparent; padding: 6px 8px; }
QPushButton#ghost:hover { background: #EDF5F0; }
QPushButton#nav { border: none; background: transparent; padding: 9px 25px; color: #5F7265; }
QPushButton#nav:checked { background: #FFFFFF; color: #153E2B; font-weight: 600; border: 1px solid #E5ECE7; }
QFrame#navGroup { background: #F4F6F5; border: 1px solid #EEF2EF; border-radius: 10px; }
QPushButton#viewToggle { padding: 7px 8px; border: none; background: transparent; }
QPushButton#viewToggle:checked { color: #159566; background: #EAF7F0; }
QFrame#bucket { border: 1px solid transparent; border-radius: 9px; background: transparent; }
QFrame#bucket[selected="true"] { border: 1px solid #A4D9C3; background: #EAF8F2; }
QFrame#bucket:hover { background: #EFF7F2; }
QLabel#bucketTitle { color: #284B3B; font-size: 13px; font-weight: 600; }
QLabel#bucketMeta { color: #56705F; font-size: 11px; }
QFrame#fileCard { background: #FFFFFF; border: 1px solid #E9EEEB; border-radius: 9px; }
QFrame#fileCard:hover { border: 1px solid #B2D8C3; }
QFrame#fileCard[selected="true"] { border: 1px solid #0AAA7B; background: #F2FBF6; }
QFrame#thumbnail { background: #F5F7F6; border: none; border-top-left-radius: 8px; border-top-right-radius: 8px; }
QFrame#fileCard[selected="true"] QFrame#thumbnail { background: #EBF8F0; }
QCheckBox { spacing: 7px; color: #677D70; }
QCheckBox::indicator { width: 15px; height: 15px; border: 1px solid #D4DFD8; border-radius: 4px; background: white; }
QCheckBox::indicator:hover { border-color: #0AAA7B; }
QCheckBox::indicator:checked { background: #07845F; border: 1px solid #07845F; image: url(__CHECK_ICON__); }
QLineEdit { background: #FFFFFF; border: 1px solid #E1E9E4; border-radius: 7px; padding: 9px 11px; selection-background-color: #C6EBDD; }
QLineEdit#search { background: #FAFCFA; padding-left: 8px; }
QComboBox { border: 1px solid #E1E9E4; border-radius: 7px; background: #FFFFFF; padding: 8px 12px; min-width: 90px; }
QComboBox::drop-down { border: none; width: 24px; }
QComboBox QAbstractItemView { background: #FFFFFF; selection-background-color: #E3F5EB; color: #294637; border: 1px solid #E1E9E4; }
QTableWidget { background: #FFFFFF; border: none; gridline-color: #EEF2EF; outline: 0; selection-background-color: #EAF8F0; selection-color: #1D6548; }
QTableWidget::item { padding: 10px 8px; border-bottom: 1px solid #F0F3F1; }
QTableWidget::item:selected { background: #EAF8F0; color: #195F43; }
QHeaderView::section { border: none; border-bottom: 1px solid #E9EEEB; background: #F9FBF9; padding: 11px 8px; color: #5F7265; font-size: 12px; text-align: left; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: #D9E3DC; border-radius: 3px; min-height: 36px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QFrame#statusbar { border-top: 1px solid #ECF1ED; background: #FFFFFF; }
QDialog { background: #FFFFFF; }
QLabel#fieldLabel { font-size: 12px; color: #526B5C; font-weight: 500; }
QLabel#helper { color: #5F7265; font-size: 11px; }
QLabel#success { color: #087450; background: #EDFAF3; padding: 9px 11px; border-radius: 6px; font-size: 12px; }
QLabel#failure { color: #9C4232; background: #FDF3EF; padding: 9px 11px; border-radius: 6px; font-size: 12px; }
QFrame#taskRow { background: #FFFFFF; border-bottom: 1px solid #EDF2EE; }
QPushButton#filter { border: none; background: transparent; color: #5F7265; padding: 8px 13px; }
QPushButton#filter:checked { background: #EAF7EF; color: #087450; font-weight: 600; }
QProgressBar { border: none; background: #EDF2EE; border-radius: 3px; min-height: 5px; max-height: 5px; }
QProgressBar::chunk { background: #17B082; border-radius: 3px; }
QLabel#toast { color: #225A40; background: #EAF9F0; border: 1px solid #BCE2CC; border-radius: 8px; padding: 13px 20px; }
"""


def icon(name: str, color: str = "#7A8E81") -> QIcon:
    return qta.icon("mdi6." + name, color=color, color_disabled="#B5C5BA")


def label(text: str, name: str = "", wrap: bool = False) -> QLabel:
    result = QLabel(text)
    if name:
        result.setObjectName(name)
    result.setWordWrap(wrap)
    return result


def button(text: str = "", glyph: str = "", *, kind: str = "", callback: Any = None, tooltip: str = "") -> QPushButton:
    result = QPushButton(text)
    if kind:
        result.setObjectName(kind)
    if glyph:
        result.setIcon(icon(glyph, "#FFFFFF" if kind == "primary" else "#6F8577"))
        result.setIconSize(QSize(17, 17))
    if callback:
        result.clicked.connect(callback)
    result.setCursor(Qt.CursorShape.PointingHandCursor)
    result.setToolTip(tooltip or text)
    result.setAccessibleName(tooltip or text)
    return result


def size_text(value: float | int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "0 B"


def file_style(entry: dict[str, Any]) -> tuple[str, str, str]:
    if entry.get("kind") == "folder":
        return "folder", "#2CB38B", "文件夹"
    suffix = Path(entry["key"]).suffix.lower()
    if suffix in (".png", ".jpg", ".jpeg", ".webp", ".svg"):
        return "file-image-outline", "#70A999", "图片"
    if suffix in (".mp4", ".mov"):
        return "file-video-outline", "#8C9AC6", "视频"
    if suffix == ".pdf":
        return "file-pdf-box", "#CF9182", "PDF 文档"
    if suffix in (".zip", ".7z"):
        return "folder-zip-outline", "#C5AC78", "压缩包"
    return "file-document-outline", "#89A8A4", "文档"


class FileCard(QFrame):
    selected_changed = Signal(str, bool)
    opened = Signal(str)

    def __init__(self, entry: dict[str, Any], selected: bool = False) -> None:
        super().__init__()
        self.entry = entry
        self.setObjectName("fileCard")
        self.setProperty("selected", selected)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(160)
        self.setFixedHeight(181)
        self.setToolTip(entry["key"])
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        thumb = QFrame()
        thumb.setObjectName("thumbnail")
        thumb.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        thumb_layout = QVBoxLayout(thumb)
        thumb_layout.setContentsMargins(12, 8, 12, 12)
        thumb_layout.addSpacing(8)
        glyph, color, kind = file_style(entry)
        artwork = QLabel()
        artwork.setPixmap(icon(glyph, color).pixmap(48, 48))
        artwork.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb_layout.addWidget(artwork, 1)
        kind_label = label(kind, "tiny")
        kind_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb_layout.addWidget(kind_label)
        outer.addWidget(thumb, 1)
        metadata = QWidget()
        metadata.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        meta = QVBoxLayout(metadata)
        meta.setContentsMargins(13, 10, 13, 11)
        meta.setSpacing(5)
        name = label(entry.get("name", entry["key"].rstrip("/").split("/")[-1]))
        font = name.font()
        font.setWeight(QFont.Weight.DemiBold)
        name.setFont(font)
        name.setToolTip(entry["key"])
        name.setFixedHeight(19)
        meta.addWidget(name)
        caption = entry.get("modified", "")[:10].replace("-", "/") if entry["kind"] == "folder" else size_text(entry.get("size", 0))
        meta.addWidget(label(caption, "tiny"))
        outer.addWidget(metadata)
        self.check = QCheckBox(self)
        self.check.setGeometry(10, 10, 20, 20)
        self.check.setChecked(selected)
        self.check.setToolTip("选择 " + entry.get("name", entry["key"]))
        self.check.toggled.connect(self._toggle)

    def _toggle(self, state: bool) -> None:
        self.setProperty("selected", state)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()
        self.selected_changed.emit(self.entry["key"], state)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.check.setChecked(not self.check.isChecked())
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: Any) -> None:
        self.opened.emit(self.entry["key"])
        event.accept()


class BucketCard(QFrame):
    activated = Signal(str)
    edited = Signal(str)

    def __init__(self, profile: dict[str, Any], selected: bool) -> None:
        super().__init__()
        self.profile = profile
        self.setObjectName("bucket")
        self.setProperty("selected", selected)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(13, 14, 12, 13)
        layout.setSpacing(9)
        title_row = QHBoxLayout()
        glyph = QLabel()
        glyph.setPixmap(icon("database-outline", GREEN if selected else "#91A499").pixmap(19, 19))
        title_row.addWidget(glyph)
        title_row.addWidget(label(profile["name"], "bucketTitle"), 1)
        edit = button("", "cog-outline", kind="ghost", tooltip="编辑连接", callback=lambda: self.edited.emit(profile["id"]))
        edit.setFixedSize(25, 25)
        title_row.addWidget(edit)
        layout.addLayout(title_row)
        layout.addWidget(label(profile["bucket"], "bucketMeta"))
        bottom = QHBoxLayout()
        bottom.addWidget(label(f"{profile.get('file_count', 0)} 个示例文件", "bucketMeta"))
        bottom.addStretch()
        bottom.addWidget(label(profile.get("size_label", ""), "bucketMeta"))
        layout.addLayout(bottom)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self.profile["id"])
        super().mousePressEvent(event)


class ConnectionDialog(QDialog):
    def __init__(self, parent: QWidget, profile: dict[str, Any] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑连接" if profile else "添加存储桶")
        self.setMinimumWidth(510)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.fields: dict[str, QLineEdit] = {}
        self.profile = profile
        outer = QVBoxLayout(self)
        outer.setContentsMargins(27, 22, 27, 23)
        outer.setSpacing(16)
        title = QHBoxLayout()
        title.addWidget(label("编辑连接" if profile else "添加存储桶", "title"))
        title.addStretch()
        title.addWidget(button("填入示例", kind="ghost", callback=self.fill_demo))
        outer.addLayout(title)
        outer.addWidget(label("把你的 R2 存储桶添加到侧栏，随时切换和浏览。", "muted"))
        preview = label("界面预览 · 请使用示例值，凭据不会连接或保存。", "previewBadge", True)
        outer.addWidget(preview)
        form = QGridLayout()
        form.setVerticalSpacing(6)
        form.setHorizontalSpacing(12)
        field_defs = [
            ("name", "连接名称", "例如：品牌素材库"),
            ("bucket", "Bucket 名称", "例如：brand-assets"),
            ("account_id", "Account ID", "Cloudflare 账户 ID"),
            ("access_key_id", "Access Key ID", "R2 访问密钥 ID"),
            ("secret_access_key", "Secret Access Key", "与访问密钥 ID 配套的私有密钥"),
        ]
        for index, (name, title_text, placeholder) in enumerate(field_defs):
            form.addWidget(label(title_text, "fieldLabel"), index * 3, 0)
            entry = QLineEdit()
            entry.setPlaceholderText(placeholder)
            entry.setAccessibleName(title_text)
            self.fields[name] = entry
            form.addWidget(entry, index * 3 + 1, 0)
            form.setRowMinimumHeight(index * 3 + 2, 5)
        secret = self.fields["secret_access_key"]
        secret.setEchoMode(QLineEdit.EchoMode.Password)
        reveal = secret.addAction(icon("eye-outline"), QLineEdit.ActionPosition.TrailingPosition)
        reveal.triggered.connect(lambda: secret.setEchoMode(QLineEdit.EchoMode.Normal if secret.echoMode() == QLineEdit.EchoMode.Password else QLineEdit.EchoMode.Password))
        outer.addLayout(form)
        self.remember = QCheckBox("记住密钥（正式版使用本机加密）")
        outer.addWidget(self.remember)
        self.advanced_toggle = QCheckBox("自定义 S3 Endpoint")
        outer.addWidget(self.advanced_toggle)
        self.endpoint = QLineEdit()
        self.endpoint.setPlaceholderText("https://<ACCOUNT_ID>.r2.cloudflarestorage.com")
        self.endpoint.setAccessibleName("自定义 S3 Endpoint")
        self.endpoint.hide()
        self.advanced_toggle.toggled.connect(self.endpoint.setVisible)
        outer.addWidget(self.endpoint)
        self.validation = label("只需对象读取权限，即可用于批量下载。", "helper", True)
        outer.addWidget(self.validation)
        actions = QHBoxLayout()
        actions.addWidget(button("测试连接", "connection", callback=self.test_connection))
        actions.addStretch()
        actions.addWidget(button("取消", callback=self.reject))
        actions.addWidget(button("保存示例连接", kind="primary", callback=self.save))
        outer.addLayout(actions)
        if profile:
            self.fields["name"].setText(profile["name"])
            self.fields["bucket"].setText(profile["bucket"])

    def fill_demo(self) -> None:
        values = {"name": "设计资源库", "bucket": "design-assets", "account_id": "demo-account-id", "access_key_id": "demo-access-key", "secret_access_key": "demo-secret-key"}
        for name, value in values.items():
            self.fields[name].setText(value)

    def test_connection(self) -> bool:
        missing = [entry for entry in self.fields.values() if not entry.text().strip()]
        if missing:
            self.validation.setText("请填完连接信息，或点击「填入示例」体验。")
            self.validation.setObjectName("failure")
            missing[0].setFocus()
            valid = False
        else:
            self.validation.setText("示例校验通过。预览模式不会连接真实 R2。")
            self.validation.setObjectName("success")
            valid = True
        self.validation.style().unpolish(self.validation)
        self.validation.style().polish(self.validation)
        return valid

    def save(self) -> None:
        if self.test_connection():
            self.accept()

    def profile_values(self) -> dict[str, Any]:
        # Only the harmless display fields leave the dialog. Credentials are discarded.
        return {"name": self.fields["name"].text().strip(), "bucket": self.fields["bucket"].text().strip()}


class DownloadDialog(QDialog):
    def __init__(self, parent: QWidget, count: int, total: int, destination: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("新建下载任务")
        self.setMinimumWidth(490)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 25, 28, 25)
        layout.setSpacing(18)
        layout.addWidget(label("下载到本地", "title"))
        layout.addWidget(label(f"已选择 {count} 个文件，共 {size_text(total)}", "muted"))
        layout.addWidget(label("保存位置", "fieldLabel"))
        path_row = QHBoxLayout()
        self.destination = QLineEdit(destination)
        self.destination.setAccessibleName("下载保存位置")
        path_row.addWidget(self.destination, 1)
        path_row.addWidget(button("选择…", "folder-open-outline", callback=self.browse))
        layout.addLayout(path_row)
        row = QHBoxLayout()
        row.addWidget(label("同时下载", "fieldLabel"))
        self.workers = QComboBox()
        self.workers.addItems(["2 个文件", "4 个文件", "6 个文件", "8 个文件"])
        self.workers.setCurrentIndex(1)
        row.addWidget(self.workers)
        row.addStretch()
        layout.addLayout(row)
        self.skip = QCheckBox("跳过本地同大小的已有文件")
        self.skip.setChecked(True)
        layout.addWidget(self.skip)
        layout.addWidget(label("界面预览 · 确认后只会创建示例任务，不写入文件。", "previewBadge", True))
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("取消", callback=self.reject))
        actions.addWidget(button("创建示例任务", "download", kind="primary", callback=self.accept))
        layout.addLayout(actions)

    def browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择下载文件夹", str(Path.home()))
        if directory:
            self.destination.setText(directory)


class PreviewWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("R2 下载器 · 新版界面预览")
        self.setWindowIcon(icon("cloud-download-outline", GREEN))
        self.resize(1280, 860)
        self.setMinimumSize(1020, 700)
        self.profiles = copy.deepcopy(BUCKETS)
        self.files = copy.deepcopy(FILES)
        self.tasks = copy.deepcopy(TASKS)
        self.bucket_id = self.profiles[0]["id"]
        self.prefix = ""
        self.selected: set[str] = set()
        self.view_mode = "grid"
        self.task_filter = "all"
        self.destination = str(Path.home() / "Downloads" / "R2")
        self.visible_entries: list[dict[str, Any]] = []
        self._rendering = False
        self._grid_columns = 0
        self.toast_label: QLabel | None = None
        self._build()
        self.render_buckets()
        self.render_files()
        self.render_tasks()
        # The first usable viewport width is known after the window's layout.
        QTimer.singleShot(0, self._render_grid)
        self.shortcut = QShortcut(QKeySequence.StandardKey.SelectAll, self)
        self.shortcut.activated.connect(self.toggle_select_all)

    def _build(self) -> None:
        center = QWidget()
        self.setCentralWidget(center)
        shell = QHBoxLayout(center)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(233)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(19, 23, 19, 18)
        side.setSpacing(17)
        brand = QHBoxLayout()
        glyph = QLabel()
        glyph.setPixmap(icon("cloud-download-outline", GREEN).pixmap(29, 29))
        brand.addWidget(glyph)
        brand.addWidget(label("R2 下载器", "brand"))
        brand.addStretch()
        side.addLayout(brand)
        side.addSpacing(17)
        bucket_header = QHBoxLayout()
        bucket_header.addWidget(label("存储桶", "section"))
        bucket_header.addStretch()
        add = button("", "plus", tooltip="添加存储桶", callback=self.add_connection)
        add.setFixedSize(27, 27)
        bucket_header.addWidget(add)
        side.addLayout(bucket_header)
        self.bucket_layout = QVBoxLayout()
        self.bucket_layout.setSpacing(9)
        side.addLayout(self.bucket_layout)
        side.addStretch()
        note = label("新版界面预览\n示例文件与任务，不连接真实 R2。", "muted", True)
        note.setStyleSheet("font-size:11px;line-height:1.5;color:#5F7265;")
        side.addWidget(note)
        side.addWidget(button("下载设置", "cog-outline", callback=self.show_settings))
        shell.addWidget(sidebar)
        right = QWidget()
        right.setObjectName("workspace")
        main = QVBoxLayout(right)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)
        topbar = QFrame()
        topbar.setObjectName("topbar")
        topbar.setFixedHeight(75)
        top = QHBoxLayout(topbar)
        top.setContentsMargins(26, 0, 26, 0)
        top.addStretch()
        segment = QFrame()
        segment.setObjectName("navGroup")
        segment.setFixedHeight(44)
        segments = QHBoxLayout(segment)
        segments.setContentsMargins(4, 4, 4, 4)
        segments.setSpacing(3)
        self.files_nav = button("文件", "folder-outline", kind="nav", callback=lambda: self.switch_page(0))
        self.tasks_nav = button("下载任务", "download-outline", kind="nav", callback=lambda: self.switch_page(1))
        for nav in (self.files_nav, self.tasks_nav):
            nav.setCheckable(True)
            segments.addWidget(nav)
        self.files_nav.setChecked(True)
        top.addWidget(segment)
        top.addStretch()
        preview_badge = label("交互预览", "previewBadge")
        preview_badge.setFixedHeight(27)
        top.addWidget(preview_badge)
        main.addWidget(topbar)
        self.pages = QStackedWidget()
        self.file_page = self._file_page()
        self.task_page = self._task_page()
        self.pages.addWidget(self.file_page)
        self.pages.addWidget(self.task_page)
        main.addWidget(self.pages, 1)
        bottom = QFrame()
        bottom.setObjectName("statusbar")
        bottom.setFixedHeight(47)
        footer = QHBoxLayout(bottom)
        footer.setContentsMargins(26, 0, 26, 0)
        self.status_label = label("", "muted")
        footer.addWidget(self.status_label)
        footer.addStretch()
        footer.addWidget(label("示例数据 · 2026/11/25", "tiny"))
        main.addWidget(bottom)
        shell.addWidget(right, 1)

    def _file_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(27, 24, 27, 13)
        outer.setSpacing(19)
        row = QHBoxLayout()
        self.page_title = label("品牌素材库", "title")
        row.addWidget(self.page_title)
        row.addStretch()
        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("搜索存储桶中的文件…")
        self.search.setAccessibleName("搜索文件")
        self.search.addAction(icon("magnify"), QLineEdit.ActionPosition.LeadingPosition)
        self.search.setFixedWidth(258)
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.search_changed)
        row.addWidget(self.search)
        outer.addLayout(row)
        tools = QHBoxLayout()
        tools.setSpacing(8)
        self.breadcrumbs = QHBoxLayout()
        self.breadcrumbs.setSpacing(0)
        tools.addLayout(self.breadcrumbs, 1)
        self.select_button = button("全选", callback=self.toggle_select_all)
        tools.addWidget(self.select_button)
        group = QFrame()
        group.setStyleSheet("QFrame{border:1px solid #E5ECE8;border-radius:7px;}")
        views = QHBoxLayout(group)
        views.setContentsMargins(3, 2, 3, 2)
        views.setSpacing(1)
        self.grid_button = button("", "view-grid-outline", kind="viewToggle", tooltip="网格视图", callback=lambda: self.set_view("grid"))
        self.list_button = button("", "format-list-bulleted", kind="viewToggle", tooltip="列表视图", callback=lambda: self.set_view("list"))
        for view in (self.grid_button, self.list_button):
            view.setCheckable(True)
            view.setFixedSize(31, 29)
            views.addWidget(view)
        self.grid_button.setChecked(True)
        tools.addWidget(group)
        tools.addWidget(button("", "refresh", tooltip="刷新文件列表", callback=lambda: self.toast("示例文件列表已刷新")))
        self.download_button = button("下载所选", "download-outline", kind="primary", callback=self.create_download)
        tools.addWidget(self.download_button)
        outer.addLayout(tools)
        self.files_stack = QStackedWidget()
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 8, 0)
        self.grid.setHorizontalSpacing(15)
        self.grid.setVerticalSpacing(15)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.grid_host)
        self.files_stack.addWidget(self.scroll)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["名称", "大小", "类型", "修改时间"])
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(1, 100)
        self.table.setColumnWidth(2, 90)
        self.table.setColumnWidth(3, 153)
        self.table.itemSelectionChanged.connect(self.table_selected)
        self.table.cellDoubleClicked.connect(lambda row, column: self.open_entry(self.visible_entries[row]["key"]))
        self.files_stack.addWidget(self.table)
        self.empty = QWidget()
        empty = QVBoxLayout(self.empty)
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        glyph = QLabel()
        glyph.setPixmap(icon("file-search-outline", "#AEC8B8").pixmap(58, 58))
        glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.addWidget(glyph)
        title = label("没有找到文件", "subheading")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.addWidget(title)
        empty.addWidget(label("换个文件名试试，或清空搜索查看全部文件。", "muted"))
        self.files_stack.addWidget(self.empty)
        outer.addWidget(self.files_stack, 1)
        self.selection_note = label("", "muted")
        outer.addWidget(self.selection_note)
        return page

    def _task_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 25, 28, 12)
        layout.setSpacing(20)
        header = QHBoxLayout()
        header.addWidget(label("下载任务", "title"))
        header.addStretch()
        header.addWidget(button("重试失败", "refresh", callback=self.retry_failed))
        header.addWidget(button("清除已完成", callback=self.clear_completed))
        layout.addLayout(header)
        layout.addWidget(label("统一查看文件进度，在这里处理等待中和失败的任务。", "muted"))
        self.task_filters = QHBoxLayout()
        self.task_filters.setSpacing(5)
        self.filter_buttons: dict[str, QPushButton] = {}
        for value, name in (("all", "全部"), ("downloading", "下载中"), ("queued", "等待中"), ("completed", "已完成"), ("failed", "失败")):
            control = button(name, kind="filter", callback=lambda checked=False, value=value: self.set_task_filter(value))
            control.setCheckable(True)
            self.filter_buttons[value] = control
            self.task_filters.addWidget(control)
        self.task_filters.addStretch()
        layout.addLayout(self.task_filters)
        task_scroll = QScrollArea()
        task_scroll.setWidgetResizable(True)
        task_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.task_host = QWidget()
        self.task_rows = QVBoxLayout(self.task_host)
        self.task_rows.setContentsMargins(0, 0, 8, 0)
        self.task_rows.setSpacing(0)
        self.task_rows.setAlignment(Qt.AlignmentFlag.AlignTop)
        task_scroll.setWidget(self.task_host)
        layout.addWidget(task_scroll, 1)
        layout.addWidget(label("此处为示例传输状态；暂停、重试等操作仅演示交互。", "tiny"))
        return page

    @staticmethod
    def clear_layout(layout: Any) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                PreviewWindow.clear_layout(item.layout())

    def render_buckets(self) -> None:
        self.clear_layout(self.bucket_layout)
        for profile in self.profiles:
            card = BucketCard(profile, profile["id"] == self.bucket_id)
            card.activated.connect(self.change_bucket)
            card.edited.connect(self.edit_connection)
            self.bucket_layout.addWidget(card)

    def profile(self) -> dict[str, Any]:
        return next(profile for profile in self.profiles if profile["id"] == self.bucket_id)

    def change_bucket(self, bucket_id: str) -> None:
        self.bucket_id = bucket_id
        self.prefix = ""
        self.selected.clear()
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.switch_page(0)
        self.render_buckets()
        self.render_files()

    def switch_page(self, page: int) -> None:
        self.pages.setCurrentIndex(page)
        self.files_nav.setChecked(page == 0)
        self.tasks_nav.setChecked(page == 1)
        if page == 1:
            self.render_tasks()
        else:
            self.update_selection_note()

    def search_changed(self, query: str) -> None:
        self.selected.clear()
        self.render_files()

    def navigate(self, prefix: str) -> None:
        self.prefix = prefix
        self.selected.clear()
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.render_files()

    def render_files(self) -> None:
        entries = self.files.get(self.bucket_id, [])
        query = self.search.text().strip().casefold()
        if query:
            self.visible_entries = [dict(entry, name=entry["key"].split("/")[-1]) for entry in entries if query in entry["key"].casefold()]
        else:
            self.visible_entries = immediate_children(entries, self.prefix)
        self.page_title.setText(self.profile()["name"])
        self.render_breadcrumbs(query)
        self._render_grid()
        self._render_table()
        if not self.visible_entries:
            self.files_stack.setCurrentIndex(2)
        else:
            self.files_stack.setCurrentIndex(0 if self.view_mode == "grid" else 1)
        self.update_selection_note()

    def render_breadcrumbs(self, query: str) -> None:
        self.clear_layout(self.breadcrumbs)
        root = button("全部文件", kind="ghost", callback=lambda: self.navigate(""))
        self.breadcrumbs.addWidget(root)
        if query:
            self.breadcrumbs.addWidget(label("/   搜索结果", "muted"))
        elif self.prefix:
            parts = self.prefix.strip("/").split("/")
            for index, part in enumerate(parts):
                self.breadcrumbs.addWidget(label("/", "tiny"))
                path = "/".join(parts[:index + 1]) + "/"
                crumb = button(part, kind="ghost", callback=lambda checked=False, path=path: self.navigate(path))
                self.breadcrumbs.addWidget(crumb)
        self.breadcrumbs.addStretch()

    def _render_grid(self) -> None:
        self.clear_layout(self.grid)
        columns = max(2, min(5, max(400, self.files_stack.width() - 28) // 197))
        self._grid_columns = columns
        for index, entry in enumerate(self.visible_entries):
            card = FileCard(entry, entry["key"] in self.selected)
            card.selected_changed.connect(self.card_selected)
            card.opened.connect(self.open_entry)
            self.grid.addWidget(card, index // columns, index % columns)
        for column in range(columns):
            self.grid.setColumnStretch(column, 1)
        for column in range(columns, 6):
            self.grid.setColumnStretch(column, 0)

    def _render_table(self) -> None:
        self._rendering = True
        self.table.setRowCount(len(self.visible_entries))
        self.table.clearSelection()
        for row, entry in enumerate(self.visible_entries):
            glyph, color, kind = file_style(entry)
            name = QTableWidgetItem(icon(glyph, color), entry["name"])
            name.setData(Qt.ItemDataRole.UserRole, entry["key"])
            name.setToolTip(entry["key"])
            self.table.setItem(row, 0, name)
            self.table.setItem(row, 1, QTableWidgetItem(size_text(entry.get("size", 0))))
            self.table.setItem(row, 2, QTableWidgetItem(kind))
            self.table.setItem(row, 3, QTableWidgetItem(entry.get("modified", "")[:16].replace("T", " ")))
            self.table.setRowHeight(row, 51)
            if entry["key"] in self.selected:
                for column in range(4):
                    self.table.item(row, column).setSelected(True)
        self._rendering = False

    def set_view(self, mode: str) -> None:
        self.view_mode = mode
        self.grid_button.setChecked(mode == "grid")
        self.list_button.setChecked(mode == "list")
        self.render_files()

    def card_selected(self, key: str, checked: bool) -> None:
        if checked:
            self.selected.add(key)
        else:
            self.selected.discard(key)
        self.update_selection_note()

    def table_selected(self) -> None:
        if not self._rendering:
            self.selected = {self.visible_entries[index.row()]["key"] for index in self.table.selectionModel().selectedRows()}
            self.update_selection_note()

    def toggle_select_all(self) -> None:
        if self.pages.currentIndex() != 0:
            return
        keys = {entry["key"] for entry in self.visible_entries}
        self.selected = set() if keys and keys.issubset(self.selected) else keys
        self.render_files()

    def selected_files(self) -> list[dict[str, Any]]:
        return [entry for entry in self.files.get(self.bucket_id, []) if entry["key"] in self.selected or any(key.endswith("/") and entry["key"].startswith(key) for key in self.selected)]

    def update_selection_note(self) -> None:
        count = len(self.selected)
        noun = "搜索结果" if self.search.text().strip() else "当前目录"
        self.selection_note.setText(f"{noun}共 {len(self.visible_entries)} 项" + (f"  ·  已选择 {count} 项" if count else "  ·  双击打开文件夹，单击选择文件"))
        self.download_button.setText(f"下载所选 ({count})" if count else "下载所选")
        self.download_button.setEnabled(bool(count))
        keys = {entry["key"] for entry in self.visible_entries}
        self.select_button.setText("取消全选" if keys and keys.issubset(self.selected) else "全选")
        self.status_label.setText(f"{self.profile()['bucket']}  ·  示例连接" + (f"  ·  已选择 {len(self.selected_files())} 个文件" if count else ""))

    def open_entry(self, key: str) -> None:
        if key.endswith("/"):
            self.navigate(key)
        else:
            self.selected = {key}
            self.render_files()
            self.toast("文件已选中，点击「下载所选」可查看下载设置。")

    def add_connection(self) -> None:
        dialog = ConnectionDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            values = dialog.profile_values()
            bucket_id = "preview-" + str(len(self.profiles))
            self.profiles.append({"id": bucket_id, **values, "file_count": 0, "size_label": "0 B", "status": "示例连接"})
            self.files[bucket_id] = []
            self.change_bucket(bucket_id)
            self.toast("示例连接已添加，配置仅在本次预览中保留。")

    def edit_connection(self, bucket_id: str) -> None:
        profile = next(item for item in self.profiles if item["id"] == bucket_id)
        dialog = ConnectionDialog(self, profile)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            profile.update(dialog.profile_values())
            self.render_buckets()
            self.render_files()
            self.toast("示例连接已更新。")

    def create_download(self) -> None:
        selected = self.selected_files()
        if not selected:
            self.toast("请先选择需要下载的文件或文件夹。")
            return
        dialog = DownloadDialog(self, len(selected), sum(entry["size"] for entry in selected), self.destination)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.destination = dialog.destination.text().strip() or self.destination
            for entry in selected:
                self.tasks.append({"id": f"preview-{len(self.tasks)}", "key": entry["key"], "name": entry["key"].split("/")[-1], "bucket": self.profile()["bucket"], "status": "queued", "size": entry["size"], "transferred": 0, "speed": "", "error": ""})
            self.task_filter = "all"
            self.switch_page(1)
            self.toast(f"已创建 {len(selected)} 个示例下载任务，不会实际传输文件。")

    def set_task_filter(self, status: str) -> None:
        self.task_filter = status
        self.render_tasks()

    def render_tasks(self) -> None:
        self.clear_layout(self.task_rows)
        counts = {state: sum(task["status"] == state for task in self.tasks) for state in self.filter_buttons}
        for state, control in self.filter_buttons.items():
            name = {"all": "全部", "downloading": "下载中", "queued": "等待中", "completed": "已完成", "failed": "失败"}[state]
            count = len(self.tasks) if state == "all" else counts[state]
            control.setText(f"{name}  {count}")
            control.setChecked(state == self.task_filter)
        tasks = [task for task in self.tasks if self.task_filter == "all" or task["status"] == self.task_filter]
        if not tasks:
            empty = label("这里暂无任务", "muted")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setMinimumHeight(180)
            self.task_rows.addWidget(empty)
        for task in tasks:
            self.task_rows.addWidget(self._task_row(task))
        self.tasks_nav.setText(f"下载任务  {len(self.tasks)}")
        if self.pages.currentIndex() == 1:
            self.status_label.setText(f"{len(self.tasks)} 个示例任务  ·  {counts.get('downloading', 0)} 个下载中  ·  {counts.get('failed', 0)} 个失败")

    def _task_row(self, task: dict[str, Any]) -> QFrame:
        row = QFrame()
        row.setObjectName("taskRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(10, 19, 10, 19)
        layout.setSpacing(14)
        glyph, color, _ = file_style({"key": task.get("key", task["name"]), "kind": "file"})
        picture = QLabel()
        picture.setPixmap(icon(glyph, color).pixmap(31, 31))
        layout.addWidget(picture)
        info = QVBoxLayout()
        info.setSpacing(7)
        name = label(task["name"])
        name.setMinimumWidth(180)
        info.addWidget(name)
        info.addWidget(label(task.get("bucket", "brand-assets"), "tiny"))
        layout.addLayout(info, 4)
        status = task["status"]
        detail = QVBoxLayout()
        detail.setSpacing(8)
        transferred = task.get("transferred", 0)
        total = task["size"]
        progress = QProgressBar()
        progress.setTextVisible(False)
        progress.setRange(0, 1000)
        progress.setValue(int(1000 * transferred / max(1, total)))
        detail.addWidget(progress)
        line = f"{size_text(transferred)} / {size_text(total)}"
        if status == "downloading":
            speed = task.get("speed", "")
            line += "  ·  " + (size_text(speed) + "/s" if isinstance(speed, (int, float)) else str(speed))
        elif status == "failed":
            line = task.get("error") or "连接超时，点击重试"
        detail.addWidget(label(line, "tiny", True))
        layout.addLayout(detail, 5)
        status_text = {"downloading": "下载中", "queued": "等待中", "completed": "已完成", "failed": "失败", "paused": "已暂停"}.get(status, status)
        status_label = label(status_text)
        status_label.setStyleSheet("font-size:12px;color:" + {"downloading": "#07845F", "failed": "#A34D3E", "completed": "#386E50"}.get(status, "#5F7265") + ";")
        status_label.setFixedWidth(53)
        layout.addWidget(status_label)
        if status in ("downloading", "paused", "failed"):
            action = "pause" if status == "downloading" else "play-outline" if status == "paused" else "refresh"
            hint = "暂停示例任务" if status == "downloading" else "恢复示例任务" if status == "paused" else "重试示例任务"
            control = button("", action, kind="ghost", tooltip=hint, callback=lambda checked=False, task=task: self.task_action(task))
            control.setFixedSize(31, 31)
            layout.addWidget(control)
        else:
            spacer = QWidget()
            spacer.setFixedWidth(31)
            layout.addWidget(spacer)
        return row

    def task_action(self, task: dict[str, Any]) -> None:
        task["status"] = "paused" if task["status"] == "downloading" else "downloading" if task["status"] == "paused" else "queued"
        if task["status"] == "queued":
            task["error"] = ""
        self.render_tasks()
        self.toast("示例任务状态已更新；没有真实文件传输。")

    def retry_failed(self) -> None:
        count = 0
        for task in self.tasks:
            if task["status"] == "failed":
                task.update(status="queued", error="")
                count += 1
        self.render_tasks()
        self.toast(f"已将 {count} 个失败示例任务重新加入队列。")

    def clear_completed(self) -> None:
        count = sum(task["status"] == "completed" for task in self.tasks)
        self.tasks = [task for task in self.tasks if task["status"] != "completed"]
        self.render_tasks()
        self.toast(f"已从预览列表移除 {count} 个已完成任务。")

    def show_settings(self) -> None:
        dialog = DownloadDialog(self, 0, 0, self.destination)
        dialog.setWindowTitle("下载设置 · 预览")
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.destination = dialog.destination.text().strip() or self.destination
            self.toast("保存位置已应用于本次预览。")

    def toast(self, text: str) -> None:
        if self.toast_label is not None:
            self.toast_label.deleteLater()
        self.toast_label = label(text, "toast")
        self.toast_label.setParent(self)
        self.toast_label.adjustSize()
        self.toast_label.move(max(245, self.width() - self.toast_label.width() - 24), self.height() - 82)
        self.toast_label.show()
        self.toast_label.raise_()
        current = self.toast_label
        QTimer.singleShot(4000, lambda: current.hide() if self.toast_label is current else None)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        if hasattr(self, "files_stack"):
            QTimer.singleShot(0, self._reflow_grid)

    def _reflow_grid(self) -> None:
        columns = max(2, min(5, max(400, self.files_stack.width() - 28) // 197))
        if columns != self._grid_columns:
            self._render_grid()


def main() -> None:
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_SynthesizeMouseForUnhandledTouchEvents, True)
    application = QApplication(sys.argv)
    application.setStyle("Fusion")
    application.setFont(QFont("Microsoft YaHei UI", 10))
    application.setStyleSheet(preview_style())
    window = PreviewWindow()
    window.show()
    sys.exit(application.exec())


def preview_style() -> str:
    return STYLE.replace("__CHECK_ICON__", (Path(__file__).parent / "preview_assets" / "check-white.png").as_posix())


if __name__ == "__main__":
    main()
