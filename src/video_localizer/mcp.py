"""Async stdio MCP entry points for the persistent Hunyuan controller."""

import json
import os
from pathlib import Path
import subprocess
import sys
import threading

from . import __version__
from .batch import ANALYSIS_FIELDS, convert, doctor, resume, submit_review
from .config import app_dir
from .backend import check_model


PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
PARAMS = {
    "inputs": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1, "maxItems": 20,
               "description": "Absolute paths of local MP4 files or immediate input folders."},
    "output_dir": {"type": "string", "description": "Absolute output folder chosen by the user."},
    "target": {"type": "string"}, "source_language": {"type": "string"},
    "profile": {"type": "string", "enum": ["source", "2k120"]},
    "translations": {"type": "string", "description": "Optional finalized translation table."},
    "glossary": {"type": "string"}, "references": {"type": "string"},
    "request": {"type": "string", "maxLength": 32000,
                "description": "Forward the human's original video requirements. The selected cloud model decides translations and edits; the host must not invent them."},
    "max_turns": {"type": "integer", "minimum": 1, "maximum": 200, "default": 80},
}
DECISION = {"type": "object", "properties": {
    "action": {"type": "string", "enum": ["keep", "replace", "adjust_timing", "retry", "reject"]},
    "note": {"type": "string", "maxLength": 4000},
    "source_text": {"type": "string", "maxLength": 4000, "description": "Exact original text confirmed by the human; use with target_text and source-frame interval to bind a specific change."},
    "target_text": {"type": "string", "maxLength": 4000},
    "start_frame": {"type": "integer", "minimum": 0}, "end_frame": {"type": "integer", "minimum": 0}},
    "required": ["action"], "additionalProperties": False}


def _tool(name, description, properties=None, required=(), read_only=False):
    return {"name": name, "description": description, "annotations": {"readOnlyHint": read_only},
            "inputSchema": {"type": "object", "properties": properties or {},
                            "required": list(required), "additionalProperties": False}}


TOOLS = [
    _tool("check_setup", "Check runtime, selected provider and saved configuration. video_workflow_ready reports whether video production is implemented. Does not contact the model.", read_only=True),
    _tool("open_model_configuration", "Open the Windows configuration window; the user enters the API key locally. Check setup after saving."),
    _tool("check_model", "Check the selected provider: Hy4 performs a billable tool-call round trip; Seedance only checks video API authentication without submitting generation, and cannot verify model entitlement. Poll batch_progress.",
          {"image_path": {"type": "string", "description": "Optional absolute JPEG/PNG/WebP test image."},
           "timeout": {"type": "number", "minimum": 10, "maximum": 600, "default": 180}}),
    _tool("convert_videos", "Start a batch with the configured provider. Seedance uses billable remote editing; supports source profile, 4-30 second originals. Seedance 2.0 mechanically splits into up to two <=15 second segments, edits each in cloud, joins unchanged candidate video streams and reuses original audio. 2.5 edits the whole source. Cloud model makes visual decisions; results require review. Poll batch_progress.",
          PARAMS, ("inputs", "output_dir")),
    _tool("resume_batch", "Resume an existing persistent batch after fixing an error, increasing the turn allowance, or recording human decisions. Completed videos are skipped.",
          {"batch_dir": {"type": "string"}, "max_turns": {"type": "integer", "minimum": 1, "maximum": 200}}, ("batch_dir",)),
    _tool("cancel_batch", "Stop local work and preserve checkpoints. An in-flight request may finish. Submitted Seedance cloud tasks may continue and incur charges; resume queries the existing task rather than submitting again."),
    _tool("submit_review", "Record an explicit human decision only. Never manufacture approval or choose for the human. Then call resume_batch when authorized to continue.",
          {"batch_dir": {"type": "string"}, "video_index": {"type": "integer", "minimum": 1, "maximum": 20},
           "issue_id": {"type": "string"}, "decision": DECISION}, ("batch_dir", "video_index", "issue_id", "decision")),
    _tool("batch_progress", "Wait briefly and return phase, read_only_turns, compactions, output_recoveries and precise final/paused/review status. paused_budget means the turn allowance was exhausted; error_code output_limit or stalled means generation or analysis could not progress. None of these is completion or full-video quality approval.",
          {"wait_seconds": {"type": "integer", "minimum": 0, "maximum": 20}}, read_only=True),
]


