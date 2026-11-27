import io
import json
import socket
import unittest
from unittest.mock import Mock, patch

from video_localizer import mediakit_client as mc


# Sanitized real Detailed OCR response for clip 9, first ten seconds, 2026-11-27.
# A single-frame Others detection legitimately has identical start/end times;
# the transport must preserve it for the binding layer instead of failing a task.
CLIP9_OCR_ROWS = [
    ("AI生成", "Others", 0, 3.233, (1024, 623, 1271, 708)),
    ("师姐你的战力怎么提升这么快", "Subtitle", 0.766, 2.933, (443, 615, 822, 658)),
    ("生成", "Others", 3.266, 7.066, (1030, 626, 1263, 715)),
    ("你以为要练很多种心法", "Subtitle", 3.766, 5.5, (499, 619, 782, 658)),
    ("生成", "Others", 6.3, 6.3, (1118, 626, 1263, 703)),
    ("生成", "Others", 6.5, 6.633, (1110, 626, 1263, 703)),
    ("错了", "Others", 6.666, 7.233, (603, 619, 670, 658)),
    ("生成", "Others", 7.033, 7.333, (1106, 626, 1263, 703)),
    ("A生成", "Others", 7.366, 9.966, (964, 619, 1265, 713)),
    ("资源只给一个先拉满", "Subtitle", 7.666, 9.766, (511, 615, 766, 658)),
]


class Response:
    def __init__(self, value=None, *, raw=None, status=200, headers=None):
        self.status = status
        self.stream = io.BytesIO(json.dumps(value).encode() if raw is None else raw)
        self.headers = headers or {}

    def read1(self, amount):
        return self.stream.read(amount)

    def getheader(self, name):
        return self.headers.get(name)


