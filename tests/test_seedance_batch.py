"""Seedance batch state transitions using synthetic bytes and a mocked API only."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from video_localizer import batch, config, seedance_batch, seedance_client


class SeedanceBatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.sources = []
        for index in range(3):
            source = self.root / f"source-{index}.mp4"
            source.write_bytes(f"synthetic source {index}".encode())
            self.sources.append(source)
        self.events = []
        self.connection = (
            {"provider": "seedance", "model": config.SEEDANCE_25_MODEL,
             "base_url": "https://example.invalid/api/v3"},
            "synthetic-test-key",
            {"ffmpeg": "unused-ffmpeg", "ffprobe": "unused-ffprobe", "font": "unused-font"},
        )
        self.media = {"width": 720, "height": 1280, "durationMs": 20000,
                      "fps": 30, "fpsRatio": "30/1", "hasAudio": True,
                      "videoCodec": "h264", "audioCodec": "aac",
                      "constantFrameRate": True}
        self.client = MagicMock(spec=seedance_client.SeedanceClient)
        self.client.upload_video.side_effect = self.upload
        self.client.get_file.side_effect = self.get_file
        self.client.create_edit.side_effect = self.create_edit
        self.client.get_task.side_effect = self.get_task
        self.client.download.side_effect = self.download
        self.factory = self.patch("video_localizer.seedance_client.SeedanceClient", return_value=self.client)
        self.patch("video_localizer.batch._connection", return_value=self.connection)
        self.patch("video_localizer.batch.app_dir", return_value=self.root / "app")
        self.patch("video_localizer.batch.read_key", side_effect=AssertionError("No credential reads in tests"))
        self.hunyuan = self.patch("video_localizer.hunyuan.HunyuanClient",
                                  side_effect=AssertionError("Seedance must not call Hy4"))
        self.controller = self.patch("video_localizer.controller.HunyuanController",
                                     side_effect=AssertionError("Seedance must not start the Hy4 controller"))
        self.executor = self.patch("video_localizer.executor.VideoExecutor",
                                   side_effect=AssertionError("No local video editing in Seedance"))
        self.probe = self.patch("video_localizer.seedance_batch.engine.probe", side_effect=self.probe_media)
        self.command = self.patch("video_localizer.seedance_batch.engine.command", side_effect=self.media_command)
        self.patch("video_localizer.seedance_batch._wait")
        original_review = seedance_batch.technical_review

        def review(source, candidate, options, info):
            self.events.append(("review", Path(source).name))
            return original_review(source, candidate, options, info)

        self.review = self.patch("video_localizer.seedance_batch.technical_review", side_effect=review)

    def patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def probe_media(self, path, _options, **_kwargs):
        return dict(self.media, bytes=Path(path).stat().st_size)

    @staticmethod
    def media_command(arguments, **_kwargs):
        if "-show_entries" in arguments:
            return json.dumps({"streams": [
                {"codec_type": "video", "start_time": "0.0"},
                {"codec_type": "audio", "start_time": "0.0"},
            ]}).encode()
        return ("SHA256=" + "a" * 64).encode() if "hash" in arguments else b""

    def upload(self, source):
        self.events.append(("upload", Path(source).name))
        return {"file_id": f"file-{self.client.upload_video.call_count}", "status": "processing"}

    def get_file(self, file_id):
        self.events.append(("file", file_id))
        return {"file_id": file_id, "status": "active",
                "video_reference": "https://media.example.invalid/source.mp4"}

    def create_edit(self, _reference, _prompt, **_kwargs):
        task_id = f"cgt-{self.client.create_edit.call_count}"
        self.events.append(("submit", task_id))
        return task_id

    def get_task(self, task_id):
        self.events.append(("poll", task_id))
        return {"id": task_id, "status": "succeeded",
                "video_url": "https://media.example.invalid/result.mp4"}

    def download(self, _url, output):
        self.events.append(("download", str(output)))
        Path(output).write_bytes(b"synthetic generated candidate")
        return {"path": str(output), "bytes": Path(output).stat().st_size,
                "sha256": hashlib.sha256(Path(output).read_bytes()).hexdigest()}

    def convert(self, count=1, **kwargs):
        return batch.convert(self.sources[:count], self.root / "outputs",
                             request="Synthetic user localization request", **kwargs)

    @staticmethod
    def stored(result):
        return json.loads(Path(result["report"]).read_text(encoding="utf-8"))

    def pause_running_task(self, **kwargs):
        self.client.get_task.side_effect = lambda task_id: {"id": task_id, "status": "running"}
        result = self.convert(max_turns=1, **kwargs)
        self.assertEqual(result["status"], "paused_budget")
        self.assertEqual(result["videos"][0]["task_id"], "cgt-1")
        return result

    def test_upload_submit_poll_download_technical_review_remains_unapproved(self):
        result = self.convert()
        self.assertEqual([name for name, _value in self.events],
                         ["upload", "file", "submit", "poll", "download", "review"])
        self.hunyuan.assert_not_called()
        self.controller.assert_not_called()
        self.executor.assert_not_called()
        row = result["videos"][0]
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(row["status"], "needs_review")
        self.assertIsNone(row["output"])
        self.assertTrue(Path(row["preview"]).is_file())
        self.assertFalse(row["human_approved"])
        self.assertFalse(row["model_reviewed"])
        self.assertFalse(result["human_approved"])
        self.assertEqual(result["outputs"], [])
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["pending_reviews"], 1)
        self.assertEqual(row["technical_review"]["status"], "passed")
        self.assertEqual(row["technical_review"]["visual_acceptance"], "not_performed")
        self.assertEqual(self.stored(result)["schema"], 3)
        prompt = self.client.create_edit.call_args.args[1]
        self.assertIn("Synthetic user localization request", prompt)
        self.assertIn("韩语", prompt)
        self.assertEqual(self.client.create_edit.call_args.kwargs["reference_images"], [])
        for path in Path(result["directory"]).rglob("*.json"):
            self.assertNotIn("synthetic-test-key", path.read_text(encoding="utf-8"))

    def test_endpoint_25_retains_full_video_workflow_and_pins_family(self):
        endpoint_id = "ep-20261126155948-example"
        self.connection[0].update(model=endpoint_id, model_family=config.SEEDANCE_25_FAMILY)
        result = self.convert()
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["model"], endpoint_id)
        self.assertEqual(self.stored(result)["model_family"], config.SEEDANCE_25_FAMILY)
        self.assertEqual(self.factory.call_args.args[2], endpoint_id)
        self.assertEqual(self.factory.call_args.kwargs["model_family"], config.SEEDANCE_25_FAMILY)
        self.assertEqual(self.client.upload_video.call_count, 1)

    def test_endpoint_missing_family_is_rejected_before_upload_or_batch_creation(self):
        self.connection[0]["model"] = "ep-20261126155948-example"
        with self.assertRaises(config.SettingsError):
            self.convert()
        self.factory.assert_not_called()
        self.client.upload_video.assert_not_called()
        self.assertFalse((self.root / "outputs").exists())

    def test_endpoint_family_change_refuses_existing_task_without_new_network_calls(self):
        self.connection[0].update(model="ep-20261126155948-example", model_family=config.SEEDANCE_25_FAMILY)
        first = self.pause_running_task()
        calls = list(self.client.mock_calls)
        self.connection[0]["model_family"] = config.SEEDANCE_20_FAMILY
        with self.assertRaises(config.SettingsError):
            batch.resume(first["directory"])
        self.assertEqual(self.client.mock_calls, calls)

    def test_legacy_direct_model_checkpoint_without_family_still_resumes(self):
        first = self.pause_running_task()
        stored = self.stored(first)
        del stored["model_family"]
        config.write_json(first["report"], stored)
        self.client.get_task.side_effect = self.get_task
        second = batch.resume(first["directory"])
        self.assertEqual(second["status"], "needs_review")
        self.assertEqual(self.client.create_edit.call_count, 1)

    def test_poll_budget_resume_only_polls_the_saved_task(self):
        first = self.pause_running_task()
        self.client.get_task.side_effect = self.get_task
        second = batch.resume(first["directory"], max_turns=3)
        self.assertEqual(second["status"], "needs_review")
        self.assertEqual(second["videos"][0]["task_id"], "cgt-1")
        self.assertEqual(self.client.upload_video.call_count, 1)
        self.assertEqual(self.client.get_file.call_count, 1)
        self.assertEqual(self.client.create_edit.call_count, 1)
        self.assertEqual({call.args[0] for call in self.client.get_task.call_args_list}, {"cgt-1"})

    def test_cancel_after_submission_is_resumable_without_resubmission(self):
        cancellation = {"requested": False}

        def running(task_id):
            cancellation["requested"] = True
            return {"id": task_id, "status": "running"}

        self.client.get_task.side_effect = running
        first = self.convert(cancelled=lambda: cancellation["requested"])
        self.assertEqual(first["status"], "cancelled")
        self.assertEqual(first["videos"][0]["task_id"], "cgt-1")
        self.client.get_task.side_effect = self.get_task
        second = batch.resume(first["directory"])
        self.assertEqual(second["status"], "needs_review")
        self.assertEqual(self.client.create_edit.call_count, 1)
        self.assertEqual(self.client.upload_video.call_count, 1)

    def test_cancel_before_upload_leaves_all_sources_unprocessed_then_resumes(self):
        first = self.convert(count=2, cancelled=lambda: True)
        self.assertEqual(first["status"], "cancelled")
        self.assertEqual(first["unprocessed"], 2)
        self.client.upload_video.assert_not_called()
        self.client.create_edit.assert_not_called()
        second = batch.resume(first["directory"])
        self.assertEqual(second["needs_review"], 2)
        self.assertEqual(self.client.create_edit.call_count, 2)

    def test_uncertain_submission_without_task_id_never_repeats_post(self):
        self.client.create_edit.side_effect = seedance_client.SeedanceError(
            "transport_error", "synthetic interrupted response", submission_uncertain=True)
        first = self.convert()
        self.assertEqual(first["status"], "paused_error")
        row = self.stored(first)["videos"][0]
        self.assertEqual(row["submission_state"], "submitting")
        self.assertFalse(row.get("task_id"))
        self.client.create_edit.side_effect = self.create_edit
        second = batch.resume(first["directory"])
        self.assertEqual(second["status"], "paused_error")
        self.assertEqual(second["videos"][0]["error_code"], "submission_uncertain")
        self.assertEqual(self.client.create_edit.call_count, 1)
        self.assertEqual(self.client.upload_video.call_count, 1)
        self.client.get_task.assert_not_called()

    def test_upload_processing_budget_resumes_existing_file(self):
        self.client.get_file.side_effect = lambda file_id: {"file_id": file_id, "status": "processing"}
        first = self.convert(max_turns=1)
        self.assertEqual(first["status"], "paused_budget")
        self.client.create_edit.assert_not_called()
        self.client.get_file.side_effect = self.get_file
        second = batch.resume(first["directory"])
        self.assertEqual(second["status"], "needs_review")
        self.assertEqual(self.client.upload_video.call_count, 1)
        self.assertEqual(self.client.create_edit.call_count, 1)

    def test_last_upload_diagnostics_survive_blocked_resume_and_are_public(self):
        self.client.upload_video.side_effect = seedance_client.SeedanceError(
            "network", "synthetic send failure", submission_uncertain=True,
            failure_stage="send", exception_type="BrokenPipeError",
            completed_body_bytes=4096, request_id="req-synthetic")
        first = self.convert()
        diagnostic = first["videos"][0]["last_transport_error"]
        self.assertEqual(diagnostic["operation"], "uploading")
        self.assertEqual(diagnostic["failure_stage"], "send")
        self.assertEqual(diagnostic["completed_body_bytes"], 4096)
        self.assertEqual(diagnostic["request_id"], "req-synthetic")
        self.assertTrue(diagnostic["submission_uncertain"])
        self.assertEqual(self.stored(first)["videos"][0]["last_transport_error"], diagnostic)
        second = batch.resume(first["directory"])
        self.assertEqual(second["videos"][0]["error_code"], "upload_uncertain")
        self.assertEqual(second["videos"][0]["last_transport_error"], diagnostic)
        self.assertEqual(self.client.upload_video.call_count, 1)
        self.client.create_edit.assert_not_called()

    def test_failed_uploaded_file_with_stale_url_never_submits_generation(self):
        self.client.get_file.side_effect = lambda file_id: {
            "file_id": file_id, "status": "failed",
            "video_reference": "https://media.example.invalid/stale-source.mp4"}
        result = self.convert()
        self.assertEqual(result["status"], "paused_error")
        self.assertEqual(result["videos"][0]["error_code"], "upload_failed")
        self.client.create_edit.assert_not_called()
        self.client.get_task.assert_not_called()

    def test_changed_source_refuses_saved_task_before_any_new_api_call(self):
        first = self.pause_running_task()
        before = list(self.client.mock_calls)
        self.sources[0].write_bytes(b"changed synthetic source")
        second = batch.resume(first["directory"])
        self.assertEqual(second["status"], "failed")
        self.assertEqual(second["videos"][0]["error_code"], "local_validation")
        self.assertIn("原视频已变化", second["videos"][0]["message"])
        self.assertEqual(self.client.mock_calls, before)

    def test_changed_user_materials_refuse_resume_before_any_new_api_call(self):
        glossary = self.root / "user-glossary.csv"
        glossary.write_text("source,target\nSOURCE,USER VALUE\n", encoding="utf-8")
        first = self.pause_running_task(glossary=glossary)
        before = list(self.client.mock_calls)
        factories_before = self.factory.call_count
        glossary.write_text("source,target\nSOURCE,CHANGED USER VALUE\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "资料已变化"):
            batch.resume(first["directory"])
        self.assertEqual(self.client.mock_calls, before)
        self.assertEqual(self.factory.call_count, factories_before)

    def test_legacy_hy4_schema_two_is_rejected_with_seedance_active(self):
        folder = self.root / "legacy"
        folder.mkdir()
        report = {
            "schema": 2, "directory": str(folder), "model": "hy4",
            "endpoint": "https://legacy.example.invalid/v1", "status": "paused_error",
            "params": {"target": "韩语", "source_language": "auto", "profile": "source",
                       "translations": None, "glossary": None, "references": None,
                       "request": "Synthetic legacy request", "max_turns": 80},
            "videos": [{"index": 1, "source": str(self.sources[0]), "status": "paused_error",
                        "work_dir": str(folder / "videos" / "001"), "output": None, "issues": []}],
        }
        (folder / "batch.json").write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaisesRegex(config.SettingsError, "旧混元断点"):
            batch.resume(folder)
        self.factory.assert_not_called()
        self.hunyuan.assert_not_called()
        self.controller.assert_not_called()

    def test_pending_review_does_not_block_next_video_and_execution_is_serial(self):
        result = self.convert(count=3)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["needs_review"], 3)
        self.assertEqual(result["unprocessed"], 0)
        self.assertEqual([event[0] for event in self.events],
                         ["upload", "file", "submit", "poll", "download", "review"] * 3)
        self.assertEqual([value for name, value in self.events if name == "review"],
                         [source.name for source in self.sources])
        self.assertEqual(self.client.create_edit.call_count, 3)
        before = list(self.client.mock_calls)
        resumed = batch.resume(result["directory"])
        self.assertEqual(resumed["needs_review"], 3)
        self.assertEqual(self.client.mock_calls, before)

    def test_unsupported_2k120_rejected_before_upload_or_output_creation(self):
        with self.assertRaisesRegex(ValueError, "2K/120"):
            self.convert(profile="2k120")
        self.factory.assert_not_called()
        self.client.upload_video.assert_not_called()
        self.probe.assert_not_called()
        self.assertFalse((self.root / "outputs").exists())

    def test_finished_download_is_revalidated_without_network_on_resume(self):
        original_side_effect = self.review.side_effect
        self.review.side_effect = ValueError("synthetic local probe interruption")
        first = self.convert()
        self.assertEqual(first["status"], "failed")
        self.assertTrue(self.stored(first)["videos"][0]["download_sha256"])
        before = list(self.client.mock_calls)
        self.review.side_effect = original_side_effect
        second = batch.resume(first["directory"])
        self.assertEqual(second["status"], "needs_review")
        self.assertEqual(self.client.mock_calls, before)

    def test_technical_attribute_failure_stays_reviewable_and_cannot_be_accepted(self):
        def wrong_dimensions(path, options, **kwargs):
            media = self.probe_media(path, options, **kwargs)
            if Path(path).name == "seedance-candidate.mp4":
                media["width"] = 704
            return media

        self.probe.side_effect = wrong_dimensions
        result = self.convert()
        row = result["videos"][0]
        self.assertEqual(row["status"], "needs_review")
        self.assertEqual(row["technical_review"]["status"], "failed")
        self.assertFalse(row["technical_review"]["checks"]["dimensions_preserved"])
        self.assertFalse(result["human_approved"])
        self.assertEqual(result["outputs"], [])
        with self.assertRaisesRegex(ValueError, "技术属性"):
            batch.submit_review(result["directory"], 1, seedance_batch.REVIEW_ID, {"action": "keep"})

    def test_only_explicit_human_acceptance_promotes_a_technically_valid_candidate(self):
        result = self.convert()
        before = list(self.client.mock_calls)
        decision = batch.submit_review(result["directory"], 1, seedance_batch.REVIEW_ID,
                                       {"action": "keep", "note": "Synthetic explicit human acceptance"})
        saved = self.stored(result)
        self.assertEqual(decision["status"], "resolved")
        self.assertEqual(saved["status"], "completed")
        self.assertTrue(saved["human_approved"])
        self.assertTrue(saved["videos"][0]["human_approved"])
        self.assertFalse(saved["videos"][0]["model_reviewed"])
        self.assertEqual(saved["outputs"], [saved["videos"][0]["preview"]])
        self.assertEqual(self.client.mock_calls, before)

    def test_human_rejection_remains_rejected_on_resume_without_new_requests(self):
        first = self.convert()
        decision = {"action": "reject", "note": "Synthetic human rejection: incorrect visible text"}
        batch.submit_review(first["directory"], 1, seedance_batch.REVIEW_ID, decision)
        rejected = self.stored(first)["videos"][0]
        self.assertTrue(rejected["human_rejected"])
        self.assertEqual(rejected["status"], "failed")
        self.assertEqual(rejected["issues"][0]["decision"], decision)
        calls_before = list(self.client.mock_calls)
        reviews_before = self.review.call_count
        resumed = batch.resume(first["directory"])
        self.assertEqual(resumed["status"], "failed")
        self.assertEqual(resumed["outputs"], [])
        self.assertFalse(resumed["human_approved"])
        self.assertEqual(self.stored(first)["videos"][0], rejected)
        self.assertEqual(self.client.mock_calls, calls_before)
        self.assertEqual(self.review.call_count, reviews_before)


@unittest.skipUnless(all(os.environ.get(name) for name in
                        ("VIDEO_LOCALIZER_TEST_FFMPEG", "VIDEO_LOCALIZER_TEST_FFPROBE")),
                     "Set VIDEO_LOCALIZER_TEST_FFMPEG/FFPROBE for synthetic media validation")
class SeedanceTechnicalMediaTests(unittest.TestCase):
    def test_actual_ffmpeg_detects_fps_change_without_claiming_visual_acceptance(self):
        engine = seedance_batch.engine
        options = {"ffmpeg": os.environ["VIDEO_LOCALIZER_TEST_FFMPEG"],
                   "ffprobe": os.environ["VIDEO_LOCALIZER_TEST_FFPROBE"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "synthetic-original.mp4"
            identical = root / "identical-candidate.mp4"
            changed_fps = root / "changed-fps-candidate.mp4"
            shifted_audio = root / "shifted-audio-candidate.mp4"
            engine.command([
                options["ffmpeg"], "-hide_banner", "-loglevel", "error", "-nostdin",
                "-f", "lavfi", "-i", "color=c=black:s=720x1280:r=30",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-t", "4", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "96k", "-shortest", str(source),
            ], timeout=90)
            source_info = seedance_batch.source_metadata(source, options)
            self.assertEqual((source_info["width"], source_info["height"], source_info["fps"]),
                             (720, 1280, 30))
            self.assertTrue(source_info["hasAudio"])
            shutil.copyfile(source, identical)
            passing = seedance_batch.technical_review(source, identical, options, source_info)
            self.assertEqual(passing["status"], "passed")
            self.assertTrue(all(passing["checks"].values()))
            self.assertFalse(passing["model_reviewed"])
            self.assertEqual(passing["visual_acceptance"], "not_performed")

            engine.command(engine.ffinput(options["ffmpeg"], source) + [
                "-vf", "fps=24", "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-c:a", "copy", str(changed_fps),
            ], timeout=90)
            failing = seedance_batch.technical_review(source, changed_fps, options, source_info)
            self.assertEqual(failing["status"], "failed")
            self.assertFalse(failing["checks"]["frame_rate_preserved"])
            self.assertTrue(failing["checks"]["full_decode"])
            self.assertTrue(failing["checks"]["source_unchanged"])
            self.assertTrue(failing["checks"]["original_audio_preserved"])
            self.assertFalse(failing["model_reviewed"])
            self.assertEqual(failing["visual_acceptance"], "not_performed")

            engine.command(engine.ffinput(options["ffmpeg"], source) + [
                "-itsoffset", "0.2", "-i", str(source), "-map", "0:v:0", "-map", "1:a:0",
                "-c", "copy", str(shifted_audio),
            ], timeout=90)
            # Packet identity alone is insufficient: the copied audio starts late.
            self.assertEqual(engine.audio_hash(source, options), engine.audio_hash(shifted_audio, options))
            shifted = seedance_batch.technical_review(source, shifted_audio, options, source_info)
            self.assertEqual(shifted["status"], "failed")
            self.assertFalse(shifted["checks"]["original_audio_preserved"])
            self.assertTrue(shifted["checks"]["full_decode"])
            self.assertTrue(shifted["checks"]["frame_rate_preserved"])
            self.assertTrue(shifted["checks"]["audio_presence_preserved"])
            self.assertEqual(shifted["visual_acceptance"], "not_performed")


if __name__ == "__main__":
    unittest.main()
