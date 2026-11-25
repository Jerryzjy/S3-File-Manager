from __future__ import annotations

import copy
import unittest

from r2_batch_download.preview_data import BUCKETS, FILES, TASKS, immediate_children


class PreviewDataTests(unittest.TestCase):
    def test_brand_root_has_three_folders_and_three_files(self) -> None:
        entries = FILES["brand-assets"]
        children = immediate_children(entries)
        self.assertEqual(len(entries), 24)
        self.assertEqual([entry["name"] for entry in children[:3]], ["产品图片", "品牌视觉", "视频素材"])
        self.assertTrue(all(entry["kind"] == "folder" for entry in children[:3]))
        self.assertEqual(sum(entry["size"] for entry in children), sum(entry["size"] for entry in entries))
        for bucket in BUCKETS:
            self.assertEqual(bucket["file_count"], len(FILES[bucket["id"]]))

    def test_nesting_and_prefix_boundary_keep_only_immediate_children(self) -> None:
        entries = [
            {"key": "photos/a.jpg", "size": 10, "modified": "2026-11-24T10:00:00+08:00"},
            {"key": "photos/detail/b.jpg", "size": 20, "modified": "2026-11-25T10:00:00+08:00"},
            {"key": "photos/detail/c.jpg", "size": 30, "modified": "2026-11-25T11:00:00+08:00"},
            {"key": "photos-other/d.jpg", "size": 40, "modified": "2026-11-25T12:00:00+08:00"},
        ]
        result = immediate_children(entries, "photos")
        self.assertEqual([entry["key"] for entry in result], ["photos/detail/", "photos/a.jpg"])
        self.assertEqual(result[0]["size"], 50)
        self.assertEqual(result[0]["modified"], "2026-11-25T11:00:00+08:00")
        self.assertEqual(immediate_children(entries, "missing/"), [])

    def test_grouping_does_not_mutate_source_and_avoids_duplicate_rows(self) -> None:
        entries = [
            {"key": "assets/", "size": 0, "modified": ""},
            {"key": "assets/a.png", "size": 10, "modified": "2026-11-25T10:00:00+08:00"},
            {"key": "assets/a.png", "size": 10, "modified": "2026-11-25T10:00:00+08:00"},
        ]
        before = copy.deepcopy(entries)
        root = immediate_children(entries)
        self.assertEqual(len(root), 1)
        self.assertEqual(root[0]["size"], 10)
        self.assertEqual(entries, before)
        self.assertEqual(len(immediate_children(entries, "assets/")), 1)

    def test_task_fixtures_refer_to_files_and_have_consistent_progress(self) -> None:
        self.assertEqual(len({task["id"] for task in TASKS}), len(TASKS))
        for task in TASKS:
            source = next(entry for entry in FILES[task["bucket"]] if entry["key"] == task["key"])
            self.assertEqual(source["size"], task["size"])
            self.assertLessEqual(task["transferred"], task["size"])
            self.assertGreaterEqual(task["transferred"], 0)
            if task["status"] == "completed":
                self.assertEqual(task["transferred"], task["size"])


if __name__ == "__main__":
    unittest.main()
