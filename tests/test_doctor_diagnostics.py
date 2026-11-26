"""Read-only diagnostics identify loaded code rather than the next-launch pointer."""

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from video_localizer import batch


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

    def test_active_pointer_does_not_impersonate_running_code_or_policy(self):
        loaded_revision, next_revision = "a" * 32, "b" * 32
        active = self.root / "active.txt"
        active.write_text(next_revision, encoding="ascii")
        loaded_file = self.root / "versions" / loaded_revision / "app/video_localizer/batch.py"
        with patch.object(batch, "__file__", str(loaded_file)), \
                patch.object(batch, "__version__", "loaded-version"), \
                patch.object(batch, "QUALITY_POLICY_REVISION", "loaded-policy"), \
                patch.object(batch, "QUALITY_RULES", "已加载的画面检查规则"), \
                patch.object(batch.os, "getpid", return_value=43210):
            result = batch.doctor()
        self.assertEqual(result["runtime_revision"], loaded_revision)
        self.assertEqual(result["running_version"], "loaded-version")
        self.assertEqual(result["process_id"], 43210)
        self.assertEqual(result["quality_policy_revision"], "loaded-policy")
        self.assertEqual(result["quality_policy_sha256"],
                         hashlib.sha256("已加载的画面检查规则".encode("utf-8")).hexdigest())
        self.assertFalse(result["model_contacted"])
        self.assertEqual(active.read_text("ascii"), next_revision)
        self.assertEqual(list(self.root.iterdir()), [active], "Diagnostics must not write installation metadata")

    def test_source_and_invalid_install_paths_do_not_claim_active_revision(self):
        (self.root / "active.txt").write_text("f" * 32, encoding="ascii")
        for relative in ("src/video_localizer/batch.py", "versions/not-a-revision/app/video_localizer/batch.py",
                         "releases/" + "a" * 32 + "/app/video_localizer/batch.py"):
            with self.subTest(path=relative), patch.object(batch, "__file__", str(self.root / relative)):
                self.assertIsNone(batch.doctor()["runtime_revision"])


if __name__ == "__main__":
    unittest.main()
