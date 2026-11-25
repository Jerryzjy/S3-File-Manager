"""Local desktop settings with optional Windows user-bound secret storage."""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import tempfile
from typing import Any


_TEXT_FIELDS = (
    "account_id",
    "access_key_id",
    "bucket",
    "endpoint_url",
    "prefix",
    "output_dir",
)
_BOOL_FIELDS = ("overwrite", "strip_prefix")
_PROTECTION = "windows-dpapi-current-user-v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


def settings_path() -> Path:
    """Return a per-user settings path independent of the executable location."""
    appdata = os.environ.get("APPDATA")
    directory = Path(appdata) if appdata else Path.home() / ".config"
    return directory / "R2BatchDownloader" / "settings.json"


def _safe_fields(values: dict[str, Any]) -> dict[str, Any]:
    """Copy known non-secret fields; ignore arbitrary or mistyped JSON values."""
    safe: dict[str, Any] = {}
    for name in _TEXT_FIELDS:
        value = values.get(name)
        if isinstance(value, str):
            safe[name] = value
    for name in _BOOL_FIELDS:
        value = values.get(name)
        if isinstance(value, bool):
            safe[name] = value
    workers = values.get("workers")
    if isinstance(workers, int) and not isinstance(workers, bool) and 1 <= workers <= 16:
        safe["workers"] = workers
    return safe


def load_settings() -> dict[str, Any]:
    """Load settings, returning a warning instead of preventing app startup."""
    path = settings_path()
    try:
        with path.open("r", encoding="utf-8") as handle:
            stored = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, ValueError):
        return {"_warning": "保存的配置无法读取，请重新填写；重新保存配置后可修复。"}
    if not isinstance(stored, dict):
        return {"_warning": "保存的配置格式不正确，请重新填写。"}

    values = _safe_fields(stored)
    values["remember_secret"] = stored.get("remember_secret") is True
    protected = stored.get("protected_secret")
    if not values["remember_secret"]:
        return values
    if not protected:
        return values
    if not isinstance(protected, str) or stored.get("secret_protection") != _PROTECTION:
        values["remember_secret"] = False
        values["_warning"] = "保存的密钥格式不受支持，请重新输入 Secret Access Key。"
        return values
    try:
        values["secret_access_key"] = _unprotect_secret(protected)
    except (OSError, ValueError, UnicodeError):
        values["remember_secret"] = False
        values["_warning"] = "无法解密保存的密钥，请重新输入；密钥只能由原 Windows 用户读取。"
    return values


def save_settings(values: dict[str, Any], remember_secret: bool = False) -> None:
    """Atomically save allowed fields, never persisting plaintext secret keys."""
    if not isinstance(values, dict):
        raise ValueError("配置格式不正确，无法保存。")
    stored = _safe_fields(values)
    stored["remember_secret"] = bool(remember_secret)
    secret = values.get("secret_access_key")
    if remember_secret and isinstance(secret, str) and secret:
        try:
            stored["protected_secret"] = _protect_secret(secret)
        except (OSError, ValueError, UnicodeError) as exc:
            raise ValueError("无法加密保存密钥；请取消“记住密钥”后重试。") from exc
        stored["secret_protection"] = _PROTECTION

    path = settings_path()
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=".settings-", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(stored, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except (OSError, ValueError) as exc:
        raise ValueError("无法保存配置，请检查用户配置目录是否可写、磁盘空间是否充足。") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _windows_libraries() -> tuple[Any, Any]:
    if os.name != "nt":
        raise ValueError("此系统不支持 Windows 用户密钥保护。")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _input_blob(data: bytes) -> tuple[_DataBlob, Any]:
    # Keep the backing buffer alive for the entire native call.
    backing = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return _DataBlob(len(data), ctypes.cast(backing, ctypes.POINTER(ctypes.c_ubyte))), backing


def _protect_secret(secret: str) -> str:
    crypt32, kernel32 = _windows_libraries()
    data, backing = _input_blob(secret.encode("utf-8"))
    output = _DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(data), "R2 Batch Downloader", None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return base64.b64encode(ctypes.string_at(output.pbData, output.cbData)).decode("ascii")
    finally:
        kernel32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))


def _unprotect_secret(protected: str) -> str:
    # Validation rejects accidental corruption before passing bytes to Windows.
    ciphertext = base64.b64decode(protected.encode("ascii"), validate=True)
    if not ciphertext:
        raise ValueError("加密密钥为空。")
    crypt32, kernel32 = _windows_libraries()
    data, backing = _input_blob(ciphertext)
    output = _DataBlob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(data), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))
