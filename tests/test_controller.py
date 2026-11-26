import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_localizer.controller import HunyuanController, record_review
from video_localizer.hunyuan import HunyuanError, MAX_OUTPUT_TOKENS, MAX_REQUEST_TIMEOUT
from video_localizer.materials import Materials


def action(name, args, number):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": str(number), "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)}}]}


def notes(start=0, end=30, tasks=None):
    return {"findings": "Observed the requested source frames.",
            "focus": {"start_frame": start, "end_frame": end, "objective": "Inspect and edit one clear region."},
            "tasks": [] if tasks is None else tasks, "next_action": "Prepare the focused preview."}


def task_record(key, status="open", frames=None, segments=None):
    return {"id": key, "description": "Check the named region.", "status": status,
            "evidence_frames": frames or [], "segment_ids": segments or []}


def segment(key, start=0, end=10):
    return {"id": key, "kind": "subtitle", "source_text": "PLAY", "target_text": "시작",
            "start_frame": start, "end_frame": end}


def legacy_budget_failure():
    return {"request": 15, "response_id": "saved-request", "model": "hy4", "status": "failed",
            "stage": "parse_response", "http_status": 200, "finish_reason": "tool_calls", "max_tokens": 32768,
            "usage": {"prompt_tokens": 100, "completion_tokens": 32768, "total_tokens": 32868}}


class Client:
    def __init__(self, actions):
        self.actions = iter(actions)
        self.calls = []
        self.messages = []
        self.options = []
        self.tools = []

    def complete(self, messages, tools, **options):
        self.messages.append(copy.deepcopy(messages))
        self.options.append(copy.deepcopy(options))
        self.tools.append(copy.deepcopy(tools))
        self.calls.append({"status": "ok", "model": "hy4"})
        result = next(self.actions)
        if isinstance(result, Exception):
            self.calls[-1].update(status="error", code=getattr(result, "code", type(result).__name__))
            raise result
        return result


