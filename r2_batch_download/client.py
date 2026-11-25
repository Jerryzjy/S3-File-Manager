"""Production R2 desktop client: real connections, files and download records."""
from __future__ import annotations

import copy
import hashlib
import json
import queue
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import Qt, QSize, QTimer
from PySide6.QtGui import QFont, QKeySequence, QShortcut, QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QGridLayout, QStackedWidget, QScrollArea, QLineEdit, QCheckBox,
    QDialog, QFileDialog, QComboBox, QProgressBar, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QMessageBox, QMenu,
)

from . import APP_NAME, DEFAULT_UPDATE_FEED, __version__
from .ui import ACCENT, ERROR, MUTED, SUCCESS, BucketCard, FileCard, app_icon, app_style, button, file_style, icon, label, size_text
from .catalog import immediate_children, normalize_objects
from .client_store import load_state, save_state, state_path
from .update_dialog import UpdateDialog
from .update_install import UpdateInstallError, launch_update, schedule_update_cleanup
from .resumable_transfer import transfer_tasks, discard_partial
from .download_preflight import DownloadPreflight, inspect_download
from .state_writer import StateWriter
from .catalog_index import CatalogIndex
from .desktop_engine import Cancelled, Connection, create_client, list_directory_page, list_objects, validate_connection

VERSION = __version__
ACTIVE = {"queued", "downloading", "cancelling", "pausing"}
FINISHED = {"completed", "skipped", "failed", "cancelled", "interrupted"}


def connection_of(profile: dict[str, Any]) -> Connection:
    return Connection(**{key: str(profile.get(key, "")).strip() for key in (
        "account_id", "access_key_id", "secret_access_key", "bucket", "endpoint_url",
    )})


def connection_fingerprint(connection: Connection) -> str:
    endpoint = connection.endpoint_url.rstrip("/") or f"https://{connection.account_id}.r2.cloudflarestorage.com"
    return hashlib.sha256(json.dumps([endpoint.lower(), connection.bucket], ensure_ascii=False).encode("utf-8")).hexdigest()


def friendly_error(error: Any, connection: Connection | None = None) -> str:
    text = str(error)
    if connection:
        for secret in (connection.secret_access_key, connection.access_key_id):
            if secret:
                text = text.replace(secret, "[已隐藏]")
    if "SignatureDoesNotMatch" in text or "InvalidAccessKeyId" in text:
        return "密钥校验失败，请检查 Access Key ID 与 Secret Access Key 是否配套。\n" + text
    if "AccessDenied" in text or "Forbidden" in text:
        return "访问被拒绝，请检查此凭据对目标存储桶是否有对象读取权限。\n" + text
    if "NoSuchBucket" in text:
        return "没有找到存储桶，请检查 Bucket 名称、账户 ID 和 Endpoint。\n" + text
    if "Could not connect" in text or "timeout" in text.lower():
        return "连接失败或超时，请检查网络和 Endpoint 后重试。\n" + text
    return text


class ConnectionDialog(QDialog):
    """Validate locally and probe the actual target bucket in a background thread."""

    def __init__(self, parent: QWidget, profile: dict[str, Any] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑连接" if profile else "添加存储桶")
        self.setMinimumWidth(510)
        self.profile = copy.deepcopy(profile or {})
        self.fields: dict[str, QLineEdit] = {}
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.testing = False
        self.save_after_test = False
        self.verified: Connection | None = None
        self.closed = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(27, 22, 27, 23)
        outer.setSpacing(14)
        outer.addWidget(label(self.windowTitle(), "title"))
        outer.addWidget(label("连接信息保存在本机，读取和下载不需要公开存储桶。", "muted", True))
        for key, title, placeholder in (
            ("name", "连接名称", "为这个连接取一个名称"),
            ("bucket", "Bucket 名称", "仅填写存储桶名称"),
            ("account_id", "Account ID", "Cloudflare 账户 ID"),
            ("access_key_id", "Access Key ID", "R2 访问密钥 ID"),
            ("secret_access_key", "Secret Access Key", "与访问密钥 ID 配套的私有密钥"),
        ):
            field = QVBoxLayout()
            field.setSpacing(5)
            field.addWidget(label(title, "fieldLabel"))
            entry = QLineEdit(str(self.profile.get(key, "")))
            entry.setPlaceholderText(placeholder)
            entry.setAccessibleName(title)
            self.fields[key] = entry
            field.addWidget(entry)
            outer.addLayout(field)
        secret = self.fields["secret_access_key"]
        secret.setEchoMode(QLineEdit.EchoMode.Password)
        reveal = secret.addAction(icon("eye-outline"), QLineEdit.ActionPosition.TrailingPosition)
        reveal.triggered.connect(lambda: secret.setEchoMode(QLineEdit.EchoMode.Normal if secret.echoMode() == QLineEdit.EchoMode.Password else QLineEdit.EchoMode.Password))
        self.remember = QCheckBox("记住密钥（使用当前 Windows 用户加密保存）")
        self.remember.setChecked(bool(self.profile.get("remember_secret", False)))
        outer.addWidget(self.remember)
        self.advanced = QCheckBox("自定义 S3 Endpoint")
        outer.addWidget(self.advanced)
        self.endpoint = QLineEdit(str(self.profile.get("endpoint_url", "")))
        self.endpoint.setPlaceholderText("https://<ACCOUNT_ID>.r2.cloudflarestorage.com")
        self.endpoint.setAccessibleName("S3 Endpoint")
        self.endpoint.setVisible(bool(self.endpoint.text()))
        self.advanced.setChecked(bool(self.endpoint.text()))
        self.advanced.toggled.connect(self.endpoint.setVisible)
        outer.addWidget(self.endpoint)
        self.validation = label("需要目标存储桶的对象读取权限。", "helper", True)
        outer.addWidget(self.validation)
        actions = QHBoxLayout()
        self.test_button = button("测试连接", "connection", callback=self.test_connection)
        self.save_button = button("连接并保存", kind="primary", callback=self.save)
        actions.addWidget(self.test_button)
        actions.addStretch()
        actions.addWidget(button("取消", callback=self.reject))
        actions.addWidget(self.save_button)
        outer.addLayout(actions)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(90)

    def values(self) -> dict[str, Any]:
        result = {key: field.text().strip() for key, field in self.fields.items()}
        result.update(id=self.profile.get("id") or uuid.uuid4().hex,
                      endpoint_url=self.endpoint.text().strip() if self.advanced.isChecked() else "",
                      remember_secret=self.remember.isChecked())
        return result

    def validate(self) -> Connection:
        if not self.fields["name"].text().strip():
            raise ValueError("请填写连接名称。")
        connection = connection_of(self.values())
        validate_connection(connection)
        return connection

    def set_message(self, text: str, style: str) -> None:
        self.validation.setText(text)
        self.validation.setObjectName(style)
        self.validation.style().unpolish(self.validation)
        self.validation.style().polish(self.validation)

    def test_connection(self) -> None:
        if self.testing:
            return
        try:
            connection = self.validate()
        except ValueError as exc:
            self.set_message(str(exc), "failure")
            self.save_after_test = False
            return
        self.testing = True
        self.test_button.setEnabled(False)
        self.save_button.setEnabled(False)
        for entry in self.fields.values():
            entry.setEnabled(False)
        self.endpoint.setEnabled(False)
        self.advanced.setEnabled(False)
        self.set_message("正在连接存储桶…", "helper")

        def probe() -> None:
            client = None
            try:
                client = create_client(connection)
                client.list_objects_v2(Bucket=connection.bucket, MaxKeys=1)
                self.events.put({"ok": True, "connection": connection})
            except Exception as exc:
                self.events.put({"ok": False, "error": friendly_error(exc, connection)})
            finally:
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass

        threading.Thread(target=probe, daemon=True, name="r2-connection-check").start()

    def save(self) -> None:
        try:
            connection = self.validate()
        except ValueError as exc:
            self.set_message(str(exc), "failure")
            return
        if connection == self.verified:
            self.accept()
        else:
            self.save_after_test = True
            self.test_connection()

    def poll(self) -> None:
        if self.closed:
            return
        try:
            event = self.events.get_nowait()
        except queue.Empty:
            return
        self.testing = False
        self.test_button.setEnabled(True)
        self.save_button.setEnabled(True)
        for entry in self.fields.values():
            entry.setEnabled(True)
        self.endpoint.setEnabled(True)
        self.advanced.setEnabled(True)
        if event["ok"]:
            self.verified = event["connection"]
            self.set_message("连接成功，已验证目标存储桶的读取权限。", "success")
            if self.save_after_test:
                self.accept()
        else:
            self.verified = None
            self.set_message(event["error"], "failure")
        self.save_after_test = False

    def done(self, result: int) -> None:
        self.closed = True
        self.timer.stop()
        super().done(result)


class DownloadDialog(QDialog):
    def __init__(
        self, parent: QWidget, preferences: dict[str, Any], prefix: str = "", *,
        select_entries: Callable[[threading.Event], list[dict[str, Any]]] | None = None,
        scope: str = "",
    ) -> None:
        super().__init__(parent)
        self.select_entries = select_entries
        self.prefix = prefix
        self.entries: list[dict[str, Any]] | None = None
        self.preflight: DownloadPreflight | None = None
        self._check_cancel: threading.Event | None = None
        self._check_events: queue.Queue[tuple[list[dict[str, Any]] | None, DownloadPreflight | None, str]] = queue.Queue()
        settings = select_entries is None
        self.setWindowTitle("下载设置" if settings else "新建下载任务")
        self.setMinimumWidth(490)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 25, 28, 25)
        layout.setSpacing(17)
        layout.addWidget(label(self.windowTitle(), "title"))
        if not settings:
            self.scope = label(scope, "muted", True)
            self.scope.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(self.scope)
        layout.addWidget(label("保存位置", "fieldLabel"))
        path = QHBoxLayout()
        self.destination = QLineEdit(str(preferences.get("output_dir") or Path.home() / "Downloads" / "R2"))
        self.destination.setAccessibleName("下载保存位置")
        path.addWidget(self.destination, 1)
        path.addWidget(button("选择…", "folder-open-outline", callback=self.browse))
        layout.addLayout(path)
        row = QHBoxLayout()
        row.addWidget(label("同时下载", "fieldLabel"))
        self.workers = QComboBox()
        for count_workers in (1, 2, 4, 6, 8, 12, 16):
            self.workers.addItem(f"{count_workers} 个文件", count_workers)
        index = self.workers.findData(preferences.get("workers", 4))
        self.workers.setCurrentIndex(max(0, index))
        row.addWidget(self.workers)
        row.addStretch()
        layout.addLayout(row)
        self.skip = QCheckBox("跳过本地同大小的已有文件")
        self.skip.setChecked(bool(preferences.get("skip_existing", True)))
        layout.addWidget(self.skip)
        layout.addWidget(label("勾选后保留已有文件。大小不同的同名文件需换目录或允许覆盖。大小比较不等于内容校验。", "helper", True))
        self.strip = QCheckBox("从本地路径中移除当前目录前缀")
        self.strip.setChecked(bool(prefix))
        self.strip.setVisible(bool(prefix) and not settings)
        layout.addWidget(self.strip)
        self.check_status = label("", "helper", True)
        self.check_status.setTextFormat(Qt.TextFormat.PlainText)
        self.check_status.setVisible(not settings)
        layout.addWidget(self.check_status)
        self.validation = label("", "failure", True)
        self.validation.setTextFormat(Qt.TextFormat.PlainText)
        self.validation.hide()
        layout.addWidget(self.validation)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("取消", callback=self.reject))
        self.confirm_button = button("保存设置" if settings else "开始下载", "" if settings else "download", kind="primary", callback=self.confirm)
        actions.addWidget(self.confirm_button)
        layout.addLayout(actions)
        self.check_timer = QTimer(self)
        self.check_timer.setSingleShot(True)
        self.check_timer.timeout.connect(self.start_check)
        self.check_poll = QTimer(self)
        self.check_poll.timeout.connect(self.poll_check)
        if not settings:
            self.destination.textChanged.connect(self.schedule_check)
            self.skip.toggled.connect(self.schedule_check)
            self.strip.toggled.connect(self.schedule_check)
            self.schedule_check()

    def schedule_check(self) -> None:
        self.preflight = None
        if self._check_cancel is not None:
            self._check_cancel.set()
        self.confirm_button.setEnabled(False)
        self.validation.hide()
        self.check_status.setText("正在检查下载范围和保存位置…")
        self.check_timer.start(160)

    def start_check(self) -> None:
        select_entries = self.select_entries
        if select_entries is None or self._check_cancel is not None:
            return
        output = self.destination.text().strip()
        if not output:
            self.check_status.setText("请选择下载保存位置。")
            return
        entries = self.entries
        prefix = self.prefix
        strip_prefix = self.strip.isChecked()
        skip_existing = self.skip.isChecked()
        cancel = threading.Event()
        self._check_cancel = cancel
        events = self._check_events

        def check() -> None:
            files = entries
            try:
                if files is None:
                    files = select_entries(cancel)
                result = inspect_download(files, output, prefix, strip_prefix, skip_existing, cancel)
                events.put((files, result, ""))
            except Cancelled:
                events.put((files, None, "检查已取消。"))
            except Exception as exc:
                events.put((files, None, str(exc)))

        threading.Thread(target=check, daemon=True, name="r2-download-preflight").start()
        self.check_poll.start(35)

    def poll_check(self) -> None:
        try:
            entries, result, error = self._check_events.get_nowait()
        except queue.Empty:
            return
        self.check_poll.stop()
        cancel = self._check_cancel
        self._check_cancel = None
        self.entries = entries
        if cancel is not None and cancel.is_set():
            if not self.check_timer.isActive():
                self.start_check()
            return
        if result is None:
            self.check_status.clear()
            self.validation.setText(error)
            self.validation.show()
            return
        details = [f"{result.count:,} 个文件 · 总大小 {size_text(result.total)}",
                   f"可用空间 {size_text(result.free)} · 预计需要 {size_text(result.required)}"]
        if result.existing:
            details.append(f"已有同名文件 {result.existing:,} 个" + (f" · 将跳过 {result.skipped:,} 个" if result.skipped else ""))
        self.check_status.setText("\n".join(details))
        problems = []
        if result.required > result.free:
            problems.append("可用空间不足。请释放空间或选择其他保存位置。")
        if result.issues:
            problems.append(f"{result.issues:,} 个对象无法保存。请检查保存位置或调整所选文件。")
        if result.different_size:
            problems.append(f"{result.different_size:,} 个同名文件大小不同。请选择其他保存位置或允许覆盖。")
        if problems:
            self.validation.setText("\n".join([*problems, *result.examples]))
            self.validation.show()
            return
        self.preflight = result
        self.confirm_button.setEnabled(True)

    def browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择下载文件夹", str(Path.home()))
        if directory:
            self.destination.setText(directory)

    def confirm(self) -> None:
        if not self.destination.text().strip():
            self.validation.setText("请选择下载保存位置。")
            self.validation.show()
            return
        if self.select_entries is not None and self.preflight is None:
            return
        if not self.skip.isChecked() and QMessageBox.question(self, "确认覆盖同名文件", "本次操作允许替换保存位置中的同名文件。是否继续？") != QMessageBox.StandardButton.Yes:
            return
        self.accept()

    def done(self, result: int) -> None:
        if self._check_cancel is not None:
            self._check_cancel.set()
        self.check_timer.stop()
        self.check_poll.stop()
        super().done(result)


