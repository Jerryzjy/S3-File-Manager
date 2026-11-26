from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import Mock, patch

from botocore.response import StreamingBody

from r2_batch_download.bucket_tools import find_duplicates
from r2_batch_download.catalog_index import CatalogIndex
from r2_batch_download.desktop_engine import Connection
from r2_batch_download.object_actions import public_object_url
from r2_batch_download.upload_import import save_url_sources, urls_from_text


class BucketToolsTests(unittest.TestCase):
    def test_duplicate_groups_use_content_and_can_filter_the_prefix(self):
        client = Mock()
        contents = {"docs/older.txt": b"abc", "docs/newer.txt": b"abc", "docs/other.txt": b"xyz"}
        client.get_object.side_effect = lambda **request: {
            "Body": StreamingBody(BytesIO(contents[request["Key"]]), 3)
        }
        catalog = CatalogIndex([
            {"key": key, "size": 3, "modified": datetime(2026, 11, index + 1, tzinfo=timezone.utc),
             "etag": f'"etag-{index}"'} for index, key in enumerate(contents)
        ] + [{"key": "backups/ignored.txt", "size": 3}])
        connection = Connection("account", "test", "secret", "bucket")
        with patch("r2_batch_download.bucket_tools.create_client", return_value=client):
            groups = find_duplicates(catalog, connection, "docs/", ".txt", Event(), lambda *_: None)
        self.assertEqual([[entry["key"] for entry in group] for group in groups], [["docs/newer.txt", "docs/older.txt"]])
        self.assertEqual(client.get_object.call_count, 3)
        self.assertEqual(client.get_object.call_args_list[0].kwargs["IfMatch"], '"etag-0"')
        client.close.assert_called_once()

    def test_public_object_url_escapes_object_key_without_changing_path(self):
        self.assertEqual(public_object_url("https://PUBLIC.Example.com/", "docs/a b?#.txt"),
                         "https://public.example.com/docs/a%20b%3F%23.txt")
        for base, key in (("https://user@public.example.com", "a"), ("https://public.example.com", "../a")):
            with self.assertRaises(ValueError):
                public_object_url(base, key)

    def test_url_import_preserves_url_queries_and_local_files(self):
        class Response(BytesIO):
            status = 200

        target = "https://public.example.com/report.txt?ids=a,b"
        self.assertEqual(urls_from_text(f"{target}\n{target}"), (target,))
        opener = Mock()
        opener.open.return_value = Response(b"new")
        with TemporaryDirectory() as directory, patch("r2_batch_download.upload_import.build_opener", return_value=opener):
            original = Path(directory) / "report.txt"
            original.write_bytes(b"old")
            paths = save_url_sources((target,), Path(directory), Event(), lambda *_: None)
            self.assertEqual(original.read_bytes(), b"old")
            self.assertEqual(paths[0].name, "report-1.txt")
            self.assertEqual(paths[0].read_bytes(), b"new")
