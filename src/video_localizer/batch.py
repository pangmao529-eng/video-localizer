"""Persistent, one-at-a-time Hunyuan video batches shared by CLI and MCP."""

from contextlib import contextmanager
import hashlib
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
from .quality_rules import QUALITY_POLICY_REVISION, QUALITY_RULES


STOP_STATES = {"paused_error", "paused_budget", "cancelled"}
PARAMETER_KEYS = {"target", "source_language", "profile", "translations", "glossary", "references", "request", "max_turns"}
ANALYSIS_FIELDS = ("phase", "read_only_turns", "compactions", "output_recoveries")


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
    paths = runtime_paths()
    dependencies = {key: bool(path and path.is_file()) for key, path in paths.items()}
    for module in ("cv2", "numpy", "PIL"):
        dependencies[module] = importlib.util.find_spec(module) is not None
    data, error, configured = None, None, False
    try:
        data = settings()
        if data:
            configured = bool(read_key(data["base_url"]))
    except SettingsError as exc:
        error = str(exc)
    provider = data.get("provider", "hunyuan") if data else None
    return {"runtime_ready": all(dependencies.values()), "dependencies": dependencies,
            "provider": provider, "video_workflow_ready": bool(configured and provider in {"hunyuan", "seedance"}),
            "automatic_visual_review_available": provider == "hunyuan",
            "model_configured": configured, "model": data["model"] if data else None,
            "model_family": seedance_family(data["model"], data.get("model_family")) if provider == "seedance" else None,
            "endpoint": data["base_url"] if data else None, "configuration_error": error,
            "model_contacted": False,
            "workflow": "seedance_video_api" if provider == "seedance" else "hunyuan_controller",
            "running_version": __version__, "process_id": os.getpid(),
            "runtime_revision": _running_revision(),
            "quality_policy_revision": QUALITY_POLICY_REVISION,
            "quality_policy_sha256": hashlib.sha256(QUALITY_RULES.encode("utf-8")).hexdigest()}


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
    if params["profile"] not in {"source", "2k120"}:
        raise ValueError("输出规格须为 source 或 2k120。")
    for language in (params["target"], params["source_language"]):
        if not isinstance(language, str) or not language.strip() or len(language) > 80:
            raise ValueError("请填写有效语言名称。")
    if not isinstance(params["request"], str) or len(params["request"]) > 32000:
        raise ValueError("原始要求需要是最多 32000 字符的文本。")
    if type(params["max_turns"]) is not int or not 1 <= params["max_turns"] <= 200:
        raise ValueError("max_turns 取值为 1–200，表示每条视频每次运行的模型轮数上限。")
    for key in ("translations", "glossary", "references"):
        if params[key] is not None:
            if not isinstance(params[key], (str, os.PathLike)) or not str(params[key]).strip():
                raise ValueError("资料路径不能为空。")
            params[key] = str(Path(params[key]).expanduser().resolve())
    return params