class Executor:
    def __init__(self, root):
        self.source = root / "source.mp4"
        self.materials = Materials()
        self.state = {"plan_revision": 0, "final_output": None, "preview_output": None}
        self.executed = []

    def schemas(self):
        return []

    def snapshot(self):
        return self.state.copy()

    def call(self, name, args):
        self.executed.append(name)
        images = []
        meta = {"plan_revision": self.state["plan_revision"]}
        if name in ("set_edit_plan", "update_edit_plan", "set_region_program"):
            self.state["plan_revision"] += 1
        if name == "set_edit_plan":
            self.state["plan"] = copy.deepcopy(args)
        if name == "update_edit_plan":
            segments = {s["id"]: s for s in self.state.get("plan", {}).get("segments", [])}
            for key in args.get("remove_ids", []):
                segments.pop(key, None)
            segments.update({s["id"]: copy.deepcopy(s) for s in args.get("upsert_segments", [])})
            self.state["plan"] = {"segments": list(segments.values())}
        if name == "set_region_program":
            self.state.setdefault("programs", {})[args["segment_id"]] = copy.deepcopy(args.get("program", {}))
        if name in ("render_preview", "render_final"):
            self.state["preview_output" if name == "render_preview" else "final_output"] = "output.mp4"
        if name == "inspect_frames":
            meta.update(artifact_kind=args["artifact"], inspected_frames=args["frames"], render_revision=self.state["plan_revision"])
            images = [{"path": "test.png", "label": "actual image", "source_frame": i, "crop_source_pixels": args.get("crop")} for i in args["frames"]]
        if name == "lookup_materials":
            return {"result": {"rows": [{"source": "PLAY", "finalized": ["시작"], "glossary": []}]}, "images": []}
        return {"result": {"metadata": meta}, "images": images}


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.executor = Executor(self.root)
        self.image_patch = patch("video_localizer.controller._image_part", return_value={"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA=="}})
        self.image_patch.start()
        self.addCleanup(self.image_patch.stop)

    def test_new_quality_policy_reaches_resumed_compact_requests_without_losing_work(self):
        from video_localizer.controller import SYSTEM, QUALITY_POLICY_REVISION, QUALITY_POLICY_SHA256
        controller = HunyuanController(Client([]), self.executor, self.root)
        controller.state.update(turns=4, accounted_turn=4, read_only_turns=9, output_recoveries=2,
            quality_policy_revision="old", quality_policy_sha256="old", compact_context=True)
        controller.state["messages"][0]["content"] = "Old policy"
        controller.state["messages"].extend([action("inspect_video", {}, "old-read"),
            {"role": "tool", "tool_call_id": "old-read", "content": "{}"}])
        controller.state["task_ledger"] = {"defect": {"status": "open", "description": "Unresolved defect"}}
        controller.state["reviews"] = [{"id": "human-1", "status": "pending", "frames": [0]}]
        controller.save()
        original_executor = copy.deepcopy(self.executor.snapshot())
        client = Client([action("record_work_notes", notes(), 5)])
        resumed = HunyuanController(client, self.executor, self.root, max_turns=1)
        self.assertEqual(resumed.state["read_only_turns"], 0)
        self.assertEqual(resumed.state["output_recoveries"], 2)
        self.assertTrue(resumed.state["force_checkpoint"])
        resumed.run()
        self.assertEqual(client.messages[0][0]["content"], SYSTEM)
        self.assertEqual(resumed.state["calls"][-1]["quality_policy_sha256"], QUALITY_POLICY_SHA256)
        self.assertEqual(resumed.state["calls"][-1]["quality_policy_revision"], QUALITY_POLICY_REVISION)
        self.assertEqual(resumed.state["task_ledger"]["defect"]["status"], "open")
        self.assertEqual(resumed.state["reviews"][0]["status"], "pending")
        self.assertEqual(self.executor.snapshot(), original_executor)
        resumed.state["read_only_turns"] = 8
        resumed.save()
        again = HunyuanController(Client([]), self.executor, self.root)
        self.assertEqual(again.state["read_only_turns"], 8)


    def save_stalled_write_checkpoint(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        controller.state.update(status="paused_error", error_code="stalled", turns=10, accounted_turn=10,
                                read_only_turns=10, source_frames_seen=[0], source_frames_delivered=[0],
                                source_seen_turn=0, last_notes_turn=10)
        controller.state["messages"].extend([action("set_edit_plan", {"segments": [segment("first")]}, "old-write"),
            {"role": "tool", "tool_call_id": "old-write", "content": json.dumps({"error": "fixed motion accepts exactly one keyframe"})}])
        # Emulate a checkpoint created before the bounded-correction policy existed.
        controller.state.pop("action_correction_turns", None)
        controller.state.pop("correction_mode", None)
        controller.save()

    def test_real_feedback_is_required_before_finishing(self):
        calls = [action("inspect_frames", {"artifact": "source", "frames": [0]}, 1),
                 action("set_edit_plan", {"segments": []}, 2),
                 action("render_preview", {}, 3),
                 action("inspect_frames", {"artifact": "preview", "frames": [0]}, 4),
                 action("render_final", {}, 5),
                 action("inspect_frames", {"artifact": "final", "frames": [0]}, 6),
                 action("finish", {"verdict": "pass", "summary": "checked", "coverage": "frame 0 only"}, 7)]
        client = Client(calls)
        result = HunyuanController(client, self.executor, self.root).run()
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["model_reviewed"])
        self.assertFalse(result["human_approved"])
        self.assertTrue(any(isinstance(m.get("content"), list) and any(p["type"] == "image_url" for p in m["content"])
                            for m in client.messages[-1]))

    def test_parallel_inspection_is_not_model_review(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        controller.state["turns"] = 3
        self.executor.state.update(plan_revision=1, final_output="final.mp4")
        controller._dispatch("inspect_frames", {"artifact": "final", "frames": [0]})
        with self.assertRaisesRegex(ValueError, "actual final"):
            controller._finish({"verdict": "pass", "summary": "ok", "coverage": "one frame"})

    def test_name_conflict_needs_exact_human_decision(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        segment = {"kind": "nameplate", "portrait_match": "mismatch", "source_text": "娜美", "target_text": "상디",
                   "start_frame": 270, "end_frame": 271}
        with self.assertRaisesRegex(ValueError, "Human decision"):
            controller._guard_plan({"segments": [segment]})
        controller.save()
        record_review(self.root, "review-1", {"action": "replace", "target_text": "상디", "start_frame": 270, "end_frame": 271})
        resumed = HunyuanController(Client([]), self.executor, self.root)
        resumed._guard_plan({"segments": [segment]})
        with self.assertRaisesRegex(ValueError, "Human decision"):
            resumed._guard_plan({"segments": [{**segment, "start_frame": 280}]})

    def test_approved_translation_cannot_be_silently_rewritten(self):
        self.executor.materials.translations["hello"] = ["안녕"]
        controller = HunyuanController(Client([]), self.executor, self.root)
        with self.assertRaisesRegex(ValueError, "Human decision"):
            controller._guard_plan({"segments": [{"source_text": "hello", "target_text": "다른", "start_frame": 0}]})

    def test_unresolved_calls_resume_without_new_model_decision(self):
        client = Client([action("finish", {"verdict": "no_change_needed", "summary": "no visible text", "coverage": "frame zero"}, 2)])
        controller = HunyuanController(client, self.executor, self.root)
        controller.state["messages"].append(action("inspect_frames", {"artifact": "source", "frames": [0]}, 1))
        controller.save()
        result = HunyuanController(client, self.executor, self.root).run()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.executor.executed, ["inspect_frames"])

    def test_api_failure_pauses_without_fallback(self):
        client = Client([])
        with patch.object(client, "complete", side_effect=TimeoutError("request timed out")):
            result = HunyuanController(client, self.executor, self.root).run()
        self.assertEqual(result["status"], "paused_error")
        self.assertEqual(self.executor.executed, [])

    def test_source_inspection_must_reach_model_before_plan(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        controller._dispatch("inspect_frames", {"artifact": "source", "frames": [0]})
        with self.assertRaisesRegex(ValueError, "Inspect source"):
            controller._dispatch("set_edit_plan", {"segments": []})
        controller.state["turns"] += 1
        controller._dispatch("set_edit_plan", {"segments": []})

    def test_no_change_verdict_cannot_share_source_inspection_turn(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        controller._dispatch("inspect_frames", {"artifact": "source", "frames": [0]})
        with self.assertRaisesRegex(ValueError, "Inspect real source"):
            controller._finish({"verdict": "no_change_needed", "summary": "clear", "coverage": "frame zero"})

    def test_incremental_plan_cannot_bypass_approved_translation_guard(self):
        self.executor.materials.translations["hello"] = ["안녕"]
        controller = HunyuanController(Client([]), self.executor, self.root)
        with self.assertRaisesRegex(ValueError, "Human decision"):
            controller._dispatch("update_edit_plan", {"upsert_segments": [{"source_text": "hello", "target_text": "다른", "start_frame": 0}]})
        self.assertNotIn("update_edit_plan", self.executor.executed)

    def test_final_boundary_frames_require_full_images(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        self.executor.state.update(plan_revision=1, final_output="final.mp4", plan={"segments": [{"start_frame": 0, "end_frame": 10}]})
        controller._dispatch("inspect_frames", {"artifact": "final", "frames": [0, 9], "crop": [0, 0, 10, 10]})
        controller.state["turns"] += 1
        with self.assertRaisesRegex(ValueError, "boundary frames"):
            controller._finish({"verdict": "pass", "summary": "checked", "coverage": "crops only"})
        controller._dispatch("inspect_frames", {"artifact": "final", "frames": [0, 9]})
        controller.state["turns"] += 1
        self.assertEqual(controller._finish({"verdict": "pass", "summary": "checked", "coverage": "full boundary frames"})["status"], "completed")

    def test_rerender_same_revision_invalidates_previous_image_review(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        self.executor.state.update(plan_revision=1, final_output="old.mp4")
        controller.state.update(turns=2, inspected_preview_revision=1, preview_seen_turn=0,
                                inspected_final_revision=1, final_seen_turn=0, final_frames_seen=[0])
        controller._dispatch("render_final", {})
        with self.assertRaisesRegex(ValueError, "actual final"):
            controller._finish({"verdict": "pass", "summary": "ok", "coverage": "old output"})
        controller._dispatch("render_preview", {})
        with self.assertRaisesRegex(ValueError, "actual preview"):
            controller._dispatch("render_final", {})

    def test_review_includes_inspected_frames_crops_and_materials(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        controller._dispatch("inspect_frames", {"artifact": "source", "frames": [0, 1]})
        controller._dispatch("inspect_frames", {"artifact": "source", "frames": [0], "crop": [0, 0, 10, 10]})
        controller._dispatch("lookup_materials", {})
        controller._review({"reason": "uncertain", "source_text": "PLAY", "proposed_text": "시작", "frames": [0], "evidence": "check", "recommendation": "confirm"})
        issue = controller.state["reviews"][0]
        self.assertEqual(len(issue["evidence_images"]), 2)
        self.assertEqual(issue["material_rows"][0]["finalized"], ["시작"])

    def test_retry_or_reject_does_not_authorize_conflicting_plan(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        segment = {"kind": "nameplate", "portrait_match": "unknown", "source_text": "name", "target_text": "target", "start_frame": 2, "end_frame": 3}
        controller.state["reviews"] = [{"status": "resolved", "source_text": "name", "frames": [2], "decision": {"action": "reject", "target_text": "target"}}]
        with self.assertRaisesRegex(ValueError, "Human decision"):
            controller._guard_plan({"segments": [segment]})

    def test_cancel_does_not_issue_model_request(self):
        client = Client([])
        result = HunyuanController(client, self.executor, self.root, cancelled=lambda: True).run()
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(client.calls, [])

    def test_tool_batch_cannot_save_images_that_will_not_reach_model(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        controller.state["pending_images"] = [{"type": "local_image", "path": "test.png"} for _ in range(16)]
        with self.assertRaisesRegex(ValueError, "Image budget"):
            controller._dispatch("inspect_frames", {"artifact": "source", "frames": [20]})
        self.assertEqual(self.executor.executed, [])

    def test_reinspection_labels_distinguish_fresh_images_from_omitted_history(self):
        for compact in (False, True):
            with self.subTest(compact=compact):
                root = self.root / str(compact)
                executor = Executor(root)
                crop = [0, 0, 10, 10]
                rounds = []
                for prefix in ("old", "fresh"):
                    response = action("inspect_frames", {"artifact": "source", "frames": [0, 1], "crop": crop}, prefix + "-0")
                    for index in range(1, 4):
                        response["tool_calls"].extend(action("inspect_frames",
                            {"artifact": "source", "frames": [0, 1], "crop": crop}, prefix + "-" + str(index))["tool_calls"])
                    # Only the unchanged protocol message carries this synthetic
                    # field; inspection labels must never copy model reasoning.
                    response["reasoning_content"] = "SYNTHETIC_PRIVATE_REASONING_DO_NOT_COPY"
                    rounds.append(response)
                client = Client(rounds + [action("inspect_video", {}, "metadata")])
                controller = HunyuanController(client, executor, root, max_turns=3)
                controller.state["compact_context"] = compact
                result = controller.run()
                parts = [part for message in client.messages[2] if isinstance(message.get("content"), list)
                         for part in message["content"]]
                placeholders = [part["text"] for part in parts if part.get("type") == "text"
                    and part.get("text", "").startswith("Historical image omitted from THIS request")]
                self.assertEqual(len(placeholders), 8)
                self.assertTrue(all("turn=1 tool_call=old-" in text for text in placeholders))
                self.assertTrue(all("not newer inspections of the same frame/crop" in text for text in placeholders))
                fresh_labels = [part["text"] for part in parts if part.get("type") == "text"
                    and part.get("text", "").startswith("Inspection turn=2 tool_call=fresh-")]
                self.assertEqual(len(set(fresh_labels)), 8)
                self.assertTrue(all("CROP source_pixels[x,y,width,height]=[0,0,10,10]" in text for text in fresh_labels))
                self.assertEqual(sum(part.get("type") == "image_url" for part in parts), 8)
                self.assertNotIn("SYNTHETIC_PRIVATE_REASONING_DO_NOT_COPY", json.dumps(placeholders + fresh_labels))
                persisted_parts = [part for message in controller.state["messages"] if isinstance(message.get("content"), list)
                                   for part in message["content"]]
                self.assertEqual(sum(part.get("type") == "local_image" for part in persisted_parts), 16)
                self.assertFalse(any(part.get("text", "").startswith("Historical image omitted") for part in persisted_parts))
                self.assertEqual(result["source_frames_delivered"], [0, 1])
                self.assertEqual(result["status"], "paused_budget")
                self.assertFalse(result["model_reviewed"])

    def test_inspection_labels_identify_full_frame_and_crop_without_changing_delivery(self):
        client = Client([action("inspect_frames", {"artifact": "source", "frames": [0]}, "full"),
                         action("inspect_frames", {"artifact": "source", "frames": [0], "crop": [1, 2, 10, 12]}, "crop"),
                         action("inspect_video", {}, "metadata")])
        controller = HunyuanController(client, self.executor, self.root, max_turns=3)
        controller.state["compact_context"] = True
        result = controller.run()
        parts = [part for message in client.messages[2] if isinstance(message.get("content"), list)
                 for part in message["content"]]
        labels = [part["text"] for part in parts if part.get("type") == "text"
                  and part.get("text", "").startswith("Inspection turn=")]
        self.assertEqual(len(labels), 2)
        self.assertIn("turn=1 tool_call=full artifact=source source_frame=0 FULL", labels[0])
        self.assertIn("turn=2 tool_call=crop artifact=source source_frame=0 CROP source_pixels[x,y,width,height]=[1,2,10,12]", labels[1])
        self.assertEqual(sum(part.get("type") == "image_url" for part in parts), 2)
        self.assertEqual(result["source_frames_delivered"], [0])
        self.assertFalse(result["model_reviewed"])

    def test_provider_may_reuse_tool_call_id_in_later_turn(self):
        client = Client([action("inspect_frames", {"artifact": "source", "frames": [0]}, 1),
                         action("finish", {"verdict": "no_change_needed", "summary": "clear", "coverage": "frame zero"}, 1)])
        result = HunyuanController(client, self.executor, self.root).run()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["source_frames_delivered"], [0])

    def test_successful_resume_clears_previous_api_error(self):
        client = Client([action("inspect_frames", {"artifact": "source", "frames": [0]}, 1),
                         action("finish", {"verdict": "no_change_needed", "summary": "clear", "coverage": "frame 0"}, 2)])
        controller = HunyuanController(client, self.executor, self.root)
        controller.state.update(status="paused_error", error_code="timeout", error_message="old error")
        result = controller.run()
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result["message"])

    def test_human_decision_waits_until_interrupted_tools_are_answered(self):
        client = Client([action("finish", {"verdict": "no_change_needed", "summary": "clear", "coverage": "frame 0"}, 2)])
        controller = HunyuanController(client, self.executor, self.root)
        controller.state["messages"].append(action("inspect_frames", {"artifact": "source", "frames": [0]}, 1))
        controller.state["reviews"] = [{"id": "review-1", "status": "pending", "source_text": "name", "proposed_text": "target", "frames": [0]}]
        controller.save()
        record_review(self.root, "review-1", {"action": "retry", "note": "inspect again"})
        resumed = HunyuanController(client, self.executor, self.root)
        self.assertEqual(resumed.run()["status"], "completed")
        messages = client.messages[0]
        tool_index = next(i for i, m in enumerate(messages) if m.get("tool_call_id") == "1")
        human_index = next(i for i, m in enumerate(messages) if isinstance(m.get("content"), str) and m["content"].startswith("Human review decision:"))
        self.assertLess(tool_index, human_index)

    def test_four_read_turns_require_a_hy4_notes_checkpoint(self):
        client = Client([action("inspect_video", {}, i) for i in range(4)] +
                        [action("record_work_notes", notes(), 4)])
        result = HunyuanController(client, self.executor, self.root, max_turns=5).run()
        self.assertEqual(result["status"], "paused_budget")
        self.assertEqual(result["read_only_turns"], 4)
        self.assertEqual(result["compactions"], 1)
        self.assertEqual([t["function"]["name"] for t in client.tools[-1]], ["record_work_notes"])
        self.assertEqual(client.options[-1]["thinking"], "disabled")
        self.assertEqual(client.options[-1]["tool_choice"],
                         {"type": "function", "function": {"name": "record_work_notes"}})

    def test_notes_preserve_omitted_tasks_and_pending_human_decisions(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        issue = {"id": "review-1", "status": "pending", "source_text": "name", "proposed_text": "target", "frames": [0]}
        controller.state["reviews"] = [copy.deepcopy(issue)]
        controller._dispatch("record_work_notes", notes(tasks=[task_record("region_a"), task_record("region_b")]))
        controller.state["source_frames_delivered"] = [0]
        controller._dispatch("record_work_notes", notes(tasks=[task_record("region_a", "done", frames=[0])]))
        self.assertEqual(controller.state["task_ledger"]["region_b"]["status"], "open")
        self.assertEqual(controller.state["reviews"], [issue])
        with self.assertRaisesRegex(ValueError, "Unfinished work"):
            controller._finish({"verdict": "pass", "summary": "ready", "coverage": "frame 0"})
        controller._dispatch("record_work_notes", notes(tasks=[task_record("region_b", "done", frames=[0])]))
        with self.assertRaisesRegex(ValueError, "Unresolved human reviews"):
            controller._finish({"verdict": "pass", "summary": "ready", "coverage": "frame 0"})

    def test_completed_task_requires_evidence_that_reached_hy4(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        controller.state["source_frames_seen"] = [0]
        with self.assertRaisesRegex(ValueError, "reached Hy4"):
            controller._dispatch("record_work_notes", notes(tasks=[task_record("region", "done", frames=[0])]))
        with self.assertRaisesRegex(ValueError, "existing segments"):
            controller._dispatch("record_work_notes", notes(tasks=[task_record("region", "done", segments=["missing"])]))
        self.assertEqual(controller.state["task_ledger"], {})

    def test_focus_is_bounded_by_source_duration_and_six_seconds(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        self.executor.state.update(media={"fps": 24}, source_frames=200)
        with self.assertRaisesRegex(ValueError, "six seconds"):
            controller._dispatch("record_work_notes", notes(end=145))
        with self.assertRaisesRegex(ValueError, "six seconds"):
            controller._dispatch("record_work_notes", notes(start=190, end=201))
        controller._dispatch("record_work_notes", notes(end=144))
        with self.assertRaisesRegex(ValueError, "Do not expand"):
            controller._dispatch("record_work_notes", notes(start=100, end=200))
        controller._dispatch("record_work_notes", notes(start=10, end=30))
        self.assertEqual(controller.state["work_notes"]["focus"]["start_frame"], 10)

    def test_focus_can_move_past_human_blocker_without_approving_it(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        controller._dispatch("record_work_notes", notes(tasks=[task_record("blocked_region")]))
        controller._dispatch("request_review", {"reason": "unreadable", "source_text": "?", "proposed_text": "",
            "frames": [10], "evidence": "The focused source needs a human decision.", "recommendation": "Inspect manually."})
        controller._dispatch("record_work_notes", notes(start=30, end=60))
        self.assertEqual(controller.state["reviews"][0]["status"], "pending")
        self.assertEqual(controller.state["task_ledger"]["blocked_region"]["status"], "open")
        self.assertEqual(controller.state["work_notes"]["focus"]["start_frame"], 30)

    def test_human_blocker_does_not_waive_pending_preview(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        controller._dispatch("record_work_notes", notes())
        self.executor.state.update(plan_revision=1, plan={"segments": [segment("pending")]})
        controller.state["pending_segment_ids"] = ["pending"]
        controller._dispatch("request_review", {"reason": "unreadable", "source_text": "?", "proposed_text": "",
            "frames": [10], "evidence": "Another region is uncertain.", "recommendation": "Inspect manually."})
        with self.assertRaisesRegex(ValueError, "Do not expand"):
            controller._dispatch("record_work_notes", notes(start=30, end=60))

    def test_unrelated_review_does_not_allow_focus_hopping(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        controller._dispatch("record_work_notes", notes())
        controller._dispatch("request_review", {"reason": "unreadable", "source_text": "?", "proposed_text": "",
            "frames": [50], "evidence": "A later interval is uncertain.", "recommendation": "Inspect manually."})
        with self.assertRaisesRegex(ValueError, "Do not expand"):
            controller._dispatch("record_work_notes", notes(start=30, end=60))

    def test_late_source_inspection_stays_in_focus_but_can_request_review(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        controller._dispatch("record_work_notes", notes(start=10, end=30))
        controller.state["read_only_turns"] = 6
        controller._dispatch("inspect_frames", {"artifact": "source", "frames": [10, 29]})
        with self.assertRaisesRegex(ValueError, "Stay inside"):
            controller._dispatch("inspect_frames", {"artifact": "source", "frames": [30]})
        result = controller._dispatch("request_review", {"reason": "unreadable", "source_text": "?", "proposed_text": "",
            "frames": [10], "evidence": "Focused source remains unreadable.", "recommendation": "Inspect manually."})
        self.assertEqual(result["result"]["status"], "pending")

    def test_ten_read_turns_pause_even_if_notes_are_refreshed_and_resumed(self):
        sequence = [action("inspect_video", {}, i) for i in range(4)]
        sequence += [action("record_work_notes", notes(), 4)]
        sequence += [action("inspect_video", {}, i) for i in range(5, 9)]
        sequence += [action("record_work_notes", notes(), 9)]
        sequence += [action("inspect_video", {}, i) for i in range(10, 12)]
        client = Client(sequence)
        result = HunyuanController(client, self.executor, self.root, max_turns=20).run()
        self.assertEqual((result["status"], result["error_code"]), ("paused_error", "stalled"))
        self.assertEqual(result["read_only_turns"], 10)
        self.assertEqual(len(client.calls), 12)
        resumed_client = Client([])
        result = HunyuanController(resumed_client, self.executor, self.root).run()
        self.assertEqual(result["error_code"], "stalled")
        self.assertEqual(resumed_client.calls, [])

    def test_plan_updates_are_small_and_stay_in_focus(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        controller.state.update(turns=2, source_frames_seen=[0], source_seen_turn=0)
        controller._dispatch("record_work_notes", notes())
        with self.assertRaisesRegex(ValueError, "at most four"):
            controller._dispatch("set_edit_plan", {"segments": [segment(str(i)) for i in range(5)]})
        with self.assertRaisesRegex(ValueError, "within the saved"):
            controller._dispatch("set_edit_plan", {"segments": [segment("outside", 20, 31)]})
        controller._dispatch("set_edit_plan", {"segments": [segment("first")]})
        self.assertEqual(controller.state["pending_segment_ids"], ["first"])
        with self.assertRaisesRegex(ValueError, "Preserve previous work"):
            controller._dispatch("set_edit_plan", {"segments": []})

    def test_plan_without_notes_still_limits_the_whole_group_to_six_seconds(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        self.executor.state.update(media={"fps": 24}, source_frames=300)
        controller.state.update(turns=2, source_frames_seen=[0], source_seen_turn=0)
        with self.assertRaisesRegex(ValueError, "six seconds"):
            controller._dispatch("set_edit_plan", {"segments": [segment("too_long", 0, 145)]})
        with self.assertRaisesRegex(ValueError, "six seconds"):
            controller._dispatch("set_edit_plan", {"segments": [segment("early", 0, 10), segment("late", 140, 150)]})
        self.assertEqual(self.executor.executed, [])
        controller._dispatch("set_edit_plan", {"segments": [segment("bounded", 0, 144)]})
        self.assertEqual(controller.state["pending_segment_ids"], ["bounded"])

    def test_expansion_requires_full_preview_boundaries_in_a_previous_model_turn(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        controller.state.update(turns=2, source_frames_seen=[0], source_seen_turn=0)
        controller._dispatch("record_work_notes", notes())
        controller._dispatch("set_edit_plan", {"segments": [segment("first")]})
        update = {"upsert_segments": [segment("second", 10, 20)]}
        with self.assertRaisesRegex(ValueError, "FULL boundary"):
            controller._dispatch("update_edit_plan", update)
        controller._dispatch("render_preview", {})
        controller._dispatch("inspect_frames", {"artifact": "preview", "frames": [0, 9], "crop": [0, 0, 10, 10]})
        controller.state["turns"] += 1
        with self.assertRaisesRegex(ValueError, "FULL boundary"):
            controller._dispatch("update_edit_plan", update)
        controller._dispatch("inspect_frames", {"artifact": "preview", "frames": [0]})
        controller.state["turns"] += 1
        with self.assertRaisesRegex(ValueError, "FULL boundary"):
            controller._dispatch("update_edit_plan", update)
        controller._dispatch("inspect_frames", {"artifact": "preview", "frames": [9]})
        with self.assertRaisesRegex(ValueError, "FULL boundary"):
            controller._dispatch("update_edit_plan", update)
        controller.state["turns"] += 1
        controller._dispatch("update_edit_plan", update)
        self.assertEqual(controller.state["pending_segment_ids"], ["second"])

    def test_region_program_cannot_expand_unreviewed_group(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        self.executor.state.update(plan_revision=1, plan={"segments": [segment("current"), segment("other", 10, 20)]})
        controller.state["pending_segment_ids"] = ["current"]
        with self.assertRaises(ValueError):
            controller._dispatch("set_region_program", {"segment_id": "other", "program": {}})
        self.assertNotIn("set_region_program", self.executor.executed)

    def test_boundary_repair_widens_focus_only_after_current_full_preview_feedback(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        controller.state.update(turns=2, source_frames_seen=[10], source_seen_turn=0)
        controller._dispatch("record_work_notes", notes(start=10, end=20))
        controller._dispatch("set_edit_plan", {"segments": [segment("same-region", 10, 20)]})
        update = {"upsert_segments": [segment("same-region", 9, 21)]}
        with self.assertRaisesRegex(ValueError, "FULL preview start/end-1"):
            controller._dispatch("update_edit_plan", update)
        with self.assertRaisesRegex(ValueError, "Do not expand"):
            controller._dispatch("record_work_notes", notes(start=9, end=21))
        controller._dispatch("render_preview", {})
        controller._dispatch("inspect_frames", {"artifact": "preview", "frames": [10, 19], "crop": [0, 0, 10, 10]})
        controller.state["turns"] += 1
        with self.assertRaisesRegex(ValueError, "Do not expand"):
            controller._dispatch("record_work_notes", notes(start=9, end=21))
        controller._dispatch("inspect_frames", {"artifact": "preview", "frames": [10, 19]})
        with self.assertRaisesRegex(ValueError, "prior model turn"):
            controller._dispatch("record_work_notes", notes(start=9, end=21))
        controller.state["turns"] += 1
        with self.assertRaisesRegex(ValueError, "First call record_work_notes"):
            controller._dispatch("update_edit_plan", update)
        controller._dispatch("record_work_notes", notes(start=9, end=21))
        state = controller._dispatch("get_work_state", {})["result"]
        update["expected_revision"] = state["plan_revision"]
        controller._dispatch("update_edit_plan", update)
        self.assertEqual(self.executor.state["plan"]["segments"], [segment("same-region", 9, 21)])
        self.assertEqual(controller.state["pending_segment_ids"], ["same-region"])
        self.assertFalse(controller._focus_expansion_allowed())

    def test_checkpoint_focus_hint_tracks_current_preview_feedback(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        controller.state.update(turns=2, source_frames_seen=[10], source_seen_turn=0)
        controller._dispatch("record_work_notes", notes(start=10, end=20))
        controller._dispatch("set_edit_plan", {"segments": [segment("same-region", 10, 20)]})
        controller.state["force_checkpoint"] = True
        instruction = controller._phase_instruction()
        self.assertIn("Keep the current focus unchanged until", instruction)
        self.assertIn("Omit unchanged tasks", instruction)
        self.assertFalse(json.loads(controller._messages()[-1]["content"])["focus_expansion_allowed"])
        controller._dispatch("render_preview", {})
        controller._dispatch("inspect_frames", {"artifact": "preview", "frames": [10, 19]})
        controller.state["turns"] += 1
        self.assertTrue(controller.state["focus_locked"])
        instruction = controller._phase_instruction()
        self.assertIn("Focus expansion is allowed", instruction)
        self.assertIn("record_work_notes within six seconds, then get_work_state and update_edit_plan", instruction)
        self.assertNotIn("Keep the current focus unchanged", instruction)
        self.assertTrue(json.loads(controller._messages()[-1]["content"])["focus_expansion_allowed"])
        controller.state["inspected_preview_revision"] -= 1
        self.assertIn("Keep the current focus unchanged until", controller._phase_instruction())

    def test_unchanged_completed_task_can_be_omitted_after_empty_evidence_error(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        controller.state["source_frames_delivered"] = [0]
        saved_task = task_record("already_done", "done", frames=[0])
        controller._dispatch("record_work_notes", notes(tasks=[saved_task]))
        with self.assertRaisesRegex(ValueError, "already_done.*Omit this task if unchanged"):
            controller._dispatch("record_work_notes", notes(tasks=[task_record("already_done", "done")]))
        controller._dispatch("record_work_notes", notes())
        self.assertEqual(controller.state["task_ledger"]["already_done"], saved_task)

    def test_next_request_focus_hint_matches_execution_without_allowing_sibling_tool_bypass(self):
        same_turn = action("inspect_frames", {"artifact": "preview", "frames": [10, 19]}, "inspect")
        same_turn["tool_calls"] += action("record_work_notes", notes(start=9, end=21), "too-early")["tool_calls"]
        client = Client([same_turn, action("record_work_notes", notes(start=9, end=21), "after-images")])
        controller = HunyuanController(client, self.executor, self.root, max_turns=2)
        controller.state.update(turns=2, source_frames_seen=[10], source_seen_turn=0)
        controller._dispatch("record_work_notes", notes(start=10, end=20))
        controller._dispatch("set_edit_plan", {"segments": [segment("same-region", 10, 20)]})
        controller._dispatch("render_preview", {})
        result = controller.run()
        first, second = [json.loads(messages[-1]["content"]) for messages in client.messages]
        self.assertFalse(first["focus_expansion_allowed"])
        self.assertEqual(first["controller_phase"], "preview")
        self.assertTrue(second["focus_expansion_allowed"])
        self.assertEqual(second["controller_phase"], "local_edit")
        self.assertEqual(sum(part.get("type") == "image_url" for message in client.messages[1]
            if isinstance(message.get("content"), list) for part in message["content"]), 2)
        too_early = next(message for message in controller.state["messages"] if message.get("tool_call_id") == "too-early")
        self.assertIn("prior model turn", json.loads(too_early["content"])["error"])
        self.assertEqual(controller.state["work_notes"]["focus"], notes(start=9, end=21)["focus"])
        self.assertEqual(controller.state["turns"], 4)
        self.assertEqual(result["status"], "paused_budget")

    def test_output_limit_recovery_checkpoint_uses_incoming_preview_feedback_for_hint(self):
        client = Client([action("inspect_frames", {"artifact": "preview", "frames": [10, 19]}, "inspect"),
            HunyuanError("output_limit", "synthetic truncation"),
            action("record_work_notes", notes(start=9, end=21), "recovery-notes")])
        controller = HunyuanController(client, self.executor, self.root, max_turns=3)
        controller.state.update(turns=2, source_frames_seen=[10], source_seen_turn=0)
        controller._dispatch("record_work_notes", notes(start=10, end=20))
        controller._dispatch("set_edit_plan", {"segments": [segment("same-region", 10, 20)]})
        controller._dispatch("render_preview", {})
        controller.run()
        hints = [json.loads(messages[-1]["content"]) for messages in client.messages]
        self.assertEqual([hint["focus_expansion_allowed"] for hint in hints], [False, True, True])
        self.assertEqual([hint["controller_phase"] for hint in hints], ["preview", "local_edit", "checkpoint"])
        self.assertIn("Focus expansion is allowed", hints[2]["instruction"])
        self.assertNotIn("Keep the current focus unchanged", hints[2]["instruction"])
        self.assertEqual(controller.state["output_recoveries"], 1)
        self.assertEqual(controller.state["turns"], 4)
        self.assertEqual(controller.state["work_notes"]["focus"], notes(start=9, end=21)["focus"])

    def test_no_change_cannot_discard_open_task_ledger(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        controller.state.update(turns=2, source_frames_delivered=[0], source_seen_turn=0,
                                task_ledger={"unread": task_record("unread")})
        with self.assertRaisesRegex(ValueError, "[Uu]nfinished|[Oo]pen|[Ee]xisting"):
            controller._finish({"verdict": "no_change_needed", "summary": "clear", "coverage": "frame 0"})

    def test_output_limit_recovers_via_checkpoint_before_full_normal_request(self):
        client = Client([HunyuanError("output_limit", "truncated"),
                         action("record_work_notes", notes(), 1), action("inspect_video", {}, 2)])
        result = HunyuanController(client, self.executor, self.root, max_turns=3).run()
        self.assertEqual(result["status"], "paused_budget")
        self.assertEqual(result["turns"], 2)
        self.assertEqual(result["model_calls"], 3)
        self.assertEqual(result["output_recoveries"], 1)
        self.assertEqual(client.options[0]["max_tokens"], MAX_OUTPUT_TOKENS)
        self.assertEqual(client.options[0]["timeout"], MAX_REQUEST_TIMEOUT)
        self.assertEqual(client.options[1]["thinking"], "disabled")
        self.assertEqual([t["function"]["name"] for t in client.tools[1]], ["record_work_notes"])
        self.assertEqual(client.options[2]["max_tokens"], MAX_OUTPUT_TOKENS)
        self.assertEqual(client.options[2]["timeout"], MAX_REQUEST_TIMEOUT)
        self.assertNotIn("thinking", client.options[2])
        self.assertEqual(self.executor.executed, ["inspect_video"])

    def test_output_recovery_limit_persists_across_resumes(self):
        for count in (1, 2):
            client = Client([HunyuanError("output_limit", "truncated")])
            result = HunyuanController(client, self.executor, self.root, max_turns=1).run()
            self.assertEqual(result["status"], "paused_budget")
            self.assertEqual(result["output_recoveries"], count)
        client = Client([HunyuanError("output_limit", "truncated")])
        result = HunyuanController(client, self.executor, self.root, max_turns=5).run()
        self.assertEqual((result["status"], result["error_code"]), ("paused_error", "output_limit"))
        self.assertEqual(result["output_recoveries"], 2)
        self.assertEqual(result["model_calls"], 3)
        self.assertEqual(result["turns"], 0)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.executor.executed, [])
        self.assertEqual(client.options[0]["max_tokens"], 16384)
        self.assertEqual(client.options[0]["thinking"], "disabled")

    def save_legacy_budget_failure(self, *, recoveries=1):
        controller = HunyuanController(Client([]), self.executor, self.root)
        controller.state.update(status="paused_error", error_code="invalid_response",
            error_message="Hy4 tool arguments are not JSON.", output_recoveries=recoveries,
            max_output_tokens=32768, calls=[legacy_budget_failure()],
            task_ledger={"unfinished": task_record("unfinished")})
        controller.save()
        return controller

    def test_legacy_invalid_json_at_budget_resumes_with_checkpoint_and_keeps_audit(self):
        saved = self.save_legacy_budget_failure()
        original_calls = copy.deepcopy(saved.state["calls"])
        client = Client([action("record_work_notes", notes(), 1)])
        resumed = HunyuanController(client, self.executor, self.root, max_turns=1)
        result = resumed.run()
        self.assertEqual((result["status"], result["output_recoveries"]), ("paused_budget", 2))
        self.assertEqual(resumed.state["calls"][:-1], original_calls)
        self.assertEqual(client.options, [{"thinking": "disabled",
            "tool_choice": {"type": "function", "function": {"name": "record_work_notes"}},
            "max_tokens": 16384, "timeout": 180}])
        self.assertEqual([t["function"]["name"] for t in client.tools[0]], ["record_work_notes"])
        self.assertEqual(self.executor.executed, [])
        self.assertEqual(resumed.state["task_ledger"]["unfinished"]["status"], "open")
        self.assertIsNone(result["output"])
        self.assertFalse(result["model_reviewed"])
        record = next(iter(resumed.state["reclassified_output_limit_calls"].values()))
        self.assertEqual(record["call_index"], 1)
        self.assertEqual(record["request"], 15)
        self.assertEqual(record["original_error_code"], "invalid_response")
        self.assertEqual(record["original_error_message"], "Hy4 tool arguments are not JSON.")
        self.assertEqual(record["disposition"], "recovery_scheduled")
        self.assertFalse(record["partial_actions_executed"])

    def test_legacy_reclassification_requires_exact_error_and_reliable_last_call(self):
        variants = [
            ("state", "status", "paused_budget"), ("state", "error_code", "network_timeout"),
            ("state", "error_message", "Hy4 tool arguments must be an object."),
            ("call", "status", "completed"), ("call", "stage", "response_body"),
            ("call", "http_status", 500), ("call", "finish_reason", "stop"),
            ("call", "max_tokens", True), ("call", "max_tokens", "32768"),
            ("call", "usage", None),
            ("call", "usage", {"prompt_tokens": 100, "completion_tokens": 32767, "total_tokens": 32867}),
            ("call", "usage", {"prompt_tokens": 100, "completion_tokens": 32768, "total_tokens": 1}),
            ("call", "usage", {"prompt_tokens": 100, "completion_tokens": "32768", "total_tokens": 32868}),
        ]
        for area, key, value in variants:
            with self.subTest(area=area, key=key, value=value):
                controller = self.save_legacy_budget_failure()
                target = controller.state if area == "state" else controller.state["calls"][-1]
                target[key] = value
                before = copy.deepcopy(controller.state)
                self.assertIsNone(controller._recover_saved_output_limit())
                self.assertEqual(controller.state, before)
                self.assertEqual(controller.client.calls, [])
        controller = self.save_legacy_budget_failure()
        controller.state["calls"] = []
        self.assertIsNone(controller._recover_saved_output_limit())
        controller.state["calls"] = [legacy_budget_failure(), {"status": "completed"}]
        self.assertIsNone(controller._recover_saved_output_limit())
        self.assertEqual(self.executor.executed, [])

    def test_legacy_reclassification_does_not_touch_an_unfinished_tool_round(self):
        controller = self.save_legacy_budget_failure()
        controller.state["messages"].append(action("set_edit_plan", {"segments": []}, "pending"))
        before = copy.deepcopy(controller.state)
        self.assertIsNone(controller._recover_saved_output_limit())
        self.assertEqual(controller.state, before)
        self.assertEqual(self.executor.executed, [])
        self.assertEqual(controller.client.calls, [])

    def test_legacy_recovery_marker_and_counter_survive_interruption_atomically(self):
        self.save_legacy_budget_failure()
        def interrupt_after_save(event):
            if event["stage"] == "hunyuan_recovery":
                raise RuntimeError("synthetic interruption after recovery state save")
        interrupted = HunyuanController(Client([]), self.executor, self.root, progress=interrupt_after_save)
        with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
            interrupted.run()
        persisted = json.loads(interrupted.path.read_text(encoding="utf-8"))
        self.assertEqual((persisted["status"], persisted["output_recoveries"]), ("paused_error", 2))
        self.assertEqual(len(persisted["reclassified_output_limit_calls"]), 1)
        self.assertTrue(persisted["force_checkpoint"])
        self.assertEqual(interrupted.client.calls, [])
        client = Client([action("record_work_notes", notes(), 1)])
        resumed = HunyuanController(client, self.executor, self.root, max_turns=1)
        result = resumed.run()
        self.assertEqual(result["output_recoveries"], 2)
        self.assertEqual(client.options[0]["thinking"], "disabled")
        self.assertEqual(len(resumed.state["reclassified_output_limit_calls"]), 1)
        events = [json.loads(line) for line in (self.root / "decisions.jsonl").read_text("utf-8").splitlines()]
        self.assertEqual(sum(item["event"] == "saved_response_reclassified" for item in events), 1)
        self.assertEqual(sum(item["event"] == "output_limit_recovery" for item in events), 1)
        self.assertEqual(self.executor.executed, [])

    def test_exhausted_legacy_recovery_budget_preserves_pause_without_api_retry(self):
        saved = self.save_legacy_budget_failure(recoveries=2)
        original_calls = copy.deepcopy(saved.state["calls"])
        for _ in range(2):
            client = Client([])
            resumed = HunyuanController(client, self.executor, self.root)
            result = resumed.run()
            self.assertEqual((result["status"], result["error_code"]), ("paused_error", "invalid_response"))
            self.assertEqual(result["message"], "Hy4 tool arguments are not JSON.")
            self.assertEqual(result["recovery_blocker"], "output_limit_recovery_budget_exhausted")
            self.assertEqual(result["output_recoveries"], 2)
            self.assertEqual(resumed.state["max_output_tokens"], MAX_OUTPUT_TOKENS)
            self.assertEqual(resumed.state["calls"], original_calls)
            self.assertEqual(client.calls, [])
            self.assertEqual(client.options, [])
            self.assertEqual(self.executor.executed, [])
        events = [json.loads(line) for line in (self.root / "decisions.jsonl").read_text("utf-8").splitlines()]
        self.assertEqual(sum(item["event"] == "saved_response_reclassified" for item in events), 1)
        self.assertFalse(any(item["event"] == "output_limit_recovery" for item in events))

    def test_separate_legacy_failures_share_the_existing_recovery_limit(self):
        controller = self.save_legacy_budget_failure(recoveries=0)
        self.assertTrue(controller._recover_saved_output_limit())
        self.assertTrue(controller._recover_saved_output_limit())
        self.assertEqual(controller.state["output_recoveries"], 1)
        # Providers may reuse request IDs after a process restart; the append-only
        # audit index still distinguishes the next failed request.
        controller.state["calls"].append(legacy_budget_failure())
        self.assertTrue(controller._recover_saved_output_limit())
        self.assertEqual(controller.state["output_recoveries"], 2)
        controller.state["calls"].append(legacy_budget_failure())
        self.assertFalse(controller._recover_saved_output_limit())
        self.assertEqual(controller.state["output_recoveries"], 2)
        self.assertEqual(len(controller.state["reclassified_output_limit_calls"]), 3)
        self.assertEqual(controller.client.calls, [])
        self.assertEqual(self.executor.executed, [])

    def test_old_normal_budget_migrates_without_resetting_history_or_recoveries(self):
        saved = self.save_legacy_budget_failure(recoveries=2)
        original_calls = copy.deepcopy(saved.state["calls"])
        client = Client([])
        migrated = HunyuanController(client, self.executor, self.root)
        self.assertEqual(migrated.state["max_output_tokens"], MAX_OUTPUT_TOKENS)
        self.assertEqual(migrated.state["output_recoveries"], 2)
        self.assertEqual(migrated.state["calls"], original_calls)
        self.assertEqual(migrated.state["status"], "paused_error")
        HunyuanController(Client([]), self.executor, self.root)
        events = [json.loads(line) for line in (self.root / "decisions.jsonl").read_text("utf-8").splitlines()]
        self.assertEqual(sum(item["event"] == "normal_output_budget_updated" for item in events), 1)

    def test_non_output_api_error_does_not_retry_or_execute(self):
        client = Client([HunyuanError("timeout", "timed out", retryable=True)])
        result = HunyuanController(client, self.executor, self.root).run()
        self.assertEqual((result["status"], result["error_code"]), ("paused_error", "timeout"))
        self.assertEqual(result["output_recoveries"], 0)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.executor.executed, [])

    def test_legacy_output_limit_migrates_to_compact_checkpoint_without_erasing_audit(self):
        controller = HunyuanController(Client([]), self.executor, self.root, request="Retain the original task.")
        for i in range(25):
            controller.state["messages"].extend([action("inspect_video", {}, i),
                {"role": "tool", "tool_call_id": str(i), "content": json.dumps({"observations": "x" * 2000})}])
        controller.state.pop("orchestration_version")
        controller.state.update(status="paused_error", error_code="output_limit", turns=25)
        controller.save()
        original_messages = len(controller.state["messages"])
        client = Client([action("record_work_notes", notes(), 26)])
        resumed = HunyuanController(client, self.executor, self.root, max_turns=1)
        result = resumed.run()
        self.assertEqual(result["status"], "paused_budget")
        self.assertEqual(client.options[0]["thinking"], "disabled")
        self.assertLess(len(json.dumps(client.messages[0])), len(json.dumps(controller.state["messages"])))
        self.assertIn("Retain the original task.", json.dumps(client.messages[0]))
        self.assertGreaterEqual(len(resumed.state["messages"]), original_messages)

    def test_archived_source_context_is_not_counted_as_new_visual_evidence(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        archived = {"type": "local_image", "path": "test.png", "label": "archived", "artifact": "source",
                    "source_frame": 42, "archived_context": True, "review_evidence": False}
        with patch("video_localizer.controller.build_context", return_value=[{"role": "user", "content": [archived]}]):
            controller._messages()
        self.assertEqual(controller._source_images_in_request, [])

    def test_invalid_notes_release_three_regular_tool_turns_before_checkpoint_returns(self):
        invalid_notes = notes(end=181)
        client = Client([action("record_work_notes", invalid_notes, 1),
                         action("record_work_notes", invalid_notes, 2),
                         action("inspect_video", {}, 3), action("inspect_video", {}, 4),
                         action("inspect_video", {}, 5), action("record_work_notes", notes(), 6)])
        controller = HunyuanController(client, self.executor, self.root, max_turns=6)
        controller.state.update(force_checkpoint=True, read_only_turns=4)
        result = controller.run()
        self.assertEqual(result["status"], "paused_budget")
        names = [[tool["function"]["name"] for tool in schemas] for schemas in client.tools]
        self.assertEqual(names[:2], [["record_work_notes"], ["record_work_notes"]])
        for i in range(2, 5):
            self.assertIn("request_review", names[i])
            self.assertNotIn("thinking", client.options[i])
        self.assertEqual(names[5], ["record_work_notes"])
        self.assertEqual(client.options[5]["thinking"], "disabled")
        self.assertEqual(result["read_only_turns"], 7)
        self.assertEqual(result["compactions"], 1)
        self.assertEqual(controller.state["checkpoint_failures"], 0)
        self.assertEqual(controller.state["checkpoint_defer_until"], 0)
        self.assertEqual(self.executor.executed, ["inspect_video"] * 3)

    def test_failed_notes_counts_and_defer_window_survive_resume_without_fake_progress(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        issue = {"id": "review-1", "status": "pending", "source_text": "?", "proposed_text": "", "frames": [0]}
        ledger = {"unfinished": task_record("unfinished")}
        controller.state.update(force_checkpoint=True, read_only_turns=4, reviews=[copy.deepcopy(issue)],
                                task_ledger=copy.deepcopy(ledger))
        controller.save()
        for attempt in (1, 2):
            client = Client([action("record_work_notes", notes(end=181), attempt)])
            resumed = HunyuanController(client, self.executor, self.root, max_turns=1)
            result = resumed.run()
            self.assertEqual(result["status"], "paused_budget")
            self.assertEqual(result["read_only_turns"], 4)
            self.assertEqual(result["compactions"], 0)
            self.assertEqual(resumed.state["work_notes"], {})
            self.assertEqual(resumed.state["reviews"], [issue])
            self.assertEqual(resumed.state["task_ledger"], ledger)
            self.assertFalse(resumed.state["turn_has_progress"])
            self.assertEqual(resumed.state["checkpoint_failures"], 1 if attempt == 1 else 0)
            self.assertEqual([t["function"]["name"] for t in client.tools[0]], ["record_work_notes"])
        self.assertEqual(resumed.state["checkpoint_defer_until"], 5)
        for turn in (3, 4, 5):
            client = Client([action("inspect_video", {}, turn)])
            resumed = HunyuanController(client, self.executor, self.root, max_turns=1)
            result = resumed.run()
            self.assertEqual(result["status"], "paused_budget")
            self.assertEqual(result["turns"], turn)
            self.assertIn("request_review", [t["function"]["name"] for t in client.tools[0]])
            self.assertEqual(resumed.state["checkpoint_defer_until"], 5)
        client = Client([action("record_work_notes", notes(), 6)])
        resumed = HunyuanController(client, self.executor, self.root, max_turns=1)
        resumed.run()
        self.assertEqual([t["function"]["name"] for t in client.tools[0]], ["record_work_notes"])
        self.assertEqual(resumed.state["checkpoint_defer_until"], 0)
        self.assertEqual(resumed.state["reviews"], [issue])
        self.assertEqual(resumed.state["task_ledger"], ledger)

    def test_successful_notes_clear_a_single_prior_checkpoint_failure(self):
        client = Client([action("record_work_notes", notes(end=181), 1),
                         action("record_work_notes", notes(), 2)])
        controller = HunyuanController(client, self.executor, self.root, max_turns=2)
        controller.state.update(force_checkpoint=True, read_only_turns=4)
        result = controller.run()
        self.assertEqual(result["status"], "paused_budget")
        self.assertEqual(result["compactions"], 1)
        self.assertEqual(result["read_only_turns"], 4)
        self.assertEqual(controller.state["checkpoint_failures"], 0)
        self.assertEqual(controller.state["checkpoint_defer_until"], 0)

    def test_old_stalled_failed_write_can_be_corrected_only_by_a_new_hy4_action(self):
        self.save_stalled_write_checkpoint()
        corrected = {"segments": [segment("hy4_corrected", 0, 10)]}
        client = Client([action("set_edit_plan", corrected, 11)])
        controller = HunyuanController(client, self.executor, self.root, max_turns=1)
        self.assertEqual(self.executor.state["plan_revision"], 0)
        self.assertEqual(self.executor.executed, [])
        result = controller.run()
        self.assertEqual(result["status"], "paused_budget")
        self.assertEqual(self.executor.state["plan"], corrected)
        self.assertEqual(self.executor.executed, ["set_edit_plan"])
        self.assertIn("request_review", [t["function"]["name"] for t in client.tools[0]])
        self.assertNotIn("thinking", client.options[0])
        self.assertIn("correcting_action", client.messages[0][-1]["content"])
        self.assertEqual(result["read_only_turns"], 0)
        self.assertEqual(controller.state["action_correction_turns"], 0)
        self.assertFalse(controller.state["correction_mode"])

    def test_two_failed_corrections_pause_without_changing_the_plan(self):
        self.save_stalled_write_checkpoint()
        invalid = {"segments": [segment(str(i)) for i in range(5)]}
        client = Client([action("set_edit_plan", invalid, 11), action("set_edit_plan", invalid, 12)])
        controller = HunyuanController(client, self.executor, self.root, max_turns=5)
        original = copy.deepcopy(self.executor.snapshot())
        result = controller.run()
        self.assertEqual((result["status"], result["error_code"]), ("paused_error", "stalled"))
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(controller.state["action_correction_turns"], 2)
        self.assertEqual(result["read_only_turns"], 12)
        self.assertEqual(self.executor.snapshot(), original)
        self.assertEqual(self.executor.executed, [])
        attempted = [json.loads(m["tool_calls"][0]["function"]["arguments"]) for m in controller.state["messages"]
                     if m.get("role") == "assistant" and m.get("tool_calls", [{}])[0].get("id") in ("11", "12")]
        self.assertEqual(attempted, [invalid, invalid])

    def test_action_correction_allowance_is_not_renewed_by_resume(self):
        self.save_stalled_write_checkpoint()
        invalid = {"segments": [segment(str(i)) for i in range(5)]}
        for count in (1, 2):
            client = Client([action("set_edit_plan", invalid, 10 + count)])
            controller = HunyuanController(client, self.executor, self.root, max_turns=1)
            result = controller.run()
            self.assertEqual(result["status"], "paused_budget")
            self.assertEqual(controller.state["action_correction_turns"], count)
            self.assertEqual(len(client.calls), 1)
        client = Client([])
        result = HunyuanController(client, self.executor, self.root).run()
        self.assertEqual((result["status"], result["error_code"]), ("paused_error", "stalled"))
        self.assertEqual(client.calls, [])
        self.assertEqual(self.executor.executed, [])

    def test_notes_do_not_reset_or_count_as_successful_action_correction(self):
        self.save_stalled_write_checkpoint()
        client = Client([action("record_work_notes", notes(), 11), action("record_work_notes", notes(), 12)])
        controller = HunyuanController(client, self.executor, self.root, max_turns=3)
        result = controller.run()
        self.assertEqual((result["status"], result["error_code"]), ("paused_error", "stalled"))
        self.assertEqual(controller.state["action_correction_turns"], 2)
        self.assertEqual(result["read_only_turns"], 10)
        self.assertEqual(result["compactions"], 2)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(self.executor.executed, [])

    def test_reads_after_a_failed_write_do_not_hide_the_bounded_correction(self):
        self.save_stalled_write_checkpoint()
        controller = HunyuanController(Client([]), self.executor, self.root)
        for tool in ("inspect_frames", "get_work_state"):
            controller.state["messages"].extend([action(tool, {}, "later-" + tool),
                {"role": "tool", "tool_call_id": "later-" + tool, "content": json.dumps({"metadata": {}})}])
        controller.save()
        corrected = {"segments": [segment("corrected")]}
        client = Client([action("set_edit_plan", corrected, "correction")])
        controller = HunyuanController(client, self.executor, self.root, max_turns=1)
        self.assertTrue(controller._last_write_failed())
        result = controller.run()
        self.assertEqual(result["status"], "paused_budget")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.executor.state["plan"], corrected)
        self.assertIn("correcting_action", client.messages[0][-1]["content"])
        # Only the successful corrected write, not the intervening reads, resets it.
        self.assertEqual(controller.state["action_correction_turns"], 0)

    def test_latest_write_result_supersedes_older_failures_in_and_across_rounds(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        prefix = copy.deepcopy(controller.state["messages"])
        failed = {"ok": False, "error": "synthetic render failure"}
        for one_round in (False, True):
            for last_failed in (False, True):
                with self.subTest(one_round=one_round, last_failed=last_failed):
                    first = action("update_edit_plan", {}, "first")
                    last = action("render_preview", {}, "last")
                    first_result, last_result = ({"ok": True}, failed) if last_failed else (failed, {"ok": True})
                    if one_round:
                        first["tool_calls"].extend(last["tool_calls"])
                        history = [first]
                    else:
                        history = [first, {"role": "tool", "tool_call_id": "first", "content": json.dumps(first_result)}, last]
                    if one_round:
                        history.append({"role": "tool", "tool_call_id": "first", "content": json.dumps(first_result)})
                    history.append({"role": "tool", "tool_call_id": "last", "content": json.dumps(last_result)})
                    history.extend([action("get_work_state", {}, "read"),
                        {"role": "tool", "tool_call_id": "read", "content": json.dumps({"error": "read failure is not a write"})}])
                    controller.state["messages"] = prefix + history
                    self.assertEqual(controller._last_write_failed(), last_failed)

    def test_failed_write_lookup_scopes_reused_ids_to_their_assistant_round(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        prefix = copy.deepcopy(controller.state["messages"])
        for failed in (False, True):
            with self.subTest(failed=failed):
                controller.state["messages"] = prefix + [action("render_preview", {}, "reused"),
                    {"role": "tool", "tool_call_id": "reused", "content": json.dumps({"ok": not failed})},
                    action("get_work_state", {}, "reused"),
                    {"role": "tool", "tool_call_id": "reused", "content": json.dumps({"ok": failed})}]
                self.assertEqual(controller._last_write_failed(), failed)

    def test_unfinished_or_invalid_newer_write_cannot_inherit_a_previous_failure(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        prefix = copy.deepcopy(controller.state["messages"]) + [action("render_preview", {}, "old"),
            {"role": "tool", "tool_call_id": "old", "content": json.dumps({"ok": False})}]
        for content in (None, "not-json", "[]", "null"):
            with self.subTest(content=content):
                controller.state["messages"] = prefix + [action("update_edit_plan", {}, "pending")]
                if content is not None:
                    controller.state["messages"].append({"role": "tool", "tool_call_id": "pending", "content": content})
                self.assertFalse(controller._last_write_failed())
        multiple = action("update_edit_plan", {}, "failed")
        multiple["tool_calls"].extend(action("render_preview", {}, "missing")["tool_calls"])
        controller.state["messages"] = prefix + [multiple,
            {"role": "tool", "tool_call_id": "failed", "content": json.dumps({"ok": False})}]
        self.assertFalse(controller._last_write_failed())

    def test_read_only_correction_requests_still_exhaust_the_existing_two_request_cap(self):
        self.save_stalled_write_checkpoint()
        client = Client([action("inspect_video", {}, 11), action("get_work_state", {}, 12)])
        controller = HunyuanController(client, self.executor, self.root, max_turns=5)
        result = controller.run()
        self.assertEqual((result["status"], result["error_code"]), ("paused_error", "stalled"))
        self.assertEqual(controller.state["action_correction_turns"], 2)
        self.assertEqual(len(client.calls), 2)
        resumed_client = Client([])
        resumed = HunyuanController(resumed_client, self.executor, self.root)
        self.assertEqual(resumed.run()["error_code"], "stalled")
        self.assertEqual(resumed.state["action_correction_turns"], 2)
        self.assertEqual(resumed_client.calls, [])

    def test_failed_preview_hint_allows_same_id_repair_without_waiving_expansion(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        controller._record_notes(notes())
        controller.state.update(turns=2, source_seen_turn=0, source_frames_seen=[0])
        controller._dispatch("set_edit_plan", {"segments": [segment("pending")]})
        controller.state["messages"].extend([action("render_preview", {}, "failed-preview"),
            {"role": "tool", "tool_call_id": "failed-preview", "content": json.dumps({"ok": False, "error": "synthetic layout failure"})}])
        instruction = controller._phase_instruction()
        work = controller._work_state({"segment_ids": ["pending"]})
        self.assertIn("same-ID corrections do not require a successful preview first", instruction)
        self.assertIn("even after render_preview failed", work["repair_guidance"])
        self.assertEqual(work["pending_segment_ids"], ["pending"])
        controller._dispatch("update_edit_plan", {"upsert_segments": [segment("pending", 0, 11)]})
        controller._dispatch("set_region_program", {"segment_id": "pending", "program": {}})
        self.assertEqual(self.executor.executed[-2:], ["update_edit_plan", "set_region_program"])
        self.assertFalse(controller._focus_expansion_allowed())
        with self.assertRaisesRegex(ValueError, "FULL boundary"):
            controller._dispatch("update_edit_plan", {"upsert_segments": [segment("new-id")]})
        with self.assertRaisesRegex(ValueError, "FULL preview"):
            controller._record_notes(notes(end=31))
        with self.assertRaisesRegex(ValueError, "actual preview"):
            controller._dispatch("render_final", {})


    def test_identical_preview_after_reads_does_not_reset_analysis_allowance(self):
        args = {"expected_revision": 0, "start_frame": 0, "end_frame": 30}
        client = Client([action("render_preview", args, 1),
                         action("inspect_frames", {"artifact": "preview", "frames": [0, 29]}, 2),
                         action("get_work_state", {}, 3), action("render_preview", args, 4)])
        controller = HunyuanController(client, self.executor, self.root, max_turns=4)
        result = controller.run()
        self.assertEqual(result["read_only_turns"], 3)
        self.assertEqual(self.executor.executed.count("render_preview"), 2)
        reply = json.loads(controller.state["messages"][-1]["content"])
        self.assertTrue(reply["repeated_successful_render"])
        self.assertIn("Inspect the current actual artifact", reply["next_action"])
        # Actual re-rendering must not retain inspection approval of the old file.
        self.assertIsNone(controller.state["inspected_preview_revision"])
        self.assertEqual(controller.state["preview_frames_seen"], [])

    def test_repeated_final_still_requires_new_actual_final_inspection(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        controller.state.update(turns=3, inspected_preview_revision=0, preview_seen_turn=1)
        controller._dispatch("render_final", {"expected_revision": 0})
        controller.state.update(inspected_final_revision=0, final_seen_turn=1, final_frames_seen=[0, 29])
        result = controller._dispatch("render_final", {"expected_revision": 0})["result"]
        self.assertTrue(result["repeated_successful_render"])
        self.assertIsNone(controller.state["inspected_final_revision"])
        self.assertEqual(controller.state["final_frames_seen"], [])
        with self.assertRaisesRegex(ValueError, "actual final"):
            controller._finish({"verdict": "pass", "summary": "checked", "coverage": "old final only"})

    def test_new_render_window_revision_or_profile_is_new_progress(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        base = {"expected_revision": 0, "start_frame": 0, "end_frame": 30}
        self.assertNotIn("repeated_successful_render", controller._dispatch("render_preview", base)["result"])
        other_window = {**base, "start_frame": 30, "end_frame": 60}
        self.assertNotIn("repeated_successful_render", controller._dispatch("render_preview", other_window)["result"])
        self.assertTrue(controller._dispatch("render_preview", base)["result"]["repeated_successful_render"])
        self.executor.state["plan_revision"] = 1
        next_revision = {**other_window, "expected_revision": 1}
        self.assertNotIn("repeated_successful_render", controller._dispatch("render_preview", next_revision)["result"])
        self.executor.state["profile"] = "2k120"
        self.assertNotIn("repeated_successful_render", controller._dispatch("render_preview", next_revision)["result"])
        self.assertTrue(controller._dispatch("render_preview", next_revision)["result"]["repeated_successful_render"])

    def test_failed_same_render_can_retry_successfully_as_progress(self):
        args = {"expected_revision": 0, "start_frame": 0, "end_frame": 30}
        controller = HunyuanController(Client([]), self.executor, self.root)
        controller._dispatch("render_preview", args)
        for error in (ValueError("synthetic worker failure"), {"result": {"ok": False}, "images": []}):
            with patch.object(self.executor, "call", side_effect=error if isinstance(error, Exception) else None,
                              return_value=error if isinstance(error, dict) else None):
                if isinstance(error, Exception):
                    with self.assertRaisesRegex(ValueError, "synthetic worker failure"):
                        controller._dispatch("render_preview", args)
                else:
                    self.assertFalse(controller._dispatch("render_preview", args)["result"]["ok"])
            # Exercise run's accounting, not only the duplicate marker.
            controller.state.update(read_only_turns=3, last_notes_read_turns=3)
            controller.client = Client([action("render_preview", args, "retry")])
            controller.max_turns = 1
            controller._seen_call_count = 0
            result = controller.run()
            self.assertEqual(result["read_only_turns"], 0)
            reply = json.loads(controller.state["messages"][-1]["content"])
            self.assertNotIn("repeated_successful_render", reply)

    def test_unrelated_failed_render_does_not_refresh_successful_window(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        args = {"expected_revision": 0, "start_frame": 0, "end_frame": 30}
        controller._dispatch("render_preview", args)
        with patch.object(self.executor, "call", side_effect=ValueError("invalid other window")):
            with self.assertRaisesRegex(ValueError, "invalid other window"):
                controller._dispatch("render_preview", {**args, "end_frame": 31})
        self.assertTrue(controller._dispatch("render_preview", args)["result"]["repeated_successful_render"])

    def test_cancelled_render_does_not_refresh_successful_window_after_resume(self):
        from video_localizer.executor import ExecutorCancelled
        controller = HunyuanController(Client([]), self.executor, self.root)
        args = {"expected_revision": 0, "start_frame": 0, "end_frame": 30}
        controller._dispatch("render_preview", args)
        with patch.object(self.executor, "call", side_effect=ExecutorCancelled("cancel before render")):
            with self.assertRaises(ExecutorCancelled):
                controller._dispatch("render_preview", args)
        controller.save()
        resumed = HunyuanController(Client([action("render_preview", args, "retry")]),
                                    self.executor, self.root, max_turns=1)
        self.assertEqual(resumed.run()["read_only_turns"], 1)

    def test_identical_render_tracking_survives_resume(self):
        args = {"expected_revision": 0, "start_frame": 0, "end_frame": 30}
        original = HunyuanController(Client([action("render_preview", args, 1)]),
                                     self.executor, self.root, max_turns=1)
        original.run()
        resumed = HunyuanController(Client([action("render_preview", args, 2)]),
                                    self.executor, self.root, max_turns=1)
        self.assertEqual(resumed.run()["read_only_turns"], 1)

    def test_repeated_render_does_not_erase_other_progress_in_same_response(self):
        args = {"expected_revision": 0, "start_frame": 0, "end_frame": 30}
        review = {"reason": "unreadable", "source_text": "?", "proposed_text": "",
                  "frames": [0], "evidence": "Synthetic uncertain region", "recommendation": "Review manually."}
        combined = action("request_review", review, "new-review")
        combined["tool_calls"] += action("render_preview", args, "repeat")["tool_calls"]
        controller = HunyuanController(Client([action("render_preview", args, 1),
                                              action("get_work_state", {}, 2), combined]),
                                       self.executor, self.root, max_turns=3)
        result = controller.run()
        self.assertEqual(result["read_only_turns"], 0)
        self.assertEqual(result["issues"][0]["status"], "pending")
        self.assertFalse(result["model_reviewed"])
        self.assertFalse(result["human_approved"])

    def test_repeated_successful_renders_reach_existing_stall_limit(self):
        args = {"expected_revision": 0, "start_frame": 0, "end_frame": 30}
        sequence = [action("render_preview", args, 0)]
        sequence += [action("render_preview", args, i) for i in range(1, 5)]
        sequence += [action("record_work_notes", notes(), 5)]
        sequence += [action("render_preview", args, i) for i in range(6, 10)]
        sequence += [action("record_work_notes", notes(), 10)]
        sequence += [action("render_preview", args, i) for i in range(11, 13)]
        controller = HunyuanController(Client(sequence), self.executor, self.root, max_turns=30)
        result = controller.run()
        self.assertEqual((result["status"], result["error_code"], result["read_only_turns"]),
                         ("paused_error", "stalled", 10))
        self.assertEqual(len(controller.client.calls), 13)
        self.assertEqual(controller.state["action_correction_turns"], 0)
        resumed = HunyuanController(Client([]), self.executor, self.root)
        self.assertEqual(resumed.run()["error_code"], "stalled")
        self.assertEqual(resumed.client.calls, [])

    def test_pending_review_blocks_final_but_preserves_existing_candidate(self):
        controller = HunyuanController(Client([]), self.executor, self.root)
        controller.state.update(turns=3, inspected_preview_revision=0, preview_seen_turn=1)
        controller._dispatch("render_final", {"expected_revision": 0})
        existing = self.executor.state["final_output"]
        controller._dispatch("request_review", {"reason": "uncertain", "source_text": "?", "proposed_text": "",
            "frames": [0], "evidence": "Synthetic unresolved region", "recommendation": "Review manually."})
        with self.assertRaisesRegex(ValueError, "Resolve human reviews"):
            controller._dispatch("render_final", {"expected_revision": 0})
        self.assertEqual(self.executor.state["final_output"], existing)
        with self.assertRaisesRegex(ValueError, "Unresolved human reviews"):
            controller._finish({"verdict": "pass", "summary": "checked", "coverage": "candidate"})
        controller._finish({"verdict": "needs_review", "summary": "unresolved", "coverage": "candidate"})
        self.assertEqual(controller.report()["status"], "needs_review")
        self.assertFalse(controller.report()["human_approved"])


if __name__ == "__main__":
    unittest.main()
