from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from r2_batch_download.desktop_engine import Cancelled
from r2_batch_download.download_preflight import inspect_download


class DownloadPreflightTests(unittest.TestCase):
    def test_skip_and_overwrite_match_transfer_policy_and_space_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "same.txt").write_bytes(b"123")
            (root / "different.txt").write_bytes(b"1")
            files = [{"key": f"fruit/{name}.txt", "size": size}
                     for name, size in [("same", 3), ("different", 4), ("new", 10)]]
            skip = inspect_download(files, directory, "fruit/", True, True, threading.Event())
            overwrite = inspect_download(files, directory, "fruit/", True, False, threading.Event())

            self.assertEqual((skip.count, skip.total, skip.required), (3, 17, 10))
            self.assertEqual((skip.existing, skip.skipped, skip.different_size), (2, 1, 1))
            self.assertIn("fruit/different.txt", skip.examples[0])
            self.assertEqual((overwrite.required, overwrite.existing, overwrite.skipped, overwrite.different_size), (17, 2, 0, 0))
            self.assertEqual((root / "different.txt").read_bytes(), b"1")

    def test_reports_file_directory_collisions_and_occupied_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "occupied").write_bytes(b"a file")
            (root / "existing-folder").mkdir()
            files = [{"key": key, "size": 1} for key in
                     ("same/a.txt", "same", "occupied/a.txt", "existing-folder")]
            result = inspect_download(files, directory, "", False, True, threading.Event())

            self.assertEqual((result.issues, result.required), (3, 1))
            self.assertEqual(len(result.examples), 3)
            self.assertIn("文件与目录重名", result.examples[0])
            self.assertIn("occupied/a.txt", result.examples[1])
            self.assertIn("existing-folder", result.examples[2])

    def test_missing_destination_uses_existing_parent_and_keeps_it_unmodified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "missing" / "deeper"
            with patch.object(shutil, "disk_usage", return_value=type("Usage", (), {"free": 7})()) as disk:
                result = inspect_download([{"key": "a.txt", "size": 10}], str(target), "", False, True, threading.Event())

            disk.assert_called_once_with(root.resolve())
            self.assertGreater(result.required, result.free)
            self.assertFalse(target.exists())

    def test_cancellation_stops_checks_before_accessing_destination(self) -> None:
        cancel = threading.Event()
        cancel.set()
        with patch.object(shutil, "disk_usage") as disk:
            with self.assertRaises(Cancelled):
                inspect_download([{"key": "a.txt", "size": 10}], "unused", "", False, True, cancel)
        disk.assert_not_called()
