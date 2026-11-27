import io
import json
import socket
import ssl
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError, URLError
from unittest.mock import Mock, patch

from video_localizer.hunyuan import HunyuanClient, HunyuanError, check_model, output_budget_exhausted

KEY = "sk-test-credential-do-not-print"
TOOL = {"type": "function", "function": {"name": "inspect_frames", "parameters": {"type": "object"}}}


class Response(io.BytesIO):
    status = 200


class Opener:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []
        self.timeouts = []

    def open(self, request, timeout):
        self.requests.append(json.loads(request.data))
        self.timeouts.append(timeout)
        value = self.answers.pop(0)
        if isinstance(value, Exception):
            raise value
        return Response(json.dumps(value).encode())


def response(content="done", calls=None, reasoning=None, *, finish="stop", usage=True):
    message = {"role": "assistant", "content": content}
    if calls is not None:
        message["tool_calls"] = calls
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    result = {"id": "test-response", "model": "hy4", "choices": [{"message": message, "finish_reason": finish}]}
    if usage:
        result["usage"] = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
    return result


def call(name="inspect_frames", arguments='{"indices":[1]}', call_id="call-1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


class HunyuanTests(unittest.TestCase):
    def client(self, *answers):
        client = HunyuanClient("https://tokenhub.tencentmaas.com/v1", "hy4", KEY)
        client._http = Opener(*answers)
        return client

    def test_tool_round_trip_preserves_interleaved_reasoning(self):
        client = self.client(response(None, [call()], "private reasoning", finish="tool_calls"), response("approved"))
        messages = [{"role": "user", "content": "check the frame"}]
        answer = client.complete(messages, [TOOL])
        self.assertEqual(answer["reasoning_content"], "private reasoning")
        messages += [answer, {"role": "tool", "tool_call_id": "call-1", "content": "actual result"}]
        self.assertEqual(client.complete(messages, [TOOL])["content"], "approved")
        self.assertEqual(client._http.requests[1]["messages"][1], answer)
        self.assertEqual(client._http.requests[0]["reasoning_effort"], "low")
        self.assertNotIn("private reasoning", json.dumps(client.calls))
        self.assertNotIn(KEY, json.dumps(client.calls))
        self.assertEqual(client.usage["total_tokens"], 60)

    def test_per_call_limits_and_forced_choice_do_not_mutate_defaults(self):
        client = self.client(response(None, [call()], finish="tool_calls"), response("normal reply"))
        forced = {"type": "function", "function": {"name": "inspect_frames"}}
        client.complete([{"role": "user", "content": "record a checkpoint"}], [TOOL],
                        max_tokens=4096, timeout=77, tool_choice=forced, thinking="disabled")
        client.complete([{"role": "user", "content": "continue normally"}], [TOOL])
        first, normal = client._http.requests
        self.assertEqual(first["max_tokens"], 4096)
        self.assertEqual(first["tool_choice"], forced)
        self.assertEqual(first["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", first)
        self.assertEqual(normal["max_tokens"], 65536)
        self.assertEqual(normal["tool_choice"], "auto")
        self.assertEqual(normal["reasoning_effort"], "low")
        self.assertNotIn("thinking", normal)
        self.assertEqual(client._http.timeouts, [77, 1200])
        self.assertEqual((client.max_tokens, client.timeout, client.reasoning_effort), (65536, 1200, "low"))
        self.assertEqual(client.calls[0]["max_tokens"], 4096)
        self.assertEqual(client.calls[0]["timeout"], 77)
        self.assertEqual(client.calls[0]["thinking"], "disabled")
        self.assertIsNone(client.calls[0]["reasoning_effort"])
        self.assertEqual(client.calls[1]["max_tokens"], 65536)
        self.assertIsNone(client.calls[1]["thinking"])

    def test_invalid_overrides_and_forced_thinking_fail_before_network(self):
        client = self.client()
        bad_options = [
            {"max_tokens": 127}, {"max_tokens": 65537}, {"max_tokens": True},
            {"timeout": 4}, {"timeout": 1201}, {"timeout": float("nan")},
            {"thinking": "adaptive"}, {"thinking": []},
            {"tool_choice": "required"}, {"tool_choice": "required", "thinking": "enabled"},
            {"tool_choice": {"type": "function", "function": {"name": "inspect_frames"}}},
            {"tool_choice": {"type": "function", "function": {"name": "unknown"}}, "thinking": "disabled"},
            {"tool_choice": "anything"}, {"tool_choice": []},
        ]
        for options in bad_options:
            with self.subTest(options=options), self.assertRaises(HunyuanError) as raised:
                client.complete([{"role": "user", "content": "go"}], [TOOL], **options)
            self.assertEqual(raised.exception.code, "invalid_input")
        self.assertEqual(client.request_count, 0)
        self.assertEqual(client._http.requests, [])

    def test_full_output_budget_and_timeout_are_allowed_as_explicit_overrides(self):
        client = self.client(response("complete"))
        answer = client.complete([{"role": "user", "content": "go"}], max_tokens=65536, timeout=1200)
        self.assertEqual(answer["content"], "complete")
        self.assertEqual(client._http.requests[0]["max_tokens"], 65536)
        self.assertEqual(client._http.timeouts, [1200])
        self.assertEqual(client.calls[0]["max_tokens"], 65536)
        self.assertEqual(client.calls[0]["timeout"], 1200)
        usage = {"prompt_tokens": 10, "completion_tokens": 65536, "total_tokens": 65546}
        self.assertTrue(output_budget_exhausted(usage, 65536))
        self.assertFalse(output_budget_exhausted(usage, 65537))

    def test_none_choice_rejects_a_server_tool_call(self):
        client = self.client(response(None, [call()], finish="tool_calls"), response("checkpoint"))
        with self.assertRaises(HunyuanError):
            client.complete([{"role": "user", "content": "summary only"}], [TOOL], tool_choice="none")
        self.assertEqual(client.complete([{"role": "user", "content": "summary only"}],
                                         tool_choice="none")["content"], "checkpoint")
        self.assertEqual(client._http.requests[1]["tool_choice"], "none")
        self.assertNotIn("tools", client._http.requests[1])

    def test_required_and_named_choices_are_enforced_on_response(self):
        another = {"type": "function", "function": {"name": "other", "parameters": {"type": "object"}}}
        for choice, answer in (("required", response("no action")),
                               ({"type": "function", "function": {"name": "inspect_frames"}},
                                response(None, [call("other")], finish="tool_calls"))):
            with self.subTest(choice=choice):
                client = self.client(answer)
                with self.assertRaises(HunyuanError) as raised:
                    client.complete([{"role": "user", "content": "go"}], [TOOL, another],
                                    tool_choice=choice, thinking="disabled")
                self.assertEqual(raised.exception.code, "invalid_response")

    def test_truncated_thinking_is_counted_but_never_returned_or_automatically_retried(self):
        limited = response(None, [call()], "private truncated reasoning", finish="length")
        limited["usage"] = {"prompt_tokens": 10, "completion_tokens": 8192, "total_tokens": 8202,
                            "completion_tokens_details": {"reasoning_tokens": 8192}}
        client = self.client(limited, response("recovered"))
        with patch("video_localizer.hunyuan._assistant") as parse_action:
            with self.assertRaises(HunyuanError) as raised:
                client.complete([{"role": "user", "content": "go"}], [TOOL], max_tokens=8192)
            parse_action.assert_not_called()
        self.assertEqual(raised.exception.code, "output_limit")
        self.assertEqual(client.request_count, 1)
        self.assertEqual(len(client._http.requests), 1)
        self.assertEqual(client.calls[0]["usage"]["reasoning_tokens"], 8192)
        self.assertEqual(client.calls[0]["max_tokens"], 8192)
        self.assertNotIn("private truncated reasoning", json.dumps(client.calls))
        self.assertEqual(client.complete([{"role": "user", "content": "one bounded recovery"}],
                                         max_tokens=32768, timeout=450)["content"], "recovered")
        self.assertEqual(client.request_count, 2)
        self.assertEqual(client.usage["completion_tokens"], 8212)
        self.assertEqual(client.calls[1]["max_tokens"], 32768)

    def test_unknown_or_malformed_calls_are_not_executed(self):
        for calls in ([call("run_shell")], [call(arguments="[]")], [call(arguments="{")],
                      [call(), call()], [{"id": "x", "type": "function", "function": []}],
                      [call(name=[])]):
            with self.subTest(calls=calls):
                client = self.client(response(None, calls))
                with self.assertRaises(HunyuanError) as raised:
                    client.complete([{"role": "user", "content": "go"}], [TOOL])
                self.assertEqual(raised.exception.code, "invalid_response")
                self.assertEqual(client.calls[0]["status"], "failed")

    def test_invalid_tool_json_at_budget_is_output_limit_without_partial_actions(self):
        answer = response(None, [call(), call(arguments='{"indices":[', call_id="call-2")],
                          "private budget reasoning", finish="tool_calls")
        answer["usage"] = {"prompt_tokens": 24982, "completion_tokens": 32768, "total_tokens": 57750,
                           "completion_tokens_details": {"reasoning_tokens": 30439}}
        client = self.client(answer, response("unused recovery"))
        with self.assertRaises(HunyuanError) as raised:
            client.complete([{"role": "user", "content": "go"}], [TOOL], max_tokens=32768)
        self.assertEqual(raised.exception.code, "output_limit")
        self.assertEqual(client.request_count, 1)
        self.assertEqual(len(client._http.requests), 1)
        self.assertEqual(client.calls[0]["tools"], [])
        self.assertEqual(client.calls[0]["status"], "failed")
        self.assertEqual(client.calls[0]["error_code"], "output_limit")
        self.assertEqual(client.calls[0]["finish_reason"], "tool_calls")
        self.assertEqual(client.calls[0]["usage"]["reasoning_tokens"], 30439)
        diagnostics = str(raised.exception) + json.dumps(client.calls)
        self.assertNotIn("private budget reasoning", diagnostics)
        self.assertNotIn('{"indices":[', diagnostics)
        self.assertNotIn(KEY, diagnostics)

    def test_budget_detection_requires_coherent_non_boolean_integer_counts(self):
        valid = {"prompt_tokens": 10, "completion_tokens": 8192, "total_tokens": 8202}
        self.assertTrue(output_budget_exhausted(valid, 8192))
        self.assertTrue(output_budget_exhausted(valid, 4096))
        for budget in (None, False, True, "8192", 8192.0, -1, 0, 127, 65537):
            with self.subTest(budget=budget):
                self.assertFalse(output_budget_exhausted(valid, budget))
        for usage in (None, [], {}, {"completion_tokens": 8192},
                      {**valid, "total_tokens": 8201}, {**valid, "completion_tokens": 8191}):
            with self.subTest(usage=usage):
                self.assertFalse(output_budget_exhausted(usage, 8192))
        for field in valid:
            for value in (None, False, True, "8192", 8192.0, -1):
                with self.subTest(field=field, value=value):
                    self.assertFalse(output_budget_exhausted({**valid, field: value}, 8192))

    def test_invalid_json_without_reliable_exhaustion_remains_invalid_response(self):
        for usage in (None, {"prompt_tokens": 10, "completion_tokens": 8191, "total_tokens": 8201},
                      {"prompt_tokens": 10, "completion_tokens": "8192", "total_tokens": 8202},
                      {"prompt_tokens": 10, "completion_tokens": 8192, "total_tokens": 8192}):
            with self.subTest(usage=usage):
                answer = response(None, [call(arguments="{")], finish="tool_calls", usage=False)
                answer["usage"] = usage
                client = self.client(answer)
                with self.assertRaises(HunyuanError) as raised:
                    client.complete([{"role": "user", "content": "go"}], [TOOL], max_tokens=8192)
                self.assertEqual(raised.exception.code, "invalid_response")
                self.assertEqual(client.calls[0]["tools"], [])

    def test_exhausted_budget_does_not_reclassify_other_errors_or_valid_actions(self):
        for calls in ([call("run_shell")], [call(arguments="[]")], [call(), call()],
                      [call(arguments=123)]):
            with self.subTest(calls=calls):
                answer = response(None, calls, finish="tool_calls")
                answer["usage"] = {"prompt_tokens": 10, "completion_tokens": 8192, "total_tokens": 8202}
                client = self.client(answer)
                with self.assertRaises(HunyuanError) as raised:
                    client.complete([{"role": "user", "content": "go"}], [TOOL], max_tokens=8192)
                self.assertEqual(raised.exception.code, "invalid_response")
        answer = response(None, [call()], finish="tool_calls")
        answer["usage"] = {"prompt_tokens": 10, "completion_tokens": 8192, "total_tokens": 8202}
        client = self.client(answer)
        result = client.complete([{"role": "user", "content": "go"}], [TOOL], max_tokens=8192)
        self.assertEqual(result["tool_calls"], [call()])
        self.assertEqual(client.calls[0]["status"], "completed")

    def test_http_status_classification_never_includes_error_body(self):
        for status, code in ((401, "auth"), (403, "auth"), (402, "billing"), (429, "rate_limit"),
                             (503, "service_unavailable"), (400, "configuration")):
            with self.subTest(status=status):
                error = HTTPError("https://api.invalid", status, "error", {}, io.BytesIO(
                    json.dumps({"error": {"message": KEY}}).encode()))
                client = self.client(error)
                with self.assertRaises(HunyuanError) as raised:
                    client.complete([{"role": "user", "content": "go"}])
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn(KEY, str(raised.exception) + json.dumps(client.calls))
                self.assertEqual(client.usage["unknown_requests"], 1)
                self.assertIsNone(client.calls[0]["usage"])

    def test_network_error_classification(self):
        for reason, code in ((TimeoutError(KEY), "network_timeout"), (socket.gaierror(-2, KEY), "network_dns"),
                             (ssl.SSLError(KEY), "network_tls"), (OSError("Tunnel failed " + KEY), "network_proxy"),
                             (ConnectionRefusedError(KEY), "network_connection")):
            with self.subTest(code=code):
                client = self.client(URLError(reason))
                with self.assertRaises(HunyuanError) as raised:
                    client.complete([{"role": "user", "content": "go"}])
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.stage, "connect")
                self.assertNotIn(KEY, str(raised.exception) + json.dumps(client.calls))
                self.assertEqual(client.request_count, 1)

    def test_rejects_other_models_and_truncated_actions(self):
        for model in ("gpt-4o", "qwen", [], None):
            with self.subTest(model=model), self.assertRaises(HunyuanError):
                HunyuanClient("https://tokenhub.tencentmaas.com/v1", model, KEY)
        client = self.client(response(None, [call()], finish="length"))
        with self.assertRaises(HunyuanError) as raised:
            client.complete([{"role": "user", "content": "go"}], [TOOL])
        self.assertEqual(raised.exception.code, "output_limit")
        self.assertEqual(client.usage["known_requests"], 1)

    def test_server_cannot_switch_to_a_non_hy4_model(self):
        answer = response()
        answer["model"] = "other-model"
        client = self.client(answer)
        with self.assertRaises(HunyuanError) as raised:
            client.complete([{"role": "user", "content": "go"}])
        self.assertEqual(raised.exception.code, "model_mismatch")

    def test_missing_usage_is_unknown_not_zero(self):
        client = self.client(response(usage=False))
        client.complete([{"role": "user", "content": "go"}])
        self.assertIsNone(client.calls[0]["usage"])
        self.assertEqual(client.usage["unknown_requests"], 1)

    def test_invalid_tool_definition_does_not_send_request(self):
        client = self.client()
        for tools in ([None], [{"type": "function", "function": []}], [TOOL, TOOL]):
            with self.subTest(tools=tools), self.assertRaises(HunyuanError):
                client.complete([{"role": "user", "content": "go"}], tools)
        self.assertEqual(client.request_count, 0)

    def test_explicit_probe_runs_both_tool_rounds(self):
        client = self.client(response(None, [call("record_probe", '{"marker":"HY4_CHECK","visible_text":""}')],
                                       "interleaved"), response('{"probe":"ok"}'))
        with patch("video_localizer.hunyuan.HunyuanClient", return_value=client):
            report = check_model("https://tokenhub.tencentmaas.com/v1", "hy4", KEY)
        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["tool_round_trip"])
        self.assertEqual(len(report["calls"]), 2)
        self.assertFalse(report["image_quality_verified"])

    def test_probe_cancellation_before_first_or_second_request(self):
        for decisions, expected_calls in (([True], 0), ([False, True], 1)):
            with self.subTest(decisions=decisions):
                client = self.client(response(None, [call("record_probe", '{"marker":"HY4_CHECK","visible_text":""}')]))
                with patch("video_localizer.hunyuan.HunyuanClient", return_value=client):
                    report = check_model("https://tokenhub.tencentmaas.com/v1", "hy4", KEY,
                                         cancelled=Mock(side_effect=decisions))
                self.assertEqual(report["status"], "cancelled")
                self.assertEqual(client.request_count, expected_calls)
                self.assertEqual(len(report["calls"]), expected_calls)
                self.assertEqual(report["usage"]["known_requests"], expected_calls)

    def test_real_loopback_transport_records_connection_and_upload(self):
        payloads = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payloads.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = json.dumps(response("transport ok")).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            client = HunyuanClient(f"http://127.0.0.1:{server.server_port}/v1", "hy4", KEY, timeout=5)
            self.assertEqual(client.complete([{"role": "user", "content": "go"}])["content"], "transport ok")
            self.assertEqual(len(payloads), 1)
            self.assertEqual(client.calls[0]["http_status"], 200)
            for field in ("connect_seconds", "upload_seconds", "headers_seconds", "first_body_seconds"):
                self.assertIsInstance(client.calls[0][field], float, field)
                self.assertGreaterEqual(client.calls[0][field], 0)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
