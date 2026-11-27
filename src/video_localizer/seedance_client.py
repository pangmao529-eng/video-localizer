"""Bounded Ark Files and Seedance video-edit transport, with no POST retries.

The caller must persist each returned file/task ID before doing more work. A
``submission_uncertain`` failure must never be retried automatically: the server
may already have created the resource. URLs returned by get_file/get_task contain
temporary access credentials and must not be logged or persisted in reports.

Official protocol references (checked 2026-11-26):
https://docs.volcengine.com/docs/ark/upload-files-api
https://docs.volcengine.com/docs/ark/upload-and-mount-files
https://docs.volcengine.com/docs/ark/create-video-generation-task-api
https://docs.volcengine.com/docs/ark/get-video-generation-task-api

Files purpose=agent provides a presigned URL for the original file. That URL is
passed as reference_video; Files IDs are NOT Seedance asset IDs. Asset creation
requires separate AK/SK credentials and is deliberately not implemented here.
"""

import base64
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import tempfile
import time
from urllib.parse import urlsplit

from .config import (SEEDANCE_20_FAMILY, SEEDANCE_25_FAMILY, SEEDANCE_20_MODEL,
                     SEEDANCE_25_MODEL, SEEDANCE_DEFAULT_MODEL, seedance_family)


DEFAULT_MODEL = SEEDANCE_DEFAULT_MODEL
MAX_UPLOAD_BYTES = 200 * 1024**2
MAX_RESPONSE_BYTES = 1024**2
MAX_REQUEST_BYTES = 48 * 1024**2
MAX_DOWNLOAD_BYTES = 1024**3
CHUNK_BYTES = 256 * 1024
DEFAULT_UPLOAD_TIMEOUT = 600
DEFAULT_CREATE_TIMEOUT = 180
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$")
_STATES = {"queued", "running", "cancelled", "succeeded", "failed", "expired"}
_FAILURE_STAGES = {"connect", "send", "body_read", "response_headers", "response_body",
                   "response_decode", "response_validation"}
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CLIENT_REQUEST_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_EARLY_RESPONSE_TIMEOUT = 5
_CONNECT_TIMEOUT = 30
_DEFINITE_EARLY_REJECTIONS = {400, 401, 403, 404, 405, 411, 413, 415, 422, 429}
_EXCEPTION_CLASSES = (
    (ssl.SSLCertVerificationError, "SSLCertVerificationError"), (ssl.SSLError, "SSLError"),
    (http.client.RemoteDisconnected, "RemoteDisconnected"),
    (TimeoutError, "TimeoutError"), (BrokenPipeError, "BrokenPipeError"),
    (ConnectionResetError, "ConnectionResetError"), (ConnectionAbortedError, "ConnectionAbortedError"),
    (ConnectionRefusedError, "ConnectionRefusedError"), (socket.gaierror, "gaierror"),
    (http.client.BadStatusLine, "BadStatusLine"), (http.client.IncompleteRead, "IncompleteRead"),
    (http.client.HTTPException, "HTTPException"), (OSError, "OSError"),
    (UnicodeError, "UnicodeError"), (ValueError, "ValueError"), (RecursionError, "RecursionError"),
)
_EXCEPTION_NAMES = {name for _, name in _EXCEPTION_CLASSES} | {"SeedanceError"}


def _exception_name(error):
    # Map to known names, never a dynamic class name or an exception message.
    return next((name for cls, name in _EXCEPTION_CLASSES if isinstance(error, cls)), "SeedanceError")


