"""Transport-only private media staging. Its credential is separate from Ark."""
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit

from . import config
from .seedance_client import SeedanceClient, SeedanceError, MAX_UPLOAD_BYTES, CHUNK_BYTES, _url


def configuration():
    path = config.app_dir() / "media-upload.json"
    if not path.exists():
        return None
    try:
        if path.stat().st_size > 8192:
            raise ValueError()
        value = json.loads(path.read_text(encoding="utf-8"))
        if set(value) != {"provider", "endpoint"} or value["provider"] != "self_hosted":
            raise ValueError()
        endpoint = value["endpoint"]
        parsed = _url(endpoint)
        if parsed.path != "/codex-media/v1" or parsed.query:
            raise ValueError()
        current = config.settings() or {}
        config.validated_media_endpoint(endpoint, current.get("base_url"))
        return value
    except (OSError, ValueError, SeedanceError, TypeError):
        raise config.SettingsError("自有素材服务器配置无效；不会改用其他上传平台。") from None


def binding():
    return configuration() or {"provider": "ark_files"}


def bind_report(report):
    current = binding()
    if report.get("media_backend", {"provider": "ark_files"}) != current:
        raise config.SettingsError("素材服务器与原批次不一致，请创建新批次；不会修改旧上传断点。")
    return current


