"""Production client state; no fixtures, implicit migration, or automatic transfers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .desktop_config import _protect_secret, _unprotect_secret


_PROTECTION = "windows-dpapi-current-user-v1"
_PROFILE_TEXT_FIELDS = ("id", "name", "bucket", "account_id", "access_key_id", "endpoint_url")
_TASK_TEXT_FIELDS = (
    "id", "key", "name", "bucket", "profile_id", "error", "path", "output_dir", "prefix", "created_at",
    "connection_fingerprint", "etag",
)
_TASK_STATUSES = {
    "queued", "downloading", "cancelling", "completed", "downloaded", "failed",
    "cancelled", "skipped", "interrupted", "pausing", "paused", "stopped",
}
_ACTIVE_STATUSES = {"queued", "downloading", "cancelling"}


def state_path() -> Path:
    """Return the per-user state path, independent of an EXE's directory."""
    appdata = os.environ.get("APPDATA")
    directory = Path(appdata) if appdata else Path.home() / ".config"
    return directory / "R2BatchDownloader" / "client-state.json"


def _empty_state() -> dict[str, Any]:
    return {
        "profiles": [],
        "tasks": [],
        "preferences": {
            "output_dir": str(Path.home() / "Downloads" / "R2"),
            "workers": 4,
            "skip_existing": True,
            "update_feed_url": "",
        },
    }


def _text(value: Any, field: str, default: str = "", *, limit: int = 32767) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or "\x00" in value or len(value) > limit:
        raise ValueError(f"{field} 字段格式不正确。")
    return value


