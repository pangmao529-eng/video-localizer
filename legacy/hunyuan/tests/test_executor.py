import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from video_localizer.executor import VideoExecutor, ExecutorCancelled
from video_localizer.materials import Materials
from video_localizer import _engine as engine


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / "input.mp4"
        self.source.write_bytes(b"test source, never modified")
        self.font = self.base / "font.ttf"
        self.font.write_bytes(b"test fixture")
        self.executor = VideoExecutor(self.source, self.base / "task", {"font": str(self.font)})
        self.executor.manifest = {"media": {"width": 320, "height": 180, "durationMs": 400, "fps": 7.5},
            "frames": [{"frameId": f"f{i:06}", "index": i, "ptsMs": t, "durationMs": d}
                       for i, (t, d) in enumerate([(0, 100), (100, 200), (300, 100)])]}
        self.executor.state["source_sha256"] = engine.digest(self.source)
        self.segment = {"id": "label", "source_text": "PLAY", "target_text": "시작", "kind": "ui",
                        "portrait_match": "not_applicable", "start_frame": 0, "end_frame": 3,
                        "keyframes": [{"frame": 0, "box": [10, 10, 50, 20]}], "motion": "fixed",
                        "mask": {"mode": "light", "dilate_px": 1},
                        "style": {"font": "sans", "font_size": 16, "color": "#ffeedd", "outline_color": "#101010",
                                  "outline_width": 1, "align": "left"}}

    def tearDown(self):
        self.temp.cleanup()

    def submit(self, segment=None, revision=0):
        return self.executor.call("set_edit_plan", {"expected_revision": revision,
            "summary": "A model-provided concrete plan", "segments": [segment or self.segment]})

    def test_plan_preserves_model_style_and_expands_fixed_motion(self):
        value = self.submit()
        self.assertTrue(value["result"]["ok"])
        path = self.executor.root / self.executor.state["compiled_plan_file"]
        segment = json.loads(path.read_text("utf-8"))["segments"][0]
        self.assertEqual(segment["style"]["color"], "#ffeedd")
        self.assertEqual(segment["mask"]["mode"], "light")
        self.assertEqual(len(segment["keyframes"]), 3)
        self.assertEqual(segment["endMs"], 400)

    def test_linear_motion_uses_pts_not_frame_number(self):
        self.segment.update(motion="linear", keyframes=[
            {"frame": 0, "box": [10, 10, 50, 20]}, {"frame": 2, "box": [100, 10, 50, 20]}])
        compiled, _ = self.executor._compile([self.segment])
        self.assertAlmostEqual(compiled[0]["keyframes"][1]["box"]["x"], 40/320)

    def test_plan_revision_invalidates_both_render_artifacts(self):
        self.submit()
        self.executor.state["artifacts"].update(preview={"file": "p.mp4"}, final={"file": "f.mp4"})
        self.executor.state.update(final_output="f.mp4", preview_output="p.mp4")
        self.assertTrue(self.submit(revision=1)["result"]["ok"])
        self.assertEqual(self.executor.state["plan_revision"], 2)
        self.assertNotIn("final", self.executor.state["artifacts"])
        self.assertNotIn("preview", self.executor.state["artifacts"])
        self.assertIsNone(self.executor.state["final_output"])
        self.assertIsNone(self.executor.state["preview_output"])

    def test_stale_plan_leaves_current_state_untouched(self):
        self.submit()
        before = self.executor.snapshot()
        self.assertFalse(self.submit(revision=0)["result"]["ok"])
        self.assertEqual(self.executor.snapshot(), before)

    def test_portrait_conflict_is_recorded_and_never_changes_the_translation(self):
        self.segment.update(kind="nameplate", portrait_match="mismatch", target_text="나미")
        value = self.submit()["result"]
        self.assertEqual(value["plan_issues"][0]["code"], "portrait_review_required")
        self.assertEqual(self.executor.state["plan"]["segments"][0]["target_text"], "나미")

    def test_finalized_translation_conflict_is_exposed(self):
        self.executor.materials = Materials(translations={"play": ["플레이"]})
        value = self.submit()["result"]
        self.assertEqual(value["plan_issues"][0]["code"], "finalized_translation_conflict")
        self.assertEqual(value["plan_issues"][0]["approved_values"], ["플레이"])

    def test_boundary_keyframe_is_mandatory(self):
        self.segment["keyframes"][0]["frame"] = 1
        self.assertFalse(self.submit()["result"]["ok"])
        self.assertEqual(self.executor.state["plan_revision"], 0)

    def test_explicit_style_required(self):
        del self.segment["style"]["color"]
        self.assertFalse(self.submit()["result"]["ok"])

    def test_auto_color_inference_not_exposed(self):
        self.segment["mask"]["mode"] = "auto"
        self.assertFalse(self.submit()["result"]["ok"])

    def test_arbitrary_code_is_not_a_tool(self):
        names = [x["function"]["name"] for x in self.executor.schemas()]
        self.assertNotIn("execute_python", names)
        self.assertNotIn("shell", names)
        with self.assertRaises(ValueError):
            self.executor.call("execute_python", {"code": "print('not permitted')"})

    def test_artifact_path_traversal_is_rejected(self):
        self.executor.state["artifacts"]["final"] = {"file": "../input.mp4", "revision": 1}
        value = self.executor.call("inspect_frames", {"artifact": "final", "frames": [0]})
        self.assertFalse(value["result"]["ok"])

    def test_invalid_frame_types_are_tool_errors(self):
        self.executor.state["artifacts"]["source"] = {"file": "source.mp4", "revision": 0}
        value = self.executor.call("inspect_frames", {"artifact": "source", "frames": [{}]})
        self.assertFalse(value["result"]["ok"])

    def test_source_directory_cannot_be_the_task_directory(self):
        with self.assertRaises(ValueError):
            VideoExecutor(self.source, self.base, {"font": str(self.font)})

    def test_cancel_is_not_swallowed_as_a_model_repairable_error(self):
        self.executor.cancelled = lambda: True
        with self.assertRaises(ExecutorCancelled):
            self.executor.call("inspect_video", {})

    def test_persistence_preserves_revision_and_source_identity(self):
        self.submit()
        other = VideoExecutor(self.source, self.executor.root, {"font": str(self.font)})
        self.assertEqual(other.state["plan_revision"], 1)
        other_source = self.base / "other.mp4"
        other_source.write_bytes(b"other")
        with self.assertRaises(ValueError):
            VideoExecutor(other_source, self.executor.root, {"font": str(self.font)})

    def test_successful_render_requires_actual_file_and_later_visual_review(self):
        self.submit()
        def render(request):
            (self.executor.root/request["output"]).write_bytes(b"test rendered output")
            return {"file": request["output"], "originalAudioPreserved": True, "changedFrames": 3,
                    "media": {"width": 320, "height": 180}}
        with patch.object(self.executor, "_run_operation", side_effect=render):
            result = self.executor.call("render_final", {"expected_revision": 1})["result"]
        self.assertTrue(result["ok"])
        self.assertTrue(result["requires_model_visual_review"])
        self.assertEqual(result["metadata"]["artifact_kind"], "final")
        self.assertEqual(result["metadata"]["render_revision"], 1)
        self.assertFalse(self.executor.state["automatic_quality_pass"])
        self.assertEqual(self.source.read_bytes(), b"test source, never modified")

    def test_region_program_changes_revision_and_keeps_the_target_text_locked(self):
        self.submit()
        self.executor.state["artifacts"]["final"] = {"file":"f.mp4","revision":1}
        result = self.executor.call("set_region_program", {"expected_revision":1,"segment_id":"label",
            "code":"background=inpaint(original,light_mask(original),3)"})["result"]
        self.assertTrue(result["ok"],result)
        self.assertEqual(result["metadata"]["plan_revision"],2)
        self.assertNotIn("final",self.executor.state["artifacts"])
        self.assertEqual(self.executor.state["plan"]["segments"][0]["target_text"],"시작")
        self.assertIn("label",self.executor.state["programs"])

    def test_unseen_reference_and_unsafe_program_are_rejected(self):
        self.submit()
        for extra in ({"code":"background=reference(0)","reference_frames":[1]},
                      {"code":"import os\nbackground=original"}):
            result=self.executor.call("set_region_program",{"expected_revision":1,"segment_id":"label",**extra})
            self.assertFalse(result["result"]["ok"],result)
        self.assertEqual(self.executor.state["plan_revision"],1)

    def test_incremental_plan_preserves_untouched_segments_and_programs(self):
        self.submit()
        self.executor.call("set_region_program",{"expected_revision":1,"segment_id":"label","code":"background=original"})
        other=copy.deepcopy(self.segment)
        other.update(id="second",source_text="GO",target_text="가자")
        value=self.executor.call("update_edit_plan",{"expected_revision":2,"summary":"Add another label",
            "upsert_segments":[other],"remove_ids":[]})
        self.assertTrue(value["result"]["ok"],value)
        self.assertEqual([s["id"] for s in self.executor.state["plan"]["segments"]],["label","second"])
        self.assertIn("label",self.executor.state["programs"])
        self.assertEqual(self.executor.state["plan"]["segments"][0]["target_text"],"시작")
        replaced=copy.deepcopy(self.segment)
        replaced["target_text"]="플레이"
        value=self.executor.call("update_edit_plan",{"expected_revision":3,"summary":"Change first, remove second",
            "upsert_segments":[replaced],"remove_ids":["second"]})
        self.assertTrue(value["result"]["ok"],value)
        self.assertEqual(len(self.executor.state["plan"]["segments"]),1)
        self.assertNotIn("label",self.executor.state["programs"])

    def test_incremental_plan_rejects_ambiguous_changes(self):
        self.submit()
        value=self.executor.call("update_edit_plan",{"expected_revision":1,"summary":"Invalid",
            "upsert_segments":[self.segment],"remove_ids":["label"]})
        self.assertFalse(value["result"]["ok"])
        self.assertEqual(self.executor.state["plan_revision"],1)

    def test_incremental_revision_and_retained_program_are_persisted_together(self):
        self.submit()
        self.executor.call("set_region_program",{"expected_revision":1,"segment_id":"label","code":"background=original"})
        other=copy.deepcopy(self.segment)
        other["id"]="second"
        saved=[]
        with patch.object(self.executor,"_persist",side_effect=lambda: saved.append(copy.deepcopy(self.executor.state))):
            value=self.executor.call("update_edit_plan",{"expected_revision":2,"summary":"Add segment",
                "upsert_segments":[other],"remove_ids":[]})
        self.assertTrue(value["result"]["ok"],value)
        revisions=[state for state in saved if state["plan_revision"]==3]
        self.assertTrue(revisions)
        self.assertTrue(all("label" in state["programs"] for state in revisions))

    def test_cancel_survives_process_tree_exit_race(self):
        count=0
        def cancelled():
            nonlocal count
            count+=1
            return count>1
        self.executor.cancelled=cancelled
        child=Mock(pid=12345)
        child.poll.return_value=None
        child.wait.return_value=0
        with patch("video_localizer.executor.subprocess.Popen",return_value=child), \
             patch("video_localizer.executor.subprocess.run",side_effect=ProcessLookupError), \
             patch("video_localizer.executor.os.killpg",side_effect=ProcessLookupError,create=True):
            with self.assertRaises(ExecutorCancelled):
                self.executor._run_operation({"operation":"render"})
        child.kill.assert_called_once()

    def test_material_lookup_returns_original_row_evidence(self):
        row={"file":"chosen.csv","row_number":2,"line_number":2,"source":"PLAY","target":"시작","raw_row":["PLAY","시작"]}
        self.executor.materials=Materials(translations={"play":["시작"]},provenance={"translations":{"play":[row]}})
        value=self.executor.call("lookup_materials",{"queries":["PLAY"]})["result"]
        self.assertEqual(value["rows"][0]["translation_rows"],[row])
        self.assertEqual(value["rows"][0]["glossary_rows"],[])


if __name__ == "__main__":
    unittest.main()
