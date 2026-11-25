"""Windowless Tk handler smoke tests; all settings/network/dialogs are mocked."""
from __future__ import annotations

import queue
import tkinter as tk
import unittest
from unittest.mock import patch

from r2_batch_download import desktop
from r2_batch_download.desktop_engine import Cancelled


class _DeferredThread:
    """Keep workers deterministic without touching Tk outside its main thread."""
    instances: list["_DeferredThread"] = []

    def __init__(self, *, target: object, **_kwargs: object) -> None:
        self.target = target
        self.started = False
        self.instances.append(self)

    def start(self) -> None:
        self.started = True

    def complete(self) -> None:
        self.target()


class DesktopUiTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.addCleanup(self._destroy_root)
        self.settings = self.enterContext(patch.object(desktop, "load_settings", return_value={}))
        self.save_settings = self.enterContext(patch.object(desktop, "save_settings"))
        self.dialogs = self.enterContext(patch.object(desktop, "messagebox"))
        self.enterContext(patch.object(self.root, "after", return_value="ignored-test-timer"))
        _DeferredThread.instances = []
        self.enterContext(patch.object(desktop.threading, "Thread", _DeferredThread))
        self.app = desktop.R2DownloaderApp(self.root)
        for name, value in {
            "account_id": "abc123", "access_key_id": "test-access", "secret_access_key": "test-secret",
            "bucket": "bucket", "output_dir": "D:/not-created-by-tests", "prefix": "photos/", "workers": "2",
        }.items():
            self.app.fields[name].set(value)

    def _destroy_root(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _drain_events(self) -> None:
        while True:
            try:
                event = self.app.events.get_nowait()
            except queue.Empty:
                return
            self.app._handle(event)

    def _worker(self) -> _DeferredThread:
        self.assertEqual(len(_DeferredThread.instances), 1)
        return _DeferredThread.instances[0]

    def _populate_context(self, objects: list[dict]) -> None:
        connection = desktop.Connection("abc123", "test-access", "test-secret", "bucket")
        self.app._handle({"kind": "listed", "objects": objects, "context": self.app._context(connection, "photos/")})

    def test_window_builds_with_masked_secret_and_no_implicit_persistence(self) -> None:
        self.assertEqual(self.app.secret_entry.cget("show"), "•")
        self.assertFalse(self.app.busy)
        self.assertEqual(self.app.tree.get_children(), ())
        self.save_settings.assert_not_called()
        self.app.fields["show_secret"].set(True)
        self.app._toggle_secret()
        self.assertEqual(self.app.secret_entry.cget("show"), "")

    def test_invalid_configuration_does_not_start_background_work(self) -> None:
        self.app.fields["secret_access_key"].set("")
        self.app._start("list")
        self.dialogs.showerror.assert_called_once()
        self.assertFalse(self.app.busy)
        self.assertEqual(_DeferredThread.instances, [])

    def test_listing_worker_only_queues_updates_until_main_thread_handles_them(self) -> None:
        objects = [{"Key": "photos/a.jpg", "Size": 12}, {"Key": "photos/b.jpg", "Size": 22}]
        with patch.object(desktop, "list_objects", return_value=objects) as list_objects:
            self.app._start("list")
            self.assertTrue(self.app.busy)
            self.assertEqual(str(self.app.stop_button.cget("state")), "normal")
            self._worker().complete()
            # Only the main-thread queue consumer changes the file table or busy state.
            self.assertEqual(self.app.tree.get_children(), ())
            self.assertTrue(self.app.busy)
            self._drain_events()
        self.assertEqual(len(self.app.tree.get_children()), 2)
        self.assertFalse(self.app.busy)
        self.assertEqual(self.app.progress_text.get(), "连接成功")
        self.assertEqual(list_objects.call_args.args[1], "photos/")
        self.save_settings.assert_not_called()

    def test_selected_download_uses_only_selected_cached_objects(self) -> None:
        objects = [{"Key": "photos/a.jpg", "Size": 12}, {"Key": "photos/b.jpg", "Size": 22}]
        self._populate_context(objects)
        self.app.tree.selection_set(self.app.row_by_key["photos/b.jpg"])
        summary = {"downloaded": 1, "skipped": 0, "failed": 0, "cancelled": 0, "total": 1, "bytes": 22}
        with patch.object(desktop, "download_batch", return_value=summary) as download_batch, patch.object(desktop, "list_objects") as list_objects:
            self.app._start("selected")
            self._worker().complete()
            self._drain_events()
        self.assertEqual(download_batch.call_args.args[1], [objects[1]])
        list_objects.assert_not_called()
        self.assertEqual(self.app.progress_text.get(), "下载完成")
        self.assertFalse(self.app.busy)

    def test_selection_is_rejected_if_prefix_changed_since_listing(self) -> None:
        self._populate_context([{"Key": "photos/a.jpg", "Size": 12}])
        self.app.tree.selection_set(self.app.row_by_key["photos/a.jpg"])
        self.app.fields["prefix"].set("other/")
        self.app._start("selected")
        self.dialogs.showerror.assert_called_once()
        self.assertIn("重新读取", self.dialogs.showerror.call_args.args[1])
        self.assertEqual(_DeferredThread.instances, [])

    def test_stop_during_listing_sets_cancel_and_returns_to_idle(self) -> None:
        def cancelled_listing(_connection: object, _prefix: str, cancel: object, **_kwargs: object) -> list:
            self.assertTrue(cancel.is_set())
            raise Cancelled()

        with patch.object(desktop, "list_objects", side_effect=cancelled_listing):
            self.app._start("list")
            self.app._stop()
            self.assertTrue(self.app.cancel.is_set())
            self.assertTrue(self.app.busy)
            self._worker().complete()
            self._drain_events()
        self.assertFalse(self.app.busy)
        self.assertEqual(self.app.progress_text.get(), "已停止")
        self.dialogs.showerror.assert_not_called()

    def test_error_redacts_credentials_and_keeps_app_usable(self) -> None:
        with patch.object(desktop, "list_objects", side_effect=RuntimeError("test-secret test-access request failed")):
            self.app._start("list")
            self._worker().complete()
            self._drain_events()
        self.assertFalse(self.app.busy)
        error = self.dialogs.showerror.call_args.args[1]
        self.assertNotIn("test-secret", error)
        self.assertNotIn("test-access", error)
        self.assertIn("已隐藏", error)

    def test_close_waits_for_worker_idle_before_destroying_window(self) -> None:
        self.dialogs.askyesno.return_value = True
        self.app._set_busy(True)
        self.app._close()
        self.assertTrue(self.app.closing)
        self.assertTrue(self.app.cancel.is_set())
        self.assertFalse(self.app.destroyed)
        self.assertTrue(self.root.winfo_exists())
        self.app._handle({"kind": "idle"})
        self.assertTrue(self.app.destroyed)

    def test_explicit_save_preserves_remember_choice_and_worker_number(self) -> None:
        self.app.fields["remember_secret"].set(True)
        self.app._save()
        values = self.save_settings.call_args.args[0]
        self.assertEqual(values["workers"], 2)
        self.assertEqual(values["secret_access_key"], "test-secret")
        self.assertTrue(self.save_settings.call_args.kwargs["remember_secret"])

    def test_completion_summary_survives_queue_batch_boundary(self) -> None:
        self.app._set_busy(True)
        self.app._handle({"kind": "batch", "total": 1})
        for _ in range(299):
            self.app.events.put({"kind": "progress", "key": "a", "bytes": 1})
        self.app.events.put({"kind": "finished", "summary": {
            "downloaded": 1, "skipped": 0, "failed": 0, "cancelled": 0,
        }})
        self.app.events.put({"kind": "idle"})
        self.app._poll()
        self.assertIn("成功 1", self.app.progress_detail.get())
        self.assertTrue(self.app.busy)
        self.app._poll()
        self.assertFalse(self.app.busy)
        self.assertIn("成功 1", self.app.progress_detail.get())


if __name__ == "__main__":
    unittest.main()
