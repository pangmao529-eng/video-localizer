"""Segment parent/child transitions with synthetic bytes and no real API/media."""

import hashlib
import json
from pathlib import Path
import unittest

import test_seedance_batch as helpers
from video_localizer import batch, config, seedance_batch, seedance_client


class SeedanceSegmentBatchTests(unittest.TestCase):
    # Share transport/probe test scaffolding, without inheriting the 2.5 test cases.
    patch = helpers.SeedanceBatchTests.patch
    upload = helpers.SeedanceBatchTests.upload
    get_file = helpers.SeedanceBatchTests.get_file
    create_edit = helpers.SeedanceBatchTests.create_edit
    get_task = helpers.SeedanceBatchTests.get_task
    convert = helpers.SeedanceBatchTests.convert
    stored = staticmethod(helpers.SeedanceBatchTests.stored)
    media_command = staticmethod(helpers.SeedanceBatchTests.media_command)

    def setUp(self):
        original_review = seedance_batch.technical_review
        helpers.SeedanceBatchTests.setUp(self)
        self.connection[0]["model"] = config.SEEDANCE_20_MODEL
        self.media["durationMs"] = 20100
        self.manifests = {}

        def review(source, candidate, options, info, **kwargs):
            self.events.append(("review", Path(source).name))
            return original_review(source, candidate, options, info, **kwargs)

        self.review.side_effect = review
        self.prepare = self.patch("video_localizer.seedance_segment_batch.prepare_segments",
                                  side_effect=self.prepare_segments)
        self.join = self.patch("video_localizer.seedance_segment_batch.join_candidates",
                               side_effect=self.join_candidates)

    def probe_media(self, path, _options, **_kwargs):
        path = Path(path)
        media = dict(self.media, bytes=path.stat().st_size)
        if path.name.startswith("part-"):
            index = int(path.stem.split("-")[-1])
            media["durationMs"] = (301 if index == 1 else 302) / 30 * 1000
        elif "seedance-jobs" in path.parts:
            index = int(path.parent.name)
            media["durationMs"] = (301 if index == 1 else 302) / 30 * 1000
            media.update(hasAudio=False, audioCodec=None)
        return media

    @staticmethod
    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def prepare_segments(self, source, work, _options, source_info):
        """Mock the mechanical helper's durable manifest and validation contract."""
        key = str(work)
        if key not in self.manifests:
            folder = Path(work) / "seedance-segments"
            folder.mkdir()
            parts = []
            for index, start, end in ((1, 0, 301), (2, 301, 603)):
                path = folder / f"part-{index:03d}.mp4"
                path.write_bytes(f"synthetic part {index} from {Path(source).name}".encode())
                parts.append({"index": index, "path": str(path), "start_frame": start,
                              "end_frame": end, "frame_count": end - start,
                              "duration_seconds": (end - start) / 30,
                              "sha256": self.digest(path)})
            self.manifests[key] = {"schema": 1, "source": str(source),
                                   "source_sha256": source_info["sha256"],
                                   "source_frame_count": 603, "fps_ratio": "30",
                                   "parts": parts}
        manifest = self.manifests[key]
        for part in manifest["parts"]:
            if self.digest(part["path"]) != part["sha256"]:
                raise ValueError("已保存分段文件哈希变化，拒绝恢复。")
        return manifest

    def download(self, _url, output):
        output = Path(output)
        self.events.append(("download", str(output)))
        output.write_bytes(f"synthetic generated candidate {output.parent.name}".encode())
        return {"path": str(output), "bytes": output.stat().st_size,
                "sha256": self.digest(output)}

    def join_candidates(self, source, candidates, output, _options):
        self.events.append(("join", Path(source).name))
        Path(output).write_bytes(b"synthetic joined: " + b" / ".join(
            Path(candidate).read_bytes() for candidate in candidates))
        return {"path": str(output), "sha256": self.digest(output)}

    def child_report(self, result):
        row = self.stored(result)["videos"][0]
        return json.loads((Path(row["child_batch_dir"]) / "batch.json").read_text(encoding="utf-8"))

    def pause_second_task(self):
        self.client.get_task.side_effect = lambda task_id: (
            self.get_task(task_id) if task_id == "cgt-1" else {"id": task_id, "status": "running"})
        result = self.convert(max_turns=1)
        self.assertEqual(result["status"], "paused_budget")
        self.assertEqual([row["task_id"] for row in self.child_report(result)["videos"]],
                         ["cgt-1", "cgt-2"])
        self.join.assert_not_called()
        return result

    def cancel_before_join(self):
        cancellation = {"requested": False}

        def progress(event):
            if (event.get("segment_index") == 2
                    and event.get("phase") == "segment_awaiting_visual_review"):
                cancellation["requested"] = True

        result = self.convert(progress=progress, cancelled=lambda: cancellation["requested"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual([row["status"] for row in self.child_report(result)["videos"]],
                         ["needs_review", "needs_review"])
        self.join.assert_not_called()
        return result

    def test_20_second_parent_edits_two_parts_then_joins_without_claiming_completion(self):
        result = self.convert()
        parent = self.stored(result)
        row = result["videos"][0]
        child = self.child_report(result)
        self.assertTrue(parent["segmented"])
        self.assertEqual(parent["videos"][0]["source_media"]["durationMs"], 20100)
        self.assertEqual(len(child["videos"]), 2)
        self.assertEqual(self.client.upload_video.call_count, 2)
        self.assertEqual(self.client.create_edit.call_count, 2)
        self.assertEqual([call.kwargs["generate_audio"] for call in self.client.create_edit.call_args_list],
                         [False, False])
        self.assertEqual([call.kwargs["resolution"] for call in self.client.create_edit.call_args_list],
                         ["720p", "720p"])
        self.join.assert_called_once()
        self.assertEqual([Path(path) for path in self.join.call_args.args[1]],
                         [Path(part["preview"]) for part in child["videos"]])
        self.assertNotEqual(Path(child["videos"][0]["source"]).read_bytes(),
                            Path(child["videos"][1]["source"]).read_bytes())
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["outputs"], [])
        self.assertEqual(result["pending_reviews"], 1)
        self.assertIsNone(row["output"])
        self.assertTrue(Path(row["preview"]).is_file())
        self.assertFalse(row["model_reviewed"])
        self.assertFalse(row["human_approved"])
        self.assertEqual(row["technical_review"]["status"], "passed")
        self.assertEqual(row["technical_review"]["segment_count"], 2)
        self.assertEqual(row["technical_review"]["boundary_visual_review"], "not_performed")
        self.assertTrue(row["technical_review"]["source_audio_reused"])
        self.assertTrue(all(part["technical_review"]["checks"]["generated_audio_absent"]
                            for part in child["videos"]))
        for path in Path(result["directory"]).rglob("*.json"):
            self.assertNotIn("synthetic-test-key", path.read_text(encoding="utf-8"))

    def test_segment_transport_diagnostics_reach_parent_and_survive_resume(self):
        self.client.upload_video.side_effect = seedance_client.SeedanceError(
            "network", "synthetic header timeout", submission_uncertain=True,
            failure_stage="response_headers", exception_type="TimeoutError",
            completed_body_bytes=8192)
        first = self.convert()
        row = first["videos"][0]
        self.assertEqual(first["status"], "paused_error")
        diagnostic = row["last_transport_error"]
        self.assertEqual(diagnostic["segment_index"], 1)
        self.assertEqual(diagnostic["failure_stage"], "response_headers")
        self.assertEqual(diagnostic["completed_body_bytes"], 8192)
        self.assertEqual(row["segment_tasks"][0]["last_transport_error"],
                         self.child_report(first)["videos"][0]["last_transport_error"])
        second = batch.resume(first["directory"])
        self.assertEqual(second["videos"][0]["error_code"], "upload_uncertain")
        self.assertEqual(second["videos"][0]["last_transport_error"], diagnostic)
        self.assertEqual(self.client.upload_video.call_count, 1)
        self.client.create_edit.assert_not_called()
        self.client.get_task.assert_not_called()
        self.assertEqual(self.child_report(second)["videos"][1]["status"], "not_processed")
        self.join.assert_not_called()

    def test_both_policy_failures_reach_parent_and_resume_does_not_regenerate(self):
        code = "OutputVideoSensitiveContentDetected.PolicyViolation"
        self.client.get_task.side_effect = lambda task_id: {
            "id": task_id, "status": "failed", "error_code": code,
            "message": "平台版权检查拒绝生成，原任务已保留。"}
        first = self.convert()
        before = len(self.client.mock_calls)
        resumed = batch.resume(first["directory"])
        for result in (first, resumed):
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["outputs"], [])
            parent = result["videos"][0]
            self.assertEqual(parent["remote_error_code"], code)
            self.assertEqual([part["remote_error_code"] for part in parent["segment_tasks"]], [code, code])
            self.assertEqual([part["task_id"] for part in parent["segment_tasks"]], ["cgt-1", "cgt-2"])
            self.assertFalse(parent["human_approved"])
        self.assertEqual(self.client.create_edit.call_count, 2)
        self.assertEqual(self.client.upload_video.call_count, 2)
        self.assertEqual(self.client.get_task.call_count, 4)
        self.assertEqual([call[0] for call in self.client.mock_calls[before:]], ["get_task", "get_task"])
        self.client.download.assert_not_called()
        self.review.assert_not_called()
        self.join.assert_not_called()

    def test_endpoint_20_uses_two_segments_with_same_id_and_explicit_family(self):
        endpoint_id = "ep-20261126155948-example"
        self.connection[0].update(model=endpoint_id, model_family=config.SEEDANCE_20_FAMILY)
        result = self.convert()
        child = self.child_report(result)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["model"], endpoint_id)
        self.assertEqual(result["model_family"], config.SEEDANCE_20_FAMILY)
        self.assertEqual(child["model"], endpoint_id)
        self.assertEqual(child["model_family"], config.SEEDANCE_20_FAMILY)
        self.assertEqual(len(child["videos"]), 2)
        self.assertEqual(self.client.upload_video.call_count, 2)
        self.assertEqual(self.client.create_edit.call_count, 2)
        self.assertTrue(all(not call.kwargs["generate_audio"] for call in self.client.create_edit.call_args_list))
        self.assertEqual(self.factory.call_args.args[2], endpoint_id)
        self.assertEqual(self.factory.call_args.kwargs["model_family"], config.SEEDANCE_20_FAMILY)
        self.join.assert_called_once()

    def test_endpoint_family_change_cannot_resume_segmented_checkpoint(self):
        self.connection[0].update(model="ep-20261126155948-example", model_family=config.SEEDANCE_20_FAMILY)
        first = self.pause_second_task()
        calls = list(self.client.mock_calls)
        prepare_count = self.prepare.call_count
        self.connection[0]["model_family"] = config.SEEDANCE_25_FAMILY
        with self.assertRaises(config.SettingsError):
            batch.resume(first["directory"])
        self.assertEqual(self.client.mock_calls, calls)
        self.assertEqual(self.prepare.call_count, prepare_count)

    def test_resume_running_second_part_keeps_first_preview_and_only_polls_second_task(self):
        first = self.pause_second_task()
        first_part = self.child_report(first)["videos"][0]
        first_bytes = Path(first_part["preview"]).read_bytes()
        calls_before = len(self.client.get_task.call_args_list)
        self.client.get_task.side_effect = self.get_task
        resumed = batch.resume(first["directory"], max_turns=3)
        self.assertEqual(resumed["status"], "needs_review")
        self.assertEqual(self.child_report(resumed)["videos"][0], first_part)
        self.assertEqual(Path(first_part["preview"]).read_bytes(), first_bytes)
        self.assertEqual([call.args[0] for call in self.client.get_task.call_args_list[calls_before:]], ["cgt-2"])
        self.assertEqual(self.client.upload_video.call_count, 2)
        self.assertEqual(self.client.create_edit.call_count, 2)
        self.join.assert_called_once()

    def test_resume_pending_second_upload_submits_only_that_part(self):
        self.client.get_file.side_effect = lambda file_id: (
            self.get_file(file_id) if file_id == "file-1" else {"file_id": file_id, "status": "processing"})
        first = self.convert(max_turns=1)
        self.assertEqual(first["status"], "paused_budget")
        child = self.child_report(first)
        self.assertEqual(child["videos"][0]["status"], "needs_review")
        self.assertEqual(child["videos"][1]["file_id"], "file-2")
        self.assertIsNone(child["videos"][1].get("task_id"))
        self.assertEqual(self.client.create_edit.call_count, 1)
        self.join.assert_not_called()
        queried_before = len(self.client.get_file.call_args_list)
        self.client.get_file.side_effect = self.get_file
        second = batch.resume(first["directory"], max_turns=3)
        self.assertEqual(second["status"], "needs_review")
        self.assertEqual(self.client.upload_video.call_count, 2)
        self.assertEqual(self.client.create_edit.call_count, 2)
        self.assertEqual([call.args[0] for call in self.client.get_file.call_args_list[queried_before:]], ["file-2"])
        self.assertEqual(self.child_report(second)["videos"][0], child["videos"][0])
        self.join.assert_called_once()

    def test_uncertain_second_submission_never_repeats_cloud_post_or_joins(self):
        def uncertain_second(reference, prompt, **kwargs):
            if self.client.create_edit.call_count == 2:
                raise seedance_client.SeedanceError(
                    "transport_error", "Synthetic second submission response lost",
                    submission_uncertain=True)
            return self.create_edit(reference, prompt, **kwargs)

        self.client.create_edit.side_effect = uncertain_second
        first = self.convert()
        self.assertEqual(first["status"], "paused_error")
        child = self.child_report(first)
        self.assertEqual(child["videos"][0]["status"], "needs_review")
        self.assertEqual(child["videos"][1]["submission_state"], "submitting")
        self.assertIsNone(child["videos"][1].get("task_id"))
        before = list(self.client.mock_calls)
        self.client.create_edit.side_effect = self.create_edit
        second = batch.resume(first["directory"])
        self.assertEqual(second["status"], "paused_error")
        self.assertEqual(second["videos"][0]["error_code"], "submission_uncertain")
        self.assertEqual(self.child_report(second)["videos"][0], child["videos"][0])
        self.assertEqual(self.client.mock_calls, before)
        self.assertEqual(self.client.create_edit.call_count, 2)
        self.join.assert_not_called()

    def test_cancel_before_upload_does_not_prepare_submit_or_join(self):
        result = self.convert(cancelled=lambda: True)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["unprocessed"], 1)
        self.prepare.assert_not_called()
        self.client.upload_video.assert_not_called()
        self.client.create_edit.assert_not_called()
        self.join.assert_not_called()

    def test_cancel_with_finished_parts_keeps_candidates_and_resume_only_joins(self):
        first = self.cancel_before_join()
        child = self.child_report(first)
        self.assertTrue(all(Path(row["preview"]).is_file() for row in child["videos"]))
        before = list(self.client.mock_calls)
        second = batch.resume(first["directory"])
        self.assertEqual(second["status"], "needs_review")
        self.assertEqual(self.client.mock_calls, before)
        self.assertEqual(self.child_report(second)["videos"], child["videos"])
        self.join.assert_called_once()

    def test_human_rejection_survives_resume_without_cloud_calls_or_rejoining(self):
        first = self.convert()
        decision = {"action": "reject", "note": "Synthetic explicit human rejection of segment seam"}
        batch.submit_review(first["directory"], 1, seedance_batch.REVIEW_ID, decision)
        rejected = self.stored(first)["videos"][0]
        self.assertTrue(rejected["human_rejected"])
        self.assertEqual(rejected["issues"][0]["decision"], decision)
        before = list(self.client.mock_calls)
        second = batch.resume(first["directory"])
        self.assertEqual(second["status"], "failed")
        self.assertEqual(second["outputs"], [])
        self.assertFalse(second["human_approved"])
        self.assertEqual(self.stored(second)["videos"][0], rejected)
        self.assertEqual(self.client.mock_calls, before)
        self.prepare.assert_called_once()
        self.join.assert_called_once()

    def test_changed_original_refuses_resume_before_any_cloud_call_or_join(self):
        first = self.pause_second_task()
        self.sources[0].write_bytes(b"changed original synthetic bytes")
        before = list(self.client.mock_calls)
        second = batch.resume(first["directory"])
        self.assertEqual(second["status"], "failed")
        self.assertEqual(second["videos"][0]["error_code"], "segment_validation")
        self.assertIn("原片已变化", second["videos"][0]["message"])
        self.assertEqual(self.client.mock_calls, before)
        self.prepare.assert_called_once()
        self.join.assert_not_called()

    def test_changed_part_rejected_by_preparation_prevents_resume_and_join(self):
        first = self.pause_second_task()
        Path(self.child_report(first)["videos"][0]["source"]).write_bytes(b"changed part bytes")
        before = list(self.client.mock_calls)
        second = batch.resume(first["directory"])
        self.assertEqual(second["status"], "failed")
        self.assertEqual(second["videos"][0]["error_code"], "segment_validation")
        self.assertIn("分段文件哈希变化", second["videos"][0]["message"])
        self.assertEqual(self.client.mock_calls, before)
        self.join.assert_not_called()

    def test_changed_child_candidate_refuses_join_without_new_cloud_calls(self):
        first = self.cancel_before_join()
        Path(self.child_report(first)["videos"][0]["preview"]).write_bytes(b"changed child candidate")
        before = list(self.client.mock_calls)
        second = batch.resume(first["directory"])
        self.assertEqual(second["status"], "failed")
        self.assertEqual(second["videos"][0]["error_code"], "segment_validation")
        self.assertIn("候选文件校验不匹配", second["videos"][0]["message"])
        self.assertEqual(self.client.mock_calls, before)
        self.join.assert_not_called()

    def test_changed_joined_candidate_cannot_be_accepted(self):
        result = self.convert()
        Path(result["videos"][0]["preview"]).write_bytes(b"changed joined candidate")
        before = list(self.client.mock_calls)
        with self.assertRaisesRegex(ValueError, "候选视频已变化"):
            batch.submit_review(result["directory"], 1, seedance_batch.REVIEW_ID,
                                {"action": "keep", "note": "Synthetic acceptance attempt"})
        self.assertEqual(self.stored(result)["status"], "needs_review")
        self.assertFalse(self.stored(result)["human_approved"])
        self.assertEqual(self.client.mock_calls, before)
        self.join.assert_called_once()


if __name__ == "__main__":
    unittest.main()
