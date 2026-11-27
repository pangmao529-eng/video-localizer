"""Windows CLI for cloud video editing and resumable subtitle replacement."""

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
    parser = argparse.ArgumentParser(prog="video-localizer", description="视频改字：Seedance 云端编辑或 MediaKit 精细擦字与定稿字幕合成。",
        epilog="结果以 JSON 报告阶段；平台拒绝、等待额度或 needs_review 均不表示成片通过验收。")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(required=True, dest="command")
    setup = commands.add_parser("setup", help="设置模型提供方及 Windows 凭据")
    setup.add_argument("--provider", choices=["seedance"], default="seedance")
    setup.add_argument("--gui", action="store_true")
    setup.add_argument("--base-url")
    setup.add_argument("--model")
    setup.add_argument("--media-base-url", help="自有素材服务器 HTTPS 地址，与 Ark API 地址及凭据分开")
    setup.add_argument("--model-family", choices=["seedance-2.0", "seedance-2.5"],
                       help="使用 Ark ep- 推理接入点时指定已开通的 Seedance 系列")
    commands.add_parser("doctor", help="只检查本机组件与凭据，不调用模型")
    mediakit_setup = commands.add_parser("setup-mediakit", help="交互式保存独立 MediaKit 凭据，不改变 Ark 配置")
    mediakit_setup.add_argument("--model-version", choices=["v5", "v5.6"], default="v5.6")
    commands.add_parser("mediakit-doctor", help="只检查独立 MediaKit 配置与凭据，不访问网络")
    commands.add_parser("mcp", help="启动 stdio MCP 入口")
    test = commands.add_parser("test-model", help="只检查 Seedance API 鉴权，不提交生成")
    test.add_argument("--timeout", type=float, default=180)
    host = commands.add_parser("connect", help="把插件连接到选定的 AI 助手")
    host.add_argument("--host", choices=["codex", "claude", "cursor", "copilot", "generic"], required=True)
    host.add_argument("--print", action="store_true", dest="preview")
    batch = commands.add_parser("convert", help="Seedance 云端编辑；2.0 原片在服务器分段、下载候选并拼接，本机仅下载整片")
    batch.add_argument("inputs", nargs="+")
    batch.add_argument("--output", required=True, dest="output_dir")
    batch.add_argument("--target", default="韩语")
    batch.add_argument("--source-language", default="auto")
    batch.add_argument("--profile", choices=["source"], default="source")
    batch.add_argument("--translations")
    batch.add_argument("--glossary")
    batch.add_argument("--references")
    batch.add_argument("--request", default="", help="直接传给所选模型的原始要求")
    batch.add_argument("--max-turns", type=int, default=80, help="Seedance 查询等待次数上限，1–200；续跑不会重复提交生成")
    again = commands.add_parser("resume", help="从检查点继续批次，跳过已完成视频")
    again.add_argument("--batch-dir", required=True)
    again.add_argument("--max-turns", type=int)
    subtitles = commands.add_parser("mediakit-convert", help="云端 OCR、精细字幕擦除、服务端韩文合成；需要用户定稿译文 JSON")
    subtitles.add_argument("input_path", help="待处理的本机 MP4 文件")
    subtitles.add_argument("--translations", required=True, help="UTF-8 JSON 数组，每项包含 source 和 target")
    subtitles.add_argument("--output", required=True, dest="output_dir")
    subtitles.add_argument("--max-polls", type=int, default=60, help="本次任务查询次数，1–200；续跑复用任务 ID")
    subtitles.add_argument("--stop-after", choices=["ocr", "erase"], help="识别或擦除完成后保留断点，供分阶段检查")
    subtitles_resume = commands.add_parser("mediakit-resume", help="续跑 MediaKit 独立断点，不重复创建已保存的云端任务")
    subtitles_resume.add_argument("--batch-dir", required=True)
    subtitles_resume.add_argument("--max-polls", type=int, default=60)
    subtitles_resume.add_argument("--stop-after", choices=["ocr", "erase"])
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
                               provider=options["provider"], model_family=options["model_family"],
                               media_endpoint=options["media_base_url"])
        elif command == "setup-mediakit":
            from .mediakit_config import configure as configure_mediakit
            result = configure_mediakit(**options)
        elif command == "mediakit-doctor":
            from .mediakit_config import check_setup as check_mediakit_setup
            result = check_mediakit_setup()
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
        elif command == "mediakit-convert":
            from .mediakit_batch import convert as convert_subtitles
            result = convert_subtitles(**options, progress=progress)
        elif command == "mediakit-resume":
            from .mediakit_batch import resume as resume_subtitles
            result = resume_subtitles(**options, progress=progress)
        elif command == "convert":
            result = convert(**options, progress=progress)
        else:
            raise ValueError("不支持的命令。")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("status") in {"failed", "paused_error", "paused_budget", "paused_checkpoint", "cancelled", "needs_review"} else 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        print(f"Video Localizer: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
