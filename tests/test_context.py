import copy
import json
import unittest

from video_localizer.context import build_context


def tool_round(number, *, image=False, complete=True, call_id=None):
    call_id = call_id or f"call-{number}"
    result = [{"role": "assistant", "content": None, "reasoning_content": f"protocol-field-{number}",
               "tool_calls": [{"id": call_id, "type": "function", "function":
                               {"name": "inspect_frames", "arguments": "{}"}}]}]
    if complete:
        result.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps({"round": number})})
    if image:
        result.append({"role": "user", "content": [
            {"type": "text", "text": f"source frame {number}"},
            {"type": "local_image", "path": f"/task/frame-{number}.png", "artifact": "source", "source_frame": number}]})
    return result


def authority(messages):
    return next(json.loads(message["content"]) for message in messages
                if isinstance(message.get("content"), str) and message["content"].startswith('{"type":"authoritative_work_state"'))


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.request = '{"request":"保留动画和姓名，翻译为韩语","target_language":"韩语"}'
        self.state = {"messages": [{"role": "system", "content": "old system"},
                                   {"role": "user", "content": self.request}],
                      "reviews": [], "work_notes": {}, "task_ledger": {}, "material_rows": []}
        self.snapshot = {"source": "/task/original.mp4", "plan_revision": 3,
                         "plan": {"summary": "exact model summary", "segments": []}, "programs": {}}

    def test_full_history_is_a_deep_copy(self):
        self.state["messages"].extend(tool_round(1, image=True))
        result = build_context(self.state, self.snapshot, "new system")
        self.assertEqual(result, self.state["messages"])
        result[-1]["content"][1]["path"] = "changed"
        self.assertEqual(self.state["messages"][-1]["content"][1]["path"], "/task/frame-1.png")

    def test_compaction_keeps_complete_rounds_and_original_request(self):
        for number in range(5):
            self.state["messages"].extend(tool_round(number, image=number == 4))
        before = copy.deepcopy(self.state)
        result = build_context(self.state, self.snapshot, "new system", compact=True)
        self.assertEqual(result[0], {"role": "system", "content": "new system"})
        self.assertEqual(result[1]["content"], self.request)
        self.assertEqual([m["reasoning_content"] for m in result if m["role"] == "assistant"],
                         ["protocol-field-3", "protocol-field-4"])
        self.assertEqual([m["tool_call_id"] for m in result if m["role"] == "tool"], ["call-3", "call-4"])
        self.assertEqual(self.state, before)
        result[-1]["content"][1]["path"] = "changed"
        self.assertEqual(self.state, before)

    def test_explicit_original_request_survives_late_history(self):
        self.state["original_request"] = self.request
        self.state["messages"] = tool_round(7)
        result = build_context(self.state, self.snapshot, "policy", compact=True)
        self.assertEqual(result[1]["content"], self.request)

    def test_unfinished_and_orphan_tool_rounds_do_not_compact(self):
        histories = [tool_round(1) + tool_round(2, complete=False),
                     tool_round(1) + [{"role": "tool", "tool_call_id": "orphan", "content": "{}"}],
                     [{"role": "tool", "tool_call_id": "orphan", "content": "{}"}] + tool_round(1)]
        for history in histories:
            with self.subTest(history=history):
                self.state["messages"] = history
                self.assertEqual(build_context(self.state, self.snapshot, "policy", compact=True), history)

    def test_duplicate_ids_across_rounds_are_valid_and_parallel_results_stay_together(self):
        self.state["messages"].extend(tool_round(1, call_id="reused"))
        block = tool_round(2, call_id="reused")
        block[0]["tool_calls"].append({"id": "other", "type": "function", "function": {"name": "lookup_materials", "arguments": "{}"}})
        block.append({"role": "tool", "tool_call_id": "other", "content": "{}"})
        self.state["messages"].extend(block)
        result = build_context(self.state, self.snapshot, "policy", compact=True)
        self.assertEqual([m["tool_call_id"] for m in result if m["role"] == "tool"], ["reused", "reused", "other"])
        self.assertEqual(authority(result)["plan_revision"], 3)

    def test_focus_contains_only_overlapping_full_segments_but_index_is_complete(self):
        self.state["work_notes"] = {"facts": ["model fact"], "focus": {"start_frame": 20, "end_frame": 80}}
        for number in range(10):
            segment = {"id": f"s{number}", "source_text": f"源文{number}", "target_text": f"문구{number}",
                       "start_frame": number * 10, "end_frame": (number + 1) * 10,
                       "style": {"font_size": 30}, "keyframes": [{"frame": number * 10, "box": [1, 2, 30, 40]}]}
            self.snapshot["plan"]["segments"].append(segment)
            self.snapshot["programs"][segment["id"]] = {"code": f"background = solid([{number},0,0])"}
        before = copy.deepcopy(self.snapshot)
        data = authority(build_context(self.state, self.snapshot, "policy", compact=True))
        self.assertEqual(len(data["plan_index"]), 10)
        self.assertEqual(data["focus_segment_count"], 6)
        self.assertEqual([row["id"] for row in data["focus_segments"]], ["s2", "s3", "s4", "s5"])
        self.assertEqual(set(data["focus_programs"]), {"s2", "s3", "s4", "s5"})
        self.assertIn("keyframes", data["focus_segments"][0])
        self.assertNotIn("keyframes", data["plan_index"][0])
        self.assertEqual(self.snapshot, before)

    def test_reviews_human_decisions_and_open_tasks_are_never_truncated(self):
        self.state["reviews"] = [{"id": f"r{i}", "status": "pending", "evidence": "raw evidence " * 30} for i in range(20)]
        decision = {"action": "replace", "source_text": "姓名", "target_text": "정확한 이름", "note": "人工原话", "start_frame": 9, "end_frame": 20}
        self.state["reviews"].append({"id": "resolved", "status": "resolved", "decision": decision})
        self.state["decisions"] = [{"decision": "independent human record"}]
        self.state["task_ledger"] = {f"t{i}": {"description": f"open task {i}", "status": "open", "evidence_frames": [i], "segment_ids": []} for i in range(30)}
        self.state["task_ledger"]["done"] = {"status": "done", "description": "completed local edit", "evidence_frames": [3, 3, 4], "segment_ids": ["s1"]}
        self.state["work_notes"] = {"open_issues": [], "facts": ["Notes do not resolve the pending reviews"]}
        data = authority(build_context(self.state, self.snapshot, "policy", compact=True))
        self.assertEqual(data["reviews"], self.state["reviews"])
        self.assertEqual(data["decisions"], self.state["decisions"])
        self.assertEqual(len(data["task_ledger"]["open"]), 30)
        self.assertEqual(data["task_ledger"]["done"][0]["evidence_frame_count"], 2)

    def test_bootstrap_replays_at_most_four_archive_images_without_new_review_credit(self):
        for number in range(8):
            self.state["messages"].extend(tool_round(number, image=number < 6))
        self.state.update(source_frames_seen=[0, 1, 1, 2], source_frames_delivered=[0, 1], final_frames_seen=[], phase="analysis")
        self.state["material_rows"] = [{"source": "Exact source", "finalized": ["정확한 문구"],
                                       "translation_rows": [{"line_number": 8, "raw_row": ["Exact source", "정확한 문구"]}]}]
        result = build_context(self.state, self.snapshot, "policy", compact=True)
        data = authority(result)
        self.assertFalse(data["context_policy"]["semantic_notes_available"])
        self.assertTrue(data["context_policy"]["source_image_history_requires_reinspection"])
        self.assertEqual(data["archived_observations"]["material_rows"], self.state["material_rows"])
        self.assertEqual(data["recorded_feedback"]["source_frames_extracted_count"], 3)
        self.assertEqual(data["recorded_feedback"]["source_frames_delivered_count"], 2)
        images = [part for part in result[-1]["content"] if part["type"] == "local_image"]
        self.assertEqual([part["source_frame"] for part in images], [2, 3, 4, 5])
        self.assertTrue(all(part["archived_context"] and part["review_evidence"] is False for part in images))
        self.assertEqual(images[0]["artifact"], "source")
        self.assertIn("source frame 2", [part["text"] for part in result[-1]["content"] if part["type"] == "text"])
        self.assertNotIn("protocol-field-0", json.dumps(result))

    def test_recent_images_do_not_gain_archive_flags_or_add_old_images(self):
        self.state["messages"].extend(tool_round(1, image=True) + tool_round(2, image=True))
        result = build_context(self.state, self.snapshot, "policy", compact=True, recent_turns=1)
        images = [part for message in result if isinstance(message.get("content"), list)
                  for part in message["content"] if part.get("type") == "local_image"]
        self.assertEqual([part["source_frame"] for part in images], [2])
        self.assertNotIn("archived_context", images[0])


if __name__ == "__main__":
    unittest.main()
