"""Summaries and read-only duplicate verification for indexed buckets."""
from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
from threading import Event

from .catalog import _timestamp
from .catalog_index import CatalogIndex
from .desktop_engine import Connection, _close_client, _raise_if_cancelled, create_client, strong_etag


def find_duplicates(catalog: CatalogIndex, connection: Connection, prefix: str, suffix: str,
                    cancel: Event, progress) -> list[list[dict]]:
    candidates = defaultdict(list)
    for index, entry in enumerate(catalog.files_under(cancel=cancel)):
        if index % 512 == 0:
            _raise_if_cancelled(cancel)
        if entry["key"].startswith(prefix) and entry["key"].casefold().endswith(suffix):
            candidates[entry["size"]].append(entry)
    total = sum(len(entries) for entries in candidates.values() if len(entries) > 1)
    progress(0, total)
    if total == 0:
        return []

    groups = defaultdict(list)
    client = create_client(connection)
    checked = 0
    try:
        for size, entries in candidates.items():
            if len(entries) < 2:
                continue
            for entry in entries:
                _raise_if_cancelled(cancel)
                params = {"Bucket": connection.bucket, "Key": entry["key"]}
                if etag := strong_etag(entry["etag"]):
                    params["IfMatch"] = etag
                response = client.get_object(**params)
                stream = response["Body"]
                digest = sha256()
                length = 0
                try:
                    for chunk in stream.iter_chunks(256 * 1024):
                        _raise_if_cancelled(cancel)
                        length += len(chunk)
                        digest.update(chunk)
                finally:
                    stream.close()
                if length != size:
                    raise ValueError("扫描时对象发生变化，请刷新存储桶后重试。")
                groups[size, digest.digest()].append(entry)
                checked += 1
                if checked == total or checked % 32 == 0:
                    progress(checked, total)
    finally:
        _close_client(client)
    _raise_if_cancelled(cancel)
    duplicates = [entries for entries in groups.values() if len(entries) > 1]
    duplicates.sort(key=lambda entries: (-entries[0]["size"], entries[0]["key"]))
    for entries in duplicates:
        entries.sort(key=lambda entry: (str(_timestamp(entry["modified"]) or ""), entry["key"]), reverse=True)
    return duplicates
