"""Real, cancellable object transfers for the production desktop client.

Each invocation uses one immutable connection snapshot. Callbacks may execute
on transfer threads; callers should enqueue the events for their UI thread.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Any, Callable

from .cli import destination_identity, path_conflicts, safe_destination
from .desktop_engine import Cancelled, Connection, create_client


EventCallback = Callable[[dict[str, Any]], None]


def _redact(message: str, connection: Connection) -> str:
    for value in (connection.secret_access_key, connection.access_key_id):
        for secret in (value, value.strip()):
            if secret:
                message = message.replace(secret, "[已隐藏]")
    return message


def _cancelled(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise Cancelled("已取消下载。")


def _result(task_id: str, status: str, **fields: Any) -> dict[str, Any]:
    return {"id": task_id, "status": status, "transferred": 0, "path": "", **fields}


def _destination(task: dict[str, Any]) -> tuple[Path, str, Path, int]:
    key = task.get("key")
    if not isinstance(key, str) or not key:
        raise ValueError("任务缺少有效的文件路径，请重新读取文件列表。")
    size = task.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ValueError("文件大小无效，请重新读取文件列表。")
    output_dir = task.get("output_dir")
    if not isinstance(output_dir, str) or not output_dir.strip():
        raise ValueError("请选择下载保存文件夹。")
    output_root = Path(output_dir).expanduser().resolve()
    prefix = task.get("prefix", "")
    if not isinstance(prefix, str):
        raise ValueError("目录前缀无效，请重新创建下载任务。")
    relative_key = key
    if task.get("strip_prefix", False) and prefix:
        if not key.startswith(prefix):
            raise ValueError("对象不符合当前目录前缀，请重新读取文件列表。")
        relative_key = key[len(prefix):]
    if not relative_key:
        raise ValueError("移除前缀后没有可保存的文件名。")
    destination = safe_destination(output_root, relative_key)
    return output_root, relative_key, destination, size


def _commit(temp_path: Path, destination: Path, overwrite: bool) -> None:
    if overwrite:
        os.replace(temp_path, destination)
    elif os.name == "nt":
        # Windows rename refuses an existing destination, including one created
        # after our earlier existence check. Do not use os.replace in this case.
        os.rename(temp_path, destination)
    else:
        # The temporary file is in the same directory/filesystem. Linking gives
        # POSIX the same atomic no-replace guarantee as Windows rename.
        os.link(temp_path, destination)
        temp_path.unlink()


def _download_task(
    client: Any,
    connection: Connection,
    task: dict[str, Any],
    output_root: Path,
    relative_key: str,
    destination: Path,
    expected_size: int,
    cancel: threading.Event,
    on_event: EventCallback,
) -> dict[str, Any]:
    from boto3.s3.transfer import TransferConfig

    task_id = task["id"]
    temporary: Path | None = None
    pending_delta = 0
    transferred = 0
    last_progress = 0.0

    def report_progress(delta: int) -> None:
        nonlocal pending_delta, transferred, last_progress
        _cancelled(cancel)
        # s3transfer sends negative deltas when it rolls a failed stream back
        # before retrying. Preserve them instead of reporting inflated progress.
        transferred += delta
        pending_delta += delta
        now = time.monotonic()
        if pending_delta and now - last_progress >= 0.12:
            on_event({"kind": "progress", "id": task_id, "bytes": pending_delta})
            pending_delta = 0
            last_progress = now

    try:
        _cancelled(cancel)
        if destination.exists() and not task.get("overwrite", False):
            if destination.is_file() and destination.stat().st_size == expected_size:
                return _result(task_id, "skipped", transferred=expected_size, path=str(destination), reason="已存在同大小的文件")
            raise FileExistsError("本地同名路径已存在；如需替换，请在创建任务时允许覆盖。")

        on_event({"kind": "started", "id": task_id})
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Recheck containment after directory creation, including any pre-existing
        # junction/symlink. The application never follows a key outside its root.
        safe_destination(output_root, relative_key)
        _cancelled(cancel)
        with tempfile.NamedTemporaryFile(
            prefix=".r2-download-", suffix=".part", dir=destination.parent, delete=False,
        ) as handle:
            temporary = Path(handle.name)
            client.download_fileobj(
                connection.bucket.strip(), task["key"], handle,
                Callback=report_progress,
                Config=TransferConfig(use_threads=False, num_download_attempts=5),
            )
            _cancelled(cancel)
            handle.flush()
            os.fsync(handle.fileno())
        actual_size = temporary.stat().st_size
        if actual_size != expected_size:
            raise OSError(f"文件大小校验失败（预期 {expected_size} 字节，实际 {actual_size} 字节）。")
        _cancelled(cancel)
        safe_destination(output_root, relative_key)
        if not task.get("overwrite", False) and destination.exists():
            raise FileExistsError("下载期间本地出现同名文件，已保留该文件；请检查后重试。")
        _commit(temporary, destination, bool(task.get("overwrite", False)))
        temporary = None
        return _result(task_id, "completed", transferred=actual_size, path=str(destination))
    except Cancelled:
        return _result(task_id, "cancelled", path=str(destination), error="已取消下载。")
    except Exception as exc:
        if cancel.is_set():
            return _result(task_id, "cancelled", path=str(destination), error="已取消下载。")
        return _result(task_id, "failed", path=str(destination), error=_redact(str(exc), connection))
    finally:
        if pending_delta:
            on_event({"kind": "progress", "id": task_id, "bytes": pending_delta})
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def transfer_tasks(
    connection: Connection,
    tasks: list[dict[str, Any]],
    cancel_events: dict[str, threading.Event],
    workers: int,
    on_event: EventCallback,
) -> None:
    """Transfer tasks using a fixed connection, emitting one result per task.

    ``progress.bytes`` is a signed delta, including SDK retry rollbacks. A final
    result has status ``completed``, ``skipped``, ``failed`` or ``cancelled``.
    Its ``transferred`` count describes the completed local file: failed and
    cancelled transfers return zero because their temporary file is removed.
    Retrying a cancelled task starts that file again; this is not byte resume.

    Invalid batch arguments raise ``ValueError`` before starting any work. SDK
    setup failures and individual file errors are converted to task results.
    """
    if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 32:
        raise ValueError("同时下载数量必须为 1 到 32 的整数。")
    snapshots = [dict(task) for task in tasks]
    ids = [task.get("id") for task in snapshots]
    if any(not isinstance(task_id, str) or not task_id for task_id in ids) or len(set(ids)) != len(ids):
        raise ValueError("下载任务必须具有互不重复的任务 ID。")
    if not snapshots:
        return
    for task_id in ids:
        cancel_events.setdefault(task_id, threading.Event())

    def record(result: dict[str, Any]) -> None:
        on_event({"kind": "result", **result})

    remaining = []
    for task in snapshots:
        if cancel_events[task["id"]].is_set():
            record(_result(task["id"], "cancelled", error="已取消下载。"))
        else:
            remaining.append(task)
    if not remaining:
        return

    try:
        client = create_client(connection, workers=workers)
    except Exception as exc:
        error = _redact(str(exc), connection)
        for task in remaining:
            if cancel_events[task["id"]].is_set():
                record(_result(task["id"], "cancelled", error="已取消下载。"))
            else:
                record(_result(task["id"], "failed", error=error))
        return

    accepted_files: set[tuple[str, ...]] = set()
    accepted_parents: set[tuple[str, ...]] = set()
    pending: dict[Future[dict[str, Any]], str] = {}

    def collect(completed: set[Future[dict[str, Any]]]) -> None:
        for future in completed:
            task_id = pending.pop(future)
            try:
                result = future.result()
            except Exception as exc:
                status = "cancelled" if cancel_events[task_id].is_set() else "failed"
                result = _result(task_id, status, error=_redact(str(exc), connection))
            record(result)

    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="r2-file") as pool:
            for task in remaining:
                task_id = task["id"]
                cancel = cancel_events[task_id]
                if cancel.is_set():
                    record(_result(task_id, "cancelled", error="已取消下载。"))
                    continue
                if isinstance(task.get("key"), str) and task["key"].endswith("/"):
                    record(_result(task_id, "skipped", reason="目录占位对象，无需下载"))
                    continue
                try:
                    task_bucket = task.get("bucket")
                    if task_bucket and task_bucket != connection.bucket.strip():
                        raise ValueError("任务的存储桶与连接不一致，请重新创建下载任务。")
                    output_root, relative_key, destination, expected_size = _destination(task)
                    # Compare canonical absolute paths, including their output
                    # roots: separate target folders must not collide, and nested
                    # output folders must not defeat file-vs-directory detection.
                    identity = destination_identity(destination.resolve().parts)
                    if path_conflicts(identity, accepted_files, accepted_parents):
                        raise ValueError("本地路径与同批其他文件冲突（大小写或文件与目录同名），已保留先前任务。")
                    accepted_files.add(identity)
                    accepted_parents.update(identity[:index] for index in range(1, len(identity)))
                except Exception as exc:
                    record(_result(task_id, "failed", error=_redact(str(exc), connection)))
                    continue
                pending[pool.submit(
                    _download_task, client, connection, task, output_root,
                    relative_key, destination, expected_size, cancel, on_event,
                )] = task_id
                # Never submit an unbounded executor queue. Per-task cancellation
                # is checked before submission and by the active file callback.
                if len(pending) >= workers:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    collect(done)
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                collect(done)
    finally:
        try:
            client.close()
        except Exception:
            # A pool close failure cannot invalidate files already committed or
            # produce a second terminal result for a task.
            pass
