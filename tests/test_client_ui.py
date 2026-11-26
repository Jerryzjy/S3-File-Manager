"""Production Qt workflows with isolated state, no sockets, and no real dialogs."""
from __future__ import annotations

import copy
import csv
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, Qt, QUrl
from PySide6.QtGui import QCloseEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QLabel, QLineEdit, QPushButton

from r2_batch_download import DEFAULT_UPDATE_FEED, client, update_dialog


class _DeferredThread:
    instances: list["_DeferredThread"] = []

    def __init__(self, *, target: object, **kwargs: object) -> None:
        self.target = target
        self.started = False
        self.instances.append(self)

    def start(self) -> None:
        self.started = True

    def complete(self) -> None:
        self.target()


class _ImmediateStateWriter:
    """Isolate GUI assertions from writer scheduling tested in test_state_writer.

    The store remains the test's injected save function. No real disk or extra
    threads are introduced, and snapshots retain the production copy boundary.
    """

    def __init__(self, on_error: object, on_saved: object = None, save_fn: object = None) -> None:
        self.on_error = on_error
        self.on_saved = on_saved
        self.save_fn = save_fn
        self.is_idle = True
        self.is_closed = False
        self.saved_revision = 0
        self.completed_revision = 0

    def submit(self, profiles: list, tasks: list, preferences: dict) -> int:
        if self.is_closed:
            raise RuntimeError("writer already closed")
        revision = self.completed_revision + 1
        self.save_fn(*copy.deepcopy((profiles, tasks, preferences)))
        self.completed_revision = self.saved_revision = revision
        if self.on_saved:
            self.on_saved(revision)
        return revision

    def flush(self, timeout: float = 5) -> bool:
        return True

    def close(self, timeout: float = 0, flush: bool = True) -> bool:
        self.is_closed = True
        return True


class ClientUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt = QApplication.instance() or QApplication([])
        cls.qt.setQuitOnLastWindowClosed(False)
        cls.qt.setStyle("Fusion")
        cls.qt.setStyleSheet(client.app_style())

    def setUp(self) -> None:
        self.state = {
            "profiles": [], "tasks": [],
            "preferences": {"output_dir": "D:/not-written-by-ui-tests", "workers": 4, "skip_existing": True},
        }
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("No network allowed in UI tests")))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("No network allowed in UI tests")))
        self.load = self.enterContext(patch.object(client, "load_state", side_effect=lambda: copy.deepcopy(self.state)))
        self.saved: list[tuple[list[dict], list[dict], dict]] = []
        self.save = self.enterContext(patch.object(client, "save_state", side_effect=lambda profiles, tasks, preferences: self.saved.append(copy.deepcopy((profiles, tasks, preferences)))))
        self.question = self.enterContext(patch.object(client.QMessageBox, "question", return_value=client.QMessageBox.StandardButton.Yes))
        self.warning = self.enterContext(patch.object(client.QMessageBox, "warning"))
        self.information = self.enterContext(patch.object(client.QMessageBox, "information"))
        self.create_client = self.enterContext(patch.object(client, "create_client", side_effect=AssertionError("SDK client must be mocked explicitly")))
        self.list_objects = self.enterContext(patch.object(client, "list_objects", side_effect=AssertionError("Listing must be mocked explicitly")))
        self.transfer = self.enterContext(patch.object(client, "transfer_tasks", side_effect=AssertionError("Transfers must be mocked explicitly")))
        self.enterContext(patch.object(client, "StateWriter", _ImmediateStateWriter))
        _DeferredThread.instances = []
        self.enterContext(patch.object(client.threading, "Thread", _DeferredThread))
        self.window: client.ClientWindow | None = None
        self.addCleanup(self._destroy_window)

    def _make_window(self) -> client.ClientWindow:
        self.window = client.ClientWindow()
        # Timer delivery is controlled explicitly; no background request can
        # run on a later test's state or unexpectedly persist on a timer tick.
        self.window.timer.stop()
        self.window.show()
        self.toast = self.enterContext(patch.object(self.window, "toast"))
        self._flush()
        return self.window

    def _flush(self) -> None:
        self.qt.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.qt.processEvents()

    def _destroy_window(self) -> None:
        if self.window is not None:
            self.window.transfer_running = False
            self.window.loading.clear()
            self.window.close()
            self._flush()
            self.window.deleteLater()
            self._flush()

    @staticmethod
    def _profile(profile_id: str = "profile-one") -> dict:
        # An unremembered secret prevents constructor auto-refresh; tests set
        # actual fixture credentials only in the connection-probe case below.
        return {
            "id": profile_id, "name": "我的存储桶", "bucket": "my-real-bucket",
            "account_id": "abc123", "access_key_id": "test-access-key", "secret_access_key": "",
            "endpoint_url": "", "remember_secret": False,
        }

    @staticmethod
    def _task(task_id: str, state: str, path: str = "") -> dict:
        return {
            "id": task_id, "key": f"folder/{task_id}.txt", "name": f"{task_id}.txt",
            "bucket": "my-real-bucket", "profile_id": "profile-one", "status": state,
            "size": 5, "transferred": 5 if state in {"completed", "skipped"} else 0,
            "path": path, "output_dir": str(Path(path).parent) if path else "D:/not-written-by-ui-tests",
            "workers": 4, "overwrite": False, "strip_prefix": False, "prefix": "", "error": "",
        }

    def test_first_launch_is_empty_and_shows_add_connection_instead_of_fixtures(self) -> None:
        window = self._make_window()
        self.assertEqual(window.profiles, [])
        self.assertEqual(window.tasks, [])
        self.assertEqual(window.files, {})
        self.assertEqual(window.visible_entries, [])
        self.assertEqual(window.bucket_id, "")
        self.assertIs(window.files_stack.currentWidget(), window.empty)
        self.assertIn("第一个存储桶", window.empty_title.text())
        self.assertEqual(window.empty_action.text(), "添加存储桶")
        self.assertTrue(window.empty_action.isEnabled())
        self.assertFalse(window.download_button.isEnabled())
        self.assertFalse(window.download_all_button.isEnabled())
        self.assertEqual(window.download_all_button.text(), "下载整桶")
        self.assertFalse(any("示例" in child.text() or "演示" in child.text() for child in window.findChildren(QLabel)))
        self.assertEqual(_DeferredThread.instances, [])
        self.create_client.assert_not_called()

    def test_direction_dropdown_previews_hover_and_accepts_mouse_and_keyboard(self) -> None:
        window = self._make_window()
        window.tasks_nav.click()
        picker = window.direction_picker
        QTest.mouseClick(picker, Qt.MouseButton.LeftButton)
        view = picker.view()
        self.assertTrue(view.isVisible())
        target = view.model().index(1, 0)
        QTest.mouseMove(view.viewport(), view.visualRect(target).center())
        self.assertEqual(view.currentIndex(), target)
        self.assertIsNone(picker.currentData())
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualRect(target).center())
        self.assertEqual(picker.currentData(), "download")
        self.assertFalse(view.isVisible())

        picker.showPopup()
        QTest.keyClick(view, Qt.Key.Key_Down)
        QTest.keyClick(view, Qt.Key.Key_Return)
        self.assertEqual(picker.currentData(), "upload")
        picker.showPopup()
        QTest.keyClick(view, Qt.Key.Key_Down)
        QTest.keyClick(view, Qt.Key.Key_Escape)
        self.assertFalse(view.isVisible())
        self.assertEqual(picker.currentData(), "upload")

    def test_more_actions_opens_dialog_and_routes_selected_action(self) -> None:
        self.state["profiles"] = [self._profile()]
        window = self._make_window()
        window.files[window.bucket_id] = client.normalize_objects([{"Key": "report.csv", "Size": 128}])
        window.loaded.add(window.bucket_id)
        window.render_files()
        window.selected = {"report.csv"}
        window.update_selection()
        self.assertTrue(window.file_actions.isEnabled())

        def choose(dialog: QDialog) -> int:
            dialog.show()
            operations = {control.text(): control for control in dialog.findChildren(QPushButton)}
            self.assertTrue(operations["增量归档"].isEnabled())
            self.assertTrue(operations["复制到桶内目录"].isEnabled())
            control = operations["生成限时下载链接"]
            self.assertTrue(control.isEnabled())
            QTest.mouseClick(control, Qt.MouseButton.LeftButton)
            return dialog.result()

        with (patch.object(QDialog, "exec", choose), patch.object(window, "show_download_link") as operation):
            window.file_actions.click()
        operation.assert_called_once_with()

    def test_batch_preparation_failure_unblocks_main_window(self) -> None:
        window = self._make_window()
        connection = client.connection_of(self._profile() | {"secret_access_key": "test-secret-key"})
        task_builder = Mock(side_effect=ValueError("test-secret-key"))
        window.prepare_tasks(501, task_builder, connection)
        self.assertTrue(window.preparing)
        _DeferredThread.instances[-1].complete()
        window.poll()
        self.assertFalse(window.preparing)
        self.assertEqual(window.tasks, [])
        self.assertNotIn("test-secret-key", self.warning.call_args.args[-1])

    def test_settings_checks_official_source_and_stages_windows_install(self) -> None:
        self.state["preferences"]["update_feed_url"] = "https://legacy.example.test/update.json"
        window = self._make_window()
        release = client.UpdateRelease("1.3.0", "https://example.test/application.exe", "a" * 64, 4096, "版本说明")
        with tempfile.TemporaryDirectory() as temporary:
            staged = Path(temporary) / "update.exe"
            staged.touch()

            def exercise(dialog: client.SettingsDialog) -> int:
                dialog.show()
                navigation = next(item for item in dialog.findChildren(QPushButton) if item.text() == "软件更新")
                navigation.click()
                updates = dialog.updates
                self.assertTrue(updates.isVisible())
                self.assertFalse(updates.findChildren(QLineEdit))
                self.assertEqual(_DeferredThread.instances, [])
                updates.check_button.click()
                _DeferredThread.instances[-1].complete()
                updates.poll()
                self.assertIn("发现新版 v1.3.0", updates.status.text())
                self.assertEqual(updates.notes.toPlainText(), release.notes)

                window.transfer_running = True
                updates.install_button.click()
                self.assertIn("请先暂停", updates.status.text())
                self.assertEqual(len(_DeferredThread.instances), 1)
                window.transfer_running = False
                updates.install_button.click()
                self.assertTrue(updates.progress.isVisible())
                updates.cancel_button.click()
                _DeferredThread.instances[-1].complete()
                updates.poll()
                self.assertEqual(updates.status.text(), "已取消下载。")
                self.assertFalse(staged.exists())
                staged.touch()
                updates.install_button.click()
                _DeferredThread.instances[-1].complete()
                updates.poll()
                return dialog.result()

            with (patch.object(client.SettingsDialog, "exec", exercise),
                  patch.object(update_dialog, "fetch_update", return_value=release) as fetch,
                  patch.object(update_dialog, "download_update", return_value=staged),
                  patch.object(update_dialog.sys, "frozen", True, create=True),
                  patch.object(update_dialog.sys, "platform", "win32")):
                window.settings_button.click()

            fetch.assert_called_once_with(DEFAULT_UPDATE_FEED, client.VERSION)
            self.assertEqual(window._update_pending, (staged, release.sha256, window.writer.saved_revision))
            self.assertEqual(self.saved[-1][2]["update_feed_url"], "https://legacy.example.test/update.json")
            window._update_pending = None

    def test_upload_tab_uses_the_selected_bucket_and_existing_upload_preflight(self) -> None:
        self.state["profiles"] = [self._profile(), {
            **self._profile("b2-profile"), "provider": "b2", "bucket": "b2-test-bucket",
        }]
        window = self._make_window()
        window.navigate("reports/")
        self.assertEqual(window.download_all_button.text(), "下载目录")
        window.upload_nav.click()
        self.assertEqual(window.pages.currentIndex(), client.UPLOAD_PAGE)
        self.assertTrue(window.upload_nav.isChecked())
        self.assertFalse(window.files_nav.isChecked())
        self.assertEqual(window.upload_target.text(), "目标位置  r2://my-real-bucket/reports/")

        window.current_profile()["secret_access_key"] = "test-secret-key"
        source = Path("report.csv")
        selected = Mock()
        selected.exec.return_value = QDialog.DialogCode.Rejected
        with (patch.object(client, "choose_upload_sources", return_value=(source,)) as choose,
              patch.object(client, "UploadDialog", return_value=selected) as preview):
            window.upload_choose_files.click()
        choose.assert_called_once_with(window, folder=False)
        preview.assert_called_once_with(window, client.connection_of(window.current_profile()), "reports/")
        selected.check_sources.assert_called_once_with((source,))

        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(source.resolve()))])
        enter = Mock()
        enter.mimeData.return_value = mime
        drop = Mock()
        drop.mimeData.return_value = mime
        selected.check_sources.reset_mock()
        with patch.object(client, "UploadDialog", return_value=selected):
            window.upload_drop.dragEnterEvent(enter)
            enter.accept.assert_called_once()
            window.upload_drop.dropEvent(drop)
            drop.accept.assert_called_once()
            selected.check_sources.assert_not_called()
            self._flush()
        selected.check_sources.assert_called_once_with((source.resolve(),))

        window.change_bucket("b2-profile")
        self.assertEqual(window.pages.currentIndex(), client.UPLOAD_PAGE)
        self.assertFalse(window.upload_drop.acceptDrops())
        self.assertTrue(window.upload_connect.isVisible())

        self.list_objects.assert_not_called()
        self.transfer.assert_not_called()

    def test_download_creation_waits_until_clear_operation_finishes(self) -> None:
        window = self._make_window()
        window.clear_when_idle = True
        with patch.object(client, "DownloadDialog") as dialog:
            window.create_download()
        dialog.assert_not_called()
        self.assertEqual(window.tasks, [])
        self.toast.assert_called_once()

    def test_batch_actions_and_failure_csv_respect_batch_scope(self) -> None:
        self.state["profiles"] = [self._profile()]
        self.state["tasks"] = [
            dict(self._task("waiting", "queued"), batch_id="one"),
            dict(self._task("failed", "failed"), batch_id="one", key="=1+1", error="连接中断"),
            dict(self._task("complete", "completed"), batch_id="one"),
            dict(self._task("other", "queued"), batch_id="two"),
            self._task("legacy", "interrupted"),
        ]
        window = self._make_window()
        window.profiles[0]["secret_access_key"] = "test-s3-secret"
        window.switch_page(1)
        window.batch_selector.setCurrentIndex(window.batch_selector.findData("one"))
        self.assertEqual(set(window.task_widgets), {"waiting", "failed", "complete"})
        self.assertEqual(window.batch_progress.value(), 333)
        window.batch_pause.click()
        self.assertEqual(window.task_by_id["waiting"]["status"], "paused")
        self.assertEqual(window.task_by_id["other"]["status"], "queued")
        window.batch_resume.click()
        self.assertEqual(window.task_by_id["waiting"]["status"], "queued")
        window.handle_event({"id": "other", "kind": "progress", "bytes": 5})
        window.handle_event({"id": "waiting", "kind": "progress", "bytes": 2})
        window.render_tasks()
        self.assertEqual(window.batch_progress.value(), 467)

        with tempfile.TemporaryDirectory() as destination:
            path = Path(destination) / "failures.csv"
            with patch.object(client.QFileDialog, "getSaveFileName", return_value=(str(path), "")):
                window.batch_export.click()
            _DeferredThread.instances[-1].complete()
            # Deliver the export event without polling and starting a queued transfer.
            window.handle_event(window.events.get_nowait())
            with path.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.reader(stream))
            self.assertEqual(rows, [["存储桶", "对象 Key", "任务状态", "失败原因"],
                                    ["my-real-bucket", "'=1+1", "失败", "连接中断"]])

        window.batch_retry.click()
        self.assertEqual(window.task_by_id["failed"]["status"], "queued")
        self.assertEqual(window.task_by_id["complete"]["status"], "completed")
        self.assertEqual(window.task_by_id["legacy"]["status"], "interrupted")
        window.batch_selector.setCurrentIndex(window.batch_selector.findData(""))
        self.assertEqual(list(window.task_widgets), ["legacy"])

    def test_bucketwide_search_disables_directory_scope_download(self) -> None:
        self.state["profiles"] = [self._profile()]
        window = self._make_window()
        window.loaded.add(window.bucket_id)
        window.files[window.bucket_id] = [{"key": "folder/target.jpg", "size": 5, "kind": "file", "modified": ""}]
        window.navigate("folder/")
        self.assertTrue(window.download_all_button.isEnabled())
        window.search.setText("target")
        QTest.qWait(280)
        self._flush()
        self.assertFalse(window.download_all_button.isEnabled())
        self.assertIn("搜索", window.download_all_button.toolTip())

    def test_directory_filters_sort_before_pagination_and_limit_download_scope(self) -> None:
        self.state["profiles"] = [self._profile()]
        window = self._make_window()
        window.PAGE_SIZE = 1
        window.loaded.add(window.bucket_id)
        window.files[window.bucket_id] = [
            {"key": "folder/z-small.PDF", "size": 20, "modified": "2026-11-25T23:30:00Z"},
            {"key": "folder/sub/a-large.pdf", "size": 500, "modified": "2026-11-26T00:30:00+01:00"},
            {"key": "folder/sub/old.pdf", "size": 90, "modified": "2026-11-24T00:00:00Z"},
            {"key": "folder/sub/ignore.png", "size": 60, "modified": "2026-11-25T00:00:00Z"},
            {"key": "folder-similar/outside.pdf", "size": 70, "modified": "2026-11-25T00:00:00Z"},
        ]
        window.navigate("folder/")
        window.search_scope.setCurrentIndex(1)
        dialog = Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        day = client.date(2026, 11, 25)
        dialog.filters = client.FileFilters((".pdf",), 20, 500, day, day)
        with patch.object(client, "FileFilterDialog", return_value=dialog):
            window.filter_button.click()
        window.set_view("list")
        header = window.table.horizontalHeader()
        header.sectionClicked.emit(1)
        self.assertEqual([entry["key"] for entry in window.visible_entries], ["folder/z-small.PDF"])
        window.toggle_select_all()
        window.file_next.click()
        self.assertEqual([entry["key"] for entry in window.visible_entries], ["folder/sub/a-large.pdf"])
        window.toggle_select_all()
        header.sectionClicked.emit(1)
        self.assertEqual([entry["key"] for entry in window.visible_entries], ["folder/sub/a-large.pdf"])
        self.assertEqual({entry["key"] for entry in window.selected_files()},
                         {"folder/z-small.PDF", "folder/sub/a-large.pdf"})
        self.assertFalse(window.download_all_button.isEnabled())
        with patch.object(client, "DownloadDialog") as download:
            window.create_download(whole_directory=True)
        download.assert_not_called()
        window.search_scope.setCurrentIndex(0)
        self.assertFalse(window.selected)
        self.assertIn("folder-similar/outside.pdf", {entry["key"] for entry in window.all_entries})

    def test_file_details_expose_literal_key_and_copy_it(self) -> None:
        self.state["profiles"] = [self._profile()]
        window = self._make_window()
        window.loaded.add(window.bucket_id)
        key = "folder/literal <tag> & name.txt"
        window.files[window.bucket_id] = [{"key": key, "size": 2500, "ETag": '"sample"'}]
        window.navigate("folder/")
        self.assertFalse(window.details_button.isEnabled())
        window.toggle_select_all()
        self.assertTrue(window.details_button.isEnabled())
        clipboard = Mock()

        def inspect_detail(dialog):
            fields = {field.text() for field in dialog.findChildren(QLineEdit)}
            self.assertTrue({key, '"sample"', "my-real-bucket"}.issubset(fields))
            action = next(button for button in dialog.findChildren(QPushButton) if button.text() == "复制对象 Key")
            action.click()

        with patch.object(QDialog, "exec", inspect_detail), patch.object(QApplication, "clipboard", return_value=clipboard):
            window.details_button.click()
        clipboard.setText.assert_called_once_with(key)

    def test_remove_connection_persists_no_profile_cancels_scan_and_never_deletes_remote_data(self) -> None:
        self.state["profiles"] = [self._profile()]
        self.state["tasks"] = [self._task("finished", "completed")]
        window = self._make_window()
        window.files["profile-one"] = [{"key": "real.txt", "size": 5, "modified": "", "kind": "file"}]
        window.loaded.add("profile-one")
        pending_scan = threading.Event()
        window.loading["profile-one"] = pending_scan
        window.scan_tokens["profile-one"] = "pending-scan"
        window.remove_connection("profile-one")
        self._flush()
        self.assertEqual(window.profiles, [])
        self.assertEqual(window.bucket_id, "")
        self.assertNotIn("profile-one", window.files)
        self.assertNotIn("profile-one", window.loaded)
        self.assertNotIn("profile-one", window.scan_tokens)
        self.assertTrue(pending_scan.is_set())
        self.assertEqual(self.saved[-1][0], [])
        self.assertEqual([task["id"] for task in self.saved[-1][1]], ["finished"])
        self.assertIn("远端存储桶和文件将保留", self.question.call_args.args[2])
        self.create_client.assert_not_called()
        self.list_objects.assert_not_called()
        self.transfer.assert_not_called()

    def test_active_task_blocks_connection_removal_without_side_effects(self) -> None:
        self.state["profiles"] = [self._profile()]
        self.state["tasks"] = [self._task("running", "downloading")]
        window = self._make_window()
        window.remove_connection("profile-one")
        self.assertEqual([profile["id"] for profile in window.profiles], ["profile-one"])
        self.assertFalse(self.saved)
        self.information.assert_called_once()
        self.question.assert_not_called()
        self.create_client.assert_not_called()

    def test_clear_all_finished_records_persists_empty_list_and_keeps_downloaded_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            downloaded = Path(directory) / "downloaded.txt"
            downloaded.write_bytes(b"saved")
            self.state["tasks"] = [self._task("done", "completed", str(downloaded)), self._task("failed", "failed")]
            window = self._make_window()
            window.clear_tasks()
            self.assertEqual(window.tasks, [])
            self.assertEqual(self.saved[-1][1], [])
            self.assertEqual(downloaded.read_bytes(), b"saved")
            self.assertEqual(len(list(Path(directory).iterdir())), 1)
            self.assertEqual(window.task_rows.count(), 1)
            self.assertIn("还没有", window.task_rows.itemAt(0).widget().text())
            self.create_client.assert_not_called()
            self.transfer.assert_not_called()

    def test_clear_completed_keeps_failed_records_and_never_unlinks_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            downloaded = Path(directory) / "downloaded.txt"
            downloaded.write_bytes(b"saved")
            self.state["tasks"] = [
                self._task("done", "completed", str(downloaded)), self._task("skip", "skipped", str(downloaded)),
                self._task("failed", "failed"), self._task("cancelled", "cancelled"),
            ]
            window = self._make_window()
            window.clear_completed()
            self.assertEqual([task["id"] for task in window.tasks], ["failed", "cancelled"])
            self.assertEqual([task["id"] for task in self.saved[-1][1]], ["failed", "cancelled"])
            self.assertEqual(downloaded.read_bytes(), b"saved")
            self.question.assert_not_called()

    def test_clear_active_tasks_requests_cancel_and_waits_for_batch_done_before_emptying(self) -> None:
        self.state["tasks"] = [self._task("running", "downloading"), self._task("waiting", "queued")]
        window = self._make_window()
        window.transfer_running = True
        window.in_flight = {"running"}
        window.cancel_events = {task["id"]: threading.Event() for task in window.tasks}
        window.task_connections = {task["id"]: object() for task in window.tasks}
        running_cancellation = window.cancel_events["running"]
        undispatched_cancellation = window.cancel_events["waiting"]
        window.clear_tasks()
        self.assertTrue(window.clear_when_idle)
        self.assertEqual(len(window.tasks), 2)
        self.assertTrue(running_cancellation.is_set())
        self.assertFalse(undispatched_cancellation.is_set())
        self.assertEqual([task["status"] for task in window.tasks], ["cancelling", "cancelled"])
        self.assertFalse(self.saved)
        # Late worker events may still arrive; no records are removed while
        # workers might be writing files or cleaning temporary data.
        window.handle_event({"kind": "result", "id": "running", "status": "cancelled", "transferred": 0})
        self.assertEqual(len(window.tasks), 2)
        window.handle_event({"kind": "batch_done", "ids": ["running", "waiting"]})
        window.render_tasks()
        window.last_persist = 0
        window.poll()
        self.assertFalse(window.transfer_running)
        self.assertFalse(window.clear_when_idle)
        self.assertEqual(window.tasks, [])
        self.assertEqual(window.cancel_events, {})
        self.assertEqual(window.task_connections, {})
        self.assertEqual(self.saved[-1][1], [])

    def test_clear_preserves_multipart_record_until_remote_cleanup_succeeds(self) -> None:
        task = self._task("paused-upload", "paused")
        task.update(direction="upload", size=64 * 1024**2, transferred=16 * 1024**2)
        self.state["tasks"] = [task]
        window = self._make_window()
        with patch.object(client, "discard_upload_checkpoint", side_effect=[OSError("offline"), False]) as cleanup:
            window.clear_tasks()
            self.assertTrue(window.clear_when_idle)
            self.assertEqual(set(window.upload_cleaning), {"paused-upload"})
            _DeferredThread.instances[-1].complete()
            window.handle_event(window.events.get_nowait())
            self.assertFalse(window.clear_when_idle)
            self.assertEqual(window.tasks[0]["status"], "failed")
            self.assertIn("远端分片清理失败", self.saved[-1][1][0]["error"])
            window.remove_task("paused-upload")
            _DeferredThread.instances[-1].complete()
            window.handle_event(window.events.get_nowait())
        self.assertEqual(cleanup.call_count, 2)
        self.assertEqual(window.tasks, [])
        self.assertEqual(self.saved[-1][1], [])

    def test_stale_scan_result_cannot_replace_new_scan_or_restore_removed_connection(self) -> None:
        self.state["profiles"] = [self._profile()]
        window = self._make_window()
        window.scan_tokens["profile-one"] = "new-scan"
        window.loading["profile-one"] = threading.Event()
        fresh = [{"key": "fresh.txt", "size": 5, "modified": "", "kind": "file"}]
        window.handle_event({"kind": "scan_done", "profile_id": "profile-one", "token": "old-scan", "objects": [{"key": "stale.txt", "size": 9}]})
        self.assertNotIn("profile-one", window.files)
        self.assertIn("profile-one", window.loading)
        window.handle_event({"kind": "scan_done", "profile_id": "profile-one", "token": "new-scan", "objects": fresh})
        self.assertEqual(window.files["profile-one"], fresh)
        self.assertIn("profile-one", window.loaded)
        window.remove_connection("profile-one")
        window.handle_event({"kind": "scan_done", "profile_id": "profile-one", "token": "new-scan", "objects": fresh})
        self.assertEqual(window.profiles, [])
        self.assertEqual(window.files, {})
        self.assertEqual(window.loaded, set())

    def test_120_item_pages_keep_full_catalog_and_page_selection_without_silent_truncation(self) -> None:
        self.state["profiles"] = [self._profile()]
        window = self._make_window()
        catalog = [{"key": f"file-{index:04d}.txt", "size": index + 1, "modified": "", "kind": "file"} for index in range(241)]
        window.files["profile-one"] = catalog
        window.loaded.add("profile-one")
        window.render_files()
        self.assertEqual(len(window.all_entries), 241)
        self.assertEqual(len(window.visible_entries), 120)
        self.assertEqual(window.page_label.text(), "1 / 3")
        self.assertIn("241", window.selection_note.text())
        self.assertEqual(window.select_button.text(), "全选本页")
        QTest.mouseClick(window.select_button, Qt.MouseButton.LeftButton)
        self._flush()
        self.assertEqual(len(window.selected_files()), 120)
        QTest.mouseClick(window.file_next, Qt.MouseButton.LeftButton)
        self._flush()
        self.assertEqual(window.page_label.text(), "2 / 3")
        self.assertEqual(len(window.visible_entries), 120)
        self.assertEqual(len(window.selected_files()), 120)
        self.assertTrue(all(entry["key"] not in window.selected for entry in window.visible_entries))
        QTest.mouseClick(window.select_button, Qt.MouseButton.LeftButton)
        self._flush()
        self.assertEqual(len(window.selected_files()), 240)
        QTest.mouseClick(window.file_next, Qt.MouseButton.LeftButton)
        self._flush()
        self.assertEqual(window.page_label.text(), "3 / 3")
        self.assertEqual(len(window.visible_entries), 1)
        QTest.mouseClick(window.select_button, Qt.MouseButton.LeftButton)
        self._flush()
        self.assertEqual(len(window.selected_files()), 241)
        self.assertEqual({entry["key"] for entry in window.selected_files()}, {entry["key"] for entry in catalog})
        self.assertFalse(window.file_next.isEnabled())

    def test_connection_save_waits_for_real_probe_result_and_masks_credentials(self) -> None:
        window = self._make_window()
        dialog = client.ConnectionDialog(window)
        dialog.timer.stop()
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.fields["secret_access_key"].echoMode(), QLineEdit.EchoMode.Password)
        dialog.save()
        self.assertIn("连接名称", dialog.validation.text())
        self.assertEqual(_DeferredThread.instances, [])
        for name, value in {
            "name": "我的连接", "bucket": "bucket", "account_id": "abc123",
            "access_key_id": "test-access-key", "secret_access_key": "test-secret-key",
        }.items():
            dialog.fields[name].setText(value)
        sdk = Mock()
        sdk.list_objects_v2.return_value = {"Contents": []}
        self.create_client.side_effect = None
        self.create_client.return_value = sdk
        accepted = QSignalSpy(dialog.accepted)
        dialog.save()
        self.assertTrue(dialog.testing)
        self.assertEqual(accepted.count(), 0)
        self.assertEqual(len(_DeferredThread.instances), 1)
        self.assertTrue(_DeferredThread.instances[0].started)
        _DeferredThread.instances[0].complete()
        self.assertEqual(accepted.count(), 0)
        dialog.poll()
        sdk.list_objects_v2.assert_called_once_with(Bucket="bucket", MaxKeys=1)
        sdk.close.assert_called_once()
        self.assertEqual(accepted.count(), 1)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(self.saved, [])
        self.assertNotIn("test-secret-key", dialog.validation.text())

    def test_connection_wizard_preserves_fields_and_validates_public_address(self) -> None:
        window = self._make_window()
        dialog = client.ConnectionDialog(window)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.steps.currentIndex(), 0)
        dialog.next_button.click()
        self.assertEqual(dialog.steps.currentIndex(), 1)
        dialog.next_button.click()
        self.assertIn("连接名称", dialog.validation.text())
        for field, value in {"name": "测试桶", "bucket": "test-bucket", "account_id": "abcdef",
                             "access_key_id": "access-test", "secret_access_key": "private-test"}.items():
            dialog.fields[field].setText(value)
        dialog.next_button.click()
        self.assertEqual(dialog.steps.currentIndex(), 2)
        dialog.public_url.setText("https://user@public.example.com")
        dialog.next_button.click()
        self.assertEqual(dialog.steps.currentIndex(), 2)
        dialog.public_url.setText("https://public.example.com")
        dialog.next_button.click()
        self.assertEqual(dialog.steps.currentIndex(), 3)
        self.assertIn("public.example.com", dialog.review.text())
        self.assertNotIn("private-test", dialog.review.text())
        dialog.previous_button.click()
        self.assertEqual(dialog.public_url.text(), "https://public.example.com")
        self.assertEqual(_DeferredThread.instances, [])

    def test_direction_filter_and_display_settings_affect_the_desktop(self) -> None:
        self.state["profiles"] = [self._profile()]
        self.state["tasks"] = [self._task("one", "failed"), {**self._task("two", "paused"), "direction": "upload"}]
        window = self._make_window()
        window.tasks_nav.click()
        window.direction_picker.setCurrentIndex(window.direction_picker.findData("upload"))
        self.assertEqual(list(window.task_widgets), ["two"])
        window.direction_picker.setCurrentIndex(0)
        self._flush()
        self.assertTrue(window.task_widgets["one"]["failure"].isVisible())

        def exercise(dialog: client.SettingsDialog) -> int:
            navigation = next(item for item in dialog.findChildren(QPushButton) if item.text() == "文件显示")
            navigation.click()
            fields = {item.accessibleName(): item for item in dialog.findChildren(QComboBox)}
            fields["每页显示"].setCurrentIndex(fields["每页显示"].findData(60))
            fields["默认视图"].setCurrentIndex(fields["默认视图"].findData("list"))
            panel = fields["默认视图"].parentWidget().parentWidget()
            save = next(item for item in panel.findChildren(QPushButton) if item.text() == "保存设置")
            save.click()
            return int(QDialog.DialogCode.Rejected)

        with patch.object(client.SettingsDialog, "exec", exercise):
            window.settings_button.click()
        self.assertEqual(window.PAGE_SIZE, 60)
        self.assertEqual(window.view_mode, "list")
        self.assertEqual(self.saved[-1][2]["file_view"], "list")

    def test_cancelled_queued_task_cannot_retry_until_previous_batch_releases_ownership(self) -> None:
        self.state["profiles"] = [self._profile()]
        self.state["tasks"] = [self._task("waiting", "queued")]
        window = self._make_window()
        window.profiles[0]["secret_access_key"] = "test-secret-key"
        connection = client.connection_of(window.profiles[0])
        window.task_connections["waiting"] = connection
        old_cancel = threading.Event()
        window.cancel_events["waiting"] = old_cancel
        window.dispatch()
        self.assertTrue(window.transfer_running)
        self.assertIn("waiting", window.in_flight)
        self.assertEqual(len(_DeferredThread.instances), 1)
        window.cancel_task("waiting")
        self.assertEqual(window.tasks[0]["status"], "cancelling")
        self.assertTrue(old_cancel.is_set())
        # A cancelled queue entry can still belong to the old worker batch.
        # Retrying now must not replace the event that that worker observes.
        window.retry_task("waiting")
        self.assertEqual(window.tasks[0]["status"], "cancelling")
        self.assertIs(window.cancel_events["waiting"], old_cancel)
        self.assertEqual(len(_DeferredThread.instances), 1)
        window.handle_event({"kind": "batch_done", "ids": ["waiting"]})
        self.assertNotIn("waiting", window.in_flight)
        with patch.object(window, "dispatch") as dispatch:
            window.retry_task("waiting")
        self.assertEqual(window.tasks[0]["status"], "queued")
        self.assertIsNot(window.cancel_events["waiting"], old_cancel)
        self.assertFalse(window.cancel_events["waiting"].is_set())
        dispatch.assert_called_once()

    def test_cancelled_queued_task_cannot_be_removed_until_previous_batch_done(self) -> None:
        self.state["profiles"] = [self._profile()]
        self.state["tasks"] = [self._task("waiting", "queued")]
        window = self._make_window()
        window.profiles[0]["secret_access_key"] = "test-secret-key"
        window.task_connections["waiting"] = client.connection_of(window.profiles[0])
        window.cancel_events["waiting"] = threading.Event()
        window.dispatch()
        window.cancel_task("waiting")
        window.remove_task("waiting")
        self.assertEqual([task["id"] for task in window.tasks], ["waiting"])
        self.assertEqual([task["id"] for task in self.saved[-1][1]], ["waiting"])
        window.handle_event({"kind": "batch_done", "ids": ["waiting"]})
        window.remove_task("waiting")
        self.assertEqual(window.tasks, [])
        self.assertEqual(self.saved[-1][1], [])

    def test_bucketwide_search_download_does_not_apply_previous_folder_prefix(self) -> None:
        self.state["profiles"] = [self._profile()]
        window = self._make_window()
        window.profiles[0]["secret_access_key"] = "test-secret-key"
        window.files["profile-one"] = [
            {"key": "folderA/notes.txt", "size": 11, "modified": "", "kind": "file"},
            {"key": "folderB/target.jpg", "size": 22, "modified": "", "kind": "file"},
        ]
        window.loaded.add("profile-one")
        window.navigate("folderA/")
        window.search.setText("target.jpg")
        QTest.qWait(280)
        self._flush()
        self.assertEqual(window.prefix, "folderA/")
        self.assertEqual([entry["key"] for entry in window.visible_entries], ["folderB/target.jpg"])
        window.card_selected("folderB/target.jpg", True)
        dialog = Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.preflight.output = Path("D:/not-written-by-ui-tests")
        dialog.workers.currentData.return_value = 4
        dialog.skip.isChecked.return_value = True
        # Even if this checkbox were remembered as true by a dialog, the
        # empty search-download prefix must prevent stripping folderA/.
        dialog.strip.isChecked.return_value = True
        def ready_download(parent, preferences, prefix, *, select_entries, scope):
            dialog.entries = select_entries(threading.Event())
            return dialog

        with patch.object(client, "DownloadDialog", side_effect=ready_download) as download_dialog, patch.object(window, "dispatch") as dispatch:
            window.create_download()
        self.assertEqual(download_dialog.call_args.args[2], "")
        self.assertEqual(len(window.tasks), 1)
        task = window.tasks[0]
        self.assertEqual(task["key"], "folderB/target.jpg")
        self.assertEqual(task["prefix"], "")
        self.assertFalse(task["strip_prefix"])
        self.assertEqual(self.saved[-1][1][0]["prefix"], "")
        self.assertFalse(self.saved[-1][1][0]["strip_prefix"])
        dispatch.assert_called_once()
        self.create_client.assert_not_called()
        self.transfer.assert_not_called()

    def test_queued_task_can_pause_without_starting_a_transfer(self) -> None:
        self.state["tasks"] = [self._task("waiting", "queued")]
        window = self._make_window()
        window.switch_page(1)
        pause = window.task_widgets["waiting"]["pause"]
        self.assertTrue(pause.isEnabled())
        QTest.mouseClick(pause, Qt.MouseButton.LeftButton)
        self._flush()
        self.assertEqual(window.tasks[0]["status"], "paused")
        self.assertTrue(window.pause_events["waiting"].is_set())
        self.assertFalse(window.transfer_running)
        self.assertNotIn("waiting", window.in_flight)
        self.assertEqual(_DeferredThread.instances, [])
        self.transfer.assert_not_called()
        window.render_tasks()
        self.assertIn("继续", window.task_widgets["waiting"]["pause"].toolTip())
        self.assertEqual(window.task_widgets["waiting"]["state"].text(), "已暂停")

    def test_resuming_paused_task_keeps_checkpoint_bytes_until_worker_reports_offset(self) -> None:
        self.state["profiles"] = [self._profile()]
        self.state["tasks"] = [{**self._task("paused", "paused"), "transferred": 3}]
        window = self._make_window()
        window.profiles[0]["secret_access_key"] = "test-secret-key"
        window.switch_page(1)
        QTest.mouseClick(window.task_widgets["paused"]["pause"], Qt.MouseButton.LeftButton)
        self._flush()
        task = window.tasks[0]
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["transferred"], 3)
        self.assertEqual(self.saved[-1][1][0]["transferred"], 3)
        self.assertTrue(window.transfer_running)
        self.assertIn("paused", window.in_flight)
        self.assertFalse(window.pause_events["paused"].is_set())
        self.assertFalse(window.cancel_events["paused"].is_set())
        self.assertEqual(len(_DeferredThread.instances), 1)
        window.handle_event({"kind": "started", "id": "paused"})
        self.assertEqual(task["transferred"], 3)
        window.handle_event({"kind": "resumed", "id": "paused", "transferred": 3})
        window.handle_event({"kind": "progress", "id": "paused", "bytes": 1})
        self.assertEqual(task["transferred"], 4)
        window.handle_event({"kind": "result", "id": "paused", "status": "completed", "transferred": 5})
        window.handle_event({"kind": "batch_done", "ids": ["paused"]})
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["transferred"], 5)
        self.assertFalse(window.transfer_running)

    def test_active_pause_waits_for_worker_checkpoint_and_batch_ownership_release(self) -> None:
        self.state["profiles"] = [self._profile()]
        self.state["tasks"] = [self._task("running", "downloading")]
        window = self._make_window()
        window.profiles[0]["secret_access_key"] = "test-secret-key"
        window.transfer_running = True
        window.in_flight.add("running")
        window.cancel_events["running"] = threading.Event()
        window.pause_events["running"] = threading.Event()
        pause = window.pause_events["running"]
        window.pause_task("running")
        self.assertEqual(window.tasks[0]["status"], "pausing")
        self.assertTrue(pause.is_set())
        self.assertFalse(window.cancel_events["running"].is_set())
        window.handle_event({"kind": "result", "id": "running", "status": "paused", "transferred": 3})
        window.render_tasks()
        self.assertEqual(window.tasks[0]["status"], "paused")
        self.assertEqual(window.tasks[0]["transferred"], 3)
        self.assertFalse(window.task_widgets["running"]["pause"].isEnabled())
        window.toggle_pause("running")
        self.assertEqual(window.tasks[0]["status"], "paused")
        self.assertIs(window.pause_events["running"], pause)
        self.assertEqual(_DeferredThread.instances, [])
        window.handle_event({"kind": "batch_done", "ids": ["running"]})
        window.render_tasks()
        self.assertNotIn("running", window.in_flight)
        self.assertEqual(window.tasks[0]["status"], "paused")
        self.assertTrue(window.task_widgets["running"]["pause"].isEnabled())
        window.toggle_pause("running")
        self.assertEqual(window.tasks[0]["status"], "queued")
        self.assertEqual(window.tasks[0]["transferred"], 3)
        self.assertIsNot(window.pause_events["running"], pause)
        self.assertEqual(len(_DeferredThread.instances), 1)

    def test_progress_updates_existing_task_row_instead_of_recreating_widgets(self) -> None:
        self.state["tasks"] = [self._task("running", "downloading")]
        window = self._make_window()
        window.switch_page(1)
        row = window.task_widgets["running"]["row"]
        progress = window.task_widgets["running"]["progress"]
        window.last_render = 0
        with patch.object(window, "clear_layout", wraps=window.clear_layout) as clear_layout:
            window.events.put({"kind": "progress", "id": "running", "bytes": 2})
            window.poll()
        self.assertEqual(window.tasks[0]["transferred"], 2)
        self.assertIs(window.task_widgets["running"]["row"], row)
        self.assertIs(window.task_widgets["running"]["progress"], progress)
        self.assertEqual(progress.value(), 400)
        clear_layout.assert_not_called()

    def test_hidden_task_page_does_not_render_in_response_to_progress(self) -> None:
        self.state["tasks"] = [self._task("running", "downloading")]
        window = self._make_window()
        self.assertEqual(window.pages.currentIndex(), 0)
        row = window.task_widgets["running"]["row"]
        window.last_render = 0
        with patch.object(window, "render_tasks", wraps=window.render_tasks) as render_tasks:
            window.events.put({"kind": "progress", "id": "running", "bytes": 3})
            window.poll()
        render_tasks.assert_not_called()
        self.assertEqual(window.tasks[0]["transferred"], 3)
        self.assertTrue(window.task_dirty)
        window.switch_page(1)
        self.assertIs(window.task_widgets["running"]["row"], row)
        self.assertEqual(window.task_widgets["running"]["progress"].value(), 600)

    def test_refreshing_large_bucket_with_same_query_replaces_old_search_results(self) -> None:
        self.state["profiles"] = [self._profile()]
        window = self._make_window()
        unchanged = [{"key": f"folder/file-{index:05d}.txt", "size": 1, "modified": "", "kind": "file"} for index in range(5001)]
        old_catalog = client.CatalogIndex([*unchanged, {"key": "old-target.jpg", "size": 2}])
        window.files[window.bucket_id] = old_catalog.entries
        window.catalogs[window.bucket_id] = old_catalog
        window.catalog_sources[window.bucket_id] = old_catalog.entries
        window.loaded.add(window.bucket_id)
        window.search.setText("target")
        QTest.qWait(280)
        self._flush()
        self.assertEqual(len(_DeferredThread.instances), 1)
        _DeferredThread.instances[0].complete()
        window.poll()
        self.assertEqual([entry["key"] for entry in window.visible_entries], ["old-target.jpg"])
        self.assertTrue(window.select_button.isEnabled())
        old_token = window._query_token
        fresh_catalog = client.CatalogIndex([*unchanged, {"key": "new-target.jpg", "size": 3}])
        window.scan_tokens[window.bucket_id] = "fresh-scan"
        window.handle_event({
            "kind": "scan_done", "profile_id": window.bucket_id, "token": "fresh-scan",
            "objects": fresh_catalog.entries, "catalog": fresh_catalog,
        })
        self.assertEqual(len(_DeferredThread.instances), 2)
        self.assertNotEqual(window._query_token, old_token)
        self.assertFalse(window.select_button.isEnabled())
        self.assertFalse(window.download_button.isEnabled())
        # A late old-query completion must not restore the old catalog, even
        # though both the bucket and query text are unchanged.
        window.handle_event({
            "kind": "search_ready", "profile_id": window.bucket_id,
            "token": old_token, "query": "target", "catalog": old_catalog,
            "rows": old_catalog.search("target"),
        })
        _DeferredThread.instances[1].complete()
        window.poll()
        self.assertEqual(window.search.text(), "target")
        self.assertEqual([entry["key"] for entry in window.visible_entries], ["new-target.jpg"])
        self.assertEqual(window.visible_entries[0]["size"], 3)
        self.assertTrue(window.select_button.isEnabled())
        self.create_client.assert_not_called()
        self.list_objects.assert_not_called()
        self.transfer.assert_not_called()

    def test_final_save_disables_mutation_and_rejects_late_submit_until_writer_drains(self) -> None:
        window = self._make_window()
        original_writer = window.writer

        class SlowClosingWriter(_ImmediateStateWriter):
            def __init__(self) -> None:
                super().__init__(lambda _error: None, save_fn=original_writer.save_fn)
                self.close_requested = False
                self.submit_count = 0

            def submit(self, profiles: list, tasks: list, preferences: dict) -> int:
                if self.close_requested:
                    raise RuntimeError("No submission allowed after final close")
                self.submit_count += 1
                return super().submit(profiles, tasks, preferences)

            def close(self, timeout: float = 0, flush: bool = True) -> bool:
                self.close_requested = True
                return self.is_closed

        writer = SlowClosingWriter()
        window.writer = writer
        event = QCloseEvent()
        window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertTrue(writer.close_requested)
        self.assertEqual(writer.submit_count, 1)
        self.assertFalse(window.centralWidget().isEnabled())
        self.assertFalse(window.persist())
        self.assertEqual(writer.submit_count, 1)
        self.assertEqual(len(self.saved), 1)
        # Once the background writer reports completion, the same close path
        # accepts the event without scheduling an impossible new submission.
        writer.is_closed = True
        final_event = QCloseEvent()
        window.closeEvent(final_event)
        self.assertTrue(final_event.isAccepted())
        self.assertEqual(writer.submit_count, 1)

    def test_failed_final_save_keeps_window_open_and_allows_retry(self) -> None:
        window = self._make_window()
        original_writer = window.writer

        class DelayedWriter(_ImmediateStateWriter):
            def submit(self, profiles, tasks, preferences) -> int:
                return self.completed_revision + 1

        writer = DelayedWriter(lambda _error: None, save_fn=original_writer.save_fn)
        window.writer = writer
        event = QCloseEvent()
        window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertTrue(window.closing)
        self.assertFalse(writer.is_closed)
        writer.completed_revision = 1
        window.poll()
        self.assertFalse(window.closing)
        self.assertTrue(window.centralWidget().isEnabled())
        self.warning.assert_called()
        window.close()
        writer.completed_revision = writer.saved_revision = 2
        window.poll()
        self.assertTrue(writer.is_closed)
        self.assertFalse(window.isVisible())


if __name__ == "__main__":
    unittest.main()
