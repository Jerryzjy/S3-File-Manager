"""Production R2 desktop client: real connections, files and download records."""
from __future__ import annotations

import copy
import csv
import hashlib
from itertools import islice
import json
import queue
import sys
import tempfile
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from PySide6.QtCore import Qt, QSize, QSignalBlocker, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut, QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QGridLayout, QStackedWidget, QScrollArea, QLineEdit, QCheckBox,
    QDialog, QFileDialog, QProgressBar, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QMessageBox, QMenu, QFormLayout, QPlainTextEdit,
)

from . import APP_NAME, __version__
from .archive import archive_changes
from .bucket_tools import find_duplicates
from .ui import ACCENT, ERROR, MUTED, SUCCESS, BucketCard, Dropdown, FileCard, TabStrip, app_icon, app_style, button, file_style, icon, label, size_text
from .catalog import immediate_children, normalize_objects
from .client_store import load_state, remember_secret_label, save_state, state_path
from .diagnostics import install_diagnostics
from .settings_dialog import SettingsDialog
from .update_dialog import UpdatePanel
from .update_feed import UpdateRelease
from .update_install import UpdateInstallError, launch_update, schedule_update_cleanup
from .resumable_transfer import transfer_tasks, discard_partial
from .download_preflight import DownloadPreflight, inspect_download
from .state_writer import StateWriter
from .catalog_index import CatalogIndex
from .file_view import FileFilters, FileView
from .multipart_upload import discard_upload_checkpoint, is_multipart
from .object_actions import LINK_DURATIONS, generate_download_link, parse_public_url, public_object_url, supports_guarded_copy, transfer_copies
from .desktop_engine import Cancelled, Connection, create_client, list_directory_page, list_objects, strong_etag, validate_connection
from .upload import transfer_uploads, upload_error
from .upload_dialog import UploadDialog, choose_upload_sources
from .upload_import import save_url_sources, urls_from_text

VERSION = __version__
UPLOAD_PAGE = 2
OVERVIEW_PAGE = 3
TOOLS_PAGE = 4
ACTIVE = {"queued", "downloading", "cancelling", "pausing"}
FINISHED = {"completed", "skipped", "failed", "cancelled", "interrupted"}
FAILED = {"failed", "interrupted"}
TASK_CATEGORIES = {"skipped": "completed", "interrupted": "failed", "cancelling": "cancelled", "pausing": "paused"}
TASK_FILTERS = {"all": "全部", "downloading": "传输中", "queued": "等待中", "paused": "已暂停",
                "completed": "已完成", "failed": "失败", "cancelled": "已停止"}


def connection_of(profile: dict[str, Any]) -> Connection:
    credentials = {key: str(profile.get(key, "")).strip() for key in (
        "account_id", "access_key_id", "secret_access_key", "bucket", "endpoint_url",
    )}
    return Connection(**credentials, provider=profile.get("provider", "r2"), region=profile.get("region", ""))


def connection_fingerprint(connection: Connection) -> str:
    target = [connection.endpoint.lower(), connection.bucket]
    if connection.provider != "r2":
        target.extend([connection.provider, connection.signing_region])
    return hashlib.sha256(json.dumps(target, ensure_ascii=False).encode("utf-8")).hexdigest()


TASK_DIRECTIONS = {"download": "下载", "upload": "上传", "copy": "复制"}
SERVICE_NAMES = {"r2": "Cloudflare R2", "b2": "Backblaze B2", "s3": "自定义 S3"}


class UploadDropArea(QFrame):
    paths_dropped = Signal(object)

    def set_drag_active(self, active: bool) -> None:
        self.setProperty("dragActive", active)
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event) -> None:
        urls = event.mimeData().urls()
        if urls and all(url.isLocalFile() for url in urls):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self.set_drag_active(True)

    def dragLeaveEvent(self, event) -> None:
        self.set_drag_active(False)
        event.accept()

    def dropEvent(self, event) -> None:
        self.set_drag_active(False)
        urls = event.mimeData().urls()
        if urls and all(url.isLocalFile() for url in urls):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self.paths_dropped.emit(tuple(Path(url.toLocalFile()) for url in urls))


class UrlImportDialog(QDialog):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.cancel_request: Callable[[], bool] = lambda: False

    def reject(self) -> None:
        if not self.cancel_request():
            super().reject()


