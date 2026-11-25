"""Bounded R2 streaming transfers with durable, identity-checked pause/resume.

Only data and object identity go into checkpoints. Credentials are never saved
here. A paused task releases its worker; a later invocation resumes its bytes.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import re
import tempfile
import threading
import time
from typing import Any, Callable

from .archive import local_stamp, record_archive
from .cli import comparison_path, destination_identity, path_conflicts, safe_destination
from .client_transfer import _commit, _destination, _redact
from .desktop_engine import Cancelled, Connection, create_client, strong_etag


CHUNK_SIZE = 256 * 1024
MAX_ATTEMPTS = 3
EventCallback = Callable[[dict[str, Any]], None]


class _Paused(Exception):
    pass


class _ProtocolError(Exception):
    """The response cannot safely be appended to an existing file."""


class _ObjectChanged(Exception):
    pass


class _StreamFailure(Exception):
    pass


def _stop(cancel: threading.Event, pause: threading.Event) -> None:
    if cancel.is_set():
        raise Cancelled("已取消下载。")
    if pause.is_set():
        raise _Paused()


def _paths(task: dict[str, Any]) -> tuple[Path, str, Path, int, Path, Path]:
    root, relative, destination, expected_size = _destination(task)
    task_id = task.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("下载任务缺少任务 ID。")
    # Derive paths, never accept stored absolute checkpoint paths. Including the
    # task ID prevents independent attempts from sharing a partial file.
    token = hashlib.sha256(json.dumps(
        [task_id, task["key"], str(comparison_path(destination.resolve())).casefold() if os.name == "nt" else str(destination.resolve())], ensure_ascii=False,
    ).encode("utf-8")).hexdigest()[:32]
    partial = destination.parent / f".r2-{token}.part"
    checkpoint = destination.parent / f".r2-{token}.json"
    return root, relative, destination, expected_size, partial, checkpoint


def _artifact_check(root: Path, relative: str, partial: Path, checkpoint: Path) -> None:
    safe_destination(root, relative)
    for path in (partial, checkpoint):
        if path.is_symlink():
            raise ValueError("断点文件不能是符号链接，请检查保存位置。")
        if not comparison_path(path.resolve()).is_relative_to(comparison_path(root.resolve())):
            raise ValueError("断点文件不在下载目录内。")
        if path.exists() and not path.is_file():
            raise ValueError("断点文件位置被其他目录占用。")


def _discard_paths(partial: Path, checkpoint: Path) -> None:
    # unlink removes a malicious link itself rather than its target.
    partial.unlink(missing_ok=True)
    checkpoint.unlink(missing_ok=True)


def discard_partial(task: dict[str, Any]) -> None:
    """Remove only this task's derived partial/checkpoint, never its final file.

    Call only after the task's previous worker has released ownership.
    """
    root, relative, _destination_path, _size, partial, checkpoint = _paths(task)
    safe_destination(root, relative)
    _discard_paths(partial, checkpoint)


def _partial_size(partial: Path, expected_size: int) -> int:
    if partial.is_symlink() or not partial.is_file():
        return 0
    return min(expected_size, max(0, partial.stat().st_size))


def _identity(connection: Connection, task: dict[str, Any], head: dict[str, Any], expected_size: int) -> dict[str, Any]:
    version = head.get("VersionId", "")
    identity = {
        "format": 1, "endpoint": connection.endpoint.lower(), "bucket": connection.bucket.strip(),
        "key": task["key"], "size": expected_size, "etag": strong_etag(head.get("ETag")),
        "version_id": version if isinstance(version, str) and version != "null" else "",
    }
    if connection.provider != "r2":
        identity.update(provider=connection.provider, region=connection.signing_region)
    return identity


def _save_checkpoint(path: Path, identity: dict[str, Any]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".r2-meta-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(identity, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _resume_offset(partial: Path, checkpoint: Path, identity: dict[str, Any]) -> int:
    if identity["etag"] and partial.is_file() and checkpoint.is_file():
        try:
            # Checkpoints are tiny. Avoid reading a user-substituted giant file.
            if checkpoint.stat().st_size > 32768:
                raise ValueError("invalid checkpoint")
            stored = json.loads(checkpoint.read_text(encoding="utf-8"))
            size = partial.stat().st_size
            if stored == identity and 0 <= size <= identity["size"]:
                return size
        except (OSError, ValueError, UnicodeError):
            pass
    _discard_paths(partial, checkpoint)
    return 0


def _retryable(exc: Exception) -> bool:
    from botocore.exceptions import (
        ClientError, ConnectionClosedError, ConnectTimeoutError,
        EndpointConnectionError, HTTPClientError, IncompleteReadError,
        ReadTimeoutError, ResponseStreamingError,
    )
    if isinstance(exc, (_StreamFailure, ConnectionClosedError, ConnectTimeoutError,
                        EndpointConnectionError, HTTPClientError, IncompleteReadError,
                        ReadTimeoutError, ResponseStreamingError)):
        return True
    if isinstance(exc, ClientError):
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        code = exc.response.get("Error", {}).get("Code", "")
        return status in (412, 429) or status >= 500 or code in {
            "PreconditionFailed", "RequestTimeout", "SlowDown", "InternalError", "ServiceUnavailable",
        }
    return False


def _response_length(response: dict[str, Any], offset: int, total: int, etag: str) -> None:
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    remaining = total - offset
    if offset:
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", str(response.get("ContentRange", "")))
        if status != 206 or not match or tuple(map(int, match.groups())) != (offset, total - 1, total):
            raise _ProtocolError("服务器未返回正确的断点范围，已保留原分片；请重试，避免重复拼接。")
    elif status not in (200, None):
        raise _ProtocolError("服务器返回了无法识别的文件响应。")
    if response.get("ContentLength") != remaining:
        raise _ProtocolError("服务器返回的文件长度与请求范围不一致，已停止写入。")
    response_etag = response.get("ETag")
    if etag and response_etag is not None and response_etag != etag:
        raise _ObjectChanged("远端文件已改变，请重新读取文件列表后重试。")


def _download(
    client: Any, connection: Connection, task: dict[str, Any],
    cancel: threading.Event, pause: threading.Event, on_event: EventCallback,
) -> dict[str, Any]:
    root, relative, destination, expected_size, partial, checkpoint = _paths(task)
    task_id = task["id"]
    pending_delta = 0
    last_report = 0.0
    started = False

    def progress(delta: int, force: bool = False) -> None:
        nonlocal pending_delta, last_report
        pending_delta += delta
        now = time.monotonic()
        if pending_delta and (force or now - last_report >= 0.12):
            on_event({"kind": "progress", "id": task_id, "bytes": pending_delta})
            pending_delta = 0
            last_report = now

    def result(status: str, transferred: int = 0, **fields: Any) -> dict[str, Any]:
        return {"id": task_id, "status": status, "transferred": transferred,
                "path": str(destination), **fields}

    try:
        _stop(cancel, pause)
        _artifact_check(root, relative, partial, checkpoint)
        archiving = task.get("archive", False)
        if archiving and local_stamp(destination) != task["archive_stamp"]:
            raise FileExistsError("本地文件在归档检查后发生变化，请重新预览归档。")
        if destination.exists() and not task.get("overwrite", False):
            if destination.is_file() and destination.stat().st_size == expected_size:
                _discard_paths(partial, checkpoint)
                return result("skipped", expected_size, reason="已存在同大小的文件")
            raise FileExistsError("本地同名路径已存在；如需替换，请在创建任务时允许覆盖。")
        on_event({"kind": "started", "id": task_id})
        started = True
        for attempt in range(MAX_ATTEMPTS):
            body = None
            try:
                _stop(cancel, pause)
                head = client.head_object(Bucket=connection.bucket.strip(), Key=task["key"])
                _stop(cancel, pause)
                if head.get("ContentLength") != expected_size:
                    _discard_paths(partial, checkpoint)
                    raise _ObjectChanged("远端文件大小已改变，请重新读取文件列表后创建下载任务。")
                identity = _identity(connection, task, head, expected_size)
                previous_etag = strong_etag(task.get("etag"))
                if archiving and previous_etag and previous_etag != identity["etag"]:
                    raise _ObjectChanged("远端对象在归档检查后发生变化，请刷新后重新预览归档。")
                destination.parent.mkdir(parents=True, exist_ok=True)
                _artifact_check(root, relative, partial, checkpoint)
                if archiving and local_stamp(destination) != task["archive_stamp"]:
                    raise FileExistsError("本地文件在归档期间发生变化，已保留该文件。请重新预览归档。")
                progress(0, force=True)
                offset = _resume_offset(partial, checkpoint, identity)
                on_event({"kind": "resumed", "id": task_id, "transferred": offset})
                _save_checkpoint(checkpoint, identity)
                _stop(cancel, pause)
                # xb avoids truncating a substituted file; a validated partial
                # is opened r+b and its current offset is always its actual size.
                mode = "r+b" if partial.exists() else "xb"
                with partial.open(mode) as handle:
                    handle.seek(offset)
                    try:
                        if offset < expected_size:
                            arguments: dict[str, Any] = {"Bucket": connection.bucket.strip(), "Key": task["key"]}
                            if offset:
                                arguments["Range"] = f"bytes={offset}-"
                            if identity["etag"]:
                                arguments["IfMatch"] = identity["etag"]
                            if identity["version_id"]:
                                arguments["VersionId"] = identity["version_id"]
                            response = client.get_object(**arguments)
                            body = response["Body"]
                            _response_length(response, offset, expected_size, identity["etag"])
                            transferred = offset
                            while transferred < expected_size:
                                _stop(cancel, pause)
                                try:
                                    chunk = body.read(min(CHUNK_SIZE, expected_size - transferred))
                                except Exception as exc:
                                    raise _StreamFailure(str(exc)) from exc
                                if not chunk:
                                    raise _StreamFailure("网络传输提前结束，将从已下载位置重试。")
                                if len(chunk) > expected_size - transferred:
                                    raise _ProtocolError("服务器返回的文件数据超出预期长度，已停止写入。")
                                handle.write(chunk)
                                transferred += len(chunk)
                                progress(len(chunk))
                            _stop(cancel, pause)
                        handle.flush()
                        os.fsync(handle.fileno())
                    finally:
                        # Pause, network failure and cancellation all leave a
                        # deterministic durable boundary before the handle closes.
                        handle.flush()
                        os.fsync(handle.fileno())
                _stop(cancel, pause)
                if partial.stat().st_size != expected_size:
                    raise _ProtocolError("分片大小校验失败，文件未提交。")
                _artifact_check(root, relative, partial, checkpoint)
                if archiving and local_stamp(destination) != task["archive_stamp"]:
                    raise FileExistsError("本地文件在归档期间发生变化，已保留该文件。请重新预览归档。")
                if not task.get("overwrite", False) and destination.exists():
                    raise FileExistsError("下载期间本地出现同名文件，已保留该文件。")
                _commit(partial, destination, bool(task.get("overwrite", False)))
                checkpoint.unlink(missing_ok=True)
                progress(0, force=True)
                if archiving:
                    try:
                        record_archive(task, destination, identity["etag"])
                    except (OSError, ValueError, sqlite3.Error) as exc:
                        return result("completed", expected_size, error=f"已保存文件。归档记录更新失败：{exc}")
                return result("completed", expected_size)
            except (Cancelled, _Paused):
                raise
            except _ObjectChanged:
                _discard_paths(partial, checkpoint)
                raise
            except Exception as exc:
                progress(0, force=True)
                if attempt + 1 == MAX_ATTEMPTS or not _retryable(exc):
                    raise
            finally:
                if body is not None:
                    try:
                        body.close()
                    except Exception:
                        pass
    except Cancelled:
        _discard_paths(partial, checkpoint)
        return result("cancelled", error="已取消下载。")
    except _Paused:
        return result("paused", _partial_size(partial, expected_size), reason="已暂停，继续时从断点下载。")
    except Exception as exc:
        if cancel.is_set():
            try:
                _discard_paths(partial, checkpoint)
            except OSError as cleanup_error:
                return result("failed", error=_redact(f"无法清理未完成文件：{cleanup_error}", connection))
            return result("cancelled", error="已取消下载。")
        if pause.is_set() and started:
            return result("paused", _partial_size(partial, expected_size), reason="已暂停，继续时从断点下载。")
        return result("failed", _partial_size(partial, expected_size), error=_redact(str(exc), connection))
    finally:
        progress(0, force=True)
    return result("failed", error="下载任务未完成，请重试。")


def transfer_tasks(
    connection: Connection, tasks: list[dict[str, Any]],
    cancel_events: dict[str, threading.Event], workers: int, on_event: EventCallback,
    pause_events: dict[str, threading.Event] | None = None,
) -> None:
    """Emit started/progress/resumed/result events for a bounded concurrent batch.

    ``resumed.transferred`` is an absolute byte offset. ``progress.bytes`` is
    a signed delta. Paused/failed results retain validated partial bytes; cancel
    removes them. A continuation must use the same task ID and destination.
    """
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 32:
        raise ValueError("同时下载数量必须为 1 到 32 的整数。")
    snapshots = [dict(task) for task in tasks]
    task_ids = [task.get("id") for task in snapshots]
    if any(not isinstance(task_id, str) or not task_id for task_id in task_ids) or len(set(task_ids)) != len(task_ids):
        raise ValueError("下载任务必须具有互不重复的任务 ID。")
    pauses = pause_events if pause_events is not None else {}
    cancels = {task_id: cancel_events.setdefault(task_id, threading.Event()) for task_id in task_ids}
    pause_map = {task_id: pauses.setdefault(task_id, threading.Event()) for task_id in task_ids}

    def record(result: dict[str, Any]) -> None:
        on_event({"kind": "result", **result})

    remaining: list[dict[str, Any]] = []
    for task in snapshots:
        task_id = task["id"]
        if cancels[task_id].is_set():
            try:
                discard_partial(task)
                record({"id": task_id, "status": "cancelled", "transferred": 0, "error": "已取消下载。"})
            except Exception as exc:
                record({"id": task_id, "status": "failed", "transferred": 0, "error": _redact(str(exc), connection)})
        elif pause_map[task_id].is_set():
            try:
                _root, _relative, _destination_path, size, partial, _checkpoint = _paths(task)
                transferred = _partial_size(partial, size)
            except Exception:
                transferred = 0
            record({"id": task_id, "status": "paused", "transferred": transferred, "reason": "已暂停。"})
        else:
            remaining.append(task)
    if not remaining:
        return
    try:
        client = create_client(connection, workers=workers)
    except Exception as exc:
        for task in remaining:
            record({"id": task["id"], "status": "failed", "transferred": task.get("transferred", 0), "error": _redact(str(exc), connection)})
        return
    accepted_files: set[tuple[str, ...]] = set()
    accepted_parents: set[tuple[str, ...]] = set()
    pending: dict[Future[dict[str, Any]], str] = {}

    def collect(completed: set[Future[dict[str, Any]]]) -> None:
        for future in completed:
            task_id = pending.pop(future)
            try:
                record(future.result())
            except Exception as exc:
                record({"id": task_id, "status": "failed", "transferred": 0, "error": _redact(str(exc), connection)})

    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="r2-stream") as pool:
            for task in remaining:
                task_id = task["id"]
                try:
                    if task.get("bucket") and task["bucket"] != connection.bucket.strip():
                        raise ValueError("任务的存储桶与连接不一致，请重新创建任务。")
                    if isinstance(task.get("key"), str) and task["key"].endswith("/"):
                        record({"id": task_id, "status": "skipped", "transferred": 0, "reason": "目录占位对象。"})
                        continue
                    output_root, _relative, _destination_path, _size = _destination(task)
                    # Stabilize the root before workers resolve paths. Windows
                    # can canonicalize an absent directory and that same newly
                    # created directory differently (junctions / redirected user
                    # folders); concurrent first-file mkdir otherwise makes a
                    # containment check spuriously fail between its two reads.
                    output_root.mkdir(parents=True, exist_ok=True)
                    _root, _relative, destination, _size, _part, _meta = _paths(task)
                    identity = destination_identity(comparison_path(destination.resolve()).parts)
                    if path_conflicts(identity, accepted_files, accepted_parents):
                        raise ValueError("本地路径与同批其他文件冲突，已保留先前任务。")
                    accepted_files.add(identity)
                    accepted_parents.update(identity[:index] for index in range(1, len(identity)))
                    pending[pool.submit(_download, client, connection, task, cancels[task_id], pause_map[task_id], on_event)] = task_id
                except Exception as exc:
                    record({"id": task_id, "status": "failed", "transferred": 0, "error": _redact(str(exc), connection)})
                if len(pending) >= workers:
                    completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                    collect(completed)
            while pending:
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                collect(completed)
    finally:
        try:
            client.close()
        except Exception:
            pass
