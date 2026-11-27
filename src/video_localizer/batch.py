"""Persistent Seedance video batches shared by CLI and MCP."""

from contextlib import contextmanager
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time

from .config import app_dir, read_key, settings, SettingsError, write_json, seedance_family
from . import __version__


STOP_STATES = {"paused_error", "paused_budget", "cancelled"}
PARAMETER_KEYS = {"target", "source_language", "profile", "translations", "glossary", "references", "request", "max_turns"}
ANALYSIS_FIELDS = ("phase",)


def runtime_paths():
    root = app_dir()
    roots = [root]
    active = root / "active.txt"
    if active.is_file():
        revision = active.read_text(encoding="ascii").strip()
        if re.fullmatch(r"[0-9a-f]{32}", revision):
            roots.insert(0, root / "versions" / revision)

    def binary(name):
        override = os.environ.get("VIDEO_LOCALIZER_" + name.upper())
        if override:
            return Path(override)
        for base in roots:
            for directory in ("bin", "tools"):
                candidate = base / directory / (name + ".exe")
                if candidate.is_file():
                    return candidate
        found = shutil.which(name)
        return Path(found) if found else None

    preferred = os.environ.get("VIDEO_LOCALIZER_FONT")
    fonts = [Path(preferred)] if preferred else [
        *(base / "fonts/NotoSansCJKkr-Regular.otf" for base in roots),
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/malgun.ttf",
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc",
    ]
    return {"ffmpeg": binary("ffmpeg"), "ffprobe": binary("ffprobe"),
            "font": next((path for path in fonts if path.is_file()), None)}


def _running_revision():
    # active.txt selects future launches; it must not label an already-running process.
    for directory in Path(__file__).resolve().parents:
        if directory.parent.name == "versions" and re.fullmatch(r"[0-9a-f]{32}", directory.name):
            return directory.name
    return None


def doctor():
    from .seedance_batch import PROMPT_REVISION
    from .selfhost_media import configuration as media_configuration
    paths = runtime_paths()
    dependencies = {key: bool(path and path.is_file()) for key, path in paths.items()}
    for module in ("cv2", "numpy", "PIL"):
        dependencies[module] = importlib.util.find_spec(module) is not None
    data, error, configured = None, None, False
    model_key = None
    media, media_error, media_ready = None, None, False
    try:
        data = settings()
        if data:
            model_key = read_key(data["base_url"])
            configured = data.get("provider") == "seedance" and bool(model_key)
    except SettingsError as exc:
        error = str(exc)
    try:
        media = media_configuration()
        if media:
            from .config import validated_media_endpoint
            validated_media_endpoint(media["endpoint"], data["base_url"] if data else None)
            media_key = read_key(media["endpoint"])
            if media_key.startswith("ark-") or (model_key and hmac.compare_digest(media_key, model_key)):
                raise SettingsError("素材上传凭据须与 Ark API Key 分开。")
            media_ready = bool(media_key)
    except SettingsError as exc:
        media_error = str(exc)
    return {"runtime_ready": all(dependencies.values()), "dependencies": dependencies,
            "provider": "seedance", "video_workflow_ready": bool(configured and media_ready and all(dependencies.values())),
            "automatic_visual_review_available": False,
            "model_configured": configured, "model": data["model"] if data else None,
            "model_family": seedance_family(data["model"], data.get("model_family")) if data and configured else None,
            "endpoint": data["base_url"] if data else None, "configuration_error": error,
            "media_backend": media, "media_upload_configured": media_ready,
            "media_configuration_error": media_error,
            "model_contacted": False, "media_server_contacted": False,
            "workflow": "seedance_video_api", "distribution": "seedance-only",
            "new_seedance20_processing_backend": "server-v1", "server_processing_contacted": False,
            "running_version": __version__, "process_id": os.getpid(),
            "runtime_revision": _running_revision(), "prompt_revision": PROMPT_REVISION}


