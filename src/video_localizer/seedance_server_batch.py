"""Server cuts/imports/assembly; the desktop coordinates Seedance and downloads only the final video."""

from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

from . import _engine as engine
from .config import SettingsError, SEEDANCE_20_FAMILY, seedance_family, write_json
from . import seedance_batch as single
from .seedance_client import SeedanceClient, SeedanceError
from .selfhost_media import MediaClient, bind_report


POLICY = "fixed-15s-v1"
BACKEND = "server-v1"


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _transport_error(error, operation, key):
    """Retain bounded transport evidence, never response prose, URLs or credentials."""
    diagnostics = {name: getattr(error, name, None) for name in
                   ("failure_stage", "exception_type", "completed_body_bytes", "request_id")}
    diagnostics = {name: value for name, value in diagnostics.items()
                   if value is not None and (not isinstance(value, str) or key not in value)}
    http_status = error.http_status if type(error.http_status) is int and 100 <= error.http_status <= 599 else None
    if not diagnostics and http_status is None:
        return None
    diagnostics.update(error_code=error.code, operation=operation, http_status=http_status,
                       submission_uncertain=error.submission_uncertain)
    provider_code = error.provider_code
    if (isinstance(provider_code, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{0,127}", provider_code)
            and key not in provider_code):
        diagnostics["provider_code"] = provider_code
    return diagnostics


def _split_binding(result, info):
    """Bind server frame ranges to the locally hashed original before paying for any edits."""
    _require(isinstance(result, dict) and result.get("source_id") == info["sha256"]
             and result.get("policy") == POLICY, "服务器分段与当前原片或策略不匹配。")
    media = result.get("source_media", {})
    _require(all(media.get(k) == info.get(k) for k in
                 ("width", "height", "bytes", "hasAudio", "videoCodec", "audioCodec", "sha256")),
             "服务器原片属性与本机原片不匹配。")
    try:
        fps = Fraction(info["fpsRatio"])
        server_fps = Fraction(media.get("fpsRatio", "0"))
        _require(fps > 0 and server_fps > 0, "服务器原片帧率无效。")
    except (ValueError, TypeError, ZeroDivisionError):
        raise ValueError("服务器原片帧率比例无效。") from None
    _require(server_fps == fps and abs(media.get("fps", 0) - info["fps"]) < .001,
             "服务器原片帧率不匹配。")
    parts, frame_count = result.get("parts"), media.get("frame_count")
    _require(isinstance(parts, list) and 1 <= len(parts) <= 20 and type(frame_count) is int and frame_count > 0,
             "服务器分段数量或帧数无效。")
    _require(abs(frame_count * 1000 / float(fps) - info["durationMs"]) <= 500 / float(fps),
             "服务器原片帧数与本机时长不匹配。")
    boundary = 0
    for index, part in enumerate(parts, 1):
        _require(isinstance(part, dict) and part.get("index") == index
                 and type(part.get("start_frame")) is int and type(part.get("end_frame")) is int
                 and part["start_frame"] == boundary and part["end_frame"] > boundary
                 and part.get("frame_count") == part["end_frame"] - boundary,
                 "服务器分段范围重叠、遗漏或顺序无效。")
        duration = part["frame_count"] / float(fps)
        _require(4 <= duration <= 15 and abs(part.get("duration_seconds", -1) - duration) < .00001
                 and abs(part.get("start_seconds", -1) - boundary / float(fps)) < .00001,
                 "服务器分段时长或起点无效。")
        MediaClient._receipt(part)
        boundary = part["end_frame"]
    _require(boundary == frame_count, "服务器分段没有覆盖完整原片。")
    return parts


def _segment_rows(row, parts):
    expected = [{"index": part["index"], "source_object_id": part["id"],
                 "source_bytes": part["bytes"], "start_frame": part["start_frame"],
                 "end_frame": part["end_frame"], "duration_seconds": part["duration_seconds"]}
                for part in parts]
    saved = row.get("segment_tasks")
    if saved is None:
        row["segment_tasks"] = [dict(part, status="not_processed") for part in expected]
    else:
        _require(isinstance(saved, list) and len(saved) == len(expected), "原有分段任务清单数量不匹配。")
        task_ids = []
        for original, current in zip(expected, saved):
            _require(isinstance(current, dict) and all(current.get(k) == v for k, v in original.items()),
                     "服务器分段已变化，拒绝混用已有生成任务。")
            if current.get("task_id"):
                task_ids.append(current["task_id"])
        _require(len(task_ids) == len(set(task_ids)), "分段任务 ID 重复，拒绝混用候选。")
    return row["segment_tasks"]


def run(folder, report, connection, progress=None, cancelled=None):
    from .batch import _persist, _summary, STOP_STATES
    data, key, options = connection
    family = seedance_family(data["model"], data.get("model_family"))
    if (data.get("provider") != "seedance" or report.get("provider") != "seedance"
            or report.get("processing_backend") != BACKEND or family != SEEDANCE_20_FAMILY
            or report.get("model") != data["model"] or report.get("endpoint") != data["base_url"]
            or seedance_family(report.get("model"), report.get("model_family")) != family):
        raise SettingsError("服务器处理批次与当前提供方、模型、系列或端点不匹配。")
    media_backend = bind_report(report)
    if media_backend.get("provider") != "self_hosted":
        raise SettingsError("服务器切分和拼接需要自有素材服务器，不会回退本机处理。")
    prepared = single.prepare(report["params"], data["model"], family)
    if report.get("materials_fingerprint") not in {None, prepared["fingerprint"]}:
        raise ValueError("要求或资料已变化，不能续用服务器生成断点。")
    if key in prepared["prompt"]:
        raise SettingsError("任务要求中含模型凭据，未保存或发送请求。")
    report.update(materials_fingerprint=prepared["fingerprint"], prompt_revision=single.PROMPT_REVISION,
                  segmented=True, segment_policy=POLICY, status="processing")
    emit, cancelled = progress or (lambda _: None), cancelled or (lambda: False)
    media_client = MediaClient(media_backend["endpoint"], cancelled=cancelled)
    client = SeedanceClient(data["base_url"], key, data["model"], model_family=family, cancelled=cancelled)
    started, elapsed = time.monotonic(), report.get("elapsed_seconds", 0)
    _persist(folder, report)
    emit({"stage": "batch_started", "provider": "seedance", "processing_backend": BACKEND,
          "batch_dir": str(folder), "total": len(report["videos"])})
    stopped = None
    for row in report["videos"]:
        if row["status"] in {"completed", "needs_review"} or row.get("human_rejected"):
            continue
        if cancelled():
            stopped = "cancelled"
            break
        source, work = Path(row["source"]), Path(row["work_dir"])
        work.mkdir(parents=True, exist_ok=True)
        polls = report["params"]["max_turns"]
        current_part = None
        base = {"provider": "seedance", "processing_backend": BACKEND, "batch_dir": str(folder),
                "index": row["index"], "total": len(report["videos"])}

        def save(phase):
            row["phase"] = phase
            report["elapsed_seconds"] = round(elapsed + time.monotonic() - started, 2)
            _persist(folder, report)
            emit({**base, "stage": phase, "segment_tasks": row.get("segment_tasks", []),
                  "segment_index": current_part.get("index") if current_part else None,
                  "segment_total": len(row.get("segment_tasks", []))})

        def check_cancel():
            if cancelled():
                raise SeedanceError("cancelled", "本机已停止等待；服务器及已提交云端任务可能继续，断点保留。")

        def wait(phase):
            nonlocal polls
            check_cancel()
            if polls <= 0:
                raise SeedanceError("poll_budget", "本次查询额度已用完；续跑查询原服务器作业和云端任务，不重复提交生成。")
            polls -= 1
            save(phase)
            single._wait(cancelled)

        def server_job(holder, field, payload, phase):
            check_cancel()
            previous = holder.get(field)
            if previous:
                _require(isinstance(previous, dict) and previous.get("kind") == payload["kind"],
                         "服务器作业断点类型不匹配。")
                MediaClient._id(previous.get("id"))
            # These are idempotent mechanical jobs, not billable generation POSTs.
            # Repeating the same request also recovers an expired cached artifact.
            job = media_client.submit_job(payload)
            if previous:
                _require(previous["id"] == job["id"], "服务器作业身份变化，拒绝混用断点。")
            holder[field] = job
            save(phase)
            while job["status"] in {"queued", "running"}:
                wait(phase)
                job = media_client.get_job(job["id"])
                _require(job["kind"] == payload["kind"], "服务器作业类型变化。")
                holder[field] = job
                save(phase)
            if job["status"] != "succeeded":
                row["server_error_code"] = job.get("error", "processing_failed")
                raise SeedanceError("server_processing", "服务器处理未完成：" + row["server_error_code"] + "；原任务和分段保留。")
            return job["result"]

        row.update(status="processing", message=None, error_code=None, server_error_code=None)
        save("validate_source")
        try:
            info = single.source_metadata(source, options, max_duration=300)
            _require(info["width"] * info["height"] <= 8295044, "原片像素数超过 Seedance 2.0 上限。")
            _require(row.get("source_sha256") in {None, info["sha256"]}, "原片已变化，拒绝续用旧任务。")
            row.update(source_sha256=info["sha256"], source_media=info)
            candidate = work / "seedance-candidate.mp4"
            downloaded = bool(row.get("download_sha256") and candidate.is_file()
                              and engine.digest(candidate) == row["download_sha256"])
            if not downloaded:
                _require(not candidate.exists(), "整片候选已存在但哈希记录不匹配，不会覆盖。")
                check_cancel()
                save("server_source_upload")
                row["media_upload"] = media_client.ensure_uploaded(source, info["sha256"])
                row["media_object_id"] = info["sha256"]
                _require(engine.digest(source) == info["sha256"], "上传期间原片发生变化，未提交生成。")
                save("server_source_uploaded")
                split = server_job(row, "server_split", {"kind": "split", "source_id": info["sha256"],
                                                        "policy": POLICY}, "server_splitting")
                parts = _split_binding(split, info)
                part_rows = _segment_rows(row, parts)
                save("server_split_ready")
                for part in part_rows:
                    current_part = part
                    check_cancel()
                    prompt = prepared["prompt"] + (
                        f"\n本次是服务器按原帧边界切分的第 {part['index']}/{len(parts)} 段。"
                        f"对应原片帧区间 [{part['start_frame']},{part['end_frame']})，"
                        f"输入时长 {part['duration_seconds']:.9f} 秒。保留完整首尾、镜头和文字动画，"
                        "只编辑文字，不加片头片尾或延长剧情；各段译名和样式保持一致。"
                        "不生成音频，服务器最后会复用原片音轨。")
                    fingerprint = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
                    _require(part.get("prompt_fingerprint") in {None, fingerprint}, "片段要求已变化，拒绝混用云端任务。")
                    part["prompt_fingerprint"] = fingerprint
                    if not part.get("task_id"):
                        if part.get("submission_state") == "submitting":
                            raise SeedanceError("submission_uncertain", "上次云端生成提交结果未知，不会自动重发；请先在平台核对任务。", submission_uncertain=True)
                        save("segment_reference")
                        reference = media_client.reference(part["source_object_id"], part["source_bytes"])
                        _require(engine.digest(source) == info["sha256"], "原片已变化，停止提交新的片段。")
                        check_cancel()
                        attempts = part.setdefault("submission_attempts", [])
                        _require(isinstance(attempts, list), "生成提交记录格式无效。")
                        attempt = {"client_request_id": "vl-" + uuid.uuid4().hex,
                                   "started_at": time.time(), "prompt_fingerprint": fingerprint,
                                   "status": "submitting"}
                        attempts.append(attempt)
                        part.update(submission_state="submitting", status="processing")
                        # The correlation ID and intent must survive a lost POST
                        # response. It is not an idempotency key or retry permission.
                        save("segment_submitting")
                        try:
                            resolution = "480p" if min(info["width"], info["height"]) <= 480 else "720p" if min(info["width"], info["height"]) <= 720 else "1080p"
                            task_id = client.create_edit(reference, prompt, resolution=resolution,
                                reference_images=prepared["images"], generate_audio=False,
                                client_request_id=attempt["client_request_id"])
                        except SeedanceError as error:
                            attempt.update(status="unknown" if error.submission_uncertain else "rejected",
                                           finished_at=time.time(), error_code=error.code,
                                           submission_uncertain=error.submission_uncertain)
                            diagnostic = _transport_error(error, "segment_submitting", key)
                            if diagnostic:
                                attempt["transport_error"] = diagnostic
                            if not error.submission_uncertain:
                                part["submission_state"] = "rejected"
                            raise
                        attempt.update(status="submitted", task_id=task_id, finished_at=time.time(),
                                       submission_uncertain=False)
                        part.update(task_id=task_id, submission_state="submitted", remote_status="queued")
                        save("segment_submitted")
                    # A successful import can be reused without fetching another
                    # signed cloud URL. The service marks expired results failed.
                    imported = None
                    old_import = part.get("server_import")
                    if old_import and old_import.get("status") == "succeeded":
                        try:
                            checked = media_client.get_job(old_import["id"])
                        except SeedanceError as error:
                            if error.http_status not in {404, 410}:
                                raise
                        else:
                            _require(checked["kind"] == "import", "服务器导入作业类型变化。")
                            part["server_import"] = checked
                            if checked["status"] == "succeeded":
                                imported = checked["result"]
                    if imported is None:
                        while True:
                            check_cancel()
                            task = client.get_task(part["task_id"])
                            part["remote_status"] = task["status"]
                            save("segment_remote_" + task["status"])
                            if task["status"] == "succeeded":
                                break
                            if task["status"] in {"failed", "cancelled", "expired"}:
                                part.update(status="failed", remote_error_code=task.get("error_code"), error_code="remote_task_failed")
                                row["remote_error_code"] = task.get("error_code")
                                raise SeedanceError("remote_task_failed", task.get("message") or "Seedance 片段任务失败；本视频剩余片段未继续提交，已有任务保留。")
                            wait("segment_waiting")
                        _require(bool(task.get("video_url")), "云端成功任务缺少候选地址。")
                        imported = server_job(part, "server_import", {"kind": "import", "task_id": part["task_id"],
                                                                      "url": task["video_url"]}, "server_importing_candidate")
                    part.update(status="completed", candidate_object=MediaClient._receipt(imported),
                                remote_status="succeeded")
                    save("server_candidate_ready")
                current_part = None
                joined = server_job(row, "server_join", {"kind": "join", "source_id": info["sha256"],
                    "split_job_id": row["server_split"]["id"],
                    "parts": [{"index": part["index"], "task_id": part["task_id"],
                               "object_id": part["candidate_object"]["id"]} for part in part_rows]}, "server_joining")
                row["server_candidate"] = MediaClient._receipt(joined["output"])
                row["server_technical_review"] = joined["technical_review"]
                save("server_joined")
                check_cancel()
                output = row["server_candidate"]
                reference = media_client.reference(output["id"], output["bytes"])
                save("downloading_final")
                client.download(reference, candidate)
                _require(engine.digest(candidate) == output["sha256"] and candidate.stat().st_size == output["bytes"],
                         "服务器整片下载哈希或大小不匹配，不会记为合格候选。")
                row["download_sha256"] = output["sha256"]
                save("downloaded_final")
            save("technical_review")
            review = single.technical_review(source, candidate, options, info)
            server_review = row.get("server_technical_review", {})
            review["checks"]["server_checks_passed"] = server_review.get("status") == "passed"
            review["status"] = "passed" if all(review["checks"].values()) else "failed"
            review.update(processing_backend=BACKEND, segment_count=len(row.get("segment_tasks", [])),
                          server_review=server_review, source_audio_reused=True, boundary_visual_review="not_performed")
            write_json(work / "technical-review.json", review)
            row.update(status="needs_review", output=None, preview=str(candidate), technical_review=review,
                       human_approved=False, model_reviewed=False, phase="awaiting_visual_review",
                       issues=[{"id": single.REVIEW_ID, "status": "pending",
                                "reason": "服务器已完成拼接；需验收译文、残字、运动、遮挡、切镜、分段接缝和音画同步。",
                                "evidence": {"technical_status": review["status"], "checks": review["checks"]}}],
                       message="服务器拼接候选已下载，原声已复用；尚未进行视觉验收。" if review["status"] == "passed"
                       else "服务器拼接候选已下载，但技术属性检查未全部通过；尚未进行视觉验收。")
        except SeedanceError as error:
            status = "cancelled" if error.code == "cancelled" else "paused_budget" if error.code == "poll_budget" else "failed" if error.code == "remote_task_failed" else "paused_error"
            row.update(status=status, error_code=error.code, message=str(error))
            diagnostic = _transport_error(error, row.get("phase"), key)
            if diagnostic:
                # A blocked resume has no new network evidence. Preserve the
                # previous failure instead of replacing it with the guard error.
                row["last_transport_error"] = diagnostic
                if current_part is not None:
                    current_part["last_transport_error"] = diagnostic
            if error.http_status is not None:
                row["http_status"] = error.http_status
            if current_part and status != "failed":
                current_part["status"] = status
            if error.provider_code:
                row["remote_error_code"] = error.provider_code
            if status in STOP_STATES:
                stopped = status
        except (ValueError, OSError, engine.BridgeError) as error:
            row.update(status="failed", error_code="server_validation", message=str(error).replace(key, "[redacted]")[:1500])
        write_json(work / "review.json", row)
        save(row.get("phase", "finished_file"))
        if stopped:
            break
    report["status"] = stopped or ("needs_review" if any(row["status"] == "needs_review" for row in report["videos"])
                                   else "failed" if any(row["status"] == "failed" for row in report["videos"]) else "completed")
    report["elapsed_seconds"] = round(elapsed + time.monotonic() - started, 2)
    _persist(folder, report)
    return _summary(report)
