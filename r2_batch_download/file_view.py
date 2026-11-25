"""Search, filter and order object metadata before splitting it into pages."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import PurePosixPath
from threading import Event
from typing import Any

from .catalog import _by_name, _timestamp
from .catalog_index import CatalogIndex
from .desktop_engine import Cancelled


@dataclass(frozen=True)
class FileFilters:
    extensions: tuple[str, ...] = ()
    min_size: int | None = None
    max_size: int | None = None
    since: date | None = None
    until: date | None = None

    def active(self) -> bool:
        return bool(self.extensions) or any(
            value is not None for value in (self.min_size, self.max_size, self.since, self.until)
        )

    def matches(self, entry: dict[str, Any]) -> bool:
        size = entry["size"]
        if self.min_size is not None and size < self.min_size:
            return False
        if self.max_size is not None and size > self.max_size:
            return False
        if self.extensions and not entry["name"].casefold().endswith(self.extensions):
            return False
        if self.since is None and self.until is None:
            return True
        timestamp = _timestamp(entry["modified"])
        if timestamp is None:
            return False
        modified = timestamp.date()
        return (self.since is None or modified >= self.since) and (self.until is None or modified <= self.until)


@dataclass(frozen=True)
class FileView:
    prefix: str
    query: str
    search_bucket: bool
    filters: FileFilters
    sort_column: int
    descending: bool

    def is_search(self) -> bool:
        return bool(self.query) or self.filters.active()

    def rows(self, catalog: CatalogIndex, cancel: Event | None = None) -> list[dict[str, Any]]:
        def check_cancelled() -> None:
            if cancel is not None and cancel.is_set():
                raise Cancelled("已取消整理文件列表。")

        check_cancelled()
        if self.query:
            rows = catalog.search(self.query, cancel=cancel)
        elif self.filters.active():
            rows = catalog.files_under("" if self.search_bucket else self.prefix, cancel=cancel)
        else:
            rows = catalog.children(self.prefix)
        scoped_query = self.query and not self.search_bucket and self.prefix
        if self.filters.active() or scoped_query:
            filtered = []
            for index, entry in enumerate(rows):
                if index % 1024 == 0:
                    check_cancelled()
                if scoped_query and not entry["key"].startswith(self.prefix):
                    continue
                if self.filters.matches(entry):
                    filtered.append(entry)
            rows = filtered
        if self.sort_column < 0:
            return rows

        def sort_key(entry: dict[str, Any]):
            check_cancelled()
            if self.sort_column == 1:
                return entry["size"]
            if self.sort_column == 2:
                return PurePosixPath(entry["name"]).suffix.casefold()
            if self.sort_column == 3:
                timestamp = _timestamp(entry["modified"])
                return timestamp.timestamp() if timestamp is not None else float("-inf")
            return _by_name(entry)

        folders = [entry for entry in rows if entry["kind"] == "folder"]
        files = [entry for entry in rows if entry["kind"] != "folder"]
        return sorted(folders, key=sort_key, reverse=self.descending) + sorted(files, key=sort_key, reverse=self.descending)