@contextmanager
def batch_lock():
    """An OS-held byte/file lock: process death releases it without stale PID logic."""
    root = app_dir()
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / "batch.lock").open("a+b")
    locked = False
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise SettingsError("当前 Windows 用户已有视频任务运行，请先等待或取消该任务。") from None
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def expand_inputs(inputs):
    if isinstance(inputs, str) or not isinstance(inputs, (list, tuple)) or not inputs:
        raise ValueError("请选择 MP4 文件或含 MP4 的文件夹。")
    files, seen = [], set()
    for value in inputs:
        if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
            raise ValueError("输入路径不能为空。")
        path = Path(value).expanduser().resolve(strict=True)
        if path.is_dir():
            entries = sorted((item for item in path.iterdir() if item.is_file() and item.suffix.lower() == ".mp4"),
                             key=lambda item: (item.name.casefold(), item.name))
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


def _parameters(params):
    if not isinstance(params, dict) or set(params) != PARAMETER_KEYS:
        raise ValueError("批次参数不完整或包含不支持的字段。")
    if params["profile"] != "source":
        raise ValueError("Seedance 仅支持 source 规格，不支持 2K/120。")
    for language in (params["target"], params["source_language"]):
        if not isinstance(language, str) or not language.strip() or len(language) > 80:
            raise ValueError("请填写有效语言名称。")
    if not isinstance(params["request"], str) or len(params["request"]) > 32000:
        raise ValueError("原始要求需要是最多 32000 字符的文本。")
    if type(params["max_turns"]) is not int or not 1 <= params["max_turns"] <= 200:
        raise ValueError("max_turns 取值为 1–200，表示每条视频本次运行的状态查询次数上限。")
    for key in ("translations", "glossary", "references"):
        if params[key] is not None:
            if not isinstance(params[key], (str, os.PathLike)) or not str(params[key]).strip():
                raise ValueError("资料路径不能为空。")
            params[key] = str(Path(params[key]).expanduser().resolve())
    return params


def _connection():
    from .selfhost_media import configuration as media_configuration
    data = settings()
    if not data or data.get("provider") != "seedance":
        raise SettingsError("请先运行 video-localizer setup --gui 保存 Seedance 配置。")
    media = media_configuration()
    if not media:
        raise SettingsError("请先在配置窗口保存自有素材服务器地址与独立上传凭据。")
    # Check both distinct credential targets before creating a batch; no API call.
    from .config import validated_media_endpoint
    validated_media_endpoint(media["endpoint"], data["base_url"])
    media_key = read_key(media["endpoint"])
    key = read_key(data["base_url"])
    if not key or not media_key or media_key.startswith("ark-") or hmac.compare_digest(media_key, key):
        raise SettingsError("素材上传凭据须与 Ark API Key 分开且均不能为空。")
    paths = runtime_paths()
    missing = [name for name, path in paths.items() if not path or not path.is_file()]
    if missing:
        raise ValueError("缺少运行组件：" + ", ".join(missing) + "。请运行 doctor 检查安装。")
    return data, key, {name: str(path) for name, path in paths.items()}


def _load_batch(batch_dir):
    folder = Path(batch_dir).expanduser().resolve(strict=True)
    path = folder / "batch.json"
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 32 * 1024**2:
        raise ValueError("找不到有效批次记录。")
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and data.get("schema") == 2:
        raise SettingsError("旧混元断点不能在 Seedance 专用版续跑；旧安装版本和断点可独立保留。")
    if not isinstance(data, dict) or data.get("schema") != 3:
        raise ValueError("本版只接受 Seedance schema 3 批次。")
    from .seedance_batch import validate_report
    return validate_report(folder, data)


