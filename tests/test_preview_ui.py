"""Offline interaction tests for the desktop design preview.

The offscreen Qt platform is selected before importing Qt, so these tests never
open visible windows or submit credentials. All application content is fixture
data; socket connections and real desktop settings writes are blocked.
"""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QPushButton

from r2_batch_download import preview
from r2_batch_download.preview_data import TASKS


class PreviewUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt = QApplication.instance() or QApplication([])
        cls.qt.setQuitOnLastWindowClosed(False)
        cls.qt.setStyle("Fusion")
        cls.qt.setStyleSheet(preview.STYLE)

    def setUp(self) -> None:
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("Preview must not connect to a network")))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("Preview must not connect to a network")))
        self.enterContext(patch("r2_batch_download.desktop_config.save_settings", side_effect=AssertionError("Preview must not save real settings")))
        self.window = preview.PreviewWindow()
        self.addCleanup(self._destroy_window)
        # Keep transient toast timers out of navigation tests; the task and
        # selection state are still rendered by the production widgets.
        self.toast = self.enterContext(patch.object(self.window, "toast"))
        self.window.show()
        self._flush()

    def _flush(self) -> None:
        self.qt.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.qt.processEvents()

    def _destroy_window(self) -> None:
        self.window.close()
        self.window.deleteLater()
        self._flush()

    def _click(self, control: QPushButton) -> None:
        QTest.mouseClick(control, Qt.MouseButton.LeftButton)
        self._flush()

    def _file_card(self, key: str) -> preview.FileCard:
        cards = [
            self.window.grid.itemAt(index).widget()
            for index in range(self.window.grid.count())
        ]
        return next(card for card in cards if isinstance(card, preview.FileCard) and card.entry["key"] == key)

    def _task_action(self, task_name: str) -> QPushButton:
        for index in range(self.window.task_rows.count()):
            row = self.window.task_rows.itemAt(index).widget()
            if row is not None and any(child.text() == task_name for child in row.findChildren(preview.QLabel)):
                return next(control for control in row.findChildren(QPushButton) if control.toolTip().endswith("示例任务"))
        self.fail(f"No task action found for {task_name}")

    def _button_text(self, text: str) -> QPushButton:
        return next(control for control in self.window.findChildren(QPushButton) if control.text() == text)

    def test_double_click_folder_and_breadcrumb_return_to_root(self) -> None:
        folder = self._file_card("产品图片/")
        QTest.mouseDClick(folder, Qt.MouseButton.LeftButton, pos=QPoint(70, 55))
        self._flush()
        self.assertEqual(self.window.prefix, "产品图片/")
        self.assertEqual(len(self.window.visible_entries), 8)
        self.assertTrue(all(entry["key"].startswith("产品图片/") for entry in self.window.visible_entries))
        self.assertEqual(self.window.selected, set())
        crumbs = [
            self.window.breadcrumbs.itemAt(index).widget()
            for index in range(self.window.breadcrumbs.count())
        ]
        self.assertIn("产品图片", [crumb.text() for crumb in crumbs if isinstance(crumb, QPushButton)])
        root = next(crumb for crumb in crumbs if isinstance(crumb, QPushButton) and crumb.text() == "全部文件")
        self._click(root)
        self.assertEqual(self.window.prefix, "")
        self.assertEqual(sum(entry["kind"] == "folder" for entry in self.window.visible_entries), 3)

    def test_search_is_case_insensitive_and_covers_current_bucket(self) -> None:
        self.window.navigate("品牌视觉/")
        self.window.search.setFocus()
        QTest.keyClicks(self.window.search, "JPG")
        self._flush()
        uppercase = {entry["key"] for entry in self.window.visible_entries}
        self.assertIn("产品图片/产品正面.jpg", uppercase)
        self.assertIn("品牌视觉/品牌主视觉.jpg", uppercase)
        self.assertTrue(all(key.lower().endswith(".jpg") for key in uppercase))
        self.window.search.clear()
        QTest.keyClicks(self.window.search, "jPg")
        self._flush()
        self.assertEqual({entry["key"] for entry in self.window.visible_entries}, uppercase)
        self.window.search.clear()
        QTest.keyClicks(self.window.search, "no-such-example-file")
        self._flush()
        self.assertEqual(self.window.visible_entries, [])
        self.assertIs(self.window.files_stack.currentWidget(), self.window.empty)

    def test_selecting_folder_and_file_expands_download_scope_without_duplicates(self) -> None:
        folder_key = "品牌视觉/"
        root_file = "项目说明.pdf"
        self._click(self._file_card(folder_key).check)
        self._click(self._file_card(root_file).check)
        self.assertEqual(self.window.selected, {folder_key, root_file})
        selected = self.window.selected_files()
        selected_keys = [entry["key"] for entry in selected]
        self.assertEqual(len(selected_keys), 9)
        self.assertIn(root_file, selected_keys)
        self.assertTrue(all(key == root_file or key.startswith(folder_key) for key in selected_keys))
        # A retained explicit child selection must not create a second download
        # when its parent folder is already selected.
        self.window.card_selected("品牌视觉/品牌标志.png", True)
        self.assertEqual([entry["key"] for entry in self.window.selected_files()], selected_keys)
        self.assertEqual(len(set(selected_keys)), len(selected_keys))
        self.assertTrue(self.window.download_button.isEnabled())

    def test_grid_and_list_switch_preserve_selection_and_allow_ctrl_multiselect(self) -> None:
        initial = {"产品图片/", "项目说明.pdf"}
        for key in initial:
            self._click(self._file_card(key).check)
        self._click(self.window.list_button)
        self.assertEqual(self.window.selected, initial)
        table_keys = {
            self.window.table.item(index.row(), 0).data(Qt.ItemDataRole.UserRole)
            for index in self.window.table.selectionModel().selectedRows()
        }
        self.assertEqual(table_keys, initial)
        row = next(index for index, entry in enumerate(self.window.visible_entries) if entry["key"] == "素材归档.zip")
        position = self.window.table.visualItemRect(self.window.table.item(row, 0)).center()
        QTest.mouseClick(self.window.table.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, position)
        self._flush()
        selected = initial | {"素材归档.zip"}
        self.assertEqual(self.window.selected, selected)
        self._click(self.window.grid_button)
        self.assertEqual(self.window.selected, selected)
        self.assertTrue(all(self._file_card(key).check.isChecked() for key in selected))

    def test_changing_bucket_resets_navigation_and_selection_scope(self) -> None:
        self.window.navigate("视频素材/")
        self.window.search.setText("mp4")
        self._flush()
        self._click(self.window.select_button)
        self.assertTrue(self.window.selected)
        self.window.change_bucket("project-backups")
        self._flush()
        self.assertEqual(self.window.prefix, "")
        self.assertEqual(self.window.search.text(), "")
        self.assertFalse(self.window.selected)
        self.assertFalse(self.window.download_button.isEnabled())
        self.assertEqual(self.window.page_title.text(), "项目备份")
        self.assertTrue(all(entry["key"].startswith("2026-") or entry["key"] == "备份说明.pdf" for entry in self.window.visible_entries))

    def test_connection_dialog_validates_demo_masks_secret_and_exports_no_credentials(self) -> None:
        original_profiles = json.dumps(self.window.profiles, ensure_ascii=False)
        dialog = preview.ConnectionDialog(self.window)
        dialog.show()
        self._flush()
        self.assertEqual(dialog.fields["secret_access_key"].echoMode(), QLineEdit.EchoMode.Password)
        self.assertFalse(dialog.test_connection())
        demo_button = next(control for control in dialog.findChildren(QPushButton) if control.text() == "填入示例")
        self._click(demo_button)
        self.assertTrue(dialog.test_connection())
        self.assertIn("不会连接真实 R2", dialog.validation.text())
        secret = dialog.fields["secret_access_key"].text()
        self.assertTrue(secret)
        accepted = QSignalSpy(dialog.accepted)
        save = next(control for control in dialog.findChildren(QPushButton) if control.text() == "保存示例连接")
        self._click(save)
        self.assertEqual(accepted.count(), 1)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        profile = dialog.profile_values()
        self.assertEqual(set(profile), {"name", "bucket"})
        self.assertNotIn(secret, json.dumps(profile))
        self.assertEqual(json.dumps(self.window.profiles, ensure_ascii=False), original_profiles)
        dialog.deleteLater()
        self._flush()

    def test_task_pause_resume_and_failed_retry_update_preview_state(self) -> None:
        self._click(self.window.tasks_nav)
        running = next(task for task in self.window.tasks if task["status"] == "downloading")
        self._click(self._task_action(running["name"]))
        self.assertEqual(running["status"], "paused")
        self._click(self._task_action(running["name"]))
        self.assertEqual(running["status"], "downloading")
        failed = [task for task in self.window.tasks if task["status"] == "failed"]
        self.assertTrue(failed)
        original_completed = sum(task["status"] == "completed" for task in self.window.tasks)
        self._click(self._button_text("重试失败"))
        self.assertTrue(all(task["status"] == "queued" and not task["error"] for task in failed))
        self.assertEqual(sum(task["status"] == "completed" for task in self.window.tasks), original_completed)
        self._click(self.window.filter_buttons["failed"])
        self.assertEqual(self.window.task_rows.count(), 1)
        self.assertIn("暂无任务", self.window.task_rows.itemAt(0).widget().text())
        self.assertTrue(any(task["status"] == "failed" for task in TASKS))


if __name__ == "__main__":
    unittest.main()
