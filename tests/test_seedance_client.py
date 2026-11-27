import base64
import hashlib
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from video_localizer import seedance_client as sc


class Response:
    def __init__(self, value=None, *, raw=None, status=200, headers=None):
        self.status = status
        self.stream = io.BytesIO(json.dumps(value).encode() if raw is None else raw)
        self.headers = headers or {}

    def read1(self, amount):
        return self.stream.read(amount)

    def getheader(self, name):
        return self.headers.get(name)


class SeedanceClientTests(unittest.TestCase):
    def setUp(self):
        self.secret = "synthetic-credential-not-real"
        self.base = "https://ark.cn-beijing.volces.com/api/v3"
        self.client = sc.SeedanceClient(self.base, self.secret)
        self.conn = Mock(sock=None)
        self.connection_patch = patch.object(sc.http.client, "HTTPSConnection", return_value=self.conn)
        self.factory = self.connection_patch.start()
        self.addCleanup(patch.stopall)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def response(self, value=None, **kwargs):
        self.conn.getresponse.return_value = Response(value, **kwargs)

    def assert_error(self, call, code, uncertain=False):
        with self.assertRaises(sc.SeedanceError) as captured:
            call()
        error = captured.exception
        self.assertEqual(error.code, code)
        self.assertEqual(error.submission_uncertain, uncertain)
        self.assertNotIn(self.secret, str(error))
        self.assertNotIn(self.secret, repr(error))
        self.assertNotIn(self.secret, json.dumps(vars(error)))
        self.assertFalse(error.retryable)
        return error

    def test_get_task_uses_exact_path_bearer_and_empty_body(self):
        self.response({"id": "cgt-protocol-1", "status": "running"})
        self.assertEqual(self.client.get_task("cgt-protocol-1"), {"id": "cgt-protocol-1", "status": "running"})
        args, kwargs = self.conn.request.call_args
        self.assertEqual(args, ("GET", "/api/v3/contents/generations/tasks/cgt-protocol-1"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + self.secret)
        self.assertIsNone(kwargs["body"])
        self.assertNotIn("Content-Length", kwargs["headers"])
        self.conn.request.assert_called_once()

    def test_seedance_20_preserves_https_video_and_two_image_references(self):
        video = "https://media.example.com/original.mp4?signature=synthetic-video"
        images = ["https://media.example.com/first.png?signature=synthetic-first",
                  "https://media.example.com/second.jpg?signature=synthetic-second"]
        self.response({"id": "cgt-https-references"})
        self.client.create_edit(video, "edit", reference_images=images)
        body = json.loads(self.conn.request.call_args.kwargs["body"])
        self.assertEqual(body["model"], sc.SEEDANCE_20_MODEL)
        self.assertNotIn("omni_reference_task_type", body)
        self.assertEqual(body["content"], [
            {"type": "text", "text": "edit"},
            {"type": "video_url", "role": "reference_video", "video_url": {"url": video}},
            *[{"type": "image_url", "role": "reference_image", "image_url": {"url": url}} for url in images],
        ])

    def test_create_edit_uses_exact_configured_endpoint_and_defaults(self):
        self.response({"id": "cgt-test-123"})
        reference = "asset://asset-test-123"
        image = "data:image/png;base64," + base64.b64encode(b"synthetic image").decode()
        self.assertEqual(self.client.create_edit(reference, "将视频1中文字改为韩语", reference_images=[image]),
                         "cgt-test-123")
        self.factory.assert_called_once_with("ark.cn-beijing.volces.com", None, timeout=unittest.mock.ANY)
        args, kwargs = self.conn.request.call_args
        self.assertEqual(args, ("POST", "/api/v3/contents/generations/tasks"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + self.secret)
        body = json.loads(kwargs["body"])
        self.assertEqual(body["model"], sc.SEEDANCE_20_MODEL)
        self.assertEqual(sc.DEFAULT_MODEL, sc.SEEDANCE_20_MODEL)
        self.assertNotIn("omni_reference_task_type", body)
        self.assertEqual(body["ratio"], "adaptive")
        self.assertEqual(body["duration"], -1)
        self.assertTrue(body["generate_audio"])
        self.assertFalse(body["watermark"])
        self.assertEqual(body["content"][1], {"type": "video_url", "role": "reference_video",
                                                "video_url": {"url": reference}})
        self.assertEqual(body["content"][2]["role"], "reference_image")
        self.assertEqual(int(kwargs["headers"]["Content-Length"]), len(kwargs["body"]))
        self.conn.close.assert_called_once()

    def test_creation_correlation_id_is_sent_without_claiming_idempotency(self):
        self.response({"id": "cgt-traced-creation"})
        trace = "vl-0123456789abcdef0123456789abcdef"
        self.assertEqual(self.client.create_edit("asset://asset-video", "edit", client_request_id=trace),
                         "cgt-traced-creation")
        headers = self.conn.request.call_args.kwargs["headers"]
        self.assertEqual(headers["X-Client-Request-Id"], trace)
        self.assertNotIn("Idempotency-Key", headers)
        self.conn.request.assert_called_once()

    def test_creation_correlation_id_is_optional_and_validated_before_connecting(self):
        for trace in ("", "x" * 129, "contains spaces", "注入", "header\r\nvalue", 1, {},
                      self.secret, "prefix-" + self.secret):
            with self.subTest(trace_type=type(trace).__name__):
                self.assert_error(lambda: self.client.create_edit("asset://asset-video", "edit", client_request_id=trace),
                                  "invalid_input")
        self.factory.assert_not_called()
        for trace in (None, "_" + "x" * 127):
            with self.subTest(optional=trace is None):
                self.response({"id": "cgt-optional-trace"})
                self.client.create_edit("asset://asset-video", "edit", client_request_id=trace)
                headers = self.conn.request.call_args.kwargs["headers"]
                if trace is None:
                    self.assertNotIn("X-Client-Request-Id", headers)
                else:
                    self.assertEqual(headers["X-Client-Request-Id"], trace)

    def test_seedance_25_retains_explicit_edit_request_and_thirty_images(self):
        self.response({"id": "cgt-test-25"})
        client = sc.SeedanceClient(self.base, self.secret, sc.SEEDANCE_25_MODEL)
        images = ["asset://asset-image-" + str(index) for index in range(30)]
        self.assertEqual(client.create_edit("asset://asset-video", "edit video", reference_images=images),
                         "cgt-test-25")
        body = json.loads(self.conn.request.call_args.kwargs["body"])
        self.assertEqual(body["model"], sc.SEEDANCE_25_MODEL)
        self.assertEqual(body["omni_reference_task_type"], "edit")
        self.assertEqual(body["ratio"], "adaptive")
        self.assertEqual(body["duration"], -1)
        self.assertEqual(len(body["content"]), 32)

    def test_endpoint_id_is_preserved_and_explicit_family_controls_capabilities(self):
        endpoint = "ep-20261126155948-example"
        for family, limit in ((sc.SEEDANCE_20_FAMILY, 9), (sc.SEEDANCE_25_FAMILY, 30)):
            with self.subTest(family=family):
                client = sc.SeedanceClient(self.base, self.secret, endpoint, model_family=family)
                self.assertEqual(client.model_family, family)
                self.response({"id": "cgt-endpoint-task"})
                images = ["asset://asset-image-" + str(index) for index in range(limit)]
                self.assertEqual(client.create_edit("https://media.example.com/video.mp4?signature=synthetic",
                                                   "edit", reference_images=images), "cgt-endpoint-task")
                body = json.loads(self.conn.request.call_args.kwargs["body"])
                self.assertEqual(body["model"], endpoint)
                self.assertEqual(len(body["content"]), limit + 2)
                self.assertEqual(body["ratio"], "adaptive")
                self.assertEqual(body["duration"], -1)
                self.assertTrue(body["generate_audio"])
                if family == sc.SEEDANCE_20_FAMILY:
                    self.assertNotIn("omni_reference_task_type", body)
                else:
                    self.assertEqual(body["omni_reference_task_type"], "edit")

    def test_endpoint_requires_explicit_family_and_direct_models_reject_mismatch(self):
        for model, family in (("ep-20261126155948-example", None),
                              ("ep-20261126155948-example", "unsupported-family"),
                              (" ep-20261126155948-example ", sc.SEEDANCE_20_FAMILY),
                              (sc.SEEDANCE_20_MODEL, sc.SEEDANCE_25_FAMILY),
                              (sc.SEEDANCE_25_MODEL, sc.SEEDANCE_20_FAMILY)):
            with self.subTest(model=model, family=family):
                self.assert_error(lambda: sc.SeedanceClient(self.base, self.secret, model,
                                                            model_family=family), "configuration")
        self.factory.assert_not_called()

    def test_direct_model_family_inference_and_matching_explicit_family_are_compatible(self):
        for model, family in ((sc.SEEDANCE_20_MODEL, sc.SEEDANCE_20_FAMILY),
                              (sc.SEEDANCE_25_MODEL, sc.SEEDANCE_25_FAMILY)):
            with self.subTest(model=model):
                inferred = sc.SeedanceClient(self.base, self.secret, model)
                explicit = sc.SeedanceClient(self.base, self.secret, model, model_family=family)
                self.assertEqual(inferred.model_family, family)
                self.assertEqual(explicit.model_family, family)
                self.assertEqual(inferred.model, model)
                self.assertEqual(explicit.model, model)

    def test_reference_image_limit_is_model_specific_before_request(self):
        for model, family, limit in ((sc.SEEDANCE_20_MODEL, None, 9), (sc.SEEDANCE_25_MODEL, None, 30),
                                     ("ep-20261126155948-example", sc.SEEDANCE_20_FAMILY, 9),
                                     ("ep-20261126155948-example", sc.SEEDANCE_25_FAMILY, 30)):
            with self.subTest(model=model, family=family):
                client = sc.SeedanceClient(self.base, self.secret, model, model_family=family)
                images = ["asset://asset-image-" + str(index) for index in range(limit + 1)]
                self.assert_error(lambda: client.create_edit("asset://asset-video", "edit", reference_images=images),
                                  "invalid_input")
        self.factory.assert_not_called()

    def test_generate_audio_false_is_sent_for_both_models_without_changing_default(self):
        for model in (sc.SEEDANCE_20_MODEL, sc.SEEDANCE_25_MODEL):
            with self.subTest(model=model):
                self.response({"id": "cgt-test-audio"})
                client = sc.SeedanceClient(self.base, self.secret, model)
                client.create_edit("asset://asset-video", "edit", generate_audio=False)
                self.assertIs(json.loads(self.conn.request.call_args.kwargs["body"])["generate_audio"], False)
                self.response({"id": "cgt-test-audio-default"})
                client.create_edit("asset://asset-video", "edit")
                self.assertIs(json.loads(self.conn.request.call_args.kwargs["body"])["generate_audio"], True)

    def test_unknown_model_and_non_boolean_audio_are_rejected(self):
        for model in ("doubao-seedance-2-0-fast-260128", "unsupported-model", [], None):
            self.assert_error(lambda: sc.SeedanceClient(self.base, self.secret, model), "configuration")
        for audio in (0, 1, "false", None):
            self.assert_error(lambda: self.client.create_edit("asset://asset-video", "edit", generate_audio=audio),
                              "invalid_input")
        self.factory.assert_not_called()

    def test_local_video_upload_streams_original_bytes_to_ark_files(self):
        original = b"original bytes\r\n" * 40000
        source = self.root / "原始文件.mp4"
        source.write_bytes(original)
        payloads = []
        def consume(method, path, *, body, headers):
            payloads.append(b"".join(body))
            self.assertEqual(method, "POST")
            self.assertEqual(path, "/api/v3/files")
            self.assertEqual(headers["Authorization"], "Bearer " + self.secret)
            self.assertEqual(int(headers["Content-Length"]), len(payloads[0]))
        self.conn.request.side_effect = consume
        signed = "https://ark-files.tos-cn-beijing.volces.com/source.mp4?signature=temporary"
        self.response({"object": "file", "id": "file-source-1", "status": "active", "download_url": signed})
        result = self.client.upload_video(source)
        self.assertEqual(result, {"file_id": "file-source-1", "status": "active", "video_reference": signed})
        self.assertIn(b'name="purpose"\r\n\r\nagent\r\n', payloads[0])
        self.assertIn(b'filename="source.mp4"', payloads[0])
        self.assertIn(original, payloads[0])
        self.assertNotIn(str(source).encode(), payloads[0])
        self.assertEqual(source.read_bytes(), original)

    def test_slow_upload_over_sixty_seconds_uses_separate_bounded_budget(self):
        source = self.root / "slow.mp4"
        original = b"v" * (sc.CHUNK_BYTES * 5)
        source.write_bytes(original)
        clock = {"now": 0}
        sent = []
        self.conn.sock = Mock()
        def consume(_method, _path, *, body, headers):
            for chunk in body:
                sent.append(chunk)
                clock["now"] += 20  # Sustained progress, no blocking sleeps.
            self.assertEqual(sum(map(len, sent)), int(headers["Content-Length"]))
        self.conn.request.side_effect = consume
        self.response({"id": "file-slow", "status": "processing"})
        with patch.object(sc.time, "monotonic", side_effect=lambda: clock["now"]):
            result = self.client.upload_video(source)
        self.assertGreater(clock["now"], 60)
        self.assertLess(clock["now"], sc.DEFAULT_UPLOAD_TIMEOUT)
        self.assertEqual(result["file_id"], "file-slow")
        self.assertIn(original, b"".join(sent))
        self.assertGreaterEqual(self.conn.sock.settimeout.call_count, len(sent))
        timeouts = [call.args[0] for call in self.conn.sock.settimeout.call_args_list]
        self.assertTrue(all(0 < value <= sc.DEFAULT_UPLOAD_TIMEOUT for value in timeouts))
        self.assertEqual(timeouts, sorted(timeouts, reverse=True))
        self.conn.request.assert_called_once()

    def test_slow_creation_response_uses_remaining_budget_without_resending(self):
        # Model a real socket's timeout enforcement without a 45-second sleep.
        # The former global 30-second cap fails this POST after its body is sent.
        clock, socket_state = {"now": 0}, {"timeout": None}
        client = sc.SeedanceClient(self.base, self.secret, timeout=10, create_timeout=60)
        self.conn.sock = Mock()
        self.conn.sock.settimeout.side_effect = lambda value: socket_state.update(timeout=value)
        self.conn.connect.side_effect = lambda: clock.update(now=clock["now"] + 2)
        self.conn.request.side_effect = lambda *args, **kwargs: clock.update(now=clock["now"] + 3)
        def reply():
            waiting = 45
            available = socket_state["timeout"]
            clock["now"] += min(waiting, available)
            if available < waiting:
                raise TimeoutError("synthetic response delay")
            return Response({"id": "cgt-delayed-creation"})
        self.conn.getresponse.side_effect = reply
        with patch.object(sc.time, "monotonic", side_effect=lambda: clock["now"]):
            result = client.create_edit("asset://asset-video", "字幕参考译文 번역 참고 " * 500)
        self.assertEqual(result, "cgt-delayed-creation")
        self.assertEqual(clock["now"], 50)
        self.factory.assert_called_once_with("ark.cn-beijing.volces.com", None, timeout=30)
        self.assertIn(55, [call.args[0] for call in self.conn.sock.settimeout.call_args_list])
        self.assertEqual(socket_state["timeout"], 10)
        self.conn.request.assert_called_once()
        self.conn.getresponse.assert_called_once()
        self.conn.close.assert_called_once()

    def test_response_wait_exhausts_original_budget_and_stays_uncertain(self):
        clock, socket_state = {"now": 0}, {"timeout": None}
        client = sc.SeedanceClient(self.base, self.secret, timeout=120, create_timeout=40)
        self.conn.sock = Mock()
        self.conn.sock.settimeout.side_effect = lambda value: socket_state.update(timeout=value)
        self.conn.connect.side_effect = lambda: clock.update(now=clock["now"] + 2)
        self.conn.request.side_effect = lambda *args, **kwargs: clock.update(now=clock["now"] + 3)
        def reply():
            self.assertEqual(socket_state["timeout"], 35)
            clock["now"] += socket_state["timeout"]
            raise TimeoutError("synthetic response deadline")
        self.conn.getresponse.side_effect = reply
        with patch.object(sc.time, "monotonic", side_effect=lambda: clock["now"]):
            error = self.assert_error(lambda: client.create_edit("asset://asset-video", "edit"), "network", True)
        self.assertEqual(clock["now"], 40)
        self.assertEqual((error.failure_stage, error.exception_type), ("response_headers", "TimeoutError"))
        self.assertEqual(error.completed_body_bytes, len(self.conn.request.call_args.kwargs["body"]))
        self.conn.request.assert_called_once()
        self.conn.getresponse.assert_called_once()
        self.conn.close.assert_called_once()

    def test_creation_default_budget_and_validation_do_not_change_queries(self):
        self.assertEqual(self.client.create_timeout, 180)
        self.assertEqual(self.client.timeout, 60)
        for value in (True, None, "180", float("nan"), float("inf"), 0, 1201):
            with self.subTest(value=value):
                self.assert_error(lambda: sc.SeedanceClient(self.base, self.secret, create_timeout=value),
                                  "invalid_input")
        self.factory.assert_not_called()
        for value in (1, 180, 1200):
            self.assertEqual(sc.SeedanceClient(self.base, self.secret, create_timeout=value).create_timeout, value)
        clock = {"now": 0}
        client = sc.SeedanceClient(self.base, self.secret, timeout=12, create_timeout=180)
        self.conn.request.side_effect = lambda *args, **kwargs: clock.update(now=13)
        with patch.object(sc.time, "monotonic", side_effect=lambda: clock["now"]):
            self.assert_error(lambda: client.get_task("cgt-query-budget"), "timeout")
        self.conn.request.assert_called_once()
        self.conn.getresponse.assert_not_called()

    def test_upload_deadline_exhaustion_is_uncertain_without_retry(self):
        source = self.root / "slow.mp4"
        source.write_bytes(b"v" * (sc.CHUNK_BYTES * 5))
        clock = {"now": 0}
        client = sc.SeedanceClient(self.base, self.secret, upload_timeout=90)
        def consume(_method, _path, *, body, headers):
            for _chunk in body:
                clock["now"] += 30
        self.conn.request.side_effect = consume
        with patch.object(sc.time, "monotonic", side_effect=lambda: clock["now"]):
            self.assert_error(lambda: client.upload_video(source), "timeout", True)
        self.assertEqual(clock["now"], 90)
        self.conn.request.assert_called_once()
        self.conn.getresponse.assert_not_called()

    def test_upload_reply_uses_same_deadline_as_upload_not_new_sixty_seconds(self):
        source = self.root / "slow.mp4"
        source.write_bytes(b"v")
        clock = {"now": 0}
        client = sc.SeedanceClient(self.base, self.secret, upload_timeout=120)
        self.conn.sock = Mock()
        def consume(_method, _path, *, body, headers):
            for _chunk in body:
                clock["now"] += 30
        self.conn.request.side_effect = consume
        def reply():
            clock["now"] = 121
            return Response({"id": "file-slow", "status": "processing"})
        self.conn.getresponse.side_effect = reply
        with patch.object(sc.time, "monotonic", side_effect=lambda: clock["now"]):
            self.assert_error(lambda: client.upload_video(source), "timeout", True)
        self.conn.request.assert_called_once()
        self.conn.getresponse.assert_called_once()

    def test_regular_api_keeps_sixty_second_budget(self):
        clock = {"now": 0}
        def request(*args, **kwargs):
            clock["now"] += 61
        self.conn.request.side_effect = request
        with patch.object(sc.time, "monotonic", side_effect=lambda: clock["now"]):
            self.assert_error(lambda: self.client.get_task("cgt-123"), "timeout")
        self.conn.request.assert_called_once()
        self.conn.getresponse.assert_not_called()

    def test_upload_timeout_validation_and_stream_cancellation(self):
        for timeout in (True, float("nan"), 0, 1201):
            self.assert_error(lambda: sc.SeedanceClient(self.base, self.secret, upload_timeout=timeout), "invalid_input")
        source = self.root / "cancel.mp4"
        source.write_bytes(b"v" * sc.CHUNK_BYTES * 2)
        state = {"cancelled": False}
        client = sc.SeedanceClient(self.base, self.secret, cancelled=lambda: state["cancelled"])
        def consume(_method, _path, *, body, headers):
            for _chunk in body:
                state["cancelled"] = True
        self.conn.request.side_effect = consume
        self.assert_error(lambda: client.upload_video(source), "cancelled", True)
        self.conn.request.assert_called_once()
        self.conn.getresponse.assert_not_called()

    def test_file_status_retains_id_without_inventing_asset_reference(self):
        self.response({"id": "file-source-1", "status": "processing", "filename": self.secret})
        self.assertEqual(self.client.get_file("file-source-1"), {"file_id": "file-source-1", "status": "processing"})
        self.assertEqual(self.conn.request.call_args.args, ("GET", "/api/v3/files/file-source-1"))

    def test_task_only_returns_needed_fields_not_raw_server_text(self):
        signed = "https://ark-output.tos-cn-beijing.volces.com/result.mp4?signature=temporary"
        self.response({"id": "cgt-123", "status": "succeeded", "content": {"video_url": signed},
                       "request": self.secret, "usage": {"total_tokens": 123, "private": self.secret},
                       "duration": 20.1})
        result = self.client.get_task("cgt-123")
        self.assertEqual(result["video_url"], signed)
        self.assertEqual(result["usage"], {"total_tokens": 123})
        self.assertEqual(result["duration"], 20.1)
        self.assertNotIn(self.secret, json.dumps(result))

    def test_failed_task_drops_server_message_and_sanitizes_code(self):
        self.response({"id": "cgt-123", "status": "failed", "error": {"message": self.secret,
                       "code": "InvalidParameter.TaskTypeMismatch"}})
        self.assertEqual(self.client.get_task("cgt-123"), {"id": "cgt-123", "status": "failed",
                         "error_code": "InvalidParameter.TaskTypeMismatch"})
        self.response({"id": "cgt-123", "status": "failed", "error": {"code": self.secret}})
        self.assertIsNone(self.client.get_task("cgt-123")["error_code"])

    def test_http_rejection_never_retries_or_echoes_body(self):
        for status in (401, 403, 429, 500, 408):
            with self.subTest(status=status):
                self.conn.reset_mock()
                self.response(raw=self.secret.encode(), status=status)
                error = self.assert_error(lambda: self.client.create_edit("asset://asset-123", "edit"),
                                          "authentication" if status == 401 else
                                          "permission_denied" if status == 403 else "http_error",
                                          status in (500, 408))
                self.assertEqual(error.http_status, status)
                self.conn.request.assert_called_once()

    def test_redirect_does_not_send_key_or_body_anywhere_else(self):
        self.response(raw=self.secret.encode(), status=307,
                      headers={"Location": "https://attacker.invalid/"})
        self.assert_error(lambda: self.client.get_file("file-123"), "redirect_blocked")
        self.factory.assert_called_once()
        self.conn.request.assert_called_once()

    def test_http_failure_only_exposes_safe_provider_code(self):
        self.response({"error": {"code": "InvalidParameter.TaskTypeConstraint", "message": self.secret}}, status=400)
        error = self.assert_error(lambda: self.client.create_edit("asset://asset-123", "edit"), "http_error")
        self.assertEqual(error.provider_code, "InvalidParameter.TaskTypeConstraint")
        self.response({"error": {"code": self.secret, "message": self.secret}}, status=400)
        error = self.assert_error(lambda: self.client.create_edit("asset://asset-123", "edit"), "http_error")
        self.assertIsNone(error.provider_code)

    def test_post_connection_failure_is_certain_but_send_failure_is_uncertain(self):
        self.conn.connect.side_effect = OSError(self.secret)
        error = self.assert_error(lambda: self.client.create_edit("asset://asset-123", "edit"), "network")
        self.assertEqual((error.failure_stage, error.exception_type, error.completed_body_bytes),
                         ("connect", "OSError", 0))
        self.conn.request.assert_not_called()
        self.conn.connect.side_effect = None
        self.conn.request.side_effect = OSError(self.secret)
        self.conn.getresponse.side_effect = TimeoutError(self.secret)
        error = self.assert_error(lambda: self.client.create_edit("asset://asset-123", "edit"), "network", True)
        self.assertEqual((error.failure_stage, error.exception_type, error.completed_body_bytes),
                         ("send", "OSError", 0))
        self.conn.request.assert_called_once()
        self.conn.getresponse.assert_called_once()

    def test_real_http_body_write_failure_reads_early_response_without_resending(self):
        # Run the actual stdlib HTTP framing/send loop over an in-memory socket.
        # No DNS, network, TLS or real credentials are involved.
        source = self.root / "synthetic.mp4"
        source.write_bytes(b"synthetic video" * 100)
        for status, uncertain, code in ((413, False, "http_error"), (401, False, "authentication"),
                                        (408, True, "http_error"), (500, True, "http_error"),
                                        (307, True, "redirect_blocked"), (499, True, "http_error"),
                                        (200, True, "invalid_response")):
            for fail_at in (2, 3):  # First body block, or after the multipart prefix.
                with self.subTest(status=status, fail_at=fail_at):
                    payload = json.dumps({"id": "file-early", "status": "active", "error": {
                        "code": "InvalidParameter.Upload", "message": self.secret}}).encode()
                    reply = (f"HTTP/1.1 {status} Synthetic\r\nContent-Length: {len(payload)}\r\n"
                             "X-Request-Id: request-test-123\r\n\r\n").encode() + payload
                    secret = self.secret
                    class WireSocket:
                        def __init__(sock):
                            sock.writes, sock.timeouts, sock.attempts = [], [], 0
                        def sendall(sock, data):
                            sock.attempts += 1
                            if sock.attempts == fail_at:
                                raise BrokenPipeError(secret)
                            sock.writes.append(data)
                        def makefile(sock, _mode):
                            return io.BytesIO(reply)
                        def settimeout(sock, value):
                            sock.timeouts.append(value)
                        def close(sock):
                            pass
                    wire = WireSocket()
                    connection = sc.http.client.HTTPConnection("ark.cn-beijing.volces.com")
                    connection.connect = Mock(side_effect=lambda: setattr(connection, "sock", wire))
                    self.factory.reset_mock()
                    self.factory.return_value = connection
                    error = self.assert_error(lambda: self.client.upload_video(source), code, uncertain)
                    self.assertEqual(error.http_status, status)
                    self.assertEqual(error.failure_stage, "send")
                    self.assertEqual(error.exception_type, "BrokenPipeError")
                    self.assertEqual(error.completed_body_bytes, sum(map(len, wire.writes[1:])))
                    self.assertEqual(error.request_id, "request-test-123")
                    self.assertEqual(error.provider_code, None if status == 200 else "InvalidParameter.Upload")
                    self.assertEqual(len(wire.writes), fail_at - 1)
                    self.assertEqual(wire.writes[0].count(b"POST /api/v3/files HTTP/1.1"), 1)
                    self.assertLessEqual(wire.timeouts[-1], sc._EARLY_RESPONSE_TIMEOUT)
                    connection.connect.assert_called_once()
                    self.factory.assert_called_once()

    def test_recovery_failure_preserves_send_diagnostic_and_is_bounded(self):
        source = self.root / "synthetic.mp4"
        source.write_bytes(b"video")
        sent = []
        def consume(_method, _path, *, body, headers):
            chunks = iter(body)
            sent.append(next(chunks))
            next(chunks)  # This failed block may be partly transmitted; do not count it.
            raise BrokenPipeError(self.secret)
        self.conn.request.side_effect = consume
        self.conn.sock = Mock()
        for failure in (TimeoutError(self.secret), sc.http.client.BadStatusLine(self.secret)):
            with self.subTest(failure=type(failure).__name__):
                self.conn.reset_mock()
                self.conn.getresponse.side_effect = failure
                error = self.assert_error(lambda: self.client.upload_video(source), "network", True)
                self.assertEqual((error.failure_stage, error.exception_type), ("send", "BrokenPipeError"))
                self.assertEqual(error.completed_body_bytes, len(sent[-1]))
                self.assertIsNone(error.request_id)
                self.assertLessEqual(self.conn.sock.settimeout.call_args.args[0], sc._EARLY_RESPONSE_TIMEOUT)
                self.conn.request.assert_called_once()
                self.conn.getresponse.assert_called_once()

    def test_local_body_read_failure_does_not_probe_for_early_response(self):
        def chunks():
            yield b"prefix"
            raise OSError(self.secret)
        self.conn.request.side_effect = lambda _method, _path, *, body, headers: b"".join(body)
        error = self.assert_error(lambda: self.client._request("POST", "/files", body=chunks(),
                                  content_type="multipart/form-data", content_length=100), "file_io", True)
        self.assertEqual((error.failure_stage, error.exception_type, error.completed_body_bytes),
                         ("body_read", "OSError", len(b"prefix")))
        self.conn.getresponse.assert_not_called()

    def test_file_open_failure_is_certain_but_close_after_post_is_uncertain(self):
        source = self.root / "synthetic.mp4"
        source.write_bytes(b"video")
        with patch.object(Path, "open", side_effect=PermissionError(self.secret)):
            error = self.assert_error(lambda: self.client.upload_video(source), "file_io")
        self.assertEqual((error.failure_stage, error.completed_body_bytes), ("body_read", 0))
        self.factory.assert_not_called()

        stream = source.open("rb")
        secret = self.secret
        class FailingClose:
            def __enter__(self):
                return stream
            def __exit__(self, *_args):
                stream.close()
                raise OSError(secret)
        sent = []
        self.conn.request.side_effect = lambda _method, _path, *, body, headers: sent.append(b"".join(body))
        self.response({"id": "file-created", "status": "processing"},
                      headers={"X-Request-Id": "request-created-123"})
        with patch.object(Path, "open", return_value=FailingClose()):
            error = self.assert_error(lambda: self.client.upload_video(source), "file_io", True)
        self.assertEqual((error.failure_stage, error.exception_type), ("body_read", "OSError"))
        self.assertEqual(error.completed_body_bytes, len(sent[0]))
        self.assertGreater(error.completed_body_bytes, source.stat().st_size)
        self.assertEqual(error.request_id, "request-created-123")
        self.conn.request.assert_called_once()
        self.conn.getresponse.assert_called_once()

    def test_local_body_validation_and_request_construction_do_not_probe(self):
        def chunks():
            yield b"prefix"
            raise sc.SeedanceError("invalid_input", "source changed")
        self.conn.request.side_effect = lambda _method, _path, *, body, headers: b"".join(body)
        error = self.assert_error(lambda: self.client._request("POST", "/files", body=chunks(),
                                  content_type="multipart/form-data", content_length=100), "invalid_input", True)
        self.assertEqual(error.failure_stage, "body_read")
        self.conn.getresponse.assert_not_called()
        self.conn.request.side_effect = sc.http.client.CannotSendRequest(self.secret)
        self.assert_error(lambda: self.client.create_edit("asset://asset-video", "edit"), "network", True)
        self.conn.getresponse.assert_not_called()

    def test_send_failure_does_not_probe_after_cancellation_or_deadline(self):
        for cancelled in (True, False):
            with self.subTest(cancelled=cancelled):
                clock = {"now": 0, "cancelled": False}
                client = sc.SeedanceClient(self.base, self.secret, cancelled=lambda: clock["cancelled"])
                def fail(*args, **kwargs):
                    clock["cancelled"] = cancelled
                    clock["now"] = client.create_timeout
                    raise BrokenPipeError(self.secret)
                self.conn.reset_mock()
                self.conn.request.side_effect = fail
                with patch.object(sc.time, "monotonic", side_effect=lambda: clock["now"]):
                    error = self.assert_error(lambda: client.create_edit("asset://asset-video", "edit"),
                                              "cancelled" if cancelled else "timeout", True)
                self.assertEqual((error.failure_stage, error.exception_type), ("send", "BrokenPipeError"))
                self.conn.getresponse.assert_not_called()

    def test_full_upload_diagnostics_distinguish_response_failures(self):
        source = self.root / "synthetic.mp4"
        source.write_bytes(b"video")
        lengths = []
        def consume(_method, _path, *, body, headers):
            data = b"".join(body)
            lengths.append(len(data))
        self.conn.request.side_effect = consume
        failing_body = Response(raw=b"", headers={"X-Request-Id": "request-body-123"})
        failing_body.read1 = Mock(side_effect=TimeoutError(self.secret))
        cases = [(None, TimeoutError(self.secret), "network", "response_headers", "TimeoutError", None),
                 (failing_body, None, "network", "response_body", "TimeoutError", "request-body-123"),
                 (Response(raw=b"null", headers={"X-Request-Id": "request-null-123"}), None,
                  "invalid_response", "response_decode", "ValueError", "request-null-123"),
                 (Response({}, headers={"X-Request-Id": "request-missing-123"}), None,
                  "invalid_response", "response_validation", "SeedanceError", "request-missing-123")]
        for response, failure, code, stage, kind, request_id in cases:
            with self.subTest(stage=stage):
                self.conn.reset_mock()
                self.conn.getresponse.side_effect = failure
                self.conn.getresponse.return_value = response
                error = self.assert_error(lambda: self.client.upload_video(source), code, True)
                self.assertEqual((error.failure_stage, error.exception_type), (stage, kind))
                self.assertEqual(error.completed_body_bytes, lengths[-1])
                self.assertEqual(error.request_id, request_id)
                self.conn.request.assert_called_once()
                self.conn.getresponse.assert_called_once()

    def test_diagnostics_drop_unsafe_request_ids_and_dynamic_exception_names(self):
        for request_id in (self.secret, "prefix-" + self.secret, "https://cdn.example.com/?token=hidden",
                           "injected\r\nAuthorization", "x" * 129, "contains spaces"):
            with self.subTest(request_id=request_id):
                self.response({"error": {"code": "InvalidParameter.Upload", "message": self.secret}},
                              status=400, headers={"X-Request-Id": request_id, "Location": request_id})
                error = self.assert_error(lambda: self.client.create_edit("asset://asset-video", "edit"), "http_error")
                self.assertIsNone(error.request_id)
                self.assertNotIn(request_id, json.dumps(vars(error)))
        self.response(raw=b"", status=400, headers={"X-Tt-Logid": "20261126-TEST-logid"})
        error = self.assert_error(lambda: self.client.create_edit("asset://asset-video", "edit"), "http_error")
        self.assertEqual(error.request_id, "20261126-TEST-logid")
        self.conn.connect.side_effect = type(self.secret, (OSError,), {})(self.secret)
        error = self.assert_error(lambda: self.client.create_edit("asset://asset-video", "edit"), "network")
        self.assertEqual(error.exception_type, "OSError")

    def test_invalid_task_id_retains_successful_request_diagnostics(self):
        self.response({"id": "not-a-task"}, headers={"X-Request-Id": "request-task-id-123"})
        error = self.assert_error(lambda: self.client.create_edit("asset://asset-video", "edit"),
                                  "invalid_response", True)
        self.assertEqual((error.failure_stage, error.exception_type), ("response_validation", "SeedanceError"))
        self.assertEqual(error.completed_body_bytes, len(self.conn.request.call_args.kwargs["body"]))
        self.assertEqual(error.request_id, "request-task-id-123")
        self.conn.request.assert_called_once()

    def test_error_constructor_remains_compatible_and_bounds_optional_diagnostics(self):
        error = sc.SeedanceError("network", "safe message", submission_uncertain=True)
        self.assertEqual(str(error), "safe message")
        self.assertTrue(error.submission_uncertain)
        self.assertEqual((error.failure_stage, error.exception_type, error.completed_body_bytes, error.request_id),
                         (None, None, None, None))
        error = sc.SeedanceError("network", "safe message", failure_stage="untrusted-stage",
                                 exception_type=self.secret, completed_body_bytes=-1, request_id="https://unsafe/")
        self.assertEqual((error.failure_stage, error.exception_type, error.completed_body_bytes, error.request_id),
                         (None, None, None, None))

    def test_bad_post_response_is_uncertain_and_no_second_post_occurs(self):
        for payload in (b"{}", b"[]", b"<html>oops</html>", b"x" * (sc.MAX_RESPONSE_BYTES + 1)):
            with self.subTest(size=len(payload)):
                self.conn.reset_mock()
                self.response(raw=payload)
                self.assert_error(lambda: self.client.create_edit("asset://asset-123", "edit"), "invalid_response", True)
                self.conn.request.assert_called_once()

    def test_task_and_file_identity_are_validated(self):
        for payload in ({"id": "cgt-other", "status": "running"},
                        {"id": "cgt-123", "status": "unknown"},
                        {"id": "cgt-123", "status": "succeeded", "content": {}},
                        {"id": "cgt-123", "status": "succeeded", "content": {"video_url": "file:///private"}}):
            self.response(payload)
            self.assert_error(lambda: self.client.get_task("cgt-123"), "invalid_response")
        self.response({"id": "file-other", "status": "active"})
        self.assert_error(lambda: self.client.get_file("file-123"), "invalid_response")

    def test_invalid_inputs_and_preexisting_cancellation_do_not_connect(self):
        for reference in ("file-123", "file://file-123", "http://example.com/v.mp4", "https://localhost/v.mp4",
                          "https://10.0.0.1/v.mp4", "https://user:pass@example.com/v.mp4"):
            self.assert_error(lambda: self.client.create_edit(reference, "edit"), "invalid_input")
        self.assert_error(lambda: self.client.get_task("cgt-123/../../files"), "invalid_input")
        cancelled = sc.SeedanceClient(self.base, self.secret, cancelled=lambda: True)
        self.assert_error(lambda: cancelled.create_edit("asset://asset-123", "edit"), "cancelled")
        self.factory.assert_not_called()

    def test_cancellation_after_send_is_uncertain(self):
        state = {"cancelled": False}
        client = sc.SeedanceClient(self.base, self.secret, cancelled=lambda: state["cancelled"])
        self.conn.request.side_effect = lambda *a, **kw: state.update(cancelled=True)
        self.assert_error(lambda: client.create_edit("asset://asset-123", "edit"), "cancelled", True)

    def test_download_has_no_credentials_and_returns_hash(self):
        content = b"\x00\x00\x00\x18ftypmp42" + b"video bytes" * 20
        conn = Mock(sock=None)
        conn.getresponse.return_value = Response(raw=content, headers={"Content-Length": str(len(content))})
        publisher = "rename" if sc.os.name == "nt" else "link"
        unused = "link" if sc.os.name == "nt" else "rename"
        with patch.object(sc, "_public_addresses", return_value=["8.8.8.8"]), \
             patch.object(sc, "_PinnedHTTPSConnection", return_value=conn) as pinned, \
             patch.object(sc.os, publisher, wraps=getattr(sc.os, publisher)) as publish, \
             patch.object(sc.os, unused, side_effect=AssertionError("Wrong platform publication method")):
            target = self.root / "output.mp4"
            result = self.client.download("https://cdn.example.com/video.mp4?signature=temp", target)
        publish.assert_called_once()
        self.assertEqual(publish.call_args.args[1], target)
        self.assertEqual(target.read_bytes(), content)
        self.assertEqual(target.stat().st_nlink, 1)
        self.assertEqual(result["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(result["bytes"], len(content))
        self.assertNotIn("Authorization", conn.request.call_args.kwargs["headers"])
        self.assertNotIn(self.secret, str(conn.request.call_args))
        self.assertNotIn("signature", json.dumps(result))
        pinned.assert_called_once_with("cdn.example.com", "8.8.8.8", timeout=unittest.mock.ANY)
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_download_rejects_redirects_oversize_partial_and_cleans_temporary(self):
        for response, code in ((Response(raw=b"", status=302, headers={"Location": "https://127.0.0.1/"}), "redirect_blocked"),
                               (Response(raw=b"1234", headers={"Content-Length": "5"}), "invalid_response"),
                               (Response(raw=b"12345"), "invalid_response"),
                               (Response(raw=b"", headers={"Content-Length": "999"}), "invalid_response")):
            conn = Mock(sock=None)
            conn.getresponse.return_value = response
            with patch.object(sc, "_public_addresses", return_value=["8.8.8.8"]), \
                 patch.object(sc, "_PinnedHTTPSConnection", return_value=conn):
                self.assert_error(lambda: self.client.download("https://cdn.example.com/video?secret=hidden",
                                  self.root / "output.mp4", max_bytes=4), code)
            self.assertFalse((self.root / "output.mp4").exists())
            self.assertEqual(list(self.root.glob(".seedance-*.part")), [])
            conn.request.assert_called_once()

    def test_existing_download_file_is_not_overwritten(self):
        target = self.root / "output.mp4"
        target.write_bytes(b"user data")
        with patch.object(sc, "_public_addresses", return_value=["8.8.8.8"]), \
             patch.object(sc, "_PinnedHTTPSConnection") as pinned:
            self.assert_error(lambda: self.client.download("https://cdn.example.com/video", target), "file_exists")
            pinned.assert_not_called()
        self.assertEqual(target.read_bytes(), b"user data")

    def test_destination_created_during_download_is_preserved_and_part_is_removed(self):
        target = self.root / "output.mp4"
        content = b"\x00\x00\x00\x18ftypmp42" + b"downloaded video"
        appeared = {"created": False}
        class RacingResponse(Response):
            def read1(response, amount):
                if not appeared["created"]:
                    # Simulate another process creating the destination after
                    # download's initial exists() check, before final publish.
                    target.write_bytes(b"concurrent user file")
                    appeared["created"] = True
                return super().read1(amount)
        conn = Mock(sock=None)
        conn.getresponse.return_value = RacingResponse(raw=content)
        with patch.object(sc, "_public_addresses", return_value=["8.8.8.8"]), \
             patch.object(sc, "_PinnedHTTPSConnection", return_value=conn):
            self.assert_error(lambda: self.client.download("https://cdn.example.com/video", target), "file_exists")
        self.assertTrue(appeared["created"])
        self.assertEqual(target.read_bytes(), b"concurrent user file")
        self.assertEqual(target.stat().st_nlink, 1)
        self.assertEqual(list(self.root.glob(".seedance-*.part")), [])
        conn.request.assert_called_once()

    def test_public_dns_rejects_any_nonpublic_answer(self):
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fe80::1", "224.0.0.1"):
            entries = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                       (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
            with patch.object(sc.socket, "getaddrinfo", return_value=entries):
                self.assert_error(lambda: sc._public_addresses("cdn.example.com"), "unsafe_download")

    def test_pinned_connection_uses_checked_ip_and_original_hostname_sni(self):
        self.connection_patch.stop()
        context, raw, wrapped = Mock(), Mock(), Mock()
        context.wrap_socket.return_value = wrapped
        with patch.object(sc.ssl, "create_default_context", return_value=context), \
             patch.object(sc.socket, "create_connection", return_value=raw) as connect:
            connection = sc._PinnedHTTPSConnection("cdn.example.com", "8.8.8.8", timeout=5)
            connection.connect()
        connect.assert_called_once_with(("8.8.8.8", 443), 5)
        context.wrap_socket.assert_called_once_with(raw, server_hostname="cdn.example.com")
        self.assertIs(connection.sock, wrapped)


if __name__ == "__main__":
    unittest.main()
