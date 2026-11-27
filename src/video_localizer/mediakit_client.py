"""Bounded native AI MediaKit OCR/erase API client, with no automatic POST retry.

Persist the exact request parameters and client_token before submitting. MediaKit
documents a 24-hour idempotency window; a new signed URL is a changed request.
Returned video_url values are temporary credentials and must not be logged.

Protocol checked 2026-11-27:
https://docs.volcengine.com/docs/intelligent-processing/erase-video-subtitle-pro
https://docs.volcengine.com/docs/intelligent-processing/submit-video-recognition-subtitle-ocrtask-api
https://docs.volcengine.com/docs/intelligent-processing/query-task
https://docs.volcengine.com/docs/intelligent-processing/fundamentals
"""

import http.client
import json
import math
import re
import time

from .seedance_client import (SeedanceError, _PinnedHTTPSConnection,
                              _exception_name, _public_addresses, _url)


DEFAULT_BASE_URL = "https://mediakit.cn-beijing.volces.com/api/v1"
MAX_RESPONSE_BYTES = 4 * 1024**2
MAX_REQUEST_BYTES = 256 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\Z")
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z")
_STATES = {"running", "completed", "failed"}
_COORDINATES = ("top_left_x", "top_left_y", "bottom_right_x", "bottom_right_y")


def _number(value, *, maximum=None):
    try:
        return (type(value) in (int, float) and math.isfinite(value) and value >= 0
                and (maximum is None or value <= maximum))
    except OverflowError:
        return False


