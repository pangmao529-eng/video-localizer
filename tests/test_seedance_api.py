import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from video_localizer import backend, batch, config, seedance
from video_localizer.model import _NoRedirect


class SeedanceApiTests(unittest.TestCase):
    def setUp(self):
        self.data = {"provider": "seedance", "base_url": config.SEEDANCE_DEFAULT_ENDPOINT,
                     "model": config.SEEDANCE_DEFAULT_MODEL}
        self.secret = "synthetic-credential-not-real"
        self.addCleanup(patch.stopall)
        patch.object(config, "settings", return_value=self.data).start()
        self.key_reader = patch.object(config, "read_key", return_value=self.secret).start()
        self.opener = Mock()
        self.builder = patch.object(seedance, "build_opener", return_value=self.opener).start()

    def reply(self, value):
        response = Mock(status=200)
        response.read.return_value = json.dumps(value).encode()
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        self.opener.open.return_value = context
        return response

    def test_authentication_uses_get_only_and_does_not_return_task_contents(self):
        private_url = "https://example.test/private-task-video.mp4"
        response = self.reply({"items": [{"content": {"video_url": private_url}}], "total": 1})
        result = seedance.check_api()
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(request.full_url, self.data["base_url"] + "/contents/generations/tasks?page_size=1")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + self.secret)
        self.assertIsInstance(self.builder.call_args.args[0], _NoRedirect)
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["authentication_verified"])
        for field in ("model_access_verified", "generation_submitted", "automatic_visual_review_available"):
            self.assertFalse(result[field])
        self.assertTrue(result["video_workflow_ready"])
        self.assertNotIn(self.secret, json.dumps(result))
        self.assertNotIn(private_url, json.dumps(result))
        response.read.assert_called_once_with(seedance.MAX_PROBE_BYTES + 1)

    def test_server_failure_never_echoes_credentials_or_server_body(self):
        for code, expected in ((401, "authentication"), (403, "permission_denied"),
                               (302, "redirect_blocked"), (429, "http_error")):
            with self.subTest(code=code):
                self.opener.open.side_effect = HTTPError(self.data["base_url"], code,
                    self.secret, {}, io.BytesIO(self.secret.encode()))
                result = seedance.check_api()
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error_code"], expected)
                self.assertFalse(result["authentication_verified"])
                self.assertNotIn(self.secret, json.dumps(result))

    def test_endpoint_authentication_is_not_mistaken_for_model_access_verification(self):
        self.data.update(model="ep-20261126155948-example", model_family=config.SEEDANCE_20_FAMILY)
        self.reply({"items": [], "total": 0})
        result = seedance.check_api()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["model"], self.data["model"])
        self.assertEqual(result["model_family"], config.SEEDANCE_20_FAMILY)
        self.assertTrue(result["authentication_verified"])
        self.assertFalse(result["model_access_verified"])
        self.assertFalse(result["generation_submitted"])
        self.assertEqual(self.opener.open.call_args.args[0].get_method(), "GET")

    def test_unexpected_response_cannot_pass_authentication(self):
        for payload in (b"{}", b"<html>ok</html>", b"x" * (seedance.MAX_PROBE_BYTES + 1)):
            response = self.reply({})
            response.read.return_value = payload
            result = seedance.check_api()
            self.assertEqual(result["error_code"], "invalid_response")
            self.assertFalse(result["authentication_verified"])

    def test_network_error_is_sanitized(self):
        self.opener.open.side_effect = URLError(self.secret)
        result = seedance.check_api()
        self.assertEqual(result["error_code"], "network")
        self.assertNotIn(self.secret, json.dumps(result))

    def test_cancellation_and_invalid_timeout_do_not_read_credentials(self):
        self.assertEqual(seedance.check_api(cancelled=lambda: True)["status"], "cancelled")
        for timeout in (True, float("nan"), 0, 601):
            self.assertEqual(seedance.check_api(timeout=timeout)["error_code"], "invalid_input")
        self.key_reader.assert_not_called()
        self.opener.open.assert_not_called()

    def test_backend_never_sends_seedance_credentials_to_hunyuan(self):
        with patch.object(backend, "settings", return_value=self.data), \
             patch("video_localizer.hunyuan.check_model") as old_backend, \
             patch.object(seedance, "check_api", return_value={"status": "passed"}) as current:
            self.assertEqual(backend.check_model()["status"], "passed")
            current.assert_called_once()
            old_backend.assert_not_called()
            current.reset_mock()
            self.assertEqual(backend.check_model(image_path="not-read.png")["error_code"], "unsupported_probe")
            current.assert_not_called()
            old_backend.assert_not_called()

    def test_unknown_provider_does_not_fall_back(self):
        with patch.object(backend, "settings", return_value={**self.data, "provider": "unknown"}), \
             patch("video_localizer.hunyuan.check_model") as old_backend:
            self.assertEqual(backend.check_model()["status"], "failed")
            old_backend.assert_not_called()

    def test_unknown_provider_cannot_read_credentials(self):
        with patch.object(batch, "settings", return_value={**self.data, "provider": "unknown"}), \
             patch.object(batch, "read_key") as read_key, patch.object(batch, "runtime_paths") as paths:
            with self.assertRaisesRegex(config.SettingsError, "提供方"):
                batch._connection()
            read_key.assert_not_called()
            paths.assert_not_called()

    def test_doctor_distinguishes_configured_from_workflow_ready(self):
        with patch.object(batch, "settings", return_value=self.data), \
             patch.object(batch, "read_key", return_value=self.secret), \
             patch.object(batch, "runtime_paths", return_value={}):
            result = batch.doctor()
        self.assertEqual(result["provider"], "seedance")
        self.assertEqual(result["workflow"], "seedance_video_api")
        self.assertTrue(result["model_configured"])
        self.assertFalse(result["model_contacted"])
        self.assertTrue(result["video_workflow_ready"])
        self.assertFalse(result["automatic_visual_review_available"])


if __name__ == "__main__":
    unittest.main()
