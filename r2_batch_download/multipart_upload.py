"""Cloudflare R2 multipart sessions backed by local, atomic checkpoints."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, BinaryIO, Callable

from botocore.exceptions import ClientError
from PySide6.QtCore import QLockFile

from .client_store import state_path
from .desktop_engine import Connection, _close_client, create_client
from .resumable_transfer import _save_checkpoint

MIB = 1024**2
MULTIPART_THRESHOLD = 64 * MIB
MAX_UPLOAD_BYTES = 5 * 1024**4 - 5 * 1024**3
_TASK_METADATA = "s3fm-task-id"


def is_multipart(task: dict[str, Any]) -> bool:
    return task.get("direction") == "upload" and task.get("size", 0) >= MULTIPART_THRESHOLD


class UploadBody:
    """A seekable window into a file. Botocore offsets are relative to the part."""

    def __init__(self, stream: BinaryIO, size: int, stop: Callable[[], None], progress: Callable[[int], None]) -> None:
        self.stream = stream
        self.offset = stream.tell()
        self.size = size
        self.stop = stop
        self.progress = progress
        self.reported = 0
        self.report_time = time.monotonic()

    def tell(self) -> int:
        return self.stream.tell() - self.offset

    def seek(self, position: int, whence: int = 0) -> int:
        base = {0: 0, 1: self.tell(), 2: self.size}[whence]
        target = base + position
        if not 0 <= target <= self.size:
            raise ValueError("上传请求的文件偏移超出分片范围。")
        return self.stream.seek(self.offset + target) - self.offset

    def read(self, amount: int = -1) -> bytes:
        self.stop()
        remaining = self.size - self.tell()
        data = self.stream.read(remaining if amount < 0 else min(amount, remaining))
        position = self.tell()
        now = time.monotonic()
        if position > self.reported and (position == self.size or now - self.report_time >= 0.15):
            self.progress(position - self.reported)
            self.reported = position
            self.report_time = now
        return data


def digest_span(stream: BinaryIO, length: int, stop: Callable[[], None]) -> bytes:
    digest = hashlib.md5(usedforsecurity=False)  # R2 uses MD5 for individual parts.
    while length:
        stop()
        block = stream.read(min(length, MIB))
        if not block:
            raise ValueError("本地文件长度已改变，请重新选择上传文件。")
        digest.update(block)
        length -= len(block)
    return digest.digest()


def error_code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def is_missing_session(error: ClientError) -> bool:
    return error_code(error) in {"NoSuchUpload", "404", "NotFound"}


class MultipartSession:
    def __init__(self, task: dict[str, Any]) -> None:
        self.task = task
        digest = hashlib.sha256(task["id"].encode("utf-8")).hexdigest()
        self.path = state_path().parent / "upload-checkpoints" / f"{digest}.json"
        self.destination = {"Bucket": task["bucket"], "Key": task["key"]}
        self.identity = {
            name: task.get(name, "") for name in (
                "id", "bucket", "key", "path", "size", "source_modified_ns", "etag", "connection_fingerprint",
            )
        }
        self.part_size = max(16, (task["size"] + 10000 * MIB - 1) // (10000 * MIB)) * MIB
        self.record: dict[str, Any] = {}

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(self.path.with_suffix(".lock")))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            raise OSError("无法锁定上传断点。请检查本机目录权限或关闭正在处理此任务的窗口。")
        try:
            self.load()
            yield
        finally:
            lock.unlock()

    def load(self) -> None:
        try:
            with self.path.open(encoding="utf-8") as stream:
                self.record = json.loads(stream.read(32769))
        except FileNotFoundError:
            self.record = {}
            return
        if (not isinstance(self.record, dict) or self.record.get("format") != 1
                or self.record.get("identity") != self.identity or self.record.get("part_size") != self.part_size
                or not isinstance(self.record.get("upload_id"), str) or not self.record["upload_id"]
                or self.record.get("phase") not in {"uploading", "committing", "completed"}):
            raise ValueError("上传断点无法验证。请保留任务记录并检查本地配置。")

    @property
    def request(self) -> dict[str, str]:
        return {**self.destination, "UploadId": self.record["upload_id"]}

    def begin(self, client: Any, content_type: str) -> None:
        response = client.create_multipart_upload(**self.destination, ContentType=content_type,
                                                  Metadata={_TASK_METADATA: self.task["id"]})
        self.record = {"format": 1, "identity": self.identity, "part_size": self.part_size,
                       "upload_id": response["UploadId"], "phase": "uploading"}
        try:
            _save_checkpoint(self.path, self.record)
        except OSError:
            client.abort_multipart_upload(**self.request)
            raise

    def phase(self, phase: str, **fields: Any) -> None:
        updated = {**self.record, "phase": phase, **fields}
        _save_checkpoint(self.path, updated)
        self.record = updated

    def list_parts(self, client: Any, stop: Callable[[], None]) -> dict[int, dict[str, Any]]:
        parts: dict[int, dict[str, Any]] = {}
        marker = 0
        while True:
            stop()
            arguments = {"PartNumberMarker": marker} if marker else {}
            response = client.list_parts(**self.request, **arguments)
            for part in response.get("Parts", []):
                number = part["PartNumber"]
                if number in parts or not 1 <= number <= self.part_count:
                    raise ValueError("远端分片编号与断点不一致，请移除任务并重新上传。")
                parts[number] = part
            if not response.get("IsTruncated"):
                return parts
            next_marker = response.get("NextPartNumberMarker")
            if not isinstance(next_marker, int) or not marker < next_marker <= self.part_count:
                raise ValueError("存储服务未提供有效的分片分页位置。")
            marker = next_marker

    @property
    def part_count(self) -> int:
        return (self.task["size"] + self.part_size - 1) // self.part_size

    def span(self, number: int) -> tuple[int, int]:
        offset = (number - 1) * self.part_size
        return offset, min(self.part_size, self.task["size"] - offset)

    def check_committed(self, client: Any) -> bool:
        if self.record.get("phase") != "committing":
            return self.record.get("phase") == "completed"
        try:
            head = client.head_object(**self.destination)
        except ClientError as exc:
            if error_code(exc) in {"404", "NotFound", "NoSuchKey"}:
                return False
            raise
        return (head.get("Metadata", {}).get(_TASK_METADATA) == self.task["id"]
                and head.get("ContentLength") == self.task["size"]
                and str(head.get("ETag", "")).strip('"').lower() == self.record.get("commit_etag"))

    def abort(self, client: Any) -> bool:
        """Discard only uncommitted parts. Return whether the object already committed."""
        if self.record.get("phase") == "completed":
            return True
        if self.check_committed(client):
            self.phase("completed")
            return True
        committed = False
        if self.record:
            try:
                client.abort_multipart_upload(**self.request)
            except ClientError as exc:
                if not is_missing_session(exc):
                    raise
                committed = self.check_committed(client)
        if committed:
            self.phase("completed")
        else:
            self.path.unlink(missing_ok=True)
            self.record = {}
        return committed


def part_digest(etag: str) -> bytes:
    digest = str(etag).strip('"').lower()
    if not re.fullmatch(r"[0-9a-f]{32}", digest):
        raise ValueError("存储服务返回的分片校验值无效，无法安全继续上传。")
    return bytes.fromhex(digest)


def discard_upload_checkpoint(task: dict[str, Any], connection: Connection | None, *, remove: bool) -> bool:
    session = MultipartSession(task)
    with session.locked():
        if not session.record:
            return False
        committed = session.record["phase"] == "completed"
        if not committed:
            if connection is None or connection.bucket != task["bucket"]:
                raise ValueError("请先恢复原存储桶连接及写入密钥，再清理远端未完成分片。")
            client = create_client(connection, upload=True)
            try:
                committed = session.abort(client)
            finally:
                _close_client(client)
        if remove:
            session.path.unlink(missing_ok=True)
        return committed
