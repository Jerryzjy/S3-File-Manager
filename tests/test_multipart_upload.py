"""Exercise multipart restarts, reconciliation and cleanup without a live bucket."""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from botocore.exceptions import ClientError

from r2_batch_download import client_store, multipart_upload, upload
from r2_batch_download.desktop_engine import Connection


def service_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class OfflineMultipart:
    def __init__(self) -> None:
        self.parts = {}
        self.upload_ids = 0
        self.part_calls = []
        self.complete_calls = []
        self.list_calls = []
        self.abort_calls = []
        self.objects = {}
        self.fail_abort = False
        self.lose_completion_response = False
        self.after_part = lambda number: None

    def create_multipart_upload(self, *, Bucket, Key, ContentType, Metadata):
        self.upload_ids += 1
        identifier = str(self.upload_ids)
        self.parts[identifier] = ({}, Metadata)
        return {"UploadId": identifier}

    def upload_part(self, *, Bucket, Key, UploadId, PartNumber, Body, ContentLength, ContentMD5):
        self.part_calls.append(PartNumber)
        Body.seek(0, 2)
        self.assert_body_size(Body.tell(), ContentLength)
        Body.seek(0)
        digest = hashlib.md5(usedforsecurity=False)
        observed = 0
        for block in iter(lambda: Body.read(256 * 1024), b""):
            digest.update(block)
            observed += len(block)
        self.assert_body_size(observed, ContentLength)
        if base64.b64encode(digest.digest()).decode("ascii") != ContentMD5:
            raise AssertionError("Incorrect Content-MD5")
        etag = f'"{digest.hexdigest()}"'
        self.parts[UploadId][0][PartNumber] = {"PartNumber": PartNumber, "Size": observed, "ETag": etag}
        self.after_part(PartNumber)
        return {"ETag": etag}

    @staticmethod
    def assert_body_size(observed, expected):
        if observed != expected:
            raise AssertionError((observed, expected))

    def list_parts(self, *, Bucket, Key, UploadId, PartNumberMarker=0):
        self.list_calls.append(PartNumberMarker)
        if UploadId not in self.parts:
            raise service_error("NoSuchUpload", "ListParts")
        parts = self.parts[UploadId][0]
        remaining = [parts[number] for number in sorted(parts) if number > PartNumberMarker]
        return {"Parts": remaining[:1], "IsTruncated": len(remaining) > 1,
                "NextPartNumberMarker": remaining[0]["PartNumber"] if remaining else 0}

    def complete_multipart_upload(self, *, Bucket, Key, UploadId, MultipartUpload, **condition):
        self.complete_calls.append(condition)
        existing = self.objects.get(Key)
        expected = condition.get("IfMatch")
        if (existing and condition.get("IfNoneMatch") == "*") or (expected and (not existing or existing["ETag"] != expected)):
            raise service_error("PreconditionFailed", "CompleteMultipartUpload")
        parts, metadata = self.parts.pop(UploadId)
        digest = hashlib.md5(usedforsecurity=False)
        size = 0
        for row in MultipartUpload["Parts"]:
            stored = parts[row["PartNumber"]]
            if row["ETag"] != stored["ETag"]:
                raise AssertionError("Incorrect part tag")
            digest.update(multipart_upload.part_digest(stored["ETag"]))
            size += stored["Size"]
        self.objects[Key] = {"ContentLength": size, "Metadata": metadata,
                             "ETag": f'"{digest.hexdigest()}-{len(parts)}"'}
        if self.lose_completion_response:
            raise TimeoutError("Completion response was lost")
        return {"ETag": self.objects[Key]["ETag"]}

    def head_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise service_error("404", "HeadObject")
        return self.objects[Key]

    def abort_multipart_upload(self, *, Bucket, Key, UploadId):
        self.abort_calls.append(UploadId)
        if self.fail_abort:
            raise service_error("AccessDenied", "AbortMultipartUpload")
        if self.parts.pop(UploadId, None) is None:
            raise service_error("NoSuchUpload", "AbortMultipartUpload")

    def close(self):
        pass


class MultipartUploadTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        state_path = root / "state.json"
        self.enterContext(patch.object(multipart_upload, "state_path", return_value=state_path))
        self.enterContext(patch.object(client_store, "state_path", return_value=state_path))
        self.enterContext(patch.object(multipart_upload, "MULTIPART_THRESHOLD", 16 * multipart_upload.MIB))
        self.source = root / "recording.bin"
        with self.source.open("wb") as stream:
            stream.truncate(2 * 16 * multipart_upload.MIB + 9)
        stat = self.source.stat()
        self.task = {"direction": "upload", "id": "test-task", "profile_id": "test-profile", "bucket": "bucket",
                     "key": "photos/recording.bin", "etag": "", "status": "queued", "path": str(self.source),
                     "size": stat.st_size, "source_modified_ns": stat.st_mtime_ns}
        self.connection = Connection("test-account", "test-access", "test-secret", "bucket")
        self.client = OfflineMultipart()
        self.enterContext(patch.object(upload, "create_client", return_value=self.client))
        self.enterContext(patch.object(multipart_upload, "create_client", return_value=self.client))
        self.pause, self.cancel = Event(), Event()

    def transfer(self):
        events = []
        transfer_signals = {self.task["id"]: self.cancel}
        upload.transfer_uploads(self.connection, [self.task], transfer_signals, 1, events.append,
                                pause_events={self.task["id"]: self.pause})
        self.assertEqual(events[-1]["kind"], "result")
        return events[-1]

    def pause_after(self, part_number):
        self.client.after_part = lambda number: self.pause.set() if number == part_number else None
        result = self.transfer()
        self.assertEqual(result["status"], "paused", result)
        self.pause.clear()
        return result

    def test_reopened_state_resumes_paginated_parts_without_reuploading(self):
        result = self.pause_after(2)
        self.task.update(status=result["status"], transferred=result["transferred"])
        client_store.save_state([], [self.task], {})
        self.task = client_store.load_state()["tasks"][0]
        self.assertEqual(self.task["transferred"], 32 * multipart_upload.MIB)
        result = self.transfer()
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(self.client.list_calls, [0, 1])
        self.assertEqual(self.client.part_calls, [1, 2, 3])
        self.assertEqual(self.client.upload_ids, 1)
        self.assertEqual(self.client.complete_calls, [{"IfNoneMatch": "*"}])

    def test_local_content_changes_with_same_size_and_time_are_detected(self):
        self.pause_after(1)
        stat = self.source.stat()
        with self.source.open("r+b") as stream:
            stream.write(b"different bytes")
        os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        result = self.transfer()
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("断点内容不一致", result["error"])
        self.assertEqual(self.client.part_calls, [1])
        self.assertFalse(self.client.complete_calls)

    def test_lost_completion_response_reconciles_without_local_file_or_duplicate_commit(self):
        self.task["etag"] = '"original"'
        self.client.objects[self.task["key"]] = {"ETag": self.task["etag"]}
        self.client.lose_completion_response = True
        self.assertEqual(self.transfer()["status"], "failed")
        self.source.unlink()
        result = self.transfer()
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(self.client.complete_calls, [{"IfMatch": '"original"'}])
        self.assertEqual(self.client.upload_ids, 1)

    def test_dormant_cleanup_failure_keeps_checkpoint_and_can_retry(self):
        self.pause_after(1)
        checkpoint = multipart_upload.MultipartSession(self.task).path
        self.client.fail_abort = True
        with self.assertRaises(ClientError):
            multipart_upload.discard_upload_checkpoint(self.task, self.connection, remove=True)
        self.assertTrue(checkpoint.exists())
        self.client.fail_abort = False
        self.assertFalse(multipart_upload.discard_upload_checkpoint(self.task, self.connection, remove=True))
        self.assertFalse(checkpoint.exists())
        self.assertFalse(self.client.parts)
        self.assertTrue(self.source.exists())

    def test_concurrent_object_is_kept_and_failed_upload_parts_are_aborted(self):
        self.pause_after(1)
        original = {"ETag": '"concurrent-object"'}
        self.client.objects[self.task["key"]] = original
        result = self.transfer()
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("目标对象", result["error"])
        self.assertIs(self.client.objects[self.task["key"]], original)
        self.assertEqual(self.client.abort_calls, ["1"])
        self.assertFalse(multipart_upload.MultipartSession(self.task).path.exists())
