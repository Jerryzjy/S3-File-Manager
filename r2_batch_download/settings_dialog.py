"""Desktop settings navigation."""

from collections.abc import Callable
import platform

from PySide6.QtCore import QSignalBlocker, Qt, QUrl, qVersion
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QLabel, QButtonGroup, QDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget

from . import APP_NAME, __version__
from .object_actions import LINK_DURATIONS
from .ui import Dropdown, app_icon, button, label
from .update_dialog import UpdatePanel


FILE_DISPLAY_OPTIONS = (
    ("startup_page", "启动页面", (("文件", "files"), ("上传", "upload"), ("传输任务", "tasks"), ("统计", "overview"))),
    ("file_view", "默认视图", (("网格", "grid"), ("列表", "list"))),
    ("file_sort", "默认排序", (("自然顺序", -1), ("名称", 0), ("大小", 1), ("扩展名", 2), ("修改时间", 3))),
    ("file_descending", "排序方向", (("升序", False), ("降序", True))),
    ("file_page_size", "每页显示", tuple((f"{size} 项", size) for size in (120, 60, 240))),
)


class LayoutSettings(QWidget):
    def __init__(self, title, options, preferences, save):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        layout.addWidget(label(title, "title"))
        card = QFrame()
        card.setObjectName("settingsCard")
        form = QFormLayout(card)
        form.setContentsMargins(18, 16, 18, 16)
        form.setSpacing(12)
        controls = {}
        for name, caption, choices in options:
            control = Dropdown()
            control.setAccessibleName(caption)
            for text, value in choices:
                control.addItem(text, value)
            control.setCurrentIndex(max(0, control.findData(preferences.get(name))))
            form.addRow(caption, control)
            controls[name] = control
        layout.addWidget(card)
        actions = QHBoxLayout()

        def reset() -> None:
            for control in controls.values():
                control.setCurrentIndex(0)

        status = label("", "muted", True)
        actions.addWidget(button("恢复默认值", callback=reset))
        actions.addStretch()
        actions.addWidget(button("保存设置", kind="primary", callback=lambda: status.setText(
            "设置已保存。" if save({key: control.currentData() for key, control in controls.items()}) else "无法保存设置。"
        )))
        layout.addLayout(actions)
        layout.addWidget(status)
        layout.addStretch()


