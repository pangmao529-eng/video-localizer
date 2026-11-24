"""Run one local batch and leave reviewable videos and UTF-8 reports together."""

import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
import time

from .config import app_dir, read_key, settings, SettingsError, write_json


def runtime_paths():
    root = app_dir()
    def binary(name):
        override = os.environ.get("VIDEO_LOCALIZER_" + name.upper())
        if override:
            return Path(override)
        for candidate in (root / "bin" / (name + ".exe"), root / "tools" / (name + ".exe")):
            if candidate.is_file():
                return candidate
        found = shutil.which(name)
        return Path(found) if found else None

    preferred = os.environ.get("VIDEO_LOCALIZER_FONT")
    fonts = [Path(preferred)] if preferred else [
        root / "fonts/NotoSansCJKkr-Regular.otf",
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/malgun.ttf",
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc",
    ]
    return {"ffmpeg": binary("ffmpeg"), "ffprobe": binary("ffprobe"),
            "font": next((p for p in fonts if p.is_file()), None)}


def doctor():
    paths = runtime_paths()
    dependencies = {k: bool(p and p.is_file()) for k, p in paths.items()}
    for module in ("cv2", "numpy", "PIL"):
        dependencies[module] = importlib.util.find_spec(module) is not None
    data = settings()
    configured = False
    if data:
        try:
            configured = bool(read_key(data["base_url"]))
        except SettingsError:
            pass
    return {"runtime_ready": all(dependencies.values()), "dependencies": dependencies,
            "model_configured": configured, "model": data["model"] if data else None}


def expand_inputs(inputs):
    if isinstance(inputs, str) or not isinstance(inputs, (list, tuple)) or not inputs:
        raise ValueError("请选择 MP4 文件或含 MP4 的文件夹。")
    files = []
    seen = set()
    for value in inputs:
        if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
            raise ValueError("输入路径不能为空。")
        path = Path(value).expanduser().resolve(strict=True)
        if path.is_dir():
            entries = sorted((x for x in path.iterdir() if x.is_file() and x.suffix.lower() == ".mp4"),
                             key=lambda p: (p.name.casefold(), p.name))
        elif path.is_file() and path.suffix.lower() == ".mp4":
            entries = [path]
        else:
            raise ValueError(f"输入需要是 MP4 或文件夹：{path}")
        for item in entries:
            key = os.path.normcase(str(item.resolve()))
            if key not in seen:
                files.append(item)
                seen.add(key)
    if not 1 <= len(files) <= 20:
        raise ValueError(f"本批找到 {len(files)} 条 MP4。每次请选择 1–20 条。")
    return files


def convert(inputs, output_dir, *, target="韩语", source_language="auto", profile="source",
            translations=None, glossary=None, references=None, progress=None):
    if profile not in {"source", "2k120"}:
        raise ValueError("输出规格须为 source 或 2k120。")
    for language in (target, source_language):
        if not isinstance(language, str) or not language.strip() or len(language) > 80:
            raise ValueError("请填写有效语言名称。")
    if not isinstance(output_dir, (str, os.PathLike)) or not str(output_dir).strip():
        raise ValueError("请指定成片输出目录。")
    files = expand_inputs(inputs)
    data = settings()
    if not data:
        raise SettingsError("请先运行 video-localizer setup --gui 配置视觉模型。")
    api_key = read_key(data["base_url"])
    paths = runtime_paths()
    missing = [name for name, path in paths.items() if not path or not path.is_file()]
    if missing:
        raise ValueError("缺少运行组件：" + ", ".join(missing) + "。请检查安装包或运行 doctor。")
    from .materials import load_materials
    from .model import ChatVisionProvider, ModelError
    from .media import run_video
    from ._engine import BridgeError
    from .review import write_review

    materials = load_materials(translations=translations, glossary=glossary, references=references)
    provider = ChatVisionProvider(data["base_url"], data["model"], api_key,
                                  source_language=source_language, target_language=target, materials=materials)
    options = {k: str(p) for k, p in paths.items()}
    options["profile"] = profile
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    batch_dir = Path(tempfile.mkdtemp(prefix=time.strftime("batch-%Y%m%d-%H%M%S-"), dir=destination))
    entries = []
    started = time.monotonic()
    report = {"directory": str(batch_dir), "total": len(files), "model": data["model"],
              "target_language": target, "profile": profile, "videos": entries}
    emit = progress or (lambda event: None)
    for index, path in enumerate(files, 1):
        target_file = batch_dir / f"{index:02d}-{path.stem[:95]}.mp4"
        base = {"index": index, "total": len(files), "filename": path.name}
        emit({**base, "stage": "start"})
        halt = False
        try:
            item = run_video(path, target_file, provider, options, lambda event: emit({**event, **base}))
            if item.get("output") and not target_file.is_file():
                raise RuntimeError("视频处理报告与磁盘输出不一致。")
        except ModelError as exc:
            halt = exc.code in {"auth", "billing", "configuration", "rate_limit", "transient"}
            item = {"source": str(path), "status": "failed", "output": None,
                    "error_code": exc.code, "message": str(exc)}
        except (ValueError, RuntimeError, OSError, BridgeError) as exc:
            item = {"source": str(path), "status": "failed", "output": None, "message": str(exc)}
        entries.append(item)
        write_json(target_file.with_suffix(".review.json"), item)
        if halt:
            entries.extend({"source": str(p), "status": "not_processed", "output": None,
                            "reason": item["error_code"]} for p in files[index:])
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        write_json(batch_dir / "batch.json", report)
        emit({**base, "stage": "finished_file", "status": item["status"]})
        if halt:
            break
    report["outputs"] = [row["output"] for row in entries if row.get("output")]
    report["failed"] = sum(row["status"] == "failed" for row in entries)
    report["unprocessed"] = sum(row["status"] == "not_processed" for row in entries)
    report["needs_review"] = sum(row["status"] == "needs_review" for row in entries)
    report["report"] = str(batch_dir / "batch.json")
    report["review"] = write_review(batch_dir, entries)
    write_json(batch_dir / "batch.json", report)
    return {key: value for key, value in report.items() if key != "videos"}
