"""Mechanical frame cuts and stream-copy joining; no visual analysis or editing."""

from fractions import Fraction
import json
import os
from pathlib import Path
import tempfile
import uuid

from . import _engine as engine
from .config import write_json


SCHEMA = 1
MAX_PART_SECONDS = 15


def _check(condition, message):
    if not condition:
        raise ValueError(message)


def _path(value):
    path = Path(value).expanduser().absolute()
    _check(path.resolve() == path and not path.is_symlink(), "分段路径不能包含符号链接或目录跳转。")
    _check(not any(char in str(path) for char in "\r\n\x00"), "分段路径包含不支持的字符。")
    return path


def _video(path, options):
    engine.regular(path)
    raw = engine.command([
        options["ffprobe"], "-v", "error", "-protocol_whitelist", "file,pipe",
        "-count_frames", "-show_streams", "-show_data_hash", "sha256", "-of", "json", str(path),
    ], timeout=180)
    videos = [stream for stream in json.loads(raw).get("streams", [])
              if stream.get("codec_type") == "video" and not stream.get("disposition", {}).get("attached_pic")]
    _check(len(videos) == 1, "候选视频必须恰好包含一个视频流。")
    video = videos[0]
    try:
        video["frame_count"] = int(video["nb_read_frames"])
        fps = Fraction(video["avg_frame_rate"])
        _check(fps > 0 and video["frame_count"] > 0, "视频帧数或帧率无效。")
        video["fps_ratio"] = str(fps)
    except (KeyError, ValueError, ZeroDivisionError):
        raise ValueError("无法取得可靠的视频帧数或帧率。") from None
    return video


def _publish(temporary, target):
    # Windows rename refuses existing targets; POSIX link gives the same guarantee.
    with temporary.open("r+b") as stream:
        os.fsync(stream.fileno())
    if os.name == "nt":
        os.rename(temporary, target)
    else:
        os.link(temporary, target)
        temporary.unlink()


def _timeline(source, options, source_info=None):
    media = engine.probe(source, options, input_limit=False)
    sha256 = engine.digest(source)
    if source_info is not None:
        _check(source_info.get("sha256") == sha256, "原视频哈希已变化，拒绝使用旧分段资料。")
        _check(all(source_info.get(key) == media.get(key) for key in ("width", "height", "fps", "hasAudio")),
               "原视频技术属性与输入资料不一致。")
    frames = engine.frame_manifest(source, options, dict(media))
    _check(frames["sourceSha256"] == sha256, "读取帧时间期间原视频发生变化。")
    _check(frames["media"]["constantFrameRate"], "仅支持恒定帧率的机械分段；不会重定时原视频。")
    fps = Fraction(media["fpsRatio"])
    count = len(frames["frames"])
    duration = Fraction(count, 1) / fps
    _check(4 <= duration <= 30 and 24 <= fps <= 60, "机械分段仅支持 4–30 秒、24–60 帧原片。")
    expected_ms = float(duration * 1000)
    _check(abs(frames["media"]["durationMs"] - expected_ms) <= max(.1, 2 / float(fps)),
           "原视频末帧时长不规则；不会重定时或补帧。")
    return media, sha256, fps, count


