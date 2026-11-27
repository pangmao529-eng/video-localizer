"""Mocked orchestration: only cloud edits and only the assembled file reaches the desktop."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from video_localizer import batch, config, seedance_batch, seedance_server_batch as server_batch
from video_localizer.seedance_client import SeedanceError


def sha(value):
    return hashlib.sha256(value).hexdigest()


def receipt(value):
    return {"id": sha(value), "sha256": sha(value), "bytes": len(value)}


class FakeMedia:
    def __init__(self, owner):
        self.owner = owner
        self.jobs, self.calls = {}, []
        self.expired_imports = set()
        self.split_waiting = False

    def ensure_uploaded(self, path, identity):
        self.calls.append(("upload", str(path)))
        value = receipt(Path(path).read_bytes())
        assert value["id"] == identity
        return value

    def reference(self, identity, size):
        self.calls.append(("reference", identity))
        return f"https://media.example.com/objects/{identity}.mp4?signature=synthetic-private-ticket"

    def submit_job(self, payload):
        self.calls.append(("submit", copy.deepcopy(payload)))
        clean = {k: v for k, v in payload.items() if k != "url"}
        identity = sha(json.dumps(clean, sort_keys=True).encode())
        kind = payload["kind"]
        if kind == "split":
            info = self.owner.info()
            ranges = [(0, 900), (900, 1800), (1800, 2604)]
            result = {"source_id": info["sha256"], "source_media": dict(info, frame_count=2604),
                      "policy": server_batch.POLICY, "parts": [
                          dict(receipt((f"server source part {i}" * 5).encode()), index=i,
                               start_frame=start, end_frame=end, frame_count=end-start,
                               start_seconds=start/60, duration_seconds=(end-start)/60)
                          for i, (start, end) in enumerate(ranges, 1)]}
        elif kind == "import":
            result = dict(receipt((payload["task_id"] * 10).encode()), media=self.owner.media)
        else:
            result = {"output": receipt(self.owner.final_bytes), "technical_review": {
                "status": self.owner.server_technical_status,
                "checks": {"duration_preserved": self.owner.server_technical_status == "passed"},
                "output_media": self.owner.media, "source_audio_reused": True}}
        job = {"id": identity, "kind": kind, "status": "succeeded", "result": result}
        self.jobs[identity] = copy.deepcopy(job)
        if kind == "split" and self.split_waiting:
            return {"id": identity, "kind": kind, "status": "running"}
        return copy.deepcopy(job)

    def get_job(self, identity):
        self.calls.append(("get", identity))
        job = self.jobs[identity]
        if job["kind"] == "split" and self.split_waiting:
            return {"id": identity, "kind": "split", "status": "running"}
        if identity in self.expired_imports:
            self.expired_imports.remove(identity)
            return {"id": identity, "kind": "import", "status": "failed", "error": "result_expired"}
        return copy.deepcopy(job)


class FakeCloud:
    def __init__(self, owner):
        self.owner = owner
        self.submits, self.polls, self.downloads = [], [], []
        self.running_task = None
        self.failed_task = None
        self.unknown_submit = False
        self.submit_error = None
        self.on_create = None

    def create_edit(self, reference, prompt, **kwargs):
        if self.on_create:
            self.on_create(reference, prompt, kwargs)
        self.submits.append((reference, prompt, kwargs))
        if self.submit_error:
            raise self.submit_error
        if self.unknown_submit:
            raise SeedanceError("network", "提交响应中断。", submission_uncertain=True,
                               failure_stage="response_headers", exception_type="TimeoutError",
                               completed_body_bytes=16899, request_id="synthetic-request-17")
        return "cgt-synthetic-" + str(len(self.submits))

    def get_task(self, identity):
        self.polls.append(identity)
        number = int(identity.rsplit("-", 1)[1])
        if self.failed_task == number:
            return {"id": identity, "status": "failed",
                    "error_code": "OutputVideoSensitiveContentDetected.PolicyViolation",
                    "message": "平台版权策略拒绝，原任务保留。"}
        if self.running_task == number:
            return {"id": identity, "status": "running"}
        return {"id": identity, "status": "succeeded", "video_url":
                "https://cloud.example.com/" + identity + ".mp4?signature=synthetic-cloud-secret"}

    def download(self, url, output):
        self.downloads.append((url, str(output)))
        Path(output).write_bytes(self.owner.final_bytes)


class ServerBatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"synthetic source, not a user video" * 5)
        self.final_bytes = b"synthetic assembled server video" * 20
        self.media = {"durationMs": 43400, "width": 1920, "height": 1080, "fps": 60,
                      "fpsRatio": "60/1", "hasAudio": True, "videoCodec": "h264", "audioCodec": "aac"}
        self.connection = ({"provider": "seedance", "model": config.SEEDANCE_20_MODEL,
                            "base_url": "https://ark.example.com/api/v3", "model_family": config.SEEDANCE_20_FAMILY},
                           "synthetic-ark-test-key", {"ffmpeg": "unused", "ffprobe": "unused", "font": "unused"})
        self.backend = {"provider": "self_hosted", "endpoint": "https://media.example.com/codex-media/v1"}
        self.server_technical_status = "passed"
        self.transport = FakeMedia(self)
        self.cloud = FakeCloud(self)
        self.patch("video_localizer.batch._connection", return_value=self.connection)
        self.patch("video_localizer.batch.app_dir", return_value=self.root / "app")
        self.patch("video_localizer.selfhost_media.configuration", return_value=self.backend)
        self.patch("video_localizer.seedance_server_batch.MediaClient", wraps=server_batch.MediaClient, side_effect=lambda *a, **k: self.transport)
        self.patch("video_localizer.seedance_server_batch.SeedanceClient", return_value=self.cloud)
        self.patch("video_localizer.seedance_batch.engine.probe", side_effect=lambda path, *a, **k: dict(self.media, bytes=Path(path).stat().st_size))
        self.review = self.patch("video_localizer.seedance_batch.technical_review", side_effect=self.technical)
        self.patch("video_localizer.seedance_batch._wait")
        self.patch("video_localizer.seedance_segments.prepare_segments", side_effect=AssertionError("No local cuts"))
        self.patch("video_localizer.seedance_segments.join_candidates", side_effect=AssertionError("No local join"))

    def patch(self, target, **kwargs):
        p = patch(target, **kwargs)
        value = p.start()
        self.addCleanup(p.stop)
        return value

    def info(self):
        return dict(self.media, bytes=self.source.stat().st_size, sha256=sha(self.source.read_bytes()))

    def technical(self, source, candidate, options, info):
        self.assertEqual(Path(source), self.source)
        self.assertEqual(Path(candidate).read_bytes(), self.final_bytes)
        return {"status": "passed", "checks": {"full_decode": True, "source_unchanged": True,
                "duration_preserved": True, "original_audio_preserved": True},
                "output_media": dict(self.media, bytes=len(self.final_bytes)), "output_sha256": sha(self.final_bytes),
                "visual_acceptance": "not_performed", "model_reviewed": False}

    def convert(self, **kwargs):
        return batch.convert([self.source], self.root / "out", request="Original human request", **kwargs)

    def stored(self, result):
        return json.loads(Path(result["report"]).read_text(encoding="utf-8"))

    def test_three_server_segments_only_one_final_download_and_no_local_processing(self):
        result = self.convert()
        self.assertEqual(result["status"], "needs_review")
        saved = self.stored(result)
        self.assertEqual(saved["processing_backend"], "server-v1")
        self.assertEqual(len(self.cloud.submits), 3)
        self.assertEqual(len(self.cloud.downloads), 1)
        self.assertIn(sha(self.final_bytes), self.cloud.downloads[0][0])
        self.assertEqual([call[1]["kind"] for call in self.transport.calls if call[0] == "submit"],
                         ["split", "import", "import", "import", "join"])
        self.assertEqual([r["duration_seconds"] for r in saved["videos"][0]["segment_tasks"]], [15, 15, 13.4])
        for _url, prompt, opts in self.cloud.submits:
            self.assertFalse(opts["generate_audio"])
            self.assertIn("Original human request", prompt)
        serialized = json.dumps(saved)
        self.assertNotIn("synthetic-cloud-secret", serialized)
        self.assertNotIn("synthetic-private-ticket", serialized)
        self.assertNotIn("synthetic-ark-test-key", serialized)
        self.assertEqual(list(Path(result["directory"]).rglob("*.mp4")), [Path(result["videos"][0]["preview"])])
        self.assertFalse(result["human_approved"])

    def test_pause_resume_keeps_cloud_ids_and_does_not_redownload_segments(self):
        self.cloud.running_task = 2
        first = self.convert(max_turns=1)
        self.assertEqual(first["status"], "paused_budget")
        self.assertEqual(len(self.cloud.submits), 2)
        self.assertEqual(self.cloud.downloads, [])
        self.cloud.running_task = None
        second = batch.resume(first["directory"], max_turns=20)
        self.assertEqual(second["status"], "needs_review")
        self.assertEqual(len(self.cloud.submits), 3)
        self.assertEqual(len(self.cloud.downloads), 1)
        self.assertEqual(self.cloud.polls.count("cgt-synthetic-1"), 1)
        attempts = [part["submission_attempts"] for part in self.stored(second)["videos"][0]["segment_tasks"]]
        self.assertEqual([len(items) for items in attempts], [1, 1, 1])
        self.assertEqual([items[0]["task_id"] for items in attempts],
                         ["cgt-synthetic-1", "cgt-synthetic-2", "cgt-synthetic-3"])

    def test_expired_import_fetches_new_url_for_same_cloud_task(self):
        self.cloud.running_task = 2
        first = self.convert(max_turns=1)
        saved = self.stored(first)
        self.transport.expired_imports.add(saved["videos"][0]["segment_tasks"][0]["server_import"]["id"])
        self.cloud.running_task = None
        second = batch.resume(first["directory"], max_turns=20)
        self.assertEqual(second["status"], "needs_review")
        self.assertEqual(len(self.cloud.submits), 3)
        self.assertEqual(self.cloud.polls.count("cgt-synthetic-1"), 2)
        self.assertEqual(len([c for c in self.transport.calls if c[0] == "submit" and c[1].get("task_id") == "cgt-synthetic-1"]), 2)

    def test_policy_failure_stops_remaining_segments_and_resume_does_not_resubmit(self):
        self.cloud.failed_task = 1
        first = self.convert()
        second = batch.resume(first["directory"])
        for result in (first, second):
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["videos"][0]["remote_error_code"], "OutputVideoSensitiveContentDetected.PolicyViolation")
            self.assertEqual(result["candidates"], [])
        self.assertEqual(len(self.cloud.submits), 1)
        self.assertEqual(self.cloud.downloads, [])
        self.assertFalse(any(c[0] == "submit" and c[1]["kind"] in {"import", "join"} for c in self.transport.calls))

    def test_unknown_generation_response_is_not_automatically_resubmitted(self):
        self.cloud.unknown_submit = True
        first = self.convert()
        self.assertEqual(first["status"], "paused_error")
        original = self.stored(first)["videos"][0]
        diagnostic = {"error_code": "network", "operation": "segment_submitting", "http_status": None,
                      "submission_uncertain": True, "failure_stage": "response_headers",
                      "exception_type": "TimeoutError", "completed_body_bytes": 16899,
                      "request_id": "synthetic-request-17"}
        self.assertEqual(original["last_transport_error"], diagnostic)
        part = original["segment_tasks"][0]
        self.assertEqual(part["last_transport_error"], diagnostic)
        self.assertEqual(part["submission_attempts"][0]["status"], "unknown")
        self.assertEqual(part["submission_attempts"][0]["transport_error"], diagnostic)
        self.cloud.unknown_submit = False
        second = batch.resume(first["directory"])
        self.assertEqual(second["videos"][0]["error_code"], "submission_uncertain")
        self.assertEqual(len(self.cloud.submits), 1)
        resumed = self.stored(second)["videos"][0]
        self.assertEqual(resumed["last_transport_error"], diagnostic)
        self.assertEqual(resumed["segment_tasks"][0]["last_transport_error"], diagnostic)
        self.assertEqual(resumed["segment_tasks"][0]["submission_attempts"], part["submission_attempts"])

    def test_correlation_id_and_intent_are_saved_before_each_post(self):
        observed_ids = []

        def check_saved(_reference, prompt, kwargs):
            report_path, = (self.root / "out").glob("batch-*/batch.json")
            saved = json.loads(report_path.read_text(encoding="utf-8"))
            part = saved["videos"][0]["segment_tasks"][len(observed_ids)]
            attempt, = part["submission_attempts"]
            self.assertEqual(part["submission_state"], "submitting")
            self.assertNotIn("task_id", part)
            self.assertEqual(attempt["status"], "submitting")
            self.assertEqual(attempt["client_request_id"], kwargs["client_request_id"])
            self.assertRegex(attempt["client_request_id"], r"^vl-[a-f0-9]{32}$")
            self.assertEqual(attempt["prompt_fingerprint"], sha(prompt.encode("utf-8")))
            self.assertGreater(attempt["started_at"], 0)
            observed_ids.append(attempt["client_request_id"])

        self.cloud.on_create = check_saved
        result = self.convert()
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(len(set(observed_ids)), 3)
        serialized = json.dumps(self.stored(result))
        self.assertNotIn("synthetic-private-ticket", serialized)
        self.assertNotIn("synthetic-cloud-secret", serialized)
        self.assertNotIn(self.connection[1], serialized)

    def test_existing_task_ids_without_attempt_history_resume_without_new_posts(self):
        self.cloud.running_task = 2
        first = self.convert(max_turns=1)
        saved = self.stored(first)
        for part in saved["videos"][0]["segment_tasks"]:
            part.pop("submission_attempts", None)
        Path(first["report"]).write_text(json.dumps(saved), encoding="utf-8")
        self.cloud.running_task = None
        second = batch.resume(first["directory"], max_turns=20)
        self.assertEqual(second["status"], "needs_review")
        self.assertEqual(len(self.cloud.submits), 3)
        parts = self.stored(second)["videos"][0]["segment_tasks"]
        self.assertNotIn("submission_attempts", parts[0])
        self.assertNotIn("submission_attempts", parts[1])
        self.assertEqual(len(parts[2]["submission_attempts"]), 1)

    def test_transport_diagnostics_exclude_credentials_and_response_payload(self):
        self.cloud.submit_error = SeedanceError("network", "提交响应中断。", submission_uncertain=True,
            failure_stage="response_headers", exception_type="TimeoutError", completed_body_bytes=17,
            request_id=self.connection[1])
        self.cloud.submit_error.payload = {"url": "https://cloud.example.com?signature=do-not-save",
                                           "api_key": self.connection[1]}
        result = self.convert()
        saved = self.stored(result)
        diagnostic = saved["videos"][0]["last_transport_error"]
        self.assertEqual(diagnostic["failure_stage"], "response_headers")
        self.assertNotIn("request_id", diagnostic)
        serialized = json.dumps(saved)
        self.assertNotIn(self.connection[1], serialized)
        self.assertNotIn("do-not-save", serialized)

    def test_definite_http_rejection_records_attempt_and_safe_provider_code(self):
        self.cloud.submit_error = SeedanceError("http_error", "Seedance 接口返回 HTTP 400。", http_status=400,
            submission_uncertain=False, provider_code="InvalidParameter", failure_stage="response_headers",
            completed_body_bytes=400, request_id="synthetic-request-400")
        result = self.convert()
        row = self.stored(result)["videos"][0]
        part = row["segment_tasks"][0]
        self.assertEqual(part["submission_state"], "rejected")
        self.assertEqual(part["submission_attempts"][0]["status"], "rejected")
        self.assertEqual(row["last_transport_error"]["http_status"], 400)
        self.assertEqual(row["last_transport_error"]["provider_code"], "InvalidParameter")
        self.assertEqual(len(self.cloud.submits), 1)

    def test_server_poll_budget_preserves_job_and_submits_no_cloud_tasks(self):
        self.transport.split_waiting = True
        first = self.convert(max_turns=1)
        self.assertEqual(first["status"], "paused_budget")
        self.assertEqual(self.cloud.submits, [])
        identity = self.stored(first)["videos"][0]["server_split"]["id"]
        self.transport.split_waiting = False
        second = batch.resume(first["directory"], max_turns=20)
        self.assertEqual(second["status"], "needs_review")
        self.assertEqual(self.stored(second)["videos"][0]["server_split"]["id"], identity)

    def test_server_technical_failure_cannot_be_overridden_by_local_pass(self):
        self.server_technical_status = "failed"
        result = self.convert()
        self.assertEqual(result["videos"][0]["technical_review"]["status"], "failed")
        with self.assertRaisesRegex(ValueError, "技术属性"):
            batch.submit_review(result["directory"], 1, seedance_batch.REVIEW_ID, {"action": "keep"})
        self.assertFalse(self.stored(result)["human_approved"])

    def test_changed_source_blocks_resume_before_new_generation(self):
        self.cloud.running_task = 1
        first = self.convert(max_turns=1)
        self.source.write_bytes(b"different source content" * 10)
        result = batch.resume(first["directory"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(self.cloud.submits), 1)


if __name__ == "__main__":
    unittest.main()
