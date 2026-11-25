"""Pure helpers for browsing real S3/R2 object listings.

Object keys remain exact: this module does not interpret dot segments, turn
backslashes into separators, or strip whitespace. Filesystem-safe mapping is a
separate responsibility of the downloader.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
import re
from typing import Any


def _size(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _modified(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value if isinstance(value, str) else ""


def normalize_objects(raw: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Copy raw boto3 or normalized objects into a consistent display catalog.

    The result contains ``key``, ``size``, ``modified``, ``kind`` and ``etag``.
    ``kind`` is ``file`` for every stored object, including directory markers;
    :func:`immediate_children` turns slash-delimited prefixes into folder rows.
    Empty/malformed keys are ignored. If pages repeat a key, its first entry
    wins, so totals and eventual downloads are not counted twice. ETag quotes
    and timestamp offsets are preserved rather than being reformatted.
    """
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        key = entry.get("key", entry.get("Key"))
        if not isinstance(key, str) or not key or key in seen:
            continue
        seen.add(key)
        etag = entry.get("etag", entry.get("ETag", ""))
        result.append({
            "key": key,
            "size": _size(entry.get("size", entry.get("Size", 0))),
            "modified": _modified(entry.get("modified", entry.get("LastModified", ""))),
            "kind": "file",
            "etag": etag if isinstance(etag, str) else "",
        })
    return result


def _timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # S3 timestamps are timezone-aware. Treat legacy naive strings as UTC only
    # for deterministic ordering; the displayed original string is untouched.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _latest(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    left_time, right_time = _timestamp(left), _timestamp(right)
    if left_time is not None and right_time is not None:
        return right if right_time > left_time else left
    if left_time is not None:
        return left
    if right_time is not None:
        return right
    return max(left, right)


def _by_name(entry: Mapping[str, Any]) -> tuple[Any, ...]:
    name = entry["name"]
    pieces = tuple(int(piece) if index % 2 else piece for index, piece in enumerate(re.split(r"(\d+)", name.casefold())))
    return pieces, name.casefold(), name


def immediate_children(
    entries: Iterable[Mapping[str, Any]], prefix: str = "",
) -> list[dict[str, Any]]:
    """Return one directory level from a complete object catalog.

    ``prefix`` is an exact folder key, with or without its trailing slash. A
    folder row has a trailing-slash ``key``, ``name``, ``kind='folder'``, the
    recursive object ``size``, latest ``modified`` time, and a recursive file
    ``count`` (directory markers do not increase that count). Marker-only empty
    folders remain visible. Stored files keep their normalized metadata and
    receive a ``name``. Folders precede files; names use case-insensitive natural
    ordering. Valid unusual keys such as leading/doubled slashes are retained,
    with an empty path component displayed as ``/``.
    """
    if not isinstance(prefix, str):
        raise TypeError("prefix must be a string")
    directory = prefix if not prefix or prefix.endswith("/") else prefix + "/"
    folders: dict[str, dict[str, Any]] = {}
    files: list[dict[str, Any]] = []
    for entry in normalize_objects(entries):
        key = entry["key"]
        if not key.startswith(directory):
            continue
        relative = key[len(directory):]
        if not relative:
            # The current directory's own marker is not one of its children.
            continue
        name, separator, _remainder = relative.partition("/")
        if not separator:
            files.append({**entry, "name": name})
            continue
        folder_key = directory + name + "/"
        folder = folders.setdefault(folder_key, {
            "key": folder_key, "name": name or "/", "kind": "folder",
            "size": 0, "modified": "", "count": 0,
        })
        folder["size"] += entry["size"]
        folder["modified"] = _latest(folder["modified"], entry["modified"])
        if not key.endswith("/"):
            folder["count"] += 1
    return sorted(folders.values(), key=_by_name) + sorted(files, key=_by_name)