def write_failed_tasks(path: Path, rows: list[tuple[str, ...]]) -> None:
    def cell(text: str) -> str:
        # Treat service-supplied text as data when opened in a spreadsheet.
        if text.lstrip().startswith(("=", "+", "-", "@", "＝", "＋", "－", "＠")) or text.startswith(("\t", "\r", "\n")):
            return "'" + text
        return text

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.writer(stream)
            writer.writerow(("存储桶", "对象 Key", "任务状态", "失败原因"))
            writer.writerows(tuple(cell(value) for value in row) for row in rows)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
        self.setMinimumWidth(590)
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
        self.step = label("", "muted")
        outer.addWidget(self.step)
        self.steps = QStackedWidget()
        outer.addWidget(self.steps, 1)

        services = self.step_layout()
        services.addWidget(label("选择存储服务", "subheading"))
        services.addWidget(label("准备目标存储桶的名称和 S3 访问密钥。", "muted", True))
        service_row = QHBoxLayout()
        service_row.addWidget(label("存储服务", "fieldLabel"))
        self.provider = Dropdown()
        self.provider.setAccessibleName("存储服务")
        for key, name in SERVICE_NAMES.items():
            self.provider.addItem(name, key)
        self.provider.setCurrentIndex(max(0, self.provider.findData(self.profile.get("provider", "r2"))))
        service_row.addWidget(self.provider, 1)
        services.addLayout(service_row)
        services.addStretch()

        details = self.step_layout()
        for key, title, placeholder in (
            ("name", "连接名称", "为这个连接取一个名称"),
            ("bucket", "Bucket 名称", "仅填写存储桶名称"),
            ("account_id", "Account ID", "Cloudflare 账户 ID"),
            ("access_key_id", "Access Key ID", "存储服务的访问密钥 ID"),
            ("secret_access_key", "Secret Access Key", "与访问密钥 ID 配套的私有密钥"),
        ):
            field = QVBoxLayout()
            field.setSpacing(5)
            caption = label(title, "fieldLabel")
            field.addWidget(caption)
            if key == "account_id":
                self.account_label = caption
            entry = QLineEdit(str(self.profile.get(key, "")))
            entry.setPlaceholderText(placeholder)
            entry.setAccessibleName(title)
            self.fields[key] = entry
            field.addWidget(entry)
            details.addLayout(field)
        secret = self.fields["secret_access_key"]
        secret.setEchoMode(QLineEdit.EchoMode.Password)
        reveal = secret.addAction(icon("eye-outline"), QLineEdit.ActionPosition.TrailingPosition)
        reveal.triggered.connect(lambda: secret.setEchoMode(QLineEdit.EchoMode.Normal if secret.echoMode() == QLineEdit.EchoMode.Password else QLineEdit.EchoMode.Password))
        self.remember = QCheckBox(remember_secret_label())
        self.remember.setChecked(bool(self.profile.get("remember_secret", False)))
        details.addWidget(self.remember)
        self.advanced = QCheckBox("自定义 S3 Endpoint")
        details.addWidget(self.advanced)
        self.endpoint = QLineEdit(str(self.profile.get("endpoint_url", "")))
        self.endpoint.setPlaceholderText("https://<ACCOUNT_ID>.r2.cloudflarestorage.com")
        self.endpoint.setAccessibleName("S3 Endpoint")
        self.endpoint.setVisible(bool(self.endpoint.text()))
        self.advanced.setChecked(bool(self.endpoint.text()))
        self.advanced.toggled.connect(self.update_provider_fields)
        details.addWidget(self.endpoint)
        self.region = QLineEdit(self.profile.get("region", ""))
        self.region.setPlaceholderText("S3 Region，例如 us-east-1")
        self.region.setAccessibleName("S3 Region")
        details.addWidget(self.region)
        details.addStretch()
        self.provider.currentIndexChanged.connect(self.update_provider_fields)
        self.update_provider_fields()
        sharing = self.step_layout()
        sharing.addWidget(label("公开链接（可选）", "subheading"))
        sharing.addWidget(label("填写已在服务商配置好的公开 HTTPS 域名，例如 R2 的自定义域名或 r2.dev 域名。留空即可继续使用私有桶和限时链接。", "muted", True))
        self.public_url = QLineEdit(self.profile.get("public_url", ""))
        self.public_url.setPlaceholderText("https://files.example.com")
        self.public_url.setAccessibleName("公开域名")
        sharing.addWidget(self.public_url)
        sharing.addStretch()
        review = self.step_layout()
        review.addWidget(label("确认连接信息", "subheading"))
        self.review = label("", "muted", True)
        self.review.setTextFormat(Qt.TextFormat.PlainText)
        review.addWidget(self.review)
        review.addStretch()
        self.validation = label("需要目标存储桶的对象读取权限。", "helper", True)
        self.validation.setTextFormat(Qt.TextFormat.PlainText)
        outer.addWidget(self.validation)
        actions = QHBoxLayout()
        self.test_button = button("测试连接", "connection", callback=self.test_connection)
        self.save_button = button("连接并保存", kind="primary", callback=self.save)
        self.previous_button = button("上一步", callback=lambda: self.show_step(self.steps.currentIndex() - 1))
        self.next_button = button("下一步", kind="primary", callback=self.next_step)
        actions.addWidget(self.previous_button)
        actions.addWidget(self.test_button)
        actions.addStretch()
        actions.addWidget(button("取消", callback=self.reject))
        actions.addWidget(self.next_button)
        actions.addWidget(self.save_button)
        outer.addLayout(actions)
        self.show_step(1 if profile else 0)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(90)

    def step_layout(self) -> QVBoxLayout:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)
        self.steps.addWidget(page)
        return layout

    def show_step(self, index: int) -> None:
        self.steps.setCurrentIndex(index)
        name = ("存储服务", "桶与密钥", "公开链接", "确认与测试")[index]
        self.step.setText(f"{index + 1} / {self.steps.count()}   {name}")
        self.previous_button.setVisible(index > 0)
        self.next_button.setVisible(index < 3)
        self.test_button.setVisible(index == 3)
        self.save_button.setVisible(index == 3)
        self.set_message("需要目标存储桶的对象读取权限。" if index == 3 else "", "helper")
        if index == 3:
            values = self.values()
            self.review.setText("\n".join((
                f"存储服务   {SERVICE_NAMES[values['provider']]}",
                f"连接名称   {values['name']}",
                f"存储桶   {values['bucket']}",
                f"Endpoint   {connection_of(values).endpoint}",
                f"公开域名   {values['public_url'] or '未配置'}",
            )))

    def next_step(self) -> None:
        index = self.steps.currentIndex()
        try:
            if index == 1:
                self.validate(check_public=False)
            elif index == 2:
                parse_public_url(self.public_url.text())
        except ValueError as exc:
            self.set_message(str(exc), "failure")
            return
        self.show_step(index + 1)

    def values(self) -> dict[str, Any]:
        result = {key: field.text().strip() for key, field in self.fields.items()}
        provider = self.provider.currentData()
        result.update(id=self.profile.get("id") or uuid.uuid4().hex,
                      provider=provider, region=self.region.text().strip() if provider == "s3" else "",
                      endpoint_url=self.endpoint.text().strip() if provider != "r2" or self.advanced.isChecked() else "",
                      remember_secret=self.remember.isChecked(), public_url=self.public_url.text().strip().rstrip("/"))
        if provider != "r2":
            result["account_id"] = ""
        return result

    def update_provider_fields(self) -> None:
        provider = self.provider.currentData()
        r2 = provider == "r2"
        self.account_label.setVisible(r2)
        self.fields["account_id"].setVisible(r2)
        self.advanced.setVisible(r2)
        self.endpoint.setVisible(not r2 or self.advanced.isChecked())
        self.region.setVisible(provider == "s3")
        self.endpoint.setPlaceholderText({"r2": "https://<ACCOUNT_ID>.r2.cloudflarestorage.com",
                                          "b2": "https://s3.us-west-004.backblazeb2.com",
                                          "s3": "https://s3.us-east-1.amazonaws.com"}[provider])
        self.fields["access_key_id"].setPlaceholderText("Backblaze keyID" if provider == "b2" else "存储服务的访问密钥 ID")
        self.fields["secret_access_key"].setPlaceholderText("Backblaze applicationKey" if provider == "b2" else "与访问密钥 ID 配套的私有密钥")

    def validate(self, *, check_public: bool = True) -> Connection:
        if not self.fields["name"].text().strip():
            raise ValueError("请填写连接名称。")
        connection = connection_of(self.values())
        validate_connection(connection)
        if check_public:
            parse_public_url(self.public_url.text())
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
        self.previous_button.setEnabled(False)
        for entry in self.fields.values():
            entry.setEnabled(False)
        self.endpoint.setEnabled(False)
        self.provider.setEnabled(False)
        self.region.setEnabled(False)
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
        self.previous_button.setEnabled(True)
        for entry in self.fields.values():
            entry.setEnabled(True)
        self.endpoint.setEnabled(True)
        self.provider.setEnabled(True)
        self.region.setEnabled(True)
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
        scope: str = "", archive_target: str = "",
    ) -> None:
        super().__init__(parent)
        self.select_entries = select_entries
        self.prefix = prefix
        self.archive_target = archive_target
        self.archive_entries: list[dict[str, Any]] = []
        self.entries: list[dict[str, Any]] | None = None
        self.preflight: DownloadPreflight | None = None
        self._check_cancel: threading.Event | None = None
        self._check_events: queue.Queue = queue.Queue()
        settings = select_entries is None
        self.setWindowTitle("增量归档" if archive_target else "下载设置" if settings else "新建下载任务")
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
        self.workers = Dropdown()
        for count_workers in (1, 2, 4, 6, 8, 12, 16):
            self.workers.addItem(f"{count_workers} 个文件", count_workers)
        index = self.workers.findData(preferences.get("workers", 4))
        self.workers.setCurrentIndex(max(0, index))
        row.addWidget(self.workers)
        row.addStretch()
        layout.addLayout(row)
        self.skip = QCheckBox("跳过本地同大小的已有文件")
        self.skip.setChecked(bool(preferences.get("skip_existing", True)))
        if archive_target:
            self.skip.setChecked(False)
            self.skip.hide()
            layout.addWidget(label("根据上次归档及本地文件状态下载变更项。保留目录中的其他文件。", "helper", True))
        else:
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
        if archive_target:
            self.archive_preview = button("查看变更", callback=self.show_archive_changes)
            self.archive_preview.setEnabled(False)
            actions.addWidget(self.archive_preview)
        actions.addStretch()
        actions.addWidget(button("取消", callback=self.reject))
        confirm_title = "开始归档" if archive_target else "保存设置" if settings else "开始下载"
        self.confirm_button = button(confirm_title, "" if settings else "download", kind="primary", callback=self.confirm)
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
        if self.archive_target:
            self.archive_preview.setEnabled(False)
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
            changes = []
            try:
                if files is None:
                    files = select_entries(cancel)
                if self.archive_target:
                    changes = archive_changes(files, output, prefix, strip_prefix, self.archive_target, cancel)
                    result = inspect_download(changes, output, prefix, strip_prefix, False, cancel) if changes else None
                else:
                    result = inspect_download(files, output, prefix, strip_prefix, skip_existing, cancel)
                events.put((files, changes, result, ""))
            except Cancelled:
                events.put((files, [], None, "检查已取消。"))
            except Exception as exc:
                events.put((files, [], None, str(exc)))

        threading.Thread(target=check, daemon=True, name="r2-download-preflight").start()
        self.check_poll.start(35)

    def poll_check(self) -> None:
        try:
            entries, changes, result, error = self._check_events.get_nowait()
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
            if self.archive_target and not error:
                self.check_status.setText("所选范围已归档，没有需要下载的变更。" if entries else "此范围没有可归档的文件。")
                return
            self.check_status.clear()
            self.validation.setText(error)
            self.validation.show()
            return
        details = [f"{result.count:,} 个文件 · 总大小 {size_text(result.total)}",
                   f"可用空间 {size_text(result.free)} · 预计需要 {size_text(result.required)}"]
        if self.archive_target:
            self.archive_entries = changes
            self.archive_preview.setEnabled(True)
            details[0] = f"{result.count:,} 个变更 · {size_text(result.total)} · {len(entries) - result.count:,} 个未变"
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
        may_replace = not self.archive_target or bool(self.preflight and self.preflight.existing)
        if may_replace and not self.skip.isChecked() and QMessageBox.question(self, "确认覆盖同名文件", "本次操作允许替换保存位置中的同名文件。是否继续？") != QMessageBox.StandardButton.Yes:
            return
        self.accept()

    def show_archive_changes(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("归档变更")
        dialog.resize(620, 360)
        layout = QVBoxLayout(dialog)
        count = len(self.archive_entries)
        layout.addWidget(label(f"共 {count:,} 项变更" + ("，显示前 500 项" if count > 500 else "")))
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText("\n".join(f"{item['archive_change']}  {item['key']}" for item in self.archive_entries[:500]))
        layout.addWidget(text)
        layout.addWidget(button("关闭", callback=dialog.accept), alignment=Qt.AlignmentFlag.AlignRight)
        dialog.exec()
        dialog.deleteLater()

    def done(self, result: int) -> None:
        if self._check_cancel is not None:
            self._check_cancel.set()
        self.check_timer.stop()
        self.check_poll.stop()
        super().done(result)


class FileFilterDialog(QDialog):
    def __init__(self, parent: QWidget, filters: FileFilters) -> None:
        super().__init__(parent)
        self.setWindowTitle("筛选文件")
        self.setMinimumWidth(420)
        self.filters = filters
        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)
        self.fields = {}
        for key, title, value, hint in (
            ("extensions", "扩展名", ", ".join(filters.extensions), "例如 pdf, jpg, tar.gz"),
            ("min_size", "最小大小（字节）", filters.min_size, ""),
            ("max_size", "最大大小（字节）", filters.max_size, ""),
            ("since", "修改日期起（UTC）", filters.since, "YYYY-MM-DD"),
            ("until", "修改日期止（UTC）", filters.until, "YYYY-MM-DD"),
        ):
            field = QLineEdit("" if value is None else str(value))
            field.setPlaceholderText(hint)
            form.addRow(title, field)
            self.fields[key] = field
        layout.addWidget(label("留空表示不限。大小和日期包含边界值。", "helper", True))
        self.error = label("", "failure", True)
        self.error.hide()
        layout.addWidget(self.error)
        actions = QHBoxLayout()
        actions.addWidget(button("清除筛选", callback=self.clear_filters))
        actions.addStretch()
        actions.addWidget(button("取消", callback=self.reject))
        actions.addWidget(button("应用", kind="primary", callback=self.apply_filters))
        layout.addLayout(actions)

    def clear_filters(self) -> None:
        self.filters = FileFilters()
        self.accept()

    def apply_filters(self) -> None:
        values = {key: field.text().strip() for key, field in self.fields.items()}
        extensions = tuple(dict.fromkeys(
            "." + value.strip().lstrip(".").casefold()
            for value in values["extensions"].replace("，", ",").split(",")
            if value.strip().lstrip(".")
        ))
        if any(any(char.isspace() or char in "/\\*?" for char in extension) for extension in extensions):
            self.error.setText("请输入逗号分隔的扩展名，例如 pdf, jpg。")
            self.error.show()
            return
        try:
            sizes = []
            for key in ("min_size", "max_size"):
                value = values[key]
                if value and (not value.isascii() or not value.isdecimal() or len(value) > 18):
                    raise ValueError
                sizes.append(int(value) if value else None)
            dates = []
            for key in ("since", "until"):
                value = values[key]
                parsed = date.fromisoformat(value) if value else None
                if parsed is not None and parsed.isoformat() != value:
                    raise ValueError
                dates.append(parsed)
        except ValueError:
            self.error.setText("大小请输入非负整数。日期格式为 YYYY-MM-DD。")
            self.error.show()
            return
        if any(start is not None and end is not None and start > end for start, end in (sizes, dates)):
            self.error.setText("下限不能大于上限。")
            self.error.show()
            return
        self.filters = FileFilters(extensions, *sizes, *dates)
        self.accept()


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
        self._search_result: tuple[tuple[str, FileView, CatalogIndex], list[dict[str, Any]]] | None = None
        self._search_pending: tuple[str, FileView, CatalogIndex] | None = None
        self._search_cancel: threading.Event | None = None
        self.file_filters = FileFilters()
        self.sort_column = self.preferences.get("file_sort", -1)
        self.sort_descending = self.preferences.get("file_descending", False)
        self.PAGE_SIZE = self.preferences.get("file_page_size", self.PAGE_SIZE)
        self.loaded: set[str] = set()
        self.loading: dict[str, threading.Event] = {}
        self.load_errors: dict[str, str] = {}
        self.scan_tokens: dict[str, str] = {}
        self.synced_at: dict[str, str] = {profile["id"]: profile.get("last_sync", "") for profile in self.profiles}
        self.sync_history: list[tuple[str, str, str]] = []
        self.duplicate_cancel: threading.Event | None = None
        self._directory_pages: list[tuple[list[dict[str, Any]], str]] = []
        self._directory_request: threading.Event | None = None
        self._directory_error = ""
        self.bucket_id = self.profiles[0]["id"] if self.profiles else ""
        self.prefix = ""
        self.selected: set[str] = set()
        self.view_mode = self.preferences.get("file_view", "grid")
        self.task_filter = "all"
        self.task_direction: str | None = None
        self.task_batch: str | None = None
        self.task_batches: dict[str, list[dict[str, Any]]] = {}
        self._batch_selector_dirty = True
        self._batch_size = self._batch_bytes = self._batch_completed = 0
        self._exporting_failures = False
        self._file_refresh_profiles: set[str] = set()
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
        self._close_revision: int | None = None
        self._state_revision = 0
        self._update_pending: tuple[Path, str, int] | None = None
        self._update_installing = False
        self.task_connections: dict[str, Connection] = {}
        self.in_flight: set[str] = set()
        self.cleaning: set[str] = set()
        self.upload_cleaning: set[str] = set()
        self.transfer_running = False
        self.worker: threading.Thread | None = None
        self.closing = False
        self.clear_when_idle = False
        self.task_dirty = False
        self.persist_dirty = False
        self.last_render = self.last_persist = time.monotonic()
        self.last_queue_preview = 0.0
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
        self.grid_button.setChecked(self.view_mode == "grid")
        self.list_button.setChecked(self.view_mode == "list")
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
        if self.profiles:
            self.switch_page({"upload": UPLOAD_PAGE, "tasks": 1, "overview": OVERVIEW_PAGE}.get(self.preferences.get("startup_page"), 0))

    @staticmethod
    def clear_layout(layout: Any) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
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
        self.settings_button = button("设置", "cog-outline", kind="sidebarAction", callback=self.show_settings)
        side.addWidget(self.settings_button)
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
        self.files_nav = button("文件", "folder-outline", kind="nav", callback=lambda: self.switch_page(0))
        self.upload_nav = button("上传", "upload-outline", kind="nav", callback=lambda: self.switch_page(UPLOAD_PAGE))
        self.tasks_nav = button("传输任务", "swap-vertical", kind="nav", callback=lambda: self.switch_page(1))
        self.overview_nav = button("统计", "chart-box-outline", kind="nav", callback=lambda: self.switch_page(OVERVIEW_PAGE))
        self.tools_nav = button("桶工具", "tools", kind="nav", callback=lambda: self.switch_page(TOOLS_PAGE))
        self.navigation = TabStrip((self.files_nav, self.upload_nav, self.tasks_nav, self.overview_nav, self.tools_nav))
        top.addWidget(self.navigation)
        top.addStretch()
        top.addWidget(label(f"v{VERSION}", "tiny"))
        layout.addWidget(topbar)
        self.pages = QStackedWidget()
        self.pages.addWidget(self.build_files())
        self.pages.addWidget(self.build_tasks())
        self.pages.addWidget(self.build_upload())
        self.pages.addWidget(self.build_overview())
        self.pages.addWidget(self.build_bucket_tools())
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

    def build_upload(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(27, 24, 27, 17)
        layout.setSpacing(16)
        layout.addWidget(label("上传", "title"))
        self.upload_target = label("", "muted", True)
        self.upload_target.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.upload_target)

        self.upload_drop = UploadDropArea()
        self.upload_drop.setObjectName("uploadDrop")
        self.upload_drop.paths_dropped.connect(self.create_upload, Qt.ConnectionType.QueuedConnection)
        drop = QVBoxLayout(self.upload_drop)
        drop.setContentsMargins(24, 22, 24, 22)
        drop.setSpacing(12)
        drop.addStretch()
        glyph = QLabel()
        glyph.setPixmap(icon("upload-outline", ACCENT).pixmap(44, 44))
        drop.addWidget(glyph, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.upload_title = label("拖入文件或文件夹", "subheading")
        drop.addWidget(self.upload_title, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.upload_help = label("", "muted", True)
        self.upload_help.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        drop.addWidget(self.upload_help)
        actions = QHBoxLayout()
        actions.addStretch()
        self.upload_choose_files = button("选择文件", "file-outline", kind="primary", callback=lambda: self.create_upload(choose_folder=False))
        actions.addWidget(self.upload_choose_files)
        self.upload_choose_folder = button("选择文件夹", "folder-outline", callback=lambda: self.create_upload(choose_folder=True))
        actions.addWidget(self.upload_choose_folder)
        self.upload_add_url = button("添加网址", "link-variant", callback=self.import_upload_urls)
        actions.addWidget(self.upload_add_url)
        self.upload_paste = button("粘贴文件或文本", "clipboard-outline", callback=self.paste_upload)
        actions.addWidget(self.upload_paste)
        self.upload_connect = button("添加存储桶", "plus", kind="primary", callback=self.add_connection)
        actions.addWidget(self.upload_connect)
        actions.addStretch()
        drop.addLayout(actions)
        drop.addStretch()
        layout.addWidget(self.upload_drop, 1)
        return page

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
        self.search.setPlaceholderText("搜索文件名或对象路径…")
        self.search.setAccessibleName("搜索文件")
        self.search.addAction(icon("magnify"), QLineEdit.ActionPosition.LeadingPosition)
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(258)
        self.search.textChanged.connect(self.search_changed)
        self.search_scope = Dropdown()
        self.search_scope.setAccessibleName("搜索和筛选范围")
        self.search_scope.addItem("整个存储桶", True)
        self.search_scope.addItem("当前目录及子目录", False)
        self.search_scope.currentIndexChanged.connect(self.file_view_changed)
        heading.addWidget(self.search_scope)
        heading.addWidget(self.search)
        self.filter_button = button("筛选", callback=self.show_file_filters)
        heading.addWidget(self.filter_button)
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
        self.upload_button = button("上传", "upload-outline", tooltip="将本地文件上传到当前目录", callback=lambda: self.create_upload())
        toolbar.addWidget(self.upload_button)
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
        self.table.setHorizontalHeaderLabels(["名称", "大小", "扩展名", "修改时间"])
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionsClickable(True)
        self.table.horizontalHeader().sectionClicked.connect(self.sort_files)
        self.table.horizontalHeader().setSortIndicatorShown(False)
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
        self.queue_toggle = button("传输队列", "chevron-up")
        self.queue_toggle.setCheckable(True)
        self.queue_toggle.toggled.connect(self.show_queue_preview)
        pagination.addWidget(self.queue_toggle)
        self.details_button = button("文件详情", callback=self.show_file_details)
        pagination.addWidget(self.details_button)
        self.file_actions = button("更多操作", "dots-horizontal", callback=self.show_file_actions)
        pagination.addWidget(self.file_actions)
        self.file_previous = button("上一页", callback=lambda: self.file_page_delta(-1))
        self.file_next = button("下一页", callback=lambda: self.file_page_delta(1))
        pagination.addWidget(self.file_previous)
        self.page_label = label("", "tiny")
        pagination.addWidget(self.page_label)
        pagination.addWidget(self.file_next)
        outer.addLayout(pagination)
        self.queue_preview = QFrame()
        self.queue_preview.setObjectName("taskRow")
        queue_layout = QVBoxLayout(self.queue_preview)
        queue_layout.setContentsMargins(12, 8, 12, 9)
        queue_header = QHBoxLayout()
        self.queue_state = label("", "muted")
        queue_header.addWidget(self.queue_state, 1)
        queue_header.addWidget(button("查看全部任务", callback=lambda: self.switch_page(1)))
        queue_layout.addLayout(queue_header)
        self.queue_preview_rows = []
        for _ in range(3):
            row = label("", "tiny")
            row.setTextFormat(Qt.TextFormat.PlainText)
            queue_layout.addWidget(row)
            self.queue_preview_rows.append(row)
        self.queue_preview.hide()
        outer.addWidget(self.queue_preview)
        return page

    def build_overview(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(27, 24, 27, 17)
        layout.setSpacing(15)
        heading = QHBoxLayout()
        heading.addWidget(label("统计", "title"), 1)
        self.overview_refresh = button("刷新存储桶", "refresh", callback=self.refresh_files)
        heading.addWidget(self.overview_refresh)
        layout.addLayout(heading)
        self.overview_note = label("", "muted", True)
        self.overview_note.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.overview_note)
        cards = QHBoxLayout()
        self.overview_values = []
        for title in ("对象数", "对象总大小", "本机已完成下载", "本机已完成上传"):
            card = QFrame()
            card.setObjectName("settingsCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(16, 15, 16, 15)
            card_layout.addWidget(label(title, "muted"))
            value = label("—", "title")
            card_layout.addWidget(value)
            self.overview_values.append(value)
            cards.addWidget(card, 1)
        layout.addLayout(cards)
        layout.addWidget(label("最近 30 天发起的上传", "subheading"))
        self.upload_calendar = QTableWidget(1, 30)
        self.configure_report_table(self.upload_calendar)
        self.upload_calendar.horizontalHeader().hide()
        self.upload_calendar.setFixedHeight(35)
        self.upload_calendar.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.upload_calendar.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.upload_calendar.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.upload_calendar.setAccessibleName("近 30 天每日上传活动")
        self.upload_calendar.cellClicked.connect(lambda _row, column: self.upload_activity.scrollToItem(self.upload_activity.item(29 - column, 0)))
        layout.addWidget(self.upload_calendar)
        self.upload_activity = QTableWidget(30, 3)
        self.upload_activity.setHorizontalHeaderLabels(["日期", "文件数", "总大小"])
        self.configure_report_table(self.upload_activity)
        self.upload_activity.setMaximumHeight(218)
        layout.addWidget(self.upload_activity)
        layout.addWidget(label("同步记录", "subheading"))
        self.sync_activity = QTableWidget(0, 2)
        self.sync_activity.setHorizontalHeaderLabels(["时间", "结果"])
        self.configure_report_table(self.sync_activity)
        layout.addWidget(self.sync_activity, 1)
        return page

    @staticmethod
    def configure_report_table(table: QTableWidget) -> None:
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

    def render_overview(self) -> None:
        profile = self.current_profile()
        catalog = self.catalogs.get(self.bucket_id)
        loading = self.bucket_id in self.loading
        self.overview_refresh.setEnabled(bool(profile) and not loading)
        self.overview_note.setText(
            self.load_errors.get(self.bucket_id) or (
                "正在读取存储桶…" if loading else
                f"最近同步  {self.synced_at.get(self.bucket_id, '—')}  ·  本机统计来自保留的传输记录。"
                if catalog is not None else "请先连接或刷新存储桶。"
            )
        )
        today = date.today()
        days = tuple((today - timedelta(days=offset)).isoformat() for offset in range(30))
        recent = set(days)
        counts, sizes, completed = Counter(), Counter(), Counter()
        for task in self.tasks:
            if not profile or task.get("profile_id") != self.bucket_id:
                continue
            direction = task.get("direction", "download")
            if task["status"] == "completed":
                completed[direction] += 1
            day = task.get("created_at", "")[:10]
            if direction == "upload" and day in recent:
                counts[day] += 1
                sizes[day] += task.get("size", 0)
        values = (
            f"{len(catalog.entries):,}" if catalog is not None else "—",
            size_text(catalog.total_size) if catalog is not None else "—",
            f"{completed['download']:,}" if profile else "—",
            f"{completed['upload']:,}" if profile else "—",
        )
        for widget, value in zip(self.overview_values, values):
            widget.setText(value)
        peak = max(counts.values(), default=1)
        palette = ("#F0F3FA", "#E2E7FE", "#B9C4FB", "#7A8AF1", "#4452CB")
        for column, day in enumerate(reversed(days)):
            cell = QTableWidgetItem()
            level = 0 if counts[day] == 0 else min(4, (4 * counts[day] + peak - 1) // peak)
            cell.setBackground(QColor(palette[level]))
            description = f"{day} · {counts[day]:,} 个文件 · {size_text(sizes[day])}"
            cell.setToolTip(description)
            cell.setData(Qt.ItemDataRole.AccessibleTextRole, description)
            self.upload_calendar.setItem(0, column, cell)
        for row, day in enumerate(days):
            for column, value in enumerate((day, str(counts[day]), size_text(sizes[day]))):
                self.upload_activity.setItem(row, column, QTableWidgetItem(value))
        history = list(reversed(profile.get("sync_history", [])))
        self.sync_activity.setRowCount(len(history))
        for row, record in enumerate(history):
            for column, value in enumerate(record):
                self.sync_activity.setItem(row, column, QTableWidgetItem(value))

    def build_bucket_tools(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(27, 24, 27, 17)
        layout.setSpacing(15)
        layout.addWidget(label("桶工具", "title"))
        actions = QHBoxLayout()
        self.tools_download = button("下载整桶", "download-outline", callback=lambda: self.create_download(True, root=True))
        self.tools_archive = button("增量归档", "archive-outline", callback=lambda: self.create_download(True, archive=True, root=True))
        actions.addWidget(self.tools_download)
        actions.addWidget(self.tools_archive)
        actions.addStretch()
        layout.addLayout(actions)
        layout.addWidget(label("重复文件", "subheading"))
        layout.addWidget(label("扫描会读取可能重复的对象内容并比较 SHA-256。可用路径前缀或扩展名缩小范围。", "muted", True))
        filters = QHBoxLayout()
        self.duplicate_prefix = QLineEdit()
        self.duplicate_prefix.setPlaceholderText("路径前缀（可选）")
        self.duplicate_prefix.setAccessibleName("路径前缀")
        filters.addWidget(self.duplicate_prefix, 1)
        self.duplicate_suffix = QLineEdit()
        self.duplicate_suffix.setPlaceholderText("扩展名（可选）")
        self.duplicate_suffix.setAccessibleName("扩展名")
        filters.addWidget(self.duplicate_suffix, 1)
        self.duplicate_button = button("扫描重复文件", "magnify", kind="primary", callback=self.scan_duplicates)
        self.duplicate_stop = button("停止扫描", callback=self.stop_duplicate_scan)
        filters.addWidget(self.duplicate_button)
        filters.addWidget(self.duplicate_stop)
        layout.addLayout(filters)
        options = QHBoxLayout()
        self.duplicate_summary = label("", "muted", True)
        self.duplicate_summary.setTextFormat(Qt.TextFormat.PlainText)
        options.addWidget(self.duplicate_summary, 1)
        self.duplicate_keep = Dropdown()
        self.duplicate_keep.addItems(["标记最新文件", "标记最旧文件"])
        self.duplicate_keep.setAccessibleName("重复组保留建议")
        self.duplicate_keep.currentIndexChanged.connect(self.render_duplicate_rows)
        options.addWidget(self.duplicate_keep)
        self.duplicate_export = button("导出结果", "download-outline", callback=self.export_duplicates)
        self.duplicate_export.setEnabled(False)
        options.addWidget(self.duplicate_export)
        layout.addLayout(options)
        self.duplicate_table = QTableWidget(0, 4)
        self.duplicate_table.setHorizontalHeaderLabels(["组", "对象 Key", "大小", "保留建议"])
        self.configure_report_table(self.duplicate_table)
        for index in (0, 2, 3):
            self.duplicate_table.horizontalHeader().setSectionResizeMode(index, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.duplicate_table, 1)
        pages = QHBoxLayout()
        pages.addStretch()
        self.duplicate_previous = button("上一页", callback=lambda: self.move_duplicate_page(-1))
        self.duplicate_page_label = label("1 / 1", "tiny")
        self.duplicate_next = button("下一页", callback=lambda: self.move_duplicate_page(1))
        for control in (self.duplicate_previous, self.duplicate_page_label, self.duplicate_next):
            pages.addWidget(control)
        layout.addLayout(pages)
        self.duplicate_page = 0
        self.duplicate_rows: list[tuple[int, dict, bool, bool]] = []
        self.render_duplicate_rows()
        return page

    def render_bucket_tools(self) -> None:
        catalog = self.catalogs.get(self.bucket_id)
        ready = catalog is not None and self.bucket_id in self.loaded
        busy = self.duplicate_cancel is not None
        self.tools_download.setEnabled(ready and bool(catalog.file_count))
        self.tools_archive.setEnabled(ready and bool(catalog.file_count))
        self.duplicate_button.setEnabled(ready and bool(catalog.file_count) and not busy)
        self.duplicate_stop.setVisible(busy)
        self.duplicate_stop.setEnabled(busy and not self.duplicate_cancel.is_set())
        if not (busy or self.duplicate_summary.text()):
            self.duplicate_summary.setText("从扫描开始。" if ready else "读取存储桶后可以扫描。")

    def move_duplicate_page(self, delta: int) -> None:
        self.duplicate_page += delta
        self.render_duplicate_rows()

    def render_duplicate_rows(self) -> None:
        page_size = 60
        pages = max(1, (len(self.duplicate_rows) + page_size - 1) // page_size)
        self.duplicate_page = min(max(0, self.duplicate_page), pages - 1)
        start = self.duplicate_page * page_size
        visible = self.duplicate_rows[start:start + page_size]
        self.duplicate_table.setRowCount(len(visible))
        for row, (group, entry, latest, oldest) in enumerate(visible):
            keep = latest if self.duplicate_keep.currentIndex() == 0 else oldest
            for column, value in enumerate((str(group), entry["key"], size_text(entry["size"]), "建议保留" if keep else "")):
                cell = QTableWidgetItem(value)
                cell.setToolTip(value)
                self.duplicate_table.setItem(row, column, cell)
        self.duplicate_page_label.setText(f"{self.duplicate_page + 1} / {pages}")
        self.duplicate_previous.setEnabled(self.duplicate_page > 0)
        self.duplicate_next.setEnabled(self.duplicate_page + 1 < pages)
        self.duplicate_export.setEnabled(bool(self.duplicate_rows))

    def export_duplicates(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "导出重复文件结果", "duplicate-objects.json", "JSON (*.json)")
        if not path:
            return
        keep_latest = self.duplicate_keep.currentIndex() == 0
        rows = self.duplicate_rows

        def work() -> None:
            try:
                with Path(path).open("w", encoding="utf-8") as stream:
                    json.dump([{"group": group, "key": entry["key"], "size": entry["size"], "keep": latest if keep_latest else oldest}
                               for group, entry, latest, oldest in rows], stream, ensure_ascii=False, indent=2)
            except OSError as exc:
                self.events.put({"kind": "background_error", "error": f"无法导出重复文件结果：{exc}"})
            else:
                self.events.put({"kind": "duplicates_exported"})

        threading.Thread(target=work, daemon=True, name="s3fm-duplicate-export").start()

    def stop_duplicate_scan(self) -> None:
        if self.duplicate_cancel is not None:
            self.duplicate_cancel.set()
            self.duplicate_stop.setEnabled(False)
            self.duplicate_summary.setText("正在停止扫描…")

    def reset_duplicate_scan(self) -> None:
        self.stop_duplicate_scan()
        self.duplicate_cancel = None
        self.duplicate_page = 0
        self.duplicate_rows = []
        self.duplicate_summary.clear()
        self.render_duplicate_rows()

    def scan_duplicates(self) -> None:
        catalog = self.catalogs.get(self.bucket_id)
        if self.duplicate_cancel is not None or catalog is None:
            return
        configured = self.file_operation_connection()
        if configured is None or catalog is not self.catalogs.get(self.bucket_id):
            return
        _, connection = configured
        prefix = self.duplicate_prefix.text()
        suffix = self.duplicate_suffix.text().strip().casefold()
        if suffix and not suffix.startswith("."):
            suffix = "." + suffix
        self.reset_duplicate_scan()
        self.duplicate_cancel = cancel = threading.Event()
        self.duplicate_summary.setText("正在整理可能重复的文件…")
        self.render_bucket_tools()

        def work() -> None:
            def progress(done, total):
                self.events.put({"kind": "duplicate_progress", "scan": cancel, "done": done, "total": total})

            event = {"kind": "duplicate_done", "scan": cancel}
            try:
                groups = find_duplicates(catalog, connection, prefix, suffix, cancel, progress)
                event["rows"] = [(group, entry, index == 0, index == len(entries) - 1)
                                 for group, entries in enumerate(groups, 1) for index, entry in enumerate(entries)]
                event["groups"] = len(groups)
            except Cancelled:
                event["status"] = "已停止扫描。"
            except Exception as exc:
                event["status"] = friendly_error(exc, connection)
            self.events.put(event)

        threading.Thread(target=work, daemon=True, name="s3fm-duplicate-scan").start()

    def show_queue_preview(self, expand: bool) -> None:
        self.queue_preview.setVisible(expand)
        self.queue_toggle.setIcon(icon("chevron-down" if expand else "chevron-up"))
        if expand:
            self.render_queue_preview()

    def render_queue_preview(self) -> None:
        self.last_queue_preview = time.monotonic()
        self.sync_task_index()
        selected = [self.task_by_id[task_id] for task_id in islice(self.in_flight, 3) if task_id in self.task_by_id]
        if not selected:
            selected = self.tasks[-3:][::-1]
        self.queue_state.setText(f"传输队列 · 共 {len(self.tasks):,} 个任务" if self.tasks else "暂无传输任务")
        for index, row in enumerate(self.queue_preview_rows):
            row.setVisible(index < len(selected))
            if index < len(selected):
                task = selected[index]
                direction = TASK_DIRECTIONS[task.get("direction", "download")]
                state = TASK_FILTERS.get(TASK_CATEGORIES.get(task["status"], task["status"]), "")
                row.setText(f"{direction} · {task.get('name') or task['key']} · {state} · {size_text(task.get('transferred', 0))} / {size_text(task.get('size', 0))}")

    def build_tasks(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 25, 28, 12)
        layout.setSpacing(18)
        heading = QHBoxLayout()
        heading.addWidget(label("传输任务", "title"), 1)
        self.clear_completed_button = button("清除已完成", callback=self.clear_completed)
        heading.addWidget(self.clear_completed_button)
        heading.addWidget(button("清空全部记录", callback=self.clear_tasks))
        layout.addLayout(heading)

        batch_panel = QVBoxLayout()
        batch_panel.setSpacing(10)
        scope = QHBoxLayout()
        batch_label = label("传输批次", "muted")
        self.batch_selector = Dropdown()
        self.batch_selector.setSizeAdjustPolicy(Dropdown.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.batch_selector.setMinimumContentsLength(18)
        self.batch_selector.setMaximumWidth(390)
        self.batch_selector.setAccessibleName("传输批次")
        self.batch_selector.currentIndexChanged.connect(self.select_task_batch)
        batch_label.setBuddy(self.batch_selector)
        scope.addWidget(batch_label)
        scope.addWidget(self.batch_selector, 1)
        scope.addStretch()
        self.batch_output = button("打开保存位置", "folder-open-outline", callback=self.open_batch_output)
        self.batch_export = button("导出失败清单", "download-outline", callback=self.export_failed_tasks)
        scope.addWidget(self.batch_output)
        scope.addWidget(self.batch_export)
        batch_panel.addLayout(scope)

        actions = QHBoxLayout()
        self.batch_progress = QProgressBar()
        self.batch_progress.setRange(0, 1000)
        self.batch_progress.setTextVisible(False)
        self.batch_progress.setAccessibleName("所选批次传输进度")
        self.batch_progress_text = label("", "tiny")
        actions.addWidget(self.batch_progress, 1)
        actions.addWidget(self.batch_progress_text)
        self.batch_pause = button("全部暂停", "pause", callback=self.pause_all)
        self.batch_resume = button("全部继续", "play-outline", callback=self.resume_all)
        self.batch_retry = button("重试失败", "refresh", callback=self.retry_failed)
        for control in (self.batch_pause, self.batch_resume, self.batch_retry):
            actions.addWidget(control)
        batch_panel.addLayout(actions)
        layout.addLayout(batch_panel)
        self.task_filters = QHBoxLayout()
        self.task_filters.setSpacing(4)
        self.filter_buttons: dict[str, QPushButton] = {}
        for state, name in TASK_FILTERS.items():
            control = button(name, kind="filter", callback=lambda checked=False, state=state: self.set_task_filter(state))
            control.setCheckable(True)
            self.filter_buttons[state] = control
            self.task_filters.addWidget(control)
        self.task_filters.addStretch()
        self.direction_picker = Dropdown()
        self.direction_picker.setAccessibleName("传输方向")
        self.direction_picker.addItem("全部方向", None)
        for direction, caption in TASK_DIRECTIONS.items():
            self.direction_picker.addItem(caption, direction)
        self.direction_picker.currentIndexChanged.connect(self.select_task_direction)
        self.task_filters.addWidget(self.direction_picker)
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
        footer.addWidget(label("任务记录保存在本机。清理记录会保留本地文件和远端对象。", "tiny"), 1)
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
        if self._closing_saved or self._close_revision is not None:
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
        for profile in self.ordered_profiles():
            card = BucketCard({**profile, "synced_at": self.synced_at.get(profile["id"], "")}, profile["id"] == self.bucket_id)
            card.activated.connect(self.change_bucket)
            card.edited.connect(self.connection_menu)
            self.bucket_layout.addWidget(card)

    def ordered_profiles(self) -> list[dict[str, Any]]:
        order = self.preferences.get("bucket_order")
        if order == "name":
            return sorted(self.profiles, key=lambda profile: profile["name"].casefold())
        if order == "provider":
            return sorted(self.profiles, key=lambda profile: (profile.get("provider", "r2"), profile["name"].casefold()))
        return self.profiles

    def connection_menu(self, profile_id: str) -> None:
        menu = QMenu(self)
        edit = menu.addAction("编辑连接")
        remove = menu.addAction("移除此连接")
        chosen = menu.exec(self.cursor().pos())
        menu.deleteLater()
        if chosen == edit:
            self.edit_connection(profile_id)
        elif chosen == remove:
            self.remove_connection(profile_id)

    def add_connection(self) -> None:
        dialog = ConnectionDialog(QApplication.activeModalWidget() or self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            dialog.deleteLater()
            return
        profile = dialog.values()
        dialog.deleteLater()
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
        dialog = ConnectionDialog(QApplication.activeModalWidget() or self, target)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            dialog.deleteLater()
            return
        old = copy.deepcopy(target)
        target.update(dialog.values())
        dialog.deleteLater()
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
            QMessageBox.information(self, "连接仍在使用", "此连接还有未结束的传输任务，请先取消任务后再移除。")
            return
        if QMessageBox.question(QApplication.activeModalWidget() or self, "移除本机连接", "移除此连接及本机保存的密钥？远端存储桶和文件将保留。") != QMessageBox.StandardButton.Yes:
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
        self.synced_at.pop(profile_id, None)
        self.bucket_id = self.profiles[0]["id"] if self.profiles else ""
        for task in self.tasks:
            if task.get("profile_id") == profile_id:
                self.task_connections.pop(task["id"], None)
        self.change_bucket(self.bucket_id)
        self.toast("本机连接已移除。")

    def change_bucket(self, profile_id: str) -> None:
        page = self.pages.currentIndex()
        if page == 1:
            page = 0
        self.reset_duplicate_scan()
        self.reset_directory_pages()
        self.search_timer.stop()
        self.bucket_id = profile_id
        self.prefix = ""
        self.page_index = 0
        self.selected.clear()
        self.file_filters = FileFilters()
        self._search_result = None
        self.cancel_file_search()
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.switch_page(page)
        self.render_buckets()
        self.render_files()
        if profile_id and profile_id not in self.loaded and profile_id not in self.loading and self.current_profile().get("secret_access_key"):
            self.refresh_files()

    def switch_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        self.navigation.select((self.files_nav, self.tasks_nav, self.upload_nav, self.overview_nav, self.tools_nav)[index])
        if index == 1:
            self.render_tasks()
        elif index == UPLOAD_PAGE:
            self.render_upload()
        elif index == OVERVIEW_PAGE:
            self.refresh_after_remote_write()
            self.render_overview()
        elif index == TOOLS_PAGE:
            self.refresh_after_remote_write()
            self.render_bucket_tools()
        else:
            self.refresh_after_remote_write()
            self.update_selection()

    def refresh_after_remote_write(self) -> None:
        if self.closing or self.bucket_id not in self._file_refresh_profiles or self.pages.currentIndex() not in {0, OVERVIEW_PAGE, TOOLS_PAGE}:
            return
        if any(task.get("direction") in {"upload", "copy"} and task.get("profile_id") == self.bucket_id
               and task["status"] in ACTIVE for task in self.tasks):
            return
        scan = self.loading.pop(self.bucket_id, None)
        if scan:
            scan.set()
        self.refresh_files()

    def empty_clicked(self) -> None:
        if not self.current_profile():
            self.add_connection()
        elif self.bucket_id in self.loaded and self.file_view().is_search():
            self.navigate(self.prefix)
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
        self.reset_duplicate_scan()
        token = uuid.uuid4().hex
        self._file_refresh_profiles.discard(profile_id)
        self.scan_tokens[profile_id] = token
        self.loading[profile_id] = cancel
        self.load_errors.pop(profile_id, None)
        self.loaded.discard(profile_id)
        self.files.pop(profile_id, None)
        self.catalogs.pop(profile_id, None)
        self.catalog_sources.pop(profile_id, None)
        self._search_result = None
        self.cancel_file_search()
        self.selected.clear()
        self.page_index = 0
        self.reset_directory_pages()
        self.render_files()
        if self.pages.currentIndex() == OVERVIEW_PAGE:
            self.render_overview()
        elif self.pages.currentIndex() == TOOLS_PAGE:
            self.render_bucket_tools()

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
        self.cancel_file_search()
        self.page_index = 0
        self.selected.clear()
        self.set_file_actions_pending()
        self.search_timer.start(250)

    def file_view(self) -> FileView:
        return FileView(
            self.prefix, self.search.text().strip().casefold(),
            bool(self.search_scope.currentData()), self.file_filters,
            self.sort_column, self.sort_descending,
        )

    def file_view_changed(self, *_: Any) -> None:
        self.cancel_file_search()
        self.search_timer.stop()
        self.page_index = 0
        self.selected.clear()
        self.render_files()

    def show_file_filters(self) -> None:
        dialog = FileFilterDialog(self, self.file_filters)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.filters != self.file_filters:
            self.file_filters = dialog.filters
            self.file_view_changed()
        dialog.deleteLater()

    def sort_files(self, column: int) -> None:
        if self.bucket_id not in self.loaded or self.bucket_id in self.loading:
            return
        self.cancel_file_search()
        self.sort_descending = column == self.sort_column and not self.sort_descending
        self.sort_column = column
        self.search_timer.stop()
        self.page_index = 0
        self.render_files()

    def show_file_details(self) -> None:
        if not self.details_button.isEnabled() or len(self.selected) != 1:
            return
        key = next(iter(self.selected))
        entry = next((row for row in self.visible_entries if row["key"] == key), None)
        if entry is None or entry["kind"] != "file":
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("文件详情")
        dialog.setMinimumWidth(512)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        layout.addLayout(form)
        suffix = PurePosixPath(entry["name"]).suffix
        kind = file_style(entry)[2]
        for title, value in (
            ("存储桶", self.current_profile().get("bucket", "")),
            ("对象 Key", key),
            ("大小", f"{size_text(entry['size'])}（{entry['size']:,} 字节）"),
            ("类型", f"{kind}（{suffix}）" if suffix else kind),
            ("修改时间", entry["modified"] or "—"),
            ("ETag", entry.get("etag") or "—"),
        ):
            field = QLineEdit(value)
            field.setReadOnly(True)
            form.addRow(title, field)
        actions = QHBoxLayout()
        copy_key = button("复制对象 Key", callback=lambda: QApplication.clipboard().setText(key))
        copy_key.clicked.connect(lambda: copy_key.setText("已复制"))
        actions.addWidget(copy_key)
        actions.addStretch()
        actions.addWidget(button("关闭", callback=dialog.accept))
        layout.addLayout(actions)
        dialog.exec()
        dialog.deleteLater()

    def show_file_actions(self) -> None:
        profile = self.current_profile()
        if not profile or not self.file_actions.isEnabled():
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("更多操作")
        dialog.setMinimumWidth(440)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 20, 24, 22)
        layout.setSpacing(10)
        layout.addWidget(label("更多操作", "title"))
        scope = f"已选 {len(self.selected):,} 项" if self.selected else ("当前目录" if self.prefix else "整个存储桶")
        layout.addWidget(label(scope, "muted"))
        selected_action = None

        def add_action(title, glyph, action, available, help_text):
            def choose() -> None:
                nonlocal selected_action
                selected_action = action
                dialog.accept()

            control = button(title, glyph, callback=choose)
            control.setObjectName("fileAction")
            control.setAutoDefault(False)
            control.setEnabled(available)
            layout.addWidget(control)
            if not available:
                layout.addWidget(label(help_text, "helper", True))

        archive_ready = self.bucket_id in self.loaded and (
            self.download_button.isEnabled() if self.selected else self.download_all_button.isEnabled()
        )
        add_action("增量归档", "archive-outline", lambda: self.create_download(not self.selected, archive=True),
                   archive_ready, "等待文件读取完成，或选择要归档的文件或文件夹。")
        if profile.get("public_url"):
            add_action("复制公开链接", "link-variant", self.copy_public_link,
                       self.details_button.isEnabled(), "请在当前页选中一个文件。")
        if profile.get("provider", "r2") == "r2":
            files_selected = bool(self.selected) and all(not key.endswith("/") for key in self.selected)
            native_copy = supports_guarded_copy(connection_of(profile))
            copy_help = "此操作需要 Cloudflare R2 原生 S3 Endpoint。"
            add_action("复制到桶内目录", "content-copy", self.create_copy,
                       native_copy and files_selected, "请选择文件。" if native_copy else copy_help)
            add_action("复制为新名称", "file-edit-outline", lambda: self.create_copy(new_name=True),
                       native_copy and files_selected and len(self.selected) == 1,
                       "请选择一个文件。" if native_copy else copy_help)
            add_action("生成限时下载链接", "link-variant", self.show_download_link,
                       self.details_button.isEnabled(), "请在当前页选中一个文件。")
        layout.addWidget(button("关闭", callback=dialog.reject), alignment=Qt.AlignmentFlag.AlignRight)
        result = dialog.exec()
        dialog.deleteLater()
        if result == QDialog.DialogCode.Accepted and selected_action is not None:
            selected_action()

    def copy_public_link(self) -> None:
        if not self.details_button.isEnabled() or len(self.selected) != 1:
            return
        try:
            link = public_object_url(self.current_profile().get("public_url", ""), next(iter(self.selected)))
        except ValueError as exc:
            self.toast(str(exc))
            return
        QApplication.clipboard().setText(link)
        self.toast("已复制公开链接。")

    def file_operation_connection(self) -> tuple[dict[str, Any], Connection] | None:
        profile = self.current_profile()
        if not profile:
            return None
        original = connection_of(profile)
        if not profile.get("secret_access_key"):
            self.edit_connection(profile["id"])
            profile = self.current_profile()
            updated = connection_of(profile)
            if not profile.get("secret_access_key") or connection_fingerprint(original) != connection_fingerprint(updated):
                return None
        connection = connection_of(profile)
        try:
            validate_connection(connection)
        except ValueError as exc:
            QMessageBox.warning(self, "请检查连接", str(exc))
            return None
        return profile, connection

    def show_download_link(self) -> None:
        if not self.details_button.isEnabled() or len(self.selected) != 1:
            return
        key = next(iter(self.selected))
        configured = self.file_operation_connection()
        if configured is None:
            return
        _, connection = configured
        dialog = QDialog(self)
        dialog.setWindowTitle("限时下载链接")
        dialog.setMinimumWidth(570)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 20, 24, 22)
        layout.addWidget(label("限时下载链接", "title"))
        form = QFormLayout()
        layout.addLayout(form)
        source = QLineEdit(key)
        source.setReadOnly(True)
        form.addRow("对象 Key", source)
        duration = Dropdown()
        for title, seconds in LINK_DURATIONS:
            duration.addItem(title, seconds)
        duration.setCurrentIndex(max(0, duration.findData(self.preferences.get("share_duration", 3600))))
        form.addRow("有效期", duration)
        url = QLineEdit()
        url.setReadOnly(True)
        url.setPlaceholderText("生成后可复制链接")
        form.addRow("下载链接", url)
        note = label("有效期内，持有链接的人可以下载该文件。", "helper", True)
        note.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(note)

        def generate() -> None:
            seconds = duration.currentData()
            try:
                signed = generate_download_link(connection, key, seconds)
            except Exception as exc:
                note.setText(friendly_error(exc, connection))
                return
            url.setText(signed)
            copy.setEnabled(True)
            expires = datetime.now().astimezone() + timedelta(seconds=seconds)
            note.setText(f"到期时间 {expires:%Y-%m-%d %H:%M:%S %z}")

        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("关闭", callback=dialog.accept))
        copy = button("复制链接", callback=lambda: QApplication.clipboard().setText(url.text()))
        copy.setEnabled(False)
        def reset_link() -> None:
            url.clear()
            copy.setEnabled(False)
            note.setText("有效期内，持有链接的人可以下载该文件。")
        duration.currentIndexChanged.connect(reset_link)
        actions.addWidget(copy)
        actions.addWidget(button("生成链接", "link-variant", kind="primary", callback=generate))
        layout.addLayout(actions)
        dialog.exec()
        dialog.deleteLater()

    def create_copy(self, new_name: bool = False) -> None:
        if self.closing or self.preparing or self.clear_when_idle:
            return
        keys = self.selected.copy()
        if not keys or any(key.endswith("/") for key in keys) or (new_name and len(keys) != 1):
            return
        catalog = self.catalogs.get(self.bucket_id)
        entries = (catalog.selected_files(keys) if self.bucket_id in self.loaded and catalog else
                   [row for row in self.visible_entries if row["kind"] == "file" and row["key"] in keys])
        if len(entries) != len(keys):
            return
        source_prefix = "" if self.file_view().is_search() and self.file_view().search_bucket else self.prefix
        configured = self.file_operation_connection()
        if configured is None:
            return
        profile, connection = configured
        if not supports_guarded_copy(connection):
            return
        first = entries[0]["key"]
        source_parent, _, source_name = first.rpartition("/")
        dialog = QDialog(self)
        title = "复制为新名称" if new_name else "复制到桶内目录"
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(570)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 20, 24, 22)
        layout.addWidget(label(title, "title"))
        form = QFormLayout()
        layout.addLayout(form)
        target = QLineEdit(source_name if new_name else "")
        target.setPlaceholderText("例如 backup/，留空表示桶根目录")
        form.addRow("新文件名" if new_name else "目标目录", target)
        preview = QPlainTextEdit()
        preview.setReadOnly(True)
        preview.setMaximumHeight(154)
        layout.addWidget(preview)
        note = label(f"目标存储桶 {connection.bucket} · {len(entries):,} 个文件。源对象和已有同名对象会保留。", "helper", True)
        note.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(note)
        planned = []

        def target_key(key: str) -> str:
            value = target.text().strip()
            if new_name:
                return f"{source_parent}/{value}" if source_parent else value
            destination = value.rstrip("/") + "/" if value else ""
            return destination + key.removeprefix(source_prefix)

        def show_preview() -> None:
            lines = [f"{row['key']}  →  {target_key(row['key'])}" for row in entries[:6]]
            if len(entries) > 6:
                lines.append(f"另有 {len(entries) - 6:,} 项")
            preview.setPlainText("\n".join(lines))

        def confirm() -> None:
            value = target.text().strip()
            if (new_name and (not value or "/" in value)) or "\\" in value or value.startswith("/") or any(
                part in {".", ".."} for part in value.rstrip("/").split("/")
            ):
                note.setText("请填写有效的新名称或桶内目录。")
                return
            pairs = [(row, target_key(row["key"])) for row in entries]
            if (any(not key or key == row["key"] or len(key.encode("utf-8")) > 1024 or not strong_etag(row.get("etag"))
                    for row, key in pairs) or len({key for _, key in pairs}) != len(pairs)):
                note.setText("请确认目标与源路径不同，且目标路径没有冲突。源对象也需要有效的 ETag，可先刷新列表。")
                return
            planned.extend(pairs)
            dialog.accept()

        target.textChanged.connect(show_preview)
        show_preview()
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("取消", callback=dialog.reject))
        actions.addWidget(button("开始复制", "content-copy", kind="primary", callback=confirm))
        layout.addLayout(actions)
        result = dialog.exec()
        dialog.deleteLater()
        if result != QDialog.DialogCode.Accepted or not planned or self.closing or self.clear_when_idle:
            return
        settings = {"batch_id": uuid.uuid4().hex, "direction": "copy", "bucket": connection.bucket,
                    "profile_id": profile["id"], "connection_fingerprint": connection_fingerprint(connection),
                    "workers": 4, "status": "queued", "transferred": 0, "error": "",
                    "created_at": datetime.now().astimezone().isoformat()}

        def materialize() -> list[dict[str, Any]]:
            return [{**settings, "id": uuid.uuid4().hex, "key": key, "name": key.rsplit("/", 1)[-1],
                     "source_key": row["key"], "size": row["size"], "etag": row["etag"]} for row, key in planned]

        self.prepare_tasks(len(planned), materialize, connection)

    def set_file_actions_pending(self) -> None:
        self.files_stack.setEnabled(False)
        for action in (self.download_button, self.download_all_button, self.select_button,
                       self.details_button, self.file_actions, self.file_previous, self.file_next):
            action.setEnabled(False)
        self.selection_note.setText("正在更新文件列表…")

    def cancel_file_search(self) -> None:
        if self._search_cancel is not None:
            self._search_cancel.set()
            self._search_cancel = None
        self._search_pending = None
        self._query_token = ""

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

    def request_search(self, catalog: CatalogIndex, view: FileView) -> None:
        key = (self.bucket_id, view, catalog)
        self.set_file_actions_pending()
        if self._search_pending == key:
            return
        self.cancel_file_search()
        cancel = threading.Event()
        self._search_cancel = cancel
        self._search_pending = key
        token = uuid.uuid4().hex
        self._query_token = token

        def search() -> None:
            event = {"kind": "search_ready", "key": key, "token": token}
            try:
                event["rows"] = view.rows(catalog, cancel)
            except Cancelled:
                return  # The catalog was superseded or the window is closing.
            except Exception:
                event["error"] = "无法整理文件列表，请清除筛选或刷新后重试。"
            self.events.put(event)
        threading.Thread(target=search, daemon=True, name="r2-file-search").start()

    def navigate(self, prefix: str) -> None:
        self.reset_directory_pages()
        self.cancel_file_search()
        self.search_timer.stop()
        self.prefix = prefix
        self.page_index = 0
        self.selected.clear()
        self.file_filters = FileFilters()
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.render_files()

    def render_files(self) -> None:
        profile = self.current_profile()
        self.upload_button.setEnabled(bool(profile) and profile.get("provider", "r2") == "r2")
        self.page_title.setText(profile.get("name", "文件"))
        entries = self.files.get(self.bucket_id, [])
        view = self.file_view()
        results_view = view.is_search()
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
                self.set_file_actions_pending()
                self.request_catalog(entries)
                return
        if not loading:
            cache_key = (self.bucket_id, view, catalog)
            if cache_key != self._file_cache_key:
                base_rows = catalog.children(self.prefix)
                large_view = (len(entries) if results_view else len(base_rows)) > 5000
                if large_view and (results_view or view.sort_column >= 0):
                    if self._search_result is None or self._search_result[0] != cache_key:
                        self.request_search(catalog, view)
                        return
                    self.all_entries = self._search_result[1]
                else:
                    self.all_entries = view.rows(catalog)
                self._file_cache_key = cache_key
            pages = max(1, (len(self.all_entries) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
            self.page_index = min(self.page_index, pages - 1)
            self.visible_entries = self.all_entries[self.page_index * self.PAGE_SIZE:(self.page_index + 1) * self.PAGE_SIZE]
            has_next = self.page_index + 1 < pages
        self.clear_layout(self.breadcrumbs)
        self.breadcrumbs.addWidget(button("全部文件", kind="ghost", callback=lambda: self.navigate("")))
        if self.prefix and (not results_view or not view.search_bucket):
            parts = self.prefix.removesuffix("/").split("/")
            for index, part in enumerate(parts):
                self.breadcrumbs.addWidget(label("/", "tiny"))
                path = "/".join(parts[:index + 1]) + "/"
                self.breadcrumbs.addWidget(button(part or "/", kind="ghost", callback=lambda checked=False, path=path: self.navigate(path)))
        if results_view and not loading:
            self.breadcrumbs.addWidget(label("/ 全桶结果" if view.search_bucket else "/ 目录结果", "muted"))
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
            glyph, color, _ = file_style(entry)
            item = QTableWidgetItem(icon(glyph, color), entry["name"])
            item.setToolTip(entry["key"])
            self.table.setItem(row, 0, item)
            display_size = "—" if loading and entry["kind"] == "folder" else size_text(entry.get("size", 0))
            self.table.setItem(row, 1, QTableWidgetItem(display_size))
            extension = PurePosixPath(entry["name"]).suffix or "—"
            self.table.setItem(row, 2, QTableWidgetItem("文件夹" if entry["kind"] == "folder" else extension))
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
        self.files_stack.setEnabled(True)
        header = self.table.horizontalHeader()
        header.setSectionsClickable(ready)
        header.setSortIndicatorShown(ready and self.sort_column >= 0)
        if ready and self.sort_column >= 0:
            header.setSortIndicator(self.sort_column, Qt.SortOrder.DescendingOrder if self.sort_descending else Qt.SortOrder.AscendingOrder)
        self.search.setEnabled(ready)
        self.search_scope.setEnabled(ready)
        self.filter_button.setEnabled(ready)
        self.filter_button.setText("筛选 · 已启用" if self.file_filters.active() else "筛选")
        self.search.setToolTip("后台正在建立全桶索引，完成后可搜索" if loading else "")
        self.refresh_button.setEnabled(bool(profile) and not loading)
        self.select_button.setEnabled((ready or loading) and bool(self.selectable_page_keys()))
        whole_scope = "目录" if self.prefix else "整桶"
        self.download_all_button.setText(f"下载{whole_scope}")
        self.download_all_button.setAccessibleName(f"下载{whole_scope}")
        self.download_all_button.setEnabled(ready and not results_view and bool(catalog.children(self.prefix)))
        scope_help = "下载当前目录及其子目录中的全部文件" if self.prefix else "下载此存储桶中的全部文件"
        self.download_all_button.setToolTip(f"后台索引完成后可下载{whole_scope}" if loading else
                                            "搜索或筛选时请选中需要下载的文件" if results_view else scope_help)
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
                title, detail, action = ("没有找到匹配文件", "请调整搜索词或筛选条件。", "清除搜索和筛选") if results_view else ("此目录为空", "当前目录没有可显示的文件或文件夹。", "刷新文件列表")
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
        if not self.files_stack.isEnabled():
            return
        count = len(self.selected)
        loading = self.bucket_id in self.loading
        listing = "全桶索引中" if loading else f"共 {len(self.all_entries):,} 项"
        self.selection_note.setText(f"{listing} · 本页 {len(self.visible_entries):,} 项" + (f" · 已选 {count:,} 项" if count else ""))
        self.download_button.setText(f"下载所选 ({count})" if count else "下载所选")
        page_keys = self.selectable_page_keys()
        self.download_button.setEnabled(bool(count) and (self.bucket_id in self.loaded or (loading and self.selected.issubset(page_keys))))
        self.details_button.setEnabled(count == 1 and any(
            entry["key"] in self.selected and entry["kind"] == "file" for entry in self.visible_entries
        ))
        self.file_actions.setEnabled(bool(self.bucket_id) and (
            self.download_button.isEnabled() or self.download_all_button.isEnabled() or self.details_button.isEnabled()
        ))
        self.select_button.setText("取消本页选择" if page_keys and page_keys.issubset(self.selected) else "全选本页")
        if self.pages.currentIndex() == 0:
            profile = self.current_profile()
            detail = f" · 已选 {count:,} 项" if count else ""
            if count and not loading:
                detail += "（文件夹包含子目录）"
            self.status_label.setText(profile.get("bucket", "") + detail if profile else "添加连接后即可浏览和下载文件。")

    def toggle_select_all(self) -> None:
        if self.pages.currentIndex() != 0 or not self.select_button.isEnabled():
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

    def upload_prefix(self) -> str:
        view = self.file_view()
        return "" if view.is_search() and view.search_bucket else self.prefix

    def render_upload(self) -> None:
        profile = self.current_profile()
        available = bool(profile) and profile.get("provider", "r2") == "r2"
        self.upload_drop.setAcceptDrops(available)
        self.upload_choose_files.setVisible(available)
        self.upload_choose_folder.setVisible(available)
        self.upload_add_url.setVisible(available)
        self.upload_paste.setVisible(available)
        self.upload_connect.setVisible(not available)
        self.upload_title.setText("拖入文件或文件夹" if available else "选择一个 Cloudflare R2 存储桶")
        if available:
            prefix = self.upload_prefix()
            self.upload_target.setText(f"目标位置  r2://{profile['bucket']}/{prefix}")
            self.upload_help.setText("上传前可确认目标路径和同名对象的处理方式。")
        else:
            self.upload_target.setText("上传适用于 Cloudflare R2。")
            self.upload_help.setText("从左侧切换到 R2 存储桶，或添加新的连接。")

    def paste_upload(self) -> None:
        mime = QApplication.clipboard().mimeData()
        if mime is None:
            return
        urls = mime.urls()
        if urls and all(url.isLocalFile() for url in urls):
            self.create_upload(tuple(Path(url.toLocalFile()) for url in urls))
        elif mime.hasText():
            content = mime.text()
            if content.lstrip().startswith("https://"):
                self.import_upload_urls(content)
                return
            path, _ = QFileDialog.getSaveFileName(self, "将剪贴板文本保存为上传文件", "clipboard.md", "Markdown (*.md);;文本 (*.txt);;所有文件 (*)")
            if path:
                try:
                    Path(path).write_text(content, encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    QMessageBox.warning(self, "无法保存剪贴板内容", str(exc))
                    return
                self.create_upload((Path(path),))
        else:
            self.toast("剪贴板里没有文件、网址或文本。")

    def import_upload_urls(self, initial: str = "") -> None:
        dialog = UrlImportDialog(self)
        dialog.setWindowTitle("从 HTTPS 网址导入")
        dialog.setMinimumWidth(550)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label("从 HTTPS 网址导入", "title"))
        layout.addWidget(label("每行一个网址。文件先保存到你选择的本地目录，再进入上传确认。单批总大小最多 512 MiB。", "muted", True))
        addresses = QPlainTextEdit(initial if isinstance(initial, str) else "")
        addresses.setAccessibleName("待导入的网址")
        layout.addWidget(addresses)
        status = label("", "muted", True)
        status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(status)
        cancel = threading.Event()
        events: queue.Queue = queue.Queue()
        paths = ()
        pending = False

        def start() -> None:
            nonlocal pending
            try:
                urls = urls_from_text(addresses.toPlainText())
            except ValueError as exc:
                status.setText(str(exc))
                return
            directory = QFileDialog.getExistingDirectory(dialog, "选择导入文件的保存目录")
            if not directory:
                return
            addresses.setEnabled(False)
            submit.setEnabled(False)
            status.setText("正在导入文件…")
            pending = True

            def work() -> None:
                try:
                    sources = save_url_sources(urls, Path(directory), cancel, lambda current, total: events.put(("progress", f"正在导入第 {current} / {total} 个文件…")))
                    events.put(("done", sources))
                except Cancelled:
                    events.put(("error", "导入已取消。"))
                except Exception as exc:
                    events.put(("error", str(exc)))

            threading.Thread(target=work, daemon=True, name="s3fm-url-import").start()

        def poll() -> None:
            nonlocal paths, pending
            while not events.empty():
                kind, payload = events.get_nowait()
                if kind == "progress":
                    if not cancel.is_set():
                        status.setText(payload)
                    continue
                pending = False
                if kind == "done" and not cancel.is_set():
                    paths = payload
                    dialog.accept()
                else:
                    if kind == "done":
                        for path in payload:
                            try:
                                path.unlink(missing_ok=True)
                            except OSError:
                                pass
                    status.setText("导入已取消。" if cancel.is_set() else payload)
                    cancel.clear()
                    addresses.setEnabled(True)
                    submit.setEnabled(True)

        def cancel_import() -> bool:
            if pending:
                cancel.set()
                status.setText("正在停止导入…")
            return pending

        dialog.cancel_request = cancel_import
        timer = QTimer(dialog)
        timer.timeout.connect(poll)
        timer.start(80)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("关闭", callback=dialog.reject))
        submit = button("选择目录并导入", kind="primary", callback=start)
        actions.addWidget(submit)
        layout.addLayout(actions)
        if dialog.exec() == QDialog.DialogCode.Accepted and paths:
            self.create_upload(paths)
        dialog.deleteLater()

    def create_upload(self, sources: tuple[Path, ...] = (), *, choose_folder: bool | None = None) -> None:
        if self.clear_when_idle or self.closing or self.preparing:
            self.toast("正在处理任务，请稍候再创建上传。")
            return
        profile = self.current_profile()
        if not profile or profile.get("provider", "r2") != "r2":
            return
        if not profile.get("secret_access_key"):
            self.edit_connection(profile["id"])
            profile = self.current_profile()
            if not profile.get("secret_access_key"):
                return
        connection = connection_of(profile)
        try:
            validate_connection(connection)
        except ValueError as exc:
            QMessageBox.warning(self, "请检查连接", str(exc))
            return
        if choose_folder is not None:
            sources = choose_upload_sources(self, folder=choose_folder)
            if not sources:
                return
        dialog = UploadDialog(self, connection, self.upload_prefix())
        if sources:
            dialog.check_sources(sources)
        result = dialog.exec()
        items = dialog.selected_items
        dialog.deleteLater()
        if result != QDialog.DialogCode.Accepted or not items or self.closing or self.clear_when_idle:
            return
        settings = {
            "batch_id": uuid.uuid4().hex, "direction": "upload", "bucket": connection.bucket,
            "profile_id": profile["id"], "connection_fingerprint": connection_fingerprint(connection),
            "workers": self.preferences.get("upload_workers", min(4, self.preferences.get("workers", 4))), "status": "queued", "transferred": 0,
            "created_at": datetime.now().astimezone().isoformat(),
        }

        def materialize() -> list[dict[str, Any]]:
            return [{**settings, "id": uuid.uuid4().hex, "name": item.source.name, "path": str(item.source),
                     "key": item.key, "size": item.size, "source_modified_ns": item.modified_ns,
                     "etag": item.remote_etag, "error": ""} for item in items]

        self.prepare_tasks(len(items), materialize, connection)

    def create_download(self, whole_directory: bool = False, *, archive: bool = False, root: bool = False) -> None:
        if self.clear_when_idle or self.closing or self.preparing:
            self.toast("正在取消和清理任务，请稍候再创建下载。")
            return
        profile = self.current_profile()
        if not profile:
            return
        view = self.file_view()
        if not root and (not self.files_stack.isEnabled() or (whole_directory and view.is_search())):
            return
        prefix = "" if root else self.prefix
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
        download_prefix = "" if view.is_search() and view.search_bucket else prefix
        scope = (f"下载目录 · {prefix}" if prefix else "下载整桶") if whole_directory else "下载所选文件和文件夹"
        if direct_files:
            scope = "下载本页所选文件"

        def select_entries(cancel: threading.Event) -> list[dict[str, Any]]:
            if archive:
                fresh = CatalogIndex(list_objects(connection, download_prefix, cancel), cancel)
                return fresh.files_under(download_prefix, cancel=cancel) if whole_directory else fresh.selected_files(keys, cancel=cancel)
            if direct_files or catalog is None:
                return direct_files
            return (catalog.files_under(prefix, cancel=cancel) if whole_directory
                    else catalog.selected_files(keys, cancel=cancel))

        if archive:
            scope = scope.replace("下载", "归档", 1)
        options = {"archive_target": connection_fingerprint(connection)} if archive else {}
        dialog = DownloadDialog(self, self.preferences, download_prefix, select_entries=select_entries,
                                scope=f"{scope}\n存储桶 {connection.bucket}", **options)
        result = dialog.exec()
        dialog.deleteLater()
        if result != QDialog.DialogCode.Accepted or self.closing:
            return
        selected = dialog.archive_entries if archive else dialog.entries
        if dialog.preflight is None or not selected:
            return
        output = str(dialog.preflight.output)
        self.preferences.update(output_dir=output, workers=dialog.workers.currentData())
        if not archive:
            self.preferences["skip_existing"] = dialog.skip.isChecked()
        # Materialize large download selections away from the GUI thread.
        settings = {
            "batch_id": uuid.uuid4().hex,
            "bucket": profile["bucket"],
            "profile_id": profile["id"],
            "connection_fingerprint": connection_fingerprint(connection),
            "output_dir": output,
            "workers": dialog.workers.currentData(),
            "overwrite": not dialog.skip.isChecked(),
            "strip_prefix": bool(download_prefix) and dialog.strip.isChecked(),
            "prefix": download_prefix,
            "created_at": datetime.now().astimezone().isoformat(),
        }
        def materialize() -> list[dict[str, Any]]:
            tasks = []
            for entry in selected:
                task = {**settings, "id": uuid.uuid4().hex, "key": entry["key"], "name": entry["key"].split("/")[-1],
                        "size": entry["size"], "etag": entry.get("etag", ""), "status": "queued", "transferred": 0, "error": ""}
                if archive:
                    task.update(archive=True, archive_stamp=entry["archive_stamp"], overwrite=entry["archive_stamp"] != "missing")
                tasks.append(task)
            return tasks

        self.prepare_tasks(len(selected), materialize, connection)

    def prepare_tasks(self, count: int, materialize: Callable[[], list[dict[str, Any]]], connection: Connection) -> None:
        if count > 500:
            self.preparing = True
            self._prepare_token = uuid.uuid4().hex
            token = self._prepare_token
            self.activity.setText("正在创建传输任务…")

            def prepare() -> None:
                event = {"kind": "tasks_ready", "token": token, "connection": connection}
                try:
                    event["tasks"] = materialize()
                except Exception as exc:
                    event["error"] = friendly_error(exc, connection)
                self.events.put(event)

            threading.Thread(target=prepare, daemon=True, name="r2-task-prepare").start()
            return
        try:
            tasks = materialize()
        except Exception as exc:
            QMessageBox.warning(self, "无法创建任务", friendly_error(exc, connection))
            return
        self.enqueue_tasks(tasks, connection)

    def enqueue_tasks(self, created: list[dict[str, Any]], connection: Connection) -> None:
        self.tasks.extend(created)
        for task in created:
            self.task_connections[task["id"]] = connection
        self._task_structure_dirty = True
        self.dispatch_needed = True
        if not self.persist():
            for task in created:
                if is_multipart(task):
                    task.update(status="failed", error="任务记录保存失败。请检查本机存储后重试。")
        self.task_filter = "all"
        self.task_batch = created[0].get("batch_id") if created else None
        self.task_direction = None
        with QSignalBlocker(self.direction_picker):
            self.direction_picker.setCurrentIndex(0)
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
        direction = first.get("direction", "download")
        upload = direction == "upload"
        for task in waiting:
            if (self.task_connections.get(task["id"]) == connection
                    and task.get("workers", 4) == first.get("workers", 4)
                    and task.get("direction", "download") == direction):
                batch.append(task)
                if len(batch) >= 128:
                    break
        if upload and any(is_multipart(task) for task in batch):
            if self.writer.completed_revision < self._state_revision:
                return
            if self.writer.saved_revision < self._state_revision:
                for task in batch:
                    if is_multipart(task):
                        task.update(status="failed", error="任务记录保存失败。请检查本机存储后重试。")
                self.task_dirty = self.persist_dirty = self._task_structure_dirty = True
                return
        snapshots = copy.deepcopy(batch)
        self.in_flight.update(task["id"] for task in snapshots)
        self.transfer_running = True
        self.dispatch_needed = False
        cancels = {task["id"]: self.cancel_events.setdefault(task["id"], threading.Event()) for task in batch}
        pauses = {task["id"]: self.pause_events.setdefault(task["id"], threading.Event()) for task in batch}

        def transfer() -> None:
            try:
                run = {"upload": transfer_uploads, "copy": transfer_copies, "download": transfer_tasks}[direction]
                run(connection, snapshots, cancels, int(first.get("workers", 4)), self.events.put, pause_events=pauses)
            except Exception as exc:
                error = upload_error(exc, connection) if direction != "download" else friendly_error(exc, connection)
                self.events.put({"kind": "batch_error", "ids": [task["id"] for task in snapshots], "error": error})
            finally:
                self.events.put({"kind": "batch_done", "ids": [task["id"] for task in snapshots]})

        self.worker = threading.Thread(target=transfer, daemon=True, name="r2-download-manager")
        self.worker.start()

    def set_task_filter(self, state: str) -> None:
        self.task_filter = state
        self.task_page_index = 0
        self._task_structure_dirty = True
        self.render_tasks()

    def select_task_batch(self) -> None:
        self.task_batch = self.batch_selector.currentData()
        self.set_task_filter("all")

    def select_task_direction(self) -> None:
        self.task_direction = self.direction_picker.currentData()
        self.set_task_filter(self.task_filter)

    def scoped_tasks(self) -> list[dict[str, Any]]:
        self.sync_task_index()
        tasks = self.tasks if self.task_batch is None else self.task_batches.get(self.task_batch, [])
        if self.task_direction is not None:
            return [task for task in tasks if task.get("direction", "download") == self.task_direction]
        return tasks

    def sync_task_index(self) -> None:
        source = (id(self.tasks), len(self.tasks))
        if source != self._task_source:
            self.task_by_id.clear()
            self.task_batches.clear()
            for task in self.tasks:
                self.task_by_id[task["id"]] = task
                self.task_batches.setdefault(task.get("batch_id", ""), []).append(task)
            self._task_source = source
            self._batch_selector_dirty = True
            self._task_structure_dirty = True

    def render_batch_selector(self) -> None:
        selector = self.batch_selector
        with QSignalBlocker(selector):
            selector.clear()
            selector.addItem("全部批次", None)
            for batch_id, tasks in reversed(self.task_batches.items()):
                first = tasks[0]
                if batch_id:
                    created = first.get("created_at", "")[:19].replace("T", " ")
                    direction = "归档" if first.get("archive") else TASK_DIRECTIONS[first.get("direction", "download")]
                    name = f"{direction} · {created} · {first['bucket']} · {len(tasks):,} 项"
                else:
                    name = f"旧任务 · {len(tasks):,} 个任务"
                selector.addItem(name, batch_id)
                selector.setItemData(selector.count() - 1, name, Qt.ItemDataRole.ToolTipRole)
            index = selector.findData(self.task_batch)
            selector.setCurrentIndex(max(0, index))
            self.task_batch = selector.currentData()
        selector.setToolTip(selector.currentText())
        self._batch_selector_dirty = False

    def render_tasks(self) -> None:
        self.sync_task_index()
        if self._batch_selector_dirty:
            self.render_batch_selector()
        self.batch_selector.setToolTip(self.batch_selector.currentText())
        self.task_dirty = False
        self.last_render = time.monotonic()
        if self._task_structure_dirty:
            counts = {state: 0 for state in self.filter_buttons}
            matching = []
            active = 0
            can_pause = can_resume = can_retry = False
            self._batch_size = self._batch_bytes = 0
            scoped = self.scoped_tasks()
            for task in scoped:
                actual = task["status"]
                active += actual in ACTIVE
                category = TASK_CATEGORIES.get(actual, actual)
                if category in counts:
                    counts[category] += 1
                if self.task_filter in ("all", category):
                    matching.append(task)
                size = task.get("size", 0)
                self._batch_size += size
                self._batch_bytes += size if category == "completed" else min(size, task.get("transferred", 0))
                can_pause |= actual in {"queued", "downloading"}
                if actual == "paused" or actual in FAILED:
                    available = task["id"] not in self.in_flight and task["id"] not in self.cleaning
                    can_resume |= available and actual == "paused"
                    can_retry |= available and actual in FAILED
            counts["all"] = len(scoped)
            self._batch_completed = counts["completed"]
            self._matching_tasks = matching
            self._active_count = active
            self._scoped_count = len(scoped)
            is_batch = bool(self.task_batch)
            scope_title = "批次" if is_batch else "旧任务"
            self.batch_pause.setText(f"暂停{scope_title}" if self.task_batch is not None else "全部暂停")
            self.batch_resume.setText(f"继续{scope_title}" if self.task_batch is not None else "全部继续")
            self.batch_pause.setEnabled(can_pause)
            self.batch_resume.setEnabled(can_resume)
            self.batch_retry.setEnabled(can_retry)
            self.batch_export.setEnabled(counts["failed"] > 0 and not self._exporting_failures)
            is_copy = bool(scoped and scoped[0].get("direction") == "copy")
            self.batch_output.setText("打开目标目录" if is_copy else "打开保存位置")
            self.batch_output.setEnabled(is_batch and bool(scoped) and (is_copy or bool(scoped[0].get("output_dir"))))
            self.clear_completed_button.setEnabled(counts["completed"] > 0)
            for state, control in self.filter_buttons.items():
                control.setText(f"{TASK_FILTERS[state]}  {counts[state]:,}")
                control.setChecked(state == self.task_filter)
            self._task_structure_dirty = False
        fraction = (self._batch_bytes / self._batch_size if self._batch_size
                    else self._batch_completed / max(1, self._scoped_count))
        self.batch_progress.setValue(round(1000 * fraction))
        self.batch_progress_text.setText(f"{size_text(self._batch_bytes)} / {size_text(self._batch_size)} · {fraction:.0%}")
        matching = getattr(self, "_matching_tasks", [])
        pages = max(1, (len(matching) + self.TASK_PAGE_SIZE - 1) // self.TASK_PAGE_SIZE)
        self.task_page_index = min(self.task_page_index, pages - 1)
        shown = matching[self.task_page_index * self.TASK_PAGE_SIZE:(self.task_page_index + 1) * self.TASK_PAGE_SIZE]
        signature = (self.task_filter, self.task_page_index, tuple(task["id"] for task in shown))
        if signature != self._task_signature:
            self.clear_layout(self.task_rows)
            self.task_widgets.clear()
            if not shown:
                empty = label("还没有传输任务\n在文件页选择对象下载，或到上传页添加本地文件。" if not self.tasks else "此分类暂无任务", "muted", True)
                empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
                empty.setMinimumHeight(200)
                self.task_rows.addWidget(empty)
            for task in shown:
                self.task_rows.addWidget(self.task_row(task))
            self._task_signature = signature
        for task in shown:
            self.update_task_row(task)
        self.tasks_nav.setText(f"传输任务  {len(self.tasks):,}" if self.tasks else "传输任务")
        self.task_previous.setEnabled(self.task_page_index > 0)
        self.task_next.setEnabled(self.task_page_index + 1 < pages)
        self.task_page_label.setText(f"{self.task_page_index + 1} / {pages}")
        if self.pages.currentIndex() == 1:
            self.status_label.setText(f"共 {self._scoped_count:,} 个任务 · {self._active_count:,} 个排队或运行中")

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
        name.setTextFormat(Qt.TextFormat.PlainText)
        name.setToolTip(task["key"])
        name.setMinimumWidth(100)
        name.setMaximumWidth(250)
        names.addWidget(name)
        direction = TASK_DIRECTIONS[task.get("direction", "download")]
        if task.get("source_key"):
            name.setToolTip(f"{task['source_key']} → {task['key']}")
        bucket = label(f"{direction} · {task.get('bucket', '')}", "tiny")
        bucket.setTextFormat(Qt.TextFormat.PlainText)
        names.addWidget(bucket)
        layout.addLayout(names, 4)
        detail = QVBoxLayout()
        progress = QProgressBar()
        progress.setRange(0, 1000)
        progress.setTextVisible(False)
        detail.addWidget(progress)
        info = label("", "tiny", True)
        info.setTextFormat(Qt.TextFormat.PlainText)
        detail.addWidget(info)
        layout.addLayout(detail, 5)
        state = label("")
        state.setFixedWidth(53)
        layout.addWidget(state)
        pause = button("", "pause", kind="ghost", tooltip=f"暂停{direction}", callback=lambda checked=False, task_id=task["id"]: self.toggle_pause(task_id))
        action = button("", "close", kind="ghost", tooltip="取消此任务", callback=lambda checked=False, task_id=task["id"]: self.task_action(task_id))
        failure = button("", "information-outline", kind="ghost", tooltip="查看失败原因", callback=lambda: self.show_task_error(task["id"]))
        remove = button("", "trash-can-outline", kind="ghost", tooltip="移除此任务记录", callback=lambda checked=False, task_id=task["id"]: self.remove_task(task_id))
        for control in (pause, action, failure, remove):
            control.setFixedSize(30, 30)
            layout.addWidget(control)
        self.task_widgets[task["id"]] = {"row": row, "progress": progress, "info": info, "state": state, "pause": pause, "action": action, "failure": failure, "remove": remove, "status": None}
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
        elif state == "paused" and task.get("direction") == "upload":
            text = text + " · 继续时核对已上传分片" if is_multipart(task) else "继续时将从头上传此文件"
        widgets["info"].setText(text)
        widgets["info"].setToolTip(task.get("error") or task.get("path") or task.get("output_dir", ""))
        status = {"queued": "等待中", "downloading": "下载中", "completed": "已完成", "skipped": "已跳过", "failed": "失败", "cancelled": "已取消", "interrupted": "未完成", "cancelling": "取消中", "paused": "已暂停", "pausing": "暂停中"}.get(state, state)
        direction = task.get("direction", "download")
        if direction != "download" and state == "downloading":
            status = TASK_DIRECTIONS[direction] + "中"
        widgets["state"].setText(status)
        if widgets["status"] != state:
            widgets["failure"].setVisible(state in FAILED)
            state_color = {"failed": ERROR, "interrupted": ERROR, "downloading": ACCENT, "completed": SUCCESS}.get(state, MUTED)
            widgets["state"].setStyleSheet(f"font-size:12px;color:{state_color};")
            widgets["pause"].setIcon(icon("play-outline" if state == "paused" else "pause"))
            if direction == "upload":
                resume = "继续分片上传" if is_multipart(task) else "从头继续上传"
                widgets["pause"].setToolTip(resume if state == "paused" else "暂停上传")
            else:
                widgets["pause"].setToolTip(("继续" if state == "paused" else "暂停") + TASK_DIRECTIONS[direction])
            widgets["action"].setIcon(icon("refresh" if state in {"failed", "cancelled", "interrupted"} else "folder-open-outline" if state in {"completed", "skipped"} else "close"))
            location = {"upload": "打开本地源目录", "copy": "打开目标目录", "download": "打开保存位置"}[direction]
            widgets["action"].setToolTip("继续重试" if state in {"failed", "cancelled", "interrupted"} else location if state in {"completed", "skipped"} else "取消此任务")
            widgets["status"] = state
        cleaning = task["id"] in self.upload_cleaning
        widgets["pause"].setEnabled(not cleaning and state in {"queued", "downloading", "paused"} and not (state == "paused" and task["id"] in self.in_flight))
        widgets["action"].setEnabled(not cleaning and state not in {"pausing", "cancelling"})
        widgets["remove"].setEnabled(not cleaning and state not in ACTIVE and task["id"] not in self.in_flight)

    def show_task_error(self, task_id: str) -> None:
        task = self.task_by_id.get(task_id)
        if not task or task["status"] not in FAILED:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("传输失败原因")
        dialog.setMinimumWidth(530)
        layout = QVBoxLayout(dialog)
        message = QPlainTextEdit(task["key"] + "\n\n" + (task.get("error") or "任务中断。可点击重试继续处理。"))
        message.setReadOnly(True)
        layout.addWidget(message)
        layout.addWidget(button("关闭", callback=dialog.accept), alignment=Qt.AlignmentFlag.AlignRight)
        dialog.exec()
        dialog.deleteLater()

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
        if task is None or task["status"] not in ACTIVE | {"paused"} or task_id in self.upload_cleaning:
            return
        if task_id in self.in_flight:
            self.cancel_events.setdefault(task_id, threading.Event()).set()
        task["status"] = "cancelling" if task_id in self.in_flight else "cancelled"
        if task_id not in self.in_flight:
            if cleanup and is_multipart(task):
                task["status"] = "cancelling"
                self.cleanup_uploads([task], remove=False)
            elif cleanup and task.get("transferred", 0):
                self.cleanup_partials([dict(task)])
            if task["status"] == "cancelled":
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
        for task in self.scoped_tasks():
            self.pause_task(task["id"])
        self.render_tasks()
        self.persist()

    def resume_all(self) -> None:
        self.retry_tasks({"paused"})

    def cleanup_partials(self, tasks: list[dict[str, Any]]) -> None:
        tasks = [task for task in tasks if task.get("direction", "download") == "download"]
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

    def cleanup_uploads(self, tasks: list[dict[str, Any]], *, remove: bool, clear: bool = False) -> None:
        if not tasks:
            return
        profiles = {profile["id"]: connection_of(profile) for profile in self.profiles if profile.get("secret_access_key")}
        by_target = {connection_fingerprint(connection): connection for connection in profiles.values()}
        jobs = []
        for task in tasks:
            fingerprint = task.get("connection_fingerprint")
            connection = profiles.get(task.get("profile_id"))
            if connection is None or connection_fingerprint(connection) != fingerprint:
                connection = by_target.get(fingerprint)
            jobs.append((dict(task), connection))
        ids = {task["id"] for task in tasks}
        self.upload_cleaning.update(ids)
        self.cleaning.update(ids)
        self.task_dirty = self._task_structure_dirty = True

        def cleanup() -> None:
            results = {}
            for task, connection in jobs:
                try:
                    committed = discard_upload_checkpoint(task, connection, remove=remove)
                except Exception as exc:
                    error = upload_error(exc, connection) if connection else str(exc)
                    results[task["id"]] = (False, f"远端分片清理失败。{error}")
                else:
                    results[task["id"]] = (committed, "")
            self.events.put({"kind": "upload_cleanup_done", "results": results, "remove": remove, "clear": clear})

        threading.Thread(target=cleanup, daemon=True, name="r2-upload-cleanup").start()

    def retry_task(self, task_id: str) -> None:
        self.sync_task_index()
        task = self.task_by_id.get(task_id)
        if task is None or task["status"] in ACTIVE:
            return
        if task_id in self.in_flight or task_id in self.cleaning:
            self.toast("任务正在结束，请稍候再重试。")
            return
        connection = self.retry_connection(task)
        if connection is None or self.task_by_id.get(task_id) is not task or task["status"] in ACTIVE:
            return
        self.queue_retry(task, connection)
        if not self.persist() and is_multipart(task):
            task.update(status="failed", error="任务记录保存失败。请检查本机存储后重试。")
        self.render_tasks()
        self.dispatch()

    def retry_connection(self, task: dict[str, Any]) -> Connection | None:
        profile = next((profile for profile in self.profiles if profile["id"] == task.get("profile_id")), None)
        if not profile:
            QMessageBox.information(self, "原连接已移除", "请添加连接并重新创建传输任务。")
            return None
        if not profile.get("secret_access_key"):
            self.edit_connection(profile["id"])
            profile = next((profile for profile in self.profiles if profile["id"] == task.get("profile_id")), None)
            if not profile or not profile.get("secret_access_key"):
                return None
        connection = connection_of(profile)
        if connection.bucket != task["bucket"] or (task.get("connection_fingerprint") and task["connection_fingerprint"] != connection_fingerprint(connection)):
            QMessageBox.warning(self, "连接目标已改变", "此连接的存储桶或地址已修改。请重新创建传输任务。")
            return None
        return connection

    def queue_retry(self, task: dict[str, Any], connection: Connection) -> None:
        task_id = task["id"]
        task.update(status="queued", error="")
        self.cancel_events[task_id] = threading.Event()
        self.pause_events[task_id] = threading.Event()
        self.task_connections[task_id] = connection
        self.task_dirty = self.persist_dirty = self._task_structure_dirty = True
        self.dispatch_needed = True

    def retry_failed(self) -> None:
        self.retry_tasks(FAILED)

    def retry_tasks(self, statuses: set[str]) -> None:
        connections: dict[tuple[str, str, str], Connection | None] = {}
        ready = []
        for task in self.scoped_tasks():
            if task["status"] not in statuses or task["id"] in self.in_flight or task["id"] in self.cleaning:
                continue
            target = (task.get("profile_id", ""), task["bucket"], task.get("connection_fingerprint", ""))
            if target not in connections:
                connections[target] = self.retry_connection(task)
            connection = connections[target]
            if connection is not None:
                ready.append((task, connection))
        self.sync_task_index()
        for task, connection in ready:
            if self.task_by_id.get(task["id"]) is task and task["status"] in statuses:
                self.queue_retry(task, connection)
        if ready:
            if not self.persist():
                for task, _ in ready:
                    if task["status"] == "queued" and is_multipart(task):
                        task.update(status="failed", error="任务记录保存失败。请检查本机存储后重试。")
            self.render_tasks()

    def remove_task(self, task_id: str) -> None:
        self.sync_task_index()
        task = self.task_by_id.get(task_id)
        if task and is_multipart(task):
            if task["status"] not in ACTIVE and task_id not in self.in_flight | self.upload_cleaning:
                self.cleanup_uploads([task], remove=True)
                self.render_tasks()
            return
        self.cleanup_partials([dict(task) for task in self.tasks if task["id"] == task_id and task["status"] not in ACTIVE and task_id not in self.in_flight])
        self.tasks = [task for task in self.tasks if task["id"] != task_id or task["status"] in ACTIVE or task_id in self.in_flight]
        self.persist()
        self.render_tasks()

    def clear_completed(self) -> None:
        tasks = [task for task in self.scoped_tasks() if task["status"] in {"completed", "skipped"}]
        owned = self.in_flight | self.upload_cleaning
        self.cleanup_uploads([task for task in tasks if is_multipart(task) and task["id"] not in owned], remove=True)
        completed = {task["id"] for task in tasks if not is_multipart(task)}
        self.tasks = [task for task in self.tasks if task["id"] not in completed]
        self.persist()
        self.render_tasks()
        self.toast("已清除完成记录，本地文件和远端对象仍保留。")

    def clear_tasks(self) -> None:
        if self.clear_when_idle or self.upload_cleaning:
            self.toast("正在清理任务，请稍候。")
            return
        if self.preparing:
            self._prepare_token = ""
            self.preparing = False
        if not self.tasks:
            self.toast("任务记录已经为空。")
            return
        active = self.transfer_running or any(task["status"] in ACTIVE for task in self.tasks)
        message = ("将取消未结束的任务并清空传输记录。本地文件和远端对象会保留。"
                   if active else "清空全部传输记录？本地文件和远端对象会保留。")
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
            self.finish_clear_tasks()
            self.persist()
            self.render_tasks()

    def finish_clear_tasks(self) -> None:
        self.cleanup_partials([dict(task) for task in self.tasks])
        self.tasks = [task for task in self.tasks if is_multipart(task)]
        self.task_connections.clear()
        self.cancel_events.clear()
        self.pause_events.clear()
        self.clear_when_idle = bool(self.tasks)
        self.cleanup_uploads(self.tasks, remove=True, clear=True)
        if not self.clear_when_idle:
            self.activity.setText("任务记录已清空")

    def open_batch_output(self) -> None:
        tasks = self.scoped_tasks()
        if self.task_batch and tasks and tasks[0].get("direction") != "upload":
            self.open_output(tasks[0])

    def export_failed_tasks(self) -> None:
        rows = [(task["bucket"], task["key"], "失败" if task["status"] == "failed" else "未完成",
                 task.get("error") or "未记录失败原因")
                for task in self.scoped_tasks() if task["status"] in FAILED]
        if not rows or self._exporting_failures:
            return
        default = f"S3FileManager-failed-{datetime.now():%Y%m%d-%H%M%S}.csv"
        filename, _ = QFileDialog.getSaveFileName(self, "导出失败清单", default, "CSV 文件 (*.csv)")
        if not filename:
            return
        path = Path(filename)
        self._exporting_failures = True
        self.batch_export.setEnabled(False)

        def export() -> None:
            try:
                write_failed_tasks(path, rows)
            except (OSError, UnicodeError) as exc:
                self.events.put({"kind": "failed_export", "error": str(exc)})
            else:
                self.events.put({"kind": "failed_export", "count": len(rows)})

        threading.Thread(target=export, daemon=True, name="s3fm-export-failures").start()

    def open_output(self, task: dict[str, Any]) -> None:
        if task.get("direction") == "copy":
            profile = next((item for item in self.profiles if item["id"] == task.get("profile_id")), None)
            if profile and connection_fingerprint(connection_of(profile)) == task.get("connection_fingerprint"):
                self.change_bucket(profile["id"])
                parent, sep, _ = task["key"].rpartition("/")
                self.navigate(parent + sep)
            else:
                QMessageBox.information(self, "原连接不可用", "请恢复原存储桶连接后查看目标目录。")
            return
        output = task.get("output_dir")
        if task.get("direction") == "upload" and task.get("path"):
            output = str(Path(task["path"]).parent)
        folder = Path(output).expanduser() if output else None
        if folder is not None and folder.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder.resolve())))
        else:
            QMessageBox.information(self, "保存位置不存在", "该保存文件夹已被移动或删除。")

    def show_settings(self) -> None:
        updates = UpdatePanel(VERSION, state_path().parent / "update-downloads", self.can_install_update)
        dialog = SettingsDialog(
            self, updates, self.show_download_settings,
            preferences=self.preferences, save_preferences=self.save_display_preferences,
            list_profiles=self.ordered_profiles, add_profile=self.add_connection,
            edit_profile=self.edit_connection, remove_profile=self.remove_connection,
        )

        def install(staged: Path, release: UpdateRelease) -> None:
            dialog.accept()
            self.install_staged_update(staged, release)

        updates.ready_to_install.connect(install)
        dialog.exec()
        dialog.deleteLater()

    def save_display_preferences(self, changes: dict[str, Any]) -> bool:
        before = self.preferences.copy()
        self.preferences.update(changes)
        if not self.persist():
            self.preferences.clear()
            self.preferences.update(before)
            return False
        if "file_view" in changes:
            self.sort_column = changes["file_sort"]
            self.sort_descending = changes["file_descending"]
            if self.PAGE_SIZE != changes["file_page_size"]:
                self.PAGE_SIZE = changes["file_page_size"]
                self.reset_directory_pages()
            self.view_mode = changes["file_view"]
            self.grid_button.setChecked(self.view_mode == "grid")
            self.list_button.setChecked(self.view_mode == "list")
            self.file_view_changed()
        if "bucket_order" in changes:
            self.render_buckets()
        return True

    def show_download_settings(self, parent: QWidget) -> None:
        dialog = DownloadDialog(parent, self.preferences)
        result = dialog.exec()
        dialog.deleteLater()
        if result != QDialog.DialogCode.Accepted:
            return
        self.preferences.update(output_dir=dialog.destination.text().strip(), workers=dialog.workers.currentData(), skip_existing=dialog.skip.isChecked())
        if self.persist():
            self.toast("下载设置已保存。")

    def install_staged_update(self, staged: Path, release: UpdateRelease) -> None:
        if not self.can_install_update():
            self.discard_update(staged)
            QMessageBox.information(self, "暂停任务后安装", "请先暂停或完成正在进行和等待中的传输，再点击检查更新。")
            return
        if not self.persist():
            self.discard_update(staged)
            return
        self._update_pending = (staged, release.sha256, self._state_revision)
        self.centralWidget().setEnabled(False)

    def can_install_update(self) -> bool:
        return not (self.closing or self.transfer_running or self.preparing or self.cleaning or self.upload_cleaning
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
        if kind == "duplicates_exported":
            self.toast("重复文件结果已导出。")
            return
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
            self.task_dirty = self._task_structure_dirty = True
            return
        if kind == "upload_cleanup_done":
            self.sync_task_index()
            self.upload_cleaning.difference_update(event["results"])
            self.cleaning.difference_update(event["results"])
            removed = set()
            failures = []
            for task_id, (committed, error) in event["results"].items():
                task = self.task_by_id.get(task_id)
                if task is None:
                    continue
                if error:
                    task["error"] = error
                    if task["status"] != "completed":
                        task["status"] = "failed"
                    failures.append(task_id)
                elif event["remove"]:
                    removed.add(task_id)
                elif committed:
                    task.update(status="completed", transferred=task["size"], error="")
                    self._file_refresh_profiles.add(task["profile_id"])
                else:
                    task.update(status="cancelled", transferred=0, error="已取消，远端未完成的分片已清理。")
            if removed:
                self.tasks = [task for task in self.tasks if task["id"] not in removed]
                for task_id in removed:
                    self.task_connections.pop(task_id, None)
                    self.cancel_events.pop(task_id, None)
                    self.pause_events.pop(task_id, None)
            if event["clear"]:
                self.clear_when_idle = False
            if failures:
                self.toast(f"{len(failures):,} 项清理失败。请查看任务详情，修复后再次移除。")
            self.task_dirty = self._task_structure_dirty = True
            self.persist()
            self.refresh_after_remote_write()
            return
        if kind == "failed_export":
            self._exporting_failures = False
            self.task_dirty = self._task_structure_dirty = True
            if "error" in event:
                QMessageBox.warning(self, "无法导出失败清单", event["error"])
            else:
                self.toast(f"已导出 {event['count']:,} 条失败记录。")
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
            if event["token"] != self._query_token:
                return
            self._search_pending = None
            self._search_cancel = None
            key = event["key"]
            if key != (self.bucket_id, self.file_view(), self.catalogs.get(self.bucket_id)):
                return
            if "error" in event:
                self.selection_note.setText(event["error"])
                return
            self._search_result = (key, event["rows"])
            self.render_files()
            return
        if kind == "tasks_ready":
            if event["token"] == self._prepare_token and not self.closing:
                self.preparing = False
                self._prepare_token = ""
                if "error" in event:
                    QMessageBox.warning(self, "无法创建任务", event["error"])
                else:
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
        if kind.startswith("duplicate_"):
            if event["scan"] is not self.duplicate_cancel:
                return
            if kind == "duplicate_progress":
                if not self.duplicate_cancel.is_set():
                    self.duplicate_summary.setText(f"正在校验内容  {event['done']:,} / {event['total']:,}")
                return
            stopped = self.duplicate_cancel.is_set()
            self.duplicate_cancel = None
            if stopped or "status" in event:
                self.duplicate_summary.setText("已停止扫描。" if stopped else event["status"])
            else:
                self.duplicate_rows = event["rows"]
                self.duplicate_summary.setText(f"{event['groups']:,} 组重复文件 · {len(self.duplicate_rows):,} 个对象")
                self.render_duplicate_rows()
            self.render_bucket_tools()
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
                self.synced_at[profile_id] = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
                profile.update(file_count=catalog.file_count if catalog else len(event["objects"]), size_label=size_text(catalog.total_size if catalog else sum(entry["size"] for entry in event["objects"])), status="已连接")
            elif kind == "scan_error":
                self.load_errors[profile_id] = event["error"]
                profile["status"] = "连接失败"
            self.render_buckets()
            outcome = {"scan_done": "读取完成", "scan_error": "读取失败", "scan_cancelled": "已停止读取"}[kind]
            sync_time = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
            profile["sync_history"] = [*profile.get("sync_history", [])[-19:], [sync_time, outcome]]
            if kind == "scan_done":
                profile["last_sync"] = sync_time
            self.persist_dirty = True
            if profile_id == self.bucket_id:
                self.activity.setText(outcome)
                self.render_files()
                if self.pages.currentIndex() == OVERVIEW_PAGE:
                    self.render_overview()
                elif self.pages.currentIndex() == TOOLS_PAGE:
                    self.duplicate_summary.clear()
                    self.render_bucket_tools()
            return
        if kind == "batch_error":
            self.sync_task_index()
            for task_id in event["ids"]:
                task = self.task_by_id.get(task_id)
                if task and task["status"] in ACTIVE:
                    task.update(status="failed", transferred=0, error=event["error"])
        elif kind == "batch_done":
            self.transfer_running = False
            self.in_flight.difference_update(event["ids"])
            self.sync_task_index()
            for task_id in event["ids"]:
                task = self.task_by_id.get(task_id)
                if task and task["status"] in ACTIVE:
                    task.update(status="cancelled" if self.cancel_events.get(task["id"], threading.Event()).is_set() else "failed", error="任务未完成，可重试。")
                if task and task.get("direction") in {"upload", "copy"} and task["status"] == "completed":
                    self._file_refresh_profiles.add(task["profile_id"])
                self.task_connections.pop(task_id, None)
                self.cancel_events.pop(task_id, None)
                self.pause_events.pop(task_id, None)
            if self.clear_when_idle:
                self.finish_clear_tasks()
            self.dispatch_needed = True
            self.refresh_after_remote_write()
        else:
            self.sync_task_index()
            task = self.task_by_id.get(event.get("id"))
            if task is None:
                return
            if kind == "started":
                if task["status"] not in {"pausing", "cancelling"}:
                    task.update(status="downloading", _started=time.monotonic(), _start_bytes=task.get("transferred", 0))
            elif kind == "resumed":
                task.update(transferred=event["transferred"], _started=time.monotonic(), _start_bytes=event["transferred"])
            elif kind == "progress":
                before = task.get("transferred", 0)
                task["transferred"] = min(task.get("size", 0), max(0, before + event.get("bytes", 0)))
                if (not self._task_structure_dirty and task["status"] not in {"completed", "skipped"}
                        and (self.task_batch is None or self.task_batch == task.get("batch_id", ""))
                        and (self.task_direction is None or self.task_direction == task.get("direction", "download"))):
                    self._batch_bytes += task["transferred"] - before
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
        if self.task_dirty and self.queue_preview.isVisible() and now - self.last_queue_preview > 0.35:
            self.render_queue_preview()
        if not self.closing and self.persist_dirty and now - self.last_persist > 4:
            self.persist()
        if self.dispatch_needed and not self.transfer_running:
            self.dispatch()
        if self.transfer_running:
            self.activity.setText("正在传输文件…")
        elif self.upload_cleaning:
            self.activity.setText("正在清理上传任务…")
        elif not self.loading and not self.clear_when_idle and not self.preparing:
            self.activity.setText("就绪")
        self.continue_update()
        if self._update_installing:
            self.activity.setText("正在准备更新，即将重启…")
        if self.closing and not (self.transfer_running or self.upload_cleaning):
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
            if QMessageBox.question(self, "暂停传输并退出", "是否暂停任务后退出？继续时会核对下载断点和已上传分片，小文件上传会从头开始。") != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.closing = True
            for task in self.tasks:
                self.pause_task(task["id"])
        if self.transfer_running or self.upload_cleaning:
            self.closing = True
            self.activity.setText("正在等待传输暂停或分片清理完成…")
            event.ignore()
            return
        for cancel in self.loading.values():
            cancel.set()
        self.stop_duplicate_scan()
        self.cancel_file_search()
        self.reset_directory_pages()
        self.closing = True
        self._prepare_token = ""
        if not self._closing_saved and self._close_revision is None:
            if not self.persist():
                event.ignore()
                self.closing = False
                return
            self._close_revision = self._state_revision
            self.centralWidget().setEnabled(False)
        if not self._closing_saved:
            if self.writer.completed_revision < self._close_revision:
                self.activity.setText("正在保存任务记录…")
                event.ignore()
                return
            if self.writer.saved_revision < self._close_revision:
                event.ignore()
                self.closing = False
                self._close_revision = None
                self.persist_dirty = True
                self.centralWidget().setEnabled(True)
                QMessageBox.warning(self, "暂时无法退出", "任务记录未能保存。请检查磁盘空间和本机目录权限后重试。")
                return
            self._closing_saved = True
            self.writer.close(timeout=0)
        if self.writer.is_closed:
            self.timer.stop()
            event.accept()
        else:
            self.activity.setText("正在保存任务记录…")
            event.ignore()


def main(update_workspace: str = "") -> None:
    application = QApplication(sys.argv)
    application.setApplicationDisplayName(APP_NAME)
    application.setWindowIcon(app_icon())
    application.setStyle("Fusion")
    application.setFont(QFont("PingFang SC" if sys.platform == "darwin" else "Microsoft YaHei UI", 10))
    application.setStyleSheet(app_style())
    window = ClientWindow()
    install_diagnostics(lambda text: window.events.put({"kind": "background_error", "error": text}))
    window.show()
    if update_workspace:
        QTimer.singleShot(0, lambda: schedule_update_cleanup(update_workspace))
    sys.exit(application.exec())


if __name__ == "__main__":
    main()
