"""Resumable remote video edits. Local work is transport and technical validation only."""

import base64
import json
from pathlib import Path
import time

from . import _engine as engine
from .config import SettingsError, write_json, SEEDANCE_25_MODEL, SEEDANCE_20_FAMILY, seedance_family
from .materials import load_materials


PROMPT_REVISION = "seedance-localization-v1"
POLL_SECONDS = 10
REVIEW_ID = "seedance-full-video-review"


def validate_report(folder, report):
    from .batch import _parameters, STOP_STATES
    if (report.get("schema") != 3 or report.get("provider") != "seedance"
            or report.get("directory") != str(folder)
            or not isinstance(report.get("endpoint"), str)):
        raise ValueError("Seedance 批次连接记录无效。")
    seedance_family(report.get("model"), report.get("model_family"))
    if report.get("processing_backend") not in {None, "server-v1"}:
        raise ValueError("Seedance 批次的媒体计算位置无效。")
    _parameters(report.get("params", {}))
    rows = report.get("videos")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
        raise ValueError("Seedance 批次视频清单无效。")
    for index, row in enumerate(rows, 1):
        expected = folder / "videos" / f"{index:03d}"
        if (not isinstance(row, dict) or row.get("index") != index
                or row.get("work_dir") != str(expected) or expected.resolve() != expected
                or not isinstance(row.get("source"), str) or not Path(row["source"]).is_absolute()
                or row.get("status") not in {"ready", "processing", "not_processed", "failed", "completed", "needs_review", *STOP_STATES}
                or not isinstance(row.get("issues", []), list)):
            raise ValueError("Seedance 批次工作目录或原片路径无效。")
        for artifact in (row.get("output"), row.get("preview")):
            if artifact and Path(artifact).resolve() != (expected / "seedance-candidate.mp4"):
                raise ValueError("Seedance 批次产物路径无效。")
    return folder, report


def prepare(params, model=SEEDANCE_25_MODEL, model_family=None):
    family = seedance_family(model, model_family)
    if params["profile"] != "source":
        raise ValueError("Seedance 编辑仅支持 source 规格；不支持承诺 2K/120 帧输出。")
    materials = load_materials(**{key: params[key] for key in ("translations", "glossary", "references")})
    tables = {}
    for kind in ("translations", "glossary"):
        # Keep exact user table values, including conflicts; the host never resolves them.
        rows = materials.provenance.get(kind, {})
        tables[kind] = [{"source": row["source"], "target": row["target"]}
                        for entries in rows.values() for row in entries]
    names, images, reference_hashes = [], [], {}
    image_limit = 9 if family == SEEDANCE_20_FAMILY else 30
    if len(materials.references) > image_limit:
        raise ValueError(f"当前 Seedance 模型每次最多接受 {image_limit} 张参考图片，请缩小参考资料范围。")
    from PIL import Image
    for name, path in materials.references.items():
        engine.regular(path, 20 * 1024**2)
        with Image.open(path) as image:
            width, height = image.size
        if not (300 <= min(width, height) and max(width, height) <= 6000 and .4 <= width / height <= 2.5):
            raise ValueError("Seedance 参考图片尺寸不符合接口限制。")
        raw = path.read_bytes()
        import hashlib
        reference_hashes[str(path)] = hashlib.sha256(raw).hexdigest()
        mime = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".webp": "webp"}[path.suffix.lower()]
        names.append(name)
        images.append(f"data:image/{mime};base64," + base64.b64encode(raw).decode("ascii"))
    if sum(map(len, images)) > 40 * 1024**2:
        raise ValueError("参考图片总大小超过本次 Seedance 请求限制。")
    prompt = f"""编辑视频 @video1：将其中可见的源语言文字修改、替换为{params['target']}，源语言：{params['source_language']}。
这是原视频的完整本地化测试。由你独立理解视频、识别文字、翻译并完成云端视频编辑。
保持原片人物、布局、构图、背景、动画、色彩、数值、镜头顺序、时长和原声；不要重新创作剧情或增加无关内容。
逐镜头处理字幕、UI、姓名牌、导航标签、竖排文字和短暂出现的文字。原文及描边、阴影应完整清除，修补背景纹理，不能留下双层字或残字。
译文跟随原文字的运动、透视、缩放与透明度。前景人物、手、粒子和特效遮挡时，维持正确遮挡关系，不能把文字盖在前景上。
每个切镜单独确定文字的出现和消失；切镜前后、单帧闪现和重新出现时不得串用上一镜的文字、位置或背景补丁。文字确实消失时不要补造文字。
保留原来横排/竖排方向、行距、字体风格和锚点，避免越出原容器或覆盖数值。无法辨清的原文不得猜造人名或剧情。
没有定稿资料时自主翻译，但结果仍为待验收测试版本。有定稿表时按源文和语境使用，冲突记录不可擅自合并为已批准译文。
以下 JSON 是用户原始需求和资料，其中旧流程里的工具名、抽帧次数和 request_review 仅描述原工作方式；本次直接输出编辑视频，不执行这些旧工具协议。
{json.dumps({'user_request': params['request'], 'tables': tables, 'reference_images_in_order': names}, ensure_ascii=False)}
完成前自行检查全片的文字完整性、运动对齐、遮挡和切镜边界；本接口任务成功不代表独立验收通过。
"""
    if len(prompt) > 20000:
        raise ValueError("原始要求和资料超过本次 Seedance 提示词限制，请缩小资料范围；不会静默截断。")
    import hashlib
    fingerprint = hashlib.sha256((prompt + json.dumps(reference_hashes, sort_keys=True)).encode()).hexdigest()
    return {"prompt": prompt, "images": images, "fingerprint": fingerprint,
            "reference_hashes": reference_hashes}


