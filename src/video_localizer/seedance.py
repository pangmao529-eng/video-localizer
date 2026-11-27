"""Read-only Ark video API authentication check; no generation task is submitted."""
import http.client
import json
import math
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

MAX_PROBE_BYTES = 1024 * 1024


def check_api(*, timeout=60, cancelled=None):
    from .config import SettingsError, read_key, settings, seedance_family

    result = {"status": "failed", "provider": "seedance", "test_type": "api_authentication",
              "authentication_verified": False, "model_access_verified": False,
              "generation_submitted": False, "video_workflow_ready": False,
              "automatic_visual_review_available": False}
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 10 <= timeout <= 600:
        return {**result, "error_code": "invalid_input", "message": "timeout 取值为 10–600 秒。"}
    if cancelled and cancelled():
        return {**result, "status": "cancelled"}
    try:
        data = settings()
        if not data or data.get("provider") != "seedance":
            raise SettingsError("请先保存 Seedance 配置。")
        key = read_key(data["base_url"])
    except SettingsError as error:
        return {**result, "error_code": "configuration", "message": str(error)}
    result.update(model=data["model"], endpoint=data["base_url"],
                  model_family=seedance_family(data["model"], data.get("model_family")))
    # Query one item only. Existing task contents are neither returned nor logged.
    request = Request(data["base_url"].rstrip("/") + "/contents/generations/tasks?page_size=1",
                      headers={"Authorization": "Bearer " + key, "Accept": "application/json"}, method="GET")
    started = time.monotonic()
    try:
        # Redirects must never forward the credential to another endpoint.
        with build_opener(_NoRedirect()).open(request, timeout=min(timeout, 60)) as response:
            result["http_status"] = response.status
            payload = response.read(MAX_PROBE_BYTES + 1)
        if len(payload) > MAX_PROBE_BYTES:
            raise ValueError("response too large")
        body = json.loads(payload)
        if (not isinstance(body, dict) or not isinstance(body.get("items"), list)):
            raise ValueError("unexpected response")
        result.update(status="passed", authentication_verified=True,
                      message="Seedance 视频任务查询接口鉴权通过；尚未验证模型开通或视频生成。")
    except HTTPError as error:
        result["http_status"] = error.code
        error.close()
        if error.code == 401:
            result.update(error_code="authentication", message="Seedance 接口拒绝此密钥，请核对密钥所属平台和 API 地址。")
        elif error.code == 403:
            result.update(error_code="permission_denied", message="当前凭据无权访问 Seedance 视频任务查询接口。")
        elif 300 <= error.code < 400:
            result.update(error_code="redirect_blocked", message="接口发生重定向，已停止发送凭据。")
        else:
            result.update(error_code="http_error", message=f"Seedance 接口返回 HTTP {error.code}。")
    except (URLError, http.client.HTTPException, OSError, TimeoutError, socket.timeout):
        result.update(error_code="network", message="Seedance 接口网络连接失败或超时。")
    except (ValueError, UnicodeError):
        result.update(error_code="invalid_response", message="接口响应不符合 Seedance 视频任务列表格式。")
    finally:
        key = None
        result["elapsed_seconds"] = round(time.monotonic() - started, 4)
    if cancelled and cancelled():
        result["status"] = "cancelled"
    return result
