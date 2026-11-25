from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import threading
import time
import unittest
from unittest.mock import patch

from r2_batch_download import catalog_index
from r2_batch_download.catalog import immediate_children
from r2_batch_download.catalog_index import CatalogIndex
from r2_batch_download.desktop_engine import Cancelled


class CatalogIndexTests(unittest.TestCase):
    def test_directory_rows_match_catalog_including_markers_and_unusual_keys(self) -> None:
        entries = [
            {"Key": "图片/a2.jpg", "Size": 11, "ETag": '"a"', "LastModified": datetime(2026, 11, 25, 1, tzinfo=timezone.utc)},
            {"Key": "图片/a10.jpg", "Size": 20, "LastModified": datetime(2026, 11, 25, 8, 30, tzinfo=timezone(timedelta(hours=8)))},
            {"Key": "图片/子目录/正面.png", "Size": 7},
            {"Key": "empty/", "Size": 0},
            {"Key": "图片", "Size": 3},
            {"Key": "图片/", "Size": 0},
            {"Key": "/leading.txt", "Size": 1},
            {"Key": "double//child.txt", "Size": 2},
            {"Key": "space / trailing .txt", "Size": 3},
            {"Key": "literal\\filename.txt", "Size": 4},
            {"Key": "图片2/not-a-child.jpg", "Size": 100},
        ]
        original = copy.deepcopy(entries)
        index = CatalogIndex(entries)
        for prefix in ("", "图片", "图片/", "图片/子目录/", "empty/", "/", "double/", "double//", "space /", "missing/"):
            self.assertEqual(index.children(prefix), immediate_children(entries, prefix), prefix)
        self.assertEqual(entries, original)
        self.assertEqual(index.file_count, 9)
        self.assertEqual(index.total_size, 151)
        self.assertEqual(index.children("图片/")[-2]["etag"], '"a"')

    def test_duplicate_pages_normalize_only_once_and_children_reuse_cached_rows(self) -> None:
        entries = [{"Key": "folder/a.bin", "Size": 5}, {"Key": "folder/a.bin", "Size": 999}, {"Key": "folder/b.bin", "Size": 7}]
        with patch.object(catalog_index, "normalize_objects", wraps=catalog_index.normalize_objects) as normalize:
            index = CatalogIndex(entries)
            first = index.children("folder/")
            with patch.object(catalog_index, "normalize_objects", side_effect=AssertionError("navigation must not normalize")):
                self.assertIs(index.children("folder"), first)
                self.assertIs(index.children("folder/"), first)
                self.assertEqual(index.children(""), [{"key": "folder/", "name": "folder", "kind": "folder", "size": 12, "modified": "", "count": 2}])
            self.assertEqual(normalize.call_count, 1)
        self.assertIs(first[0], index.entries[0])
        self.assertEqual(index.file_count, 2)

    def test_search_is_bucketwide_casefolded_and_uses_bounded_lru(self) -> None:
        index = CatalogIndex([{"key": "照片/正面.JPG", "size": 1}, {"key": "docs/notes.txt", "size": 2}, {"key": "empty/", "size": 0}])
        first = index.search("jpg")
        self.assertEqual([row["key"] for row in first], ["照片/正面.JPG"])
        self.assertIs(index.search("JpG"), first)
        for query in ("notes", "照片", "absent", "docs"):
            index.search(query)
        self.assertEqual(len(index._search_cache), 4)
        self.assertNotIn("jpg", index._search_cache)
        self.assertEqual(len(index.search("")), 2)
        self.assertEqual(len(index._search_cache), 4)

    def test_files_under_uses_directory_boundaries_and_keeps_unicode_suffixes(self) -> None:
        index = CatalogIndex([{"key": key, "size": 1} for key in ("a/file.txt", "a/\U0010ffffsuffix.bin", "a/", "a2/other.txt", "a")])
        self.assertEqual({row["key"] for row in index.files_under("a")}, {"a/file.txt", "a/\U0010ffffsuffix.bin"})
        self.assertEqual(index.files_under("missing/"), [])
        self.assertEqual(len(index.files_under()), 4)
        self.assertIs(index.files_under(), index.files_under(""))

    def test_selection_collapses_nested_folders_and_preserves_input_order(self) -> None:
        entries = [{"key": key, "size": 1} for key in ("root.txt", "a/z.txt", "a/sub/b.txt", "a2/file.txt", "a/sub/a.txt", "empty/")]
        index = CatalogIndex(entries)
        with patch.object(index, "_range", wraps=index._range) as ranges:
            selected = index.selected_files({"a/", "a/sub/", "a/sub/b.txt", "root.txt", "missing.txt", "empty/"})
        self.assertEqual([row["key"] for row in selected], ["root.txt", "a/z.txt", "a/sub/b.txt", "a/sub/a.txt"])
        self.assertEqual(ranges.call_count, 2)
        self.assertNotIn("a2/file.txt", [row["key"] for row in selected])
        self.assertIs(selected[0], index.entries[0])

    def test_cancel_is_checked_during_normalization_and_index_build(self) -> None:
        cancellation = threading.Event()
        cancellation.set()
        with self.assertRaises(Cancelled):
            CatalogIndex([], cancellation)

        cancellation.clear()

        def rows():
            for index in range(5000):
                if index == 1001:
                    cancellation.set()
                yield {"Key": f"file{index}", "Size": 1}

        with self.assertRaises(Cancelled):
            CatalogIndex(rows(), cancellation)

    def test_100000_objects_have_bounded_directory_work_on_navigation(self) -> None:
        entries = [{"key": f"folder{number // 1000}/sub/file{number}.bin", "size": 2, "modified": "2026-11-25T00:00:00Z"} for number in range(100_000)]
        started = time.perf_counter()
        index = CatalogIndex(entries)
        build_seconds = time.perf_counter() - started
        self.assertEqual((index.file_count, index.total_size), (100_000, 200_000))
        self.assertEqual(len(index.children()), 100)
        first_page = index.children("folder42/sub/")[:120]
        self.assertEqual(len(first_page), 120)
        self.assertEqual(len(index.children("folder42/sub/")), 1000)
        started = time.perf_counter()
        for _ in range(1000):
            self.assertIs(index.children("folder42/sub/"), index.children("folder42/sub"))
        navigation_seconds = time.perf_counter() - started
        self.assertEqual(len(index.selected_files({"folder1/", "folder2/", "folder2/sub/"})), 2000)
        # Recorded as evidence, without a machine-speed-dependent test limit.
        print(f"\nCatalogIndex benchmark: 100000 objects built in {build_seconds:.3f}s; 2000 cached lookups in {navigation_seconds:.6f}s")


if __name__ == "__main__":
    unittest.main()
