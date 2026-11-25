"""Background operations for the R2 desktop client.

This module deliberately has no Tk dependency. Callbacks can run on worker
threads; the GUI must forward them to its own event queue.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .catalog import normalize_objects
from .cli import destination_identity, path_conflicts, safe_destination


class Cancelled(Exception):
    """The user cancelled an operation."""


@dataclass(frozen=True)
class Connection:
    account_id: str
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    bucket: str
    endpoint_url: str = ""
    provider: str = "r2"
    region: str = ""

    @property
    def endpoint(self) -> str:
        return self.endpoint_url.strip().rstrip("/") or f"https://{self.account_id.strip()}.r2.cloudflarestorage.com"

    @property
    def signing_region(self) -> str:
        if self.provider == "r2":
            return "auto"
        if self.provider == "b2":
            host = urlsplit(self.endpoint).hostname or ""
            match = re.fullmatch(r"s3\.([a-z0-9-]+)\.backblazeb2\.com", host)
            return match.group(1) if match else ""
        return self.region.strip()


@dataclass(frozen=True)
class DownloadOptions:
    output_dir: str
    prefix: str = ""
    workers: int = 4
    overwrite: bool = False
    strip_prefix: bool = False


EventCallback = Callable[[dict[str, Any]], None]


def strong_etag(value: Any) -> str:
    # Multipart ETags are valid opaque validators as well.
    return value if isinstance(value, str) and re.fullmatch(r'"[^"\r\n]+"', value) else ""


def validate_connection(connection: Connection) -> None:
    """Validate user input without opening a connection or reading env vars."""
    if connection.provider not in {"r2", "b2", "s3"}:
        raise ValueError("请选择支持的存储服务。")
    if connection.provider == "r2" and not connection.account_id.strip() and not connection.endpoint_url.strip():
        raise ValueError("请填写 Cloudflare Account ID 或完整的 R2 S3 endpoint。")
    if connection.provider != "r2" and not connection.endpoint_url.strip():
        raise ValueError("请填写服务商的 HTTPS S3 Endpoint。")
    if not connection.access_key_id.strip():
        raise ValueError("请填写 Access Key ID。")
    if not connection.secret_access_key.strip():
        raise ValueError("请填写对应的 Secret Access Key。")
    bucket = connection.bucket.strip()
    if not bucket:
        raise ValueError("请填写存储桶 Bucket 名称。")
    if any(character.isspace() for character in bucket) or "/" in bucket or "\\" in bucket:
        raise ValueError("Bucket 请只填写存储桶名称，不要填写网址或路径。")
    endpoint = connection.endpoint_url.strip()
    if endpoint:
        try:
            parsed = urlsplit(endpoint)
            valid = (
                parsed.scheme == "https"
                and bool(parsed.hostname)
                and not parsed.username
                and not parsed.password
                and parsed.path in ("", "/")
                and not parsed.query
                and not parsed.fragment
            )
            # Accessing .port validates malformed/non-numeric ports.
            _ = parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Endpoint 必须为完整的 HTTPS S3 地址，且不能包含 Bucket 路径、查询参数或登录信息。")
    elif not re.fullmatch(r"[A-Za-z0-9]+", connection.account_id.strip()):
        raise ValueError("Account ID 请填写账户 ID；如果提供的是完整网址，请填入 R2 endpoint。")
    if connection.provider == "b2" and not connection.signing_region:
        raise ValueError("B2 Endpoint 示例为 https://s3.us-west-004.backblazeb2.com，请使用存储桶页面显示的地址。")
    if connection.provider == "s3" and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", connection.signing_region):
        raise ValueError("请填写服务商的 S3 区域，例如 us-east-1。")


def create_client(connection: Connection, workers: int = 4, *, upload: bool = False) -> Any:
    validate_connection(connection)
    import boto3
    from botocore.config import Config

    s3_options = {"addressing_style": "path"}
    upload_options = {}
    if upload:
        s3_options["payload_signing_enabled"] = False  # HTTPS and Content-MD5 protect the body.
        upload_options["request_checksum_calculation"] = "when_required"
    return boto3.client(
        "s3",
        endpoint_url=connection.endpoint,
        aws_access_key_id=connection.access_key_id.strip(),
        aws_secret_access_key=connection.secret_access_key.strip(),
        region_name=connection.signing_region,
        config=Config(
            signature_version="s3v4",
            s3=s3_options,
            retries={"max_attempts": 0 if upload else 3, "mode": "standard"},
            connect_timeout=10,
            read_timeout=20,
            max_pool_connections=max(4, workers),
            **upload_options,
        ),
    )


def _raise_if_cancelled(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise Cancelled("操作已取消。")


def _close_client(client: Any) -> None:
    close = getattr(client, "close", None)
    if close:
        close()


def _redact(message: str, connection: Connection) -> str:
    for value in (connection.secret_access_key, connection.access_key_id):
        if value:
            message = message.replace(value, "[已隐藏]")
        if value.strip():
            message = message.replace(value.strip(), "[已隐藏]")
    return message


def list_objects(
    connection: Connection,
    prefix: str,
    cancel: threading.Event,
    on_progress: Callable[[int], None] | None = None,
) -> list[dict[str, Any]]:
    """List all pages of objects; cancellation raises :class:`Cancelled`."""
    _raise_if_cancelled(cancel)
    client = create_client(connection)
    objects: list[dict[str, Any]] = []
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=connection.bucket.strip(), Prefix=prefix):
            _raise_if_cancelled(cancel)
            objects.extend(page.get("Contents", []))
            if on_progress:
                on_progress(len(objects))
            _raise_if_cancelled(cancel)
        _raise_if_cancelled(cancel)
        return objects
    except Cancelled:
        raise
    except Exception as exc:
        raise RuntimeError(_redact(str(exc), connection)) from None
    finally:
        _close_client(client)


def list_directory_page(
    connection: Connection, prefix: str, continuation: str, page_size: int, cancel: threading.Event,
) -> tuple[list[dict[str, Any]], str]:
    """Read one S3 page of direct children while the recursive index is built."""
    _raise_if_cancelled(cancel)
    client = create_client(connection)
    try:
        arguments = {"Bucket": connection.bucket.strip(), "Prefix": prefix, "Delimiter": "/", "MaxKeys": page_size}
        if continuation:
            arguments["ContinuationToken"] = continuation
        response = client.list_objects_v2(**arguments)
        _raise_if_cancelled(cancel)
        rows = []
        for folder in response.get("CommonPrefixes", []):
            key = folder["Prefix"]
            if key.startswith(prefix) and key != prefix:
                rows.append({"key": key, "name": key[len(prefix):-1] or "/", "kind": "folder", "modified": ""})
        for file in normalize_objects(response.get("Contents", [])):
            name = file["key"][len(prefix):]
            if file["key"].startswith(prefix) and name and "/" not in name:
                file["name"] = name
                rows.append(file)
        next_token = response.get("NextContinuationToken", "") if response.get("IsTruncated") else ""
        if response.get("IsTruncated") and (not next_token or next_token == continuation):
            raise RuntimeError("存储服务未返回有效的下一页位置。")
        return rows, next_token
    except Cancelled:
        raise
    except Exception as exc:
        raise RuntimeError(_redact(str(exc), connection)) from None
    finally:
        _close_client(client)


def _download_object(
    client: Any,
    connection: Connection,
    item: dict[str, Any],
    destination: Path,
    overwrite: bool,
    cancel: threading.Event,
    on_event: EventCallback,
) -> dict[str, Any]:
    from boto3.s3.transfer import TransferConfig

    key = item["Key"]
    temp_path: Path | None = None
    transferred = 0
    pending_bytes = 0
    last_progress = 0.0

    def progress(delta: int) -> None:
        nonlocal transferred, pending_bytes, last_progress
        _raise_if_cancelled(cancel)
        transferred += delta
        pending_bytes += delta
        now = time.monotonic()
        if now - last_progress >= 0.12:
            on_event({"kind": "progress", "key": key, "bytes": pending_bytes})
            pending_bytes = 0
            last_progress = now

    try:
        _raise_if_cancelled(cancel)
        expected_size = item.get("Size")
        if destination.exists() and not overwrite:
            if destination.is_file() and expected_size is not None and destination.stat().st_size == expected_size:
                return {"key": key, "status": "skipped", "path": str(destination), "bytes": 0, "reason": "已存在同大小的文件"}
            return {
                "key": key,
                "status": "failed",
                "path": str(destination),
                "bytes": 0,
                "error": "本地路径已存在且大小不同；如需替换，请勾选覆盖已有文件。",
            }
        on_event({"kind": "started", "key": key})
        destination.parent.mkdir(parents=True, exist_ok=True)
        _raise_if_cancelled(cancel)
        with tempfile.NamedTemporaryFile(prefix=".r2-download-", suffix=".part", dir=destination.parent, delete=False) as temp_file:
            temp_path = Path(temp_file.name)
            client.download_fileobj(
                connection.bucket.strip(),
                key,
                temp_file,
                Callback=progress,
                Config=TransferConfig(use_threads=False, num_download_attempts=5),
            )
            _raise_if_cancelled(cancel)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        if expected_size is not None and temp_path.stat().st_size != expected_size:
            raise OSError(f"文件大小校验失败（预期 {expected_size} 字节，实际 {temp_path.stat().st_size} 字节）。")
        _raise_if_cancelled(cancel)
        # A file created after the initial existence check is preserved unless
        # replacement was explicitly selected. Batch-level collisions have
        # already been rejected before workers are scheduled.
        if not overwrite and destination.exists():
            raise FileExistsError("下载期间本地出现同名文件，已保留该文件；请检查后重试。")
        os.replace(temp_path, destination)
        temp_path = None
        return {"key": key, "status": "downloaded", "path": str(destination), "bytes": destination.stat().st_size}
    except Cancelled:
        return {"key": key, "status": "cancelled", "path": str(destination), "bytes": 0, "reason": "已取消下载"}
    except Exception as exc:
        if cancel.is_set():
            return {"key": key, "status": "cancelled", "path": str(destination), "bytes": 0, "reason": "已取消下载"}
        return {"key": key, "status": "failed", "path": str(destination), "bytes": 0, "error": _redact(str(exc), connection)}
    finally:
        if pending_bytes:
            on_event({"kind": "progress", "key": key, "bytes": pending_bytes})
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def download_batch(
    connection: Connection,
    objects: list[dict[str, Any]],
    options: DownloadOptions,
    cancel: threading.Event,
    on_event: EventCallback,
) -> dict[str, Any]:
    """Download a bounded concurrent batch, reporting each object's result.

    The callback receives ``started``, ``progress`` (delta bytes), and
    ``result`` events. The returned summary counts every supplied object,
    including unscheduled objects when the user cancels.
    """
    validate_connection(connection)
    if not options.output_dir.strip():
        raise ValueError("请选择本地保存文件夹。")
    if not isinstance(options.workers, int) or isinstance(options.workers, bool) or not 1 <= options.workers <= 32:
        raise ValueError("并发下载数必须为 1 到 32 之间的整数。")

    output_root = Path(options.output_dir).expanduser().resolve()
    if not cancel.is_set():
        output_root.mkdir(parents=True, exist_ok=True)
        if not output_root.is_dir():
            raise ValueError("保存位置必须是文件夹。")
    summary: dict[str, Any] = {
        "downloaded": 0, "skipped": 0, "failed": 0, "cancelled": 0,
        "total": len(objects), "bytes": 0, "results": [],
    }

    def record(result: dict[str, Any]) -> None:
        summary[result["status"]] += 1
        summary["bytes"] += result.get("bytes", 0)
        summary["results"].append(result)
        on_event({"kind": "result", **result})

    if cancel.is_set():
        for item in objects:
            record({"key": item.get("Key", ""), "status": "cancelled", "bytes": 0, "reason": "已取消下载"})
        return summary

    client = create_client(connection, workers=options.workers)
    accepted_files: set[tuple[str, ...]] = set()
    accepted_parents: set[tuple[str, ...]] = set()
    pending: dict[Future[dict[str, Any]], str] = {}

    def collect(completed: set[Future[dict[str, Any]]]) -> None:
        for future in completed:
            key = pending.pop(future)
            try:
                result = future.result()
            except Exception as exc:
                status = "cancelled" if cancel.is_set() else "failed"
                result = {"key": key, "status": status, "bytes": 0, "error": _redact(str(exc), connection)}
            record(result)

    try:
        with ThreadPoolExecutor(max_workers=options.workers, thread_name_prefix="r2-download") as pool:
            for item in objects:
                key = item.get("Key", "")
                if cancel.is_set():
                    record({"key": key, "status": "cancelled", "bytes": 0, "reason": "已取消下载"})
                    continue
                if key.endswith("/"):
                    record({"key": key, "status": "skipped", "bytes": 0, "reason": "目录占位对象"})
                    continue
                relative_key = key
                if options.strip_prefix and options.prefix:
                    if not key.startswith(options.prefix):
                        record({"key": key, "status": "failed", "bytes": 0, "error": "对象不符合当前目录前缀，请重新加载文件列表。"})
                        continue
                    relative_key = key[len(options.prefix):]
                if not relative_key:
                    record({"key": key, "status": "failed", "bytes": 0, "error": "移除前缀后没有可保存的文件名。"})
                    continue
                try:
                    destination = safe_destination(output_root, relative_key)
                    identity = destination_identity(destination.relative_to(output_root).parts)
                    if path_conflicts(identity, accepted_files, accepted_parents):
                        raise ValueError("本地路径与其他对象冲突（可能仅大小写不同或文件与目录同名），已跳过以防覆盖。")
                    accepted_files.add(identity)
                    accepted_parents.update(identity[:index] for index in range(1, len(identity)))
                except (ValueError, OSError) as exc:
                    record({"key": key, "status": "failed", "bytes": 0, "error": _redact(str(exc), connection)})
                    continue

                future = pool.submit(_download_object, client, connection, item, destination, options.overwrite, cancel, on_event)
                pending[future] = key
                # Limit waiting work so cancellation does not leave an enormous
                # executor queue behind and memory scales with active workers.
                if len(pending) >= options.workers:
                    completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                    collect(completed)
            while pending:
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                collect(completed)
    finally:
        _close_client(client)
    return summary
