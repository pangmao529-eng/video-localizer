"""Bounded job protocol and URL secrecy; no network or credential access."""
import copy
import json
import unittest
from unittest.mock import MagicMock

from video_localizer.selfhost_media import MediaClient
from video_localizer.seedance_client import SeedanceError


class SelfhostJobTests(unittest.TestCase):
    def setUp(self):
        self.client = MediaClient.__new__(MediaClient)
        self.client.endpoint = "https://media.example.com/codex-media/v1"
        self.client.transport = MagicMock()
        self.client.transport._key = "synthetic-upload-key"
        self.receipt = {"id": "b" * 64, "sha256": "b" * 64, "bytes": 1000}
        self.media = {"width": 1920, "height": 1080, "durationMs": 8000, "fps": 24,
                      "fpsRatio": "24/1", "bytes": 1000, "hasAudio": False,
                      "videoCodec": "h264", "audioCodec": None, "frame_count": 192}

    def job(self, kind, result=None, status="succeeded", **extra):
        value = {"id": "a" * 64, "kind": kind, "status": status, **extra}
        if result is not None:
            value["result"] = result
        return value

    def split(self):
        return {"source_id": "c" * 64, "policy": "fixed-15s-v1",
                "source_media": dict(self.media, sha256="c" * 64),
                "parts": [dict(self.receipt, index=1, start_frame=0, end_frame=192, frame_count=192,
                               start_seconds=0, duration_seconds=8)]}

    def test_import_url_is_sent_only_in_request_not_returned_to_checkpoint(self):
        private_url = "https://cloud.example.com/result.mp4?signature=do-not-persist"
        response = self.job("import", dict(self.receipt, media=dict(self.media, url=private_url), url=private_url),
                            request={"url": private_url}, message=private_url)
        self.client.transport._request.return_value = response
        result = self.client.submit_job({"kind": "import", "task_id": "cgt-test", "url": private_url})
        self.assertEqual(result["result"], dict(self.receipt, media=self.media))
        self.assertNotIn("do-not-persist", json.dumps(result))
        call = self.client.transport._request.call_args
        self.assertEqual(call.args, ("POST", "/jobs"))
        self.assertIn(private_url, call.kwargs["body"].decode())
        self.assertNotIn("synthetic-upload-key", call.kwargs["body"].decode())

    def test_invalid_request_shapes_are_rejected_before_network(self):
        good_join = {"kind": "join", "source_id": "c"*64, "split_job_id": "d"*64,
                     "parts": [{"index": 1, "task_id": "cgt-1", "object_id": "b"*64}]}
        payloads = [None, {"kind": "unknown"}, {"kind": "split", "source_id": "c"*64, "policy": "other"},
                    {"kind": "import", "task_id": "cgt-1", "url": "http://127.0.0.1/a.mp4"},
                    {"kind": "import", "task_id": "wrong", "url": "https://public.example.com/a.mp4"},
                    {**good_join, "parts": []},
                    {**good_join, "parts": [{"index": 2, "task_id": "cgt-1", "object_id": "b"*64}]},
                    {**good_join, "key": "secret"},
                    {**good_join, "parts": good_join["parts"] + [{"index": 2, "task_id": "cgt-1", "object_id": "e"*64}]}]
        for value in payloads:
            with self.subTest(value=value), self.assertRaises(SeedanceError):
                self.client.submit_job(value)
        self.client.transport._request.assert_not_called()

    def test_same_upload_secret_cannot_be_forwarded_in_import_url(self):
        with self.assertRaises(SeedanceError):
            self.client.submit_job({"kind": "import", "task_id": "cgt-test",
                                    "url": "https://cloud.example.com/" + self.client.transport._key})
        self.client.transport._request.assert_not_called()

    def test_split_receipt_has_complete_safe_frame_binding(self):
        payload = {"kind": "split", "source_id": "c"*64, "policy": "fixed-15s-v1"}
        self.client.transport._request.return_value = self.job("split", self.split(), arbitrary="discard")
        returned = self.client.submit_job(payload)
        self.assertEqual(returned["result"], self.split())
        self.assertNotIn("arbitrary", returned)
        for change in (lambda r: r["parts"][0].update(start_frame=1),
                       lambda r: r["parts"][0].update(duration_seconds=16),
                       lambda r: r["source_media"].update(frame_count=193),
                       lambda r: r["source_media"].update(sha256="e"*64),
                       lambda r: r["parts"][0].update(bytes=True),
                       lambda r: r["source_media"].update(fpsRatio="24/0")):
            value = self.split()
            change(value)
            self.client.transport._request.return_value = self.job("split", value)
            with self.assertRaises(SeedanceError):
                self.client.submit_job(payload)

    def test_job_identity_and_kind_cannot_change(self):
        self.client.transport._request.return_value = self.job("split", status="running")
        with self.assertRaises(SeedanceError):
            self.client.get_job("d"*64)
        self.client.transport._request.return_value = self.job("import", dict(self.receipt, media=self.media))
        with self.assertRaises(SeedanceError):
            self.client.submit_job({"kind": "split", "source_id": "c"*64, "policy": "fixed-15s-v1"})

    def test_failure_code_is_allowlisted_not_server_prose(self):
        self.client.transport._request.return_value = self.job("split", status="failed",
            error="https://private.example.com/?signature=secret", message="server prose")
        result = self.client.get_job("a"*64)
        self.assertEqual(result["error"], "processing_failed")
        self.assertNotIn("secret", json.dumps(result))
        self.client.transport._request.return_value = self.job("split", status="failed", error="result_expired")
        self.assertEqual(self.client.get_job("a"*64)["error"], "result_expired")

    def test_response_cannot_echo_upload_credential_into_report(self):
        self.client.transport._request.return_value = self.job("split", status="failed",
                                                             error=self.client.transport._key)
        with self.assertRaises(SeedanceError) as result:
            self.client.get_job("a"*64)
        self.assertNotIn(self.client.transport._key, str(result.exception))

    def test_join_checks_must_agree_with_passed_status(self):
        review = {"status": "passed", "checks": {"full_decode": True, "duration_preserved": False},
                  "output_media": self.media, "source_audio_reused": True}
        self.client.transport._request.return_value = self.job("join", {"output": self.receipt, "technical_review": review})
        with self.assertRaises(SeedanceError):
            self.client.get_job("a"*64)
        review["status"] = "failed"
        result = self.client.get_job("a"*64)
        self.assertEqual(result["result"]["technical_review"]["status"], "failed")
        self.assertFalse(result["result"]["technical_review"]["model_reviewed"])


if __name__ == "__main__":
    unittest.main()
