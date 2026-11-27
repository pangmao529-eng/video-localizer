"""Authenticated bounded job routing; synthetic loopback, no model or credentials."""
import http.client
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest

spec = importlib.util.spec_from_file_location("media_jobs_service", Path(__file__).resolve().parents[1] / "server/media_service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class WorkerError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status


class FakeProcessor:
    def __init__(self):
        self.calls = []
        self.error = None

    def health(self):
        return {"available": True, "policy": "fixed-15s-v1", "max_source_seconds": 300,
                "max_parts": 20, "ffmpeg_ready": True, "ffprobe_ready": True,
                "private_binary_path": "DO-NOT-RETURN"}

    def submit(self, value):
        self.calls.append(("submit", value))
        if self.error:
            raise self.error
        return {"id": "c" * 64, "kind": value["kind"], "status": "queued"}

    def get(self, job_id):
        self.calls.append(("get", job_id))
        if self.error:
            raise self.error
        return {"id": job_id, "kind": "split", "status": "succeeded", "result": {"parts": []}}


class JobHttpTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = service.Store(temp.name, "a" * 64, "b" * 64, "https://example.invalid")
        self.processor = FakeProcessor()
        self.server = service.Server(("127.0.0.1", 0), self.store, self.processor)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.path = service.PREFIX + "/jobs"

    def request(self, method, path=None, body=None, *, auth=True, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        values = {"Content-Type": "application/json"}
        if auth:
            values["Authorization"] = "Bearer " + "a" * 64
        values.update(headers or {})
        try:
            connection.request(method, path or self.path, body=body, headers=values)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def payload(self, **extra):
        return json.dumps({"kind": "split", "source_id": "d" * 64, "policy": "fixed-15s-v1", **extra}).encode()

    def test_job_requests_require_upload_credential(self):
        for method, path, body in (("POST", self.path, self.payload()), ("GET", self.path + "/" + "c" * 64, None)):
            status, data = self.request(method, path, body, auth=False)
            self.assertEqual((status, data), (401, {"error": "unauthorized"}))
        self.assertEqual(self.processor.calls, [])

    def test_create_and_query_returns_job_not_an_output_url(self):
        status, data = self.request("POST", body=self.payload())
        self.assertEqual(status, 202)
        self.assertEqual(data["status"], "queued")
        status, data = self.request("GET", self.path + "/" + data["id"])
        self.assertEqual((status, data["status"]), (200, "succeeded"))
        self.assertEqual([call[0] for call in self.processor.calls], ["submit", "get"])

    def test_import_and_join_request_shapes_are_forwarded_without_extra_fields(self):
        for value in ({"kind": "import", "task_id": "cgt-test", "url": "https://example.invalid/signed?secret=not-real"},
                      {"kind": "join", "source_id": "d" * 64, "split_job_id": "c" * 64,
                       "parts": [{"index": 1, "task_id": "cgt-test", "object_id": "e" * 64}]}):
            status, response = self.request("POST", body=json.dumps(value).encode())
            self.assertEqual(status, 202)
            self.assertEqual(self.processor.calls[-1], ("submit", value))
            self.assertNotIn("url", json.dumps(response))

    def test_query_path_and_methods_cannot_smuggle_arguments(self):
        cases = [("GET", self.path + "/" + "c" * 64 + "?url=private"),
                 ("POST", self.path + "/" + "c" * 64),
                 ("DELETE", self.path + "/" + "c" * 64), ("GET", self.path)]
        for method, path in cases:
            with self.subTest(method=method, path=path):
                status, _ = self.request(method, path, self.payload() if method == "POST" else None)
                self.assertIn(status, {400, 405})
        self.assertEqual(self.processor.calls, [])

    def test_rejects_oversized_non_json_duplicate_and_extra_fields(self):
        for payload, content_type in [(b" " * (service.MAX_JOB_BODY + 1), "application/json"),
                                      (self.payload(), "text/plain"),
                                      (b'{"kind":"split","kind":"import"}', "application/json"),
                                      (self.payload(api_key="must-not-forward"), "application/json"),
                                      (b"[]", "application/json"), (b"{", "application/json")]:
            with self.subTest(content_type=content_type, length=len(payload)):
                status, _ = self.request("POST", body=payload, headers={"Content-Type": content_type})
                self.assertIn(status, {400, 413, 415})
        self.assertEqual(self.processor.calls, [])

    def test_worker_errors_return_only_bounded_safe_codes(self):
        self.processor.error = WorkerError("source_expired", 410)
        self.assertEqual(self.request("POST", body=self.payload()), (410, {"error": "source_expired"}))
        self.processor.error = WorkerError("https://secret.invalid/?signature=secret", 99)
        self.assertEqual(self.request("POST", body=self.payload()), (500, {"error": "processing_error"}))
        self.processor.error = RuntimeError("password and signed-url never returned")
        self.assertEqual(self.request("POST", body=self.payload()), (500, {"error": "processing_error"}))

    def test_health_exposes_capabilities_without_paths_and_staging_remains_available(self):
        status, data = self.request("GET", "/codex-media/healthz", auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(data["version"], service.VERSION)
        self.assertTrue(data["processing"]["ffmpeg_ready"])
        self.assertNotIn("DO-NOT-RETURN", json.dumps(data))
        self.server.processor = None
        self.assertEqual(self.request("POST", body=self.payload()), (503, {"error": "processing_unavailable"}))
        status, data = self.request("GET", "/codex-media/healthz", auth=False)
        self.assertTrue(data["ok"])
        self.assertFalse(data["processing"]["available"])


if __name__ == "__main__":
    unittest.main()
