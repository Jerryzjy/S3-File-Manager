"""Persistent successful archive baselines, independent of task history."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import stat
from threading import Event
from typing import Any

from .cli import comparison_path, destination_identity, path_conflicts
from .client_store import state_path
from .client_transfer import _destination
from .desktop_engine import Cancelled, strong_etag


def _database() -> Path:
    return state_path().parent / "archive-index.sqlite3"


def local_stamp(path: Path) -> str:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "missing"
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError(f"归档位置被链接或其他非普通文件占用：{path}")
    return f"{info.st_size}:{info.st_mtime_ns}:{info.st_dev}:{info.st_ino}"


def _path_identity(path: Path) -> str:
    return "/".join(destination_identity(comparison_path(path.resolve()).parts))


def _connect() -> sqlite3.Connection:
    path = _database()
    path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path, timeout=10)
    try:
        database.execute("""CREATE TABLE IF NOT EXISTS archived_objects (
            storage TEXT, local_path TEXT, object_key TEXT, etag TEXT, local_stamp TEXT,
            PRIMARY KEY (storage, local_path)
        )""")
    except Exception:
        database.close()
        raise
    return database


def archive_changes(entries: list[dict], output: str, prefix: str, strip_prefix: bool,
                    fingerprint: str, cancel: Event) -> list[dict]:
    files: set[tuple[str, ...]] = set()
    directories: set[tuple[str, ...]] = set()
    settings = {"output_dir": output, "prefix": prefix, "strip_prefix": strip_prefix}
    changes = []
    with closing(_connect()) as database:
        for entry in entries:
            if cancel.is_set():
                raise Cancelled("归档检查已取消。")
            _, _, target, _ = _destination({**settings, **entry})
            path = comparison_path(target.resolve())
            identity = destination_identity(path.parts)
            if path_conflicts(identity, files, directories):
                raise ValueError(f"多个对象映射到同一个归档位置：{entry['key']}")
            files.add(identity)
            directories.update(identity[:index] for index in range(1, len(identity)))
            stamp = local_stamp(target)
            previous = database.execute(
                "SELECT object_key, etag, local_stamp FROM archived_objects WHERE storage = ? AND local_path = ?",
                (fingerprint, "/".join(identity)),
            ).fetchone()
            etag = strong_etag(entry.get("etag"))
            if etag and stamp != "missing" and previous == (entry["key"], etag, stamp):
                continue
            changes.append({**entry, "archive_stamp": stamp,
                            "archive_change": "新增或缺失" if stamp == "missing" else "替换同名文件"})
    return changes


def record_archive(task: dict, destination: Path, etag: str) -> None:
    etag = strong_etag(etag)
    if not etag:
        return
    stamp = local_stamp(destination)
    if stamp == "missing":
        raise FileNotFoundError(destination)
    with closing(_connect()) as database, database:
        database.execute("""INSERT INTO archived_objects VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(storage, local_path) DO UPDATE SET object_key=excluded.object_key,
                etag=excluded.etag, local_stamp=excluded.local_stamp""",
            (task["connection_fingerprint"], _path_identity(destination), task["key"], etag, stamp))
