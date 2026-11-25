"""Plan and stream conditional uploads to Cloudflare R2."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import mimetypes
import os
from pathlib import Path
import stat
from threading import Event
from typing import BinaryIO, Callable

from botocore.exceptions import ClientError

from .desktop_engine import Cancelled, Connection, EventCallback, _close_client, _redact, create_client
from .multipart_upload import (
    MAX_UPLOAD_BYTES, MultipartSession, UploadBody, digest_span, error_code,
    is_missing_session, is_multipart, part_digest,
)


@dataclass(frozen=True)
class UploadItem:
    source: Path
    key: str
    size: int
    modified_ns: int
    remote_etag: str = ""
    original_key: str = ""


def _check_cancel(cancel: Event) -> None:
    if cancel.is_set():
        raise Cancelled("操作已取消。")


def _linked(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def plan_upload(connection: Connection, prefix: str, sources: tuple[Path, ...], cancel: Event,
                *, rename_conflicts: bool = False) -> list[UploadItem]:
    planned: dict[str, UploadItem] = {}

    def add(path: Path, relative: str) -> None:
        _check_cancel(cancel)
        info = path.lstat()
        if _linked(info) or not stat.S_ISREG(info.st_mode):
            raise ValueError(f"无法上传链接或非普通文件：{path}")
        key = prefix + relative
        if len(key.encode("utf-8")) > 1024:
            raise ValueError(f"目标对象路径超过 1024 个 UTF-8 字节：{key}")
        if info.st_size > MAX_UPLOAD_BYTES:
            raise ValueError(f"文件超过 R2 对象大小上限（约 4.995 TiB）：{path}")
        previous = planned.get(key)
        if previous is not None and previous.source != path:
            raise ValueError(f"两个本地文件会写入同一个对象，请分开上传或修改本地文件名：{key}")
        planned[key] = UploadItem(path, key, info.st_size, info.st_mtime_ns)

    for source in sources:
        _check_cancel(cancel)
        if not source.name:
            raise ValueError("请选择具体文件或文件夹。")
        info = source.lstat()
        if _linked(info):
            raise ValueError(f"请选择链接指向的实际文件或文件夹：{source}")
        if not stat.S_ISDIR(info.st_mode):
            add(source, source.name)
            continue
        pending = [source]
        while pending:
            _check_cancel(cancel)
            folder = pending.pop()
            for child in sorted(folder.iterdir()):
                _check_cancel(cancel)
                child_info = child.lstat()
                if _linked(child_info):
                    raise ValueError(f"文件夹含链接，请移除该链接或单独选择其他文件：{child}")
                if stat.S_ISDIR(child_info.st_mode):
                    pending.append(child)
                else:
                    add(child, child.relative_to(source.parent).as_posix())
    if not planned:
        raise ValueError("所选位置没有可上传的文件。")
    _check_cancel(cancel)
    client = create_client(connection, workers=8, upload=True)

    def inspect(item: UploadItem) -> UploadItem:
        _check_cancel(cancel)
        try:
            response = client.head_object(Bucket=connection.bucket, Key=item.key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"404", "NotFound", "NoSuchKey"}:
                return item
            cancel.set()
            raise
        etag = response.get("ETag")
        if not etag or etag == "*":
            cancel.set()
            raise ValueError(f"存储服务未返回可用于确认覆盖的 ETag：{item.key}")
        return replace(item, remote_etag=etag)

    try:
        items = sorted(planned.values(), key=lambda item: item.key)
        checked = []
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix="r2-inspect-upload") as pool:
            for index in range(0, len(items), 16):
                futures = [pool.submit(inspect, item) for item in items[index:index + 16]]
                results = []
                for future in futures:
                    try:
                        results.append(future.result())
                    except Cancelled:
                        continue  # Prefer the original error from an in-flight check.
                _check_cancel(cancel)
                checked.extend(results)
        if not rename_conflicts:
            return checked
        reserved = set(planned)
        renamed = []
        for item in checked:
            if not item.remote_etag:
                renamed.append(item)
                continue
            suffix = item.source.suffix
            stem = item.key.removesuffix(suffix) if suffix else item.key
            for number in range(1, 101):
                _check_cancel(cancel)
                candidate = f"{stem} ({number}){suffix}"
                if candidate in reserved:
                    continue
                if len(candidate.encode("utf-8")) > 1024:
                    raise ValueError(f"同名改名后的对象路径过长，请缩短本地文件名：{item.key}")
                alternative = inspect(replace(item, key=candidate, remote_etag="", original_key=item.key))
                if not alternative.remote_etag:
                    reserved.add(candidate)
                    renamed.append(alternative)
                    break
            else:
                raise ValueError(f"找不到可用的同名备选名称，请修改本地文件名：{item.key}")
        return renamed
    finally:
        _close_client(client)


class SourceChanged(ValueError):
    pass


def _verify_source(source: Path, stream: BinaryIO, task: dict) -> None:
    expected = (task["size"], task["source_modified_ns"])
    opened = os.fstat(stream.fileno())
    current = source.lstat()
    if (_linked(current) or not os.path.samestat(current, opened)
            or (opened.st_size, opened.st_mtime_ns) != expected):
        raise SourceChanged("本地文件已变动。请恢复原文件后继续，或移除任务并重新选择文件。")


@contextmanager
def _source_stream(task: dict):
    source = Path(task["path"])
    info = source.lstat()
    if _linked(info) or not stat.S_ISREG(info.st_mode) or task["size"] > MAX_UPLOAD_BYTES:
        raise SourceChanged("文件类型或大小已变更，请重新选择上传文件。")
    with source.open("rb") as stream:
        _verify_source(source, stream, task)
        yield source, stream


def upload_error(exc: Exception, connection: Connection) -> str:
    if isinstance(exc, ClientError):
        code = error_code(exc)
        if code in {"AccessDenied", "403", "Forbidden"}:
            return "访问被拒绝。请检查密钥是否有目标桶的对象读取与写入权限。"
        if code in {"412", "409", "PreconditionFailed", "ConditionalRequestConflict"}:
            return "目标对象在检查后发生变化或已存在。请刷新文件列表并重新检查上传目标。"
        if code == "BadDigest":
            return "上传内容校验失败。请确认本地文件没有变动，再重新选择文件。"
        if code == "NoSuchBucket":
            return "未找到目标存储桶，请检查连接配置。"
    return _redact(str(exc), connection)


def _precondition(task: dict) -> dict[str, str]:
    return {"IfMatch": task["etag"]} if task.get("etag") else {"IfNoneMatch": "*"}


def _content_type(source: Path) -> str:
    return mimetypes.guess_type(source.name)[0] or "application/octet-stream"


def _multipart(client, connection: Connection, task: dict, cancel: Event, pause: Event,
               stop: Callable[[], None], emit: Callable) -> dict:
    session = MultipartSession(task)
    confirmed = 0
    with session.locked():
        try:
            if session.record.get("phase") == "completed":
                return {"status": "completed", "transferred": task["size"]}
            stop()
            emit("started")
            if session.check_committed(client):
                session.phase("completed")
                return {"status": "completed", "transferred": task["size"]}
            with _source_stream(task) as (source, stream):
                remote = {}
                if session.record:
                    try:
                        remote = session.list_parts(client, stop)
                    except ClientError as exc:
                        if not is_missing_session(exc):
                            raise
                        if session.check_committed(client):
                            session.phase("completed")
                            return {"status": "completed", "transferred": task["size"]}
                        session.begin(client, _content_type(source))
                else:
                    session.begin(client, _content_type(source))

                valid: dict[int, str] = {}
                for number, part in sorted(remote.items()):
                    offset, length = session.span(number)
                    stream.seek(offset)
                    if (part["Size"] != length
                            or part_digest(part["ETag"]) != digest_span(stream, length, stop)):
                        raise SourceChanged("本地文件与远端断点内容不一致。请恢复原文件，或移除任务后重新上传。")
                    valid[number] = part["ETag"]
                    confirmed += length
                _verify_source(source, stream, task)
                emit("resumed", transferred=confirmed)

                for number in range(1, session.part_count + 1):
                    if number in valid:
                        continue
                    offset, length = session.span(number)
                    stream.seek(offset)
                    digest = digest_span(stream, length, stop)
                    _verify_source(source, stream, task)
                    stop()
                    stream.seek(offset)
                    body = UploadBody(stream, length, stop, lambda delta: emit("progress", bytes=delta))
                    response = client.upload_part(**session.request, PartNumber=number, Body=body, ContentLength=length,
                                                  ContentMD5=base64.b64encode(digest).decode("ascii"))
                    etag = response["ETag"]
                    if part_digest(etag) != digest:
                        raise ValueError("远端分片校验失败。请移除任务并重新上传。")
                    valid[number] = etag
                    confirmed += length

                _verify_source(source, stream, task)
                parts = [{"PartNumber": number, "ETag": valid[number]} for number in sorted(valid)]
                digest = hashlib.md5(usedforsecurity=False)
                for part in parts:
                    digest.update(part_digest(part["ETag"]))
                stop()
                session.phase("committing", commit_etag=f"{digest.hexdigest()}-{len(parts)}")
                stop()
                client.complete_multipart_upload(**session.request, MultipartUpload={"Parts": parts}, **_precondition(task))
                try:
                    session.phase("completed")
                except OSError:
                    pass  # The durable committing checkpoint can reconcile through HEAD.
            return {"status": "completed", "transferred": task["size"]}
        except Exception as exc:
            if cancel.is_set():
                try:
                    if session.abort(client):
                        return {"status": "completed", "transferred": task["size"]}
                except Exception as cleanup_error:
                    return {"status": "failed", "transferred": confirmed,
                            "error": f"远端分片清理失败。{upload_error(cleanup_error, connection)}"}
                return {"status": "cancelled", "transferred": 0, "error": "已取消，远端未完成的分片已清理。"}
            if pause.is_set() and not isinstance(exc, (SourceChanged, ClientError)):
                return {"status": "paused", "transferred": confirmed}
            error = upload_error(exc, connection)
            if isinstance(exc, ClientError) and error_code(exc) in {"412", "409", "PreconditionFailed", "ConditionalRequestConflict"}:
                try:
                    session.abort(client)
                    confirmed = 0
                except Exception as cleanup_error:
                    error += f"\n远端分片清理失败。{upload_error(cleanup_error, connection)}"
            return {"status": "failed", "transferred": confirmed, "error": error}


def transfer_uploads(connection: Connection, tasks: list[dict], cancels: dict[str, Event], workers: int,
                     on_event: EventCallback, *, pause_events: dict[str, Event]) -> None:
    client = create_client(connection, workers=workers, upload=True)

    def transfer(task: dict) -> None:
        task_id = task["id"]
        cancel = cancels[task_id]
        pause = pause_events[task_id]

        def stop() -> None:
            if cancel.is_set() or pause.is_set():
                raise Cancelled("上传已中止。")

        def emit(kind: str, **fields) -> None:
            on_event({"kind": kind, "id": task_id, **fields})

        if is_multipart(task):
            try:
                result = _multipart(client, connection, task, cancel, pause, stop, emit)
            except Exception as exc:
                result = {"status": "failed", "transferred": 0, "error": upload_error(exc, connection)}
            emit("result", **result)
            return

        status, error = "failed", ""
        submitted = False
        try:
            stop()
            emit("started")
            size = task["size"]
            with _source_stream(task) as (source, stream):
                digest = digest_span(stream, size, stop)
                _verify_source(source, stream, task)
                stream.seek(0)
                stop()
                body = UploadBody(stream, size, stop, lambda delta: emit("progress", bytes=delta))
                submitted = True
                client.put_object(Bucket=connection.bucket, Key=task["key"], Body=body, ContentLength=size,
                                  ContentMD5=base64.b64encode(digest).decode("ascii"),
                                  ContentType=_content_type(source), **_precondition(task))
            status = "completed"
        except Exception as exc:
            if isinstance(exc, Cancelled) or ((cancel.is_set() or pause.is_set()) and not isinstance(exc, ClientError)):
                status = "cancelled" if cancel.is_set() else "paused"
                if submitted:
                    error = "上传请求已中止，请刷新目标目录确认对象状态。"
            else:
                error = upload_error(exc, connection)
        emit("result", status=status, transferred=task["size"] if status == "completed" else 0, error=error)

    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="r2-upload") as pool:
            list(pool.map(transfer, tasks))
    finally:
        _close_client(client)