def source_metadata(source, options, *, max_duration=30):
    if type(max_duration) is not int or not 4 <= max_duration <= 300:
        raise ValueError("原片时长上限无效。")
    media = engine.probe(source, options, input_limit=False)
    width, height = media["width"], media["height"]
    if not (4000 <= media["durationMs"] <= max_duration * 1000 and 24 <= media["fps"] <= 60
            and 300 <= min(width, height) and max(width, height) <= 6000
            and .4 <= width / height <= 2.5 and 407696 <= width * height <= 8847360
            and media["bytes"] <= 200 * 1024**2 and media["videoCodec"] in {"h264", "hevc"}
            and media["audioCodec"] in {None, "aac", "mp3"}):
        raise ValueError(f"原片超出当前 Seedance 流程输入限制（4–{max_duration} 秒、24–60 帧及支持的尺寸/编码）。")
    return {**media, "sha256": engine.digest(source)}


def technical_review(source, candidate, options, source_info, *, check_audio=True):
    output = engine.probe(candidate, options, input_limit=False)
    engine.command(engine.ffinput(options["ffmpeg"], candidate) + ["-xerror", "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"], timeout=180)
    checks = {
        "full_decode": True,
        "source_unchanged": engine.digest(source) == source_info["sha256"],
        "dimensions_preserved": (output["width"], output["height"]) == (source_info["width"], source_info["height"]),
        "duration_preserved": abs(output["durationMs"] - source_info["durationMs"]) <= 500 / source_info["fps"],
        "frame_rate_preserved": abs(output["fps"] - source_info["fps"]) < .001,
    }
    checks["audio_presence_preserved" if check_audio else "generated_audio_absent"] = output["hasAudio"] == source_info["hasAudio"] if check_audio else not output["hasAudio"]
    if check_audio and source_info["hasAudio"]:
        # Check codec, packet bytes AND the audio/video offset, without remuxing.
        try:
            engine.verify_full_audio(source, candidate, source_info, output, options)
            checks["original_audio_preserved"] = True
        except engine.BridgeError:
            checks["original_audio_preserved"] = False
    return {"status": "passed" if all(checks.values()) else "failed", "checks": checks,
            "output_media": output, "output_sha256": engine.digest(candidate),
            "visual_acceptance": "not_performed", "model_reviewed": False,
            "note": "仅校验文件与源片技术属性；未验证译文、残字、运动、遮挡或切镜。"}


def _wait(cancelled):
    until = time.monotonic() + POLL_SECONDS
    while time.monotonic() < until:
        if cancelled():
            return
        time.sleep(min(.25, max(0, until - time.monotonic())))


