"""MediaKit configuration and checkpoint safety, without network or real keys."""

from contextlib import nullcontext
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from video_localizer import config, mediakit_batch as batch, mediakit_config
from video_localizer.mediakit_client import MediaKitClient
from video_localizer.seedance_client import SeedanceError
from video_localizer.selfhost_media import MediaClient


class PatchCase(unittest.TestCase):
    def patch(self, target, **kwargs):
        handle = patch(target, **kwargs)
        result = handle.start()
        self.addCleanup(handle.stop)
        return result

    def temporary_root(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name).resolve()


class MediaKitConfigurationTests(PatchCase):
    def setUp(self):
        self.root = self.temporary_root()
        self.patch("video_localizer.config.app_dir", return_value=self.root)
        self.resolve = self.patch("video_localizer.config._resolved_key",
                                  return_value=("synthetic-mediakit-secret", True))
        self.store = self.patch("video_localizer.config._store_key")
        self.read = self.patch("video_localizer.config.read_key",
                               side_effect=config.SettingsError("synthetic unavailable credential"))

    def test_save_uses_independent_credential_and_contains_no_key(self):
        result = mediakit_config.save_configuration("synthetic-mediakit-secret")
        self.resolve.assert_called_once_with(mediakit_config.ENDPOINT, "synthetic-mediakit-secret")
        self.store.assert_called_once_with(mediakit_config.ENDPOINT, "synthetic-mediakit-secret")
        self.assertFalse(result["connection_tested"])
        saved = (self.root / "mediakit.json").read_text(encoding="utf-8")
        self.assertNotIn("synthetic-mediakit-secret", saved)
        self.assertEqual(mediakit_config.settings(), {"provider": "mediakit",
            "base_url": mediakit_config.ENDPOINT, "model_version": "v5.6"})
        self.assertEqual([path.name for path in self.root.iterdir()], ["mediakit.json"])
        self.read.assert_not_called()

    def test_existing_credential_is_reused_without_storing_again(self):
        self.resolve.return_value = ("synthetic-mediakit-secret", False)
        mediakit_config.save_configuration(model_version="v5")
        self.store.assert_not_called()
        self.assertEqual(mediakit_config.settings()["model_version"], "v5")

    def test_ark_key_and_unsupported_versions_do_not_write_configuration(self):
        self.resolve.return_value = ("ark-synthetic-not-real", True)
        with self.assertRaises(config.SettingsError):
            mediakit_config.save_configuration("ark-synthetic-not-real")
        with self.assertRaises(config.SettingsError):
            mediakit_config.save_configuration(model_version="unknown")
        self.store.assert_not_called()
        self.assertFalse((self.root / "mediakit.json").exists())

    def test_invalid_config_and_missing_credential_are_not_reported_connected(self):
        self.assertIsNone(mediakit_config.settings())
        self.assertFalse(mediakit_config.check_setup()["configured"])
        mediakit_config.save_configuration()
        result = mediakit_config.check_setup()
        self.assertTrue(result["configured"])
        self.assertFalse(result["credential_available"])
        self.assertFalse(result["connection_tested"])
        for value in ({"provider": "mediakit"}, [], None,
                      {"provider": "mediakit", "base_url": "https://wrong.example/api/v1", "model_version": "v5.6"}):
            (self.root / "mediakit.json").write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(config.SettingsError):
                mediakit_config.settings()

    def test_noninteractive_configuration_never_reads_a_secret(self):
        self.patch("video_localizer.mediakit_config.sys.stdin.isatty", return_value=False)
        prompt = self.patch("video_localizer.mediakit_config.getpass.getpass")
        with self.assertRaises(config.SettingsError):
            mediakit_config.configure()
        prompt.assert_not_called()