def _connection():
    data = settings()
    if not data:
        raise SettingsError("请先运行 video-localizer setup --gui 保存模型配置。")
    if data.get("provider", "hunyuan") not in {"hunyuan", "seedance"}:
        raise SettingsError("不支持当前模型提供方；不会自动切换平台。")
    key = read_key(data["base_url"])
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
    if isinstance(data, dict) and data.get("schema") == 3:
        from .seedance_batch import validate_report
        return validate_report(folder, data)
    if not isinstance(data, dict) or data.get("schema") != 2 or data.get("directory") != str(folder):
        raise ValueError("批次格式或目录不匹配；本版只能恢复新版混元批次。")
    if data.get("model") not in {"hy4", "hy4-flash"} or not isinstance(data.get("endpoint"), str):
        raise ValueError("批次混元连接记录无效。")
    _parameters(data.get("params", {}))
    rows = data.get("videos")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
        raise ValueError("批次视频清单无效。")
    for index, row in enumerate(rows, 1):
        expected = folder / "videos" / f"{index:03d}"
        if (not isinstance(row, dict) or row.get("index") != index
                or row.get("work_dir") != str(expected) or expected.resolve() != expected
                or not isinstance(row.get("source"), str) or not Path(row["source"]).is_absolute()
                or row.get("status") not in {"ready", "processing", "not_processed", "failed", "completed", "needs_review", *STOP_STATES}
                or not isinstance(row.get("issues", []), list)):
            raise ValueError("批次工作目录或原片路径无效。")
        for artifact in (row.get("output"), row.get("preview")):
            if artifact:
                try:
                    Path(artifact).resolve().relative_to(expected)
                except (TypeError, ValueError):
                    raise ValueError("批次产物路径超出当前视频工作目录。") from None
    return folder, data


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
    if report.get("provider") == "seedance":
        from .seedance_batch import run
        return run(folder, report, connection, progress, cancelled)
    if connection[0].get("provider", "hunyuan") != "hunyuan":
        raise SettingsError("旧混元断点不能在 Seedance 配置下续跑，请对原片新建 Seedance 批次。")
    from .controller import HunyuanController
    from .executor import VideoExecutor, ExecutorCancelled
    from .hunyuan import HunyuanClient
    from .materials import load_materials
    from .model import ModelError
    data, api_key, options = connection
    if report["model"] != data["model"] or report["endpoint"] != data["base_url"]:
        raise SettingsError("已保存的模型或端点与原批次不同，请恢复原配置后续跑。")
    params = report["params"]
    options["profile"] = params["profile"]
    materials = load_materials(**{key: params[key] for key in ("translations", "glossary", "references")})
    emit, is_cancelled = progress or (lambda event: None), cancelled or (lambda: False)
    started, elapsed = time.monotonic(), float(report.get("elapsed_seconds", 0))
    report["status"] = "processing"
    _persist(folder, report)
    emit({"stage": "batch_started", "batch_dir": str(folder), "total": len(report["videos"])})
    stopped = None
    for row in report["videos"]:
        if row["status"] == "completed":
            continue
        if is_cancelled():
            stopped = "cancelled"
            break
        base = {"index": row["index"], "total": len(report["videos"]),
                "filename": Path(row["source"]).name, "batch_dir": str(folder)}
        row["status"] = "processing"
        _persist(folder, report)
        emit({**base, "stage": "start"})
        try:
            executor = VideoExecutor(row["source"], row["work_dir"], options=options,
                                     materials=materials, cancelled=is_cancelled)
            client = HunyuanClient(data["base_url"], data["model"], api_key)
            controller = HunyuanController(client, executor, row["work_dir"], request=params["request"],
                target=params["target"], source_language=params["source_language"], max_turns=params["max_turns"],
                progress=lambda event: emit({**event, **base}), cancelled=is_cancelled)
            item = controller.run()
            if item.get("status") not in {"completed", "needs_review", *STOP_STATES}:
                raise RuntimeError("控制器返回了未知状态。")
            if item.get("output") and not Path(item["output"]).is_file():
                raise RuntimeError("视频处理报告与磁盘输出不一致。")
            for artifact in (item.get("output"), item.get("preview")):
                if artifact:
                    try:
                        Path(artifact).resolve().relative_to(Path(row["work_dir"]))
                    except (TypeError, ValueError):
                        raise RuntimeError("视频产物路径超出当前工作目录。") from None
            row.update(item)
        except ExecutorCancelled:
            row.update(status="cancelled", output=None, message="任务已取消，可从检查点继续。")
        except ModelError as exc:
            row.update(status="paused_error", output=None, error_code=exc.code, message=str(exc))
        except (ValueError, RuntimeError, OSError) as exc:
            row.update(status="failed", output=None, message=str(exc).replace(api_key, "[redacted]")[:2000])
        row.setdefault("human_approved", False)
        row.setdefault("model_reviewed", False)
        write_json(Path(row["work_dir"]) / "review.json", row)
        report["elapsed_seconds"] = round(elapsed + time.monotonic() - started, 2)
        if row["status"] in STOP_STATES:
            stopped = row["status"]
        _persist(folder, report)
        emit({**base, "stage": "finished_file", "status": row["status"],
              **{key: row.get(key) for key in ("error_code", "message", *ANALYSIS_FIELDS)}})
        if stopped:
            break
    report["status"] = stopped or ("needs_review" if any(row["status"] == "needs_review" for row in report["videos"])
                                   else "failed" if any(row["status"] == "failed" for row in report["videos"])
                                   else "completed")
    report["elapsed_seconds"] = round(elapsed + time.monotonic() - started, 2)
    _persist(folder, report)
    return _summary(report)


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
        provider = connection[0].get("provider", "hunyuan")
        if provider == "seedance":
            from .seedance_batch import prepare
            prepare(params, connection[0]["model"], connection[0].get("model_family"))  # Validate before creating or uploading a batch.
        destination = Path(output_dir).expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix=time.strftime("batch-%Y%m%d-%H%M%S-"), dir=destination))
        rows = [{"index": index, "source": str(path), "status": "not_processed", "output": None,
                 "work_dir": str(folder / "videos" / f"{index:03d}"), "issues": [],
                 "human_approved": False, "model_reviewed": False} for index, path in enumerate(files, 1)]
        report = {"schema": 2, "directory": str(folder), "total": len(files), "model": connection[0]["model"],
                  "endpoint": connection[0]["base_url"], "params": params, "status": "ready", "videos": rows,
                  "target_language": target, "profile": profile, "created_at": time.time(), "elapsed_seconds": 0}
        if provider == "seedance":
            report.update(schema=3, provider="seedance",
                          model_family=seedance_family(connection[0]["model"], connection[0].get("model_family")))
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
    from .controller import record_review
    if type(video_index) is not int or not isinstance(issue_id, str) or not issue_id:
        raise ValueError("请提供视频序号和审核问题 ID。")
    if not isinstance(decision, dict) or set(decision) - {"action", "note", "source_text", "target_text", "start_frame", "end_frame"}:
        raise ValueError("审核决定包含不支持的字段。")
    if (type(decision.get("start_frame")) is int and type(decision.get("end_frame")) is int
            and decision["end_frame"] <= decision["start_frame"]):
        raise ValueError("审核决定的结束帧必须晚于开始帧。")
    with batch_lock():
        folder, report = _load_batch(batch_dir)
        if report.get("provider") == "seedance":
            from .seedance_batch import record_review as record_seedance_review
            return record_seedance_review(folder, report, video_index, issue_id, decision)
        if not 1 <= video_index <= len(report["videos"]):
            raise ValueError("视频序号不在当前批次中。")
        row = report["videos"][video_index - 1]
        result = record_review(row["work_dir"], issue_id, decision)
        state = json.loads((Path(row["work_dir"]) / "controller-state.json").read_text(encoding="utf-8"))
        row.update(status="ready", issues=state["reviews"], human_approved=False, model_reviewed=False, output=None)
        report["status"] = "ready"
        write_json(Path(row["work_dir"]) / "review.json", row)
        _persist(folder, report)
        return {**result, "batch_dir": str(folder), "video_index": video_index, "batch_status": "ready",
                "message": "审核意见已保存；调用 resume_batch 或 resume 让混元继续。", "review": report["review"]}