def run(folder, report, connection, progress=None, cancelled=None):
    if report.get("processing_backend") == "server-v1":
        from .seedance_server_batch import run as run_server
        return run_server(folder, report, connection, progress, cancelled)
    if (seedance_family(report.get("model"), report.get("model_family")) == SEEDANCE_20_FAMILY
            and not report.get("segment_worker")):
        from .seedance_segment_batch import run as run_segmented
        return run_segmented(folder, report, connection, progress, cancelled)
    return _run_single(folder, report, connection, progress, cancelled)


def _run_single(folder, report, connection, progress=None, cancelled=None):
    from .batch import _persist, _summary
    from .seedance_client import SeedanceClient, SeedanceError
    data, key, options = connection
    if (data.get("provider") != "seedance" or report["provider"] != "seedance"
            or report["model"] != data["model"] or report["endpoint"] != data["base_url"]
            or seedance_family(report["model"], report.get("model_family")) != seedance_family(data["model"], data.get("model_family"))):
        raise SettingsError("已保存的提供方、模型、系列或端点与 Seedance 原批次不同，不能混用断点。")
    family = seedance_family(data["model"], data.get("model_family"))
    from .selfhost_media import bind_report, MediaClient
    media_backend = bind_report(report)
    media_client = MediaClient(media_backend["endpoint"], cancelled=cancelled) if media_backend["provider"] == "self_hosted" else None
    prepared = prepare(report["params"], data["model"], family)
    if report.get("materials_fingerprint") not in {None, prepared["fingerprint"]}:
        raise ValueError("提示词或资料已变化，原 Seedance 批次不能继续；请创建新批次。")
    report["materials_fingerprint"] = prepared["fingerprint"]
    report["prompt_revision"] = PROMPT_REVISION
    emit, cancelled = progress or (lambda _: None), cancelled or (lambda: False)
    client = SeedanceClient(data["base_url"], key, data["model"], model_family=family, cancelled=cancelled)
    started, elapsed = time.monotonic(), report.get("elapsed_seconds", 0)
    report["status"] = "processing"
    _persist(folder, report)
    emit({"stage": "batch_started", "provider": "seedance", "batch_dir": str(folder), "total": len(report["videos"])})
    stopped = None
    for row in report["videos"]:
        if row["status"] in {"completed", "needs_review"} or row.get("human_rejected") is True:
            continue
        if cancelled():
            stopped = "cancelled"
            break
        work, source = Path(row["work_dir"]), Path(row["source"])
        work.mkdir(parents=True, exist_ok=True)
        base = {"index": row["index"], "total": len(report["videos"]), "batch_dir": str(folder), "provider": "seedance"}

        def save(phase):
            row["phase"] = phase
            report["elapsed_seconds"] = round(elapsed + time.monotonic() - started, 2)
            _persist(folder, report)
            emit({**base, "stage": phase, "task_id": row.get("task_id"), "remote_status": row.get("remote_status")})

        row.update(status="processing", message=None, error_code=None)
        save("validate_source")
        try:
            info = source_metadata(source, options)
            if family == SEEDANCE_20_FAMILY and (info["durationMs"] > 15000.1 or info["width"] * info["height"] > 8295044):
                raise ValueError("Seedance 2.0 单次输入超过 15 秒或像素上限，未提交生成。")
            if row.get("source_sha256") not in {None, info["sha256"]}:
                raise ValueError("原视频已变化，拒绝使用旧上传或生成断点。")
            row.update(source_sha256=info["sha256"], source_media=info)
            (work / "seedance-prompt.txt").write_text(prepared["prompt"], encoding="utf-8")
            polls = report["params"]["max_turns"]
            if not row.get("task_id"):
                if row.get("submission_state") == "submitting":
                    raise SeedanceError("submission_uncertain", "上次生成提交结果未知；为避免重复计费不会自动重发。请先在平台核对任务。", submission_uncertain=True)
                if media_client:
                    # The content hash identifies this idempotent upload even if
                    # the server saved it but its previous response was lost.
                    row["media_object_id"] = info["sha256"]
                    save("self_hosted_upload")
                    receipt = media_client.ensure_uploaded(source, info["sha256"])
                    row.update(media_upload=receipt, upload_state="uploaded")
                    save("self_hosted_uploaded")
                    video_reference = media_client.reference(info["sha256"], info["bytes"])
                elif not row.get("file_id"):
                    if row.get("upload_state") == "uploading":
                        raise SeedanceError("upload_uncertain", "上次文件上传结果未知，请先在平台核对文件；不会自动重复上传。")
                    row["upload_state"] = "uploading"
                    save("uploading")
                    try:
                        uploaded = client.upload_video(source)
                    except SeedanceError as error:
                        if not error.submission_uncertain:
                            row["upload_state"] = "rejected"
                        raise
                    row.update(file_id=uploaded["file_id"], upload_state="uploaded")
                    save("uploaded")
                if not media_client:
                    uploaded = client.get_file(row["file_id"])
                while not media_client and uploaded.get("status") == "processing":
                    if cancelled():
                        raise SeedanceError("cancelled", "本机等待已取消，远端已有任务可能仍继续。")
                    if polls <= 0:
                        raise SeedanceError("poll_budget", "文件仍在准备，断点已保存；续跑只查询既有文件。")
                    polls -= 1
                    save("waiting_for_upload")
                    _wait(cancelled)
                    uploaded = client.get_file(row["file_id"])
                if not media_client and uploaded.get("status") != "active":
                    row["remote_error_code"] = uploaded.get("error_code")
                    raise SeedanceError("upload_failed", "方舟文件未就绪，未提交视频编辑任务。")
                if not media_client and not uploaded.get("video_reference"):
                    raise SeedanceError("file_reference_unavailable", "方舟文件已就绪但没有返回临时下载地址，无法作为 Seedance 视频参考；未提交生成。")
                if not media_client:
                    video_reference = uploaded["video_reference"]
                if engine.digest(source) != info["sha256"]:
                    raise ValueError("上传期间原片发生变化，未提交生成。")
                if cancelled():
                    raise SeedanceError("cancelled", "已取消，上传断点保留。")
                row["submission_state"] = "submitting"
                save("submitting")
                try:
                    resolution = "480p" if min(info["width"], info["height"]) <= 480 else "720p" if min(info["width"], info["height"]) <= 720 else "1080p"
                    task_id = client.create_edit(video_reference, prepared["prompt"],
                        reference_images=prepared["images"], resolution=resolution,
                        generate_audio=not report.get("segment_worker", False))
                except SeedanceError as error:
                    if not error.submission_uncertain:
                        row["submission_state"] = "rejected"
                    raise
                row.update(task_id=task_id, submission_state="submitted", remote_status="queued")
                save("submitted")
            candidate = work / "seedance-candidate.mp4"
            # A complete download can be revalidated without another API request.
            if not (row.get("download_sha256") and candidate.is_file()
                    and engine.digest(candidate) == row["download_sha256"]):
                if candidate.exists():
                    raise ValueError("候选文件已存在但下载校验记录不匹配；请先核对文件，不会覆盖或当作合格产物。")
                while True:
                    if cancelled():
                        raise SeedanceError("cancelled", "本机等待已取消；远端已提交任务可能继续计费，续跑将查询原任务。")
                    task = client.get_task(row["task_id"])
                    row["remote_status"] = task["status"]
                    save("remote_" + task["status"])
                    if task["status"] == "succeeded":
                        break
                    if task["status"] in {"failed", "cancelled", "expired"}:
                        row["remote_error_code"] = task.get("error_code")
                        raise SeedanceError("remote_task_failed", task.get("message") or "Seedance 远端编辑任务失败，未生成候选成片。")
                    if polls <= 0:
                        raise SeedanceError("poll_budget", "远端任务仍在运行，断点已保存；续跑只查询同一任务，不重复计费提交。")
                    polls -= 1
                    _wait(cancelled)
                if not task.get("video_url"):
                    raise SeedanceError("invalid_response", "远端报告成功但没有返回可下载的视频。")
                save("downloading")
                client.download(task["video_url"], candidate)
                row["download_sha256"] = engine.digest(candidate)
                save("downloaded")
            save("technical_review")
            review = technical_review(source, candidate, options, info, check_audio=False) if report.get("segment_worker") else technical_review(source, candidate, options, info)
            write_json(work / "technical-review.json", review)
            row.update(status="needs_review", output=None, preview=str(candidate), technical_review=review,
                       model_reviewed=False, human_approved=False, phase="awaiting_visual_review",
                       issues=[{"id": REVIEW_ID, "status": "pending", "reason": "Seedance 已返回编辑视频；需要检查译文、残字、运动对齐、遮挡、切镜和原声。",
                                "evidence": {"technical_status": review["status"], "checks": review["checks"]}}],
                       message="已生成候选视频，尚未完成独立视觉验收。" if review["status"] == "passed" else "已生成候选视频，但源片技术属性检查未全部通过，且尚未完成视觉验收。")
        except SeedanceError as error:
            status = "cancelled" if error.code == "cancelled" else "paused_budget" if error.code == "poll_budget" else "failed" if error.code == "remote_task_failed" else "paused_error"
            row.update(status=status, error_code=error.code, message=str(error))
            diagnostics = {name: getattr(error, name, None) for name in
                           ("failure_stage", "exception_type", "completed_body_bytes", "request_id")}
            diagnostics = {name: value for name, value in diagnostics.items() if value is not None}
            if diagnostics or error.http_status is not None:
                # Keep the last network failure across a blocked resume. A later
                # upload_uncertain guard has no new transport evidence.
                row["last_transport_error"] = dict(diagnostics, error_code=error.code,
                    operation=row.get("phase"), http_status=error.http_status,
                    submission_uncertain=error.submission_uncertain)
            if error.http_status is not None:
                row["http_status"] = error.http_status
            if error.provider_code:
                row["remote_error_code"] = error.provider_code
            if status != "failed":
                stopped = status
        except (ValueError, OSError, engine.BridgeError) as error:
            row.update(status="failed", error_code="local_validation", message=str(error).replace(key, "[redacted]")[:1500])
        write_json(work / "review.json", row)
        save(row.get("phase", "finished_file"))
        if stopped:
            break
    report["status"] = stopped or ("needs_review" if any(row["status"] == "needs_review" for row in report["videos"])
                                   else "failed" if any(row["status"] == "failed" for row in report["videos"]) else "completed")
    report["elapsed_seconds"] = round(elapsed + time.monotonic() - started, 2)
    _persist(folder, report)
    return _summary(report)


