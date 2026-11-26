"""Offline large-catalog/queue responsiveness checks for the production client.

These tests build the 100k-object index before opening a window. UI/network/
persistence dependencies are isolated; the tests never read or write user data.
Timing is recorded as evidence, not asserted against fragile machine-speed
limits. Structural assertions protect the work bounds that keep Qt responsive.
"""
from __future__ import annotations

import copy
import os
import time
import unittest
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from r2_batch_download import client
from r2_batch_download.catalog_index import CatalogIndex


class _ImmediateWriter:
    """No actual serialization, encryption or filesystem access in UI tests."""

    def __init__(self, **_kwargs: object) -> None:
        self.is_closed = False
        self.is_idle = True
        self.submissions = 0

    def submit(self, *_args: object) -> int:
        self.submissions += 1
        return self.submissions

    @property
    def completed_revision(self) -> int:
        return self.submissions

    @property
    def saved_revision(self) -> int:
        return self.submissions

    def close(self, **_kwargs: object) -> bool:
        self.is_closed = True
        return True


class _NonIterableTasks(list):
    """Fails if a progress-only path accidentally scans the full task queue."""

    allow_iteration = True

    def __iter__(self):
        if not self.allow_iteration:
            raise AssertionError("Progress-only updates must not iterate the entire task list")
        return super().__iter__()


class ClientPerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt = QApplication.instance() or QApplication([])
        cls.qt.setQuitOnLastWindowClosed(False)
        cls.qt.setStyle("Fusion")
        cls.qt.setStyleSheet(client.app_style())
        started = time.perf_counter()
        # This expensive work intentionally happens before the GUI is created,
        # matching scanner-worker construction in the production application.
        cls.catalog = CatalogIndex([
            {"key": f"folder{number // 1000}/sub/file{number}.bin", "size": 1024,
             "modified": "2026-11-25T00:00:00Z"}
            for number in range(100_000)
        ])
        cls.index_seconds = time.perf_counter() - started

    def setUp(self) -> None:
        state = {
            "profiles": [{"id": "load-test", "name": "压力测试目录", "bucket": "test-only-bucket",
                          "account_id": "", "access_key_id": "", "secret_access_key": "",
                          "remember_secret": False, "endpoint_url": ""}],
            "tasks": [], "preferences": {"workers": 4, "skip_existing": True},
        }
        self.enterContext(patch.object(client, "load_state", side_effect=lambda: copy.deepcopy(state)))
        self.enterContext(patch.object(client, "StateWriter", _ImmediateWriter))
        self.enterContext(patch.object(client, "save_state", side_effect=AssertionError("No real state writes")))
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("No network")))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("No network")))
        self.enterContext(patch.object(client, "list_objects", side_effect=AssertionError("No network listing")))
        self.enterContext(patch.object(client, "transfer_tasks", side_effect=AssertionError("No real transfer")))
        self.enterContext(patch.object(client.QMessageBox, "question", return_value=client.QMessageBox.StandardButton.Yes))
        self.enterContext(patch.object(client.QMessageBox, "warning"))
        self.window = client.ClientWindow()
        self.window.timer.stop()
        self.window.resize_timer.stop()
        self.window.files["load-test"] = self.catalog.entries
        self.window.catalogs["load-test"] = self.catalog
        self.window.catalog_sources["load-test"] = self.catalog.entries
        self.window.loaded.add("load-test")
        self.addCleanup(self._destroy_window)

    def _destroy_window(self) -> None:
        self.window.transfer_running = False
        self.window.dispatch_needed = False
        self.window.tasks = []
        self.window.loading.clear()
        self.window.close()
        self.window.deleteLater()
        self.qt.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.qt.processEvents()

    def _tasks(self, count: int = 20_000) -> _NonIterableTasks:
        tasks = _NonIterableTasks([
            {"id": f"task-{number}", "key": f"folder{number // 1000}/file{number}.bin",
             "name": f"file{number}.bin", "bucket": "test-only-bucket", "profile_id": "load-test",
             "status": "downloading" if number < 60 else "completed",
             "size": 100_000_000, "transferred": 0 if number < 60 else 100_000_000,
             "error": "", "output_dir": "D:/not-written-by-performance-tests", "workers": 4,
             "overwrite": False, "prefix": "", "strip_prefix": False}
            for number in range(count)
        ])
        self.window.tasks = tasks
        self.window.sync_task_index()
        self.window.switch_page(1)
        return tasks

    def test_100k_catalog_navigation_and_page_changes_reuse_background_index(self) -> None:
        self.window.view_mode = "list"
        with patch.object(client, "CatalogIndex", side_effect=AssertionError("UI must reuse the background index")):
            self.window.navigate("folder42/sub/")
            rows = self.window.all_entries
            self.assertIs(rows, self.catalog.children("folder42/sub/"))
            self.assertEqual(len(rows), 1000)
            self.assertEqual(len(self.window.visible_entries), self.window.PAGE_SIZE)
            first_key = self.window.visible_entries[0]["key"]
            self.window.file_page_delta(1)
            self.assertIs(self.window.all_entries, rows)
            self.assertEqual(self.window.page_index, 1)
            self.assertNotEqual(self.window.visible_entries[0]["key"], first_key)
            for _ in range(3):
                self.window.render_files()
            self.assertIs(self.window.catalogs["load-test"], self.catalog)

    def test_20k_progress_updates_use_id_index_and_keep_existing_widgets(self) -> None:
        tasks = self._tasks()
        rows = {task_id: widgets["row"] for task_id, widgets in self.window.task_widgets.items()}
        self.assertEqual(len(rows), self.window.TASK_PAGE_SIZE)
        tasks.allow_iteration = False
        started = time.perf_counter()
        for number in range(6000):
            self.window.handle_event({"kind": "progress", "id": f"task-{number % 60}", "bytes": 128})
        self.window.render_tasks()
        seconds = time.perf_counter() - started
        self.assertEqual(self.window.task_by_id["task-0"]["transferred"], 12_800)
        self.assertEqual({task_id: widgets["row"] for task_id, widgets in self.window.task_widgets.items()}, rows)
        tasks.allow_iteration = True
        print(f"\nQt queue benchmark: 20,000 tasks, 6,000 indexed progress updates + in-place render in {seconds:.4f}s")

    def test_progress_storm_yields_to_15ms_qt_heartbeat(self) -> None:
        tasks = self._tasks()
        tasks.allow_iteration = False
        self.window.persist_dirty = False
        self.window.dispatch_needed = False
        for number in range(60_000):
            self.window.events.put({"kind": "progress", "id": f"task-{number % 60}", "bytes": 1})
        poll_seconds: list[float] = []
        pulses: list[float] = []
        heartbeat = QTimer(self.window)
        heartbeat.setInterval(15)
        heartbeat.timeout.connect(lambda: pulses.append(time.perf_counter()))
        pump = QTimer(self.window)
        pump.setInterval(0)

        def poll_once() -> None:
            if self.window.events.empty():
                pump.stop()
                return
            start = time.perf_counter()
            self.window.poll()
            poll_seconds.append(time.perf_counter() - start)

        pump.timeout.connect(poll_once)
        start = time.perf_counter()
        heartbeat.start()
        pump.start()
        QTest.qWait(450)
        heartbeat.stop()
        pump.stop()
        tasks.allow_iteration = True
        self.assertGreaterEqual(len(pulses), 3, "Qt must continue processing independent UI timers during a progress storm")
        self.assertGreater(len(poll_seconds), 1)
        consumed = 60_000 - self.window.events.qsize()
        self.assertGreater(consumed, 0)
        self.assertLessEqual(consumed, len(poll_seconds) * 350)
        print(f"\nQt heartbeat benchmark: {consumed:,} events in {time.perf_counter() - start:.3f}s, {len(pulses)} timer pulses, max poll {max(poll_seconds) * 1000:.2f}ms")

    def test_resize_burst_is_debounced_without_rebuilding_large_catalog(self) -> None:
        self.window.view_mode = "list"
        self.window.navigate("folder42/sub/")
        self.window.show()
        self.qt.processEvents()
        self.window.resize_timer.stop()
        with patch.object(client, "CatalogIndex", side_effect=AssertionError("Resize cannot rebuild catalog")), patch.object(self.catalog, "children", wraps=self.catalog.children) as children:
            for number in range(30):
                self.window.resize(1100 + number, 760 + number % 4)
            QTest.qWait(220)
            # Two constant-time children lookups per render are acceptable:
            # cached rows and whether directory download is enabled.
            self.assertGreaterEqual(children.call_count, 1)
            self.assertLessEqual(children.call_count, 4, "One resize burst must not schedule one render per pixel")
        self.assertIs(self.window.catalogs["load-test"], self.catalog)
        self.assertIs(self.window.all_entries, self.catalog.children("folder42/sub/"))
        print(f"\nQt catalog benchmark: 100,000 objects indexed before GUI in {self.index_seconds:.3f}s; 30 resize events coalesced")

    def test_clearing_20k_tasks_schedules_one_partial_cleanup_batch(self) -> None:
        tasks = self._tasks()
        for number, task in enumerate(tasks):
            task["status"] = "paused" if number % 2 else "queued"
            task["transferred"] = 1024 if number % 2 else 0
        self.window.transfer_running = True
        self.window.in_flight = {"task-0", "task-1"}
        with patch.object(self.window, "cleanup_partials") as cleanup:
            started = time.perf_counter()
            self.window.clear_tasks()
            seconds = time.perf_counter() - started
        cleanup.assert_called_once()
        cleanup_tasks = cleanup.call_args.args[0]
        self.assertEqual(len(cleanup_tasks), 9999)
        self.assertTrue(all(task["transferred"] == 1024 for task in cleanup_tasks))
        self.assertNotIn("task-1", {task["id"] for task in cleanup_tasks})
        self.assertTrue(self.window.clear_when_idle)
        self.assertEqual(self.window.task_by_id["task-0"]["status"], "cancelling")
        self.assertEqual(self.window.task_by_id["task-19999"]["status"], "cancelled")
        self.assertEqual(self.window.task_by_id["task-19999"]["transferred"], 0)
        self.assertEqual(set(self.window.cancel_events), {"task-0", "task-1"})
        print(f"\nQt bulk-clear benchmark: 20,000 tasks cancelled in {seconds:.4f}s; one cleanup batch for {len(cleanup_tasks):,} partial files")


if __name__ == "__main__":
    unittest.main()
