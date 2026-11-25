from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import unittest

from r2_batch_download.catalog import immediate_children, normalize_objects


class NormalizeObjectsTests(unittest.TestCase):
    def test_raw_sdk_objects_preserve_datetime_etag_and_input(self) -> None:
        changed = datetime(2026, 11, 25, 8, 40, 32, tzinfo=timezone(timedelta(hours=8)))
        raw = [{"Key": "图片/产品.png", "Size": 425, "LastModified": changed, "ETag": '"etag-value"'}]
        before = copy.deepcopy(raw)
        self.assertEqual(normalize_objects(raw), [{
            "key": "图片/产品.png", "size": 425, "modified": "2026-11-25T08:40:32+08:00",
            "kind": "file", "etag": '"etag-value"',
        }])
        self.assertEqual(raw, before)

    def test_mixed_inputs_deduplicate_exact_keys_without_losing_markers(self) -> None:
        result = normalize_objects([
            {"Key": "空目录/", "Size": 0},
            {"key": "A.txt", "size": 10, "modified": "2026-11-25T00:01:02Z", "etag": '"a"'},
            {"Key": "A.txt", "Size": 999},
            {"Key": "a.txt", "Size": 3},
            {"Key": ""}, {"Key": None}, {"unexpected": "ignored"}, None,
        ])
        self.assertEqual([entry["key"] for entry in result], ["空目录/", "A.txt", "a.txt"])
        self.assertEqual(result[1]["size"], 10)
        self.assertEqual(result[1]["etag"], '"a"')
        self.assertEqual(result[1]["modified"], "2026-11-25T00:01:02Z")
        self.assertTrue(all(entry["kind"] == "file" for entry in result))

    def test_size_normalization_does_not_break_an_entire_listing(self) -> None:
        inputs = [None, -4, "12", True, float("inf"), "invalid"]
        result = normalize_objects({"key": str(index), "size": value} for index, value in enumerate(inputs))
        self.assertEqual([entry["size"] for entry in result], [0, 0, 12, 0, 0, 0])


class ImmediateChildrenTests(unittest.TestCase):
    def test_unicode_nested_objects_aggregate_size_count_and_time_without_mutation(self) -> None:
        entries = [
            {"Key": "照片/目录占位/", "Size": 0, "LastModified": datetime(2026, 11, 22, tzinfo=timezone.utc)},
            {"Key": "照片/正面.jpg", "Size": 10, "LastModified": datetime(2026, 11, 24, tzinfo=timezone.utc), "ETag": '"x"'},
            {"key": "照片/细节/a.jpg", "size": 20, "modified": "2026-11-25T01:00:00Z"},
            {"key": "照片/细节/b.jpg", "size": 30, "modified": "2026-11-25T08:30:00+08:00"},
            {"key": "说明.txt", "size": 5, "modified": "2026-11-24T12:00:00Z"},
        ]
        before = copy.deepcopy(entries)
        root = immediate_children(entries)
        self.assertEqual([entry["key"] for entry in root], ["照片/", "说明.txt"])
        self.assertEqual((root[0]["size"], root[0]["count"]), (60, 3))
        # 01:00 UTC is later than 08:30 UTC+8, despite lexical ISO ordering.
        self.assertEqual(root[0]["modified"], "2026-11-25T01:00:00Z")
        children = immediate_children(entries, "照片")
        self.assertEqual([entry["key"] for entry in children], ["照片/目录占位/", "照片/细节/", "照片/正面.jpg"])
        self.assertEqual(children[0]["count"], 0)
        self.assertEqual(children[2]["etag"], '"x"')
        self.assertEqual(entries, before)

    def test_prefix_boundaries_exclude_similarly_named_siblings(self) -> None:
        entries = [
            {"key": "photos/a.jpg", "size": 1},
            {"key": "photos2/b.jpg", "size": 2},
            {"key": "photos-other/c.jpg", "size": 3},
            {"key": "photos/sub/d.jpg", "size": 4},
            {"key": "photos/", "size": 0},
        ]
        for prefix in ("photos", "photos/"):
            self.assertEqual([entry["key"] for entry in immediate_children(entries, prefix)], ["photos/sub/", "photos/a.jpg"])
        self.assertEqual(immediate_children(entries, "missing"), [])

    def test_marker_only_folders_and_file_directory_collisions_stay_visible(self) -> None:
        entries = [
            {"Key": "empty/", "Size": 0},
            {"Key": "docs", "Size": 7},
            {"Key": "docs/", "Size": 0},
            {"Key": "nested/child/", "Size": 0},
        ]
        root = immediate_children(entries)
        self.assertEqual([entry["key"] for entry in root], ["docs/", "empty/", "nested/", "docs"])
        self.assertTrue(all(entry["count"] == 0 for entry in root[:3]))
        self.assertEqual(immediate_children(entries, "empty/"), [])
        nested = immediate_children(entries, "nested/")
        self.assertEqual([(entry["key"], entry["kind"]) for entry in nested], [("nested/child/", "folder")])

    def test_duplicate_pagination_entries_are_counted_once(self) -> None:
        first = {"Key": "folder/a.bin", "Size": 17}
        root = immediate_children([first, first.copy(), {"key": "folder/b.bin", "size": 23}])
        self.assertEqual((root[0]["size"], root[0]["count"]), (40, 2))

    def test_folders_sort_before_files_and_names_use_natural_casefold_order(self) -> None:
        entries = [{"Key": key, "Size": 0} for key in ["file10.txt", "Folder10/", "FILE2.txt", "folder2/", "b.txt", "A.txt"]]
        self.assertEqual(
            [entry["name"] for entry in immediate_children(entries)],
            ["folder2", "Folder10", "A.txt", "b.txt", "FILE2.txt", "file10.txt"],
        )

    def test_unusual_valid_keys_keep_exact_slash_whitespace_and_backslash_semantics(self) -> None:
        entries = [
            {"key": "/leading.txt", "size": 1},
            {"key": "double//child.txt", "size": 2},
            {"key": "space / trailing .txt", "size": 3},
            {"key": "literal\\filename.txt", "size": 4},
        ]
        root = immediate_children(entries)
        self.assertEqual({entry["key"] for entry in root}, {"/", "double/", "space /", "literal\\filename.txt"})
        self.assertEqual(immediate_children(entries, "/")[0]["key"], "/leading.txt")
        doubled = immediate_children(entries, "double/")
        self.assertEqual((doubled[0]["key"], doubled[0]["name"]), ("double//", "/"))
        self.assertEqual(immediate_children(entries, "double//")[0]["key"], "double//child.txt")
        self.assertEqual(immediate_children(entries, "space /")[0]["name"], " trailing .txt")


if __name__ == "__main__":
    unittest.main()
