"""Connect the installed executable to the assistant chosen by the user."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from .config import app_dir, write_json


def connect(host, preview=False):
    exe = Path(os.environ.get("VIDEO_LOCALIZER_EXE") or app_dir() / "video-localizer.exe")
    if not exe.is_file() and not preview:
        raise ValueError("请先安装 Windows 版，再注册助手入口。")
    server = {"type": "stdio", "command": str(exe), "args": ["mcp"]}
    if host == "generic":
        return {"mcpServers": {"video-localizer": server}}
    if host in {"codex", "claude"}:
        if host == "codex":
            args = ["mcp", "add", "video-localizer", "--", str(exe), "mcp"]
        else:
            args = ["mcp", "add", "--scope", "user", "--transport", "stdio",
                    "video-localizer", "--", str(exe), "mcp"]
        if preview:
            return {"command": [host, *args]}
        command = shutil.which(host)
        if not command:
            raise ValueError(f"找不到 {host} CLI。可用 connect --host generic 查看 MCP 启动参数。")
        proc = subprocess.run([command, *args], stdout=sys.stderr, stderr=sys.stderr, check=False)
        if proc.returncode:
            raise ValueError(f"{host} 注册失败（退出码 {proc.returncode}）。")
        return {"connected": host, "command": str(exe)}

    paths = {"cursor": Path.home() / ".cursor/mcp.json",
             "copilot": Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot") / "mcp-config.json"}
    path = paths[host]
    if preview:
        return {"path": str(path), "entry": {"mcpServers": {"video-localizer": server}}}
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    except (UnicodeError, ValueError):
        raise ValueError(f"已有配置使用自定义格式。请通过助手的 MCP 设置加入 connect --host generic 返回的启动命令。文件：{path}") from None
    if not isinstance(config, dict) or not isinstance(config.get("mcpServers", {}), dict):
        raise ValueError(f"MCP 配置结构无法解析：{path}")
    servers = config.setdefault("mcpServers", {})
    if "video-localizer" in servers and servers["video-localizer"] != server:
        raise ValueError(f"已有同名 MCP 配置。请在助手中核对该项：{path}")
    servers["video-localizer"] = server
    write_json(path, config)
    return {"connected": host, "path": str(path)}