def prepare_segments(source, work_dir, options, source_info):
    """Return durable frame-exclusive ranges; resume verifies source and part hashes.

    Input parts are mechanically re-encoded at the original size and frame rate.
    Audio in input parts is context only. Final audio is copied from the original.
    """
    source, work = _path(source), _path(work_dir)
    media, sha256, fps, count = _timeline(source, options, source_info)
    folder = _path(work / "seedance-segments")
    _check(not source.is_relative_to(folder), "原视频不能位于分段输出目录内。")
    folder.mkdir(parents=True, exist_ok=True)
    manifest_path = folder / "manifest.json"
    split = count if Fraction(count, 1) / fps <= MAX_PART_SECONDS else count // 2
    boundaries = [0, count] if split == count else [0, split, count]
    parts = []
    for index, (start, end) in enumerate(zip(boundaries, boundaries[1:]), 1):
        duration = Fraction(end - start, 1) / fps
        _check(4 <= duration <= MAX_PART_SECONDS, "机械分段时长超出 4–15 秒范围。")
        parts.append({"index": index, "path": str(folder / f"part-{index:03d}.mp4"),
                      "start_frame": start, "end_frame": end, "frame_count": end - start,
                      "start_seconds": float(Fraction(start, 1) / fps),
                      "duration_seconds": float(duration), "sha256": None})
    manifest = {"schema": SCHEMA, "source": str(source), "source_sha256": sha256,
                "source_frame_count": count, "fps_ratio": str(fps),
                "width": media["width"], "height": media["height"],
                "range_convention": "start_inclusive_end_exclusive", "parts": parts,
                "local_operations": "frame-aligned cuts; input video re-encode; context audio AAC"}
    if manifest_path.exists():
        engine.regular(manifest_path, 1024 * 1024)
        saved = json.loads(manifest_path.read_text(encoding="utf-8"))
        _check(isinstance(saved, dict), "分段清单无效。")
        _check({key: value for key, value in saved.items() if key != "parts"}
               == {key: value for key, value in manifest.items() if key != "parts"},
               "原视频或机械分段参数已变化，拒绝续用清单。")
        _check(isinstance(saved.get("parts"), list) and len(saved["parts"]) == len(parts), "分段清单范围无效。")
        for expected, previous in zip(parts, saved["parts"]):
            _check(isinstance(previous, dict) and
                   {key: value for key, value in previous.items() if key != "sha256"}
                   == {key: value for key, value in expected.items() if key != "sha256"},
                   "分段清单路径或帧范围已变化。")
            expected["sha256"] = previous.get("sha256")
    else:
        _check(not any(Path(part["path"]).exists() for part in parts), "分段文件已存在但没有清单；不会覆盖。")
        write_json(manifest_path, manifest)
    for part in parts:
        path = _path(part["path"])
        if part["sha256"]:
            engine.regular(path)
            _check(engine.digest(path) == part["sha256"], "已保存分段文件哈希变化，拒绝恢复。")
            continue
        _check(not path.exists(), "分段文件已存在但没有完整校验记录；不会覆盖。")
        temporary = folder / (".cut-" + uuid.uuid4().hex + ".mp4")
        start = part["start_seconds"]
        end = start + part["duration_seconds"]
        arguments = engine.ffinput(options["ffmpeg"], source) + [
            "-n", "-map", "0:v:0", "-vf",
            f"trim=start_frame={part['start_frame']}:end_frame={part['end_frame']},setpts=PTS-STARTPTS",
            "-fps_mode", "passthrough", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-map_metadata", "-1",
        ]
        if media["hasAudio"]:
            arguments += ["-map", "0:a:0", "-af", f"atrim=start={start:.12f}:end={end:.12f},asetpts=PTS-STARTPTS",
                          "-c:a", "aac", "-b:a", "192k"]
        arguments += ["-movflags", "+faststart", str(temporary)]
        try:
            engine.command(arguments, timeout=300)
            video = _video(temporary, options)
            _check(video["frame_count"] == part["frame_count"] and Fraction(video["fps_ratio"]) == fps,
                   "机械分段帧数或帧率不符；未发布此段。")
            _check((video["width"], video["height"]) == (media["width"], media["height"]),
                   "机械分段尺寸不符；未发布此段。")
            _check(engine.digest(source) == sha256, "机械分段期间原视频发生变化。")
            part["sha256"] = engine.digest(temporary)
            _publish(temporary, path)
            write_json(manifest_path, manifest)
        finally:
            temporary.unlink(missing_ok=True)
    _check(engine.digest(source) == sha256, "原视频已变化，分段未通过最终校验。")
    return manifest