class ClientWindow(QMainWindow):
    PAGE_SIZE = 120
    TASK_PAGE_SIZE = 60

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {VERSION}")
        self.setWindowIcon(app_icon())
        self.resize(1280, 860)
        self.setMinimumSize(1060, 720)
        state = load_state()
        self.profiles: list[dict[str, Any]] = state.get("profiles", [])
        self.tasks: list[dict[str, Any]] = state.get("tasks", [])
        self.preferences = state.get("preferences", {})
        self.files: dict[str, list[dict[str, Any]]] = {}
        self.catalogs: dict[str, CatalogIndex] = {}
        self.catalog_sources: dict[str, object] = {}
        self._file_cache_key: tuple[Any, ...] | None = None
        self._query_token = ""
        self._search_results: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
        self.loaded: set[str] = set()
        self.loading: dict[str, threading.Event] = {}
        self.load_errors: dict[str, str] = {}
        self.scan_tokens: dict[str, str] = {}
        self._directory_pages: list[tuple[list[dict[str, Any]], str]] = []
        self._directory_request: threading.Event | None = None
        self._directory_error = ""
        self.bucket_id = self.profiles[0]["id"] if self.profiles else ""
        self.prefix = ""
        self.selected: set[str] = set()
        self.view_mode = "grid"
        self.task_filter = "all"
        self.page_index = self.task_page_index = 0
        self.visible_entries: list[dict[str, Any]] = []
        self.all_entries: list[dict[str, Any]] = []
        self._rendering = False
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.cancel_events: dict[str, threading.Event] = {}
        self.pause_events: dict[str, threading.Event] = {}
        self.task_by_id: dict[str, dict[str, Any]] = {}
        self._task_source: tuple[int, int] = (0, -1)
        self.task_widgets: dict[str, dict[str, Any]] = {}
        self._task_signature: tuple[Any, ...] | None = None
        self._task_structure_dirty = True
        self.dispatch_needed = False
        self.preparing = False
        self._prepare_token = ""
        self._closing_saved = False
        self._state_revision = 0
        self._update_pending: tuple[Path, str, int] | None = None
        self._update_installing = False
        self.task_connections: dict[str, Connection] = {}
        self.in_flight: set[str] = set()
        self.cleaning: set[str] = set()
        self.transfer_running = False
        self.worker: threading.Thread | None = None
        self.closing = False
        self.clear_when_idle = False
        self.task_dirty = False
        self.persist_dirty = False
        self.last_render = self.last_persist = time.monotonic()
        self.restore_warning = state.get("_warning", "")
        self.writer = StateWriter(on_error=lambda error: self.events.put({"kind": "save_error", "error": error}), save_fn=save_state)
        self.toast_label: QLabel | None = None
        self._build()
        self.resize_timer = QTimer(self)
        self.resize_timer.setSingleShot(True)
        self.resize_timer.timeout.connect(self.render_files)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.timeout.connect(self.render_files)
        self.render_buckets()
        self.render_files()
        self.render_tasks()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(90)
        self.shortcut = QShortcut(QKeySequence.StandardKey.SelectAll, self)
        self.shortcut.activated.connect(self.toggle_select_all)
        if state.get("_warning"):
            QTimer.singleShot(0, lambda: QMessageBox.warning(self, "配置读取提示", state["_warning"]))
        if self.bucket_id and self.current_profile().get("secret_access_key"):
            QTimer.singleShot(0, self.refresh_files)

    @staticmethod
    def clear_layout(layout: Any) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                ClientWindow.clear_layout(item.layout())

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
        glyph.setPixmap(app_icon().pixmap(30, 30))
        brand.addWidget(glyph)
        brand.addWidget(label(APP_NAME, "brand"))
        brand.addStretch()
        side.addLayout(brand)
        side.addSpacing(17)
        header = QHBoxLayout()
        header.addWidget(label("存储桶", "section"))
        header.addStretch()
        add = button("", "plus", kind="sidebarAction", tooltip="添加存储桶", callback=self.add_connection)
        add.setFixedSize(27, 27)
        header.addWidget(add)
        side.addLayout(header)
        bucket_scroll = QScrollArea()
        bucket_scroll.setWidgetResizable(True)
        bucket_scroll.setFrameShape(QFrame.Shape.NoFrame)
        bucket_scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget, QScrollArea > QWidget > QWidget { background: transparent; }")
        bucket_host = QWidget()
        bucket_host.setStyleSheet("background:transparent;")
        self.bucket_layout = QVBoxLayout(bucket_host)
        self.bucket_layout.setContentsMargins(0, 0, 2, 0)
        self.bucket_layout.setSpacing(9)
        self.bucket_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        bucket_scroll.setWidget(bucket_host)
        side.addWidget(bucket_scroll, 1)
        side.addWidget(label("连接与任务记录保存在本机。", "tiny", True))
        side.addWidget(button("检查更新", "update", kind="sidebarAction", callback=self.show_update))
        side.addWidget(button("下载设置", "cog-outline", kind="sidebarAction", callback=self.show_settings))
        shell.addWidget(sidebar)
        workspace = QWidget()
        workspace.setObjectName("workspace")
        layout = QVBoxLayout(workspace)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        topbar = QFrame()
        topbar.setObjectName("topbar")
        topbar.setFixedHeight(75)
        top = QHBoxLayout(topbar)
        top.setContentsMargins(26, 0, 26, 0)
        top.addStretch()
        nav = QFrame()
        nav.setObjectName("navGroup")
        nav.setFixedHeight(44)
        controls = QHBoxLayout(nav)
        controls.setContentsMargins(4, 4, 4, 4)
        self.files_nav = button("文件", "folder-outline", kind="nav", callback=lambda: self.switch_page(0))
        self.tasks_nav = button("下载任务", "download-outline", kind="nav", callback=lambda: self.switch_page(1))
        for control in (self.files_nav, self.tasks_nav):
            control.setCheckable(True)
            controls.addWidget(control)
        self.files_nav.setChecked(True)
        top.addWidget(nav)
        top.addStretch()
        top.addWidget(label(f"v{VERSION}", "tiny"))
        layout.addWidget(topbar)
        self.pages = QStackedWidget()
        self.pages.addWidget(self.build_files())
        self.pages.addWidget(self.build_tasks())
        layout.addWidget(self.pages, 1)
        footer = QFrame()
        footer.setObjectName("statusbar")
        footer.setFixedHeight(47)
        status = QHBoxLayout(footer)
        status.setContentsMargins(26, 0, 26, 0)
        self.status_label = label("添加连接后即可浏览和下载文件。", "muted")
        status.addWidget(self.status_label, 1)
        self.activity = label("就绪", "tiny")
        status.addWidget(self.activity)
        layout.addWidget(footer)
        shell.addWidget(workspace, 1)

    def build_files(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(27, 24, 27, 13)
        outer.setSpacing(17)
        heading = QHBoxLayout()
        self.page_title = label("文件", "title")
        heading.addWidget(self.page_title, 1)
        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("搜索当前存储桶中的文件…")
        self.search.setAccessibleName("搜索文件")
        self.search.addAction(icon("magnify"), QLineEdit.ActionPosition.LeadingPosition)
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(258)
        self.search.textChanged.connect(self.search_changed)
        heading.addWidget(self.search)
        outer.addLayout(heading)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(7)
        self.breadcrumbs = QHBoxLayout()
        self.breadcrumbs.setSpacing(0)
        toolbar.addLayout(self.breadcrumbs, 1)
        self.select_button = button("全选本页", callback=self.toggle_select_all)
        toolbar.addWidget(self.select_button)
        self.grid_button = button("", "view-grid-outline", kind="viewToggle", tooltip="网格视图", callback=lambda: self.set_view("grid"))
        self.list_button = button("", "format-list-bulleted", kind="viewToggle", tooltip="列表视图", callback=lambda: self.set_view("list"))
        for control in (self.grid_button, self.list_button):
            control.setCheckable(True)
            control.setFixedSize(31, 31)
            toolbar.addWidget(control)
        self.grid_button.setChecked(True)
        self.refresh_button = button("", "refresh", tooltip="刷新文件列表", callback=self.refresh_files)
        toolbar.addWidget(self.refresh_button)
        self.download_all_button = button("下载目录", tooltip="下载当前目录及其子目录中的全部文件", callback=lambda: self.create_download(True))
        toolbar.addWidget(self.download_all_button)
        self.download_button = button("下载所选", "download-outline", kind="primary", callback=lambda: self.create_download(False))
        toolbar.addWidget(self.download_button)
        outer.addLayout(toolbar)
        self.files_stack = QStackedWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        host = QWidget()
        self.grid = QGridLayout(host)
        self.grid.setContentsMargins(0, 0, 8, 0)
        self.grid.setHorizontalSpacing(15)
        self.grid.setVerticalSpacing(15)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(host)
        self.files_stack.addWidget(scroll)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["名称", "大小", "类型", "修改时间"])
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, width in ((1, 100), (2, 90), (3, 153)):
            self.table.setColumnWidth(column, width)
        self.table.itemSelectionChanged.connect(self.table_selected)
        self.table.cellDoubleClicked.connect(lambda row, column: self.open_entry(self.visible_entries[row]["key"]))
        self.files_stack.addWidget(self.table)
        self.empty = QWidget()
        empty = QVBoxLayout(self.empty)
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        glyph = QLabel()
        glyph.setPixmap(icon("database-outline", ACCENT).pixmap(60, 60))
        glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.addWidget(glyph)
        self.empty_title = label("连接你的第一个存储桶", "subheading")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.addWidget(self.empty_title)
        self.empty_detail = label("添加 R2 访问凭据后，即可浏览文件并批量下载。", "muted", True)
        self.empty_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_detail.setMaximumWidth(510)
        self.empty_detail.setMinimumWidth(420)
        empty.addWidget(self.empty_detail)
        self.empty_action = button("添加存储桶", "plus", kind="primary", callback=self.empty_clicked)
        empty.addWidget(self.empty_action, alignment=Qt.AlignmentFlag.AlignCenter)
        self.files_stack.addWidget(self.empty)
        outer.addWidget(self.files_stack, 1)
        pagination = QHBoxLayout()
        self.selection_note = label("", "muted")
        pagination.addWidget(self.selection_note, 1)
        self.file_previous = button("上一页", callback=lambda: self.file_page_delta(-1))
        self.file_next = button("下一页", callback=lambda: self.file_page_delta(1))
        pagination.addWidget(self.file_previous)
        self.page_label = label("", "tiny")
        pagination.addWidget(self.page_label)
        pagination.addWidget(self.file_next)
        outer.addLayout(pagination)
        return page

    def build_tasks(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 25, 28, 12)
        layout.setSpacing(18)
        heading = QHBoxLayout()
        heading.addWidget(label("下载任务", "title"), 1)
        heading.addWidget(button("全部暂停", "pause", callback=self.pause_all))
        heading.addWidget(button("全部继续", "play-outline", callback=self.resume_all))
        heading.addWidget(button("重试失败", "refresh", callback=self.retry_failed))
        heading.addWidget(button("清除已完成", callback=self.clear_completed))
        heading.addWidget(button("清空记录", callback=self.clear_tasks))
        layout.addLayout(heading)
        self.task_filters = QHBoxLayout()
        self.task_filters.setSpacing(4)
        self.filter_buttons: dict[str, QPushButton] = {}
        for state, name in (("all", "全部"), ("downloading", "下载中"), ("queued", "等待中"), ("paused", "已暂停"), ("completed", "已完成"), ("failed", "失败"), ("cancelled", "已停止")):
            control = button(name, kind="filter", callback=lambda checked=False, state=state: self.set_task_filter(state))
            control.setCheckable(True)
            self.filter_buttons[state] = control
            self.task_filters.addWidget(control)
        self.task_filters.addStretch()
        layout.addLayout(self.task_filters)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        host = QWidget()
        self.task_rows = QVBoxLayout(host)
        self.task_rows.setContentsMargins(0, 0, 8, 0)
        self.task_rows.setSpacing(0)
        self.task_rows.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(host)
        layout.addWidget(scroll, 1)
        footer = QHBoxLayout()
        footer.addWidget(label("任务记录保存在本机；清理记录不会删除已下载的文件。", "tiny"), 1)
        self.task_previous = button("上一页", callback=lambda: self.task_page_delta(-1))
        self.task_next = button("下一页", callback=lambda: self.task_page_delta(1))
        footer.addWidget(self.task_previous)
        self.task_page_label = label("", "tiny")
        footer.addWidget(self.task_page_label)
        footer.addWidget(self.task_next)
        layout.addLayout(footer)
        return page

    def current_profile(self) -> dict[str, Any]:
        return next((profile for profile in self.profiles if profile["id"] == self.bucket_id), {})

    def persist(self) -> bool:
        if self._closing_saved:
            return False
        if self.restore_warning:
            if QMessageBox.question(self, "修复本机数据", "原配置读取时出现问题。是否使用当前显示的连接和任务替换旧配置？取消会保留原文件。") != QMessageBox.StandardButton.Yes:
                return False
            self.restore_warning = ""
        try:
            self._state_revision = self.writer.submit(self.profiles, self.tasks, self.preferences)
            self.persist_dirty = False
            self.last_persist = time.monotonic()
            return True
        except (ValueError, OSError) as exc:
            self.status_label.setText("本机配置保存失败，请检查磁盘空间和目录权限。")
            if not getattr(self, "save_warning_shown", False):
                self.save_warning_shown = True
                QMessageBox.warning(self, "无法保存", str(exc))
            return False

    def render_buckets(self) -> None:
        self.clear_layout(self.bucket_layout)
        if not self.profiles:
            self.bucket_layout.addWidget(label("还没有添加存储桶", "muted", True))
        for profile in self.profiles:
            card = BucketCard(profile, profile["id"] == self.bucket_id)
            card.activated.connect(self.change_bucket)
            card.edited.connect(self.connection_menu)
            self.bucket_layout.addWidget(card)

    def connection_menu(self, profile_id: str) -> None:
        menu = QMenu(self)
        menu.addAction("编辑连接", lambda: self.edit_connection(profile_id))
        menu.addAction("移除此连接", lambda: self.remove_connection(profile_id))
        menu.exec(self.cursor().pos())

    def add_connection(self) -> None:
        dialog = ConnectionDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        profile = dialog.values()
        self.profiles.append(profile)
        if not self.persist():
            self.profiles.pop()
            return
        self.change_bucket(profile["id"])
        self.toast("连接已保存，正在读取真实文件列表。")

    def edit_connection(self, profile_id: str | None = None) -> None:
        target = next((profile for profile in self.profiles if profile["id"] == (profile_id or self.bucket_id)), None)
        if target is None:
            self.add_connection()
            return
        dialog = ConnectionDialog(self, target)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        old = copy.deepcopy(target)
        target.update(dialog.values())
        if not self.persist():
            target.clear()
            target.update(old)
            return
        self.files.pop(target["id"], None)
        self.catalogs.pop(target["id"], None)
        self.catalog_sources.pop(target["id"], None)
        self.loaded.discard(target["id"])
        if target["id"] in self.loading:
            self.loading.pop(target["id"]).set()
        self.scan_tokens.pop(target["id"], None)
        self.change_bucket(target["id"])

    def remove_connection(self, profile_id: str) -> None:
        if any(task.get("profile_id") == profile_id and task["status"] in ACTIVE for task in self.tasks):
            QMessageBox.information(self, "连接仍在使用", "此连接还有未结束的下载任务，请先取消任务后再移除。")
            return
        if QMessageBox.question(self, "移除本机连接", "仅移除此连接及本机保存的密钥，不会删除 R2 中的存储桶或文件。是否继续？") != QMessageBox.StandardButton.Yes:
            return
        before = self.profiles
        self.profiles = [profile for profile in before if profile["id"] != profile_id]
        if not self.persist():
            self.profiles = before
            return
        if profile_id in self.loading:
            self.loading.pop(profile_id).set()
        self.scan_tokens.pop(profile_id, None)
        self.files.pop(profile_id, None)
        self.catalogs.pop(profile_id, None)
        self.catalog_sources.pop(profile_id, None)
        self.loaded.discard(profile_id)
        self.load_errors.pop(profile_id, None)
        self.bucket_id = self.profiles[0]["id"] if self.profiles else ""
        for task in self.tasks:
            if task.get("profile_id") == profile_id:
                self.task_connections.pop(task["id"], None)
        self.change_bucket(self.bucket_id)
        self.toast("本机连接已移除。")

    def change_bucket(self, profile_id: str) -> None:
        self.reset_directory_pages()
        self.bucket_id = profile_id
        self.prefix = ""
        self.page_index = 0
        self.selected.clear()
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.switch_page(0)
        self.render_buckets()
        self.render_files()
        if profile_id and profile_id not in self.loaded and profile_id not in self.loading and self.current_profile().get("secret_access_key"):
            self.refresh_files()

    def switch_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        self.files_nav.setChecked(index == 0)
        self.tasks_nav.setChecked(index == 1)
        if index == 1:
            self.render_tasks()
        else:
            self.update_selection()

    def empty_clicked(self) -> None:
        if not self.current_profile():
            self.add_connection()
        elif not self.current_profile().get("secret_access_key"):
            self.edit_connection()
        else:
            self.refresh_files()

    def refresh_files(self) -> None:
        profile = self.current_profile()
        if not profile:
            self.add_connection()
            return
        if not profile.get("secret_access_key"):
            self.edit_connection()
            return
        profile_id = profile["id"]
        if profile_id in self.loading:
            return
        connection = connection_of(profile)
        try:
            validate_connection(connection)
        except ValueError as exc:
            QMessageBox.warning(self, "连接配置不完整", str(exc))
            return
        cancel = threading.Event()
        token = uuid.uuid4().hex
        self.scan_tokens[profile_id] = token
        self.loading[profile_id] = cancel
        self.load_errors.pop(profile_id, None)
        self.loaded.discard(profile_id)
        self.files.pop(profile_id, None)
        self.catalogs.pop(profile_id, None)
        self.catalog_sources.pop(profile_id, None)
        self.selected.clear()
        self.page_index = 0
        self.reset_directory_pages()
        self.render_files()

        def scan() -> None:
            try:
                raw = list_objects(connection, "", cancel, lambda count: self.events.put({"kind": "scan_progress", "profile_id": profile_id, "token": token, "count": count}))
                catalog = CatalogIndex(raw, cancel)
                self.events.put({"kind": "scan_done", "profile_id": profile_id, "token": token, "objects": catalog.entries, "catalog": catalog})
            except Cancelled:
                self.events.put({"kind": "scan_cancelled", "profile_id": profile_id, "token": token})
            except Exception as exc:
                self.events.put({"kind": "scan_error", "profile_id": profile_id, "token": token, "error": friendly_error(exc, connection)})

        threading.Thread(target=scan, daemon=True, name="r2-file-list").start()

    def reset_directory_pages(self) -> None:
        if self._directory_request is not None:
            self._directory_request.set()
            self._directory_request = None
        self._directory_pages.clear()
        self._directory_error = ""

    def request_directory_page(self) -> None:
        if self._directory_request is not None:
            return
        profile_id, prefix = self.bucket_id, self.prefix
        connection = connection_of(self.current_profile())
        continuation = self._directory_pages[-1][1] if self._directory_pages else ""
        cancel = threading.Event()
        self._directory_request = cancel
        page_size = self.PAGE_SIZE

        def read() -> None:
            event = {"kind": "directory_page", "profile_id": profile_id, "prefix": prefix, "request": cancel}
            try:
                rows, token = list_directory_page(connection, prefix, continuation, page_size, cancel)
                event.update(rows=rows, continuation=token)
            except Cancelled:
                return
            except Exception as exc:
                event["error"] = friendly_error(exc, connection)
            self.events.put(event)

        threading.Thread(target=read, daemon=True, name="r2-directory-page").start()

    def search_changed(self, query: str) -> None:
        self.page_index = 0
        self.selected.clear()
        self.search_timer.start(250)

    def request_catalog(self, entries: list[dict[str, Any]]) -> None:
        profile_id = self.bucket_id
        source = entries
        if getattr(self, "_catalog_build_source", None) is source:
            return
        self._catalog_build_source = source
        self.activity.setText("正在整理文件目录…")

        def build() -> None:
            try:
                catalog = CatalogIndex(source)
                self.events.put({"kind": "catalog_ready", "profile_id": profile_id, "source": source, "catalog": catalog})
            except Exception as exc:
                self.events.put({"kind": "background_error", "error": str(exc)})
        threading.Thread(target=build, daemon=True, name="r2-catalog-index").start()

    def request_search(self, catalog: CatalogIndex, query: str) -> None:
        key = (self.bucket_id, query, id(catalog))
        if getattr(self, "_search_pending", None) == key:
            return
        self._search_pending = key
        token = uuid.uuid4().hex
        self._query_token = token
        self.activity.setText("正在搜索文件…")
        self.download_button.setEnabled(False)
        self.select_button.setEnabled(False)
        self.download_all_button.setEnabled(False)

        def search() -> None:
            rows = catalog.search(query)
            self.events.put({"kind": "search_ready", "profile_id": key[0], "query": query, "token": token, "catalog": catalog, "rows": rows})
        threading.Thread(target=search, daemon=True, name="r2-file-search").start()

    def navigate(self, prefix: str) -> None:
        self.reset_directory_pages()
        self.prefix = prefix
        self.page_index = 0
        self.selected.clear()
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.render_files()

    def render_files(self) -> None:
        profile = self.current_profile()
        self.page_title.setText(profile.get("name", "文件"))
        entries = self.files.get(self.bucket_id, [])
        query = self.search.text().strip().casefold()
        loading = self.bucket_id in self.loading
        ready = bool(profile) and self.bucket_id in self.loaded and not loading
        catalog = self.catalogs.get(self.bucket_id)
        if loading:
            if not self._directory_pages and not self._directory_error:
                self.request_directory_page()
            self._file_cache_key = None
            self.all_entries, next_token = self._directory_pages[self.page_index] if self._directory_pages else ([], "")
            self.visible_entries = self.all_entries
            pages = len(self._directory_pages)
            has_next = self.page_index + 1 < pages or bool(next_token)
        elif catalog is None or self.catalog_sources.get(self.bucket_id) is not entries:
            # Loaded production catalogs are built by the scanner, never here.
            # Small direct catalogs support isolated UI fixtures and empty state.
            if len(entries) <= 5000:
                catalog = CatalogIndex(entries)
                self.catalogs[self.bucket_id] = catalog
                self.catalog_sources[self.bucket_id] = entries
            else:
                self.request_catalog(entries)
                return
        if not loading:
            cache_key = (id(catalog), self.prefix, query)
            if cache_key != self._file_cache_key:
                if query and len(entries) > 5000:
                    search_key = (self.bucket_id, query, id(catalog))
                    if search_key not in self._search_results:
                        self.request_search(catalog, query)
                        return
                    self.all_entries = self._search_results[search_key]
                else:
                    self.all_entries = catalog.search(query) if query else catalog.children(self.prefix)
                self._file_cache_key = cache_key
            pages = max(1, (len(self.all_entries) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
            self.page_index = min(self.page_index, pages - 1)
            self.visible_entries = self.all_entries[self.page_index * self.PAGE_SIZE:(self.page_index + 1) * self.PAGE_SIZE]
            has_next = self.page_index + 1 < pages
        self.clear_layout(self.breadcrumbs)
        self.breadcrumbs.addWidget(button("全部文件", kind="ghost", callback=lambda: self.navigate("")))
        if query:
            self.breadcrumbs.addWidget(label("/ 搜索结果", "muted"))
        elif self.prefix:
            parts = self.prefix.removesuffix("/").split("/")
            for index, part in enumerate(parts):
                self.breadcrumbs.addWidget(label("/", "tiny"))
                path = "/".join(parts[:index + 1]) + "/"
                self.breadcrumbs.addWidget(button(part or "/", kind="ghost", callback=lambda checked=False, path=path: self.navigate(path)))
        self.breadcrumbs.addStretch()
        if self.view_mode == "grid":
            self.clear_layout(self.grid)
            columns = max(2, min(5, (self.width() - 315) // 195))
            for index, entry in enumerate(self.visible_entries):
                card = FileCard(entry, entry["key"] in self.selected)
                if loading and entry["kind"] == "folder":
                    card.check.setEnabled(False)
                    card.check.setToolTip("后台索引完成后可选择整个文件夹")
                card.selected_changed.connect(self.card_selected)
                card.opened.connect(self.open_entry)
                self.grid.addWidget(card, index // columns, index % columns)
            for column in range(6):
                self.grid.setColumnStretch(column, 1 if column < columns else 0)
        self._rendering = True
        self.table.setRowCount(len(self.visible_entries))
        self.table.clearSelection()
        for row, entry in enumerate(self.visible_entries if self.view_mode == "list" else []):
            glyph, color, kind = file_style(entry)
            item = QTableWidgetItem(icon(glyph, color), entry["name"])
            item.setToolTip(entry["key"])
            self.table.setItem(row, 0, item)
            display_size = "—" if loading and entry["kind"] == "folder" else size_text(entry.get("size", 0))
            self.table.setItem(row, 1, QTableWidgetItem(display_size))
            self.table.setItem(row, 2, QTableWidgetItem(kind))
            self.table.setItem(row, 3, QTableWidgetItem(str(entry.get("modified", ""))[:16].replace("T", " ")))
            if loading and entry["kind"] == "folder":
                for column in range(4):
                    cell = self.table.item(row, column)
                    cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.table.setRowHeight(row, 50)
            if entry["key"] in self.selected:
                for column in range(4):
                    self.table.item(row, column).setSelected(True)
        self._rendering = False
        self.search.setEnabled(ready)
        self.search.setToolTip("后台正在建立全桶索引，完成后可搜索" if loading else "")
        self.refresh_button.setEnabled(bool(profile) and not loading)
        self.select_button.setEnabled((ready or loading) and bool(self.selectable_page_keys()))
        self.download_all_button.setEnabled(ready and not query and bool(catalog.children(self.prefix)))
        self.download_all_button.setToolTip("后台索引完成后可下载整个目录" if loading else "搜索时请选中需要下载的文件" if query else "下载当前目录及其子目录中的全部文件")
        if self.visible_entries:
            self.files_stack.setCurrentIndex(0 if self.view_mode == "grid" else 1)
        else:
            self.files_stack.setCurrentIndex(2)
            title, detail, action = "连接你的第一个存储桶", "添加 R2 访问凭据后，即可浏览文件并批量下载。", "添加存储桶"
            if loading:
                title, detail, action = "正在读取当前目录", "当前目录优先显示，全桶搜索将在后台索引完成后可用。", "正在读取…"
                if self._directory_error:
                    detail = self._directory_error + " · 正在等待全桶索引。"
                elif self._directory_pages and not next_token:
                    title, detail = "此目录为空", "全桶搜索将在后台索引完成后可用。"
            elif profile and not profile.get("secret_access_key"):
                title, detail, action = "需要填写私有密钥", "此连接未记住密钥。请重新输入 Secret Access Key 后连接。", "编辑连接"
            elif self.bucket_id in self.load_errors:
                title, detail, action = "连接失败", self.load_errors[self.bucket_id], "重新连接"
            elif ready:
                title, detail, action = ("没有找到匹配文件", "请换个文件名搜索，或清空搜索。", "刷新文件列表") if query else ("此目录为空", "当前目录没有可显示的文件或文件夹。", "刷新文件列表")
            elif profile:
                title, detail, action = "读取存储桶文件", "点击连接后读取真实文件列表。", "连接并读取"
            self.empty_title.setText(title)
            self.empty_detail.setText(detail)
            self.empty_action.setText(action)
            self.empty_action.setEnabled(not loading)
        self.file_previous.setEnabled(self.page_index > 0)
        self.file_next.setEnabled(has_next and (not loading or self._directory_request is None))
        self.file_next.setToolTip(self._directory_error if loading else "")
        self.page_label.setText(f"第 {self.page_index + 1} 页 · 索引中" if loading else f"{self.page_index + 1} / {pages}")
        self.update_selection()

    def set_view(self, mode: str) -> None:
        self.view_mode = mode
        self.grid_button.setChecked(mode == "grid")
        self.list_button.setChecked(mode == "list")
        self.render_files()

    def card_selected(self, key: str, checked: bool) -> None:
        self.selected.add(key) if checked else self.selected.discard(key)
        self.update_selection()

    def table_selected(self) -> None:
        if self._rendering:
            return
        self.selected.difference_update(entry["key"] for entry in self.visible_entries)
        self.selected.update(self.visible_entries[row.row()]["key"] for row in self.table.selectionModel().selectedRows())
        self.update_selection()

    def selected_files(self) -> list[dict[str, Any]]:
        catalog = self.catalogs.get(self.bucket_id)
        return catalog.selected_files(self.selected) if catalog else []

    def selectable_page_keys(self) -> set[str]:
        loading = self.bucket_id in self.loading
        return {entry["key"] for entry in self.visible_entries if not loading or entry["kind"] == "file"}

    def update_selection(self) -> None:
        count = len(self.selected)
        loading = self.bucket_id in self.loading
        listing = "全桶索引中" if loading else f"共 {len(self.all_entries):,} 项"
        self.selection_note.setText(f"{listing} · 本页 {len(self.visible_entries):,} 项" + (f" · 已选 {count:,} 项" if count else ""))
        self.download_button.setText(f"下载所选 ({count})" if count else "下载所选")
        page_keys = self.selectable_page_keys()
        self.download_button.setEnabled(bool(count) and (self.bucket_id in self.loaded or (loading and self.selected.issubset(page_keys))))
        self.select_button.setText("取消本页选择" if page_keys and page_keys.issubset(self.selected) else "全选本页")
        if self.pages.currentIndex() == 0:
            profile = self.current_profile()
            detail = f" · 已选 {count:,} 项" if count else ""
            if count and not loading:
                detail += "（文件夹包含子目录）"
            self.status_label.setText(profile.get("bucket", "") + detail if profile else "添加连接后即可浏览和下载文件。")

    def toggle_select_all(self) -> None:
        if self.pages.currentIndex() != 0:
            return
        keys = self.selectable_page_keys()
        self.selected.difference_update(keys) if keys and keys.issubset(self.selected) else self.selected.update(keys)
        self.render_files()

    def file_page_delta(self, delta: int) -> None:
        if self.bucket_id in self.loading:
            target = max(0, self.page_index + delta)
            if target >= len(self._directory_pages):
                if self._directory_pages and self._directory_pages[-1][1] and self._directory_request is None:
                    self._directory_error = ""
                    self.request_directory_page()
                    self.file_next.setEnabled(False)
                return
            self.selected.clear()
        self.page_index = max(0, self.page_index + delta)
        self.render_files()

    def open_entry(self, key: str) -> None:
        if key.endswith("/"):
            self.navigate(key)
        else:
            self.selected = {key}
            self.render_files()

    def create_download(self, whole_directory: bool = False) -> None:
        if self.clear_when_idle or self.closing or self.preparing:
            self.toast("正在取消和清理任务，请稍候再创建下载。")
            return
        profile = self.current_profile()
        if not profile:
            return
        prefix = self.prefix
        keys = self.selected.copy()
        catalog = self.catalogs.get(self.bucket_id)
        if not whole_directory and not keys:
            self.toast("所选范围没有可下载的文件。")
            return
        direct_files = []
        if self.bucket_id not in self.loaded or catalog is None:
            direct_files = [entry for entry in self.visible_entries
                            if entry["kind"] == "file" and entry["key"] in keys]
            if (self.bucket_id not in self.loading or whole_directory or len(direct_files) != len(keys)):
                self.toast("请等待读取完成后再下载文件夹或整桶。当前可以下载本页已加载的文件。")
                return

        connection = connection_of(profile)
        try:
            validate_connection(connection)
        except ValueError as exc:
            QMessageBox.warning(self, "请检查连接", str(exc))
            return
        download_prefix = "" if self.search.text().strip() and not whole_directory else prefix
        scope = (f"下载目录 · {prefix}" if prefix else "下载整桶") if whole_directory else "下载所选文件和文件夹"
        if direct_files:
            scope = "下载本页所选文件"

        def select_entries(cancel: threading.Event) -> list[dict[str, Any]]:
            if direct_files or catalog is None:
                return direct_files
            return (catalog.files_under(prefix, cancel=cancel) if whole_directory
                    else catalog.selected_files(keys, cancel=cancel))

        dialog = DownloadDialog(self, self.preferences, download_prefix, select_entries=select_entries,
                                scope=f"{scope}\n存储桶 {connection.bucket}")
        result = dialog.exec()
        dialog.deleteLater()
        if result != QDialog.DialogCode.Accepted or self.closing:
            return
        selected = dialog.entries
        if dialog.preflight is None or not selected:
            return
        output = str(dialog.preflight.output)
        self.preferences.update(output_dir=output, workers=dialog.workers.currentData(), skip_existing=dialog.skip.isChecked())
        # Materialize large download selections away from the GUI thread.
        settings = {"bucket": profile["bucket"], "profile_id": profile["id"], "connection_fingerprint": connection_fingerprint(connection), "output_dir": output, "workers": dialog.workers.currentData(), "overwrite": not dialog.skip.isChecked(), "strip_prefix": bool(download_prefix) and dialog.strip.isChecked(), "prefix": download_prefix, "created_at": datetime.now().astimezone().isoformat()}
        def materialize() -> list[dict[str, Any]]:
            return [{**settings, "id": uuid.uuid4().hex, "key": entry["key"], "name": entry["key"].split("/")[-1], "size": entry["size"], "etag": entry.get("etag", ""), "status": "queued", "transferred": 0, "error": ""} for entry in selected]
        if len(selected) > 500:
            self.preparing = True
            self._prepare_token = uuid.uuid4().hex
            token = self._prepare_token
            self.activity.setText("正在创建下载任务…")
            def prepare() -> None:
                self.events.put({"kind": "tasks_ready", "token": token, "tasks": materialize(), "connection": connection})
            threading.Thread(target=prepare, daemon=True, name="r2-task-prepare").start()
            return
        self.enqueue_tasks(materialize(), connection)

    def enqueue_tasks(self, created: list[dict[str, Any]], connection: Connection) -> None:
        self.tasks.extend(created)
        for task in created:
            self.task_connections[task["id"]] = connection
        self._task_structure_dirty = True
        self.dispatch_needed = True
        self.persist()
        self.task_filter = "all"
        self.task_page_index = 0
        self.switch_page(1)
        self.dispatch()

    def dispatch(self) -> None:
        if self.transfer_running or self.closing or self.clear_when_idle:
            return
        waiting = [task for task in self.tasks if task["status"] == "queued"]
        if not waiting:
            self.dispatch_needed = False
            return
        first = waiting[0]
        connection = self.task_connections.get(first["id"])
        if connection is None:
            first.update(status="interrupted", error="请重新确认连接后重试此任务。")
            self.task_dirty = self.persist_dirty = True
            return
        batch = []
        for task in waiting:
            if self.task_connections.get(task["id"]) == connection and task.get("workers", 4) == first.get("workers", 4):
                batch.append(task)
                if len(batch) >= 128:
                    break
        snapshots = copy.deepcopy(batch)
        self.in_flight.update(task["id"] for task in snapshots)
        self.transfer_running = True
        self.dispatch_needed = False
        cancels = {task["id"]: self.cancel_events.setdefault(task["id"], threading.Event()) for task in batch}
        pauses = {task["id"]: self.pause_events.setdefault(task["id"], threading.Event()) for task in batch}

        def transfer() -> None:
            try:
                transfer_tasks(connection, snapshots, cancels, int(first.get("workers", 4)), self.events.put, pause_events=pauses)
            except Exception as exc:
                self.events.put({"kind": "batch_error", "ids": [task["id"] for task in snapshots], "error": friendly_error(exc, connection)})
            finally:
                self.events.put({"kind": "batch_done", "ids": [task["id"] for task in snapshots]})

        self.worker = threading.Thread(target=transfer, daemon=True, name="r2-download-manager")
        self.worker.start()

    def set_task_filter(self, state: str) -> None:
        self.task_filter = state
        self.task_page_index = 0
        self._task_structure_dirty = True
        self.render_tasks()

    def task_matches(self, task: dict[str, Any], state: str) -> bool:
        actual = task["status"]
        return state == "all" or actual == state or (state == "completed" and actual == "skipped") or (state == "failed" and actual == "interrupted") or (state == "cancelled" and actual == "cancelling") or (state == "paused" and actual == "pausing")

    def sync_task_index(self) -> None:
        source = (id(self.tasks), len(self.tasks))
        if source != self._task_source:
            self.task_by_id = {task["id"]: task for task in self.tasks}
            self._task_source = source
            self._task_structure_dirty = True

    def render_tasks(self) -> None:
        self.sync_task_index()
        self.task_dirty = False
        self.last_render = time.monotonic()
        if self._task_structure_dirty:
            counts = {state: 0 for state in self.filter_buttons}
            matching = []
            active = 0
            for task in self.tasks:
                actual = task["status"]
                active += actual in ACTIVE
                counts["all"] += 1
                category = {"skipped": "completed", "interrupted": "failed", "cancelling": "cancelled", "pausing": "paused"}.get(actual, actual)
                if category in counts:
                    counts[category] += 1
                if self.task_matches(task, self.task_filter):
                    matching.append(task)
            self._matching_tasks = matching
            self._active_count = active
            for state, control in self.filter_buttons.items():
                name = {"all": "全部", "downloading": "下载中", "queued": "等待中", "paused": "已暂停", "completed": "已完成", "failed": "失败", "cancelled": "已停止"}[state]
                control.setText(f"{name}  {counts[state]:,}")
                control.setChecked(state == self.task_filter)
            self._task_structure_dirty = False
        matching = getattr(self, "_matching_tasks", [])
        pages = max(1, (len(matching) + self.TASK_PAGE_SIZE - 1) // self.TASK_PAGE_SIZE)
        self.task_page_index = min(self.task_page_index, pages - 1)
        shown = matching[self.task_page_index * self.TASK_PAGE_SIZE:(self.task_page_index + 1) * self.TASK_PAGE_SIZE]
        signature = (self.task_filter, self.task_page_index, tuple(task["id"] for task in shown))
        if signature != self._task_signature:
            self.clear_layout(self.task_rows)
            self.task_widgets.clear()
            if not shown:
                empty = label("还没有下载任务\n在文件页选择文件或目录后，点击下载即可开始。" if not self.tasks else "此分类暂无任务", "muted", True)
                empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
                empty.setMinimumHeight(200)
                self.task_rows.addWidget(empty)
            for task in shown:
                self.task_rows.addWidget(self.task_row(task))
            self._task_signature = signature
        for task in shown:
            self.update_task_row(task)
        self.tasks_nav.setText(f"下载任务  {len(self.tasks):,}" if self.tasks else "下载任务")
        self.task_previous.setEnabled(self.task_page_index > 0)
        self.task_next.setEnabled(self.task_page_index + 1 < pages)
        self.task_page_label.setText(f"{self.task_page_index + 1} / {pages}")
        if self.pages.currentIndex() == 1:
            self.status_label.setText(f"共 {len(self.tasks):,} 个任务 · {getattr(self, '_active_count', 0):,} 个未结束")

    def task_row(self, task: dict[str, Any]) -> QFrame:
        row = QFrame()
        row.setObjectName("taskRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(10, 18, 10, 18)
        layout.setSpacing(11)
        glyph, color, _ = file_style({"key": task["key"], "kind": "file"})
        image = QLabel()
        image.setPixmap(icon(glyph, color).pixmap(28, 28))
        layout.addWidget(image)
        names = QVBoxLayout()
        name = label(task.get("name", task["key"].split("/")[-1]))
        name.setToolTip(task["key"])
        name.setMinimumWidth(100)
        name.setMaximumWidth(250)
        names.addWidget(name)
        names.addWidget(label(task.get("bucket", ""), "tiny"))
        layout.addLayout(names, 4)
        detail = QVBoxLayout()
        progress = QProgressBar()
        progress.setRange(0, 1000)
        progress.setTextVisible(False)
        detail.addWidget(progress)
        info = label("", "tiny", True)
        detail.addWidget(info)
        layout.addLayout(detail, 5)
        state = label("")
        state.setFixedWidth(53)
        layout.addWidget(state)
        pause = button("", "pause", kind="ghost", tooltip="暂停下载", callback=lambda checked=False, task_id=task["id"]: self.toggle_pause(task_id))
        action = button("", "close", kind="ghost", tooltip="取消此任务", callback=lambda checked=False, task_id=task["id"]: self.task_action(task_id))
        remove = button("", "trash-can-outline", kind="ghost", tooltip="移除此任务记录", callback=lambda checked=False, task_id=task["id"]: self.remove_task(task_id))
        for control in (pause, action, remove):
            control.setFixedSize(30, 30)
            layout.addWidget(control)
        self.task_widgets[task["id"]] = {"row": row, "progress": progress, "info": info, "state": state, "pause": pause, "action": action, "remove": remove, "status": None}
        return row

    def update_task_row(self, task: dict[str, Any]) -> None:
        widgets = self.task_widgets.get(task["id"])
        if not widgets:
            return
        state = task["status"]
        size = task.get("size", 0)
        transferred = max(0, min(task.get("transferred", 0), size))
        fraction = transferred / size if size else (1 if state in {"completed", "skipped"} else 0)
        widgets["progress"].setValue(round(1000 * fraction))
        text = f"{round(fraction * 100)}% · {size_text(transferred)} / {size_text(size)}"
        if state == "downloading":
            elapsed = max(0.1, time.monotonic() - task.get("_started", time.monotonic()))
            text += f" · {size_text(max(0, transferred - task.get('_start_bytes', 0)) / elapsed)}/s"
        elif task.get("error") and state not in {"paused", "pausing"}:
            text = task["error"]
        widgets["info"].setText(text)
        widgets["info"].setToolTip(task.get("error") or task.get("path") or task.get("output_dir", ""))
        status = {"queued": "等待中", "downloading": "下载中", "completed": "已完成", "skipped": "已跳过", "failed": "失败", "cancelled": "已取消", "interrupted": "未完成", "cancelling": "取消中", "paused": "已暂停", "pausing": "暂停中"}.get(state, state)
        widgets["state"].setText(status)
        if widgets["status"] != state:
            state_color = {"failed": ERROR, "interrupted": ERROR, "downloading": ACCENT, "completed": SUCCESS}.get(state, MUTED)
            widgets["state"].setStyleSheet(f"font-size:12px;color:{state_color};")
            widgets["pause"].setIcon(icon("play-outline" if state == "paused" else "pause"))
            widgets["pause"].setToolTip("继续下载" if state == "paused" else "暂停下载")
            widgets["action"].setIcon(icon("refresh" if state in {"failed", "cancelled", "interrupted"} else "folder-open-outline" if state in {"completed", "skipped"} else "close"))
            widgets["action"].setToolTip("继续重试" if state in {"failed", "cancelled", "interrupted"} else "打开保存位置" if state in {"completed", "skipped"} else "取消此任务")
            widgets["status"] = state
        widgets["pause"].setEnabled(state in {"queued", "downloading", "paused"} and not (state == "paused" and task["id"] in self.in_flight))
        widgets["action"].setEnabled(state not in {"pausing", "cancelling"})
        widgets["remove"].setEnabled(state not in ACTIVE and task["id"] not in self.in_flight)

    def task_action(self, task_id: str) -> None:
        self.sync_task_index()
        task = self.task_by_id.get(task_id)
        if not task:
            return
        if task["status"] in ACTIVE or task["status"] == "paused":
            self.cancel_task(task_id)
        elif task["status"] in {"failed", "cancelled", "interrupted"}:
            self.retry_task(task_id)
        else:
            self.open_output(task)

    def task_page_delta(self, delta: int) -> None:
        self.task_page_index = max(0, self.task_page_index + delta)
        self.render_tasks()

    def cancel_task(self, task_id: str, cleanup: bool = True) -> None:
        self.sync_task_index()
        task = self.task_by_id.get(task_id)
        if task is None or task["status"] not in ACTIVE | {"paused"}:
            return
        if task_id in self.in_flight:
            self.cancel_events.setdefault(task_id, threading.Event()).set()
        task["status"] = "cancelling" if task_id in self.in_flight else "cancelled"
        if task_id not in self.in_flight:
            if cleanup and task.get("transferred", 0):
                self.cleanup_partials([dict(task)])
            task["transferred"] = 0
        self._task_structure_dirty = True
        self.task_dirty = self.persist_dirty = True

    def toggle_pause(self, task_id: str) -> None:
        self.sync_task_index()
        task = self.task_by_id.get(task_id)
        if task and task["status"] == "paused":
            self.retry_task(task_id)
        else:
            self.pause_task(task_id)

    def pause_task(self, task_id: str) -> None:
        self.sync_task_index()
        task = self.task_by_id.get(task_id)
        if not task or task["status"] not in {"queued", "downloading"}:
            return
        self.pause_events.setdefault(task_id, threading.Event()).set()
        task["status"] = "pausing" if task_id in self.in_flight else "paused"
        task["error"] = ""
        self.task_dirty = self.persist_dirty = self._task_structure_dirty = True

    def pause_all(self) -> None:
        for task in self.tasks:
            self.pause_task(task["id"])
        self.render_tasks()
        self.persist()

    def resume_all(self) -> None:
        for task in self.tasks:
            if task["status"] == "paused" and task["id"] not in self.in_flight:
                self.retry_task(task["id"], defer=True)
        self.persist()
        self.render_tasks()
        self.dispatch_needed = True

    def cleanup_partials(self, tasks: list[dict[str, Any]]) -> None:
        if not tasks:
            return
        self.cleaning.update(task["id"] for task in tasks)
        def cleanup() -> None:
            for task in tasks:
                try:
                    discard_partial(task)
                except (ValueError, OSError) as exc:
                    self.events.put({"kind": "background_error", "error": f"断点文件清理失败：{exc}"})
            self.events.put({"kind": "cleanup_done", "ids": [task["id"] for task in tasks]})
        threading.Thread(target=cleanup, daemon=True, name="r2-partial-cleanup").start()

    def retry_task(self, task_id: str, defer: bool = False) -> None:
        self.sync_task_index()
        task = self.task_by_id.get(task_id)
        if task is None or task["status"] in ACTIVE:
            return
        if task_id in self.in_flight or task_id in self.cleaning:
            self.toast("任务正在结束，请稍候再重试。")
            return
        profile = next((profile for profile in self.profiles if profile["id"] == task.get("profile_id")), None)
        if not profile:
            QMessageBox.information(self, "原连接已移除", "请添加连接并在文件页重新选择要下载的文件。")
            return
        if not profile.get("secret_access_key"):
            self.edit_connection(profile["id"])
            profile = next((profile for profile in self.profiles if profile["id"] == task.get("profile_id")), None)
            if not profile or not profile.get("secret_access_key"):
                return
        connection = connection_of(profile)
        if connection.bucket != task["bucket"] or (task.get("connection_fingerprint") and task["connection_fingerprint"] != connection_fingerprint(connection)):
            QMessageBox.warning(self, "连接目标已改变", "此连接的存储桶已修改。请从新的文件列表创建下载任务，避免下载错误对象。")
            return
        task.update(status="queued", error="")
        self.cancel_events[task_id] = threading.Event()
        self.pause_events[task_id] = threading.Event()
        self.task_connections[task_id] = connection
        self.task_dirty = self.persist_dirty = self._task_structure_dirty = True
        self.dispatch_needed = True
        if not defer:
            self.persist()
            self.render_tasks()
            self.dispatch()

    def retry_failed(self) -> None:
        for task in list(self.tasks):
            if task["status"] in {"failed", "interrupted"}:
                self.retry_task(task["id"], defer=True)
        self.persist()
        self.render_tasks()
        self.dispatch_needed = True

    def remove_task(self, task_id: str) -> None:
        self.cleanup_partials([dict(task) for task in self.tasks if task["id"] == task_id and task["status"] not in ACTIVE and task_id not in self.in_flight])
        self.tasks = [task for task in self.tasks if task["id"] != task_id or task["status"] in ACTIVE or task_id in self.in_flight]
        self.persist()
        self.render_tasks()

    def clear_completed(self) -> None:
        self.tasks = [task for task in self.tasks if task["status"] not in {"completed", "skipped"}]
        self.persist()
        self.render_tasks()
        self.toast("已清除完成记录，下载文件仍保留。")

    def clear_tasks(self) -> None:
        if self.preparing:
            self._prepare_token = ""
            self.preparing = False
        if not self.tasks:
            self.toast("任务记录已经为空。")
            return
        active = self.transfer_running or any(task["status"] in ACTIVE for task in self.tasks)
        message = "将取消未结束的任务，等待临时文件清理后清空全部记录。已下载成功的文件会保留。" if active else "清空全部下载记录？已下载的文件会保留。"
        if QMessageBox.question(self, "清空任务记录", message) != QMessageBox.StandardButton.Yes:
            return
        if active and self.transfer_running:
            self.clear_when_idle = True
            cleanup_tasks = [dict(task) for task in self.tasks if task["id"] not in self.in_flight and task.get("transferred", 0)]
            for task in self.tasks:
                self.cancel_task(task["id"], cleanup=False)
            self.cleanup_partials(cleanup_tasks)
            self.activity.setText("正在取消并清理任务…")
        else:
            self.cleanup_partials([dict(task) for task in self.tasks])
            self.tasks.clear()
            self.task_connections.clear()
            self.cancel_events.clear()
            self.pause_events.clear()
            self.persist()
            self.render_tasks()

    def open_output(self, task: dict[str, Any]) -> None:
        folder = Path(task.get("output_dir", "")).expanduser()
        if folder.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder.resolve())))
        else:
            QMessageBox.information(self, "保存位置不存在", "该保存文件夹已被移动或删除。")

    def show_settings(self) -> None:
        dialog = DownloadDialog(self, self.preferences)
        result = dialog.exec()
        dialog.deleteLater()
        if result != QDialog.DialogCode.Accepted:
            return
        self.preferences.update(output_dir=dialog.destination.text().strip(), workers=dialog.workers.currentData(), skip_existing=dialog.skip.isChecked())
        if self.persist():
            self.toast("下载设置已保存。")

    def show_update(self) -> None:
        dialog = UpdateDialog(self, VERSION, self.preferences.get("update_feed_url") or DEFAULT_UPDATE_FEED,
                              state_path().parent / "update-downloads", self.can_install_update)
        dialog.exec()
        feed, staged, release = dialog.checked_url, dialog.downloaded, dialog.release
        dialog.deleteLater()
        if feed:
            self.preferences["update_feed_url"] = feed
        if staged is None or release is None:
            if feed:
                self.persist()
            return
        if not self.can_install_update():
            self.discard_update(staged)
            QMessageBox.information(self, "暂停任务后安装", "请先暂停或完成正在进行和等待中的下载，再点击检查更新。")
            if feed:
                self.persist()
            return
        if not self.persist():
            self.discard_update(staged)
            return
        self._update_pending = (staged, release.sha256, self._state_revision)
        self.centralWidget().setEnabled(False)

    def can_install_update(self) -> bool:
        return not (self.closing or self.transfer_running or self.preparing or self.cleaning
                    or any(task["status"] in ACTIVE for task in self.tasks))

    @staticmethod
    def discard_update(staged: Path) -> None:
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass

    def continue_update(self) -> None:
        if self._update_pending is None:
            return
        self.activity.setText("正在保存任务以安装更新…")
        staged, digest, revision = self._update_pending
        if self.writer.completed_revision < revision:
            return
        self._update_pending = None
        if self.writer.saved_revision < revision:
            self.centralWidget().setEnabled(True)
            self.discard_update(staged)
            QMessageBox.warning(self, "无法安装更新", "任务记录未能保存，请检查本机磁盘和目录权限后重试。")
            return
        self._update_installing = True

        def prepare_restart() -> None:
            try:
                launch_update(staged, digest)
            except (UpdateInstallError, OSError) as exc:
                self.discard_update(staged)
                self.events.put({"kind": "update_failed", "error": str(exc)})
            else:
                self.events.put({"kind": "update_restart"})

        threading.Thread(target=prepare_restart, daemon=True, name="s3fm-update-install").start()

    def handle_event(self, event: dict[str, Any]) -> None:
        kind = event["kind"]
        if kind == "update_restart":
            self._update_installing = False
            self._closing_saved = True
            self.writer.close(timeout=0)
            self.close()
            return
        if kind == "update_failed":
            self._update_installing = False
            self.centralWidget().setEnabled(True)
            QMessageBox.warning(self, "无法安装更新", event["error"])
            return
        if kind == "cleanup_done":
            self.cleaning.difference_update(event["ids"])
            return
        if kind in {"save_error", "background_error"}:
            self.status_label.setText(event["error"])
            return
        if kind == "catalog_ready":
            profile_id = event["profile_id"]
            if self.files.get(profile_id) is event["source"]:
                self.catalogs[profile_id] = event["catalog"]
                self.catalog_sources[profile_id] = event["source"]
                self._file_cache_key = None
                if profile_id == self.bucket_id:
                    self.render_files()
            self._catalog_build_source = None
            return
        if kind == "search_ready":
            if event["token"] == self._query_token and self.catalogs.get(event["profile_id"]) is event["catalog"]:
                self._search_results.clear()
                self._search_results[(event["profile_id"], event["query"], id(event["catalog"]))] = event["rows"]
                self._file_cache_key = None
                self.render_files()
            self._search_pending = None
            return
        if kind == "tasks_ready":
            if event["token"] == self._prepare_token and not self.closing:
                self.preparing = False
                self.enqueue_tasks(event["tasks"], event["connection"])
            return
        if kind == "directory_page":
            if event["request"] is not self._directory_request or event["profile_id"] != self.bucket_id or event["prefix"] != self.prefix or self.bucket_id not in self.loading:
                return
            self._directory_request = None
            if "error" in event:
                self._directory_error = event["error"]
                if self._directory_pages:
                    self.toast("无法读取下一页：" + self._directory_error)
            else:
                self.selected.clear()
                self._directory_pages.append((event["rows"], event["continuation"]))
                self.page_index = len(self._directory_pages) - 1
            self.render_files()
            return
        if kind.startswith("scan_"):
            profile_id = event["profile_id"]
            profile = next((profile for profile in self.profiles if profile["id"] == profile_id), None)
            if profile is None or event["token"] != self.scan_tokens.get(profile_id):
                return
            if kind == "scan_progress":
                if profile_id == self.bucket_id:
                    self.activity.setText(f"已读取 {event['count']:,} 个对象…")
                return
            self.loading.pop(profile_id, None)
            if profile_id == self.bucket_id:
                self.reset_directory_pages()
            if kind == "scan_done":
                self.files[profile_id] = event["objects"]
                catalog = event.get("catalog")
                if catalog is not None:
                    self.catalogs[profile_id] = catalog
                    self.catalog_sources[profile_id] = event["objects"]
                self._file_cache_key = None
                self.loaded.add(profile_id)
                profile.update(file_count=catalog.file_count if catalog else len(event["objects"]), size_label=size_text(catalog.total_size if catalog else sum(entry["size"] for entry in event["objects"])), status="已连接")
            elif kind == "scan_error":
                self.load_errors[profile_id] = event["error"]
                profile["status"] = "连接失败"
            self.render_buckets()
            if profile_id == self.bucket_id:
                self.activity.setText("读取完成" if kind == "scan_done" else "读取失败" if kind == "scan_error" else "已停止读取")
                self.render_files()
            return
        if kind == "batch_error":
            for task in self.tasks:
                if task["id"] in event["ids"] and task["status"] in ACTIVE:
                    task.update(status="failed", transferred=0, error=event["error"])
        elif kind == "batch_done":
            self.transfer_running = False
            self.in_flight.difference_update(event["ids"])
            self.sync_task_index()
            for task_id in event["ids"]:
                task = self.task_by_id.get(task_id)
                if task and task["status"] in ACTIVE:
                    task.update(status="cancelled" if self.cancel_events.get(task["id"], threading.Event()).is_set() else "failed", error="任务未完成，可重试。")
                self.task_connections.pop(task_id, None)
                self.cancel_events.pop(task_id, None)
                self.pause_events.pop(task_id, None)
            if self.clear_when_idle:
                self.cleanup_partials([dict(task) for task in self.tasks])
                self.tasks.clear()
                self.task_connections.clear()
                self.cancel_events.clear()
                self.pause_events.clear()
                self.clear_when_idle = False
                self.activity.setText("任务记录已清空")
            self.dispatch_needed = True
        else:
            self.sync_task_index()
            task = self.task_by_id.get(event.get("id"))
            if task is None:
                return
            if kind == "started":
                task.update(status="downloading", _started=time.monotonic(), _start_bytes=task.get("transferred", 0))
            elif kind == "resumed":
                task.update(transferred=event["transferred"], _started=time.monotonic(), _start_bytes=event["transferred"])
            elif kind == "progress":
                task["transferred"] = min(task.get("size", 0), max(0, task.get("transferred", 0) + event.get("bytes", 0)))
            elif kind == "result":
                task.update({key: value for key, value in event.items() if key not in {"id", "kind"}})
                task["error"] = event.get("error", "")
            else:
                return
        self.task_dirty = True
        if kind != "progress":
            self.persist_dirty = True
            self._task_structure_dirty = True

    def poll(self) -> None:
        deadline = time.monotonic() + 0.012
        for _ in range(350):
            try:
                self.handle_event(self.events.get_nowait())
            except queue.Empty:
                break
            if time.monotonic() >= deadline:
                break
        now = time.monotonic()
        if self.task_dirty and self.pages.currentIndex() == 1 and now - self.last_render > 0.35:
            self.render_tasks()
        if self.persist_dirty and now - self.last_persist > 4:
            self.persist()
        if self.dispatch_needed and not self.transfer_running:
            self.dispatch()
        if self.transfer_running:
            self.activity.setText("正在下载文件…")
        elif not self.loading and not self.clear_when_idle and not self.preparing:
            self.activity.setText("就绪")
        self.continue_update()
        if self._update_installing:
            self.activity.setText("正在准备更新，即将重启…")
        if self.closing and not self.transfer_running:
            if not self._closing_saved:
                self.persist()
                self.writer.close(timeout=0)
                self._closing_saved = True
                self.centralWidget().setEnabled(False)
            if self.writer.is_closed:
                self.timer.stop()
                self.close()

    def toast(self, text: str) -> None:
        if self.toast_label is not None:
            self.toast_label.deleteLater()
        self.toast_label = label(text, "toast")
        self.toast_label.setParent(self)
        self.toast_label.adjustSize()
        self.toast_label.move(max(240, self.width() - self.toast_label.width() - 24), self.height() - 84)
        self.toast_label.show()
        self.toast_label.raise_()
        current = self.toast_label
        QTimer.singleShot(4000, lambda: current.hide() if current is self.toast_label else None)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        if hasattr(self, "resize_timer"):
            self.resize_timer.start(120)

    def closeEvent(self, event: Any) -> None:
        if self._update_pending is not None or self._update_installing:
            event.ignore()
            return
        if self.transfer_running and not self.closing:
            if QMessageBox.question(self, "暂停下载并退出", "正在下载文件。是否暂停任务、保留断点后退出？下次可继续下载。") != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.closing = True
            for task in self.tasks:
                self.pause_task(task["id"])
        if self.transfer_running:
            self.activity.setText("正在保存下载断点…")
            event.ignore()
            return
        for cancel in self.loading.values():
            cancel.set()
        self.reset_directory_pages()
        self.closing = True
        self._prepare_token = ""
        if not self._closing_saved:
            if not self.persist():
                event.ignore()
                self.closing = False
                return
            self.writer.close(timeout=0)
            self._closing_saved = True
            self.centralWidget().setEnabled(False)
        if self.writer.is_closed:
            self.timer.stop()
            event.accept()
        else:
            self.activity.setText("正在保存任务记录…")
            event.ignore()


def main(update_workspace: str = "") -> None:
    from .diagnostics import install_diagnostics
    application = QApplication(sys.argv)
    application.setApplicationDisplayName(APP_NAME)
    application.setWindowIcon(app_icon())
    application.setStyle("Fusion")
    application.setFont(QFont("Microsoft YaHei UI", 10))
    application.setStyleSheet(app_style())
    window = ClientWindow()
    install_diagnostics(lambda text: window.events.put({"kind": "background_error", "error": text}))
    window.show()
    if update_workspace:
        QTimer.singleShot(0, lambda: schedule_update_cleanup(update_workspace))
    sys.exit(application.exec())


if __name__ == "__main__":
    main()
