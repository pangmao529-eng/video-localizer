"""Async Seedance MCP state and explicit human decisions, without external calls."""

from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from video_localizer import mcp


class SessionTests(unittest.TestCase):
    def arguments(self):
        root = Path(tempfile.gettempdir()).resolve()
        return {"inputs": [str(root / "synthetic.mp4")], "output_dir": str(root / "output")}

    def test_paused_status_is_not_relabelled_as_finished(self):
        for status in ("paused_error", "paused_budget", "needs_review", "cancelled"):
            with self.subTest(status=status), patch.object(mcp, "convert", return_value={"status": status}):
                session = mcp.Session()
                self.assertEqual(session.call("convert_videos", self.arguments())["status"], "processing")
                self.assertTrue(session.done.wait(2))
                self.assertEqual(session.call("batch_progress", {"wait_seconds": 0})["status"], status)

    def test_phase_survives_progress_events_but_not_next_video(self):
        session = mcp.Session()
        snapshots = []

        def run(*, progress, **kwargs):
            progress({"stage": "progress", "index": 1, "phase": "uploading"})
            progress({"stage": "progress", "index": 1, "completed_bytes": 256})
            snapshots.append(session.call("batch_progress", {"wait_seconds": 0}))
            progress({"stage": "start", "index": 2})
            snapshots.append(session.call("batch_progress", {"wait_seconds": 0}))
            return {"status": "paused_error", "videos": [{"error_code": "network", "phase": "uploading"}]}

        with patch.object(mcp, "convert", side_effect=run):
            session.call("convert_videos", self.arguments())
            self.assertTrue(session.done.wait(2))
        self.assertEqual(snapshots[0]["progress"]["phase"], "uploading")
        self.assertNotIn("phase", snapshots[1]["progress"])
        result = session.call("batch_progress", {"wait_seconds": 0})
        self.assertEqual(result["status"], "paused_error")
        self.assertEqual(result["videos"][0]["error_code"], "network")

    def test_model_check_runs_asynchronously_and_preserves_auth_scope(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def probe(**kwargs):
            self.assertTrue(release.wait(2))
            return {"status": "passed", "authentication_verified": True,
                    "model_access_verified": False, "generation_submitted": False}

        with patch.object(mcp, "check_model", side_effect=probe):
            session = mcp.Session()
            self.assertEqual(session.call("check_model", {})["status"], "processing")
            self.assertTrue(session.running)
            release.set()
            self.assertTrue(session.done.wait(2))
        result = session.call("batch_progress", {"wait_seconds": 0})
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["authentication_verified"])
        self.assertFalse(result["model_access_verified"])
        self.assertFalse(result["generation_submitted"])

    def test_cancel_reaches_active_worker_and_blocks_competing_work_or_review(self):
        entered = threading.Event()

        def run(*, cancelled, **kwargs):
            entered.set()
            for _ in range(200):
                if cancelled():
                    return {"status": "cancelled", "task_id": "cgt-synthetic-saved"}
                threading.Event().wait(.005)
            raise AssertionError("Cancellation was not observed")

        with patch.object(mcp, "convert", side_effect=run), patch.object(mcp, "submit_review") as submit:
            session = mcp.Session()
            self.addCleanup(session.cancel.set)
            session.call("convert_videos", self.arguments())
            self.assertTrue(entered.wait(2))
            with self.assertRaises(ValueError):
                session.call("convert_videos", self.arguments())
            with self.assertRaises(ValueError):
                session.call("submit_review", self.review_arguments())
            submit.assert_not_called()
            self.assertTrue(session.call("cancel_batch", {})["cancellation_requested"])
            self.assertTrue(session.done.wait(2))
        result = session.call("batch_progress", {"wait_seconds": 0})
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["task_id"], "cgt-synthetic-saved")

    @staticmethod
    def review_arguments():
        return {"batch_dir": str(Path(tempfile.gettempdir()).resolve()), "video_index": 1,
                "issue_id": "seedance-visual-review", "decision": {"action": "keep", "note": "用户明确确认"}}

    def test_review_requires_explicit_supported_fields(self):
        for extra in ({"invented": "approval"}, {"target_text": "host-invented"}, {"start_frame": 1}):
            args = self.review_arguments()
            args["decision"].update(extra)
            with self.subTest(extra=extra), patch.object(mcp, "submit_review") as submit:
                with self.assertRaises(ValueError):
                    mcp.Session().call("submit_review", args)
                submit.assert_not_called()

    def test_review_forwards_exact_human_decision_and_note(self):
        for action in ("keep", "reject"):
            args = self.review_arguments()
            args["decision"] = {"action": action, "note": "原始人类意见 <保持原文>"}
            with self.subTest(action=action), patch.object(mcp, "submit_review", return_value={"status": "resolved"}) as submit:
                self.assertEqual(mcp.Session().call("submit_review", args)["status"], "resolved")
                submit.assert_called_once_with(**args)

    def test_resume_uses_saved_batch_path_and_explicit_poll_allowance(self):
        args = {"batch_dir": str(Path(tempfile.gettempdir()).resolve()), "max_turns": 3}
        with patch.object(mcp, "resume", return_value={"status": "paused_budget"}) as resume:
            session = mcp.Session()
            session.call("resume_batch", args)
            self.assertTrue(session.done.wait(2))
        self.assertEqual(resume.call_args.kwargs["batch_dir"], args["batch_dir"])
        self.assertEqual(resume.call_args.kwargs["max_turns"], 3)
        self.assertEqual(session.call("batch_progress", {"wait_seconds": 0})["status"], "paused_budget")

    def test_invalid_paths_and_probe_options_never_start_worker(self):
        with patch.object(mcp.Session, "_start") as start:
            for name, args in (("convert_videos", {"inputs": ["relative.mp4"], "output_dir": "relative"}),
                               ("resume_batch", {"batch_dir": "relative"}),
                               ("check_model", {"image_path": "unused.png"}),
                               ("check_model", {"timeout": True}),
                               ("check_model", {"timeout": float("nan")})):
                with self.subTest(name=name, args=args), self.assertRaises(ValueError):
                    mcp.Session().call(name, args)
            start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
