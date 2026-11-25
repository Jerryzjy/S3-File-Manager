from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from r2_batch_download.desktop_engine import (
    Cancelled,
    Connection,
    DownloadOptions,
    create_client,
    download_batch,
    list_objects,
    validate_connection,
)


CONNECTION = Connection("testaccount", "access-sensitive", "secret-sensitive", "bucket")


class ConnectionTests(unittest.TestCase):
    def test_required_fields_are_rejected_without_echoing_credentials(self) -> None:
        for field_name in ("account_id", "access_key_id", "secret_access_key", "bucket"):
            with self.subTest(field=field_name):
                with self.assertRaises(ValueError) as caught:
                    validate_connection(replace(CONNECTION, **{field_name: ""}))
                self.assertNotIn(CONNECTION.access_key_id, str(caught.exception))
                self.assertNotIn(CONNECTION.secret_access_key, str(caught.exception))
        self.assertNotIn(CONNECTION.secret_access_key, repr(CONNECTION))
        self.assertNotIn(CONNECTION.access_key_id, repr(CONNECTION))

    def test_custom_endpoint_must_be_https_and_not_contain_credentials_or_path(self) -> None:
        validate_connection(replace(CONNECTION, account_id="", endpoint_url="https://example.r2.cloudflarestorage.com/"))
        for endpoint in ("http://example.com", "https://example.com/bucket", "https://user:secret@example.com", "https://example.com?token=secret", "https://example.com:invalid"):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    validate_connection(replace(CONNECTION, endpoint_url=endpoint))

    def test_real_sdk_uses_explicit_credentials_and_r2_settings(self) -> None:
        client = create_client(CONNECTION, workers=7)
        try:
            self.assertEqual(client.meta.endpoint_url, "https://testaccount.r2.cloudflarestorage.com")
            self.assertEqual(client.meta.region_name, "auto")
            self.assertEqual(client.meta.config.signature_version, "s3v4")
            self.assertEqual(client.meta.config.s3["addressing_style"], "path")
            self.assertEqual(client.meta.config.max_pool_connections, 7)
            self.assertEqual(client.meta.config.connect_timeout, 10)
        finally:
            client.close()


class ListingTests(unittest.TestCase):
    @patch("r2_batch_download.desktop_engine.create_client")
    def test_listing_paginates_and_closes_client(self, create: Mock) -> None:
        client = create.return_value
        client.get_paginator.return_value.paginate.return_value = [
            {"Contents": [{"Key": "folder/a", "Size": 1}]},
            {"Contents": [{"Key": "folder/b", "Size": 2}]},
        ]
        counts = []
        objects = list_objects(CONNECTION, "folder/", threading.Event(), counts.append)
        self.assertEqual([item["Key"] for item in objects], ["folder/a", "folder/b"])
        self.assertEqual(counts, [1, 2])
        client.get_paginator.return_value.paginate.assert_called_once_with(Bucket="bucket", Prefix="folder/")
        client.close.assert_called_once()

    @patch("r2_batch_download.desktop_engine.create_client")
    def test_cancelled_listing_does_not_fetch_another_page(self, create: Mock) -> None:
        cancel = threading.Event()
        fetched = []

        def pages():
            fetched.append(1)
            yield {"Contents": [{"Key": "a"}]}
            fetched.append(2)
            yield {"Contents": [{"Key": "b"}]}

        create.return_value.get_paginator.return_value.paginate.return_value = pages()
        with self.assertRaises(Cancelled):
            list_objects(CONNECTION, "", cancel, lambda count: cancel.set())
        self.assertEqual(fetched, [1])
        create.return_value.close.assert_called_once()

    @patch("r2_batch_download.desktop_engine.create_client")
    def test_listing_errors_redact_credentials(self, create: Mock) -> None:
        create.return_value.get_paginator.side_effect = RuntimeError("bad " + CONNECTION.secret_access_key + " " + CONNECTION.access_key_id)
        with self.assertRaises(RuntimeError) as caught:
            list_objects(CONNECTION, "", threading.Event())
        self.assertNotIn(CONNECTION.access_key_id, str(caught.exception))
        self.assertNotIn(CONNECTION.secret_access_key, str(caught.exception))


class DownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cancel = threading.Event()
        self.events = []
        self.client = Mock()
        self.client.download_fileobj.side_effect = self.write
        self.client_patch = patch("r2_batch_download.desktop_engine.create_client", return_value=self.client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    @staticmethod
    def write(bucket, key, fileobj, Callback, Config) -> None:
        fileobj.write(b"abc")
        Callback(3)

    def run_batch(self, objects, **options):
        return download_batch(
            CONNECTION, objects, DownloadOptions(str(self.root), **options),
            self.cancel, self.events.append,
        )

    def test_atomic_download_preserves_paths_and_reports_progress(self) -> None:
        summary = self.run_batch([{"Key": "photos/a.bin", "Size": 3}], prefix="photos/", strip_prefix=True)
        self.assertEqual((self.root / "a.bin").read_bytes(), b"abc")
        self.assertEqual(summary["downloaded"], 1)
        self.assertEqual(summary["bytes"], 3)
        self.assertEqual(sum(e["bytes"] for e in self.events if e["kind"] == "progress"), 3)
        self.assertEqual(self.events[-1]["kind"], "result")
        self.assertFalse(any(p.suffix == ".part" for p in self.root.iterdir()))
        self.client.close.assert_called_once()

    def test_same_size_existing_file_is_skipped_and_different_size_is_preserved(self) -> None:
        (self.root / "same.bin").write_bytes(b"old")
        (self.root / "different.bin").write_bytes(b"different")
        summary = self.run_batch([{"Key": "same.bin", "Size": 3}, {"Key": "different.bin", "Size": 3}])
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual((self.root / "same.bin").read_bytes(), b"old")
        self.assertEqual((self.root / "different.bin").read_bytes(), b"different")
        self.client.download_fileobj.assert_not_called()

    def test_failed_transfer_cleans_partial_and_does_not_replace_existing_file(self) -> None:
        (self.root / "a.bin").write_bytes(b"keep")

        def fail(bucket, key, fileobj, Callback, Config):
            fileobj.write(b"partial")
            raise OSError("failure " + CONNECTION.secret_access_key)

        self.client.download_fileobj.side_effect = fail
        summary = self.run_batch([{"Key": "a.bin", "Size": 3}], overwrite=True)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual((self.root / "a.bin").read_bytes(), b"keep")
        self.assertEqual(len(list(self.root.iterdir())), 1)
        self.assertNotIn(CONNECTION.secret_access_key, summary["results"][0]["error"])

    def test_cancelling_mid_transfer_preserves_existing_and_cancels_remaining_objects(self) -> None:
        (self.root / "a.bin").write_bytes(b"keep")

        def cancel_during_download(bucket, key, fileobj, Callback, Config):
            fileobj.write(b"partial")
            self.cancel.set()
            Callback(7)

        self.client.download_fileobj.side_effect = cancel_during_download
        summary = self.run_batch([{"Key": "a.bin", "Size": 3}, {"Key": "b.bin", "Size": 3}], workers=1, overwrite=True)
        self.assertEqual(summary["cancelled"], 2)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual((self.root / "a.bin").read_bytes(), b"keep")
        self.assertFalse((self.root / "b.bin").exists())
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_pre_cancelled_batch_never_connects_or_creates_output_directory(self) -> None:
        self.cancel.set()
        output = self.root / "not-created"
        with patch("r2_batch_download.desktop_engine.create_client") as create:
            summary = download_batch(
                CONNECTION, [{"Key": "a.bin", "Size": 3}],
                DownloadOptions(str(output)), self.cancel, self.events.append,
            )
        self.assertEqual(summary["cancelled"], 1)
        create.assert_not_called()
        self.assertFalse(output.exists())

    def test_file_directory_conflicts_do_not_abort_other_downloads(self) -> None:
        summary = self.run_batch([
            {"Key": "a", "Size": 3}, {"Key": "a/nested", "Size": 3}, {"Key": "b", "Size": 3},
        ], workers=1)
        self.assertEqual(summary["downloaded"], 2)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual((self.root / "a").read_bytes(), b"abc")
        self.assertEqual((self.root / "b").read_bytes(), b"abc")

    def test_windows_case_collisions_are_rejected_even_when_overwrite_selected(self) -> None:
        with patch("r2_batch_download.desktop_engine.destination_identity", side_effect=lambda parts: tuple(p.casefold() for p in parts)):
            summary = self.run_batch([{"Key": "a.bin", "Size": 3}, {"Key": "A.bin", "Size": 3}], overwrite=True)
        self.assertEqual(summary["downloaded"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(self.client.download_fileobj.call_count, 1)

    def test_bad_size_is_not_committed_and_other_files_continue(self) -> None:
        summary = self.run_batch([{"Key": "a.bin", "Size": 9}, {"Key": "b.bin", "Size": 3}])
        self.assertEqual(summary["downloaded"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertFalse((self.root / "a.bin").exists())
        self.assertEqual((self.root / "b.bin").read_bytes(), b"abc")


if __name__ == "__main__":
    unittest.main()
