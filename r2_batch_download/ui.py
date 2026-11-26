"""Shared Qt visual components without fixtures, credentials or network state."""
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


ACCENT = "#4452CB"
MUTED = "#56657D"
SUCCESS = "#087450"
ERROR = "#9C4232"
SIDEBAR_ICON = "#BEC9E3"
DEFAULT_ICON = "#596B88"

STYLE = """
QWidget { color: #273550; font-family: 'Microsoft YaHei UI'; font-size: 13px; }
QMainWindow, QWidget#workspace { background: #F4F6FA; }
QScrollArea, QScrollArea>QWidget>QWidget { background: #F4F6FA; }
QFrame#sidebar { background: #202B42; border-right: 1px solid #162038; }
QFrame#topbar { border-bottom: 1px solid #E2E7F0; background: #FFFFFF; }
QLabel#brand { font-size: 16px; font-weight: 700; color: #F4F6FF; }
QLabel#section { color: #56657D; font-size: 11px; font-weight: 600; }
QLabel#muted { color: #56657D; font-size: 12px; }
QLabel#tiny { color: #56657D; font-size: 11px; }
QFrame#sidebar QLabel#section, QFrame#sidebar QLabel#tiny, QFrame#sidebar QLabel#muted { color: #BEC9E3; }
QLabel#title { color: #192640; font-size: 22px; font-weight: 700; }
QLabel#subheading { color: #263655; font-size: 15px; font-weight: 600; }
QPushButton, QToolButton { background: #FFFFFF; color: #445572; border: 1px solid #DDE4EF; border-radius: 7px; padding: 8px 12px; }
QPushButton:hover, QToolButton:hover { background: #EDF0FE; color: #303CAB; border-color: #C2CAF5; }
QPushButton:pressed, QToolButton:pressed { background: #E3E8FE; }
QPushButton:focus, QToolButton:focus, QLineEdit:focus, QComboBox:focus { border: 1px solid #4452CB; }
QPushButton:disabled, QToolButton:disabled { background: #F1F3F8; color: #828DA3; border-color: #E6EAF2; }
QPushButton#primary { color: #FFFFFF; background: #4452CB; border: 1px solid #4452CB; font-weight: 600; padding: 9px 16px; }
QPushButton#primary:hover { background: #3541AB; border-color: #3541AB; }
QPushButton#primary:pressed { background: #2B358E; border-color: #2B358E; }
QPushButton#primary:focus { border-color: #1A215F; }
QPushButton#primary:disabled { background: #DFE4F7; color: #7380A2; border-color: #DFE4F7; }
QPushButton#ghost, QFrame#sidebar QPushButton#sidebarGhost { border-color: transparent; background: transparent; padding: 6px 8px; }
QPushButton#ghost:hover { background: #EBEFFD; }
QPushButton#nav { border: 1px solid transparent; background: transparent; padding: 9px 25px; color: #56657D; }
QPushButton#nav:checked { background: #FFFFFF; color: #303CAB; font-weight: 600; border: 1px solid #DDE4F5; }
QFrame#navGroup { background: #EEF1F8; border: 1px solid #E5EAF3; border-radius: 10px; }
QPushButton#viewToggle { padding: 7px 8px; border: 1px solid transparent; background: transparent; }
QPushButton#viewToggle:checked { color: #303CAB; background: #E7EBFE; }
QFrame#bucket { border: 1px solid transparent; border-radius: 9px; background: #27334D; }
QFrame#bucket:hover { background: #303E5D; }
QFrame#bucket[selected="true"] { border: 1px solid #8290F9; background: #343F67; }
QLabel#bucketTitle { color: #F2F5FF; font-size: 13px; font-weight: 600; }
QLabel#bucketMeta { color: #BEC9E3; font-size: 11px; }
QFrame#sidebar QPushButton#sidebarAction { color: #EDF1FF; background: #2C3956; border: 1px solid #4A5978; }
QFrame#sidebar QPushButton#sidebarAction:hover, QFrame#sidebar QPushButton#sidebarGhost:hover { background: #3B4B71; }
QFrame#sidebar QPushButton#sidebarAction:pressed, QFrame#sidebar QPushButton#sidebarGhost:pressed { background: #4A5C91; }
QFrame#sidebar QPushButton:focus, QPushButton#ghost:focus, QPushButton#nav:focus, QPushButton#viewToggle:focus, QPushButton#filter:focus { border: 1px solid #8290F9; }
QFrame#fileCard { background: #FFFFFF; border: 1px solid #E2E7F0; border-radius: 9px; }
QFrame#fileCard:hover { border: 1px solid #AFB9EB; }
QFrame#fileCard[selected="true"] { border: 1px solid #4452CB; background: #F2F4FF; }
QFrame#thumbnail { background: #EDF0F7; border: none; border-top-left-radius: 8px; border-top-right-radius: 8px; }
QFrame#fileCard[selected="true"] QFrame#thumbnail { background: #E7EBFE; }
QCheckBox { spacing: 7px; color: #56657D; }
QCheckBox::indicator { width: 15px; height: 15px; border: 1px solid #A7B2CD; border-radius: 4px; background: white; }
QCheckBox::indicator:hover, QCheckBox::indicator:focus { border-color: #4452CB; }
QCheckBox::indicator:checked { background: #4452CB; border: 1px solid #4452CB; image: url(__CHECK_ICON__); }
QLineEdit { background: #FFFFFF; border: 1px solid #D8E0ED; border-radius: 7px; padding: 9px 11px; selection-background-color: #DCE2FF; }
QLineEdit#search { background: #FFFFFF; padding-left: 8px; }
QComboBox { border: 1px solid #D8E0ED; border-radius: 7px; background: #FFFFFF; padding: 8px 12px; min-width: 90px; }
QComboBox::drop-down { border: none; width: 24px; }
QComboBox QAbstractItemView { background: #FFFFFF; selection-background-color: #E4E9FC; selection-color: #303CAB; color: #273550; border: 1px solid #D8E0ED; }
QTableWidget { background: #FFFFFF; border: 1px solid #E2E7F0; gridline-color: #EDF0F6; outline: 0; selection-background-color: #E7EBFE; selection-color: #303CAB; }
QTableWidget::item { padding: 10px 8px; border-bottom: 1px solid #EDF0F6; }
QTableWidget::item:selected { background: #E7EBFE; color: #303CAB; }
QHeaderView::section { border: none; border-bottom: 1px solid #E2E7F0; background: #F4F6FA; padding: 11px 8px; color: #56657D; font-size: 12px; text-align: left; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: #BFCADC; border-radius: 3px; min-height: 36px; }
QFrame#sidebar QScrollBar::handle:vertical { background: #68799C; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QFrame#statusbar { border-top: 1px solid #E2E7F0; background: #FFFFFF; }
QDialog { background: #FFFFFF; }
QLabel#fieldLabel { font-size: 12px; color: #465875; font-weight: 500; }
QLabel#helper { color: #56657D; font-size: 11px; }
QLabel#success { color: #087450; background: #EDFAF3; padding: 9px 11px; border-radius: 6px; font-size: 12px; }
QLabel#failure { color: #9C4232; background: #FDF3EF; padding: 9px 11px; border-radius: 6px; font-size: 12px; }
QFrame#taskRow { background: #FFFFFF; border-bottom: 1px solid #E7EBF4; }
QPushButton#filter { border: 1px solid transparent; background: transparent; color: #56657D; padding: 8px 13px; }
QPushButton#filter:checked { background: #E7EBFE; color: #303CAB; font-weight: 600; }
QProgressBar { border: none; background: #E5EAF4; border-radius: 3px; min-height: 5px; max-height: 5px; }
QProgressBar::chunk { background: #4452CB; border-radius: 3px; }
QLabel#toast { color: #303CAB; background: #EDF0FE; border: 1px solid #C3CCF8; border-radius: 8px; padding: 13px 20px; }
"""


