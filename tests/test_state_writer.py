from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from r2_batch_download import client_store
from r2_batch_download.state_writer import StateWriter


class StateWriterTests(unittest.TestCase):
    def test_submit_remains_responsive_and_coalesces_while_disk_write_is_blocked(self) -> None:
        started, release, submitted = threading.Event(), threading.Event(), threading.Event()
        calls: list[list[str]] = []

        def save(_profiles: list, tasks: list, _preferences: dict) -> None:
            calls.append([task["id"] for task in tasks])
            if len(calls) == 1:
                started.set()
                self.assertTrue(release.wait(3))

        writer = StateWriter(self.fail, save_fn=save)
        self.addCleanup(lambda: (release.set(), writer.close(3)))
        first = writer.submit([], [{"id": "first"}], {})
        self.assertTrue(started.wait(2))
        self.assertFalse(writer.is_idle)

        def update() -> None:
            for index in range(100):
                writer.submit([], [{"id": str(index)}], {})
            submitted.set()

        thread = threading.Thread(target=update)
        thread.start()
        # Event-based proof of responsiveness: submissions complete while the
        # disk operation is deliberately waiting for us to release it.
        self.assertTrue(submitted.wait(2))
        self.assertEqual(calls, [["first"]])
        self.assertFalse(writer.flush(0))
        release.set()
        thread.join(2)
        self.assertTrue(writer.flush(3))
        self.assertEqual(calls, [["first"], ["99"]])
        self.assertEqual(writer.saved_revision, first + 100)
        self.assertTrue(writer.is_idle)

    def test_pending_snapshots_are_isolated_from_later_ui_mutations(self) -> None:
        started, release = threading.Event(), threading.Event()
        calls: list[tuple[list, list, dict]] = []

        def save(profiles: list, tasks: list, preferences: dict) -> None:
            if not calls:
                calls.append((profiles, tasks, preferences))
                started.set()
                self.assertTrue(release.wait(3))
            else:
                calls.append((profiles, tasks, preferences))

        writer = StateWriter(self.fail, save_fn=save)
        self.addCleanup(lambda: (release.set(), writer.close(3)))
        writer.submit([], [], {})
        self.assertTrue(started.wait(2))
        profiles = [{"id": "p", "secret_access_key": "secret-before"}]
        tasks = [{"id": "t", "status": "paused", "transferred": 123}]
        preferences = {"workers": 4}
        revision = writer.submit(profiles, tasks, preferences)
        profiles[0]["secret_access_key"] = "secret-after"
        profiles.append({"id": "later"})
        tasks[0]["status"] = "completed"
        tasks.clear()
        preferences["workers"] = 16
        release.set()
        self.assertTrue(writer.flush(3))
        saved_profiles, saved_tasks, saved_preferences = calls[-1]
        self.assertEqual(saved_profiles, [{"id": "p", "secret_access_key": "secret-before"}])
        self.assertEqual(saved_tasks, [{"id": "t", "status": "paused", "transferred": 123}])
        self.assertEqual(saved_preferences, {"workers": 4})
        self.assertEqual(writer.completed_revision, revision)

    def test_close_with_zero_timeout_drains_latest_state_before_thread_exits(self) -> None:
        started, release = threading.Event(), threading.Event()
        saved: list[int] = []

        def save(_profiles: list, _tasks: list, preferences: dict) -> None:
            saved.append(preferences["value"])
            if len(saved) == 1:
                started.set()
                self.assertTrue(release.wait(3))

        writer = StateWriter(self.fail, save_fn=save)
        self.addCleanup(lambda: (release.set(), writer.close(3)))
        writer.submit([], [], {"value": 1})
        self.assertTrue(started.wait(2))
        final_revision = writer.submit([], [], {"value": 2})
        self.assertFalse(writer.close(timeout=0, flush=True))
        with self.assertRaises(RuntimeError):
            writer.submit([], [], {"value": 3})
        release.set()
        self.assertTrue(writer.close(timeout=3, flush=True))
        self.assertTrue(writer.is_idle)
        self.assertTrue(writer.is_closed)
        self.assertEqual(saved, [1, 2])
        self.assertEqual(writer.saved_revision, final_revision)

    def test_failed_save_notifies_with_redacted_error_and_later_submission_recovers(self) -> None:
        messages: list[str] = []
        saved_revisions: list[int] = []
        failure = threading.Event()

        def save(_profiles: list, tasks: list, _preferences: dict) -> None:
            if tasks[0]["id"] == "failure":
                failure.set()
                raise OSError("Could not save secret-key or access-key")

        writer = StateWriter(messages.append, saved_revisions.append, save_fn=save)
        self.addCleanup(lambda: writer.close(3))
        failed = writer.submit([{"secret_access_key": "secret-key", "access_key_id": "access-key"}], [{"id": "failure"}], {})
        self.assertTrue(failure.wait(2))
        self.assertFalse(writer.flush(3))
        self.assertTrue(writer.is_idle)
        self.assertEqual(writer.completed_revision, failed)
        self.assertEqual(writer.saved_revision, 0)
        self.assertEqual(messages, ["Could not save [已隐藏] or [已隐藏]"])
        recovery = writer.submit([], [{"id": "recovery"}], {})
        self.assertTrue(writer.flush(3))
        self.assertEqual(writer.saved_revision, recovery)
        self.assertEqual(saved_revisions, [recovery])

    def test_callback_exceptions_do_not_kill_writer_or_prevent_flush(self) -> None:
        calls: list[int] = []

        def fail_callback(_value: object) -> None:
            raise RuntimeError("bad UI callback")

        writer = StateWriter(fail_callback, fail_callback, save_fn=lambda _p, _t, prefs: calls.append(prefs["revision"]))
        self.addCleanup(lambda: writer.close(3))
        writer.submit([], [], {"revision": 1})
        self.assertTrue(writer.flush(3))
        writer.submit([], [], {"revision": 2})
        self.assertTrue(writer.flush(3))
        self.assertEqual(calls, [1, 2])

    def test_default_store_writes_latest_flat_state_atomically_without_plaintext_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "client-state.json"
            with patch.object(client_store, "state_path", return_value=path):
                writer = StateWriter(self.fail)
                self.addCleanup(lambda: writer.close(3))
                profiles = [{"id": "p", "name": "中文连接", "bucket": "bucket", "remember_secret": False, "secret_access_key": "never-save-secret"}]
                tasks = [{"id": "t", "key": "folder/file", "status": "paused", "size": 1000, "transferred": 400, "etag": '"etag-before"'}]
                writer.submit(profiles, tasks, {"workers": 4})
                self.assertTrue(writer.close(3))
                stored = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(stored["tasks"][0]["transferred"], 400)
                self.assertEqual(stored["tasks"][0]["etag"], '"etag-before"')
                self.assertNotIn("never-save-secret", path.read_text(encoding="utf-8"))
                self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
