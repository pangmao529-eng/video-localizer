"""Seedance 2.0 per-segment cloud editing with exact, mechanical local assembly."""
import json
from pathlib import Path
import time

from . import _engine as engine
from .config import SEEDANCE_20_FAMILY, SettingsError, write_json, seedance_family
from . import seedance_batch as single
from .seedance_segments import prepare_segments, join_candidates


def run(folder, report, connection, progress=None, cancelled=None):
    from .batch import _load_batch, _persist, _summary, STOP_STATES
    data, key, options = connection
    if (data.get("provider") != "seedance" or seedance_family(data["model"], data.get("model_family")) != SEEDANCE_20_FAMILY
            or report.get("provider") != "seedance" or report["model"] != data["model"]
            or report["endpoint"] != data["base_url"]
            or seedance_family(report["model"], report.get("model_family")) != SEEDANCE_20_FAMILY):
        raise SettingsError("Seedance 2.0 分段批次与当前模型/端点不匹配。")
    prepared = single.prepare(report["params"], data["model"], SEEDANCE_20_FAMILY)
    from .selfhost_media import bind_report
    media_backend = bind_report(report)
    if report.get("materials_fingerprint") not in {None, prepared["fingerprint"]}:
        raise ValueError("原要求或资料已变化，拒绝混用分段生成结果。")
    report.update(materials_fingerprint=prepared["fingerprint"], prompt_revision=single.PROMPT_REVISION,
                  segmented=True, segment_policy="frame-balanced-v1", status="processing")
    started, previous_elapsed = time.monotonic(), report.get("elapsed_seconds", 0)
    emit, cancelled = progress or (lambda _: None), cancelled or (lambda: False)
    _persist(folder, report)
    emit({"stage": "batch_started", "provider": "seedance", "batch_dir": str(folder), "total": len(report["videos"])})
    stopped = None
    for row in report["videos"]:
        if row["status"] in {"completed", "needs_review"} or row.get("human_rejected"):
            continue
        if cancelled():
            stopped = "cancelled"
            break
        source, work = Path(row["source"]), Path(row["work_dir"])
        work.mkdir(parents=True, exist_ok=True)
        base = {"provider": "seedance", "batch_dir": str(folder), "index": row["index"], "total": len(report["videos"])}

        def save(phase):
            row["phase"] = phase
            report["elapsed_seconds"] = round(previous_elapsed + time.monotonic() - started, 2)
            _persist(folder, report)
            emit({**base, "stage": phase, "segment_tasks": row.get("segment_tasks", [])})

        row.update(status="processing", message=None, error_code=None)
        save("prepare_segments")
        try:
            info = single.source_metadata(source, options)
            if info["width"] * info["height"] > 8295044:
                raise ValueError("原片像素数超过 Seedance 2.0 输入上限，未进行分段或上传。")
            if row.get("source_sha256") not in {None, info["sha256"]}:
                raise ValueError("原片已变化，拒绝继续旧分段批次。")
            row.update(source_sha256=info["sha256"], source_media=info)
            segments = prepare_segments(source, work, options, info)
            row["segments"] = segments
            child_folder = work / "seedance-jobs"
            child_file = child_folder / "batch.json"
            if child_file.exists():
                _, child = _load_batch(child_folder)
                if not child.get("segment_worker") or child.get("parent_source_sha256") != info["sha256"]:
                    raise ValueError("分段任务记录与原片不一致。")
                if [r["source"] for r in child["videos"]] != [str(p["path"]) for p in segments["parts"]]:
                    raise ValueError("分段任务清单不匹配。")
                child["params"]["max_turns"] = report["params"]["max_turns"]
            else:
                child_folder.mkdir(parents=True, exist_ok=True)
                params = dict(report["params"])
                params["request"] += "\n本次输入是整条原片按帧边界机械分段后的一个连续片段，请保留此片段的完整首尾、原始镜头边界与文字动画。只编辑文字，不增加片头片尾或延长剧情。各段使用一致的译名和字体风格；输出不生成音频，插件会按用户要求复用原片音轨。"
                child = {"schema": 3, "provider": "seedance", "model": data["model"], "endpoint": data["base_url"],
                         "model_family": SEEDANCE_20_FAMILY,
                         "media_backend": media_backend,
                         "directory": str(child_folder), "status": "ready", "total": len(segments["parts"]),
                         "segment_worker": True, "parent_source_sha256": info["sha256"], "params": params,
                         "target_language": params["target"], "profile": params["profile"],
                         "created_at": time.time(), "elapsed_seconds": 0,
                         "videos": [{"index": index, "source": str(part["path"]), "work_dir": str(child_folder / "videos" / f"{index:03d}"),
                                     "status": "not_processed", "output": None, "issues": [], "human_approved": False,
                                     "model_reviewed": False} for index, part in enumerate(segments["parts"], 1)]}
                _persist(child_folder, child)
            row["child_batch_dir"] = str(child_folder)
            save("segment_editing")

            def child_progress(event):
                row["phase"] = "segment_" + event.get("stage", "processing")
                row["segment_tasks"] = [{"index": r["index"], "task_id": r.get("task_id"), "file_id": r.get("file_id"),
                                         "status": r["status"], "remote_status": r.get("remote_status"),
                                         "error_code": r.get("error_code"),
                                         "remote_error_code": r.get("remote_error_code"),
                                         "last_transport_error": r.get("last_transport_error")} for r in child["videos"]]
                _persist(folder, report)
                emit({**event, **base, "segment_index": event.get("index"), "segment_total": len(child["videos"]),
                      "phase": row["phase"]})

            child_result = single._run_single(child_folder, child, connection, child_progress, cancelled)
            row["segment_tasks"] = [{"index": r["index"], "task_id": r.get("task_id"), "file_id": r.get("file_id"),
                                     "status": r["status"], "remote_status": r.get("remote_status"),
                                     "error_code": r.get("error_code"), "message": r.get("message"),
                                     "remote_error_code": r.get("remote_error_code"),
                                     "last_transport_error": r.get("last_transport_error")} for r in child["videos"]]
            if child_result["status"] in STOP_STATES or any(r["status"] not in {"completed", "needs_review"} for r in child["videos"]):
                status = child_result["status"] if child_result["status"] in STOP_STATES else "failed"
                failing = next((r for r in child["videos"] if r["status"] not in {"completed", "needs_review"}), {})
                row.update(status=status, error_code=failing.get("error_code"), message=failing.get("message") or "分段任务尚未完成。")
                row["remote_error_code"] = failing.get("remote_error_code")
                if failing.get("last_transport_error"):
                    row["last_transport_error"] = dict(failing["last_transport_error"], segment_index=failing["index"])
                if status in STOP_STATES:
                    stopped = status
            else:
                candidates = [Path(r.get("preview") or r.get("output") or "") for r in child["videos"]]
                for candidate, part_row in zip(candidates, child["videos"]):
                    if not candidate.is_file() or engine.digest(candidate) != part_row.get("download_sha256"):
                        raise ValueError("云端分段候选文件校验不匹配。")
                if cancelled():
                    row.update(status="cancelled", message="拼接前已取消，云端分段候选与任务断点保留。")
                    stopped = "cancelled"
                else:
                    candidate = work / "seedance-candidate.mp4"
                    if candidate.exists():
                        if not row.get("download_sha256") or engine.digest(candidate) != row["download_sha256"]:
                            raise ValueError("已存在整片候选但校验记录不匹配，不会覆盖。")
                    else:
                        save("joining_segments")
                        join_candidates(source, candidates, candidate, options)
                        row["download_sha256"] = engine.digest(candidate)
                        save("joined_segments")
                    review = single.technical_review(source, candidate, options, info)
                    review.update(segment_count=len(candidates), boundary_visual_review="not_performed",
                                  source_audio_reused=True)
                    write_json(work / "technical-review.json", review)
                    row.update(status="needs_review", output=None, preview=str(candidate), technical_review=review,
                               model_reviewed=False, human_approved=False, phase="awaiting_visual_review",
                               issues=[{"id": single.REVIEW_ID, "status": "pending", "reason": "分段已拼接；需检查跨段译名一致性、接缝、切镜、遮挡、文字和音画同步。",
                                        "evidence": {"technical_status": review["status"], "checks": review["checks"]}}],
                               message="Seedance 2.0 分段候选已拼接，复用原声音轨；尚未完成视觉验收。" if review["status"] == "passed" else "已拼接候选，但技术属性未全部保持；分段接缝和文字仍待视觉验收。")
        except (ValueError, OSError, engine.BridgeError, RuntimeError) as error:
            row.update(status="failed", error_code="segment_validation", message=str(error).replace(key, "[redacted]")[:1500])
        write_json(work / "review.json", row)
        save(row.get("phase", "finished_file"))
        if stopped:
            break
    report["status"] = stopped or ("needs_review" if any(r["status"] == "needs_review" for r in report["videos"])
                                   else "failed" if any(r["status"] == "failed" for r in report["videos"]) else "completed")
    report["elapsed_seconds"] = round(previous_elapsed + time.monotonic() - started, 2)
    _persist(folder, report)
    return _summary(report)
