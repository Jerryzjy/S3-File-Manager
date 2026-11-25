from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from r2_batch_download import client_store


class ClientStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "R2BatchDownloader" / "client-state.json"
        self.enterContext(patch.object(client_store, "state_path", return_value=self.path))
        self.profile = {
            "id": "profile-1", "name": "生产素材", "bucket": "real-assets",
            "account_id": "account-id", "access_key_id": "access-id",
            "secret_access_key": "private-secret-中文", "endpoint_url": "",
            "remember_secret": False,
        }
        self.task = {
            "id": "task-1", "key": "照片/封面.jpg", "name": "封面.jpg",
            "bucket": "real-assets", "profile_id": "profile-1", "status": "completed",
            "size": 1024, "transferred": 1024, "error": "", "path": "D:/下载/照片/封面.jpg",
            "output_dir": "D:/下载", "workers": 4, "overwrite": False,
            "strip_prefix": False, "prefix": "", "created_at": "2026-11-25T14:20:00+08:00",
        }
        self.preferences = {"output_dir": "D:/下载", "workers": 6, "skip_existing": True}

    def _stored(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, value: object) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def test_first_launch_is_empty_and_does_not_import_legacy_or_preview_data(self) -> None:
        self.path.parent.mkdir(parents=True)
        legacy = self.path.parent / "settings.json"
        legacy.write_text(json.dumps(self.profile), encoding="utf-8")
        state = client_store.load_state()
        self.assertEqual(state["profiles"], [])
        self.assertEqual(state["tasks"], [])
        self.assertEqual(state["preferences"]["workers"], 4)
        self.assertTrue(state["preferences"]["skip_existing"])
        self.assertNotIn("_warning", state)
        self.assertFalse(self.path.exists())
        self.assertEqual(json.loads(legacy.read_text(encoding="utf-8")), self.profile)

    def test_roundtrip_saves_allowed_metadata_but_not_plaintext_or_extra_credentials(self) -> None:
        profile = {**self.profile, "protected_secret": "forged-ciphertext", "token": "profile-token"}
        task = {
            **self.task, "secret_access_key": "unexpected-secret", "access_key_id": "unexpected-access",
            "connection": self.profile, "headers": {"Authorization": "private-header"},
            "error": "Request failed: access-id private-secret-中文",
        }
        before = copy.deepcopy((profile, task, self.preferences))
        client_store.save_state([profile], [task], {**self.preferences, "secret_access_key": "preference-secret"})
        content = self.path.read_text(encoding="utf-8")
        for secret in ("private-secret-中文", "unexpected-secret", "unexpected-access", "private-header", "profile-token", "forged-ciphertext", "preference-secret"):
            self.assertNotIn(secret, content)
        stored_task = self._stored()["tasks"][0]
        self.assertNotIn("connection", stored_task)
        self.assertNotIn("secret_access_key", self._stored()["profiles"][0])
        self.assertEqual(stored_task["error"], "Request failed: [已隐藏] [已隐藏]")
        state = client_store.load_state()
        self.assertEqual(state["profiles"][0]["secret_access_key"], "")
        self.assertEqual(state["preferences"], self.preferences)
        self.assertEqual(state["tasks"][0]["status"], "completed")
        self.assertEqual((profile, task, self.preferences), before)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI is only available on Windows")
    def test_windows_dpapi_remembers_secret_without_plaintext_on_disk(self) -> None:
        profile = {**self.profile, "remember_secret": True}
        client_store.save_state([profile], [], self.preferences)
        content = self.path.read_text(encoding="utf-8")
        self.assertNotIn(profile["secret_access_key"], content)
        self.assertNotIn("secret_access_key", content)
        self.assertEqual(self._stored()["profiles"][0]["secret_protection"], "windows-dpapi-current-user-v1")
        state = client_store.load_state()
        self.assertEqual(state["profiles"][0]["secret_access_key"], profile["secret_access_key"])
        self.assertTrue(state["profiles"][0]["remember_secret"])

    def test_removing_profile_or_unchecking_remember_drops_its_ciphertext(self) -> None:
        profile = {**self.profile, "remember_secret": True}
        with patch.object(client_store, "_protect_secret", return_value="encrypted-one"):
            client_store.save_state([profile], [self.task], self.preferences)
        self.assertIn("encrypted-one", self.path.read_text(encoding="utf-8"))
        client_store.save_state([self.profile], [self.task], self.preferences)
        self.assertNotIn("protected_secret", self.path.read_text(encoding="utf-8"))
        with patch.object(client_store, "_protect_secret", return_value="encrypted-two"):
            client_store.save_state([profile], [self.task], self.preferences)
        client_store.save_state([], [], self.preferences)
        content = self.path.read_text(encoding="utf-8")
        self.assertNotIn("encrypted-two", content)
        self.assertEqual(client_store.load_state()["profiles"], [])
        self.assertEqual(client_store.load_state()["tasks"], [])

    def test_restart_marks_pending_or_running_tasks_interrupted_without_auto_start(self) -> None:
        tasks = [{**self.task, "id": f"task-{index}", "status": status, "transferred": 100} for index, status in enumerate(("queued", "downloading", "cancelling"))]
        client_store.save_state([self.profile], tasks, self.preferences)
        state = client_store.load_state()
        self.assertTrue(all(task["status"] == "interrupted" for task in state["tasks"]))
        self.assertTrue(all("手动重试" in task["error"] for task in state["tasks"]))
        self.assertEqual([task["transferred"] for task in state["tasks"]], [100, 100, 100])
        # Loading itself is read-only and does not rewrite persisted statuses.
        self.assertEqual([task["status"] for task in self._stored()["tasks"]], ["queued", "downloading", "cancelling"])

    def test_inconsistent_complete_record_never_becomes_false_success(self) -> None:
        client_store.save_state([self.profile], [{**self.task, "transferred": 100}], self.preferences)
        task = client_store.load_state()["tasks"][0]
        self.assertEqual(task["status"], "interrupted")
        self.assertIn("不一致", task["error"])

    def test_connection_fingerprint_is_retained_as_nonsecret_retry_context(self) -> None:
        fingerprint = "abcd0123" * 8
        client_store.save_state([self.profile], [{**self.task, "connection_fingerprint": fingerprint}], self.preferences)
        self.assertEqual(client_store.load_state()["tasks"][0]["connection_fingerprint"], fingerprint)

    def test_pause_checkpoint_metadata_survives_restart_without_auto_resuming(self) -> None:
        tasks = [
            {**self.task, "id": "paused", "status": "paused", "transferred": 400, "etag": '"remote-one"'},
            {**self.task, "id": "pausing", "status": "pausing", "transferred": 250, "etag": '"remote-two"'},
        ]
        client_store.save_state([self.profile], tasks, self.preferences)
        state = client_store.load_state()
        self.assertEqual([task["status"] for task in state["tasks"]], ["paused", "paused"])
        self.assertEqual([task["transferred"] for task in state["tasks"]], [400, 250])
        self.assertEqual([task["etag"] for task in state["tasks"]], ['"remote-one"', '"remote-two"'])
        self.assertIn("点击继续", state["tasks"][1]["error"])
        self.assertEqual([task["status"] for task in self._stored()["tasks"]], ["paused", "pausing"])

    def test_corrupt_file_or_invalid_schema_gives_warning_and_preserves_file(self) -> None:
        self.path.parent.mkdir(parents=True)
        for content in ("{invalid", "[]", '{"version": 99}'):
            self.path.write_text(content, encoding="utf-8")
            state = client_store.load_state()
            self.assertIn("_warning", state)
            self.assertEqual(state["profiles"], [])
            self.assertEqual(state["tasks"], [])
            self.assertEqual(self.path.read_text(encoding="utf-8"), content)

    def test_malformed_entries_do_not_poison_valid_profile_task_or_settings(self) -> None:
        self._write({
            "version": 1,
            "profiles": [self.profile, {"id": True, "bucket": []}, dict(self.profile)],
            "tasks": [self.task, {**self.task, "id": "bad-task", "size": True}, {**self.task, "id": "bad-status", "status": "unknown"}],
            "preferences": {"output_dir": [], "workers": True, "skip_existing": "yes"},
        })
        state = client_store.load_state()
        self.assertIn("_warning", state)
        self.assertEqual([profile["id"] for profile in state["profiles"]], [self.profile["id"]])
        self.assertEqual([task["id"] for task in state["tasks"]], [self.task["id"]])
        self.assertEqual(state["preferences"]["workers"], 4)
        self.assertTrue(state["preferences"]["skip_existing"])

    def test_invalid_or_undecryptable_secret_keeps_profile_and_requests_reentry(self) -> None:
        for protected in (None, "not-valid!!!"):
            self._write({"profiles": [{**self.profile, "remember_secret": True, "secret_protection": "windows-dpapi-current-user-v1", "protected_secret": protected}]})
            state = client_store.load_state()
            self.assertEqual(state["profiles"][0]["bucket"], self.profile["bucket"])
            self.assertEqual(state["profiles"][0]["secret_access_key"], "")
            self.assertFalse(state["profiles"][0]["remember_secret"])
            self.assertIn("_warning", state)

    def test_write_failure_and_encryption_failure_preserve_old_state(self) -> None:
        client_store.save_state([self.profile], [self.task], self.preferences)
        content = self.path.read_text(encoding="utf-8")
        with patch.object(client_store.os, "replace", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(ValueError, "无法保存客户端数据"):
                client_store.save_state([], [], self.preferences)
        self.assertEqual(self.path.read_text(encoding="utf-8"), content)
        self.assertEqual([item.name for item in self.path.parent.iterdir()], ["client-state.json"])
        with patch.object(client_store, "_protect_secret", side_effect=OSError("failed")):
            with self.assertRaisesRegex(ValueError, "无法加密保存密钥"):
                client_store.save_state([{**self.profile, "remember_secret": True}], [], self.preferences)
        self.assertEqual(self.path.read_text(encoding="utf-8"), content)

    def test_runtime_validation_rejects_unsafe_types_before_overwriting_state(self) -> None:
        client_store.save_state([], [], self.preferences)
        content = self.path.read_text(encoding="utf-8")
        cases = [
            ([{**self.profile, "remember_secret": "yes"}], [], self.preferences),
            ([{**self.profile, "remember_secret": True, "secret_access_key": ""}], [], self.preferences),
            ([self.profile], [{**self.task, "key": "bad\x00key"}], self.preferences),
            ([self.profile], [{**self.task, "transferred": 2048}], self.preferences),
            ([], [], {"workers": 99}),
        ]
        for profiles, tasks, preferences in cases:
            with self.subTest(profiles=bool(profiles), tasks=bool(tasks), preferences=preferences):
                with self.assertRaises(ValueError):
                    client_store.save_state(profiles, tasks, preferences)
                self.assertEqual(self.path.read_text(encoding="utf-8"), content)


if __name__ == "__main__":
    unittest.main()