class MediaKitBatchTests(PatchCase):
    def setUp(self):
        self.root = self.temporary_root()
        self.source = self.root / "first-ten-seconds.mp4"
        self.source.write_bytes(b"synthetic source bytes without real video")
        self.refs = self.root / "approved-translations.json"
        self.translations = [{"source": "师姐，你的战力怎么提升这么快？", "target": "사저, 전투력 왜 이리 빨리 올라?"}]
        self.refs.write_text(json.dumps(self.translations, ensure_ascii=False), encoding="utf-8")
        self.output = self.root / "output"
        self.profile = {"provider": "mediakit", "base_url": mediakit_config.ENDPOINT, "model_version": "v5.6"}
        self.backend = {"provider": "self_hosted", "endpoint": "https://media.example.invalid/codex-media/v1"}
        self.profile_mock = self.patch("video_localizer.mediakit_config.settings", return_value=self.profile)
        self.backend_mock = self.patch("video_localizer.mediakit_batch.configuration", return_value=self.backend)
        self.patch("video_localizer.mediakit_batch.batch_lock", side_effect=nullcontext)
        self.patch("video_localizer.config.read_key", return_value="synthetic-mediakit-secret")
        self.patch("video_localizer.mediakit_batch.time.sleep")
        self.metadata = {"duration": 10.0, "width": 1280, "height": 720, "fps": 30.0,
                         "fps_ratio": "30/1", "frame_count": 300, "has_audio": True, "probe": {}}
        self.probe = self.patch("video_localizer.mediakit_batch._probe", return_value=self.metadata)
        self.client = MagicMock(spec=MediaKitClient)
        self.client_factory = self.patch("video_localizer.mediakit_batch.MediaKitClient", return_value=self.client)
        self.media = MagicMock(spec=MediaClient)
        self.media_factory = self.patch("video_localizer.mediakit_batch.MediaClient", return_value=self.media)
        self.signed_url = "https://media.example.invalid/clip.mp4?signature=synthetic-private-ticket"
        self.media.reference.return_value = self.signed_url
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.media.ensure_uploaded.return_value = {"id": self.source_hash, "sha256": self.source_hash,
                                                  "bytes": self.source.stat().st_size}
        self.task_id = "amk-tool-video-ocr-test"
        self.client.submit_video_ocr.return_value = {"success": True, "task_id": self.task_id,
                                                   "request_id": "synthetic-submit-id"}
        self.ocr = {"duration": 10, "subtitles": [{"subtitle_text": self.translations[0]["source"],
            "start_time": 0.2, "end_time": 3.5, "text_label": "Subtitle", "text_location": {
                "top_left_x": 300, "top_left_y": 610, "bottom_right_x": 980, "bottom_right_y": 660}}]}
        self.client.get_task.return_value = self.completed()

    def completed(self, result=None):
        return {"success": True, "task_id": self.task_id, "status": "completed",
                "request_id": "synthetic-query-id", "result": copy.deepcopy(self.ocr if result is None else result)}

    def convert(self, **kwargs):
        return batch.convert(self.source, self.refs, self.output, max_polls=1, **kwargs)

    def checkpoint(self, result):
        return json.loads((Path(result["batch_dir"]) / "mediakit-batch.json").read_text(encoding="utf-8"))

    def assert_no_disk_secrets(self):
        for path in self.root.rglob("*.json"):
            text = path.read_text(encoding="utf-8")
            for secret in (self.signed_url, "synthetic-private-ticket", "synthetic-mediakit-secret"):
                self.assertNotIn(secret, text, path.name)

    def test_convert_saves_bound_source_and_native_ocr_checkpoint(self):
        result = self.convert(stop_after="ocr")
        report = self.checkpoint(result)
        self.assertEqual(result["status"], "paused_checkpoint")
        self.assertEqual(report["source_sha256"], self.source_hash)
        self.assertEqual(report["translations_sha256"], hashlib.sha256(self.refs.read_bytes()).hexdigest())
        self.assertEqual(report["ocr_result"], self.ocr)
        task = report["cloud_tasks"]["source_ocr"]
        self.assertEqual(task["task_id"], self.task_id)
        self.assertEqual(task["submission_state"], "submitted")
        self.assertEqual(task["status"], "completed")
        self.client.submit_video_ocr.assert_called_once_with(self.signed_url, task["client_token"], mode="Detailed")
        self.client.submit_erase.assert_not_called()
        self.assert_no_disk_secrets()

    def test_poll_budget_resume_queries_saved_task_without_resubmit(self):
        self.client.get_task.side_effect = [
            {"success": True, "task_id": self.task_id, "status": "running"}, self.completed()]
        first = self.convert(stop_after="ocr")
        self.assertEqual(first["status"], "paused_budget")
        second = batch.resume(first["batch_dir"], max_polls=1, stop_after="ocr")
        self.assertEqual(second["status"], "paused_checkpoint")
        self.assertEqual(self.client.get_task.call_count, 2)
        self.client.submit_video_ocr.assert_called_once()
        self.media.reference.assert_called_once()
        self.media.ensure_uploaded.assert_called_once()
        self.assert_no_disk_secrets()

    def test_unknown_post_is_checkpointed_and_resume_never_repeats_it(self):
        self.client.submit_video_ocr.side_effect = SeedanceError("network", "synthetic timeout",
            submission_uncertain=True, failure_stage="response_headers", request_id="synthetic-request-id")
        first = self.convert(stop_after="ocr")
        self.assertEqual(first["status"], "paused_error")
        report = self.checkpoint(first)
        self.assertEqual(report["cloud_tasks"]["source_ocr"]["submission_state"], "unknown")
        self.assertTrue(report["error"]["submission_uncertain"])
        second = batch.resume(first["batch_dir"], max_polls=1, stop_after="ocr")
        self.assertEqual(second["error"]["code"], "submission_unresolved")
        self.client.submit_video_ocr.assert_called_once()
        self.client.get_task.assert_not_called()
        self.media.reference.assert_called_once()
        self.assert_no_disk_secrets()

    def test_definite_rejection_is_not_retried_on_resume(self):
        self.client.submit_video_ocr.side_effect = SeedanceError("permission_denied", "synthetic refusal", http_status=403)
        first = self.convert(stop_after="ocr")
        self.assertEqual(self.checkpoint(first)["cloud_tasks"]["source_ocr"]["submission_state"], "rejected")
        second = batch.resume(first["batch_dir"], max_polls=1, stop_after="ocr")
        self.assertEqual(second["error"]["code"], "submission_unresolved")
        self.client.submit_video_ocr.assert_called_once()

    def test_changed_source_or_approved_translations_refuse_resume_before_network(self):
        first = self.convert(stop_after="ocr")
        for path in (self.source, self.refs):
            original = path.read_bytes()
            with self.subTest(path=path.name):
                path.write_bytes(original + b" changed")
                with self.assertRaises(ValueError):
                    batch.resume(first["batch_dir"], max_polls=1, stop_after="ocr")
                path.write_bytes(original)
        self.client_factory.assert_called_once()
        self.media_factory.assert_called_once()
        self.client.submit_video_ocr.assert_called_once()

    def test_changed_model_or_media_server_refuses_resume_before_network(self):
        first = self.convert(stop_after="ocr")
        self.profile_mock.return_value = {**self.profile, "model_version": "v5"}
        with self.assertRaises(ValueError):
            batch.resume(first["batch_dir"], max_polls=1, stop_after="ocr")
        self.profile_mock.return_value = self.profile
        self.backend_mock.return_value = {**self.backend, "endpoint": "https://other.example.invalid/codex-media/v1"}
        with self.assertRaises(ValueError):
            batch.resume(first["batch_dir"], max_polls=1, stop_after="ocr")
        self.client_factory.assert_called_once()

    def test_unmatched_ocr_does_not_submit_paid_erasure(self):
        self.ocr["subtitles"][0]["subtitle_text"] = "没有对应参考译文的字幕"
        self.client.get_task.return_value = self.completed()
        result = self.convert()
        report = self.checkpoint(result)
        self.assertEqual(report["binding"]["status"], "needs_review")
        self.assertTrue(report["binding"]["unmatched"])
        self.client.submit_erase.assert_not_called()
        self.media.submit_job.assert_not_called()
        self.assert_no_disk_secrets()

    def test_ocr_response_never_persists_an_unexpected_signed_video_url(self):
        self.client.get_task.return_value = self.completed({**self.ocr, "video_url": self.signed_url})
        self.convert(stop_after="ocr")
        self.assert_no_disk_secrets()

    def test_failed_task_keeps_id_and_resume_only_queries_it(self):
        self.client.get_task.return_value = {"success": True, "task_id": self.task_id, "status": "failed",
                                           "error": {"code": "AbilityProcessingError"}}
        first = self.convert(stop_after="ocr")
        self.assertEqual(first["error"]["provider_code"], "AbilityProcessingError")
        second = batch.resume(first["batch_dir"], max_polls=1, stop_after="ocr")
        self.assertEqual(second["error"]["code"], "cloud_task_failed")
        self.client.submit_video_ocr.assert_called_once()
        self.assertEqual(self.client.get_task.call_count, 2)

    def test_cancellation_preserves_submitted_task_for_resume(self):
        self.client.get_task.side_effect = SeedanceError("cancelled", "synthetic cancelled wait")
        first = self.convert(stop_after="ocr")
        self.assertEqual(first["status"], "cancelled")
        self.assertEqual(self.checkpoint(first)["cloud_tasks"]["source_ocr"]["task_id"], self.task_id)
        self.client.get_task.side_effect = None
        second = batch.resume(first["batch_dir"], max_polls=1, stop_after="ocr")
        self.assertEqual(second["status"], "paused_checkpoint")
        self.client.submit_video_ocr.assert_called_once()

    def test_invalid_translation_shape_never_uploads_or_submits(self):
        for refs in ([], {}, [{"source": "中文"}], [{"source": "", "target": "한국어"}],
                     [{"source": "中文", "target": "한국어", "extra": True}]):
            self.refs.write_text(json.dumps(refs), encoding="utf-8")
            with self.assertRaises(ValueError):
                self.convert(stop_after="ocr")
        self.client_factory.assert_not_called()
        self.media_factory.assert_not_called()

    def test_modified_checkpoint_translation_or_batch_location_refuses_resume(self):
        first = self.convert(stop_after="ocr")
        path = Path(first["batch_dir"]) / "mediakit-batch.json"
        original = self.checkpoint(first)
        for key, value in (("translations", [{"source": "changed", "target": "변경"}]),
                           ("batch_dir", str(self.root / "different-batch"))):
            with self.subTest(field=key):
                path.write_text(json.dumps({**original, key: value}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    batch.resume(first["batch_dir"], max_polls=1, stop_after="ocr")
        self.client_factory.assert_called_once()

    def configure_full_pipeline(self):
        self.erase_task = "amk-tool-erase-video-subtitle-pro-test"
        self.final_task = "amk-tool-video-ocr-final"
        self.clean_id = "c" * 64
        self.import_job = "a" * 64
        self.render_job = "b" * 64
        self.output_bytes = b"synthetic rendered Korean candidate"
        self.output_hash = hashlib.sha256(self.output_bytes).hexdigest()
        self.client.submit_video_ocr.side_effect = [
            {"success": True, "task_id": self.task_id}, {"success": True, "task_id": self.final_task}]
        self.client.submit_erase.return_value = {"success": True, "task_id": self.erase_task}
        self.final_ocr = {"duration": 10, "subtitles": [{
            **self.ocr["subtitles"][0], "subtitle_text": self.translations[0]["target"]}]}

        def task(task_id):
            if task_id == self.task_id:
                return self.completed()
            if task_id == self.erase_task:
                return {"success": True, "task_id": task_id, "status": "completed", "result": {
                    "duration": 10, "video_url": self.signed_url + "-clean"}}
            if task_id == self.final_task:
                return {"success": True, "task_id": task_id, "status": "completed", "result": self.final_ocr}
            self.fail("Unexpected cloud task")

        self.client.get_task.side_effect = task

        def submit_job(payload):
            if payload["kind"] == "import":
                return {"id": self.import_job, "kind": "import", "status": "queued"}
            if payload["kind"] == "render_subtitles":
                return {"id": self.render_job, "kind": "render_subtitles", "status": "queued"}
            self.fail("Unexpected server job")

        def get_job(job_id):
            if job_id == self.import_job:
                return {"id": job_id, "kind": "import", "status": "succeeded", "result": {
                    "id": self.clean_id, "sha256": self.clean_id, "bytes": 123}}
            if job_id == self.render_job:
                return {"id": job_id, "kind": "render_subtitles", "status": "succeeded", "result": {
                    "source_id": self.source_hash, "clean_id": self.clean_id,
                    "output": {"id": self.output_hash, "sha256": self.output_hash, "bytes": len(self.output_bytes)},
                    "technical_review": {"passed": True}}}
            self.fail("Unexpected server job id")

        self.media.submit_job.side_effect = submit_job
        self.media.get_job.side_effect = get_job
        self.media.transport = MagicMock()
        self.media.transport.download.side_effect = lambda url, path: Path(path).write_bytes(self.output_bytes)

    def test_full_pipeline_uses_cloud_erasure_server_render_and_requires_visual_review(self):
        self.configure_full_pipeline()
        result = self.convert()
        report = self.checkpoint(result)
        self.assertEqual(result["status"], "candidate_ready_needs_visual_review")
        self.assertEqual(result["visual_acceptance"], "not_reviewed")
        self.assertTrue(result["final_ocr"]["visual_review_required"])
        self.assertEqual(result["final_ocr"]["status"], "manual_review_required_ocr_language_limit")
        self.assertEqual(result["final_ocr"]["observations_possible_false_positive"], [])
        self.assertEqual(Path(result["candidate"]).read_bytes(), self.output_bytes)
        self.assertEqual(self.client.submit_video_ocr.call_count, 2)
        self.client.submit_erase.assert_called_once()
        erase = self.client.submit_erase.call_args.kwargs
        self.assertEqual(erase["model_version"], "v5.6")
        self.assertEqual(erase["mode"], "Text")
        self.assertEqual(erase["boxes"], report["render_plan"]["erase_boxes"])
        payloads = [call.args[0] for call in self.media.submit_job.call_args_list]
        self.assertEqual([payload["kind"] for payload in payloads], ["import", "render_subtitles"])
        self.assertEqual(payloads[0]["task_id"], self.erase_task)
        self.assertEqual(payloads[1]["source_id"], self.source_hash)
        self.assertEqual(payloads[1]["clean_id"], self.clean_id)
        self.assertEqual(payloads[1]["cues"][0]["text"], self.translations[0]["target"])
        self.assert_no_disk_secrets()

    def test_completed_pipeline_resume_makes_no_duplicate_cloud_or_server_work(self):
        self.configure_full_pipeline()
        first = self.convert()
        second = batch.resume(first["batch_dir"], max_polls=1)
        self.assertEqual(second["status"], first["status"])
        self.assertEqual(self.client.submit_video_ocr.call_count, 2)
        self.client.submit_erase.assert_called_once()
        self.assertEqual(self.client.get_task.call_count, 3)
        self.assertEqual(self.media.submit_job.call_count, 2)
        self.assertEqual(self.media.get_job.call_count, 2)
        self.media.transport.download.assert_called_once()
        self.assert_no_disk_secrets()

    def test_erase_unknown_post_resume_does_not_create_second_paid_task(self):
        self.configure_full_pipeline()
        self.client.submit_erase.side_effect = SeedanceError("network", "synthetic timeout", submission_uncertain=True)
        first = self.convert()
        self.assertEqual(first["status"], "paused_error")
        self.assertEqual(self.checkpoint(first)["cloud_tasks"]["erase"]["submission_state"], "unknown")
        second = batch.resume(first["batch_dir"], max_polls=1)
        self.assertEqual(second["error"]["code"], "submission_unresolved")
        self.client.submit_erase.assert_called_once()
        self.client.submit_video_ocr.assert_called_once()
        self.media.submit_job.assert_not_called()
        self.assert_no_disk_secrets()

    def test_erase_checkpoint_resume_recovers_url_by_query_without_new_erasure(self):
        self.configure_full_pipeline()
        first = self.convert(stop_after="erase")
        self.assertEqual(first["status"], "paused_checkpoint")
        self.assert_no_disk_secrets()
        second = batch.resume(first["batch_dir"], max_polls=1)
        self.assertEqual(second["status"], "candidate_ready_needs_visual_review")
        self.client.submit_erase.assert_called_once()
        erase_queries = [call.args[0] for call in self.client.get_task.call_args_list if call.args[0] == self.erase_task]
        self.assertEqual(len(erase_queries), 2)
        self.assert_no_disk_secrets()

    def test_downloaded_frame_count_mismatch_stops_before_final_paid_ocr(self):
        self.configure_full_pipeline()
        self.probe.side_effect = [self.metadata, {**self.metadata, "frame_count": 299}]
        result = self.convert()
        self.assertEqual(result["status"], "paused_error")
        self.assertEqual(result["error"]["code"], "candidate_metadata_mismatch")
        self.client.submit_video_ocr.assert_called_once()

    def test_final_han_detection_is_an_observation_without_claiming_residue_or_acceptance(self):
        self.configure_full_pipeline()
        self.final_ocr["subtitles"][0]["subtitle_text"] = "残字"
        result = self.convert()
        self.assertEqual(result["status"], "candidate_ready_needs_visual_review")
        self.assertEqual(result["final_ocr"]["status"], "manual_review_required_ocr_language_limit")
        self.assertEqual(result["final_ocr"]["observations_possible_false_positive"], self.final_ocr["subtitles"])
        self.assertTrue(result["final_ocr"]["visual_review_required"])
        self.assertEqual(result["visual_acceptance"], "not_reviewed")

    def test_real_korean_candidate_ocr_garbling_never_counts_as_verified_chinese_residue(self):
        # Sanitized observations from the real clip9 Korean candidate, 2026-11-27.
        # Official video-ocr supports only Simplified Chinese and English:
        # https://docs.volcengine.com/docs/intelligent-processing/video-ocr-guide?lang=zh
        rows = [
            (",早引021引?", 0.7, 2.966, (483, 635, 806, 674)),
            ("创卫", 3.733, 5.566, (495, 635, 786, 674)),
            ("喜烈01.", 6.6, 7.266, (599, 635, 682, 674)),
            ("叶列", 7.6, 7.833, (455, 635, 830, 674)),
            ("对别号号叶音列", 7.966, 9.8, (451, 635, 830, 674)),
        ]
        observations = [{"subtitle_text": text, "start_time": start, "end_time": end, "text_label": "Others",
                         "text_location": dict(zip(("top_left_x", "top_left_y", "bottom_right_x", "bottom_right_y"), box))}
                        for text, start, end, box in rows]
        watermark = {"subtitle_text": "AI生成", "start_time": 0, "end_time": 3.233, "text_label": "Others",
                     "text_location": {"top_left_x": 1025, "top_left_y": 623, "bottom_right_x": 1272, "bottom_right_y": 707}}
        plan = {"cues": [{"box": {"x": 400, "y": 600, "width": 500, "height": 100}}]}
        result = batch._ocr_review({"duration": 10, "subtitles": [watermark, *observations]},
                                   plan, {"groups": [{"target_preserved": True}]})
        self.assertEqual(result["status"], "manual_review_required_ocr_language_limit")
        self.assertEqual(result["observations_possible_false_positive"], observations)
        self.assertNotIn("chinese_in_target_region", result)
        self.assertTrue(result["visual_review_required"])
        self.assertTrue(result["reference_targets_preserved_in_render"])


if __name__ == "__main__":
    unittest.main()
