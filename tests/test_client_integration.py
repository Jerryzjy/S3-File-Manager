"""Offline end-to-end Qt workflows using real storage and transfer code.

Only the S3 transport, the download dialog decision, and modal answers are
substituted. The real scanner, catalog, queue, worker threads, transfer engine,
local file writes and JSON persistence run together in temporary directories.
"""
from __future__ import annotations

from bisect import bisect
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QCoreApplication, QEvent, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
from botocore.response import StreamingBody

from r2_batch_download import client, client_store
from r2_batch_download import resumable_transfer
from r2_batch_download.ui import FileCard, app_style


class _OfflineS3:
    """HEAD/GET transport substitute; the actual streaming engine writes files."""

    def __init__(self, payloads: dict[str, bytes], failures: set[str] | None = None) -> None:
        self.payloads = payloads
        self.failures = set(failures or ())
        self.attempts: dict[str, int] = {}
        self.get_calls: list[dict] = []
        self.lock = threading.Lock()
        self.scan_calls = 0
        self.close_calls = 0
        self.read_gates: dict[str, tuple[threading.Event, threading.Event]] = {}
        self.scan_gate: threading.Event | None = None
        self.directory_calls: list[tuple[str, str]] = []

    def list_objects_v2(self, *, Bucket: str, Prefix: str, Delimiter: str, MaxKeys: int, ContinuationToken: str = "") -> dict:
        if Bucket != "integration-bucket" or Delimiter != "/":
            raise AssertionError((Bucket, Delimiter))
        self.directory_calls.append((Prefix, ContinuationToken))
        objects: dict[str, dict] = {}
        folders: set[str] = set()
        for key, payload in self.payloads.items():
            if not key.startswith(Prefix):
                continue
            suffix = key[len(Prefix):]
            if "/" in suffix:
                folders.add(Prefix + suffix.split("/", 1)[0] + "/")
            else:
                objects[key] = {"Key": key, "Size": len(payload), "ETag": '"offline-etag"'}
        keys = sorted(folders | objects.keys())
        start = bisect(keys, ContinuationToken) if ContinuationToken else 0
        page = keys[start:start + MaxKeys]
        truncated = start + len(page) < len(keys)
        return {
            "CommonPrefixes": [{"Prefix": key} for key in page if key in folders],
            "Contents": [objects[key] for key in page if key in objects],
            "IsTruncated": truncated, "NextContinuationToken": page[-1] if truncated else "",
        }

    def get_paginator(self, operation: str) -> "_OfflineS3":
        if operation != "list_objects_v2":
            raise AssertionError(operation)
        return self

    def paginate(self, *, Bucket: str, Prefix: str) -> object:
        if Bucket != "integration-bucket" or Prefix != "":
            raise AssertionError((Bucket, Prefix))
        self.scan_calls += 1
        if self.scan_gate is not None and not self.scan_gate.wait(6):
            raise AssertionError("Integration test did not release its scan gate")
        raw = [{
            "Key": key, "Size": len(payload), "ETag": '"offline-etag"',
            "LastModified": datetime(2026, 11, 25, tzinfo=timezone.utc),
        } for key, payload in self.payloads.items()]
        # Real scan logic must consume more than one provider page.
        yield {"Contents": raw[:1]}
        yield {"Contents": raw[1:]}

    def head_object(self, *, Bucket: str, Key: str) -> dict:
        if Bucket != "integration-bucket":
            raise AssertionError(Bucket)
        return {"ContentLength": len(self.payloads[Key]), "ETag": '"offline-etag"'}

    def get_object(self, **arguments: object) -> dict:
        if arguments.get("Bucket") != "integration-bucket" or arguments.get("IfMatch") != '"offline-etag"':
            raise AssertionError(arguments)
        key = arguments["Key"]
        with self.lock:
            self.attempts[key] = self.attempts.get(key, 0) + 1
            self.get_calls.append(dict(arguments))
            fail = key in self.failures
        payload = self.payloads[key]
        offset = int(str(arguments["Range"]).split("=")[1][:-1]) if arguments.get("Range") else 0
        read_gate = self.read_gates.get(key)

        class Stream(io.BytesIO):
            def read(self, amount: int = -1) -> bytes:
                if fail and self.tell() >= resumable_transfer.CHUNK_SIZE:
                    raise OSError("offline transport interrupted")
                if read_gate is not None and self.tell() == 0:
                    entered, release = read_gate
                    entered.set()
                    if not release.wait(4):
                        raise AssertionError("Integration test did not release its stream gate")
                chunk = super().read(amount)
                return chunk

        response = {
            "Body": StreamingBody(Stream(payload[offset:]), len(payload) - offset),
            "ContentLength": len(payload) - offset, "ETag": '"offline-etag"',
            "ResponseMetadata": {"HTTPStatusCode": 206 if offset else 200},
        }
        if offset:
            response["ContentRange"] = f"bytes {offset}-{len(payload) - 1}/{len(payload)}"
        return response

    def close(self) -> None:
        self.close_calls += 1


class ClientWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt = QApplication.instance() or QApplication([])
        cls.qt.setQuitOnLastWindowClosed(False)
        cls.qt.setStyle("Fusion")
        cls.qt.setStyleSheet(app_style())

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.state_file = self.root / "state" / "client-state.json"
        self.output = self.root / "downloaded"
        self.enterContext(patch.object(client_store, "state_path", return_value=self.state_file))
        # Nothing in the workflow may fall through to a real network endpoint.
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("Real network is forbidden in integration tests")))
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("Real network is forbidden in integration tests")))
        self.enterContext(patch.object(client.QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes))
        self.warning = self.enterContext(patch.object(client.QMessageBox, "warning"))
        self.information = self.enterContext(patch.object(client.QMessageBox, "information"))
        self.windows: list[client.ClientWindow] = []
        self.window = self.new_window()
        self.addCleanup(self.close_windows)

    def new_window(self) -> client.ClientWindow:
        window = client.ClientWindow()
        self.windows.append(window)
        window.show()
        self.qt.processEvents()
        return window

    def close_windows(self) -> None:
        for window in reversed(self.windows):
            for cancel in window.cancel_events.values():
                cancel.set()
            self.until(lambda: not window.transfer_running, window, timeout=4)
            self.assertTrue(window.writer.flush(5))
            window.timer.stop()
            window.close()
            self.assertTrue(window.writer.close(timeout=5))
            window.close()
            window.deleteLater()
        self.qt.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.qt.processEvents()

    def until(self, condition: object, window: client.ClientWindow | None = None, timeout: float = 6) -> None:
        target = window or self.window
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.qt.processEvents()
            target.poll()
            if condition():
                return
            QTest.qWait(10)
        self.fail(f"Workflow timed out; tasks={target.tasks!r}")

    def connect_and_scan(self, s3: _OfflineS3, *, wait: bool = True) -> None:
        self.enterContext(patch("r2_batch_download.desktop_engine.create_client", return_value=s3))
        self.enterContext(patch("r2_batch_download.resumable_transfer.create_client", return_value=s3))
        profile = {
            "id": "offline-profile", "name": "集成测试存储桶", "bucket": "integration-bucket",
            "account_id": "offlineaccount", "access_key_id": "offline-access",
            "secret_access_key": "offline-private-secret", "endpoint_url": "", "remember_secret": False,
        }
        self.window.profiles.append(profile)
        self.window.bucket_id = profile["id"]
        self.window.refresh_files()
        if not wait:
            return
        self.until(lambda: profile["id"] in self.window.loaded and not self.window.loading)
        self.assertEqual(s3.scan_calls, 1)
        catalog = self.window.files[profile["id"]]
        self.assertEqual({entry["key"] for entry in catalog}, set(s3.payloads))
        self.assertTrue(all(entry["modified"].startswith("2026-11-25") and entry["etag"] == '"offline-etag"' for entry in catalog))

    def select_folder(self, key: str) -> None:
        cards = [self.window.grid.itemAt(index).widget() for index in range(self.window.grid.count())]
        card = next(card for card in cards if isinstance(card, FileCard) and card.entry["key"] == key)
        QTest.mouseClick(card.check, Qt.MouseButton.LeftButton)
        self.qt.processEvents()
        self.assertIn(key, self.window.selected)

    def queue_selected(self) -> None:
        output = str(self.output)
        dialog_exec = client.DownloadDialog.exec
        confirmed = []

        def accept_download(dialog: client.DownloadDialog) -> QDialog.DialogCode:
            dialog.destination.setText(output)
            dialog.workers.setCurrentIndex(dialog.workers.findData(2))
            dialog.skip.setChecked(True)
            dialog.strip.setChecked(False)
            poll = QTimer(dialog)
            def click_when_ready() -> None:
                if dialog.confirm_button.isEnabled():
                    confirmed.append(dialog.preflight)
                    dialog.confirm_button.click()
            poll.timeout.connect(click_when_ready)
            poll.start(25)
            timeout = QTimer(dialog)
            timeout.setSingleShot(True)
            timeout.timeout.connect(dialog.reject)
            timeout.start(3000)
            result = dialog_exec(dialog)
            poll.stop()
            timeout.stop()
            return result

        with patch.object(client.DownloadDialog, "exec", accept_download):
            self.window.create_download()
        self.assertTrue(confirmed, "The real download preflight did not approve these files")

    def stored(self, window: client.ClientWindow | None = None) -> dict:
        target = window or self.window
        self.assertTrue(target.persist())
        self.assertTrue(target.writer.flush(5))
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def test_directory_pages_and_navigation_work_before_the_full_scan_completes(self) -> None:
        s3 = _OfflineS3({"folder/first.txt": b"first", "folder/second.txt": b"second", "root.txt": b"root"})
        s3.scan_gate = threading.Event()
        self.addCleanup(s3.scan_gate.set)
        self.window.PAGE_SIZE = 1
        self.connect_and_scan(s3, wait=False)
        self.until(lambda: [row["key"] for row in self.window.visible_entries] == ["folder/"])
        self.assertNotIn(self.window.bucket_id, self.window.loaded)
        self.assertFalse(self.window.download_all_button.isEnabled())
        self.window.file_next.click()
        self.until(lambda: [row["key"] for row in self.window.visible_entries] == ["root.txt"])
        self.window.toggle_select_all()
        self.assertTrue(self.window.download_button.isEnabled())
        self.queue_selected()
        self.until(lambda: self.window.tasks and self.window.tasks[0]["status"] == "completed")
        self.assertEqual((self.output / "root.txt").read_bytes(), b"root")
        self.window.switch_page(0)
        self.window.file_previous.click()
        self.window.open_entry("folder/")
        self.until(lambda: [row["key"] for row in self.window.visible_entries] == ["folder/first.txt"])
        self.assertEqual(s3.directory_calls, [("", ""), ("", "folder/"), ("folder/", "")])
        s3.scan_gate.set()
        self.until(lambda: self.window.bucket_id in self.window.loaded)
        self.assertEqual(self.window.prefix, "folder/")
        self.assertTrue(self.window.search.isEnabled())
        self.assertTrue(self.window.download_all_button.isEnabled())

    def test_real_files_complete_persist_restart_and_clear_records_without_deleting_files(self) -> None:
        self.assertEqual(self.window.profiles, [])
        self.assertEqual(self.window.tasks, [])
        self.assertEqual(self.window.files, {})
        s3 = _OfflineS3({
            "品牌资料/": b"",
            "品牌资料/标志.png": b"offline image content",
            "品牌资料/子目录/说明.txt": "完整的中文说明".encode("utf-8"),
            "outside.txt": b"must not be downloaded",
        })
        self.connect_and_scan(s3)
        self.select_folder("品牌资料/")
        self.queue_selected()
        self.until(lambda: len(self.window.tasks) == 2 and not self.window.transfer_running and all(task["status"] == "completed" for task in self.window.tasks))
        expected = {key: payload for key, payload in s3.payloads.items() if key.startswith("品牌资料/") and not key.endswith("/")}
        self.assertEqual(set(s3.attempts), set(expected))
        for key, payload in expected.items():
            self.assertEqual((self.output / key).read_bytes(), payload)
        self.assertFalse((self.output / "outside.txt").exists())
        self.assertEqual(list(self.output.glob("**/*.part")), [])
        records = self.stored()["tasks"]
        self.assertEqual({row["key"] for row in records}, set(expected))
        self.assertTrue(all(row["status"] == "completed" and row["transferred"] == row["size"] for row in records))
        self.assertNotIn("offline-private-secret", self.state_file.read_text(encoding="utf-8"))

        restarted = self.new_window()
        self.assertEqual(len(restarted.tasks), 2)
        self.assertTrue(all(task["status"] == "completed" for task in restarted.tasks))
        self.assertEqual(restarted.profiles[0]["secret_access_key"], "")
        self.assertFalse(restarted.transfer_running)
        self.assertEqual(s3.scan_calls, 1, "A restart without remembered private key must not scan or transfer automatically")
        restarted.clear_tasks()
        self.assertEqual(restarted.tasks, [])
        self.assertEqual(self.stored(restarted)["tasks"], [])
        for key, payload in expected.items():
            self.assertEqual((self.output / key).read_bytes(), payload)
        self.warning.assert_not_called()

    def test_failed_object_can_be_retried_to_completion_while_successful_neighbor_is_preserved(self) -> None:
        s3 = _OfflineS3({"folder/a.txt": b"first" * resumable_transfer.CHUNK_SIZE, "folder/b.txt": b"second complete file"}, failures={"folder/a.txt"})
        self.connect_and_scan(s3)
        self.select_folder("folder/")
        self.queue_selected()
        self.until(lambda: len(self.window.tasks) == 2 and not self.window.transfer_running and all(task["status"] in {"completed", "failed"} for task in self.window.tasks))
        failed = next(task for task in self.window.tasks if task["status"] == "failed")
        self.assertEqual(failed["key"], "folder/a.txt")
        self.assertIn("offline transport interrupted", failed["error"])
        self.assertFalse((self.output / "folder/a.txt").exists())
        neighbor = next(task for task in self.window.tasks if task["key"] == "folder/b.txt")
        self.assertEqual(neighbor["status"], "completed", neighbor)
        successful = self.output / "folder/b.txt"
        self.assertEqual(successful.read_bytes(), s3.payloads["folder/b.txt"])
        neighbor_timestamp = successful.stat().st_mtime_ns
        self.assertEqual(len(list(self.output.glob("**/*.part"))), 1)
        self.assertEqual(failed["transferred"], resumable_transfer.CHUNK_SIZE * 3)

        task_id = failed["id"]
        s3.failures.clear()
        self.window.retry_task(task_id)
        self.until(lambda: not self.window.transfer_running and all(task["status"] == "completed" for task in self.window.tasks))
        self.assertEqual(s3.attempts, {"folder/a.txt": 4, "folder/b.txt": 1})
        self.assertEqual(s3.get_calls[-1]["Range"], f"bytes={resumable_transfer.CHUNK_SIZE * 3}-")
        self.assertEqual(successful.stat().st_mtime_ns, neighbor_timestamp)
        self.assertEqual((self.output / "folder/a.txt").read_bytes(), s3.payloads["folder/a.txt"])
        self.assertTrue(all(task["status"] == "completed" for task in self.stored()["tasks"]))
        self.warning.assert_not_called()
        self.information.assert_not_called()

    def test_gui_pause_preserves_partial_and_continue_requests_only_remaining_bytes(self) -> None:
        key = "folder/video.bin"
        payload = b"first" * resumable_transfer.CHUNK_SIZE + b"last"
        s3 = _OfflineS3({key: payload})
        entered, release = threading.Event(), threading.Event()
        s3.read_gates[key] = entered, release
        # Always release a gated test worker even when an assertion fails.
        self.addCleanup(release.set)
        self.connect_and_scan(s3)
        self.select_folder("folder/")
        self.queue_selected()
        self.until(lambda: entered.is_set() and self.window.tasks[0]["status"] == "downloading")
        task = self.window.tasks[0]
        self.window.pause_task(task["id"])
        self.assertEqual(task["status"], "pausing")
        release.set()
        self.until(lambda: not self.window.transfer_running and task["status"] == "paused")
        self.assertEqual(task["transferred"], resumable_transfer.CHUNK_SIZE)
        self.assertFalse((self.output / key).exists())
        partial = next(self.output.rglob("*.part"))
        self.assertEqual(partial.read_bytes(), payload[:resumable_transfer.CHUNK_SIZE])
        self.assertEqual(self.stored()["tasks"][0]["status"], "paused")
        s3.read_gates.clear()
        self.window.toggle_pause(task["id"])
        self.until(lambda: not self.window.transfer_running and task["status"] == "completed")
        self.assertEqual(s3.get_calls[-1]["Range"], f"bytes={resumable_transfer.CHUNK_SIZE}-")
        self.assertEqual((self.output / key).read_bytes(), payload)
        self.assertEqual(list(self.output.rglob("*.part")), [])
        self.assertEqual(self.stored()["tasks"][0]["transferred"], len(payload))


if __name__ == "__main__":
    unittest.main()