def app_icon() -> QIcon:
    return QIcon(str(Path(__file__).parent / "preview_assets" / "s3-file-manager.ico"))


def icon(name: str, color: str = DEFAULT_ICON) -> QIcon:
    return qta.icon("mdi6." + name, color=color, color_disabled="#A6B1C7")


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
        glyph_color = {
            "primary": "#FFFFFF",
            "sidebarAction": SIDEBAR_ICON,
            "sidebarGhost": SIDEBAR_ICON,
        }.get(kind, DEFAULT_ICON)
        result.setIcon(icon(glyph, glyph_color))
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
        return "folder", "#5D6ED8", "文件夹"
    suffix = Path(entry["key"]).suffix.lower()
    if suffix in (".png", ".jpg", ".jpeg", ".webp", ".svg"):
        return "file-image-outline", "#506DA2", "图片"
    if suffix in (".mp4", ".mov"):
        return "file-video-outline", "#6C62A6", "视频"
    if suffix == ".pdf":
        return "file-pdf-box", "#A56050", "PDF 文档"
    if suffix in (".zip", ".7z"):
        return "folder-zip-outline", "#8B7337", "压缩包"
    return "file-document-outline", "#5E7595", "文档"


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
        name.setMinimumWidth(0)
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
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
        glyph.setPixmap(icon("database-outline", "#A8B3FF" if selected else SIDEBAR_ICON).pixmap(19, 19))
        title_row.addWidget(glyph)
        title_row.addWidget(label(profile["name"], "bucketTitle"), 1)
        edit = button("", "cog-outline", kind="sidebarGhost", tooltip="编辑连接", callback=lambda: self.edited.emit(profile["id"]))
        edit.setFixedSize(25, 25)
        title_row.addWidget(edit)
        layout.addLayout(title_row)
        provider = {"r2": "R2", "b2": "B2", "s3": "S3"}.get(profile.get("provider", "r2"), "S3")
        bucket = label(f"{provider} · {profile['bucket']}", "bucketMeta")
        bucket.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(bucket)
        bottom = QHBoxLayout()
        bottom.addWidget(label(f"{profile['file_count']:,} 个文件" if profile.get("file_count") is not None else profile.get("status", "尚未连接"), "bucketMeta"))
        bottom.addStretch()
        bottom.addWidget(label(profile.get("size_label", ""), "bucketMeta"))
        layout.addLayout(bottom)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self.profile["id"])
        super().mousePressEvent(event)




def app_style() -> str:
    style = STYLE.replace("__CHECK_ICON__", (Path(__file__).parent / "preview_assets" / "check-white.png").as_posix())
    return style.replace("Microsoft YaHei UI", "PingFang SC") if sys.platform == "darwin" else style