class SeedanceError(RuntimeError):
    """A diagnostic safe to show to the user; never holds server text or URLs."""

    def __init__(self, code, message, *, http_status=None,
                 submission_uncertain=False, provider_code=None, failure_stage=None,
                 exception_type=None, completed_body_bytes=None, request_id=None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.submission_uncertain = submission_uncertain
        self.provider_code = provider_code
        self.retryable = False
        self.failure_stage = failure_stage if isinstance(failure_stage, str) and failure_stage in _FAILURE_STAGES else None
        self.exception_type = exception_type if isinstance(exception_type, str) and exception_type in _EXCEPTION_NAMES else None
        # A lower bound: a failing sendall may have transmitted part of its block.
        # This is HTTP body bytes (including multipart framing), not source bytes.
        self.completed_body_bytes = (completed_body_bytes if type(completed_body_bytes) is int
                                     and 0 <= completed_body_bytes < 2**63 else None)
        self.request_id = (request_id if isinstance(request_id, str) and _REQUEST_ID.fullmatch(request_id)
                           else None)


def _url(value, *, code="invalid_input"):
    try:
        if (not isinstance(value, str) or not 1 <= len(value) <= 16384
                or "\\" in value or any(ord(c) < 33 or ord(c) > 126 for c in value)):
            raise ValueError()
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or parsed.port not in (None, 443)):
            raise ValueError()
        host = parsed.hostname.lower().rstrip(".")
        if (host == "localhost" or "." not in host or host.endswith((".localhost", ".local", ".internal"))
                or "%" in host):
            raise ValueError()
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not addr.is_global or addr.is_multicast:
                raise ValueError()
        return parsed
    except (ValueError, TypeError):
        raise SeedanceError(code, "需要无用户名、无片段的公网 HTTPS 媒体地址。") from None


