"""Policy diagnostics use a fixed explanation, never the provider's private body."""

import json
import unittest
from unittest.mock import patch

from video_localizer.seedance_client import SeedanceClient


class SeedancePolicyTests(unittest.TestCase):
    def test_copyright_policy_error_keeps_code_with_safe_actionable_message(self):
        client = SeedanceClient("https://example.invalid/api/v3", "synthetic-not-a-key")
        private = "private-provider-body-with-signed-url-and-secret"
        code = "OutputVideoSensitiveContentDetected.PolicyViolation"
        with patch.object(client, "_request", return_value={
                "id": "cgt-synthetic", "status": "failed", "error": {"code": code, "message": private}}):
            result = client.get_task("cgt-synthetic")
        self.assertEqual(result["error_code"], code)
        self.assertIn("版权", result["message"])
        self.assertIn("不会自动重新生成", result["message"])
        self.assertNotIn(private, json.dumps(result))
        self.assertNotIn("synthetic-not-a-key", json.dumps(result))

    def test_other_policy_codes_are_preserved_without_inventing_copyright_diagnosis(self):
        client = SeedanceClient("https://example.invalid/api/v3", "synthetic-not-a-key")
        for code in ("InputVideoSensitiveContentDetected", "OtherProviderFailure"):
            with self.subTest(code=code), patch.object(client, "_request", return_value={
                    "id": "cgt-synthetic", "status": "failed", "error": {"code": code, "message": "private"}}):
                result = client.get_task("cgt-synthetic")
            self.assertEqual(result["error_code"], code)
            self.assertNotIn("message", result)


if __name__ == "__main__":
    unittest.main()