class MediaClient:
    def __init__(self, endpoint, *, cancelled=None):
        self.endpoint = endpoint
        # Read ONLY the credential for this media endpoint. Never send Ark keys here.
        key = config.read_key(endpoint)
        if key.startswith("ark-"):
            raise config.SettingsError("素材上传凭据不能使用 Ark API Key。")
        self.transport = SeedanceClient(endpoint, key, cancelled=cancelled)

    @staticmethod
    def _id(sha256):
        if not isinstance(sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", sha256):
            raise SeedanceError("media_invalid", "素材 SHA-256 无效。")
        return sha256

    def _metadata(self, data, sha256, size):
        if (data.get("id") != sha256 or data.get("sha256") != sha256
                or type(data.get("bytes")) is not int or data["bytes"] != size):
            raise SeedanceError("media_invalid", "服务器素材校验记录与本机片段不一致。")
        return {"id": sha256, "sha256": sha256, "bytes": size}

    def ensure_uploaded(self, path, sha256):
        sha256 = self._id(sha256)
        source = Path(path)
        size = source.stat().st_size
        if not 12 <= size <= MAX_UPLOAD_BYTES:
            raise SeedanceError("media_invalid", "素材大小超出上传限制。")
        suffix = "/objects/" + sha256
        try:
            found = self.transport._request("GET", suffix)
        except SeedanceError as error:
            if error.http_status not in {404, 410}:
                raise
        else:
            return self._metadata(found, sha256, size)
        position = self.transport._request("GET", suffix + "/upload")
        offset = position.get("received_bytes")
        if (position.get("id") != sha256 or position.get("sha256") != sha256
                or type(offset) is not int or not 0 <= offset <= size):
            raise SeedanceError("media_invalid", "服务器上传断点无效。")
        deadline = time.monotonic() + 1200
        with source.open("rb") as stream:
            stream.seek(offset)
            while True:
                self.transport._check(deadline)
                block = stream.read(min(CHUNK_BYTES, size-offset))
                if not block and offset < size:
                    raise SeedanceError("file_io", "上传期间素材发生变化。")
                value = self.transport._request("PUT", suffix + f"/upload?offset={offset}&total={size}",
                    body=block, content_type="application/octet-stream", content_length=len(block), deadline=deadline)
                offset += len(block)
                if offset == size:
                    if stream.read(1):
                        raise SeedanceError("file_io", "上传期间素材发生变化。")
                    return self._metadata(value, sha256, size)
                if (value.get("id") != sha256 or value.get("sha256") != sha256
                        or type(value.get("received_bytes")) is not int or value["received_bytes"] != offset
                        or value.get("complete") is not False):
                    raise SeedanceError("media_invalid", "服务器分块回执无效。")

    def reference(self, sha256, size):
        sha256 = self._id(sha256)
        value = self.transport._request("POST", "/objects/" + sha256 + "/ticket",
                                        body=b"", content_type="application/json", content_length=0)
        self._metadata(value, sha256, size)
        url = value.get("url")
        parsed, base = _url(url, code="media_invalid"), urlsplit(self.endpoint)
        query = parse_qs(parsed.query, keep_blank_values=True)
        expires = value.get("expires_at")
        if (parsed.netloc != base.netloc or parsed.path != base.path + "/objects/" + sha256 + ".mp4"
                or type(expires) is not int or not time.time() + 60 < expires <= time.time() + 12 * 3600 + 60
                or set(query) != {"expires", "signature"} or query["expires"] != [str(expires)]
                or len(query["signature"]) != 1 or not re.fullmatch(r"[a-f0-9]{64}", query["signature"][0])):
            raise SeedanceError("media_invalid", "素材服务器返回的临时地址不符合本次文件绑定。")
        return url  # Only caller memory; never persist this bearer URL in reports.

    @classmethod
    def _job_payload(cls, payload):
        if not isinstance(payload, dict):
            raise SeedanceError("media_invalid", "服务器作业参数无效。")
        kind = payload.get("kind")
        if kind == "split" and set(payload) == {"kind", "source_id", "policy"}:
            cls._id(payload["source_id"])
            if payload["policy"] != "fixed-15s-v1":
                raise SeedanceError("media_invalid", "服务器分段策略无效。")
        elif kind == "import" and set(payload) == {"kind", "task_id", "url"}:
            if not isinstance(payload["task_id"], str) or not re.fullmatch(r"(?:cgt|amk)-[a-zA-Z0-9][a-zA-Z0-9_-]{0,119}", payload["task_id"]):
                raise SeedanceError("media_invalid", "云端任务 ID 无效。")
            _url(payload["url"], code="media_invalid")
            if len(payload["url"]) > 8192:
                raise SeedanceError("media_invalid", "云端候选地址过长。")
        elif kind == "join" and set(payload) == {"kind", "source_id", "split_job_id", "parts"}:
            cls._id(payload["source_id"])
            cls._id(payload["split_job_id"])
            parts = payload["parts"]
            if not isinstance(parts, list) or not 1 <= len(parts) <= 20:
                raise SeedanceError("media_invalid", "服务器拼接片段清单无效。")
            seen = set()
            for index, part in enumerate(parts, 1):
                if (not isinstance(part, dict) or set(part) != {"index", "task_id", "object_id"}
                        or type(part["index"]) is not int or part["index"] != index):
                    raise SeedanceError("media_invalid", "服务器拼接片段顺序无效。")
                cls._id(part["object_id"])
                if not isinstance(part["task_id"], str) or not re.fullmatch(r"cgt-[a-zA-Z0-9_-]{1,120}", part["task_id"]):
                    raise SeedanceError("media_invalid", "云端任务 ID 无效。")
                if part["task_id"] in seen:
                    raise SeedanceError("media_invalid", "服务器拼接不能重复使用生成任务。")
                seen.add(part["task_id"])
        elif kind == "render_subtitles":
            cls._render_payload(payload)
        else:
            raise SeedanceError("media_invalid", "服务器作业类型或字段无效。")
        return payload

    @classmethod
    def _render_payload(cls, payload):
        def valid(condition):
            if not condition:
                raise SeedanceError("media_invalid", "服务器字幕作业参数无效。")
        valid({"kind", "source_id", "clean_id", "cues"} <= set(payload)
              and set(payload) <= {"kind", "source_id", "clean_id", "cues", "style"})
        cls._id(payload["source_id"])
        cls._id(payload["clean_id"])
        cues = payload["cues"]
        valid(isinstance(cues, list) and 1 <= len(cues) <= 100)
        previous = -1
        for cue in cues:
            valid(isinstance(cue, dict) and {"start", "end", "text"} <= set(cue)
                  and set(cue) <= {"start", "end", "text", "box"})
            valid(all(type(cue[k]) in (int, float) and math.isfinite(cue[k]) for k in ("start", "end"))
                  and 0 <= cue["start"] < cue["end"] <= 300 and cue["start"] >= previous)
            previous = cue["start"]
            text = cue["text"]
            valid(isinstance(text, str) and 1 <= len(text) <= 2000 and text.strip()
                  and not any(c in text for c in "{}\\")
                  and not any(ord(c) < 32 and c != "\n" for c in text))
            if "box" in cue:
                box = cue["box"]
                valid(isinstance(box, dict) and set(box) == {"x", "y", "width", "height"}
                      and all(type(n) is int for n in box.values())
                      and 0 <= box["x"] < 8192 and 0 <= box["y"] < 8192
                      and 0 < box["width"] <= 8192 and 0 < box["height"] <= 8192)
        valid(all("box" in cue for cue in cues) or all("box" not in cue for cue in cues))
        style = payload.get("style", {})
        valid(isinstance(style, dict) and set(style) <= {"font_name", "font_size", "outline", "margin_v", "margin_x"})
        valid(isinstance(style.get("font_name", "Noto Sans CJK KR"), str)
              and re.fullmatch(r"[A-Za-z0-9 -]{1,80}", style.get("font_name", "Noto Sans CJK KR")))
        for key, low, high in (("font_size", 8, 120), ("outline", 0, 8), ("margin_v", 0, 500), ("margin_x", 0, 1920)):
            valid(key not in style or type(style[key]) is int and low <= style[key] <= high)

    @classmethod
    def _receipt(cls, value):
        if (not isinstance(value, dict) or value.get("id") != value.get("sha256")
                or type(value.get("bytes")) is not int or not 12 <= value["bytes"] <= MAX_UPLOAD_BYTES):
            raise SeedanceError("media_invalid", "服务器媒体回执无效。")
        cls._id(value["id"])
        return {name: value[name] for name in ("id", "sha256", "bytes")}

    @staticmethod
    def _media(value):
        if not isinstance(value, dict):
            raise SeedanceError("media_invalid", "服务器视频元数据无效。")
        clean = {}
        for name in ("durationMs", "width", "height", "fps", "bytes", "frame_count"):
            number = value.get(name)
            if number is None and name != "frame_count":
                raise SeedanceError("media_invalid", "服务器视频元数据缺少必要数值。")
            if number is not None:
                if type(number) not in (int, float) or not math.isfinite(number) or not 0 < number <= 10**10:
                    raise SeedanceError("media_invalid", "服务器视频数值无效。")
                clean[name] = number
        for name in ("fpsRatio", "videoCodec", "audioCodec"):
            text = value.get(name)
            if text is None and name != "audioCodec":
                raise SeedanceError("media_invalid", "服务器视频元数据缺少必要格式。")
            if text is not None and (not isinstance(text, str) or not re.fullmatch(r"[a-zA-Z0-9_./-]{1,40}", text)):
                raise SeedanceError("media_invalid", "服务器视频格式字段无效。")
            clean[name] = text
        if not re.fullmatch(r"[1-9][0-9]{0,8}(?:/[1-9][0-9]{0,8})?", clean["fpsRatio"]):
            raise SeedanceError("media_invalid", "服务器帧率比例无效。")
        if type(value.get("hasAudio")) is not bool:
            raise SeedanceError("media_invalid", "服务器音轨元数据无效。")
        clean["hasAudio"] = value["hasAudio"]
        if "sha256" in value:
            clean["sha256"] = MediaClient._id(value["sha256"])
        return clean

    @classmethod
    def _job_result(cls, kind, result):
        if not isinstance(result, dict):
            raise SeedanceError("media_invalid", "服务器作业缺少结果。")
        if kind == "import":
            return {**cls._receipt(result), "media": cls._media(result.get("media"))}
        if kind == "split":
            source_id = cls._id(result.get("source_id"))
            if result.get("policy") != "fixed-15s-v1":
                raise SeedanceError("media_invalid", "服务器分段策略不匹配。")
            parts = result.get("parts")
            if not isinstance(parts, list) or not 1 <= len(parts) <= 20:
                raise SeedanceError("media_invalid", "服务器分段数量无效。")
            clean, boundary = [], 0
            for index, part in enumerate(parts, 1):
                receipt = cls._receipt(part)
                if (type(part.get("index")) is not int or part["index"] != index
                        or any(type(part.get(k)) is not int for k in ("start_frame", "end_frame", "frame_count"))
                        or part["start_frame"] != boundary or part["end_frame"] <= boundary
                        or part["frame_count"] != part["end_frame"] - boundary):
                    raise SeedanceError("media_invalid", "服务器分段帧范围无效。")
                for name in ("start_seconds", "duration_seconds"):
                    val = part.get(name)
                    if type(val) not in (int, float) or not math.isfinite(val) or not 0 <= val <= 300:
                        raise SeedanceError("media_invalid", "服务器分段时长无效。")
                if not 4 <= part["duration_seconds"] <= 15.000001:
                    raise SeedanceError("media_invalid", "服务器分段未保持 4–15 秒范围。")
                boundary = part["end_frame"]
                clean.append({**receipt, **{name: part[name] for name in (
                    "index", "start_frame", "end_frame", "frame_count", "start_seconds", "duration_seconds")}})
            media = cls._media(result.get("source_media"))
            if media.get("frame_count") != boundary or media.get("sha256") != source_id:
                raise SeedanceError("media_invalid", "服务器分段未覆盖对应原片。")
            return {"source_id": source_id, "source_media": media, "policy": result["policy"], "parts": clean}
        if kind in {"join", "render_subtitles"}:
            output = cls._receipt(result.get("output"))
            review = result.get("technical_review")
            if not isinstance(review, dict) or review.get("status") not in {"passed", "failed"}:
                raise SeedanceError("media_invalid", "服务器技术检查结果无效。")
            checks = review.get("checks")
            if (not isinstance(checks, dict) or not checks or len(checks) > 32
                    or any(not isinstance(k, str) or not re.fullmatch(r"[a-z_]{1,80}", k) or type(v) is not bool
                           for k, v in checks.items())
                    or (review["status"] == "passed") != all(checks.values())):
                raise SeedanceError("media_invalid", "服务器技术检查字段无效。")
            clean_review = {"status": review["status"], "checks": checks,
                            "output_media": cls._media(review.get("output_media")),
                            "visual_acceptance": "not_performed", "model_reviewed": False}
            if review.get("output_sha256") not in {None, output["id"]}:
                raise SeedanceError("media_invalid", "服务器候选哈希记录不匹配。")
            clean_review["output_sha256"] = output["id"]
            clean_review["source_audio_reused"] = review.get("source_audio_reused") is True
            if kind == "render_subtitles":
                if (review.get("composition_mode") not in {"source_roi", "full_clean"}
                        or review.get("pixel_preservation_guaranteed") is not False
                        or review.get("video_reencoded") is not True
                        or type(review.get("cue_count")) is not int or not 1 <= review["cue_count"] <= 100):
                    raise SeedanceError("media_invalid", "服务器字幕合成检查结果无效。")
                for name in ("composition_mode", "pixel_preservation_guaranteed", "video_reencoded", "cue_count"):
                    clean_review[name] = review[name]
                return {"source_id": cls._id(result.get("source_id")), "clean_id": cls._id(result.get("clean_id")),
                        "output": output, "technical_review": clean_review}
            return {"output": output, "technical_review": clean_review}
        raise SeedanceError("media_invalid", "服务器作业类型无效。")

    @classmethod
    def _job(cls, value, *, expected_id=None, expected_kind=None):
        if not isinstance(value, dict):
            raise SeedanceError("media_invalid", "服务器作业响应无效。")
        job_id = cls._id(value.get("id"))
        kind, status = value.get("kind"), value.get("status")
        if (expected_id is not None and job_id != expected_id or expected_kind is not None and kind != expected_kind
                or kind not in {"split", "import", "join", "render_subtitles"} or status not in {"queued", "running", "succeeded", "failed"}):
            raise SeedanceError("media_invalid", "服务器作业身份或状态不匹配。")
        clean = {"id": job_id, "kind": kind, "status": status}
        if status == "succeeded":
            clean["result"] = cls._job_result(kind, value.get("result"))
        elif status == "failed":
            error = value.get("error")
            clean["error"] = error if isinstance(error, str) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", error) else "processing_failed"
        for name in ("created_at", "updated_at"):
            number = value.get(name)
            if type(number) in (int, float) and math.isfinite(number) and 0 <= number <= 10**12:
                clean[name] = number
        return clean

    def submit_job(self, payload):
        payload = self._job_payload(payload)
        if self.transport._key in json.dumps(payload, ensure_ascii=False):
            raise SeedanceError("media_invalid", "服务器作业参数不能包含上传凭据。")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > 65536:
            raise SeedanceError("media_invalid", "服务器作业参数过大。")
        value = self.transport._request("POST", "/jobs", body=body,
                                        content_type="application/json", content_length=len(body))
        if self.transport._key in json.dumps(value, ensure_ascii=False):
            raise SeedanceError("media_invalid", "服务器作业响应包含凭据，已停止保存。")
        job = self._job(value, expected_kind=payload["kind"])
        if payload["kind"] == "render_subtitles" and job["status"] == "succeeded":
            if any(job["result"][key] != payload[key] for key in ("source_id", "clean_id")):
                raise SeedanceError("media_invalid", "服务器字幕结果不属于本次原片与擦字成片。")
        return job

    def get_job(self, job_id):
        job_id = self._id(job_id)
        value = self.transport._request("GET", "/jobs/" + job_id)
        if self.transport._key in json.dumps(value, ensure_ascii=False):
            raise SeedanceError("media_invalid", "服务器作业响应包含凭据，已停止保存。")
        return self._job(value, expected_id=job_id)
