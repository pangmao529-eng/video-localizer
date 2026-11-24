"""Newline-delimited JSON-RPC over stdio, scoped to the running assistant."""

import json
import os
from pathlib import Path
import subprocess
import sys
import threading

from . import __version__
from .batch import convert, doctor
from .config import app_dir


PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
PARAMS = {
    "inputs": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1, "maxItems": 20,
               "description": "Absolute paths of local MP4 files or immediate input folders."},
    "output_dir": {"type": "string", "description": "Absolute local output folder chosen by the user."},
    "target": {"type": "string", "description": "Target language, default Korean."},
    "source_language": {"type": "string", "description": "Source language, default auto."},
    "profile": {"type": "string", "enum": ["source", "2k120"]},
    "translations": {"type": "string", "description": "Optional local finalized translation table."},
    "glossary": {"type": "string", "description": "Optional local glossary."},
    "references": {"type": "string", "description": "Optional portraits folder or JSON reference manifest."},
}
TOOLS = [
    {"name": "check_setup", "description": "Check local runtime and whether a model credential is configured. Does not contact the model.",
     "annotations": {"readOnlyHint": True}, "inputSchema": EMPTY},
    {"name": "open_model_configuration",
     "description": "Open the Windows model settings window so the user can enter their API URL, vision model ID and API key locally. After the user saves, call check_setup.",
     "annotations": {"readOnlyHint": False}, "inputSchema": EMPTY},
    {"name": "convert_videos", "description": "Start a local video lettering batch in this connection. Model calls send selected frames to the configured API. Poll batch_progress until complete, then present output and review paths.", "inputSchema": {
        "type": "object", "properties": PARAMS, "required": ["inputs", "output_dir"], "additionalProperties": False},
     "annotations": {"readOnlyHint": False}},
    {"name": "batch_progress", "description": "Wait briefly for the active batch in this assistant and return progress or its final result.", "inputSchema": {
        "type": "object", "properties": {"wait_seconds": {"type": "integer", "minimum": 0, "maximum": 20}},
        "additionalProperties": False}, "annotations": {"readOnlyHint": True}},
]


class Session:
    def __init__(self):
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.state = {"status": "idle"}

    def _run(self, args):
        def update(event):
            with self.lock:
                self.state = {"status": "processing", "progress": event}
        try:
            summary = convert(**args, progress=update)
            result = {"status": "finished", **summary}
        except Exception as exc:
            result = {"status": "failed", "message": str(exc)}
        with self.lock:
            self.state = result
            self.done.set()

    def call(self, name, args):
        if not isinstance(args, dict):
            raise ValueError("工具参数需要 JSON 对象。")
        if name == "check_setup" and not args:
            return doctor()
        if name == "open_model_configuration" and not args:
            launcher = Path(os.environ.get("VIDEO_LOCALIZER_EXE") or app_dir() / "video-localizer.exe")
            if not launcher.is_absolute() or not launcher.is_file():
                raise ValueError("找不到本机安装的 Video Localizer，请先安装 Windows 版。")
            subprocess.Popen(
                [str(launcher), "setup", "--gui"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return {"status": "window_requested",
                    "message": "请在本机配置窗口填写 API 地址、视觉模型 ID 和 API Key。保存后调用 check_setup 检查配置状态。"}
        if name == "convert_videos":
            if set(args) - PARAMS.keys() or not {"inputs", "output_dir"} <= args.keys():
                raise ValueError("请提供 inputs、output_dir 及支持的可选参数。")
            files = args["inputs"]
            if not isinstance(files, list) or not 1 <= len(files) <= 20:
                raise ValueError("请提供 1–20 个本机输入路径。")
            for item in [*files, args["output_dir"], *(args[k] for k in ("translations","glossary","references") if k in args)]:
                if not isinstance(item, str) or not Path(item).is_absolute():
                    raise ValueError("MCP 输入、输出和资料文件请使用本机绝对路径。")
            with self.lock:
                if self.state["status"] == "processing":
                    raise ValueError("当前批次正在处理，请调用 batch_progress。")
                self.state = {"status": "processing"}
                self.done.clear()
                threading.Thread(target=self._run, args=(args,), daemon=True).start()
                return self.state.copy()
        if name == "batch_progress" and not set(args) - {"wait_seconds"}:
            seconds = args.get("wait_seconds", 10)
            if type(seconds) is not int or not 0 <= seconds <= 20:
                raise ValueError("wait_seconds 取值为 0–20。")
            with self.lock:
                active = self.state["status"] == "processing"
            if active:
                self.done.wait(seconds)
            with self.lock:
                return self.state.copy()
        raise ValueError("工具名或参数无效。")


def serve():
    session = Session()
    output = sys.stdout.buffer
    for line in sys.stdin.buffer:
        request_id = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                raise ValueError()
            if "id" not in request:
                continue
            request_id = request["id"]
            method = request.get("method")
            params = request.get("params") or {}
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
                              "isError": value.get("status") == "failed"}
                except (ValueError, OSError) as exc:
                    result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            else:
                raise KeyError(method)
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except (ValueError, KeyError, TypeError) as exc:
            response = {"jsonrpc": "2.0", "id": request_id, "error": {
                "code": -32601 if isinstance(exc, KeyError) else -32600, "message": "Unsupported method or invalid request"}}
        output.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
        output.flush()
