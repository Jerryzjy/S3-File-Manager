from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import boto3
from botocore.response import StreamingBody
from botocore.stub import Stubber

from r2_batch_download import cli


class BatchTests(unittest.TestCase):
    def run_batch(
        self,
        root: Path,
        payloads: dict[str, bytes],
        *,
        client: Mock | None = None,
        overwrite: bool = False,
    ) -> tuple[int, dict, Mock]:
        client = client or Mock()
        client.get_paginator.return_value.paginate.return_value = [
            {"Contents": [{"Key": key, "Size": len(data)} for key, data in payloads.items()]}
        ]
        if not client.download_fileobj.side_effect:
            client.download_fileobj.side_effect = lambda _bucket, key, fileobj: fileobj.write(payloads[key])
        args = cli.build_parser().parse_args([
            "--json", "download", "--bucket", "bucket", "--out", str(root), "--workers", "3",
            *(["--overwrite"] if overwrite else []),
        ])
        output = io.StringIO()
        with patch.object(cli, "make_client", return_value=client), redirect_stdout(output):
            code = cli.run_download(args)
        return code, json.loads(output.getvalue()), client

    def test_local_directory_error_fails_one_object_and_continues_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            # Existing local file makes mkdir fail for this key, independent of
            # object scheduling order or whether the process has admin rights.
            (root / "blocked").write_bytes(b"keep me")
            code, result, client = self.run_batch(root, {"blocked/file.bin": b"bad", "ok.bin": b"complete"})
            self.assertEqual(code, 1)
            self.assertEqual((result["downloaded"], result["failed"], result["total"]), (1, 1, 2))
            by_key = {row["key"]: row for row in result["results"]}
            self.assertEqual(by_key["blocked/file.bin"]["status"], "failed")
            self.assertEqual(by_key["ok.bin"]["status"], "downloaded")
            self.assertEqual((root / "blocked").read_bytes(), b"keep me")
            self.assertEqual((root / "ok.bin").read_bytes(), b"complete")
            self.assertEqual(client.download_fileobj.call_count, 1)

    def test_partial_remote_failure_cleans_up_and_downloads_other_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client = Mock()

            def transfer(_bucket: str, key: str, fileobj: object) -> None:
                if key == "failed.bin":
                    fileobj.write(b"partial")
                    raise OSError("interrupted transfer")
                fileobj.write(b"success")

            client.download_fileobj.side_effect = transfer
            code, result, _ = self.run_batch(root, {"failed.bin": b"incomplete", "ok.bin": b"success"}, client=client)
            self.assertEqual(code, 1)
            self.assertEqual((result["downloaded"], result["failed"]), (1, 1))
            self.assertFalse((root / "failed.bin").exists())
            self.assertEqual((root / "ok.bin").read_bytes(), b"success")
            self.assertEqual(list(root.glob(".r2-download-*.part")), [])

    def test_unexpected_worker_exception_is_recorded_and_batch_continues(self) -> None:
        original = cli.download_one

        def transfer(*args: object, **kwargs: object) -> dict:
            if args[2]["Key"] == "failed.bin":
                raise RuntimeError("worker crashed")
            return original(*args, **kwargs)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(cli, "download_one", side_effect=transfer):
                code, result, _ = self.run_batch(root, {"failed.bin": b"bad", "ok.bin": b"success"})
            self.assertEqual(code, 1)
            self.assertEqual((result["downloaded"], result["failed"]), (1, 1))
            self.assertEqual((root / "ok.bin").read_bytes(), b"success")

    def test_file_directory_conflicts_are_rejected_in_both_listing_orders(self) -> None:
        variants = [
            {"node": b"file", "node/child.bin": b"child"},
            {"node/child.bin": b"child", "node": b"file"},
        ]
        for payloads in variants:
            with self.subTest(order=list(payloads)), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                code, result, client = self.run_batch(root, payloads, overwrite=True)
                self.assertEqual(code, 1)
                self.assertEqual((result["downloaded"], result["failed"], result["total"]), (1, 1, 2))
                first_key = next(iter(payloads))
                self.assertEqual((root / first_key).read_bytes(), payloads[first_key])
                self.assertEqual(client.download_fileobj.call_count, 1)
                failed = next(row for row in result["results"] if row["status"] == "failed")
                self.assertIn("collides", failed["error"])

    @unittest.skipUnless(os.name == "nt", "case collision is specific to Windows path comparison")
    def test_windows_case_conflicts_never_overwrite_first_key(self) -> None:
        for overwrite in (False, True):
            with self.subTest(overwrite=overwrite), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                payloads = {"Folder/A.txt": b"first", "folder/a.txt": b"second"}
                code, result, client = self.run_batch(root, payloads, overwrite=overwrite)
                self.assertEqual(code, 1)
                self.assertEqual((result["downloaded"], result["failed"]), (1, 1))
                self.assertEqual((root / "Folder" / "A.txt").read_bytes(), b"first")
                self.assertEqual(client.download_fileobj.call_count, 1)


class SdkContractTests(unittest.TestCase):
    def setUp(self) -> None:
        # Explicit fake credentials prevent provider-chain/metadata requests.
        self.client = boto3.client(
            "s3",
            endpoint_url="https://example.r2.cloudflarestorage.com",
            region_name="auto",
            aws_access_key_id="offline-access-key",
            aws_secret_access_key="offline-secret-key",
        )

    def tearDown(self) -> None:
        self.client.close()

    def test_real_sdk_paginator_reads_continuation_token(self) -> None:
        with Stubber(self.client) as stub:
            stub.add_response("list_objects_v2", {
                "IsTruncated": True,
                "NextContinuationToken": "page-two",
                "Contents": [{"Key": "folder/a.bin", "Size": 3}],
            }, {"Bucket": "bucket", "Prefix": "folder/"})
            stub.add_response("list_objects_v2", {
                "IsTruncated": False,
                "Contents": [{"Key": "folder/b.bin", "Size": 4}],
            }, {"Bucket": "bucket", "Prefix": "folder/", "ContinuationToken": "page-two"})
            rows = list(cli.iter_objects(self.client, "bucket", "folder/"))
            self.assertEqual([item["Key"] for item in rows], ["folder/a.bin", "folder/b.bin"])
            stub.assert_no_pending_responses()

    def test_real_sdk_download_fileobj_writes_full_file(self) -> None:
        payload = b"verified offline SDK transfer"
        with tempfile.TemporaryDirectory() as temp, Stubber(self.client) as stub:
            # SDK additions such as ChecksumMode vary across boto3 versions;
            # request order and response consumption verify the transfer flow.
            stub.add_response("head_object", {"ContentLength": len(payload)})
            stub.add_response("get_object", {
                "ContentLength": len(payload),
                "Body": StreamingBody(io.BytesIO(payload), len(payload)),
            })
            root = Path(temp)
            result = cli.download_one(self.client, "bucket", {"Key": "folder/file.bin", "Size": len(payload)}, root, "", False, False)
            self.assertEqual(result["status"], "downloaded", result)
            self.assertEqual((root / "folder" / "file.bin").read_bytes(), payload)
            self.assertEqual(list(root.glob("**/*.part")), [])
            stub.assert_no_pending_responses()


if __name__ == "__main__":
    unittest.main()