def join_candidates(source, candidates, output, options):
    """Concat compatible cloud videos with stream copy and copy original audio.

    No resizing, interpolation, timing adjustment, background repair or translation
    occurs here. Failed validation leaves the original cloud candidates intact.
    """
    source, output = _path(source), _path(output)
    _check(isinstance(candidates, (list, tuple)) and 1 <= len(candidates) <= 2,
           "拼接需要 1–2 个已完成的云端候选视频。")
    paths = [_path(path) for path in candidates]
    _check(output.suffix.lower() == ".mp4" and not output.exists(), "拼接输出必须是尚不存在的 MP4 文件。")
    _check(all(path.is_relative_to(output.parent) and path != output and path != source for path in paths),
           "云候选必须位于指定输出目录内，且不能使用原片或输出自身。")
    _check(len(set(paths)) == len(paths), "云候选文件重复。")
    media, source_sha256, _source_fps, source_count = _timeline(source, options)
    videos = [_video(path, options) for path in paths]
    hashes = [engine.digest(path) for path in paths]
    signature_keys = ("codec_name", "profile", "level", "width", "height", "pix_fmt", "time_base",
                      "sample_aspect_ratio", "field_order", "extradata_hash", "fps_ratio")
    signature = tuple(videos[0].get(key) for key in signature_keys)
    _check(videos[0].get("codec_name") in {"h264", "hevc"} and videos[0].get("extradata_hash"),
           "云候选编码或参数集无法验证，不能无损拼接。")
    candidate_fps = Fraction(videos[0]["fps_ratio"])
    candidate_count = sum(video["frame_count"] for video in videos)
    candidate_duration_ms = candidate_count * 1000 / float(candidate_fps)
    for video in videos:
        _check(tuple(video.get(key) for key in signature_keys) == signature,
               "云候选编码、尺寸、帧率或参数集不兼容；保留原候选，不转码修复。")
    for path, video in zip(paths, videos):
        candidate_media = engine.probe(path, options, input_limit=False)
        timeline = engine.frame_manifest(path, options, candidate_media)
        _check(timeline["media"]["constantFrameRate"]
               and abs(timeline["media"]["durationMs"] - video["frame_count"] * 1000 / float(candidate_fps)) <= .1,
               "云候选帧时间不规则；保留候选，不重定时。")
    segment_manifest = output.parent / "seedance-segments" / "manifest.json"
    if segment_manifest.exists():
        engine.regular(segment_manifest, 1024 * 1024)
        saved = json.loads(segment_manifest.read_text(encoding="utf-8"))
        _check(saved.get("source_sha256") == source_sha256 and len(saved.get("parts", [])) == len(videos),
               "拼接与原分段清单不一致。")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".seedance-join-", dir=output.parent) as temporary_dir:
        scratch = Path(temporary_dir)
        listing = scratch / "inputs.ffconcat"
        # Quoted, local absolute paths; the protocol allowlist forbids network reads.
        entries = []
        for path, video in zip(paths, videos):
            entries.append("file '" + path.as_posix().replace("'", "'\\''") + "'")
            # Boundaries use the actual video stream length, not cloud audio padding.
            entries.append(f"duration {video['frame_count'] / float(candidate_fps):.12f}")
        listing.write_text("ffconcat version 1.0\n" + "\n".join(entries) + "\n", encoding="utf-8")
        temporary = scratch / "joined.mp4"
        engine.command([
            options["ffmpeg"], "-nostdin", "-hide_banner", "-loglevel", "error", "-n",
            "-protocol_whitelist", "file,pipe", "-f", "concat", "-safe", "0", "-i", str(listing),
            "-protocol_whitelist", "file,pipe", "-i", str(source), "-map", "0:v:0",
            "-map", "1:a:0?", "-c:v", "copy", "-c:a", "copy", "-map_metadata", "-1",
            "-movflags", "+faststart", str(temporary),
        ], timeout=300)
        output_video = _video(temporary, options)
        measured = engine.probe(temporary, options, input_limit=False)
        _check(output_video["frame_count"] == candidate_count
               and Fraction(output_video["fps_ratio"]) == candidate_fps,
               "无损拼接后的帧数或时序发生变化；未发布拼接结果。")
        _check(abs(measured["durationMs"] - candidate_duration_ms) <= 500 / float(candidate_fps),
               "无损拼接时长与云候选实际时长不符；未发布拼接结果。")
        engine.verify_full_audio(source, temporary, media, measured, options)
        engine.command(engine.ffinput(options["ffmpeg"], temporary) +
                       ["-xerror", "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"], timeout=300)
        _check(engine.digest(source) == source_sha256 and hashes == [engine.digest(path) for path in paths],
               "拼接期间原片或云候选发生变化；未发布结果。")
        sha256 = engine.digest(temporary)
        _publish(temporary, output)
    return {"path": str(output), "sha256": sha256, "bytes": measured["bytes"], "media": measured,
            "source_sha256": source_sha256, "candidate_sha256": hashes,
            "frame_count": candidate_count, "source_frame_count": source_count,
            "candidate_frame_count": candidate_count, "visual_acceptance": "not_performed",
            "local_operations": "video concat stream copy; original audio stream copy"}
