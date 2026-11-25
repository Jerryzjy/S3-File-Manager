from __future__ import annotations

import tempfile
import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from r2_batch_download.cli import Settings, comparison_path, destination_identity, download_one, iter_objects, path_conflicts, safe_destination


class CliTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Win32 resolved-path representation")
    def test_mixed_extended_resolved_forms_still_accept_same_root(self) -> None:
        root = Path("C:/downloads")
        destination = root / "folder" / "file.bin"
        with patch.object(type(root), "resolve", side_effect=[root, Path(r"\\?\C:\downloads\folder\file.bin")]):
            self.assertEqual(safe_destination(root, "folder/file.bin"), destination)
        with patch.object(type(root), "resolve", side_effect=[Path(r"\\?\C:\downloads"), destination]):
            self.assertEqual(safe_destination(root, "folder/file.bin"), destination)

    @unittest.skipUnless(os.name == "nt", "Win32 UNC resolved-path representation")
    def test_extended_unc_paths_compare_equally_but_outside_target_is_rejected(self) -> None:
        self.assertEqual(comparison_path(Path(r"\\?\UNC\server\share\folder")), Path(r"\\server\share\folder"))
        root = Path(r"\\server\share\downloads")
        with patch.object(type(root), "resolve", side_effect=[root, Path(r"\\?\UNC\server\share\downloads\file.bin")]):
            self.assertEqual(safe_destination(root, "file.bin"), root / "file.bin")
        with patch.object(type(root), "resolve", side_effect=[Path("C:/downloads"), Path(r"\\?\C:\outside\file.bin")]):
            with self.assertRaisesRegex(ValueError, "outside"):
                safe_destination(Path("C:/downloads"), "file.bin")

    def test_settings_report_missing_credentials_without_values(self) -> None:
        settings = Settings(None, None, None, None, None)
        missing = settings.missing()
        self.assertIn("R2_ACCESS_KEY_ID (or AWS_ACCESS_KEY_ID)", missing)
        self.assertIn("R2_SECRET_ACCESS_KEY (or AWS_SECRET_ACCESS_KEY)", missing)
        self.assertTrue(all("secret-value" not in item for item in missing))

    def test_key_path_is_nested_under_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(safe_destination(root, "images/2026/a.png"), root / "images" / "2026" / "a.png")
            # Dot segments are encoded, never interpreted as a parent directory.
            self.assertTrue(safe_destination(root, "../outside.txt").is_relative_to(root))

    def test_path_identity_follows_target_filesystem_case_rules(self) -> None:
        identity = destination_identity(("Folder", "A.txt"))
        if __import__("os").name == "nt":
            self.assertEqual(identity, ("folder", "a.txt"))
        else:
            self.assertEqual(identity, ("Folder", "A.txt"))

    def test_path_conflicts_cover_duplicate_files_and_file_directory_prefixes(self) -> None:
        self.assertTrue(path_conflicts(("a",), {("a", "child")}, {("a",)}))
        self.assertTrue(path_conflicts(("a", "child"), {("a",)}, set()))
        self.assertFalse(path_conflicts(("b", "child"), {("a",)}, set()))

    def test_list_objects_uses_all_paginator_pages(self) -> None:
        first = {"Contents": [{"Key": "a", "Size": 1}]}
        second = {"Contents": [{"Key": "b", "Size": 2}]}
        paginator = Mock()
        paginator.paginate.return_value = [first, second]
        client = Mock()
        client.get_paginator.return_value = paginator
        self.assertEqual([row["Key"] for row in iter_objects(client, "bucket", "folder/")], ["a", "b"])
        paginator.paginate.assert_called_once_with(Bucket="bucket", Prefix="folder/")

    def test_download_writes_atomically_and_skips_matching_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = b"abc"
            client = Mock()

            def write_file(_bucket: str, _key: str, fileobj: object) -> None:
                fileobj.write(payload)

            client.download_fileobj.side_effect = write_file
            item = {"Key": "folder/file.bin", "Size": len(payload)}
            result = download_one(client, "bucket", item, root, "", False, False)
            self.assertEqual(result["status"], "downloaded")
            self.assertEqual((root / "folder" / "file.bin").read_bytes(), payload)
            skipped = download_one(client, "bucket", item, root, "", False, False)
            self.assertEqual(skipped["status"], "skipped")
            self.assertEqual(client.download_fileobj.call_count, 1)

    def test_failed_download_does_not_leave_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client = Mock()

            def write_partial_then_fail(_bucket: str, _key: str, fileobj: object) -> None:
                fileobj.write(b"partial")
                raise OSError("network error")

            client.download_fileobj.side_effect = write_partial_then_fail
            result = download_one(client, "bucket", {"Key": "file.bin", "Size": 9}, root, "", False, False)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(list(root.iterdir()), [])

    def test_object_failure_redacts_configured_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client = Mock()
            client.download_fileobj.side_effect = OSError("failure test-secret and test-access")
            result = download_one(
                client, "bucket", {"Key": "file.bin", "Size": 3}, Path(temp),
                "", False, False, ("test-access", "test-secret"),
            )
            self.assertEqual(result["status"], "failed")
            self.assertNotIn("test-secret", result["error"])
            self.assertNotIn("test-access", result["error"])


if __name__ == "__main__":
    unittest.main()