def _absolute(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("MCP 文件、批次和输出目录请使用本机绝对路径。")


class Session:
    def __init__(self):
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.cancel = threading.Event()
        self.running = False
        self.state = {"status": "idle"}

    def _run(self, operation, args):
        def update(event):
            with self.lock:
                previous = self.state.get("progress", {})
                # Some tool events do not repeat controller analysis counters.
                # Retain them only for this video, never across a new video.
                retained = {key: previous[key] for key in ANALYSIS_FIELDS if key in previous}
                if event.get("stage") in {"batch_started", "start"} or previous.get("index") != event.get("index"):
                    retained = {}
                self.state = {"status": "processing", "operation": operation, "progress": {**retained, **event},
                              "cancellation_requested": self.cancel.is_set()}
        try:
            if operation == "check_model":
                result = check_model(**args, cancelled=self.cancel.is_set)
            else:
                function = convert if operation == "convert_videos" else resume
                result = function(**args, progress=update, cancelled=self.cancel.is_set)
            result = {**result, "operation": operation}
        except Exception as exc:
            result = {"status": "failed", "operation": operation, "message": str(exc)}
        with self.lock:
            self.state = result
            self.running = False
            self.done.set()

    def _start(self, operation, args):
        with self.lock:
            if self.running:
                raise ValueError("当前连接已有任务运行，请查看 batch_progress 或取消后重试。")
            self.state = {"status": "processing", "operation": operation}
            self.running = True
            self.done.clear()
            self.cancel.clear()
            # On ordinary stdio EOF, allow the cancellation callback to stop an
            # active executor and persist its checkpoint before Python exits.
            threading.Thread(target=self._run, args=(operation, args), daemon=False).start()
            return self.state.copy()

    def call(self, name, args):
        if not isinstance(args, dict):
            raise ValueError("工具参数需要 JSON 对象。")
        if name == "check_setup" and not args:
            return doctor()
        if name == "open_model_configuration" and not args:
            launcher = Path(os.environ.get("VIDEO_LOCALIZER_EXE") or app_dir() / "video-localizer.exe")
            if not launcher.is_absolute() or not launcher.is_file():
                raise ValueError("找不到本机安装的 Video Localizer，请先安装 Windows 版。")
            subprocess.Popen([str(launcher), "setup", "--gui"], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW)
            return {"status": "window_requested", "message": "请在本机窗口保存模型配置，然后调用 check_setup。"}
        if name == "convert_videos":
            if set(args) - PARAMS.keys() or not {"inputs", "output_dir"} <= args.keys():
                raise ValueError("请提供 inputs、output_dir 及支持的参数。")
            files = args["inputs"]
            if not isinstance(files, list) or not 1 <= len(files) <= 20:
                raise ValueError("请提供 1–20 个本机输入路径。")
            for value in [*files, args["output_dir"], *(args[key] for key in ("translations", "glossary", "references") if key in args)]:
                _absolute(value)
            return self._start(name, args)
        if name == "resume_batch" and not set(args) - {"batch_dir", "max_turns"} and "batch_dir" in args:
            _absolute(args["batch_dir"])
            return self._start(name, args)
        if name == "check_model" and not set(args) - {"image_path", "timeout"}:
            if "image_path" in args:
                _absolute(args["image_path"])
            timeout = args.get("timeout", 180)
            if type(timeout) not in (int, float) or not 10 <= timeout <= 600:
                raise ValueError("timeout 取值为 10–600 秒。")
            return self._start(name, args)
        if name == "cancel_batch" and not args:
            with self.lock:
                if self.running:
                    self.cancel.set()
                    self.state["cancellation_requested"] = True
                return self.state.copy()
        if name == "submit_review" and set(args) == {"batch_dir", "video_index", "issue_id", "decision"}:
            _absolute(args["batch_dir"])
            with self.lock:
                if self.running:
                    raise ValueError("请等当前任务暂停后再提交审核意见。")
            if not isinstance(args["decision"], dict) or set(args["decision"]) - DECISION["properties"].keys():
                raise ValueError("审核决定包含不支持的字段。")
            return submit_review(**args)
        if name == "batch_progress" and not set(args) - {"wait_seconds"}:
            seconds = args.get("wait_seconds", 10)
            if type(seconds) is not int or not 0 <= seconds <= 20:
                raise ValueError("wait_seconds 取值为 0–20。")
            with self.lock:
                active = self.running
            if active:
                self.done.wait(seconds)
            with self.lock:
                return self.state.copy()
        raise ValueError("工具名或参数无效。")


def serve():
    session = Session()
    output = sys.stdout.buffer
    try:
        for line in sys.stdin.buffer:
            request_id = None
            try:
                request = json.loads(line)
                if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                    raise ValueError()
                if "id" not in request:
                    continue
                request_id = request["id"]
                method, params = request.get("method"), request.get("params") or {}
                if not isinstance(params, dict):
                    raise ValueError()
                if method == "initialize":
                    version = params.get("protocolVersion")
                    result = {"protocolVersion": version if version in PROTOCOLS else PROTOCOLS[0],
                              "capabilities": {"tools": {}}, "serverInfo": {"name": "video-localizer", "version": __version__}}
                elif method == "tools/list":
                    result = {"tools": TOOLS}
                elif method == "ping":
                    result = {}
                elif method == "tools/call":
                    try:
                        value = session.call(params.get("name"), params.get("arguments", {}))
                        result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                                  "isError": value.get("status") in {"failed", "paused_error"}}
                    except (ValueError, OSError, RuntimeError) as exc:
                        result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
                else:
                    raise KeyError(method)
                response = {"jsonrpc": "2.0", "id": request_id, "result": result}
            except (ValueError, KeyError, TypeError) as exc:
                code = -32700 if isinstance(exc, json.JSONDecodeError) else -32601 if isinstance(exc, KeyError) else -32600
                response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": "Unsupported method or invalid request"}}
            output.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            output.flush()
    finally:
        session.cancel.set()