def record_review(folder, report, video_index, issue_id, decision):
    from .batch import _persist
    if not 1 <= video_index <= len(report["videos"]) or issue_id != REVIEW_ID:
        raise ValueError("Seedance 审核项不存在。")
    row = report["videos"][video_index - 1]
    if row["status"] != "needs_review" or not row.get("preview"):
        raise ValueError("当前视频没有可验收的候选文件。")
    if set(decision) - {"action", "note"} or decision.get("action") not in {"keep", "reject"}:
        raise ValueError("Seedance 整片审核仅支持 keep 或 reject；修改需求请创建新的明确编辑任务。")
    if not isinstance(decision.get("note", ""), str) or len(decision.get("note", "")) > 4000:
        raise ValueError("审核备注无效。")
    if engine.digest(Path(row["preview"])) != row.get("download_sha256"):
        raise ValueError("候选视频已变化，请重新核查后再验收。")
    if engine.digest(Path(row["source"])) != row.get("source_sha256"):
        raise ValueError("原视频已变化，不能使用旧的技术检查记录验收。")
    accepted = decision["action"] == "keep"
    if accepted and row.get("technical_review", {}).get("status") != "passed":
        raise ValueError("源片技术属性尚未全部通过，不能记为完整验收通过。")
    row["issues"][0].update(status="resolved", decision=decision)
    row.update(status="completed" if accepted else "failed", human_approved=accepted, human_rejected=not accepted,
               output=row["preview"] if accepted else None,
               message="用户已确认整片验收。" if accepted else "用户已拒绝当前候选视频。")
    report["status"] = "needs_review" if any(r["status"] == "needs_review" for r in report["videos"]) else "completed" if all(r["status"] == "completed" for r in report["videos"]) else "failed"
    write_json(Path(row["work_dir"]) / "review.json", row)
    _persist(folder, report)
    return {"status": "resolved", "batch_dir": str(folder), "video_index": video_index, "batch_status": report["status"], "review": report["review"]}
