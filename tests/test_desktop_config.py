from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from r2_batch_download import desktop_config


class DesktopConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "preferences" / "settings.json"
        self.patcher = patch.object(desktop_config, "settings_path", return_value=self.path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_missing_config_does_not_prevent_startup(self) -> None:
        self.assertEqual(desktop_config.load_settings(), {})

    def test_default_save_preserves_options_but_not_secret_or_unknown_fields(self) -> None:
        values = {
            "account_id": "account-id", "access_key_id": "access-id",
            "secret_access_key": "private-secret", "bucket": "照片库",
            "endpoint_url": "https://test.r2.cloudflarestorage.com", "prefix": "photos/",
            "output_dir": "D:\\下载", "workers": 6, "overwrite": False, "strip_prefix": True,
            "random_payload": "do-not-persist", "protected_secret": "old-ciphertext",
        }
        desktop_config.save_settings(values)
        content = self.path.read_text(encoding="utf-8")
        self.assertNotIn("private-secret", content)
        self.assertNotIn("secret_access_key", content)
        self.assertNotIn("old-ciphertext", content)
        self.assertNotIn("random_payload", content)
        loaded = desktop_config.load_settings()
        self.assertEqual(loaded["bucket"], "照片库")
        self.assertEqual(loaded["workers"], 6)
        self.assertFalse(loaded["remember_secret"])
        self.assertTrue(loaded["strip_prefix"])

    def test_corrupt_config_returns_a_warning(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{broken", encoding="utf-8")
        self.assertIn("_warning", desktop_config.load_settings())
        self.path.write_text("[]", encoding="utf-8")
        self.assertIn("_warning", desktop_config.load_settings())

    def test_bad_dpapi_blob_keeps_non_secret_options_and_requests_new_key(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({
            "bucket": "bucket", "remember_secret": True,
            "secret_protection": "windows-dpapi-current-user-v1", "protected_secret": "not-valid!!!",
        }), encoding="utf-8")
        loaded = desktop_config.load_settings()
        self.assertEqual(loaded["bucket"], "bucket")
        self.assertIn("_warning", loaded)
        self.assertNotIn("secret_access_key", loaded)
        self.assertFalse(loaded["remember_secret"])

    def test_unchecking_remember_removes_previous_ciphertext(self) -> None:
        with patch.object(desktop_config, "_protect_secret", return_value="encrypted-value"):
            desktop_config.save_settings({"secret_access_key": "secret"}, remember_secret=True)
        self.assertIn("protected_secret", json.loads(self.path.read_text(encoding="utf-8")))
        desktop_config.save_settings({"bucket": "bucket", "secret_access_key": "secret"}, remember_secret=False)
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotIn("protected_secret", stored)
        self.assertNotIn("secret_protection", stored)
        self.assertFalse(stored["remember_secret"])

    def test_invalid_types_are_not_used_as_runtime_settings(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({
            "bucket": ["wrong"], "workers": True, "overwrite": "false",
            "strip_prefix": False, "remember_secret": "true", "secret_access_key": "unsafe-plaintext",
        }), encoding="utf-8")
        loaded = desktop_config.load_settings()
        self.assertNotIn("bucket", loaded)
        self.assertNotIn("workers", loaded)
        self.assertNotIn("overwrite", loaded)
        self.assertNotIn("secret_access_key", loaded)
        self.assertFalse(loaded["remember_secret"])

    def test_atomic_write_failure_preserves_old_settings_and_cleans_temp(self) -> None:
        desktop_config.save_settings({"bucket": "original"})
        with patch.object(desktop_config.os, "replace", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(ValueError, "无法保存配置"):
                desktop_config.save_settings({"bucket": "replacement"})
        self.assertEqual(desktop_config.load_settings()["bucket"], "original")
        self.assertEqual([item.name for item in self.path.parent.iterdir()], ["settings.json"])

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI is only available on Windows")
    def test_windows_dpapi_roundtrip_persists_no_plaintext_secret(self) -> None:
        secret = "private-secret-中文-123"
        desktop_config.save_settings({"bucket": "bucket", "secret_access_key": secret}, remember_secret=True)
        content = self.path.read_text(encoding="utf-8")
        self.assertNotIn(secret, content)
        self.assertNotIn("secret_access_key", content)
        self.assertEqual(desktop_config.load_settings()["secret_access_key"], secret)


if __name__ == "__main__":
    unittest.main()
