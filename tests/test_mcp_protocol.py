"""Real stdio subprocess smoke tests; no model request or user configuration write."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from video_localizer import __version__


SOURCE = Path(__file__).resolve().parents[1] / "src"


class StdioProtocolTests(unittest.TestCase):
    def exchange(self, requests):
        with tempfile.TemporaryDirectory() as folder:
            env = {key: value for key, value in os.environ.items() if not key.startswith("VIDEO_LOCALIZER_")}
            # The doctor only examines an empty, isolated account directory.
            env["LOCALAPPDATA"] = folder
            code = "import sys; sys.path.insert(0, " + repr(str(SOURCE)) + "); from video_localizer.mcp import serve; serve()"
            data = "".join(json.dumps(row) + "\n" for row in requests).encode("utf-8")
            result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", code], input=data,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
            self.assertEqual(result.stderr, b"")
            lines = result.stdout.decode("utf-8").splitlines()
            self.assertEqual(len(lines), len(requests), "MCP stdout must contain only one JSON response per request")
            self.assertEqual(list(Path(folder).iterdir()), [], "Read-only protocol checks must not write configuration")
            return [json.loads(line) for line in lines]

    def test_initialize_tools_and_read_only_setup(self):
        replies = self.exchange([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "check_setup", "arguments": {}}},
        ])
        self.assertEqual([row["id"] for row in replies], [1, 2, 3])
        self.assertEqual(replies[0]["result"]["serverInfo"]["version"], __version__)
        names = {tool["name"] for tool in replies[1]["result"]["tools"]}
        self.assertEqual(names, {"check_setup", "open_model_configuration", "check_model", "convert_videos",
                                 "resume_batch", "submit_review", "cancel_batch", "batch_progress"})
        setup = json.loads(replies[2]["result"]["content"][0]["text"])
        self.assertFalse(setup["model_contacted"])
        self.assertFalse(setup["model_configured"])
        self.assertEqual(setup["workflow"], "hunyuan_controller")
        self.assertEqual(setup["running_version"], replies[0]["result"]["serverInfo"]["version"])
        self.assertGreater(setup["process_id"], 0)
        self.assertIsNone(setup["runtime_revision"], "Source tests must not claim an installed revision")
        self.assertRegex(setup["quality_policy_sha256"], r"^[0-9a-f]{64}$")

    def test_unknown_method_is_a_protocol_error_not_a_traceback(self):
        reply = self.exchange([{"jsonrpc": "2.0", "id": 4, "method": "not-a-method"}])[0]
        self.assertEqual(reply["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