class SettingsDialog(QDialog):
    def __init__(
        self, parent: QWidget, updates: UpdatePanel,
        edit_downloads: Callable[[QWidget], None],
        *, preferences=None, save_preferences=None, list_profiles=None,
        add_profile=None, edit_profile=None, remove_profile=None,
    ) -> None:
        super().__init__(parent)
        self.updates = updates
        self.setWindowTitle("设置")
        self.resize(740, 550)
        self.setMinimumSize(650, 508)
        self.setObjectName("settingsDialog")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("settingsSidebar")
        sidebar.setFixedWidth(164)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(14, 22, 14, 18)
        side.setSpacing(6)
        side.addWidget(label("设置", "title"))
        side.addSpacing(12)
        layout.addWidget(sidebar)

        content = QVBoxLayout()
        content.setContentsMargins(26, 22, 26, 18)
        content.setSpacing(14)
        pages = QStackedWidget()
        sections = [
            ("下载", "download-outline", self.action_page(
                "下载", "设置新建下载任务的默认保存位置、并发数量和同名文件处理方式。",
                "修改下载设置", lambda: edit_downloads(self),
            )),
            ("软件更新", "update", updates),
        ]
        if list_profiles is not None:
            sections.insert(0, ("存储桶", "database-outline", self.connections_page(
                list_profiles, add_profile, edit_profile, remove_profile, preferences, save_preferences,
            )))
        if preferences is not None:
            sections.insert(1, ("文件显示", "view-grid-outline", LayoutSettings(
                "文件显示", FILE_DISPLAY_OPTIONS, preferences, save_preferences,
            )))
            sections.insert(3, ("上传", "upload-outline", LayoutSettings(
                "上传", (("upload_workers", "同时上传", tuple((f"{count} 个文件", count) for count in (4, 1, 2, 6, 8))),),
                preferences, save_preferences,
            )))
            durations = (LINK_DURATIONS[1], LINK_DURATIONS[0], *LINK_DURATIONS[2:])
            sections.insert(4, ("分享链接", "link-variant", LayoutSettings(
                "分享链接", (("share_duration", "限时链接默认有效期", durations),), preferences, save_preferences,
            )))
        sections.append(("关于", "information-outline", self.about_page()))
        navigation = QButtonGroup(self)
        navigation.idClicked.connect(pages.setCurrentIndex)
        for index, (title, glyph, page) in enumerate(sections):
            control = button(title, glyph, kind="settingsNav")
            control.setCheckable(True)
            navigation.addButton(control, index)
            side.addWidget(control)
            pages.addWidget(page)
        navigation.button(0).setChecked(True)
        side.addStretch()

        content.addWidget(pages, 1)
        close = button("关闭", callback=self.reject)
        close.setAutoDefault(False)
        content.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        layout.addLayout(content, 1)

    @staticmethod
    def about_page() -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        layout.addWidget(label("关于", "title"))
        card = QFrame()
        card.setObjectName("settingsCard")
        details = QVBoxLayout(card)
        details.setContentsMargins(19, 17, 19, 17)
        header = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(52, 52))
        header.addWidget(logo)
        name = QVBoxLayout()
        name.addWidget(label(APP_NAME, "subheading"))
        name.addWidget(label(f"版本 {__version__}", "muted"))
        header.addLayout(name, 1)
        details.addLayout(header)
        details.addWidget(label("连接 Cloudflare R2、Backblaze B2 和 S3 兼容存储，浏览对象、批量传输和增量归档。", "muted", True))
        system = "macOS" if platform.system() == "Darwin" else platform.system()
        release = platform.mac_ver()[0] if system == "macOS" else platform.release()
        environment = f"{system} {release} · {platform.machine()} · Qt {qVersion()}"
        values = QFormLayout()
        values.addRow("开发者", label("Mdaeo"))
        website = label('<a href="https://blog.mdaeo.org">blog.mdaeo.org</a>')
        website.setTextFormat(Qt.TextFormat.RichText)
        website.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        website.setOpenExternalLinks(True)
        website.setAccessibleName("Mdaeo 开发者网站")
        values.addRow("网站", website)
        values.addRow("当前环境", label(environment))
        values.addRow("开源许可", label("GPL-3.0"))
        details.addLayout(values)
        layout.addWidget(card)

        project = "https://github.com/Jerryzjy/S3-File-Manager"
        actions = QGridLayout()
        for index, (caption, glyph, path) in enumerate((
            ("图文使用指南", "book-open-variant", "/blob/main/docs/guide.zh-CN.md"),
            ("更新记录", "history", "/blob/main/CHANGELOG.md"),
            ("项目源码", "github", ""),
            ("反馈问题", "message-outline", "/issues/new"),
        )):
            actions.addWidget(button(caption, glyph, callback=lambda checked=False, path=path:
                                     QDesktopServices.openUrl(QUrl(project + path))), index // 2, index % 2)
        layout.addLayout(actions)
        copied = button("复制版本和环境", "content-copy")

        def copy_info() -> None:
            QApplication.clipboard().setText(f"{APP_NAME} {__version__}\n{environment}")
            copied.setText("已复制版本和环境")

        copied.clicked.connect(copy_info)
        layout.addWidget(copied, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch()
        return page

    def connections_page(self, profiles, add, edit, remove, preferences, save) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        layout.addWidget(label("存储桶", "title"))
        layout.addWidget(label("管理保存在本机的存储桶连接。", "muted", True))
        picker = Dropdown()
        picker.setAccessibleName("已保存的存储桶")
        picker.setSizeAdjustPolicy(Dropdown.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        layout.addWidget(picker)
        order = Dropdown()
        order.setAccessibleName("存储桶排序")
        for text, value in (("按添加顺序", "saved"), ("按连接名称", "name"), ("按服务商", "provider")):
            order.addItem(text, value)
        order.setCurrentIndex(max(0, order.findData(preferences.get("bucket_order", "saved"))))
        order_row = QHBoxLayout()
        order_row.addWidget(label("排序", "muted"))
        order_row.addWidget(order)
        order_row.addStretch()
        layout.addLayout(order_row)
        actions = QHBoxLayout()

        def refresh() -> None:
            selected = picker.currentData()
            picker.clear()
            for profile in profiles():
                picker.addItem(f"{profile['name']}   ·   {profile['bucket']}", profile["id"])
            picker.setCurrentIndex(max(0, picker.findData(selected)))
            picker.setPlaceholderText("暂无连接")
            edit_button.setEnabled(picker.count() > 0)
            remove_button.setEnabled(picker.count() > 0)

        def run(action, *args) -> None:
            action(*args)
            refresh()

        def sort() -> None:
            if save({"bucket_order": order.currentData()}):
                refresh()
            else:
                with QSignalBlocker(order):
                    order.setCurrentIndex(max(0, order.findData(preferences.get("bucket_order", "saved"))))

        order.currentIndexChanged.connect(sort)
        actions.addWidget(button("添加存储桶", "plus", kind="primary", callback=lambda: run(add)))
        edit_button = button("编辑", "pencil-outline", callback=lambda: run(edit, picker.currentData()))
        remove_button = button("移除连接", callback=lambda: run(remove, picker.currentData()))
        actions.addWidget(edit_button)
        actions.addWidget(remove_button)
        actions.addStretch()
        layout.addLayout(actions)
        layout.addStretch()
        refresh()
        return page

    @staticmethod
    def action_page(title: str, description: str, action: str, callback: Callable[[], None]) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        layout.addWidget(label(title, "title"))
        card = QFrame()
        card.setObjectName("settingsCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 16, 18, 16)
        card_layout.setSpacing(12)
        card_layout.addWidget(label(description, "muted", True))
        card_layout.addWidget(button(action, callback=callback), alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(card)
        layout.addStretch()
        return page

    def reject(self) -> None:
        if not self.updates.cancel_download():
            super().reject()
