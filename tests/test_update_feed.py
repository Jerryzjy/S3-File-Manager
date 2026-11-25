from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request

from r2_batch_download import update_feed


_URL = "https://updates.example.com/releases/latest.json"
_ASSET = "https://updates.example.com/releases/S3FileManager.exe"
_PAYLOAD = b"Synthetic test payload for the update package."
_DIGEST = sha256(_PAYLOAD).hexdigest()


class _Response(BytesIO):
    def __init__(self, data: bytes, url: str = _URL, content_length: int | None = None):
        super().__init__(data)
        self.url = url
        self.headers = {} if content_length is None else {"Content-Length": str(content_length)}

    def geturl(self) -> str:
        return self.url


class UpdateFeedTests(unittest.TestCase):
    def manifest(self, **changes) -> bytes:
        return json.dumps({
            "schema_version": 1,
            "version": "1.2.0-beta.10",
            "url": "S3FileManager.exe",
            "size": len(_PAYLOAD),
            "sha256": _DIGEST,
            "notes": "改进下载队列",
            **changes,
        }).encode()

    def test_compares_numeric_beta_versions_and_resolves_relative_asset(self):
        with patch.object(update_feed, "_open", return_value=_Response(self.manifest())):
            release = update_feed.fetch_update(_URL, "1.2.0-beta.2")
        self.assertEqual(release, update_feed.UpdateRelease("1.2.0-beta.10", _ASSET, _DIGEST, len(_PAYLOAD), "改进下载队列"))
        for current in ("1.2.0-beta.10", "1.2.0"):
            with self.subTest(current=current), patch.object(update_feed, "_open", return_value=_Response(self.manifest())):
                self.assertIsNone(update_feed.fetch_update(_URL, current))

    def test_rejects_oversized_or_invalid_manifest(self):
        cases = [
            b"X" * (update_feed.MAX_MANIFEST_BYTES + 1),
            self.manifest(schema_version=True),
            self.manifest(version="latest"),
            self.manifest(size=True),
            self.manifest(sha256="broken"),
            self.manifest(url="file:///C:/update.exe"),
            self.manifest(url="http://example.com/update.exe"),
        ]
        for index, data in enumerate(cases):
            with self.subTest(index=index), patch.object(update_feed, "_open", return_value=_Response(data)):
                with self.assertRaises(update_feed.UpdateError):
                    update_feed.fetch_update(_URL, "1.2.0-beta.2")

    def test_checks_url_and_downgrade_redirect(self):
        self.assertEqual(update_feed.validate_feed_url(_URL), _URL)
        with self.assertRaises(update_feed.UpdateError):
            update_feed.validate_feed_url("http://localhost/manifest.json")
        with self.assertRaises(update_feed.UpdateError):
            update_feed.validate_feed_url("https://username:password@updates.example.com/latest.json")
        with self.assertRaises(update_feed.UpdateError):
            update_feed._UpdateRedirectHandler(True).redirect_request(
                Request(_URL), None, 302, "Found", {}, "http://127.0.0.1/update.exe",
            )

    def test_stages_only_the_verified_payload(self):
        release = update_feed.UpdateRelease("1.2.0", _ASSET, _DIGEST, len(_PAYLOAD))
        progress = []
        with tempfile.TemporaryDirectory() as temp:
            stage = Path(temp)
            with patch.object(update_feed, "_open", return_value=_Response(_PAYLOAD, _ASSET, len(_PAYLOAD))):
                executable = update_feed.download_update(release, stage, on_progress=lambda done, total: progress.append((done, total)))
            self.assertEqual([executable], list(stage.iterdir()))
            self.assertEqual(executable.suffix, ".exe")
            self.assertEqual(executable.read_bytes(), _PAYLOAD)
        self.assertEqual(progress[-1], (len(_PAYLOAD), len(_PAYLOAD)))

    def test_removes_partial_on_bad_size_bad_digest_and_cancel(self):
        release = update_feed.UpdateRelease("1.2.0", _ASSET, _DIGEST, len(_PAYLOAD))
        for payload, length, cancel_during_progress in (
            (_PAYLOAD[:-1], None, False),
            (_PAYLOAD + b"x", None, False),
            (b"a" * len(_PAYLOAD), len(_PAYLOAD), False),
            (_PAYLOAD, len(_PAYLOAD) - 1, False),
            (_PAYLOAD, len(_PAYLOAD), True),
        ):
            with self.subTest(length=length, cancelled=cancel_during_progress), tempfile.TemporaryDirectory() as temp:
                stage = Path(temp)
                cancel = threading.Event()
                on_progress = (lambda done, total: cancel.set()) if cancel_during_progress else None
                with patch.object(update_feed, "_open", return_value=_Response(payload, _ASSET, length)):
                    with self.assertRaises(update_feed.UpdateError):
                        update_feed.download_update(release, stage, on_progress=on_progress, cancel=cancel)
                self.assertEqual([], list(stage.iterdir()))
