"""Install a verified update over a portable Windows executable after exit."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from typing import Iterator


UPDATE_ARGUMENT = "--s3fm-apply-update"
CLEANUP_ARGUMENT = "--s3fm-update-started"
_WORKSPACE_PREFIX = ".s3fm-update-"
_WORKSPACE_NAME = re.compile(r"\.s3fm-update-[a-z0-9_]{8}")
_HELPER_NAME = "update-helper.exe"
_STAGED_NAME = "downloaded.exe"
_BACKUP_NAME = "previous.exe"
_STARTED_NAME = "startup.ok"
_FINISHED_NAME = "install.ok"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SYNCHRONIZE = 0x00100000
_QUERY_LIMITED_INFORMATION = 0x1000


class UpdateInstallError(RuntimeError):
    """An update could not be safely installed."""


class _WindowsProcesses:
    def __init__(self) -> None:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.open = kernel.OpenProcess
        self.open.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.open.restype = wintypes.HANDLE
        self.close = kernel.CloseHandle
        self.close.argtypes = (wintypes.HANDLE,)
        self.close.restype = wintypes.BOOL
        self.wait = kernel.WaitForSingleObject
        self.wait.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self.wait.restype = wintypes.DWORD
        self.times = kernel.GetProcessTimes
        self.times.argtypes = (wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4))
        self.times.restype = wintypes.BOOL
        self.image = kernel.QueryFullProcessImageNameW
        self.image.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        self.image.restype = wintypes.BOOL

    @contextmanager
    def process(self, pid: int) -> Iterator[int | None]:
        handle = self.open(_SYNCHRONIZE | _QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # Windows returns ERROR_INVALID_PARAMETER for an already exited PID.
            if ctypes.get_last_error() == 87:
                yield None
                return
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield handle
        finally:
            self.close(handle)

    def identity(self, handle: int) -> tuple[int, Path]:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not self.times(handle, *(ctypes.byref(value) for value in (created, exited, kernel, user))):
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not self.image(handle, 0, buffer, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        timestamp = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return timestamp, Path(buffer.value).resolve()


def _running_instances(executable: Path) -> list[str]:
    processes = _WindowsProcesses()
    identities = []
    for pid in (os.getpid(), os.getppid()):
        if pid <= 0:
            continue
        try:
            with processes.process(pid) as handle:
                if handle is not None:
                    timestamp, path = processes.identity(handle)
                    if path == executable:
                        identities.append(f"{pid}:{timestamp}")
        except OSError:
            if pid == os.getpid():
                raise
    if not identities:
        raise UpdateInstallError("无法识别正在运行的程序，升级已取消。")
    return identities


def _wait_for_exit(executable: Path, identities: list[str]) -> None:
    processes = _WindowsProcesses()
    deadline = time.monotonic() + 60
    for identity in identities:
        parts = identity.split(":")
        if len(parts) != 2 or any(not part.isascii() or not part.isdecimal() or len(part) > 20 for part in parts):
            raise UpdateInstallError("升级进程信息无效。")
        pid, timestamp = map(int, parts)
        if not 0 < pid <= 0xFFFFFFFF:
            raise UpdateInstallError("升级进程信息无效。")
        with processes.process(pid) as handle:
            if handle is None or processes.wait(handle, 0) == 0:
                continue
            if processes.identity(handle) != (timestamp, executable):
                continue  # The PID was reused after the original process exited.
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            if processes.wait(handle, remaining) != 0:
                raise UpdateInstallError("等待旧版本退出超时，请关闭其他程序窗口后重试。")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_executable(path: Path, sha256: str) -> None:
    if _SHA256.fullmatch(sha256) is None or path.is_symlink() or _hash_file(path) != sha256:
        raise UpdateInstallError("更新文件校验失败，请重新检查更新。")
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise UpdateInstallError("更新文件不是有效的 Windows 程序。")
        stream.seek(struct.unpack_from("<I", header, 0x3C)[0])
        pe = stream.read(26)
    if len(pe) != 26 or pe[:6] != b"PE\0\0\x64\x86" or pe[24:26] != b"\x0b\x02":
        raise UpdateInstallError("更新文件需为 64 位 Windows 程序。")


def _start(executable: Path, *args: str) -> subprocess.Popen:
    environment = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    return subprocess.Popen([str(executable), *args], cwd=executable.parent, env=environment)


def _packaged_executable() -> Path:
    if os.name != "nt" or not getattr(sys, "frozen", False):
        raise UpdateInstallError("请在打包的 Windows 客户端中安装更新。")
    return Path(sys.executable).resolve()


def launch_update(staged_path: Path, sha256: str) -> None:
    """Hand off an update to a private copy of the running executable."""
    executable = _packaged_executable()
    staged_path = Path(staged_path)
    identities = _running_instances(executable)
    workspace = Path(tempfile.mkdtemp(prefix=_WORKSPACE_PREFIX, dir=executable.parent))
    try:
        staged = workspace / _STAGED_NAME
        helper = workspace / _HELPER_NAME
        shutil.copyfile(staged_path, staged)
        _verify_executable(staged, sha256.lower())
        shutil.copyfile(executable, helper)
        _start(helper, UPDATE_ARGUMENT, executable.name, sha256.lower(), *identities)
    except Exception:
        _remove_workspace(workspace)
        raise
    try:
        staged_path.unlink()
    except OSError:
        pass


def _target_for_helper(helper: Path, filename: str) -> Path:
    workspace = helper.parent
    if (helper.name != _HELPER_NAME or not _WORKSPACE_NAME.fullmatch(workspace.name)
            or Path(filename).name != filename or ":" in filename or not filename.lower().endswith(".exe")):
        raise UpdateInstallError("升级目标位置无效。")
    target = workspace.parent / filename
    if target.is_symlink() or target.resolve() != target or not target.is_file():
        raise UpdateInstallError("升级目标不可用，请重新打开客户端并检查更新。")
    return target


def _install_and_restart(workspace: Path, target: Path, sha256: str) -> None:
    staged, helper, backup = (workspace / name for name in (_STAGED_NAME, _HELPER_NAME, _BACKUP_NAME))
    _verify_executable(staged, sha256)
    if _hash_file(helper) != _hash_file(target):
        raise UpdateInstallError("当前程序文件已更改，请重新打开客户端并检查更新。")
    shutil.copyfile(target, backup)
    os.replace(staged, target)
    try:
        restarted = _start(target, CLEANUP_ARGUMENT, workspace.name)
    except OSError:
        os.replace(backup, target)
        raise
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if (workspace / _STARTED_NAME).is_file():
            (workspace / _FINISHED_NAME).touch(exist_ok=False)
            return
        if restarted.poll() is not None:
            os.replace(backup, target)
            raise UpdateInstallError("新版未能启动，已恢复到原版本。")
        time.sleep(0.25)
    raise UpdateInstallError(f"新版仍在启动，原版本已备份到 {backup}。")


def _message(text: str) -> None:
    message_box = ctypes.WinDLL("user32", use_last_error=True).MessageBoxW
    message_box.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT)
    message_box.restype = ctypes.c_int
    message_box(None, text, "S3 File Manager 升级", 0x10)


def run_update_helper(args: list[str]) -> int:
    """Frozen launcher entry point for the temporary Windows helper."""
    try:
        helper = _packaged_executable()
        if len(args) not in (3, 4):
            raise UpdateInstallError("升级参数无效。")
        filename, sha256, *identities = args
        target = _target_for_helper(helper, filename)
        _wait_for_exit(target, identities)
    except (OSError, ValueError, UpdateInstallError) as exc:
        _message(str(exc))
        return 1
    try:
        _install_and_restart(helper.parent, target, sha256)
    except (OSError, ValueError, UpdateInstallError) as exc:
        _message(f"{exc}\n可重新打开原程序检查更新。")
        return 1
    return 0


def _remove_workspace(workspace: Path) -> None:
    # Only remove the files created by the updater. Workspace paths are passed
    # between two processes, and must never be used as recursive deletion roots.
    for name in (_HELPER_NAME, _STAGED_NAME, _BACKUP_NAME, _STARTED_NAME, _FINISHED_NAME):
        try:
            (workspace / name).unlink(missing_ok=True)
        except OSError:
            return
    try:
        workspace.rmdir()
    except OSError:
        pass


def schedule_update_cleanup(workspace_name: str) -> None:
    """Confirm the new desktop window opened and remove the temporary copies."""
    if not _WORKSPACE_NAME.fullmatch(workspace_name):
        return
    try:
        workspace = _packaged_executable().parent / workspace_name
        if workspace.resolve() != workspace or not (workspace / _BACKUP_NAME).is_file():
            return
        (workspace / _STARTED_NAME).touch(exist_ok=False)
    except (OSError, UpdateInstallError):
        return

    def clean() -> None:
        # Windows keeps the copied helper locked until its one-file bootloader exits.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if not (workspace / _FINISHED_NAME).is_file():
                time.sleep(0.25)
                continue
            try:
                (workspace / _HELPER_NAME).unlink(missing_ok=True)
            except OSError:
                time.sleep(0.25)
            else:
                _remove_workspace(workspace)
                return

    threading.Thread(target=clean, name="s3fm-update-cleanup", daemon=True).start()
