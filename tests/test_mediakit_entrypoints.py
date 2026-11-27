"""CLI/MCP provider isolation and resumable control, without network or keys."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from video_localizer import __main__ as cli, mcp, mediakit_batch, mediakit_config


class MediaKitEntryPointTests(unittest.TestCase):
    def args(self):
        root = Path(tempfile.gettempdir()).resolve()
        return {"input_path": str(root / "source.mp4"), "translations": str(root / "approved.json"),
                "output_dir": str(root / "korean")}

    def invoke_cli(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["video-localizer", *args]), redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_config_uses_separate_interactive_configuration(self):
        with patch.object(mediakit_config, "configure", return_value={"configured": True}) as configure, \
                patch("video_localizer.config.configure") as ark:
            code, output, errors = self.invoke_cli(["setup-mediakit", "--model-version", "v5"])
        configure.assert_called_once_with(model_version="v5")
        ark.assert_not_called()
        self.assertEqual((code, errors), (0, ""))
        self.assertTrue(json.loads(output)["configured"])

    def test_cli_doctor_has_no_generation_side_effect(self):
        with patch.object(mediakit_config, "check_setup", return_value={"credential_available": False}) as check, \
                patch.object(mediakit_batch, "convert") as run:
            code, output, _ = self.invoke_cli(["mediakit-doctor"])
        check.assert_called_once_with()
        run.assert_not_called()
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output)["credential_available"])

    def test_cli_media_conversion_forwards_exact_paths_and_marks_checkpoint_incomplete(self):
        args = self.args()
        def convert(**options):
            options["progress"]({"phase": "ocr_completed"})
            return {"status": "paused_checkpoint", "batch_dir": args["output_dir"]}
        with patch.object(mediakit_batch, "convert", side_effect=convert) as media, \
                patch("video_localizer.batch.convert") as seedance:
            code, output, errors = self.invoke_cli(["mediakit-convert", args["input_path"],
                "--translations", args["translations"], "--output", args["output_dir"],
                "--max-polls", "3", "--stop-after", "ocr"])
        seedance.assert_not_called()
        self.assertEqual({key: media.call_args.kwargs[key] for key in args}, args)
        self.assertEqual(media.call_args.kwargs["max_polls"], 3)
        self.assertEqual(media.call_args.kwargs["stop_after"], "ocr")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["status"], "paused_checkpoint")
        self.assertEqual(json.loads(errors)["phase"], "ocr_completed")

    def test_cli_resume_routes_to_mediakit_only(self):
        folder = self.args()["output_dir"]
        with patch.object(mediakit_batch, "resume", return_value={"status": "needs_review"}) as media, \
                patch("video_localizer.batch.resume") as seedance:
            code, _, _ = self.invoke_cli(["mediakit-resume", "--batch-dir", folder, "--max-polls", "7",
                                         "--stop-after", "erase"])
        self.assertEqual(code, 1)
        self.assertEqual(media.call_args.kwargs["batch_dir"], folder)
        self.assertEqual(media.call_args.kwargs["max_polls"], 7)
        self.assertEqual(media.call_args.kwargs["stop_after"], "erase")
        seedance.assert_not_called()

    def test_cli_does_not_accept_a_key_argument(self):
        with patch.object(mediakit_config, "configure") as configure, self.assertRaises(SystemExit), \
                redirect_stderr(io.StringIO()):
            self.invoke_cli(["setup-mediakit", "--api-key", "synthetic-not-a-key"])
        configure.assert_not_called()

    def test_mcp_media_operations_do_not_fall_through_to_seedance_resume(self):
        for operation, args, function in (("convert_subtitles", self.args(), "convert"),
                ("resume_subtitles", {"batch_dir": self.args()["output_dir"], "max_polls": 4,
                                      "stop_after": "erase"}, "resume")):
            with self.subTest(operation=operation), patch.object(mediakit_batch, function,
                    return_value={"status": "needs_review", "provider": "mediakit"}) as media, \
                    patch.object(mcp, "resume") as old_resume, patch.object(mcp, "convert") as old_convert:
                session = mcp.Session()
                self.assertEqual(session.call(operation, args)["status"], "processing")
                self.assertTrue(session.done.wait(2))
                result = session.call("batch_progress", {"wait_seconds": 0})
                self.assertEqual(result["operation"], operation)
                self.assertEqual(result["status"], "needs_review")
                self.assertEqual({key: media.call_args.kwargs[key] for key in args}, args)
                self.assertTrue(callable(media.call_args.kwargs["cancelled"]))
                old_resume.assert_not_called()
                old_convert.assert_not_called()

    def test_mcp_cancel_reaches_mediakit_worker(self):
        entered = threading.Event()
        def convert(*, cancelled, progress, **kwargs):
            progress({"phase": "ocr_running"})
            entered.set()
            for _ in range(200):
                if cancelled():
                    return {"status": "cancelled", "task_id": "amk-tool-synthetic"}
                threading.Event().wait(.005)
            raise AssertionError("cancellation not forwarded")
        with patch.object(mediakit_batch, "convert", side_effect=convert):
            session = mcp.Session()
            self.addCleanup(session.cancel.set)
            session.call("convert_subtitles", self.args())
            self.assertTrue(entered.wait(2))
            with self.assertRaises(ValueError):
                session.call("resume_subtitles", {"batch_dir": self.args()["output_dir"]})
            session.call("cancel_batch", {})
            self.assertTrue(session.done.wait(2))
        self.assertEqual(session.state["task_id"], "amk-tool-synthetic")
        self.assertEqual(session.state["status"], "cancelled")

    def test_mcp_invalid_media_options_never_start_work(self):
        variants = [{"max_polls": True}, {"max_polls": 0}, {"max_polls": 201},
                    {"stop_after": "invented"}, {"input_path": "relative.mp4"},
                    {"translations": "invented inline translations"}, {"api_key": "synthetic"}]
        with patch.object(mcp.Session, "_start") as start:
            for changes in variants:
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    mcp.Session().call("convert_subtitles", {**self.args(), **changes})
            start.assert_not_called()

    def test_mcp_unknown_internal_operation_does_not_resume_any_provider(self):
        with patch.object(mcp, "resume") as old_resume, patch.object(mediakit_batch, "resume") as media_resume:
            session = mcp.Session()
            session._run("unknown", {})
        self.assertEqual(session.state["status"], "failed")
        old_resume.assert_not_called()
        media_resume.assert_not_called()

    def test_mcp_setup_is_read_only_and_uses_own_configuration(self):
        with patch.object(mediakit_config, "check_setup", return_value={"provider": "mediakit"}) as check, \
                patch.object(mcp.Session, "_start") as start:
            self.assertEqual(mcp.Session().call("check_mediakit_setup", {}), {"provider": "mediakit"})
        check.assert_called_once_with()
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
