"""Exact-byte pause/resume and HTTP range contract tests; no live service."""
from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import boto3
from botocore.response import StreamingBody
from botocore.stub import Stubber

from r2_batch_download import resumable_transfer as transfer
from r2_batch_download.desktop_engine import Connection


CONNECTION = Connection("offline-account", "sensitive-access-key", "sensitive-secret-key", "bucket")
ETAG = '"object-version-1"'


class Body(io.BytesIO):
    def __init__(self, payload: bytes, fail_after: int | None = None) -> None:
        super().__init__(payload)
        self.fail_after = fail_after

    def read(self, amount: int = -1) -> bytes:
        if self.fail_after is not None and self.tell() >= self.fail_after:
            raise OSError("connection interrupted")
        return super().read(amount)


class RangeServer:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.etags = {key: ETAG for key in payloads}
        self.calls: list[dict] = []
        self.closed_bodies: list[Body] = []
        self.failures = 0
        self.ignore_range = False
        self.close = Mock()

    def head_object(self, Bucket: str, Key: str) -> dict:
        return {"ContentLength": len(self.payloads[Key]), "ETag": self.etags[Key]}

    def get_object(self, **arguments: object) -> dict:
        self.calls.append(dict(arguments))
        payload = self.payloads[arguments["Key"]]
        etag = self.etags[arguments["Key"]]
        if arguments.get("IfMatch") != etag:
            raise AssertionError("Every range must be pinned to HEAD's validator")
        offset = int(str(arguments["Range"]).split("=")[1][:-1]) if arguments.get("Range") else 0
        if self.ignore_range:
            offset = 0
        fail_after = None
        if self.failures:
            self.failures -= 1
            fail_after = transfer.CHUNK_SIZE
        body = Body(payload[offset:], fail_after=fail_after)
        self.closed_bodies.append(body)
        result = {"ContentLength": len(payload) - offset, "ETag": etag, "Body": body,
                  "ResponseMetadata": {"HTTPStatusCode": 206 if offset else 200}}
        if offset:
            result["ContentRange"] = f"bytes {offset}-{len(payload) - 1}/{len(payload)}"
        return result


class ResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payload = b"a" * transfer.CHUNK_SIZE + b"b" * transfer.CHUNK_SIZE + b"final-bytes"
        self.server = RangeServer({"folder/file.bin": self.payload, "neighbor.bin": b"neighbour"})
        self.enterContext(patch.object(transfer, "create_client", return_value=self.server))
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("No network")))
        self.events: list[dict] = []
        self.cancels: dict[str, threading.Event] = {}
        self.pauses: dict[str, threading.Event] = {}

    def task(self, key: str = "folder/file.bin", task_id: str = "main", **changes: object) -> dict:
        return {"id": task_id, "key": key, "size": len(self.server.payloads[key]),
                "bucket": "bucket", "output_dir": str(self.root), "overwrite": False, **changes}

    def run_tasks(self, tasks: list[dict], callback: object | None = None, workers: int = 1) -> dict[str, dict]:
        self.events = []

        def receive(event: dict) -> None:
            self.events.append(event)
            if callback:
                callback(event)

        transfer.transfer_tasks(CONNECTION, tasks, self.cancels, workers, receive, self.pauses)
        return {event["id"]: event for event in self.events if event["kind"] == "result"}

    def pause_after_first_chunk(self, task: dict) -> dict:
        self.pauses[task["id"]] = threading.Event()

        def pause(event: dict) -> None:
            if event["kind"] == "progress" and event["id"] == task["id"]:
                self.pauses[task["id"]].set()

        result = self.run_tasks([task], pause)[task["id"]]
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["transferred"], transfer.CHUNK_SIZE)
        return result

    def test_pause_then_resume_requests_only_remaining_bytes_and_commits_exact_file(self) -> None:
        task = self.task()
        self.pause_after_first_chunk(task)
        self.assertFalse((self.root / task["key"]).exists())
        partials = list(self.root.rglob("*.part"))
        self.assertEqual(len(partials), 1)
        self.assertEqual(partials[0].read_bytes(), self.payload[:transfer.CHUNK_SIZE])
        metadata = next(self.root.rglob(".r2-*.json")).read_text(encoding="utf-8")
        self.assertNotIn(CONNECTION.secret_access_key, metadata)
        self.assertNotIn(CONNECTION.access_key_id, metadata)
        self.assertEqual(json.loads(metadata)["etag"], ETAG)
        self.pauses[task["id"]].clear()
        result = self.run_tasks([task])["main"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["transferred"], len(self.payload))
        self.assertEqual(self.server.calls[-1]["Range"], f"bytes={transfer.CHUNK_SIZE}-")
        self.assertEqual(self.server.calls[-1]["IfMatch"], ETAG)
        self.assertEqual((self.root / task["key"]).read_bytes(), self.payload)
        self.assertEqual([event["transferred"] for event in self.events if event["kind"] == "resumed"], [transfer.CHUNK_SIZE])
        self.assertEqual(sum(event["bytes"] for event in self.events if event["kind"] == "progress"), len(self.payload) - transfer.CHUNK_SIZE)
        self.assertEqual(list(self.root.rglob("*.part")), [])
        self.assertEqual(list(self.root.rglob(".r2-*.json")), [])
        self.assertTrue(all(body.closed for body in self.server.closed_bodies))

    def test_cancel_paused_checkpoint_removes_only_partial_and_keeps_existing_output(self) -> None:
        task = self.task(overwrite=True)
        destination = self.root / task["key"]
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"old local file")
        self.pause_after_first_chunk(task)
        transfer.discard_partial(task)
        self.assertEqual(destination.read_bytes(), b"old local file")
        self.assertEqual(list(self.root.rglob("*.part")), [])
        self.assertEqual(list(self.root.rglob(".r2-*.json")), [])
        transfer.discard_partial(task)  # Cleanup is idempotent.

    def test_remote_etag_change_discards_old_bytes_before_restarting(self) -> None:
        task = self.task()
        self.pause_after_first_chunk(task)
        replacement = b"z" * len(self.payload)
        self.server.payloads[task["key"]] = replacement
        self.server.etags[task["key"]] = '"new-object-version"'
        self.pauses["main"].clear()
        result = self.run_tasks([task])["main"]
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("Range", self.server.calls[-1])
        self.assertEqual(next(event for event in self.events if event["kind"] == "resumed")["transferred"], 0)
        self.assertEqual((self.root / task["key"]).read_bytes(), replacement)

    def test_changed_remote_size_never_commits_stale_catalog_file(self) -> None:
        task = self.task()
        self.pause_after_first_chunk(task)
        self.server.payloads[task["key"]] = b"remote size changed"
        self.pauses["main"].clear()
        result = self.run_tasks([task])["main"]
        self.assertEqual(result["status"], "failed")
        self.assertIn("大小已改变", result["error"])
        self.assertFalse((self.root / task["key"]).exists())
        self.assertEqual(list(self.root.rglob("*.part")), [])

    def test_server_ignoring_range_never_appends_full_payload_to_partial(self) -> None:
        task = self.task()
        self.pause_after_first_chunk(task)
        part = next(self.root.rglob("*.part"))
        before = part.read_bytes()
        self.pauses["main"].clear()
        self.server.ignore_range = True
        result = self.run_tasks([task])["main"]
        self.assertEqual(result["status"], "failed")
        self.assertIn("断点范围", result["error"])
        self.assertEqual(part.read_bytes(), before)
        self.assertFalse((self.root / task["key"]).exists())

    def test_stream_failure_retries_from_written_offset_without_duplicate_bytes(self) -> None:
        task = self.task()
        self.server.failures = 1
        result = self.run_tasks([task])["main"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.server.calls), 2)
        self.assertEqual(self.server.calls[1]["Range"], f"bytes={transfer.CHUNK_SIZE}-")
        self.assertEqual((self.root / task["key"]).read_bytes(), self.payload)

    def test_failure_retains_last_valid_partial_for_manual_retry(self) -> None:
        self.payload = b"q" * (transfer.CHUNK_SIZE * 4) + b"remaining"
        self.server.payloads["folder/file.bin"] = self.payload
        task = self.task()
        self.server.failures = 3
        result = self.run_tasks([task])["main"]
        self.assertEqual(result["status"], "failed")
        self.assertGreater(result["transferred"], 0)
        self.assertEqual(len(self.server.calls), 3)
        self.assertFalse((self.root / task["key"]).exists())
        self.server.failures = 0
        result = self.run_tasks([task])["main"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual((self.root / task["key"]).read_bytes(), self.payload)

    def test_cancel_one_file_cleans_its_partial_and_does_not_cancel_neighbor(self) -> None:
        task = self.task()
        self.cancels["main"] = threading.Event()

        def cancel(event: dict) -> None:
            if event["kind"] == "progress" and event["id"] == "main":
                self.cancels["main"].set()

        result = self.run_tasks([task, self.task("neighbor.bin", "neighbor")], cancel)
        self.assertEqual(result["main"]["status"], "cancelled")
        self.assertEqual(result["main"]["transferred"], 0)
        self.assertEqual(result["neighbor"]["status"], "completed")
        self.assertFalse((self.root / task["key"]).exists())
        self.assertEqual(list(self.root.rglob("*.part")), [])

    def test_pre_paused_batch_never_connects_or_creates_output(self) -> None:
        task = self.task(output_dir=str(self.root / "not-created"))
        self.pauses["main"] = threading.Event()
        self.pauses["main"].set()
        with patch.object(transfer, "create_client") as create:
            result = self.run_tasks([task])["main"]
        self.assertEqual(result["status"], "paused")
        create.assert_not_called()
        self.assertFalse((self.root / "not-created").exists())

    def test_pausing_active_file_releases_slot_for_waiting_neighbor(self) -> None:
        task = self.task()
        self.pauses["main"] = threading.Event()

        def pause(event: dict) -> None:
            if event["kind"] == "progress" and event["id"] == "main":
                self.pauses["main"].set()

        results = self.run_tasks([task, self.task("neighbor.bin", "neighbor")], pause, workers=1)
        self.assertEqual(results["main"]["status"], "paused")
        self.assertEqual(results["neighbor"]["status"], "completed")
        self.assertEqual((self.root / "neighbor.bin").read_bytes(), b"neighbour")
        self.assertEqual(len(list(self.root.rglob("*.part"))), 1)

    def test_resume_after_new_event_maps_uses_durable_checkpoint_not_runtime_state(self) -> None:
        task = self.task()
        self.pause_after_first_chunk(task)
        # Like a restarted process: only the persisted task fields and on-disk
        # checkpoint survive; the event dictionaries are entirely fresh.
        restored = json.loads(json.dumps(task))
        self.cancels = {}
        self.pauses = {}
        results = self.run_tasks([restored])
        self.assertEqual(results["main"]["status"], "completed")
        self.assertEqual(self.server.calls[-1]["Range"], f"bytes={transfer.CHUNK_SIZE}-")
        self.assertEqual((self.root / task["key"]).read_bytes(), self.payload)

    def test_precancelled_paused_task_cleans_checkpoint_without_connecting(self) -> None:
        task = self.task()
        self.pause_after_first_chunk(task)
        self.cancels["main"] = threading.Event()
        self.cancels["main"].set()
        with patch.object(transfer, "create_client") as create:
            result = self.run_tasks([task])["main"]
        self.assertEqual(result["status"], "cancelled")
        create.assert_not_called()
        self.assertEqual(list(self.root.rglob("*.part")), [])

    def test_collision_is_reported_and_other_tasks_continue(self) -> None:
        task = self.task()
        result = self.run_tasks([task, self.task(task_id="duplicate"), self.task("neighbor.bin", "neighbor")], workers=2)
        self.assertEqual(result["main"]["status"], "completed")
        self.assertEqual(result["duplicate"]["status"], "failed")
        self.assertEqual(result["neighbor"]["status"], "completed")

    def test_no_validator_means_partial_is_restarted_instead_of_blind_resume(self) -> None:
        task = self.task()
        self.pause_after_first_chunk(task)
        self.pauses["main"].clear()
        self.server.etags[task["key"]] = ""
        def get_without_etag(**arguments: object) -> dict:
            # Missing validator is supported for a full transfer only.
            self.assertNotIn("IfMatch", arguments)
            self.assertNotIn("Range", arguments)
            body = Body(self.payload)
            self.server.calls.append(dict(arguments))
            return {"Body": body, "ContentLength": len(self.payload), "ResponseMetadata": {"HTTPStatusCode": 200}}

        self.server.get_object = get_without_etag
        result = self.run_tasks([task])["main"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual((self.root / task["key"]).read_bytes(), self.payload)

    def test_malformed_checkpoint_is_ignored_and_never_controls_output_path(self) -> None:
        task = self.task()
        self.pause_after_first_chunk(task)
        metadata = next(self.root.rglob(".r2-*.json"))
        metadata.write_text(json.dumps({"part_path": str(self.root / "unrelated.txt")}), encoding="utf-8")
        (self.root / "unrelated.txt").write_bytes(b"keep")
        self.pauses["main"].clear()
        result = self.run_tasks([task])["main"]
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("Range", self.server.calls[-1])
        self.assertEqual((self.root / "unrelated.txt").read_bytes(), b"keep")


class RealSdkTests(unittest.TestCase):
    def test_actual_sdk_head_range_and_ifmatch_contract_writes_exact_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = boto3.client("s3", region_name="auto", endpoint_url="https://offline.r2.cloudflarestorage.com",
                                  aws_access_key_id="test", aws_secret_access_key="test")
            payload = b"x" * transfer.CHUNK_SIZE + b"remaining"
            task = {"id": "sdk", "key": "real.bin", "size": len(payload), "output_dir": directory}
            pause = threading.Event()
            with Stubber(client) as stub, patch.object(transfer, "create_client", return_value=client):
                for offset in (0, transfer.CHUNK_SIZE):
                    stub.add_response("head_object", {"ContentLength": len(payload), "ETag": ETAG}, {"Bucket": "bucket", "Key": "real.bin"})
                    arguments = {"Bucket": "bucket", "Key": "real.bin", "IfMatch": ETAG}
                    response = {"ContentLength": len(payload) - offset, "ETag": ETAG,
                                "Body": StreamingBody(io.BytesIO(payload[offset:]), len(payload) - offset),
                                "ResponseMetadata": {"HTTPStatusCode": 206 if offset else 200}}
                    if offset:
                        arguments["Range"] = f"bytes={offset}-"
                        response["ContentRange"] = f"bytes {offset}-{len(payload) - 1}/{len(payload)}"
                    stub.add_response("get_object", response, arguments)

                events = []

                def callback(event: dict) -> None:
                    events.append(event)
                    if event["kind"] == "progress":
                        pause.set()

                transfer.transfer_tasks(CONNECTION, [task], {}, 1, callback, {"sdk": pause})
                self.assertEqual(events[-1]["status"], "paused")
                pause.clear()
                events.clear()
                transfer.transfer_tasks(CONNECTION, [task], {}, 1, events.append, {"sdk": pause})
                self.assertEqual(events[-1]["status"], "completed")
                self.assertEqual((Path(directory) / "real.bin").read_bytes(), payload)
                stub.assert_no_pending_responses()


if __name__ == "__main__":
    unittest.main()
