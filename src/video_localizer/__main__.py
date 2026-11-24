"""Windows command line for user setup, MCP registration and batch processing."""

import argparse
import json
import sys

from . import __version__


def main():
    from .batch import convert, doctor
    from .config import configure
    from .hosts import connect
    parser = argparse.ArgumentParser(prog="video-localizer", description="本机批量视频改字，保留原声。")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(required=True, dest="command")
    setup = commands.add_parser("setup", help="设置视觉模型与 Windows 凭据")
    setup.add_argument("--gui", action="store_true", help="打开本机密钥输入窗口")
    setup.add_argument("--base-url")
    setup.add_argument("--model")
    commands.add_parser("doctor", help="检查本机组件与凭据状态")
    commands.add_parser("mcp", help="启动 stdio MCP 入口")
    host = commands.add_parser("connect", help="把插件连接到选定的 AI 助手")
    host.add_argument("--host", choices=["codex", "claude", "cursor", "copilot", "generic"], required=True)
    host.add_argument("--print", action="store_true", dest="preview", help="只显示连接参数")
    batch = commands.add_parser("convert", help="将视频或目录批量转成改字 MP4")
    batch.add_argument("inputs", nargs="+", help="1–20 条 MP4，或包含 MP4 的目录")
    batch.add_argument("--output", required=True, dest="output_dir")
    batch.add_argument("--target", default="韩语")
    batch.add_argument("--source-language", default="auto")
    batch.add_argument("--profile", choices=["source", "2k120"], default="source")
    batch.add_argument("--translations", help="定稿表")
    batch.add_argument("--glossary", help="术语表")
    batch.add_argument("--references", help="角色参考图目录或 JSON 清单")
    options = vars(parser.parse_args())
    command = options.pop("command")
    try:
        if command == "mcp":
            from .mcp import serve
            serve()
            return 0
        if command == "setup":
            result = configure(gui=options["gui"], endpoint=options["base_url"], model=options["model"])
        elif command == "connect":
            result = connect(**options)
        elif command == "doctor":
            result = doctor()
        else:
            def progress(event):
                print(json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)
            result = convert(**options, progress=progress)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("failed") or result.get("unprocessed") else 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        print(f"Video Localizer: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