def _persist(folder, report):
    from .review import write_review
    rows = report["videos"]
    report.update(outputs=[row["output"] for row in rows if row.get("output") and row["status"] == "completed"],
                  completed=sum(row["status"] == "completed" for row in rows),
                  failed=sum(row["status"] == "failed" for row in rows),
                  unprocessed=sum(row["status"] == "not_processed" for row in rows),
                  needs_review=sum(row["status"] == "needs_review" for row in rows),
                  pending_reviews=sum(issue.get("status") == "pending" for row in rows for issue in row.get("issues", [])),
                  report=str(folder / "batch.json"), updated_at=time.time(),
                  human_approved=bool(report.get("provider") == "seedance" and all(row.get("human_approved") and row["status"] == "completed" for row in rows)))
    if report.get("provider") == "seedance":
        report["candidates"] = [row["preview"] for row in rows if row.get("preview")]
    report["review"] = write_review(folder, rows)
    write_json(folder / "batch.json", report)


def _summary(report):
    result = {key: value for key, value in report.items() if key not in {"videos", "params", "endpoint"}}
    result["videos"] = [{key: row.get(key) for key in ("index", "source", "status", "work_dir", "output", "preview", "issues", "model_reviewed", "human_approved", "summary", "coverage", "message", "error_code", "task_id", "remote_status", "remote_error_code", "last_transport_error", "technical_review", "segment_tasks", "child_batch_dir", *ANALYSIS_FIELDS)}
                        for row in report["videos"]]
    return result


def _run(folder, report, connection, progress, cancelled):
    if report.get("provider") != "seedance" or connection[0].get("provider") != "seedance":
        raise SettingsError("本版仅运行 Seedance 批次，不调用旧模型提供方。")
    from .seedance_batch import run
    return run(folder, report, connection, progress, cancelled)


def convert(inputs, output_dir, *, target="韩语", source_language="auto", profile="source",
            translations=None, glossary=None, references=None, request="", max_turns=80,
            progress=None, cancelled=None):
    params = _parameters(dict(target=target, source_language=source_language, profile=profile,
        translations=translations, glossary=glossary, references=references, request=request, max_turns=max_turns))
    if not isinstance(output_dir, (str, os.PathLike)) or not str(output_dir).strip():
        raise ValueError("请指定成片输出目录。")
    files = expand_inputs(inputs)
    with batch_lock():
        connection = _connection()
        from .seedance_batch import prepare
        from .selfhost_media import binding
        prepare(params, connection[0]["model"], connection[0].get("model_family"))
        media_backend = binding()
        destination = Path(output_dir).expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix=time.strftime("batch-%Y%m%d-%H%M%S-"), dir=destination))
        rows = [{"index": index, "source": str(path), "status": "not_processed", "output": None,
                 "work_dir": str(folder / "videos" / f"{index:03d}"), "issues": [],
                 "human_approved": False, "model_reviewed": False} for index, path in enumerate(files, 1)]
        report = {"schema": 3, "provider": "seedance", "directory": str(folder), "total": len(files),
                  "model": connection[0]["model"], "endpoint": connection[0]["base_url"],
                  "model_family": seedance_family(connection[0]["model"], connection[0].get("model_family")),
                  "media_backend": media_backend, "params": params, "status": "ready", "videos": rows,
                  "target_language": target, "profile": profile, "created_at": time.time(), "elapsed_seconds": 0}
        if report["model_family"] == "seedance-2.0" and media_backend.get("provider") == "self_hosted":
            report["processing_backend"] = "server-v1"
        _persist(folder, report)
        return _run(folder, report, connection, progress, cancelled)


def resume(batch_dir, *, max_turns=None, progress=None, cancelled=None):
    with batch_lock():
        folder, report = _load_batch(batch_dir)
        if max_turns is not None:
            report["params"]["max_turns"] = max_turns
            _parameters(report["params"])
        return _run(folder, report, _connection(), progress, cancelled)


def submit_review(batch_dir, video_index, issue_id, decision):
    if type(video_index) is not int or not isinstance(issue_id, str) or not issue_id:
        raise ValueError("请提供视频序号和审核问题 ID。")
    if not isinstance(decision, dict) or set(decision) - {"action", "note"}:
        raise ValueError("Seedance 成片审核只接受 keep/reject 及备注。")
    with batch_lock():
        folder, report = _load_batch(batch_dir)
        from .seedance_batch import record_review
        return record_review(folder, report, video_index, issue_id, decision)
