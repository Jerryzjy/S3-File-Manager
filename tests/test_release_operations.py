"""Check new desktop operations with an SDK transport substitute."""
from __future__ import annotations

import io
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit
from zipfile import ZipFile

from botocore.awsrequest import AWSResponse
from botocore.exceptions import ClientError

from r2_batch_download import client_store, diagnostics, object_actions, upload
from r2_batch_download.desktop_engine import Connection, create_client, validate_connection


class WireBody(io.BytesIO):
    def stream(self, *args, **kwargs):
        yield self.read()


class ReleaseOperationTests(unittest.TestCase):
    def test_b2_and_custom_s3_sign_with_configured_endpoint_and_keep_state(self):
        configurations = [
            ("b2", "https://s3.us-west-004.backblazeb2.com", "", "us-west-004"),
            ("s3", "https://s3.us-east-2.amazonaws.com", "us-east-2", "us-east-2"),
        ]
        with TemporaryDirectory() as folder, patch.object(client_store, "state_path", return_value=Path(folder) / "state.json"):
            profiles = []
            for provider, endpoint, region, expected in configurations:
                connection = Connection("", "fake-id", "fake-secret", "bucket", endpoint, provider, region)
                with self.subTest(provider=provider):
                    sdk = create_client(connection)
                    try:
                        self.assertEqual(sdk.meta.region_name, expected)
                        self.assertEqual(sdk.meta.endpoint_url, endpoint)
                    finally:
                        sdk.close()
                    profiles.append({"id": provider, "bucket": "bucket", "provider": provider,
                                     "endpoint_url": endpoint, "region": region})
            client_store.save_state(profiles, [], {})
            self.assertEqual([row["provider"] for row in client_store.load_state()["profiles"]], ["b2", "s3"])
        with self.assertRaises(ValueError):
            validate_connection(Connection("", "fake-id", "fake-secret", "bucket", "https://example.com", "b2"))

    def test_copy_signs_source_and_destination_guards_and_presign_uses_get(self):
        connection = Connection("account", "fake-id", "fake-secret", "bucket")
        sdk = create_client(connection, upload=True)
        signed_copy = []

        def send(request, **kwargs):
            headers = {key.lower(): value.decode() if isinstance(value, bytes) else value for key, value in request.headers.items()}
            signed_copy.append(headers)
            if request.url.endswith("conflict.txt"):
                return AWSResponse(request.url, 412, {}, WireBody(b"<Error><Code>PreconditionFailed</Code></Error>"))
            data = b'<CopyObjectResult><ETag>"copied"</ETag></CopyObjectResult>'
            return AWSResponse(request.url, 200, {}, WireBody(data))

        sdk.meta.events.register("before-send.s3.CopyObject", send)
        tasks = [dict(id=key, key=f"target/{key}", source_key="source/A +.txt", bucket="bucket", etag='"original"', size=5)
                 for key in ("new.txt", "conflict.txt")]
        events = []
        with patch.object(object_actions, "create_client", return_value=sdk):
            object_actions.transfer_copies(connection, tasks, {t["id"]: Event() for t in tasks}, 1, events.append,
                                           pause_events={t["id"]: Event() for t in tasks})
        self.assertEqual(len(signed_copy), 2)
        for headers in signed_copy:
            self.assertEqual(headers["cf-copy-destination-if-none-match"], "*")
            self.assertEqual(headers["x-amz-copy-source-if-match"], '"original"')
            self.assertEqual(unquote(headers["x-amz-copy-source"]).lstrip("/"), "bucket/source/A +.txt")
            self.assertIn("cf-copy-destination-if-none-match", headers["authorization"])
        self.assertEqual({event["id"]: event["status"] for event in events if event["kind"] == "result"},
                         {"new.txt": "completed", "conflict.txt": "failed"})
        url = urlsplit(object_actions.generate_download_link(connection, "中文/A +.txt", 900))
        self.assertEqual(unquote(url.path), "/bucket/中文/A +.txt")
        self.assertEqual(parse_qs(url.query)["X-Amz-Expires"], ["900"])
        self.assertNotIn("fake-secret", url.geturl())

    def test_upload_rename_reserves_other_files_in_same_batch(self):
        endpoint = Connection("account", "fake-id", "fake-secret", "bucket")
        with TemporaryDirectory() as folder:
            paths = tuple(Path(folder) / name for name in ("note.txt", "note (2).txt"))
            for path in paths:
                path.write_text("test", encoding="utf-8")

            class Storage:
                def head_object(self, *, Bucket, Key):
                    if Key in {"docs/note.txt", "docs/note (1).txt"}:
                        return {"ETag": '"original"'}
                    raise ClientError({"Error": {"Code": "404"}}, "HeadObject")

            with patch.object(upload, "create_client", return_value=Storage()):
                rows = upload.plan_upload(endpoint, "docs/", paths, Event(), rename_conflicts=True)
            self.assertEqual({row.key for row in rows}, {"docs/note (2).txt", "docs/note (3).txt"})
            self.assertTrue(all(not row.remote_etag for row in rows))
            self.assertEqual({row.original_key for row in rows if row.original_key}, {"docs/note.txt"})

    def test_diagnostics_export_uses_sanitized_frames_and_excludes_state(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            state = root / "client-state.json"
            state.write_text("private-state-key", encoding="utf-8")
            logs = root / "logs"
            logs.mkdir()
            (logs / "errors.log").write_text("2026-11-26 12:00:00,005 RuntimeError\nengine.py:12 in run\nsecret=abc\n", encoding="utf-8")
            (logs / "native-fault.log").write_text("C:/Users/Private/anything", encoding="utf-8")
            output = root / "report.zip"
            with patch.object(diagnostics, "state_path", return_value=state):
                diagnostics.export_diagnostics(output, "test-version")
            with ZipFile(output) as report:
                self.assertEqual(set(report.namelist()), {"version.txt", "errors.log"})
                self.assertIn(b"engine.py", report.read("errors.log"))
                self.assertNotIn(b"secret", report.read("errors.log"))
