"""Read-only diagnostics identify loaded code rather than the next-launch pointer."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from video_localizer import batch, config, seedance_batch


class DoctorDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        for name, value in (("app_dir", self.root), ("runtime_paths", {}),
                            ("settings", None), ("read_key", None)):
            mocked = patch.object(batch, name, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        mocked = patch.object(batch.importlib.util, "find_spec", return_value=object())
        mocked.start()
        self.addCleanup(mocked.stop)
        mocked = patch("video_localizer.selfhost_media.configuration", return_value=None)
        mocked.start()
        self.addCleanup(mocked.stop)

    def test_active_pointer_does_not_impersonate_running_code_or_prompt(self):
        loaded_revision, next_revision = "a" * 32, "b" * 32
        active = self.root / "active.txt"
        active.write_text(next_revision, encoding="ascii")
        loaded_file = self.root / "versions" / loaded_revision / "app/video_localizer/batch.py"
        with patch.object(batch, "__file__", str(loaded_file)), \
                patch.object(batch, "__version__", "loaded-version"), \
                patch.object(seedance_batch, "PROMPT_REVISION", "loaded-prompt"), \
                patch.object(batch.os, "getpid", return_value=43210):
            result = batch.doctor()
        self.assertEqual(result["runtime_revision"], loaded_revision)
        self.assertEqual(result["running_version"], "loaded-version")
        self.assertEqual(result["process_id"], 43210)
        self.assertEqual(result["prompt_revision"], "loaded-prompt")
        self.assertEqual(result["distribution"], "seedance-only")
        self.assertNotIn("quality_policy_revision", result)
        self.assertNotIn("quality_policy_sha256", result)
        self.assertFalse(result["model_contacted"])
        self.assertFalse(result["media_server_contacted"])
        self.assertEqual(active.read_text("ascii"), next_revision)
        self.assertEqual(list(self.root.iterdir()), [active], "Diagnostics must not write installation metadata")

    def test_source_and_invalid_install_paths_do_not_claim_active_revision(self):
        (self.root / "active.txt").write_text("f" * 32, encoding="ascii")
        for relative in ("src/video_localizer/batch.py", "versions/not-a-revision/app/video_localizer/batch.py",
                         "releases/" + "a" * 32 + "/app/video_localizer/batch.py"):
            with self.subTest(path=relative), patch.object(batch, "__file__", str(self.root / relative)):
                self.assertIsNone(batch.doctor()["runtime_revision"])

    def test_readiness_requires_runtime_and_both_separate_credentials(self):
        executable = self.root / "synthetic-runtime.exe"
        executable.write_bytes(b"not executable; existence check only")
        data = {"provider": "seedance", "model": config.SEEDANCE_20_MODEL,
                "base_url": "https://ark.example.invalid/api/v3"}
        media = {"provider": "self_hosted", "endpoint": "https://media.example.invalid/codex-media/v1"}
        for runtime, model_key, media_key in ((True, True, True), (False, True, True),
                                              (True, False, True), (True, True, False)):
            with self.subTest(runtime=runtime, model_key=model_key, media_key=media_key):
                keys = {data["base_url"]: "synthetic-ark-key" if model_key else None,
                        media["endpoint"]: "synthetic-upload-token" if media_key else None}

                def read_key(endpoint):
                    if not keys[endpoint]:
                        raise config.SettingsError("synthetic missing credential")
                    return keys[endpoint]

                with patch.object(batch, "settings", return_value=data), \
                        patch.object(batch, "runtime_paths", return_value={"ffmpeg": executable if runtime else None}), \
                        patch.object(batch, "read_key", side_effect=read_key) as reader, \
                        patch("video_localizer.selfhost_media.configuration", return_value=media):
                    result = batch.doctor()
                self.assertEqual(result["runtime_ready"], runtime)
                self.assertEqual(result["model_configured"], model_key)
                self.assertEqual(result["media_upload_configured"], media_key)
                self.assertEqual(result["video_workflow_ready"], runtime and model_key and media_key)
                self.assertEqual({call.args[0] for call in reader.call_args_list}, set(keys))
                self.assertFalse(result["model_contacted"])
                self.assertFalse(result["media_server_contacted"])
                self.assertFalse(result["automatic_visual_review_available"])
                self.assertNotIn("synthetic-ark-key", str(result))
                self.assertNotIn("synthetic-upload-token", str(result))

    def test_reusing_model_key_for_upload_blocks_doctor_readiness_and_connection(self):
        data = {"provider": "seedance", "model": config.SEEDANCE_20_MODEL,
                "base_url": "https://ark.example.invalid/api/v3"}
        media = {"provider": "self_hosted", "endpoint": "https://media.example.invalid/codex-media/v1"}
        with patch.object(batch, "settings", return_value=data), \
                patch.object(batch, "read_key", return_value="synthetic-same-credential"), \
                patch("video_localizer.selfhost_media.configuration", return_value=media):
            result = batch.doctor()
            self.assertTrue(result["model_configured"])
            self.assertFalse(result["media_upload_configured"])
            self.assertFalse(result["video_workflow_ready"])
            self.assertIn("分开", result["media_configuration_error"])
            self.assertNotIn("synthetic-same-credential", str(result))
            with patch.object(batch, "runtime_paths") as paths:
                with self.assertRaisesRegex(config.SettingsError, "分开"):
                    batch._connection()
                paths.assert_not_called()

    def test_invalid_media_config_is_reported_without_contacting_server(self):
        with patch("video_localizer.selfhost_media.configuration",
                   side_effect=config.SettingsError("synthetic invalid media configuration")):
            result = batch.doctor()
        self.assertEqual(result["media_configuration_error"], "synthetic invalid media configuration")
        self.assertFalse(result["media_upload_configured"])
        self.assertFalse(result["video_workflow_ready"])
        self.assertFalse(result["media_server_contacted"])


if __name__ == "__main__":
    unittest.main()
