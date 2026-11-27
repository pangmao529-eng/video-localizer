import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from video_localizer import batch, mcp
from video_localizer.config import SettingsError
from video_localizer.review import write_review


class FakeExecutor:
    def __init__(self, source, work_dir, **kwargs):
        self.source = Path(source)
        self.root = Path(work_dir)
        self.root.mkdir(parents=True, exist_ok=True)


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.inputs = []
        for index in range(3):
            path = self.root / f"source-{index}.mp4"
            path.write_bytes(b"source fixture")
            self.inputs.append(str(path))
        connection = ({"model": "hy4", "base_url": "https://example.invalid/v1"}, "fixture-secret",
                      {"ffmpeg": "unused", "ffprobe": "unused", "font": "unused"})
        for target, value in (("video_localizer.batch.app_dir", lambda: self.root / "user-app"),
                              ("video_localizer.batch._connection", lambda: connection),
                              ("video_localizer.executor.VideoExecutor", FakeExecutor)):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("video_localizer.hunyuan.HunyuanClient")
        self.client = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("video_localizer.controller.HunyuanController")
        self.controller = patcher.start()
        self.addCleanup(patcher.stop)

    def result(self, status):
        return {"status": status, "output": None, "model_reviewed": False, "human_approved": False,
                "issues": [], "message": "test checkpoint"}

    def test_model_failure_pauses_batch_and_resume_skips_completed(self):
        self.controller.return_value.run.side_effect = [self.result("completed"), self.result("paused_error")]
        first = batch.convert(self.inputs, self.root / "outputs", request="原始要求")
        self.assertEqual(first["status"], "paused_error")
        self.assertEqual([row["status"] for row in first["videos"]], ["completed", "paused_error", "not_processed"])
        stored = json.loads(Path(first["report"]).read_text("utf-8"))
        self.assertEqual(stored["schema"], 2)
        self.assertEqual(stored["params"]["request"], "原始要求")
        self.assertEqual(self.controller.call_count, 2)
        self.controller.return_value.run.side_effect = [self.result("completed"), self.result("completed")]
        second = batch.resume(first["directory"])
        self.assertEqual(second["status"], "completed")
        self.assertEqual(self.controller.call_count, 4)
        self.assertFalse(second["human_approved"])
        self.assertEqual(second["completed"], 3)

    def test_review_does_not_block_other_videos_but_budget_does(self):
        self.controller.return_value.run.side_effect = [self.result("needs_review"), self.result("paused_budget")]
        result = batch.convert(self.inputs, self.root / "outputs")
        self.assertEqual(result["status"], "paused_budget")
        self.assertEqual(result["needs_review"], 1)
        self.assertEqual(result["unprocessed"], 1)
        self.assertEqual(self.controller.call_count, 2)

    def test_pause_reports_preserve_analysis_phase_and_distinct_stop_reasons(self):
        fields = {"phase": "draft", "read_only_turns": 6, "compactions": 2, "output_recoveries": 1}
        for status, code in (("paused_budget", None), ("paused_error", "output_limit"), ("paused_error", "stalled")):
            with self.subTest(status=status, code=code):
                item = {**self.result(status), **fields, "error_code": code}
                self.controller.return_value.run.return_value = item
                progress = []
                result = batch.convert(self.inputs[:2], self.root / (code or "turn-budget"), progress=progress.append)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["unprocessed"], 1)
                self.assertEqual(result["videos"][0]["error_code"], code)
                for field, value in fields.items():
                    self.assertEqual(result["videos"][0][field], value)
                    self.assertEqual(progress[-1][field], value)
                self.assertEqual(progress[-1]["status"], status)
                self.assertEqual(progress[-1]["error_code"], code)
                saved = json.loads(Path(result["report"]).read_text("utf-8"))
                self.assertEqual(saved["videos"][0]["phase"], "draft")

    def test_twenty_videos_run_serially_and_twenty_one_are_rejected_before_start(self):
        inputs = list(self.inputs)
        for index in range(3, 21):
            path = self.root / f"source-{index}.mp4"
            path.write_bytes(b"source fixture")
            inputs.append(str(path))
        events, active = [], []
        guard = threading.Lock()
        test = self

        def create_controller(client, executor, work_dir, **kwargs):
            source = str(executor.source)

            class Controller:
                def run(self):
                    with guard:
                        active.append(source)
                        test.assertEqual(len(active), 1, "Video execution must remain serial")
                        events.append(("start", source))
                    # Yield long enough for a parallel queue to expose overlap.
                    threading.Event().wait(.003)
                    with guard:
                        events.append(("complete", source))
                        active.remove(source)
                    return test.result("completed")

            return Controller()

        self.controller.side_effect = create_controller
        result = batch.convert(inputs[:20], self.root / "outputs")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed"], 20)
        self.assertEqual(self.controller.call_count, 20)
        self.assertEqual(events, [(event, source) for source in inputs[:20] for event in ("start", "complete")])
        self.assertEqual(len({row["work_dir"] for row in result["videos"]}), 20)
        calls_before = self.client.call_count
        rejected_output = self.root / "rejected-output"
        with self.assertRaisesRegex(ValueError, "21.*1–20"):
            batch.convert(inputs, rejected_output)
        self.assertEqual(self.client.call_count, calls_before)
        self.assertEqual(self.controller.call_count, 20)
        self.assertFalse(rejected_output.exists())

    def test_pending_review_allows_following_videos_to_finish_and_resume_skips_them(self):
        pending = self.result("needs_review")
        pending["issues"] = [{"id": "review-1", "status": "pending", "source_text": "ナミ",
                              "proposed_text": "나미", "reason": "角色名牌与立绘需要人工确认"}]
        self.controller.return_value.run.side_effect = [pending, self.result("completed"), self.result("completed")]
        first = batch.convert(self.inputs, self.root / "outputs")
        self.assertEqual(first["status"], "needs_review")
        self.assertEqual([row["status"] for row in first["videos"]], ["needs_review", "completed", "completed"])
        self.assertEqual(first["completed"], 2)
        self.assertEqual(first["unprocessed"], 0)
        self.assertEqual(first["pending_reviews"], 1)
        self.controller.reset_mock()
        self.controller.return_value.run.side_effect = [pending]
        second = batch.resume(first["directory"])
        self.assertEqual(self.controller.call_count, 1)
        self.assertEqual(self.controller.call_args.args[1].source, Path(self.inputs[0]))
        self.assertEqual(second["status"], "needs_review")
        self.assertEqual(second["completed"], 2)
        self.assertEqual(second["pending_reviews"], 1)
        self.assertFalse(second["human_approved"])

    def test_cancel_before_first_video_is_resumable_and_has_no_model_request(self):
        result = batch.convert(self.inputs, self.root / "outputs", cancelled=lambda: True)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["unprocessed"], 3)
        self.client.assert_not_called()
        self.assertTrue(Path(result["report"]).is_file())

    def test_user_lock_prevents_a_second_batch_and_is_released(self):
        with batch.batch_lock():
            with self.assertRaises(SettingsError):
                with batch.batch_lock():
                    self.fail("second lock must not be acquired")
        with batch.batch_lock():
            pass

    def test_resume_rejects_work_directory_escape(self):
        self.controller.return_value.run.side_effect = [self.result("paused_error")]
        result = batch.convert(self.inputs, self.root / "outputs")
        report_file = Path(result["report"])
        data = json.loads(report_file.read_text("utf-8"))
        data["videos"][0]["work_dir"] = str(self.root / "elsewhere")
        report_file.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "工作目录"):
            batch.resume(result["directory"])

    def test_review_records_human_decision_without_calling_model(self):
        self.controller.return_value.run.side_effect = [self.result("needs_review")]
        result = batch.convert(self.inputs[:1], self.root / "outputs")
        work = Path(result["videos"][0]["work_dir"])
        state = {"status": "needs_review", "messages": [], "reviews": [
            {"id": "review-1", "status": "pending", "source_text": "ナミ", "proposed_text": "나미"}]}
        (work / "controller-state.json").write_text(json.dumps(state), encoding="utf-8")
        calls_before = self.client.call_count
        decision = {"action": "adjust_timing", "source_text": "ナミ", "target_text": "나미",
                    "start_frame": 271, "end_frame": 300, "note": "第271帧才切换"}
        saved = batch.submit_review(result["directory"], 1, "review-1", decision)
        updated = json.loads((work / "controller-state.json").read_text("utf-8"))
        report = json.loads(Path(result["report"]).read_text("utf-8"))
        self.assertEqual(saved["status"], "resolved")
        self.assertEqual(updated["reviews"][0]["decision"], decision)
        self.assertEqual(report["videos"][0]["status"], "ready")
        self.assertFalse(report["human_approved"])
        self.assertEqual(self.client.call_count, calls_before)

    def test_runtime_paths_use_active_version_without_launcher_environment(self):
        install = self.root / "user-app"
        version = install / "versions" / ("a" * 32)
        (version / "bin").mkdir(parents=True)
        (version / "fonts").mkdir()
        (install / "active.txt").write_text("a" * 32)
        for name in ("ffmpeg", "ffprobe"):
            (version / "bin" / (name + ".exe")).write_bytes(b"runtime fixture")
        (version / "fonts" / "NotoSansCJKkr-Regular.otf").write_bytes(b"font fixture")
        with patch.dict(batch.os.environ, {key: value for key, value in batch.os.environ.items()
                                           if not key.startswith("VIDEO_LOCALIZER_")}, clear=True):
            paths = batch.runtime_paths()
        self.assertEqual(paths["ffmpeg"], version / "bin" / "ffmpeg.exe")
        self.assertEqual(paths["font"], version / "fonts" / "NotoSansCJKkr-Regular.otf")

    def test_review_is_visible_without_a_video_and_escapes_text(self):
        folder = self.root / "review"
        folder.mkdir()
        file = write_review(folder, [{"index": 1, "source": self.inputs[0], "status": "needs_review",
            "issues": [{"id": "review-1", "status": "pending", "source_text": "<script>bad()</script>",
                        "reason": "名牌冲突", "frames": [269, 270, 271], "evidence": "立绘仍是山治"}]}])
        text = Path(file).read_text("utf-8")
        self.assertIn("review-1", text)
        self.assertIn("269, 270, 271", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertNotIn("<script>", text)
        self.assertIn("未确认", text)

    def test_review_images_stay_inside_batch_and_material_rows_are_escaped(self):
        folder = self.root / "review"
        work = folder / "videos" / "001"
        work.mkdir(parents=True)
        (work / "frame 0.png").write_bytes(b"image fixture")
        (work / "review.json").write_text("{}", encoding="utf-8")
        (work / "controller-state.json").write_text("{}", encoding="utf-8")
        outside = self.root / "outside.png"
        outside.write_bytes(b"private image")
        page = write_review(folder, [{"index": 1, "source": self.inputs[0], "status": "needs_review",
            "work_dir": str(work), "issues": [{"id": "review-1", "status": "pending",
                "evidence_images": [{"path": "videos/001/frame 0.png", "label": '<img onerror="bad()">',
                                     "source_frame": 0, "time_ms": 0, "crop_source_pixels": [1, 2, 20, 30]},
                                    {"path": str(outside), "label": "private"},
                                    {"path": "../outside.png", "label": "escape"},
                                    {"path": str(work / "controller-state.json"), "label": "not image"}],
                "material_rows": [{"source": "<script>bad()</script>", "target": "나미", "row": 2}],
                "decision": {"action": "adjust_timing", "source_text": "ナミ", "target_text": "나미",
                             "start_frame": 0, "end_frame": 10}}]}])
        text = Path(page).read_text("utf-8")
        self.assertEqual(text.count('<img '), 1)
        self.assertIn('src="videos/001/frame%200.png"', text)
        self.assertIn('原片帧号：0', text)
        self.assertIn('时间（毫秒）：0', text)
        self.assertIn('&lt;img onerror=&quot;bad()&quot;&gt;', text)
        self.assertIn('<td>&lt;script&gt;bad()&lt;/script&gt;</td>', text)
        self.assertIn('确认原文：ナミ', text)
        self.assertIn('开始帧（含）：0', text)
        self.assertIn('videos/001/review.json', text)
        self.assertNotIn('outside.png', text)
        self.assertNotIn('controller-state.json', text)
        self.assertNotIn('<script>', text)


class SessionTests(unittest.TestCase):
    def arguments(self):
        root = Path(tempfile.gettempdir()).resolve()
        return {"inputs": [str(root / "example.mp4")], "output_dir": str(root / "output")}

    def test_paused_status_is_not_relabelled_as_finished(self):
        with patch.object(mcp, "convert", return_value={"status": "paused_error", "error_code": "timeout"}):
            session = mcp.Session()
            started = session.call("convert_videos", self.arguments())
            self.assertEqual(started["status"], "processing")
            self.assertTrue(session.done.wait(2))
            self.assertEqual(session.call("batch_progress", {"wait_seconds": 0})["status"], "paused_error")

    def test_analysis_progress_survives_tool_events_but_not_next_video(self):
        session = mcp.Session()
        snapshots = []
        fields = {"phase": "draft", "read_only_turns": 3, "compactions": 1, "output_recoveries": 2}

        def run(*, progress, **kwargs):
            progress({"stage": "model_turn", "index": 1, **fields})
            progress({"stage": "tool_finished", "index": 1, "tool": "inspect_frames"})
            snapshots.append(session.call("batch_progress", {"wait_seconds": 0}))
            progress({"stage": "start", "index": 2})
            snapshots.append(session.call("batch_progress", {"wait_seconds": 0}))
            return {"status": "paused_error", "videos": [{"error_code": "stalled", **fields}]}

        with patch.object(mcp, "convert", side_effect=run):
            session.call("convert_videos", self.arguments())
            self.assertTrue(session.done.wait(2))
        for field, value in fields.items():
            self.assertEqual(snapshots[0]["progress"][field], value)
            self.assertNotIn(field, snapshots[1]["progress"])
        result = session.call("batch_progress", {"wait_seconds": 0})
        self.assertEqual(result["status"], "paused_error")
        self.assertEqual(result["videos"][0]["error_code"], "stalled")
        self.assertEqual(result["videos"][0]["phase"], "draft")

    def test_model_check_runs_asynchronously(self):
        release = threading.Event()

        def probe(**kwargs):
            self.assertTrue(release.wait(2))
            return {"status": "passed", "tool_round_trip": True}

        with patch.object(mcp, "check_model", side_effect=probe):
            session = mcp.Session()
            self.assertEqual(session.call("check_model", {})["status"], "processing")
            self.assertTrue(session.running)
            release.set()
            self.assertTrue(session.done.wait(2))
            self.assertEqual(session.call("batch_progress", {"wait_seconds": 0})["status"], "passed")

    def test_cancel_is_delivered_to_active_controller(self):
        entered = threading.Event()

        def run(*, cancelled, **kwargs):
            entered.set()
            for _ in range(200):
                if cancelled():
                    return {"status": "cancelled"}
                threading.Event().wait(.005)
            raise AssertionError("Cancellation was not observed")

        with patch.object(mcp, "convert", side_effect=run):
            session = mcp.Session()
            session.call("convert_videos", self.arguments())
            self.assertTrue(entered.wait(2))
            self.assertTrue(session.call("cancel_batch", {})["cancellation_requested"])
            self.assertTrue(session.done.wait(2))
            self.assertEqual(session.call("batch_progress", {"wait_seconds": 0})["status"], "cancelled")

    def test_review_requires_explicit_supported_fields(self):
        args = {"batch_dir": str(Path(tempfile.gettempdir()).resolve()), "video_index": 1,
                "issue_id": "review-1", "decision": {"action": "keep", "invented": "approval"}}
        with patch.object(mcp, "submit_review") as submit:
            with self.assertRaises(ValueError):
                mcp.Session().call("submit_review", args)
        submit.assert_not_called()

    def test_review_forwards_exact_human_text_and_interval(self):
        args = {"batch_dir": str(Path(tempfile.gettempdir()).resolve()), "video_index": 1,
                "issue_id": "review-1", "decision": {"action": "replace", "source_text": "ナミ",
                "target_text": "나미", "start_frame": 271, "end_frame": 300}}
        with patch.object(mcp, "submit_review", return_value={"status": "resolved"}) as submit:
            self.assertEqual(mcp.Session().call("submit_review", args)["status"], "resolved")
        submit.assert_called_once_with(**args)


if __name__ == "__main__":
    unittest.main()