class MediaKitClient:
    def __init__(self, api_key, base_url=DEFAULT_BASE_URL, *, timeout=60,
                 create_timeout=180, cancelled=None):
        # This provider-specific client intentionally cannot send its credentials
        # to an arbitrary configured endpoint or follow a redirect there.
        if not isinstance(base_url, str) or base_url.rstrip("/") != DEFAULT_BASE_URL:
            raise SeedanceError("configuration", "MediaKit 须使用受支持的官方 HTTPS API 地址。")
        if (not isinstance(api_key, str) or not 1 <= len(api_key) <= 2560
                or any(ord(char) < 33 or ord(char) > 126 for char in api_key)):
            raise SeedanceError("configuration", "MediaKit API 密钥无效。")
        if any(not _number(value, maximum=1200) or value < 1
               for value in (timeout, create_timeout)):
            raise SeedanceError("invalid_input", "MediaKit 请求超时须为 1–1200 秒。")
        if cancelled is not None and not callable(cancelled):
            raise SeedanceError("invalid_input", "取消检查器须可调用。")
        self.base_url = DEFAULT_BASE_URL
        self.timeout = timeout
        self.create_timeout = create_timeout
        self.cancelled = cancelled
        self._key = api_key

    def _check(self, deadline):
        if self.cancelled and self.cancelled():
            raise SeedanceError("cancelled", "本地等待已取消；已提交的 MediaKit 任务可能继续运行。")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SeedanceError("timeout", "MediaKit 请求已超时。")
        return remaining

    def _tick(self, connection, deadline):
        connection.timeout = self._check(deadline)
        if connection.sock is not None:
            connection.sock.settimeout(connection.timeout)

    def _safe_id(self, value):
        return (value if isinstance(value, str) and _ID.fullmatch(value)
                and self._key not in value else None)

    def _provider_code(self, value):
        return (value if isinstance(value, str) and _CODE.fullmatch(value)
                and self._key not in value else None)

    def _failure_code(self, value):
        if not isinstance(value, dict):
            return None
        error = value.get("error")
        return self._provider_code(error.get("code") if isinstance(error, dict) else value.get("code"))

    def _task_id(self, value, *, code="invalid_input"):
        if not self._safe_id(value) or not value.startswith("amk-"):
            raise SeedanceError(code, "MediaKit 任务 ID 无效。")
        return value

    def _token(self, value):
        if (not isinstance(value, str) or not 1 <= len(value) <= 64
                or any(ord(char) < 32 or ord(char) > 126 for char in value)
                or self._key in value):
            raise SeedanceError("invalid_input", "MediaKit client_token 须为不含凭据的 1–64 位可打印 ASCII 字符。")
        return value

    def _video_url(self, value, *, code="invalid_input"):
        _url(value, code=code)
        if self._key in value:
            raise SeedanceError(code, "MediaKit 媒体地址不能包含 API 凭据。")
        return value

    def _request(self, method, suffix, payload=None):
        modifying = method == "POST"
        deadline = time.monotonic() + (self.create_timeout if modifying else self.timeout)
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            if len(body) > MAX_REQUEST_BYTES:
                raise SeedanceError("invalid_input", "MediaKit 请求内容过大。")
        connection = None
        started = False
        stage = "connect"
        completed = 0
        request_id = None
        try:
            self._check(deadline)
            host = "mediakit.cn-beijing.volces.com"
            addresses = _public_addresses(host)
            connection = _PinnedHTTPSConnection(host, addresses[0], timeout=min(self._check(deadline), 30))
            connection.connect()
            self._tick(connection, deadline)
            headers = {"Authorization": "Bearer " + self._key,
                       "Accept": "application/json", "Accept-Encoding": "identity"}
            if body is not None:
                headers.update({"Content-Type": "application/json", "Content-Length": str(len(body))})
            stage = "send"
            started = True
            connection.request(method, "/api/v1" + suffix, body=body, headers=headers)
            completed = len(body) if body else 0
            stage = "response_headers"
            self._tick(connection, deadline)
            response = connection.getresponse()
            for header in ("X-Request-Id", "X-Tt-Logid"):
                request_id = self._safe_id(response.getheader(header))
                if request_id:
                    break
            stage = "response_body"
            data = bytearray()
            while True:
                self._tick(connection, deadline)
                block = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - len(data)))
                if not block:
                    break
                data.extend(block)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise SeedanceError("invalid_response", "MediaKit 响应内容过大。")
            stage = "response_decode"
            try:
                value = json.loads(data)
                if not isinstance(value, dict):
                    raise ValueError()
            except (ValueError, UnicodeError, RecursionError):
                value = None
            if value is not None:
                request_id = self._safe_id(value.get("request_id")) or request_id
            if not 200 <= response.status < 300:
                code = ("authentication" if response.status == 401 else "permission_denied" if response.status == 403
                        else "redirect_blocked" if 300 <= response.status < 400 else "http_error")
                raise SeedanceError(code, f"MediaKit 接口返回 HTTP {response.status}。",
                    http_status=response.status, provider_code=self._failure_code(value),
                    submission_uncertain=modifying and (not 400 <= response.status < 500 or response.status == 408))
            stage = "response_validation"
            if value is None or type(value.get("success")) is not bool:
                raise SeedanceError("invalid_response", "MediaKit 响应缺少有效的 success 状态。")
            if value["success"] is False:
                if value.get("task_id"):
                    raise SeedanceError("invalid_response", "MediaKit 拒绝状态与任务 ID 不一致，需核实提交结果。")
                # Explicit business rejection is definite even with HTTP 200.
                raise SeedanceError("provider_error", "MediaKit 拒绝了本次请求。",
                    http_status=response.status, provider_code=self._failure_code(value))
            return value, request_id
        except SeedanceError as error:
            if modifying and started and error.http_status is None:
                error.submission_uncertain = True
            error.failure_stage = stage
            error.completed_body_bytes = completed
            error.request_id = request_id
            error.exception_type = error.exception_type or "SeedanceError"
            raise
        except (OSError, http.client.HTTPException, ValueError) as cause:
            raise SeedanceError("network", "MediaKit 网络连接失败或超时。",
                submission_uncertain=modifying and started, failure_stage=stage,
                completed_body_bytes=completed, request_id=request_id,
                exception_type=_exception_name(cause)) from None
        finally:
            if connection is not None:
                connection.close()

    def _submit(self, tool, payload):
        value, request_id = self._request("POST", "/tools/" + tool, payload)
        try:
            result = {"success": True, "task_id": self._task_id(value.get("task_id"), code="invalid_response")}
        except SeedanceError as error:
            error.submission_uncertain = True
            error.failure_stage = "response_validation"
            error.request_id = request_id
            raise
        if request_id:
            result["request_id"] = request_id
        return result

    def submit_video_ocr(self, video_url, client_token, *, mode="Detailed"):
        if mode not in ("Subtitle", "Detailed"):
            raise SeedanceError("invalid_input", "MediaKit OCR 模式须为 Subtitle 或 Detailed。")
        return self._submit("video-ocr", {"video_url": self._video_url(video_url), "mode": mode,
                                          "client_token": self._token(client_token)})

    def _boxes(self, boxes, *, allow_empty=False):
        if (not isinstance(boxes, (list, tuple)) or len(boxes) > 20
                or not boxes and not allow_empty):
            raise SeedanceError("invalid_input", "MediaKit 擦除框须包含 1–20 个归一化矩形。")
        result = []
        for box in boxes:
            if (not isinstance(box, dict) or set(box) != set(_COORDINATES)
                    or any(not _number(box[name], maximum=1) for name in _COORDINATES)
                    or box["top_left_x"] >= box["bottom_right_x"]
                    or box["top_left_y"] >= box["bottom_right_y"]):
                raise SeedanceError("invalid_input", "MediaKit 擦除框坐标无效。")
            result.append({name: box[name] for name in _COORDINATES})
        return result

    def _segments(self, value, *, global_boxes):
        if isinstance(value, (list, tuple)):
            value = {"mode": "selected", "segments": list(value)}
        if (not isinstance(value, dict) or set(value) != {"mode", "segments"}
                or value["mode"] not in ("selected", "skip")
                or not isinstance(value["segments"], (list, tuple))
                or not 1 <= len(value["segments"]) <= 1000):
            raise SeedanceError("invalid_input", "MediaKit 擦除时间段无效。")
        segments = []
        for item in value["segments"]:
            if (not isinstance(item, dict) or not {"start_time", "end_time"} <= set(item)
                    or set(item) - {"start_time", "end_time", "erase_ratio_location"}
                    or not _number(item["start_time"]) or not _number(item["end_time"])
                    or item["start_time"] >= item["end_time"]):
                raise SeedanceError("invalid_input", "MediaKit 擦除时间段必须具有有效的起止秒数。")
            segment = {name: item[name] for name in ("start_time", "end_time")}
            if "erase_ratio_location" in item:
                if global_boxes or value["mode"] != "selected":
                    raise SeedanceError("invalid_input", "分段擦除框不能与全局擦除框或跳过模式同时使用。")
                segment["erase_ratio_location"] = self._boxes(item["erase_ratio_location"], allow_empty=True)
            segments.append(segment)
        return {"mode": value["mode"], "segments": segments}

    def submit_erase(self, video_url, client_token, *, boxes=None, model_version="v5.6",
                     mode="Subtitle", time_segments=None, output_encode_mode="Quality"):
        if model_version not in ("v4", "v5", "v5.6") or mode not in ("Subtitle", "Text"):
            raise SeedanceError("invalid_input", "MediaKit 擦除算法或模式无效。")
        if output_encode_mode not in ("Quality", "Size"):
            raise SeedanceError("invalid_input", "MediaKit 编码模式须为 Quality 或 Size。")
        payload = {"video_url": self._video_url(video_url), "client_token": self._token(client_token),
                   "model_version": model_version, "mode": mode, "output_encode_mode": output_encode_mode,
                   "output_format": "mp4"}
        if boxes is not None:
            payload["erase_ratio_location"] = self._boxes(boxes)
        if time_segments is not None:
            payload["time_segment_filter"] = self._segments(time_segments, global_boxes=boxes is not None)
        return self._submit("erase-video-subtitle-pro", payload)

    def _result(self, value):
        if not isinstance(value, dict) or not _number(value.get("duration")):
            raise SeedanceError("invalid_response", "MediaKit 完成结果缺少有效时长。")
        result = {"duration": value["duration"]}
        if "video_url" in value:
            result["video_url"] = self._video_url(value["video_url"], code="invalid_response")
        if "subtitles" in value:
            if not isinstance(value["subtitles"], list) or len(value["subtitles"]) > 20000:
                raise SeedanceError("invalid_response", "MediaKit OCR 字幕列表无效。")
            subtitles = []
            for item in value["subtitles"]:
                if (not isinstance(item, dict) or not isinstance(item.get("subtitle_text"), str)
                        or len(item["subtitle_text"]) > 10000 or self._key in item["subtitle_text"]
                        or not _number(item.get("start_time")) or not _number(item.get("end_time"))
                        or item["start_time"] > item["end_time"]):
                    raise SeedanceError("invalid_response", "MediaKit OCR 字幕内容或时间段无效。")
                subtitle = {name: item[name] for name in ("subtitle_text", "start_time", "end_time")}
                if "text_label" in item:
                    if item["text_label"] not in ("Subtitle", "Others"):
                        raise SeedanceError("invalid_response", "MediaKit OCR 文字类别无效。")
                    subtitle["text_label"] = item["text_label"]
                if "text_location" in item:
                    location = item["text_location"]
                    if (not isinstance(location, dict) or any(type(location.get(name)) is not int
                            or location[name] < 0 for name in _COORDINATES)
                            or location["top_left_x"] >= location["bottom_right_x"]
                            or location["top_left_y"] >= location["bottom_right_y"]):
                        raise SeedanceError("invalid_response", "MediaKit OCR 文字坐标无效。")
                    subtitle["text_location"] = {name: location[name] for name in _COORDINATES}
                subtitles.append(subtitle)
            result["subtitles"] = subtitles
        if not {"video_url", "subtitles"} & result.keys():
            raise SeedanceError("invalid_response", "MediaKit 完成结果缺少视频或字幕。")
        return result

    def get_task(self, task_id):
        self._task_id(task_id)
        value, request_id = self._request("GET", "/tasks/" + task_id)
        if (value.get("task_id") != task_id or not isinstance(value.get("status"), str)
                or value["status"] not in _STATES):
            raise SeedanceError("invalid_response", "MediaKit 查询结果的任务 ID 或状态无效。", request_id=request_id)
        result = {"success": True, "task_id": task_id, "status": value["status"]}
        if request_id:
            result["request_id"] = request_id
        if self._safe_id(value.get("task_type")):
            result["task_type"] = value["task_type"]
        for name in ("created_at", "finished_at", "expires_at"):
            if type(value.get(name)) is int and value[name] >= 0:
                result[name] = value[name]
        if value["status"] == "completed":
            try:
                result["result"] = self._result(value.get("result"))
            except SeedanceError as error:
                error.request_id = request_id
                error.failure_stage = "response_validation"
                raise
        elif value["status"] == "failed":
            result["error"] = {"code": self._failure_code(value)}
        return result