def _boolean(value: Any, field: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{field} 字段必须为开关值。")
    return value


def _number(value: Any, field: str, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} 字段必须为非负整数。")
    return value


def _workers(value: Any) -> int:
    number = _number(value, "workers", 4)
    if not 1 <= number <= 16:
        raise ValueError("并发数必须为 1 到 16 之间的整数。")
    return number


def _preferences(values: Any) -> dict[str, Any]:
    if not isinstance(values, dict):
        raise ValueError("下载偏好格式不正确。")
    defaults = _empty_state()["preferences"]
    result = {
        "output_dir": _text(values.get("output_dir"), "output_dir", defaults["output_dir"]),
        "workers": _workers(values.get("workers")),
        "skip_existing": _boolean(values.get("skip_existing"), "skip_existing", True),
    }
    update_feed = _text(values.get("update_feed_url"), "update_feed_url", limit=4096)
    if update_feed:
        result["update_feed_url"] = update_feed
    return result


def _profile_metadata(values: Any) -> dict[str, Any]:
    if not isinstance(values, dict):
        raise ValueError("连接配置格式不正确。")
    result = {field: _text(values.get(field), field) for field in _PROFILE_TEXT_FIELDS}
    if not result["id"].strip() or not result["bucket"].strip():
        raise ValueError("连接配置缺少标识或存储桶名称。")
    result["name"] = result["name"] or result["bucket"]
    provider = _text(values.get("provider"), "provider", "r2", limit=16)
    if provider not in {"r2", "b2", "s3"}:
        raise ValueError("存储服务无法识别。")
    if provider != "r2":
        result["provider"] = provider
        result["region"] = _text(values.get("region"), "region", limit=64)
    result["remember_secret"] = _boolean(values.get("remember_secret"), "remember_secret", False)
    return result


def _task(values: Any, secret_values: tuple[str, ...] = ()) -> dict[str, Any]:
    if not isinstance(values, dict):
        raise ValueError("任务记录格式不正确。")
    result = {field: _text(values.get(field), field) for field in _TASK_TEXT_FIELDS}
    if not result["id"].strip() or not result["key"]:
        raise ValueError("任务记录缺少标识或对象路径。")
    batch_id = _text(values.get("batch_id"), "batch_id", limit=64)
    if batch_id:
        result["batch_id"] = batch_id
    result["name"] = result["name"] or result["key"].rsplit("/", 1)[-1]
    status = _text(values.get("status"), "status", "interrupted", limit=64)
    if status not in _TASK_STATUSES:
        raise ValueError("任务状态无法识别。")
    result["status"] = status
    result["size"] = _number(values.get("size"), "size")
    result["transferred"] = _number(values.get("transferred"), "transferred")
    direction = _text(values.get("direction"), "direction", "download", limit=12)
    if direction == "upload":
        if not result["path"] or values.get("source_modified_ns") is None:
            raise ValueError("上传任务缺少本地文件信息。")
        result["direction"] = direction
        modified = values["source_modified_ns"]
        if isinstance(modified, bool) or not isinstance(modified, int):
            raise ValueError("上传任务的本地修改时间格式不正确。")
        result["source_modified_ns"] = modified
    elif direction == "copy":
        result["direction"] = direction
        result["source_key"] = _text(values.get("source_key"), "source_key")
        if not result["source_key"] or not result["etag"]:
            raise ValueError("复制任务缺少源对象信息。")
    elif direction != "download":
        raise ValueError("任务传输方向无法识别。")
    if values.get("archive") is not None:
        result["archive"] = _boolean(values.get("archive"), "archive", False)
        if result["archive"]:
            result["archive_stamp"] = _text(values.get("archive_stamp"), "archive_stamp", limit=200)
            if not result["archive_stamp"]:
                raise ValueError("归档任务缺少本地文件状态。")
    if result["transferred"] > result["size"]:
        raise ValueError("任务已传输字节数超出文件大小。")
    result["workers"] = _workers(values.get("workers"))
    result["overwrite"] = _boolean(values.get("overwrite"), "overwrite", False)
    result["strip_prefix"] = _boolean(values.get("strip_prefix"), "strip_prefix", False)
    # Service errors occasionally repeat request credentials. The metadata is
    # allowlisted, and this free-form field is additionally redacted.
    for secret in secret_values:
        if secret:
            result["error"] = result["error"].replace(secret, "[已隐藏]")
    result["error"] = result["error"][:8192]
    return result


def _credential_values(profiles: list[dict[str, Any]]) -> tuple[str, ...]:
    values: set[str] = set()
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        for field in ("secret_access_key", "access_key_id"):
            value = profile.get(field)
            if isinstance(value, str) and value:
                values.add(value)
                if value.strip():
                    values.add(value.strip())
    # Replace long credentials first when one happens to contain another.
    return tuple(sorted(values, key=len, reverse=True))


def load_state() -> dict[str, Any]:
    """Load real state, returning an empty first launch and actionable warnings.

    Interrupted transfers never resume implicitly. Only connection metadata
    and successfully decrypted user-bound secrets enter the runtime result.
    """
    state = _empty_state()
    try:
        with state_path().open("r", encoding="utf-8") as handle:
            stored = json.load(handle)
    except FileNotFoundError:
        return state
    except (OSError, UnicodeError, ValueError):
        state["_warning"] = "保存的客户端数据无法读取，请检查配置文件或重新添加连接；原文件未被修改。"
        return state
    if not isinstance(stored, dict) or stored.get("version", 1) != 1:
        state["_warning"] = "保存的客户端数据格式或版本无法识别；原文件未被修改。"
        return state

    warnings: list[str] = []
    try:
        state["preferences"] = _preferences(stored.get("preferences", {}))
    except ValueError:
        warnings.append("下载设置格式不正确，已使用默认设置。")

    profiles = stored.get("profiles", [])
    if not isinstance(profiles, list):
        profiles = []
        warnings.append("存储桶连接列表格式不正确，请重新添加连接。")
    profile_ids: set[str] = set()
    for values in profiles:
        try:
            profile = _profile_metadata(values)
            if profile["id"] in profile_ids:
                raise ValueError("连接标识重复。")
            profile_ids.add(profile["id"])
        except ValueError:
            warnings.append("部分连接配置无法读取，请重新添加该连接。")
            continue
        profile["secret_access_key"] = ""
        if profile["remember_secret"]:
            protected = values.get("protected_secret")
            if not isinstance(protected, str) or not protected or values.get("secret_protection") != _PROTECTION:
                profile["remember_secret"] = False
                warnings.append("部分连接没有可用的加密密钥，请重新输入 Secret Access Key。")
            else:
                try:
                    profile["secret_access_key"] = _unprotect_secret(protected)
                except (OSError, ValueError, UnicodeError):
                    profile["remember_secret"] = False
                    warnings.append("部分密钥无法解密，请重新输入；密钥只能由原 Windows 用户读取。")
        state["profiles"].append(profile)

    tasks = stored.get("tasks", [])
    if not isinstance(tasks, list):
        tasks = []
        warnings.append("传输任务记录格式不正确，未加载任务。")
    task_ids: set[str] = set()
    credentials = _credential_values(state["profiles"])
    for values in tasks:
        try:
            task = _task(values, credentials)
            if task["id"] in task_ids:
                raise ValueError("任务标识重复。")
            task_ids.add(task["id"])
        except ValueError:
            warnings.append("部分任务记录无法读取，请重新创建传输任务。")
            continue
        was_active = task["status"] in _ACTIVE_STATUSES or task["status"] == "pausing"
        if task["status"] == "pausing":
            task["status"] = "paused"
            task["error"] = ("上次运行在暂停期间结束，请点击继续上传。" if task.get("direction") == "upload"
                             else "上次运行在暂停期间结束，点击继续后核对断点并恢复。")
        elif task["status"] in _ACTIVE_STATUSES:
            task["status"] = "interrupted"
            task["error"] = "上次运行未完成，请检查连接后手动重试。"
        elif task["status"] in ("completed", "downloaded") and task["transferred"] != task["size"]:
            task["status"] = "interrupted"
            task["error"] = "任务记录中的文件大小与完成字节数不一致，请手动重试。"
        if task.get("direction") in {"upload", "copy"} and was_active:
            task["transferred"] = 0
        state["tasks"].append(task)

    if warnings:
        state["_warning"] = " ".join(dict.fromkeys(warnings))
    return state


def save_state(profiles: list[dict[str, Any]], tasks: list[dict[str, Any]], preferences: dict[str, Any]) -> None:
    """Atomically replace the real state with the caller's current collections.

    Removing a connection or clearing tasks takes effect on the next call;
    previous ciphertext and task history are never merged back into the state.
    """
    if not isinstance(profiles, list) or not isinstance(tasks, list) or not isinstance(preferences, dict):
        raise ValueError("客户端数据格式不正确，无法保存。")
    stored: dict[str, Any] = {"version": 1, "profiles": [], "tasks": [], "preferences": _preferences(preferences)}
    profile_ids: set[str] = set()
    for values in profiles:
        profile = _profile_metadata(values)
        if profile["id"] in profile_ids:
            raise ValueError("连接标识重复，无法保存。")
        profile_ids.add(profile["id"])
        if profile["remember_secret"]:
            secret = values.get("secret_access_key")
            if not isinstance(secret, str) or not secret.strip():
                raise ValueError("已选择记住密钥，但未填写 Secret Access Key。")
            try:
                profile["protected_secret"] = _protect_secret(secret)
            except (OSError, ValueError, UnicodeError) as exc:
                raise ValueError("无法加密保存密钥，请取消“记住密钥”后重试。") from exc
            profile["secret_protection"] = _PROTECTION
        stored["profiles"].append(profile)
    credentials = _credential_values(profiles)
    task_ids: set[str] = set()
    for values in tasks:
        task = _task(values, credentials)
        if task["id"] in task_ids:
            raise ValueError("任务标识重复，无法保存。")
        task_ids.add(task["id"])
        stored["tasks"].append(task)

    path = state_path()
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=".client-state-", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(stored, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except (OSError, ValueError) as exc:
        raise ValueError("无法保存客户端数据，请检查配置目录权限或磁盘空间。") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
