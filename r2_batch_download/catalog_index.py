"""Build-once directory metadata for large R2 listings.

Construct an index on a background worker. Directory navigation and paging can
then reuse sorted child rows without scanning, normalizing or copying the whole
bucket. Returned rows/lists are read-only views owned by the index.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import OrderedDict
from collections.abc import Iterable, Mapping
import threading
from typing import Any

from .catalog import _by_name, _timestamp, normalize_objects
from .desktop_engine import Cancelled


class CatalogIndex:
    """An immutable display index over one complete object-list snapshot.

    ``entries`` contains normalized exact object keys, including directory
    markers. ``children`` returns a precomputed, naturally sorted direct-level
    list with folders first. Files are shared between the object list, child
    lists and search results; metadata is never copied per ancestor folder.

    Search results use a four-query LRU cache. Search and large selection work
    should run on a background worker; a directory lookup itself is O(1).
    """

    SEARCH_CACHE_SIZE = 4
    _EMPTY: list[dict[str, Any]] = []

    def __init__(
        self,
        entries: Iterable[Mapping[str, Any]],
        cancel: threading.Event | None = None,
    ) -> None:
        self._cancel = cancel
        self._check_cancelled()
        self.entries = normalize_objects(self._cancellable(entries))
        self._check_cancelled()
        self._children: dict[str, list[dict[str, Any]]] = {"": []}
        self._folders: dict[str, dict[str, Any]] = {}
        self._file_by_key: dict[str, dict[str, Any]] = {}
        self._position: dict[str, int] = {}
        self._files: list[dict[str, Any]] = []
        self._search_cache: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        self._search_lock = threading.Lock()
        # Parse each distinct timestamp once rather than once per ancestor.
        times: dict[str, Any] = {"": None}

        def latest(left: str, right: str) -> str:
            if not left:
                return right
            if not right:
                return left
            if left not in times:
                times[left] = _timestamp(left)
            if right not in times:
                times[right] = _timestamp(right)
            left_time, right_time = times[left], times[right]
            if left_time is not None and right_time is not None:
                return right if right_time > left_time else left
            if left_time is not None:
                return left
            if right_time is not None:
                return right
            return max(left, right)

        self.file_count = 0
        self.total_size = 0
        for position, entry in enumerate(self.entries):
            if position % 1000 == 0:
                self._check_cancelled()
            key = entry["key"]
            is_file = not key.endswith("/")
            self.total_size += entry["size"]
            parent = ""
            start = 0
            # Each slash defines one ancestor, preserving leading and doubled
            # slashes and never interpreting dots, whitespace or backslashes.
            while True:
                slash = key.find("/", start)
                if slash == -1:
                    break
                folder_key = key[:slash + 1]
                folder = self._folders.get(folder_key)
                if folder is None:
                    folder = {
                        "key": folder_key,
                        "name": key[start:slash] or "/",
                        "kind": "folder",
                        "size": 0,
                        "modified": "",
                        "count": 0,
                    }
                    self._folders[folder_key] = folder
                    self._children.setdefault(parent, []).append(folder)
                    self._children.setdefault(folder_key, [])
                folder["size"] += entry["size"]
                folder["modified"] = latest(folder["modified"], entry["modified"])
                if is_file:
                    folder["count"] += 1
                parent = folder_key
                start = slash + 1
            if is_file:
                entry["name"] = key[start:]
                self._file_by_key[key] = entry
                self._position[key] = len(self._files)
                self._files.append(entry)
                self.file_count += 1
                self._children.setdefault(parent, []).append(entry)

        for position, children in enumerate(self._children.values()):
            if position % 1000 == 0:
                self._check_cancelled()
            children.sort(key=lambda row: (row["kind"] != "folder", _by_name(row)))
        self._check_cancelled()
        self._keys = sorted(self._file_by_key)
        self._folded_keys = [(entry["key"].casefold(), entry) for entry in self._files]
        self._check_cancelled()

    def _check_cancelled(self, cancel: threading.Event | None = None) -> None:
        token = cancel if cancel is not None else self._cancel
        if token is not None and token.is_set():
            raise Cancelled("已停止整理文件列表。")

    def _cancellable(self, entries: Iterable[Mapping[str, Any]]) -> Iterable[Mapping[str, Any]]:
        for position, entry in enumerate(entries):
            if position % 1000 == 0:
                self._check_cancelled()
            yield entry

    @staticmethod
    def _directory(prefix: str) -> str:
        if not isinstance(prefix, str):
            raise TypeError("prefix must be a string")
        return prefix if not prefix or prefix.endswith("/") else prefix + "/"

    def children(self, prefix: str = "") -> list[dict[str, Any]]:
        """Return a cached, folder-first direct-level list. Do not mutate it."""
        return self._children.get(self._directory(prefix), self._EMPTY)

    def search(self, query: str) -> list[dict[str, Any]]:
        """Find files by a case-insensitive substring anywhere in this bucket."""
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        folded = query.casefold()
        with self._search_lock:
            if folded in self._search_cache:
                self._search_cache.move_to_end(folded)
                return self._search_cache[folded]
        result = []
        for position, (key, entry) in enumerate(self._folded_keys):
            if position % 1000 == 0:
                self._check_cancelled()
            if folded in key:
                result.append(entry)
        with self._search_lock:
            # A superseded search worker may finish after a newer query; only
            # cache insertion is serialized, never the potentially long scan.
            cached = self._search_cache.get(folded)
            if cached is not None:
                return cached
            self._search_cache[folded] = result
            if len(self._search_cache) > self.SEARCH_CACHE_SIZE:
                self._search_cache.popitem(last=False)
        return result

    def _range(self, directory: str) -> tuple[int, int]:
        if not directory:
            return 0, len(self._keys)
        # Directory prefixes always end in '/', so increment that final code
        # point for the exact exclusive lexicographic bound. This also handles
        # arbitrary Unicode object suffixes without a sentinel character.
        return (
            bisect_left(self._keys, directory),
            bisect_left(self._keys, directory[:-1] + "0"),
        )

    def files_under(self, prefix: str = "", *, cancel: threading.Event | None = None) -> list[dict[str, Any]]:
        """Return files below an exact directory, excluding marker objects."""
        self._check_cancelled(cancel)
        directory = self._directory(prefix)
        if not directory:
            return self._files
        start, stop = self._range(directory)
        entries = []
        for index in range(start, stop):
            if index % 1000 == 0:
                self._check_cancelled(cancel)
            entries.append(self._file_by_key[self._keys[index]])
        return entries

    def selected_files(self, selected: set[str], *, cancel: threading.Event | None = None) -> list[dict[str, Any]]:
        """Expand selected files/folders once without scanning per selection.

        Nested selected directories are collapsed before binary key-range
        lookups, and exact child selections are deduplicated. The output keeps
        the original listing order. Complexity depends on selection expansion,
        never the product of bucket size and number of selected folders.
        """
        self._check_cancelled(cancel)
        if not selected:
            return []
        directories = sorted(key for key in selected if key.endswith("/"))
        positions = {
            self._position[key]
            for key in selected
            if key in self._file_by_key
        }
        accepted_prefix = None
        for index, directory in enumerate(directories):
            if index % 1000 == 0:
                self._check_cancelled(cancel)
            if accepted_prefix is not None and directory.startswith(accepted_prefix):
                continue
            accepted_prefix = directory
            start, stop = self._range(directory)
            for offset in range(start, stop):
                if offset % 1000 == 0:
                    self._check_cancelled(cancel)
                positions.add(self._position[self._keys[offset]])
        return [self._files[position] for position in sorted(positions)]
