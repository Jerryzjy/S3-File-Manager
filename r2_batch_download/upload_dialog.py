"""Choose local sources and review the exact object keys before upload."""
from __future__ import annotations

from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSignalBlocker, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFileDialog, QHeaderView, QHBoxLayout,
    QLineEdit, QMessageBox, QTableView, QVBoxLayout, QWidget,
)

from .desktop_engine import Cancelled, Connection
from .ui import ERROR, Dropdown, button, label, size_text
from .upload import UploadItem, plan_upload, upload_error


def choose_upload_sources(parent: QWidget, *, folder: bool) -> tuple[Path, ...]:
    if folder:
        path = QFileDialog.getExistingDirectory(parent, "选择上传文件夹")
        return (Path(path),) if path else ()
    paths, _ = QFileDialog.getOpenFileNames(parent, "选择上传文件")
    return tuple(map(Path, paths))


class UploadPreview(QAbstractTableModel):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.items: list[UploadItem] = []

    def replace(self, items: list[UploadItem]) -> None:
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else 3

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return ("目标对象 Key", "大小", "目标状态")[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        item = self.items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            state = "已改名" if item.original_key else "已存在" if item.remote_etag else "新文件"
            return (item.key, size_text(item.size), state)[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return item.key if index.column() == 0 else str(item.source)
        return None


class UploadDialog(QDialog):
    def __init__(self, parent: QWidget, connection: Connection, prefix: str) -> None:
        super().__init__(parent)
        self.connection = connection
        self.prefix = prefix
        self.target = f"r2://{connection.bucket}/{prefix}"
        self.setWindowTitle("上传文件")
        self.setAcceptDrops(True)
        self.resize(730, 490)
        self.setMinimumSize(650, 420)
        self.cancel: Event | None = None
        self.events: Queue = Queue()
        self.sources: tuple[Path, ...] = ()
        self.selected_items: list[UploadItem] = []
        self.conflicts = 0
        self.renamed = 0
        self.rename_mode = False
        self.total_bytes = 0
        self.new_bytes = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 22, 26, 22)
        layout.setSpacing(12)
        layout.addWidget(label("上传文件", "title"))
        destination = QHBoxLayout()
        destination.addWidget(label("上传到", "muted"))
        path = QLineEdit(self.target)
        path.setAccessibleName("目标桶和目录")
        path.setReadOnly(True)
        destination.addWidget(path, 1)
        layout.addLayout(destination)

        source_row = QHBoxLayout()
        source_row.addWidget(button("选择文件", "file-outline", callback=self.choose_files))
        source_row.addWidget(button("选择文件夹", "folder-outline", callback=self.choose_folder))
        self.recheck = button("重新检查", "refresh", callback=self.check_again)
        self.recheck.setEnabled(False)
        source_row.addWidget(self.recheck)
        source_row.addStretch()
        layout.addLayout(source_row)

        self.preview = UploadPreview(self)
        self.table = QTableView()
        self.table.setAccessibleName("待上传对象预览")
        self.table.setModel(self.preview)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 95)
        self.table.setColumnWidth(2, 90)
        layout.addWidget(self.table, 1)

        self.note = label("选择或拖入文件和文件夹以检查目标。同名对象默认跳过。", "muted", True)
        self.note.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.note)
        actions = QHBoxLayout()
        conflict_label = label("同名处理", "muted")
        self.policy = Dropdown()
        self.policy.addItem("跳过同名", False)
        self.policy.addItem("同名改名", "rename")
        self.policy.addItem("覆盖同名", True)
        self.policy.setAccessibleName("同名处理")
        conflict_label.setBuddy(self.policy)
        self.policy.currentIndexChanged.connect(self.policy_changed)
        self.policy.setEnabled(False)
        actions.addWidget(conflict_label)
        actions.addWidget(self.policy)
        actions.addStretch()
        actions.addWidget(button("取消", callback=self.reject))
        self.submit_button = button("开始上传", "upload-outline", kind="primary", callback=self.confirm)
        self.submit_button.setEnabled(False)
        actions.addWidget(self.submit_button)
        layout.addLayout(actions)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(75)

    def choose_files(self) -> None:
        paths = choose_upload_sources(self, folder=False)
        if paths:
            self.check_sources(paths)

    def choose_folder(self) -> None:
        paths = choose_upload_sources(self, folder=True)
        if paths:
            self.check_sources(paths)

    def dragEnterEvent(self, event) -> None:
        urls = event.mimeData().urls()
        if urls and all(url.isLocalFile() for url in urls):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()

    def dropEvent(self, event) -> None:
        self.check_sources(tuple(Path(url.toLocalFile()) for url in event.mimeData().urls()))
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()

    def check_again(self) -> None:
        self.check_sources(self.sources, keep_policy=True)

    def policy_changed(self) -> None:
        if self.sources and (self.policy.currentData() == "rename") != self.rename_mode:
            self.check_sources(self.sources, keep_policy=True)
        else:
            self.update_summary()

    def check_sources(self, sources: tuple[Path, ...], *, keep_policy: bool = False) -> None:
        if self.cancel is not None:
            self.cancel.set()
        self.cancel = cancel = Event()
        self.sources = sources
        self.preview.replace([])
        self.recheck.setEnabled(False)
        self.policy.setEnabled(False)
        if not keep_policy:
            with QSignalBlocker(self.policy):
                self.policy.setCurrentIndex(0)
        rename = self.rename_mode = self.policy.currentData() == "rename"
        self.submit_button.setEnabled(False)
        self.note.setStyleSheet("")
        self.note.setText("正在读取本地文件并检查目标对象…")
        events, connection, prefix = self.events, self.connection, self.prefix

        def inspect() -> None:
            try:
                items = plan_upload(connection, prefix, sources, cancel, rename_conflicts=rename)
                conflicts = [item for item in items if item.remote_etag]
                totals = (len(conflicts), sum(item.size for item in items), sum(item.size for item in conflicts),
                          sum(bool(item.original_key) for item in items))
                events.put((cancel, items, totals, ""))
            except Cancelled:
                return
            except Exception as exc:
                events.put((cancel, [], (0, 0, 0, 0), upload_error(exc, connection)))

        Thread(target=inspect, name="r2-upload-preflight", daemon=True).start()

    def poll(self) -> None:
        while True:
            try:
                token, items, totals, error = self.events.get_nowait()
            except Empty:
                return
            if token is not self.cancel:
                continue
            self.recheck.setEnabled(True)
            if error:
                self.note.setStyleSheet(f"color:{ERROR};")
                self.note.setText(error)
                self.policy.setEnabled(self.rename_mode)
                continue
            self.conflicts, self.total_bytes, conflict_bytes, self.renamed = totals
            self.new_bytes = self.total_bytes - conflict_bytes
            self.preview.replace(items)
            self.policy.setEnabled(self.conflicts > 0 or self.rename_mode)
            self.update_summary()

    def update_summary(self) -> None:
        total = len(self.preview.items)
        if not total:
            return
        overwrite = self.policy.currentData() is True
        count = total if overwrite else total - self.conflicts
        size = self.total_bytes if overwrite else self.new_bytes
        action = "覆盖" if overwrite else "跳过"
        conflict_text = f" · {action} {self.conflicts:,} 个同名对象" if self.conflicts else ""
        if self.renamed:
            conflict_text = f" · 改名 {self.renamed:,} 个同名对象"
        self.note.setText(f"将上传 {count:,} 个文件 · {size_text(size)}{conflict_text}")
        self.submit_button.setEnabled(count > 0)

    def confirm(self) -> None:
        if not self.submit_button.isEnabled():
            return
        overwrite = self.policy.currentData() is True
        if overwrite and self.conflicts:
            answer = QMessageBox.question(self, "确认覆盖对象",
                                          f"将覆盖 {self.target} 下的 {self.conflicts:,} 个同名对象。是否继续？",
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                          QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.selected_items = [item for item in self.preview.items if overwrite or not item.remote_etag]
        self.accept()

    def done(self, result: int) -> None:
        if self.cancel is not None:
            self.cancel.set()
        self.timer.stop()
        super().done(result)