def _public_addresses(host):
    """Resolve once; downloads connect to these checked IPs, preventing rebinding."""
    try:
        entries = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(row[4][0] for row in entries))
        if not addresses or any(not ipaddress.ip_address(ip).is_global
                                or ipaddress.ip_address(ip).is_multicast for ip in addresses):
            raise SeedanceError("unsafe_download", "下载地址解析到非公网网络，已停止。")
        return addresses
    except (OSError, ValueError):
        raise SeedanceError("network", "媒体地址 DNS 解析失败。") from None


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, *, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self):
        # Use the original hostname for certificate verification/SNI but never
        # resolve it again between validation and connecting.
        raw = socket.create_connection((self._address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class SeedanceClient:
    def __init__(self, base_url, api_key, model=DEFAULT_MODEL, *, timeout=60,
                 create_timeout=DEFAULT_CREATE_TIMEOUT, upload_timeout=DEFAULT_UPLOAD_TIMEOUT,
                 cancelled=None, model_family=None):
        try:
            parsed = urlsplit(base_url)
            if (not isinstance(base_url, str) or parsed.scheme != "https" or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment or "\\" in base_url
                    or any(ord(c) < 33 or ord(c) > 126 for c in base_url)
                    or parsed.port == 0):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise SeedanceError("configuration", "Seedance API 地址须为完整 HTTPS 基础地址。") from None
        if (not isinstance(api_key, str) or not 1 <= len(api_key) <= 2560
                or any(ord(c) < 33 or ord(c) > 126 for c in api_key)):
            raise SeedanceError("configuration", "Seedance API 密钥无效。")
        try:
            if isinstance(model, str) and model != model.strip():
                raise ValueError()
            family = seedance_family(model, model_family)
        except ValueError:
            raise SeedanceError("configuration", "Seedance 模型或接入点须指定匹配的受支持模型系列。") from None
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 1200:
            raise SeedanceError("invalid_input", "Seedance 单次请求超时须为 1–1200 秒。")
        if (type(create_timeout) not in (int, float) or not math.isfinite(create_timeout)
                or not 1 <= create_timeout <= 1200):
            raise SeedanceError("invalid_input", "Seedance 创建任务超时须为 1–1200 秒。")
        if (type(upload_timeout) not in (int, float) or not math.isfinite(upload_timeout)
                or not 1 <= upload_timeout <= 1200):
            raise SeedanceError("invalid_input", "Seedance 上传超时须为 1–1200 秒。")
        if cancelled is not None and not callable(cancelled):
            raise SeedanceError("invalid_input", "取消检查器须可调用。")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.model_family = family
        self.timeout = timeout
        self.create_timeout = create_timeout
        self.upload_timeout = upload_timeout
        self.cancelled = cancelled
        self._endpoint = parsed
        self._key = api_key

    def _check(self, deadline):
        if self.cancelled and self.cancelled():
            raise SeedanceError("cancelled", "本地等待已取消；已提交的远程任务可能继续运行。")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SeedanceError("timeout", "Seedance 请求已超时。")
        # This is the remaining total request budget, not a polling interval.
        # A socket timeout aborts http.client's response reader; a 30-second cap
        # here silently truncated longer configured waits without a safe way to
        # continue the same POST. Only establishing a connection has its own cap.
        return remaining

    def _tick(self, connection, deadline):
        remaining = self._check(deadline)
        connection.timeout = remaining
        if connection.sock is not None:
            connection.sock.settimeout(remaining)

    def _identifier(self, value, prefix, *, code="invalid_input"):
        if (not isinstance(value, str) or not value.startswith(prefix) or not _ID.fullmatch(value)
                or self._key in value):
            raise SeedanceError(code, "Seedance 返回或传入的资源 ID 无效。")
        return value

    def _provider_code(self, value):
        if (isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{0,127}", value)
                and self._key not in value):
            return value
        return None

    def _http_failure(self, status, *, uncertain=False):
        code = ("authentication" if status == 401 else "permission_denied" if status == 403
                else "redirect_blocked" if 300 <= status < 400 else "http_error")
        return SeedanceError(code, f"Seedance 接口返回 HTTP {status}。", http_status=status,
                             submission_uncertain=uncertain)

    def _request_id(self, response):
        # Ark's request ID is useful to support; no other headers are retained.
        for header in ("X-Request-Id", "X-Tt-Logid"):
            value = response.getheader(header)
            if isinstance(value, str) and _REQUEST_ID.fullmatch(value) and self._key not in value:
                return value
        return None

    def _client_request_id(self, value):
        if value is not None and (not isinstance(value, str) or not _CLIENT_REQUEST_ID.fullmatch(value)
                                  or self._key in value):
            raise SeedanceError("invalid_input", "客户端请求标识须为不含凭据的 1–128 位 ASCII 字母、数字、点、横线或下划线。")
        return value

    def _annotate(self, error, diagnostics):
        error.failure_stage = diagnostics["failure_stage"]
        error.completed_body_bytes = diagnostics["completed_body_bytes"]
        error.request_id = diagnostics["request_id"]
        if error.exception_type is None:
            error.exception_type = diagnostics["exception_type"] or _exception_name(error)
        return error

    def _failure_code(self, response, connection, deadline):
        """Only expose a bounded machine code; server prose can echo credentials."""
        try:
            data = bytearray()
            while len(data) <= 8192:
                self._tick(connection, deadline)
                chunk = response.read1(min(4096, 8193 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > 8192:
                return None
            value = json.loads(data)
            error = value.get("error") if isinstance(value, dict) else None
            return self._provider_code(error.get("code")) if isinstance(error, dict) else None
        except (SeedanceError, OSError, http.client.HTTPException, ValueError, UnicodeError, RecursionError):
            return None

    def _request(self, method, suffix, *, body=None, content_type=None, content_length=None,
                 deadline=None, diagnostics=None, client_request_id=None):
        client_request_id = self._client_request_id(client_request_id)
        if deadline is None:
            deadline = time.monotonic() + self.timeout
        if diagnostics is None:
            diagnostics = {}
        diagnostics.update(failure_stage="connect", exception_type=None, completed_body_bytes=0,
                           request_id=None, request_started=False)
        connection = None
        sent = False
        modifying = method == "POST"
        try:
            connection = http.client.HTTPSConnection(self._endpoint.hostname, self._endpoint.port,
                                                     timeout=min(self._check(deadline), _CONNECT_TIMEOUT))
            # Connect before marking the POST uncertain: DNS/TLS failures cannot
            # have created a task. No redirects, authentication handlers or retries.
            connection.connect()
            self._tick(connection, deadline)
            headers = {"Authorization": "Bearer " + self._key, "Accept": "application/json",
                       "Accept-Encoding": "identity"}
            if client_request_id is not None:
                # A log correlation identifier, not an idempotency guarantee.
                headers["X-Client-Request-Id"] = client_request_id
            if content_type:
                headers["Content-Type"] = content_type
                headers["Content-Length"] = str(content_length)
            request_body = body
            if body is not None and not isinstance(body, bytes):
                def timed_chunks():
                    iterator = iter(body)
                    while True:
                        diagnostics["failure_stage"] = "body_read"
                        try:
                            chunk = next(iterator)
                        except StopIteration:
                            return
                        except (OSError, ValueError) as cause:
                            # A source-read failure is not a socket write failure.
                            raise SeedanceError("file_io", "无法读取本地上传视频。",
                                                 exception_type=_exception_name(cause)) from None
                        diagnostics["failure_stage"] = "send"
                        # http.client streams each yielded block with sendall.
                        # Refresh its timeout before every block, so a long
                        # upload remains within its original total deadline.
                        self._tick(connection, deadline)
                        yield chunk
                        # http.client asks for the next block only after the
                        # preceding sendall completed. Do not count a failed block.
                        diagnostics["completed_body_bytes"] += len(chunk)
                request_body = timed_chunks()
            sent = True
            diagnostics["request_started"] = True
            diagnostics["failure_stage"] = "send"
            early_response = False
            try:
                connection.request(method, self._endpoint.path.rstrip("/") + suffix,
                                   body=request_body, headers=headers)
            except OSError as cause:
                # Some servers reject without consuming the entire upload. Read
                # that response once on the SAME connection; never resend or follow.
                # Local read/cancellation/deadline exceptions must bypass this path.
                if diagnostics["failure_stage"] != "send":
                    raise
                diagnostics["exception_type"] = _exception_name(cause)
                self._check(deadline)
                recovery_deadline = min(deadline, time.monotonic() + _EARLY_RESPONSE_TIMEOUT)
                try:
                    self._tick(connection, recovery_deadline)
                    response = connection.getresponse()
                except (OSError, http.client.HTTPException, ValueError):
                    raise SeedanceError("network", "Seedance 网络连接失败或超时。",
                                         submission_uncertain=modifying) from None
                diagnostics["request_id"] = self._request_id(response)
                # A success received after an incomplete write is not proof that
                # all source bytes were accepted. Caller must resolve uncertainty.
                if 200 <= response.status < 300:
                    raise SeedanceError("invalid_response", "上传发送失败后收到成功响应，结果需核实。",
                                         http_status=response.status, submission_uncertain=modifying)
                early_response = True
                deadline = recovery_deadline
            else:
                if isinstance(body, bytes):
                    diagnostics["completed_body_bytes"] = len(body)
                diagnostics["failure_stage"] = "response_headers"
                self._tick(connection, deadline)
                response = connection.getresponse()
                diagnostics["request_id"] = self._request_id(response)
            if not 200 <= response.status < 300:
                # 5xx and request timeout can occur after the resource committed.
                error = self._http_failure(response.status, uncertain=modifying and
                                          (response.status not in _DEFINITE_EARLY_REJECTIONS if early_response else
                                           not 400 <= response.status < 500 or response.status == 408))
                error.provider_code = self._failure_code(response, connection, deadline)
                raise error
            diagnostics["failure_stage"] = "response_body"
            chunks, count = [], 0
            while True:
                self._tick(connection, deadline)
                chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - count))
                if not chunk:
                    break
                chunks.append(chunk)
                count += len(chunk)
                if count > MAX_RESPONSE_BYTES:
                    raise SeedanceError("invalid_response", "Seedance 响应过大。")
            try:
                diagnostics["failure_stage"] = "response_decode"
                value = json.loads(b"".join(chunks))
                if not isinstance(value, dict):
                    raise ValueError()
            except (ValueError, UnicodeError, RecursionError) as cause:
                raise SeedanceError("invalid_response", "Seedance 响应不是有效 JSON 对象。",
                                     exception_type=_exception_name(cause)) from None
            return value
        except SeedanceError as error:
            if modifying and sent and error.http_status is None:
                error.submission_uncertain = True
            raise self._annotate(error, diagnostics)
        except (OSError, http.client.HTTPException, ValueError) as cause:
            error = SeedanceError("network", "Seedance 网络连接失败或超时。",
                                  submission_uncertain=modifying and sent,
                                  exception_type=_exception_name(cause))
            raise self._annotate(error, diagnostics) from None
        finally:
            if connection is not None:
                connection.close()

    def _file_result(self, value, *, expected_id=None):
        file_id = self._identifier(value.get("id"), "file-", code="invalid_response")
        status = value.get("status")
        if not isinstance(status, str) or status not in {"processing", "active", "failed"} or expected_id and file_id != expected_id:
            raise SeedanceError("invalid_response", "Seedance 文件状态或 ID 不匹配。")
        result = {"file_id": file_id, "status": status}
        url = value.get("download_url")
        if url is not None:
            _url(url, code="invalid_response")
            if self._key in url:
                raise SeedanceError("invalid_response", "Seedance 文件地址无效。")
            result["video_reference"] = url
        for name in ("bytes", "expire_at"):
            if type(value.get(name)) is int and value[name] >= 0:
                result[name] = value[name]
        if status == "failed":
            err = value.get("error")
            result["error_code"] = self._provider_code(err.get("code")) if isinstance(err, dict) else None
        return result

    def upload_video(self, path):
        """Upload the original bytes to configured Ark /files; never transcodes.

        Returns file_id/status and video_reference when available. Persist the ID
        immediately, poll get_file until active, then require video_reference.
        Uploads have a separate 600-second budget by default; the normal API
        timeout remains 60 seconds. Both include connecting and reading the reply.
        """
        deadline = time.monotonic() + self.upload_timeout
        self._check(deadline)
        source = Path(path)
        try:
            size = source.stat().st_size
            if not source.is_file() or source.suffix.lower() not in {".mp4", ".mov"} or not 0 < size <= MAX_UPLOAD_BYTES:
                raise ValueError()
        except (OSError, ValueError):
            raise SeedanceError("invalid_input", "请提供不超过 200 MB 的本地 MP4 或 MOV 视频。") from None
        boundary = "video-localizer-" + secrets.token_hex(16)
        mime = "video/mp4" if source.suffix.lower() == ".mp4" else "video/quicktime"
        # A fixed safe filename avoids disclosing local paths or injecting MIME headers.
        prefix = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\nagent\r\n"
                  f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"source{source.suffix.lower()}\"\r\n"
                  f"Content-Type: {mime}\r\n\r\n").encode("ascii")
        suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        diagnostics = {}
        try:
            with source.open("rb") as stream:
                if os.fstat(stream.fileno()).st_size != size:
                    raise SeedanceError("invalid_input", "上传前原始文件已改变。")
                def chunks():
                    yield prefix
                    count = 0
                    while True:
                        self._check(deadline)
                        data = stream.read(CHUNK_BYTES)
                        if not data:
                            break
                        count += len(data)
                        if count > size:
                            raise SeedanceError("invalid_input", "上传期间原始文件已改变。")
                        yield data
                    if count != size:
                        raise SeedanceError("invalid_input", "上传期间原始文件已改变。")
                    yield suffix
                value = self._request("POST", "/files", body=chunks(),
                                      content_type="multipart/form-data; boundary=" + boundary,
                                      content_length=len(prefix) + size + len(suffix),
                                      deadline=deadline, diagnostics=diagnostics)
        except OSError as cause:
            error = SeedanceError("file_io", "无法读取或关闭本地上传视频。", failure_stage="body_read",
                                  exception_type=_exception_name(cause), completed_body_bytes=0,
                                  submission_uncertain=diagnostics.get("request_started", False))
            if diagnostics:
                # Closing the local stream can fail AFTER the POST completed.
                # Never turn that into permission to upload the same file again.
                diagnostics["failure_stage"] = "body_read"
                self._annotate(error, diagnostics)
            raise error from None
        try:
            return self._file_result(value)
        except SeedanceError as error:
            error.submission_uncertain = True
            diagnostics["failure_stage"] = "response_validation"
            raise self._annotate(error, diagnostics)

    def get_file(self, file_id):
        self._identifier(file_id, "file-")
        return self._file_result(self._request("GET", "/files/" + file_id), expected_id=file_id)

    def _reference(self, value, *, image=False):
        if isinstance(value, str) and value.startswith("asset://"):
            self._identifier(value[8:], "asset-")
        elif image and isinstance(value, str) and value.startswith("data:image/"):
            match = re.fullmatch(r"data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/]*={0,2})", value)
            if not match or len(value) > 28 * 1024**2:
                raise SeedanceError("invalid_input", "参考图片须为有界 JPEG、PNG 或 WebP Base64 图片。")
            try:
                if not base64.b64decode(match.group(2), validate=True):
                    raise ValueError()
            except ValueError:
                raise SeedanceError("invalid_input", "参考图片 Base64 无效。") from None
        else:
            _url(value)
        if self._key in value:
            raise SeedanceError("invalid_input", "媒体引用包含 API 凭据，已停止。")
        return value

    def create_edit(self, video_reference, prompt, *, resolution="720p", reference_images=None,
                    generate_audio=True, client_request_id=None):
        """Submit exactly one task; caller must durably save the returned task ID.

        Seedance 2.0 infers editing from the prompt and reference video; its
        duration=-1 chooses a 4–15 second duration rather than guaranteeing exact
        input timing. Only 2.5 supports the explicit edit task-type parameter.
        The creation request has a separate bounded budget (180 seconds by
        default); task queries retain the regular API budget (60 seconds).
        """
        deadline = time.monotonic() + self.create_timeout
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20000 or self._key in prompt:
            raise SeedanceError("invalid_input", "请提供有效且不含凭据的视频编辑提示词。")
        if resolution not in {"480p", "720p", "1080p"}:
            raise SeedanceError("invalid_input", "视频分辨率须为 480p、720p 或 1080p。")
        if type(generate_audio) is not bool:
            raise SeedanceError("invalid_input", "generate_audio 须为布尔值。")
        images = [] if reference_images is None else reference_images
        max_images = 9 if self.model_family == SEEDANCE_20_FAMILY else 30
        if not isinstance(images, (list, tuple)) or len(images) > max_images:
            raise SeedanceError("invalid_input", f"当前 Seedance 模型参考图片最多 {max_images} 张。")
        content = [{"type": "text", "text": prompt},
                   {"type": "video_url", "video_url": {"url": self._reference(video_reference)},
                    "role": "reference_video"}]
        content.extend({"type": "image_url", "image_url": {"url": self._reference(item, image=True)},
                        "role": "reference_image"} for item in images)
        payload = {"model": self.model, "content": content,
                   "ratio": "adaptive", "duration": -1, "resolution": resolution,
                   "generate_audio": generate_audio, "watermark": False}
        if self.model_family == SEEDANCE_25_FAMILY:
            payload["omni_reference_task_type"] = "edit"
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise SeedanceError("invalid_input", "视频编辑请求过大。")
        diagnostics = {}
        value = self._request("POST", "/contents/generations/tasks", body=body,
                              content_type="application/json", content_length=len(body), diagnostics=diagnostics,
                              client_request_id=self._client_request_id(client_request_id), deadline=deadline)
        try:
            return self._identifier(value.get("id"), "cgt-", code="invalid_response")
        except SeedanceError as error:
            error.submission_uncertain = True
            diagnostics["failure_stage"] = "response_validation"
            raise self._annotate(error, diagnostics)

    def get_task(self, task_id):
        self._identifier(task_id, "cgt-")
        value = self._request("GET", "/contents/generations/tasks/" + task_id)
        if value.get("id") != task_id or not isinstance(value.get("status"), str) or value["status"] not in _STATES:
            raise SeedanceError("invalid_response", "Seedance 任务 ID 或状态不匹配。")
        result = {"id": task_id, "status": value["status"]}
        if value["status"] == "succeeded":
            content = value.get("content")
            url = content.get("video_url") if isinstance(content, dict) else None
            _url(url, code="invalid_response")
            if self._key in url:
                raise SeedanceError("invalid_response", "Seedance 产物地址无效。")
            result["video_url"] = url
        if value["status"] == "failed":
            err = value.get("error")
            result["error_code"] = self._provider_code(err.get("code")) if isinstance(err, dict) else None
            if result["error_code"] == "OutputVideoSensitiveContentDetected.PolicyViolation":
                result["message"] = "Seedance 平台以生成视频可能涉及版权限制为由拒绝本次任务；上传和任务提交已完成，未产生可交付成片。原任务已保留，不会自动重新生成。"
        for name in ("created_at", "updated_at", "duration", "framespersecond"):
            if type(value.get(name)) in (int, float) and 0 <= value[name] <= 10**12 and math.isfinite(value[name]):
                result[name] = value[name]
        usage = value.get("usage")
        if isinstance(usage, dict):
            result["usage"] = {name: usage[name] for name in ("completion_tokens", "total_tokens")
                               if type(usage.get(name)) is int and usage[name] >= 0}
        return result

    def download(self, url, output_path, *, max_bytes=MAX_DOWNLOAD_BYTES):
        """Download a public HTTPS artifact without credentials or redirects.

        DNS answers are checked and the connection is pinned to a checked IP.
        Writes through a unique temporary file; an existing destination is kept.
        """
        parsed = _url(url, code="unsafe_download")
        if self._key in url:
            raise SeedanceError("unsafe_download", "下载地址包含 API 凭据，已停止。")
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_DOWNLOAD_BYTES:
            raise SeedanceError("invalid_input", "下载大小上限无效。")
        deadline = time.monotonic() + self.timeout
        self._check(deadline)
        addresses = _public_addresses(parsed.hostname)
        target = Path(output_path)
        if target.exists():
            raise SeedanceError("file_exists", "下载目标已存在，请选择新文件。")
        connection = _PinnedHTTPSConnection(parsed.hostname, addresses[0],
                                            timeout=min(self._check(deadline), _CONNECT_TIMEOUT))
        temporary = None
        try:
            connection.connect()
            self._tick(connection, deadline)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            connection.request("GET", path, headers={"Accept": "video/mp4,application/octet-stream",
                                                       "Accept-Encoding": "identity"})
            self._tick(connection, deadline)
            response = connection.getresponse()
            if response.status != 200:
                raise self._http_failure(response.status)
            length = response.getheader("Content-Length")
            if length is not None:
                if not re.fullmatch(r"[0-9]+", length) or not 0 < int(length) <= max_bytes:
                    raise SeedanceError("invalid_response", "产物下载大小无效或超过上限。")
            encoding = response.getheader("Content-Encoding")
            if encoding and encoding.lower() != "identity":
                raise SeedanceError("invalid_response", "产物下载使用了不支持的传输编码。")
            target.parent.mkdir(parents=True, exist_ok=True)
            digest, count = hashlib.sha256(), 0
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".seedance-", suffix=".part", delete=False) as out:
                temporary = Path(out.name)
                while True:
                    self._tick(connection, deadline)
                    data = response.read1(min(CHUNK_BYTES, max_bytes + 1 - count))
                    if not data:
                        break
                    count += len(data)
                    if count > max_bytes:
                        raise SeedanceError("invalid_response", "产物下载超过大小上限。")
                    out.write(data)
                    digest.update(data)
                if not count or length is not None and count != int(length):
                    raise SeedanceError("invalid_response", "产物下载不完整。")
                out.flush()
                os.fsync(out.fileno())
            self._check(deadline)
            # Exclusive creation protects existing user files, including a file
            # another process created while this download was in progress.
            if os.name == "nt":
                # Windows rename is atomic and fails if destination exists;
                # unlike hard links it also works in the app's sandbox.
                os.rename(temporary, target)
            else:
                os.link(temporary, target)
            return {"path": str(target.resolve()), "bytes": count, "sha256": digest.hexdigest()}
        except SeedanceError:
            raise
        except FileExistsError:
            raise SeedanceError("file_exists", "下载目标已存在，请选择新文件。") from None
        except (OSError, http.client.HTTPException, ValueError):
            raise SeedanceError("download_failed", "产物下载失败或超时。") from None
        finally:
            connection.close()
            if temporary is not None:
                temporary.unlink(missing_ok=True)
