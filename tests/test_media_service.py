"""Loopback service regressions with synthetic bytes; no credentials or cloud APIs."""

import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest


spec = importlib.util.spec_from_file_location(
    "media_service_recovery", Path(__file__).resolve().parents[1] / "server/media_service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class MediaServiceRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.now = time.time()
        self.store = service.Store(temporary.name, "a" * 64, "b" * 64,
                                   "https://example.invalid", clock=lambda: self.now)
        self.server = service.Server(("127.0.0.1", 0), self.store)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.body = b"\x00\x00\x00\x18ftypisom" + b"synthetic service recovery bytes" * 100
        self.object_id = hashlib.sha256(self.body).hexdigest()
        self.path = service.PREFIX + "/objects/" + self.object_id

    def request(self, method, path, body=None, content_type=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        headers = {"Authorization": "Bearer " + "a" * 64}
        if content_type:
            headers["Content-Type"] = content_type
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            payload = json.loads(response.read())
            return response.status, payload
        finally:
            connection.close()

    def chunk(self, offset, body):
        return self.request("PUT", self.path + f"/upload?offset={offset}&total={len(self.body)}",
                            body, "application/octet-stream")

    def upload_complete(self):
        status, metadata = self.request("PUT", self.path, self.body, "video/mp4")
        self.assertEqual(status, 200)
        self.assertEqual(metadata["sha256"], self.object_id)

    def make_expired(self, path):
        expired = self.now - service.RETENTION - 1
        os.utime(path, (expired, expired))

    def assert_published(self):
        status, metadata = self.request("GET", self.path)
        self.assertEqual(status, 200)
        self.assertEqual(metadata["sha256"], self.object_id)
        self.assertEqual(metadata["bytes"], len(self.body))
        self.assertEqual(self.store.path(self.object_id).read_bytes(), self.body)

    def test_expired_complete_object_can_be_reuploaded_without_manual_cleanup(self):
        self.upload_complete()
        destination = self.store.path(self.object_id)
        self.make_expired(destination)
        self.assertEqual(self.request("GET", self.path)[0], 410)
        self.assertTrue(destination.exists())
        status, position = self.request("GET", self.path + "/upload")
        self.assertEqual((status, position["received_bytes"]), (200, 0))
        status, result = self.chunk(0, self.body)
        self.assertEqual(status, 200)
        self.assertTrue(result["complete"])
        self.assert_published()

    def test_live_complete_object_is_not_overwritten_by_chunk_upload(self):
        self.upload_complete()
        destination = self.store.path(self.object_id)
        before = destination.stat().st_mtime_ns
        status, result = self.chunk(0, self.body[:-1] + b"!")
        self.assertEqual(status, 409)
        self.assertEqual(result["error"], "already_complete")
        self.assertEqual(destination.stat().st_mtime_ns, before)
        self.assert_published()

    def test_fully_received_partial_can_finalize_with_empty_last_chunk(self):
        partial = self.store.root / (".partial-" + self.object_id)
        partial.write_bytes(self.body)
        status, position = self.request("GET", self.path + "/upload")
        self.assertEqual((status, position["received_bytes"]), (200, len(self.body)))
        self.assertEqual(self.request("POST", self.path + "/ticket", b"")[0], 404)
        status, result = self.chunk(len(self.body), b"")
        self.assertEqual(status, 200)
        self.assertTrue(result["complete"])
        self.assertFalse(partial.exists())
        self.assert_published()

    def test_expired_partial_recovery_requires_fresh_offset_before_restarting(self):
        partial = self.store.root / (".partial-" + self.object_id)
        split = len(self.body) // 2
        partial.write_bytes(self.body[:split])
        self.make_expired(partial)
        status, position = self.request("GET", self.path + "/upload")
        self.assertEqual((status, position["received_bytes"]), (200, split))
        status, result = self.chunk(split, self.body[split:])
        self.assertEqual(status, 409)
        self.assertEqual(result["error"], "expired_partial")
        self.assertEqual(self.request("GET", self.path)[0], 404)
        status, position = self.request("GET", self.path + "/upload")
        self.assertEqual((status, position["received_bytes"]), (200, 0))
        self.assertEqual(self.chunk(0, self.body)[0], 200)
        self.assert_published()


if __name__ == "__main__":
    unittest.main()
