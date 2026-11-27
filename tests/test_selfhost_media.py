"""Synthetic transport and permission tests; no model calls or real credentials."""
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
from unittest.mock import patch, MagicMock
from urllib.parse import urlsplit

from video_localizer import config, selfhost_media
from video_localizer.seedance_client import SeedanceError

spec = importlib.util.spec_from_file_location("private_media_service", Path(__file__).resolve().parents[1] / "server/media_service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = time.time()
        self.store = service.Store(self.temp.name, "a"*64, "b"*64, "https://example.com", clock=lambda: self.now)
        self.server = service.Server(("127.0.0.1", 0), self.store)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.body = b"\x00\x00\x00\x18ftypisom" + b"synthetic transport bytes"*200
        self.sha = hashlib.sha256(self.body).hexdigest()
        self.path = service.PREFIX + "/objects/" + self.sha

    def call(self, method, path=None, body=None, auth=True, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        h = {"Authorization": "Bearer " + "a"*64} if auth else {}
        h.update(headers or {})
        connection.request(method, path or self.path, body, h)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def upload(self):
        status, _, raw = self.call("PUT", body=self.body, headers={"Content-Type": "video/mp4"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["sha256"], self.sha)

    def ticket(self):
        status, _, raw = self.call("POST", self.path + "/ticket", body=b"")
        self.assertEqual(status, 200)
        p = urlsplit(json.loads(raw)["url"])
        return p.path + "?" + p.query

    def test_signed_reads_ranges_expiry_and_private_control(self):
        self.assertEqual(self.call("PUT", body=self.body, auth=False)[0], 401)
        self.upload()
        self.assertEqual(self.call("GET", auth=False)[0], 401)
        self.assertEqual(self.call("POST", self.path + "/ticket", auth=False)[0], 401)
        self.assertEqual(self.call("GET", self.path + ".mp4", auth=False)[0], 403)
        ticket = self.ticket()
        status, headers, raw = self.call("GET", ticket, auth=False)
        self.assertEqual((status, raw), (200, self.body))
        self.assertEqual(headers["Content-Type"], "video/mp4")
        self.assertEqual(self.call("HEAD", ticket, auth=False)[2], b"")
        result = self.call("GET", ticket, auth=False, headers={"Range": "bytes=4-7"})
        self.assertEqual((result[0], result[2]), (206, b"ftyp"))
        self.assertEqual(self.call("GET", ticket, auth=False, headers={"Range": "bytes=999999-"})[0], 416)
        self.assertEqual(self.call("GET", ticket.replace(self.sha, "c"*64), auth=False)[0], 403)
        self.assertEqual(self.call("GET", ticket + "&signature=a", auth=False)[0], 403)
        self.now += 6*3600 + 1
        self.assertEqual(self.call("GET", ticket, auth=False)[0], 403)

    def test_hash_idempotence_validation_and_deletion(self):
        self.upload()
        self.upload()
        self.assertEqual(len(list(self.store.root.glob("*.mp4"))), 1)
        self.assertEqual(self.call("PUT", body=self.body+b"wrong", headers={"Content-Type":"video/mp4"})[0], 422)
        self.assertEqual(self.call("PUT", body=b"x"*20, headers={"Content-Type":"video/mp4"})[0], 415)
        self.assertEqual(self.call("GET", headers={"Authorization":"Bearer é"})[0], 401)
        self.assertEqual(self.call("GET", service.PREFIX + "/objects/../secrets")[0], 404)
        self.assertEqual(self.call("DELETE", auth=False)[0], 401)
        self.assertEqual(self.call("DELETE")[0], 200)
        self.assertEqual(self.call("GET")[0], 404)

    def test_cleanup_is_scoped_and_expired_upload_can_be_replaced(self):
        self.upload()
        unrelated = self.store.root / "keep.txt"
        unrelated.write_text("unrelated")
        abandoned = self.store.root / (".upload-" + "c"*32)
        abandoned.write_bytes(b"partial")
        os.utime(abandoned, (self.now-8000, self.now-8000))
        self.store.cleanup_and_usage()
        self.assertFalse(abandoned.exists())
        self.now += service.RETENTION + 1
        self.assertEqual(self.call("GET")[0], 410)
        self.store.cleanup_and_usage()
        self.assertFalse(self.store.path(self.sha).exists())
        self.assertTrue(unrelated.exists())

    def test_bounded_chunks_resume_and_verify_before_becoming_readable(self):
        self.body += b"z"*(600*1024)
        self.sha = hashlib.sha256(self.body).hexdigest()
        self.path = service.PREFIX + "/objects/" + self.sha
        position = self.path + "/upload"
        self.assertEqual(json.loads(self.call("GET", position)[2])["received_bytes"], 0)
        def send(offset, data):
            return self.call("PUT", position + f"?offset={offset}&total={len(self.body)}", body=data,
                             headers={"Content-Type":"application/octet-stream"})
        first = self.body[:256*1024]
        self.assertEqual(send(0,first)[0], 200)
        self.assertEqual(send(0,first)[0], 409)
        self.assertEqual(self.call("GET")[0], 404)
        self.assertEqual(self.call("POST", self.path + "/ticket")[0], 404)
        offset = json.loads(self.call("GET",position)[2])["received_bytes"]
        self.assertEqual(offset,len(first))
        while offset < len(self.body):
            data = self.body[offset:offset+256*1024]
            status, _, raw = send(offset,data)
            self.assertEqual(status,200)
            offset += len(data)
        self.assertTrue(json.loads(raw)["complete"])
        self.assertEqual(self.call("GET",self.ticket(),auth=False)[2], self.body)

    def test_corrupt_final_chunk_never_publishes(self):
        status,_,_ = self.call("PUT",self.path+f"/upload?offset=0&total={len(self.body)}",
            body=self.body[:-1]+b"!",headers={"Content-Type":"application/octet-stream"})
        self.assertEqual(status,422)
        self.assertEqual(self.call("GET")[0],404)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.endpoint = "https://example.com/codex-media/v1"
        self.sha = "d"*64
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "synthetic.mp4"
        self.path.write_bytes(b"synthetic bytes"*100)
        self.meta = {"id": self.sha, "sha256": self.sha, "bytes": self.path.stat().st_size}
        self.transport = MagicMock()
        with patch.object(config, "read_key", return_value="media-only") as credential, patch.object(selfhost_media, "SeedanceClient", return_value=self.transport) as factory:
            self.client = selfhost_media.MediaClient(self.endpoint)
            credential.assert_called_once_with(self.endpoint)
            factory.assert_called_once_with(self.endpoint, "media-only", cancelled=None)

    def test_lost_upload_reply_recovers_by_hash_without_second_put(self):
        error = SeedanceError("network", "synthetic lost reply")
        calls = []
        def request(method, suffix, **kwargs):
            calls.append(method)
            if calls == ["GET"]:
                raise SeedanceError("http_error", "missing", http_status=404)
            if method == "GET" and suffix.endswith("/upload"):
                return {"id":self.sha,"sha256":self.sha,"received_bytes":0}
            if method == "PUT":
                self.assertEqual(kwargs["body"], self.path.read_bytes())
                raise error
            return self.meta
        self.transport._request.side_effect = request
        with self.assertRaises(SeedanceError):
            self.client.ensure_uploaded(self.path, self.sha)
        self.assertEqual(self.client.ensure_uploaded(self.path, self.sha), self.meta)
        self.assertEqual(calls, ["GET", "GET", "PUT", "GET"])

    def test_client_resumes_partial_from_server_offset(self):
        self.path.write_bytes(b"x"*(700*1024))
        size = self.path.stat().st_size
        offsets = []
        received = 256*1024
        def request(method, suffix, **kwargs):
            nonlocal received
            if method == "GET" and suffix.endswith("/upload"):
                return {"id":self.sha,"sha256":self.sha,"received_bytes":received}
            if method == "GET":
                raise SeedanceError("http_error","missing",http_status=404)
            self.assertIn(f"offset={received}&total={size}",suffix)
            offsets.append(received)
            self.assertLessEqual(len(kwargs["body"]),256*1024)
            received += len(kwargs["body"])
            return ({"id":self.sha,"sha256":self.sha,"bytes":size,"complete":True} if received == size
                    else {"id":self.sha,"sha256":self.sha,"received_bytes":received,"complete":False})
        self.transport._request.side_effect = request
        self.assertEqual(self.client.ensure_uploaded(self.path,self.sha)["bytes"],size)
        self.assertEqual(offsets,[256*1024,512*1024])

    def test_auth_failure_and_mismatched_metadata_never_upload(self):
        self.transport._request.side_effect = SeedanceError("authentication", "no", http_status=401)
        with self.assertRaises(SeedanceError): self.client.ensure_uploaded(self.path, self.sha)
        self.assertEqual(self.transport._request.call_count, 1)
        self.transport._request.side_effect = None
        self.transport._request.return_value = dict(self.meta, bytes=1)
        with self.assertRaises(SeedanceError): self.client.ensure_uploaded(self.path, self.sha)

    def test_signed_reference_bound_to_server_file_and_lifetime(self):
        expiry = int(time.time()) + 3600
        url = f"{self.endpoint}/objects/{self.sha}.mp4?expires={expiry}&signature={'e'*64}"
        self.transport._request.return_value = dict(self.meta, url=url, expires_at=expiry)
        self.assertEqual(self.client.reference(self.sha, self.meta["bytes"]), url)
        for bad in [url.replace("example.com", "foreign.example"), url.replace(self.sha, "f"*64), url+"&expires=1"]:
            self.transport._request.return_value = dict(self.meta, url=bad, expires_at=expiry)
            with self.assertRaises(SeedanceError): self.client.reference(self.sha, self.meta["bytes"])

    def test_backend_changes_do_not_reuse_old_upload_checkpoints(self):
        value = {"provider":"self_hosted", "endpoint":self.endpoint}
        with patch.object(selfhost_media, "configuration", return_value=value):
            self.assertEqual(selfhost_media.bind_report({"media_backend":value}), value)
            with self.assertRaises(config.SettingsError): selfhost_media.bind_report({})
