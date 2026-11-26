"""Production transfer tests: exact local writes, cancellation and real SDK flow."""
from __future__ import annotations

import io
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import boto3
from botocore.response import StreamingBody
from botocore.stub import Stubber

from r2_batch_download import client_transfer
from r2_batch_download.desktop_engine import Connection


CONNECTION = Connection("offlineaccount", "test-access-sensitive", "test-secret-sensitive", "bucket")


class TransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.client = Mock()
        self.payloads = {"a.bin": b"abc", "b.bin": b"second", "folder/c.bin": b"nested"}
        self.client.download_fileobj.side_effect = self.write_file
        self.create = self.enterContext(patch.object(client_transfer, "create_client", return_value=self.client))
        self.events: list[dict] = []
        self.cancels: dict[str, threading.Event] = {}

    def task(self, key: str, task_id: str | None = None, **options: object) -> dict:
        return {
            "id": task_id or key, "key": key, "size": len(self.payloads.get(key, b"abc")),
            "output_dir": str(self.root), "bucket": "bucket", "overwrite": False,
            **options,
        }

    def write_file(self, bucket: str, key: str, fileobj: object, Callback: object, Config: object) -> None:
        self.assertEqual(bucket, "bucket")
        self.assertFalse(Config.use_threads)
        self.assertEqual(Config.num_download_attempts, 5)
        payload = self.payloads.get(key, b"abc")
        fileobj.write(payload)
        Callback(len(payload))

    def transfer(self, tasks: list[dict], workers: int = 2) -> dict[str, dict]:
        result = client_transfer.transfer_tasks(CONNECTION, tasks, self.cancels, workers, self.events.append)
        self.assertIsNone(result)
        results = [event for event in self.events if event["kind"] == "result"]
        self.assertEqual(len(results), len(tasks))
        self.assertEqual(len({result["id"] for result in results}), len(tasks))
        return {result["id"]: result for result in results}

    def test_writes_multiple_real_files_and_reports_terminal_bytes(self) -> None:
        tasks = [self.task("a.bin"), self.task("b.bin"), self.task("folder/c.bin")]
        results = self.transfer(tasks)
        for key, payload in self.payloads.items():
            self.assertEqual((self.root / key).read_bytes(), payload)
            self.assertEqual(results[key]["status"], "completed")
            self.assertEqual(results[key]["transferred"], len(payload))
            self.assertEqual(Path(results[key]["path"]).resolve(), (self.root / key).resolve())
        self.assertEqual(sum(event["bytes"] for event in self.events if event["kind"] == "progress"), sum(map(len, self.payloads.values())))
        self.assertEqual(list(self.root.glob("**/*.part")), [])
        self.client.close.assert_called_once()
        self.create.assert_called_once_with(CONNECTION, workers=2)

    def test_same_size_is_skipped_and_different_size_preserved_without_overwrite(self) -> None:
        (self.root / "a.bin").write_bytes(b"old")
        (self.root / "b.bin").write_bytes(b"local data must survive")
        results = self.transfer([self.task("a.bin"), self.task("b.bin")])
        self.assertEqual(results["a.bin"]["status"], "skipped")
        self.assertEqual(results["a.bin"]["transferred"], 3)
        self.assertEqual(results["b.bin"]["status"], "failed")
        self.assertEqual((self.root / "a.bin").read_bytes(), b"old")
        self.assertEqual((self.root / "b.bin").read_bytes(), b"local data must survive")
        self.client.download_fileobj.assert_not_called()

    def test_explicit_overwrite_commits_new_complete_file(self) -> None:
        (self.root / "a.bin").write_bytes(b"outdated")
        result = self.transfer([self.task("a.bin", overwrite=True)])["a.bin"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual((self.root / "a.bin").read_bytes(), b"abc")

    def test_failure_cleans_partial_preserves_existing_and_continues_other_files(self) -> None:
        (self.root / "a.bin").write_bytes(b"keep")

        def fail_one(bucket: str, key: str, fileobj: object, Callback: object, Config: object) -> None:
            if key == "a.bin":
                fileobj.write(b"part")
                Callback(4)
                raise OSError("network " + CONNECTION.secret_access_key + " " + CONNECTION.access_key_id)
            self.write_file(bucket, key, fileobj, Callback, Config)

        self.client.download_fileobj.side_effect = fail_one
        results = self.transfer([self.task("a.bin", overwrite=True), self.task("b.bin")])
        self.assertEqual(results["a.bin"]["status"], "failed")
        self.assertEqual(results["a.bin"]["transferred"], 0)
        self.assertNotIn(CONNECTION.secret_access_key, results["a.bin"]["error"])
        self.assertNotIn(CONNECTION.access_key_id, results["a.bin"]["error"])
        self.assertEqual(results["b.bin"]["status"], "completed")
        self.assertEqual((self.root / "a.bin").read_bytes(), b"keep")
        self.assertEqual((self.root / "b.bin").read_bytes(), b"second")
        self.assertEqual(list(self.root.glob("**/*.part")), [])

    def test_size_mismatch_never_becomes_a_completed_file(self) -> None:
        results = self.transfer([self.task("a.bin", size=9), self.task("b.bin")])
        self.assertEqual(results["a.bin"]["status"], "failed")
        self.assertEqual(results["b.bin"]["status"], "completed")
        self.assertFalse((self.root / "a.bin").exists())
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_cancellation_is_per_task_and_does_not_stop_other_files(self) -> None:
        self.cancels["a.bin"] = threading.Event()
        self.cancels["folder/c.bin"] = threading.Event()
        (self.root / "a.bin").write_bytes(b"keep")

        def cancel_one(bucket: str, key: str, fileobj: object, Callback: object, Config: object) -> None:
            if key == "a.bin":
                fileobj.write(b"part")
                self.cancels["a.bin"].set()
                self.cancels["folder/c.bin"].set()
                Callback(4)
            else:
                self.write_file(bucket, key, fileobj, Callback, Config)

        self.client.download_fileobj.side_effect = cancel_one
        results = self.transfer([
            self.task("a.bin", overwrite=True), self.task("b.bin"), self.task("folder/c.bin"),
        ], workers=1)
        self.assertEqual(results["a.bin"]["status"], "cancelled")
        self.assertEqual(results["folder/c.bin"]["status"], "cancelled")
        self.assertEqual(results["b.bin"]["status"], "completed")
        self.assertEqual((self.root / "a.bin").read_bytes(), b"keep")
        self.assertFalse((self.root / "folder/c.bin").exists())
        self.assertEqual(list(self.root.glob("**/*.part")), [])
        self.assertEqual(self.client.download_fileobj.call_count, 2)

    def test_pre_cancelled_tasks_do_not_create_client_or_folders(self) -> None:
        cancel = threading.Event()
        cancel.set()
        self.cancels["a.bin"] = cancel
        output = self.root / "not-created"
        results = self.transfer([self.task("a.bin", output_dir=str(output))])
        self.assertEqual(results["a.bin"]["status"], "cancelled")
        self.assertFalse(output.exists())
        self.create.assert_not_called()

    def test_sdk_retry_rollback_does_not_inflate_total_progress(self) -> None:
        def retry(bucket: str, key: str, fileobj: object, Callback: object, Config: object) -> None:
            Callback(3)
            Callback(-3)
            fileobj.write(b"abc")
            Callback(3)

        self.client.download_fileobj.side_effect = retry
        results = self.transfer([self.task("a.bin")])
        self.assertEqual(results["a.bin"]["status"], "completed")
        self.assertEqual(sum(event["bytes"] for event in self.events if event["kind"] == "progress"), 3)

    def test_case_and_file_directory_conflicts_cannot_overwrite_batch_files(self) -> None:
        with patch.object(client_transfer, "destination_identity", side_effect=lambda parts: tuple(part.casefold() for part in parts)):
            tasks = [self.task("a.bin", overwrite=True), self.task("A.bin", overwrite=True), self.task("a.bin/child", overwrite=True)]
            results = self.transfer(tasks)
        self.assertEqual(results["a.bin"]["status"], "completed")
        self.assertEqual(results["A.bin"]["status"], "failed")
        self.assertEqual(results["a.bin/child"]["status"], "failed")
        self.assertEqual((self.root / "a.bin").read_bytes(), b"abc")
        self.assertEqual(self.client.download_fileobj.call_count, 1)

    def test_separate_output_roots_allow_same_object_but_nested_roots_conflict(self) -> None:
        tasks = [
            self.task("a.bin", "one", output_dir=str(self.root / "first")),
            self.task("a.bin", "two", output_dir=str(self.root / "second")),
            self.task("b.bin", "nested", output_dir=str(self.root / "first" / "a.bin")),
        ]
        results = self.transfer(tasks)
        self.assertEqual(results["one"]["status"], "completed")
        self.assertEqual(results["two"]["status"], "completed")
        self.assertEqual(results["nested"]["status"], "failed")
        self.assertEqual((self.root / "first/a.bin").read_bytes(), b"abc")
        self.assertEqual((self.root / "second/a.bin").read_bytes(), b"abc")

    def test_existing_file_parent_failure_does_not_abort_the_batch(self) -> None:
        (self.root / "folder").write_bytes(b"local file")
        results = self.transfer([self.task("folder/c.bin"), self.task("b.bin")])
        self.assertEqual(results["folder/c.bin"]["status"], "failed")
        self.assertEqual(results["b.bin"]["status"], "completed")
        self.assertEqual((self.root / "folder").read_bytes(), b"local file")

    def test_file_created_during_transfer_is_preserved(self) -> None:
        def concurrent_local_writer(bucket: str, key: str, fileobj: object, Callback: object, Config: object) -> None:
            self.write_file(bucket, key, fileobj, Callback, Config)
            (self.root / key).write_bytes(b"created locally during transfer")

        self.client.download_fileobj.side_effect = concurrent_local_writer
        result = self.transfer([self.task("a.bin")])["a.bin"]
        self.assertEqual(result["status"], "failed")
        self.assertEqual((self.root / "a.bin").read_bytes(), b"created locally during transfer")
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_path_stripping_is_applied_without_flattening_subdirectories(self) -> None:
        result = self.transfer([self.task("folder/c.bin", prefix="folder/", strip_prefix=True)])["folder/c.bin"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual((self.root / "c.bin").read_bytes(), b"nested")
        self.assertFalse((self.root / "folder").exists())

    def test_bad_task_metadata_and_wrong_bucket_fail_without_network_transfer(self) -> None:
        tasks = [
            self.task("a.bin", size=-1),
            self.task("b.bin", bucket="other-bucket"),
            self.task("folder/c.bin", prefix="not-matching/", strip_prefix=True),
        ]
        results = self.transfer(tasks)
        self.assertTrue(all(result["status"] == "failed" for result in results.values()))
        self.client.download_fileobj.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_connection_setup_failure_finishes_each_task_and_redacts_secrets(self) -> None:
        self.create.side_effect = RuntimeError("setup " + CONNECTION.secret_access_key)
        results = self.transfer([self.task("a.bin"), self.task("b.bin")])
        self.assertTrue(all(result["status"] == "failed" for result in results.values()))
        self.assertTrue(all(CONNECTION.secret_access_key not in result["error"] for result in results.values()))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_worker_limit_is_enforced_while_files_transfer_in_parallel(self) -> None:
        lock = threading.Lock()
        active = 0
        peak = 0

        def slow_write(bucket: str, key: str, fileobj: object, Callback: object, Config: object) -> None:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.03)
                self.write_file(bucket, key, fileobj, Callback, Config)
            finally:
                with lock:
                    active -= 1

        self.client.download_fileobj.side_effect = slow_write
        tasks = [self.task(f"file-{index}.bin") for index in range(7)]
        results = self.transfer(tasks, workers=2)
        self.assertEqual(peak, 2)
        self.assertTrue(all(result["status"] == "completed" for result in results.values()))
        self.assertTrue(all((self.root / task["key"]).read_bytes() == b"abc" for task in tasks))

    def test_invalid_batch_ids_or_workers_are_rejected_before_work(self) -> None:
        for tasks, workers in (([self.task("a.bin"), self.task("b.bin", "a.bin")], 2), ([self.task("a.bin")], 0)):
            with self.subTest(workers=workers), self.assertRaises(ValueError):
                client_transfer.transfer_tasks(CONNECTION, tasks, {}, workers, self.events.append)
        self.create.assert_not_called()
        self.assertEqual(self.events, [])


class RealSdkTransferTests(unittest.TestCase):
    def test_real_sdk_writes_streamed_bytes_and_consumes_head_get_contract(self) -> None:
        payload = b"real SDK, offline transport, exact committed bytes"
        client = boto3.client(
            "s3", endpoint_url="https://example.r2.cloudflarestorage.com", region_name="auto",
            aws_access_key_id="offline-access", aws_secret_access_key="offline-secret",
        )
        self.addCleanup(client.close)
        with tempfile.TemporaryDirectory() as temp, Stubber(client) as stub:
            stub.add_response("head_object", {"ContentLength": len(payload)})
            stub.add_response("get_object", {"ContentLength": len(payload), "Body": StreamingBody(io.BytesIO(payload), len(payload))})
            task = {"id": "sdk-task", "key": "folder/sdk.bin", "size": len(payload), "output_dir": temp, "overwrite": False}
            events = []
            with patch.object(client_transfer, "create_client", return_value=client):
                client_transfer.transfer_tasks(CONNECTION, [task], {}, 1, events.append)
            result = next(event for event in events if event["kind"] == "result")
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual((Path(temp) / "folder/sdk.bin").read_bytes(), payload)
            self.assertEqual(result["transferred"], len(payload))
            self.assertEqual(sum(event["bytes"] for event in events if event["kind"] == "progress"), len(payload))
            self.assertEqual(list(Path(temp).glob("**/*.part")), [])
            stub.assert_no_pending_responses()


if __name__ == "__main__":
    unittest.main()
