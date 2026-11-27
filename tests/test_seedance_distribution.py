"""The active package can start without archived Hunyuan implementation modules."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class SeedanceDistributionTests(unittest.TestCase):
    def test_active_entrypoints_import_without_archived_modules_or_configuration_writes(self):
        source = Path(__file__).resolve().parents[1] / "src"
        code = r'''
import importlib, importlib.abc, json, pkgutil, sys
sys.path.insert(0, sys.argv[1])
blocked = {"hunyuan", "controller", "executor", "model", "context", "media", "frame_program", "quality_rules"}
class RejectLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("video_localizer.") and fullname.split(".")[1] in blocked:
            raise AssertionError("Archived module requested: " + fullname)
sys.meta_path.insert(0, RejectLegacy())
import video_localizer
names = {module.name for module in pkgutil.iter_modules(video_localizer.__path__)}
assert names.isdisjoint(blocked), sorted(names & blocked)
for name in ("__main__", "mcp", "batch", "backend", "seedance", "seedance_batch", "seedance_segment_batch", "seedance_client", "selfhost_media"):
    importlib.import_module("video_localizer." + name)
print(json.dumps({"legacy_loaded": sorted(name for name in sys.modules if name.startswith("video_localizer.") and name.split(".")[1] in blocked)}))
'''
        with tempfile.TemporaryDirectory() as directory:
            env = {key: value for key, value in os.environ.items() if not key.startswith("VIDEO_LOCALIZER_")}
            env["LOCALAPPDATA"] = directory
            result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", code, str(source)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
            self.assertEqual(result.stderr, b"")
            self.assertEqual(json.loads(result.stdout), {"legacy_loaded": []})
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