class MediaKitClientTests(unittest.TestCase):
    def setUp(self):
        self.secret = "synthetic-mediakit-credential-not-real"
        self.client = mc.MediaKitClient(self.secret)
        self.url = "https://media.example.com/clip.mp4?signature=synthetic-ticket"
        self.conn = Mock(sock=None)
        self.dns = patch.object(mc, "_public_addresses", return_value=["8.8.8.8"]).start()
        self.factory = patch.object(mc, "_PinnedHTTPSConnection", return_value=self.conn).start()
        self.addCleanup(patch.stopall)

    def response(self, value=None, **kwargs):
        self.conn.getresponse.return_value = Response(value, **kwargs)

    def assert_error(self, call, code, uncertain=False):
        with self.assertRaises(mc.SeedanceError) as caught:
            call()
        error = caught.exception
        self.assertEqual(error.code, code)
        self.assertEqual(error.submission_uncertain, uncertain)
        self.assertFalse(error.retryable)
        for value in (str(error), repr(error), json.dumps(vars(error))):
            self.assertNotIn(self.secret, value)
            self.assertNotIn("synthetic-ticket", value)
        return error

    def success(self, task_id="amk-tool-video-ocr-test"):
        self.response({"success": True, "task_id": task_id, "request_id": "request-test-001",
                       "ignored_server_field": self.secret})

    def body(self):
        return json.loads(self.conn.request.call_args.kwargs["body"])

    def test_ocr_preserves_client_token_and_native_task_response(self):
        self.success()
        result = self.client.submit_video_ocr(self.url, "vl-test-001")
        self.assertEqual(result, {"success": True, "task_id": "amk-tool-video-ocr-test",
                                  "request_id": "request-test-001"})
        self.assertEqual(self.body(), {"video_url": self.url, "mode": "Detailed", "client_token": "vl-test-001"})
        args, kwargs = self.conn.request.call_args
        self.assertEqual(args, ("POST", "/api/v1/tools/video-ocr"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + self.secret)
        self.assertEqual(int(kwargs["headers"]["Content-Length"]), len(kwargs["body"]))
        self.dns.assert_called_once_with("mediakit.cn-beijing.volces.com")
        self.factory.assert_called_once_with("mediakit.cn-beijing.volces.com", "8.8.8.8", timeout=unittest.mock.ANY)
        self.conn.request.assert_called_once()
        self.conn.close.assert_called_once()

    def test_erase_preserves_boxes_time_ranges_and_algorithm(self):
        self.success("amk-tool-erase-video-subtitle-pro-test")
        box = {"top_left_x": 0.1, "top_left_y": 0.8, "bottom_right_x": 0.8, "bottom_right_y": 0.95}
        segments = [{"start_time": 1.25, "end_time": 3.5}, {"start_time": 4, "end_time": 9.75}]
        self.client.submit_erase(self.url, "vl-erase-001", boxes=[box], time_segments=segments)
        self.assertEqual(self.body(), {"video_url": self.url, "client_token": "vl-erase-001",
            "mode": "Subtitle", "model_version": "v5.6", "output_encode_mode": "Quality", "output_format": "mp4",
            "erase_ratio_location": [box], "time_segment_filter": {"mode": "selected", "segments": segments}})
        self.assertEqual(self.conn.request.call_args.args, ("POST", "/api/v1/tools/erase-video-subtitle-pro"))

    def test_per_segment_boxes_and_skip_filter(self):
        self.success("amk-tool-erase-video-subtitle-pro-test")
        box = {"top_left_x": 0.1, "top_left_y": 0.6, "bottom_right_x": 0.9, "bottom_right_y": 0.9}
        segments = [{"start_time": 0, "end_time": 4, "erase_ratio_location": [box]},
                    {"start_time": 5, "end_time": 10, "erase_ratio_location": []}]
        self.client.submit_erase(self.url, "vl-erase-002", mode="Text", time_segments=segments)
        self.assertEqual(self.body()["time_segment_filter"]["segments"], segments)
        self.success("amk-tool-erase-video-subtitle-pro-test")
        self.client.submit_erase(self.url, "vl-erase-003", time_segments={"mode": "skip", "segments": [
            {"start_time": 0, "end_time": 1}]})
        self.assertEqual(self.body()["time_segment_filter"]["mode"], "skip")

    def test_invalid_inputs_never_connect(self):
        for url in ("http://media.example.com/x.mp4", "https://127.0.0.1/x.mp4", "https://localhost/x",
                    "https://u:p@media.example.com/x", "https://media.example.com/x#fragment",
                    "https://media.example.com/x?key=" + self.secret, "https://media.example.com:444/x"):
            with self.subTest(url=url[:20]):
                self.assert_error(lambda: self.client.submit_video_ocr(url, "token"), "invalid_input")
        for token in (None, "", "x" * 65, "中文", "inject\r\nheader", self.secret):
            with self.subTest(token_type=type(token).__name__):
                self.assert_error(lambda: self.client.submit_video_ocr(self.url, token), "invalid_input")
        self.assert_error(lambda: self.client.submit_video_ocr(self.url, "token", mode="Unknown"), "invalid_input")
        self.factory.assert_not_called()

    def test_endpoint_key_timeout_validation(self):
        for base in ("http://mediakit.cn-beijing.volces.com/api/v1", "https://evil.example/api/v1",
                     mc.DEFAULT_BASE_URL + "?key=1", None):
            self.assert_error(lambda: mc.MediaKitClient(self.secret, base_url=base), "configuration")
        for key in ("", None, "key\nheader", "密钥"):
            self.assert_error(lambda: mc.MediaKitClient(key), "configuration")
        for seconds in (0, 1201, True, float("nan"), float("inf"), 10**400):
            self.assert_error(lambda: mc.MediaKitClient(self.secret, timeout=seconds), "invalid_input")
        self.factory.assert_not_called()

    def test_invalid_box_and_segment_combinations_never_connect(self):
        good = {"top_left_x": 0.1, "top_left_y": 0.6, "bottom_right_x": 0.9, "bottom_right_y": 0.9}
        for boxes in ([], [good] * 21, [{**good, "top_left_x": -1}], [{**good, "bottom_right_x": 0.01}],
                      [{**good, "top_left_y": True}], [{**good, "bottom_right_y": float("nan")}],
                      [{**good, "unknown": 1}], "all"):
            self.assert_error(lambda: self.client.submit_erase(self.url, "token", boxes=boxes), "invalid_input")
        for ranges in ([], [{"start_time": 1, "end_time": 0}], [{"start_time": 0, "end_time": float("inf")}],
                       {"mode": "all", "segments": [{"start_time": 0, "end_time": 1}]}):
            self.assert_error(lambda: self.client.submit_erase(self.url, "token", time_segments=ranges), "invalid_input")
        segment = {"start_time": 0, "end_time": 1, "erase_ratio_location": [good]}
        self.assert_error(lambda: self.client.submit_erase(self.url, "token", boxes=[good],
                          time_segments=[segment]), "invalid_input")
        self.assert_error(lambda: self.client.submit_erase(self.url, "token",
                          time_segments={"mode": "skip", "segments": [segment]}), "invalid_input")
        self.factory.assert_not_called()

    def test_http_business_rejection_is_safe_and_definite(self):
        self.response({"success": False, "request_id": "safe-request-id", "error": {
            "code": "InvalidParameter", "message": self.secret + self.url}})
        error = self.assert_error(lambda: self.client.submit_video_ocr(self.url, "token"), "provider_error")
        self.assertEqual(error.provider_code, "InvalidParameter")
        self.assertEqual(error.request_id, "safe-request-id")
        self.conn.request.assert_called_once()

    def test_http_errors_top_level_and_nested_code_without_server_prose(self):
        for status, code, uncertain in ((401, "authentication", False), (403, "permission_denied", False),
                                        (400, "http_error", False), (408, "http_error", True),
                                        (503, "http_error", True), (302, "redirect_blocked", True)):
            for nested in (True, False):
                with self.subTest(status=status, nested=nested):
                    err = {"code": "ServiceFailure", "message": self.secret + self.url}
                    self.response({"error": err} if nested else err, status=status,
                                  headers={"X-Request-Id": "safe-header-id"})
                    error = self.assert_error(lambda: self.client.submit_video_ocr(self.url, "token"), code, uncertain)
                    self.assertEqual(error.provider_code, "ServiceFailure")
                    self.assertEqual(error.request_id, "safe-header-id")

    def test_credentials_in_provider_metadata_are_not_exposed(self):
        self.response({"success": False, "request_id": self.secret, "error": {"code": self.secret,
            "message": self.secret}}, headers={"X-Request-Id": self.secret})
        error = self.assert_error(lambda: self.client.submit_video_ocr(self.url, "token"), "provider_error")
        self.assertIsNone(error.provider_code)
        self.assertIsNone(error.request_id)

    def test_connect_failure_is_definite_but_post_timeout_is_uncertain(self):
        self.conn.connect.side_effect = socket.timeout(self.secret + self.url)
        self.assert_error(lambda: self.client.submit_video_ocr(self.url, "token"), "network")
        self.conn.request.assert_not_called()
        self.conn.connect.side_effect = None
        self.conn.getresponse.side_effect = socket.timeout(self.secret + self.url)
        error = self.assert_error(lambda: self.client.submit_video_ocr(self.url, "token"), "network", True)
        self.assertEqual(error.failure_stage, "response_headers")
        self.assertGreater(error.completed_body_bytes, 0)
        self.conn.request.assert_called_once()

    def test_get_timeout_never_becomes_submission_uncertain(self):
        self.conn.getresponse.side_effect = socket.timeout(self.secret)
        self.assert_error(lambda: self.client.get_task("amk-tool-video-ocr-test"), "network")

    def test_malformed_create_is_uncertain_and_never_retried(self):
        for value in ({"success": True}, {"success": "true"}, {"task_id": "amk-tool-video-ocr-test"},
                      {"success": False, "task_id": "amk-tool-video-ocr-test"},
                      {"success": True, "task_id": "../bad"}):
            with self.subTest(value=value):
                self.response(value)
                self.assert_error(lambda: self.client.submit_video_ocr(self.url, "token"), "invalid_response", True)
        self.assertEqual(self.conn.request.call_count, 5)

    def test_unparseable_or_oversized_create_response_is_uncertain(self):
        for raw in (b"not-json", b"[]", b"x" * (mc.MAX_RESPONSE_BYTES + 1)):
            self.response(raw=raw)
            self.assert_error(lambda: self.client.submit_video_ocr(self.url, "token"), "invalid_response", True)

    def test_get_running_uses_native_state_and_never_returns_extra_fields(self):
        self.response({"success": True, "task_id": "amk-tool-video-ocr-test", "status": "running",
                       "task_type": "video-ocr", "created_at": 1234, "request_id": "safe-id",
                       "parameters": {"video_url": self.url}, "secret_echo": self.secret})
        result = self.client.get_task("amk-tool-video-ocr-test")
        self.assertEqual(result, {"success": True, "task_id": "amk-tool-video-ocr-test", "status": "running",
                                  "task_type": "video-ocr", "created_at": 1234, "request_id": "safe-id"})
        args, kwargs = self.conn.request.call_args
        self.assertEqual(args, ("GET", "/api/v1/tasks/amk-tool-video-ocr-test"))
        self.assertIsNone(kwargs["body"])
        self.assertNotIn("Content-Length", kwargs["headers"])

    def test_get_completed_ocr_preserves_text_timing_and_location(self):
        subtitle = {"subtitle_text": "师姐，你的战力怎么提升这么快？", "start_time": 0.8, "end_time": 3.3,
                    "text_label": "Subtitle", "text_location": {"top_left_x": 450, "top_left_y": 625,
                    "bottom_right_x": 830, "bottom_right_y": 653}}
        self.response({"success": True, "task_id": "amk-tool-video-ocr-test", "status": "completed",
                       "result": {"duration": 10, "subtitles": [{**subtitle, "debug": self.secret}]}})
        result = self.client.get_task("amk-tool-video-ocr-test")
        self.assertEqual(result["result"], {"duration": 10, "subtitles": [subtitle]})

    def test_real_clip9_ocr_preserves_single_frame_others_detection(self):
        subtitles = [{"subtitle_text": text, "text_label": label, "start_time": start, "end_time": end,
                      "text_location": dict(zip(("top_left_x", "top_left_y", "bottom_right_x", "bottom_right_y"), box))}
                     for text, label, start, end, box in CLIP9_OCR_ROWS]
        self.response({"success": True, "task_id": "amk-tool-video-ocr-test", "status": "completed",
                       "result": {"duration": 10, "subtitles": subtitles}})
        result = self.client.get_task("amk-tool-video-ocr-test")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["subtitles"], subtitles)
        self.assertEqual(result["result"]["subtitles"][4]["start_time"], 6.3)
        self.assertEqual(result["result"]["subtitles"][4]["end_time"], 6.3)

    def test_zero_length_subtitle_is_preserved_for_binding_review_but_reversed_is_rejected(self):
        subtitle = {"subtitle_text": "字幕", "text_label": "Subtitle", "start_time": 1, "end_time": 1}
        self.response({"success": True, "task_id": "amk-tool-video-ocr-test", "status": "completed",
                       "result": {"duration": 10, "subtitles": [subtitle]}})
        self.assertEqual(self.client.get_task("amk-tool-video-ocr-test")["result"]["subtitles"], [subtitle])
        self.response({"success": True, "task_id": "amk-tool-video-ocr-test", "status": "completed",
                       "result": {"duration": 10, "subtitles": [{**subtitle, "end_time": 0.5}]}})
        self.assert_error(lambda: self.client.get_task("amk-tool-video-ocr-test"), "invalid_response")

    def test_get_completed_erase_allows_signed_result_url_in_memory(self):
        self.response({"success": True, "task_id": "amk-tool-erase-test", "status": "completed",
                       "result": {"duration": 10, "video_url": self.url}})
        result = self.client.get_task("amk-tool-erase-test")
        self.assertEqual(result["result"], {"duration": 10, "video_url": self.url})

    def test_get_failed_is_task_failure_not_query_failure(self):
        self.response({"success": True, "task_id": "amk-tool-erase-test", "status": "failed",
                       "error": {"code": "AbilityProcessingError", "message": self.secret + self.url}})
        result = self.client.get_task("amk-tool-erase-test")
        self.assertEqual(result["error"], {"code": "AbilityProcessingError"})
        self.assertEqual(result["status"], "failed")
        self.assertNotIn(self.secret, json.dumps(result))

    def test_invalid_task_status_and_result_are_rejected(self):
        for status in ("succeeded", None, {}, ["completed"]):
            self.response({"success": True, "task_id": "amk-tool-erase-test", "status": status})
            self.assert_error(lambda: self.client.get_task("amk-tool-erase-test"), "invalid_response")
        for result in ({"duration": 10, "video_url": "http://unsafe.example/video.mp4"},
                       {"duration": 10, "video_url": "https://media.example.com/x?key=" + self.secret},
                       {"duration": float("nan"), "subtitles": []}, {"duration": 10},
                       {"duration": 10, "subtitles": [{"subtitle_text": self.secret, "start_time": 0, "end_time": 1}]}):
            self.response({"success": True, "task_id": "amk-tool-erase-test", "status": "completed",
                           "request_id": "validation-request-id", "result": result})
            error = self.assert_error(lambda: self.client.get_task("amk-tool-erase-test"), "invalid_response")
            self.assertEqual(error.request_id, "validation-request-id")
            self.assertEqual(error.failure_stage, "response_validation")

    def test_cancellation_before_connect_does_not_submit(self):
        client = mc.MediaKitClient(self.secret, cancelled=lambda: True)
        self.assert_error(lambda: client.submit_video_ocr(self.url, "token"), "cancelled")
        self.factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
