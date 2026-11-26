"""Windows CLI for Hunyuan setup, diagnostics and resumable video batches."""

import argparse
import json
from pathlib import Path
import sys

from . import __version__


def _decision(value):
    if value.startswith("@"):
        path = Path(value[1:])
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise ValueError("审核决定文件不存在或超过 1 MiB。")
        value = path.read_text(encoding="utf-8-sig")
    result = json.loads(value)
    if not isinstance(result, dict):
        raise ValueError("审核决定必须为 JSON 对象。")
    return result


def main():
    from .batch import convert, doctor, resume, submit_review
    from .config import configure
    from .hosts import connect
    from .backend import check_model
    parser = argparse.ArgumentParser(prog="video-localizer", description="视频改字插件：混元制作流程及 Seedance API 配置。",
        epilog="结果以 JSON 报告阶段与暂停原因；paused_budget、output_limit、stalled 均不表示制作完成或画面通过验收。")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(required=True, dest="command")
    setup = commands.add_parser("setup", help="设置模型提供方及 Windows 凭据")
    setup.add_argument("--provider", choices=["hunyuan", "seedance"])
    setup.add_argument("--gui", action="store_true")
    setup.add_argument("--base-url")
    setup.add_argument("--model")
    setup.add_argument("--model-family", choices=["seedance-2.0", "seedance-2.5"],
                       help="使用 Ark ep- 推理接入点时指定已开通的 Seedance 系列")
    commands.add_parser("doctor", help="只检查本机组件与凭据，不调用模型")
    commands.add_parser("mcp", help="启动 stdio MCP 入口")
    test = commands.add_parser("test-model", help="检查当前提供方：混元验证工具回合；Seedance只检查API鉴权，不提交生成")
    test.add_argument("--image", dest="image_path", help="可选测试图片；会发送给混元")
    test.add_argument("--timeout", type=float, default=180)
    host = commands.add_parser("connect", help="把插件连接到选定的 AI 助手")
    host.add_argument("--host", choices=["codex", "claude", "cursor", "copilot", "generic"], required=True)
    host.add_argument("--print", action="store_true", dest="preview")
    batch = commands.add_parser("convert", help="使用所选云端模型制作视频，创建持久批次；Seedance 会上传原片并产生生成费用")
    batch.add_argument("inputs", nargs="+")
    batch.add_argument("--output", required=True, dest="output_dir")
    batch.add_argument("--target", default="韩语")
    batch.add_argument("--source-language", default="auto")
    batch.add_argument("--profile", choices=["source", "2k120"], default="source")
    batch.add_argument("--translations")
    batch.add_argument("--glossary")
    batch.add_argument("--references")
    batch.add_argument("--request", default="", help="直接传给所选模型的原始要求")
    batch.add_argument("--max-turns", type=int, default=80, help="混元轮数或 Seedance 查询等待次数上限，1–200；Seedance 续跑不会重复提交生成")
    again = commands.add_parser("resume", help="从检查点继续批次，跳过已完成视频")
    again.add_argument("--batch-dir", required=True)
    again.add_argument("--max-turns", type=int)
    review = commands.add_parser("review", help="记录人工决定；之后使用 resume 继续")
    review.add_argument("--batch-dir", required=True)
    review.add_argument("--video-index", required=True, type=int)
    review.add_argument("--issue-id", required=True)
    review.add_argument("--decision", required=True, help="JSON 对象，或 @UTF-8文件路径")
    options = vars(parser.parse_args())
    command = options.pop("command")

    def progress(event):
        print(json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)

    try:
        if command == "mcp":
            from .mcp import serve
            serve()
            return 0
        if command == "setup":
            result = configure(gui=options["gui"], endpoint=options["base_url"], model=options["model"],
                               provider=options["provider"], model_family=options["model_family"])
        elif command == "connect":
            result = connect(**options)
        elif command == "doctor":
            result = doctor()
        elif command == "test-model":
            result = check_model(**options)
        elif command == "resume":
            result = resume(**options, progress=progress)
        elif command == "review":
            options["decision"] = _decision(options["decision"])
            result = submit_review(**options)
        else:
            result = convert(**options, progress=progress)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("status") in {"failed", "paused_error", "paused_budget", "cancelled", "needs_review"} else 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        print(f"Video Localizer: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
