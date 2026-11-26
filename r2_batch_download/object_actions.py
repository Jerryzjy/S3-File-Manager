"""Single-object sharing and guarded same-bucket R2 copies."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from urllib.parse import quote, urlsplit

from botocore.exceptions import ClientError

from .desktop_engine import Connection, EventCallback, _close_client, _redact, create_client, strong_etag

COPY_LIMIT = 5 * 1024**3 - 5 * 1024**2
LINK_DURATIONS = (("15 分钟", 900), ("1 小时", 3600), ("24 小时", 86400), ("7 天", 604800))


def parse_public_url(value: str) -> str:
    address = value.strip()
    if not address:
        return ""
    if any(character.isspace() or ord(character) < 32 or character in "\\?#" for character in address):
        raise ValueError("请填写完整的公开 HTTPS 域名。")
    try:
        parsed = urlsplit(address)
        valid = (parsed.scheme == "https" and parsed.hostname and "%" not in parsed.netloc
                 and parsed.username is None and parsed.password is None
                 and parsed.path in {"", "/"} and parsed.port in {None, 443})
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("公开链接需要 HTTPS 域名，域名后请留空。")
    host = parsed.hostname.encode("idna").decode("ascii")
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}"


def public_object_url(address: str, key: str) -> str:
    base = parse_public_url(address)
    if not base or any(segment in {".", ".."} for segment in key.split("/")):
        raise ValueError("此对象路径无法生成公开链接，请使用限时下载链接。")
    return f"{base}/{quote(key, safe='/')}"


def supports_guarded_copy(connection: Connection) -> bool:
    try:
        endpoint = urlsplit(connection.endpoint)
        host = endpoint.hostname or ""
    except ValueError:
        return False
    return connection.provider == "r2" and endpoint.scheme == "https" and host.endswith(".r2.cloudflarestorage.com")


def generate_download_link(connection: Connection, key: str, seconds: int) -> str:
    if not 1 <= seconds <= 7 * 24 * 3600:
        raise ValueError("下载链接有效期必须在 1 秒到 7 天之间。")
    client = create_client(connection)
    try:
        return client.generate_presigned_url("get_object", Params={"Bucket": connection.bucket, "Key": key},
                                             ExpiresIn=seconds, HttpMethod="GET")
    finally:
        _close_client(client)


def transfer_copies(connection: Connection, tasks: list[dict], cancels: dict[str, Event], workers: int,
                    on_event: EventCallback, *, pause_events: dict[str, Event]) -> None:
    if not supports_guarded_copy(connection):
        raise ValueError("桶内复制需要 Cloudflare R2 的原生 S3 Endpoint。")
    client = create_client(connection, workers=workers, upload=True)

    def destination_condition(request, **kwargs) -> None:
        # R2 evaluates this beta extension atomically at destination commit.
        request.headers["cf-copy-destination-if-none-match"] = "*"

    def copy(task: dict) -> None:
        task_id = task["id"]
        cancel = cancels[task_id]
        pause = pause_events[task_id]
        error = ""
        if cancel.is_set():
            status = "cancelled"
        elif pause.is_set():
            status = "paused"
        else:
            try:
                source, target = task["source_key"], task["key"]
                etag = strong_etag(task["etag"])
                if (not source or source == target or not etag or task["bucket"] != connection.bucket
                        or len(target.encode("utf-8")) > 1024):
                    raise ValueError("源对象或目标路径无法验证。请刷新文件列表后重新发起复制。")
                if task["size"] > COPY_LIMIT:
                    raise ValueError("桶内复制的单文件上限约为 4.995 GiB。")
                on_event({"kind": "started", "id": task_id})
                client.copy_object(Bucket=connection.bucket, Key=target,
                                   CopySource={"Bucket": connection.bucket, "Key": source}, CopySourceIfMatch=etag)
                status = "completed"
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                status = "failed"
                if code in {"412", "PreconditionFailed"}:
                    error = "源对象有变动或目标已存在。请检查两个路径后重试。"
                elif code in {"403", "AccessDenied", "Forbidden"}:
                    error = "复制被拒绝。请检查目标桶的对象读取和写入权限。"
                else:
                    error = _redact(str(exc), connection)
            except Exception as exc:
                status = "failed"
                error = _redact(str(exc), connection)
        on_event({"kind": "result", "id": task_id, "status": status, "error": error,
                  "transferred": task["size"] if status == "completed" else 0})

    try:
        client.meta.events.register("before-sign.s3.CopyObject", destination_condition)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="r2-copy") as pool:
            list(pool.map(copy, tasks))
    finally:
        _close_client(client)
