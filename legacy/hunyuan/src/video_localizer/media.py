"""Run the local video workflow using a supplied visual model.

All geometry and timestamps refer to the decoded source. Only selected JPEG
frames leave this module through model.recognize(); source and output stay local.
"""
from __future__ import annotations

import bisect
import contextlib
import errno
import hashlib
import math
import os
from pathlib import Path
import shutil
import tempfile
import time

from . import _engine as engine


def _select_frames(source, config, manifest):
    """Use a quiet-scene heartbeat and the adjacent frames at visible changes."""
    import cv2

    media, frames = manifest["media"], manifest["frames"]
    timestamps = [frame["ptsMs"] for frame in frames]
    selected = {0, len(frames) - 1}
    for ms in range(0, math.ceil(media["durationMs"]), 1000):
        selected.add(min(bisect.bisect_left(timestamps, ms), len(frames) - 1))
    scale = min(1, 480 / max(media["width"], media["height"]))
    width, height = [max(2, round(media[key] * scale)) for key in ("width", "height")]
    last_gray = None
    differences = [(0.0, 0)]
    count = 0
    for i, rgb in enumerate(engine.raw_frames(source, config, width, height)):
        engine.require(i < len(frames), "video_localization_frame_count_changed")
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        if last_gray is not None:
            delta = cv2.absdiff(gray, last_gray)
            global_mean = float(delta.mean())
            tiles = cv2.resize(delta, (min(width, 24), min(height, 16)),
                               interpolation=cv2.INTER_AREA)
            local_peak = int(tiles.max())
            differences.append((global_mean, local_peak))
        last_gray = gray
        count += 1
    engine.require(count == len(frames), "video_localization_frame_count_changed")
    abrupt, moving = {}, {}
    for i in range(1, len(frames)):
        average, peak = differences[i]
        if average < 32 and peak < 63 and not (peak > 35 and average < 12):
            continue
        neighbors = differences[max(1, i-3):i] + differences[i+1:i+4]
        surrounding = sorted(a + p for a, p in neighbors)
        baseline = surrounding[len(surrounding)//2] if surrounding else 0
        score = peak + average
        # Spend adjoining frames on isolated cuts or a brief change in a quiet
        # label. Continuous camera motion needs dispersed anchors, not a burst
        # on every ninth frame throughout the shot.
        sharp = score > max(38, 1.55 * baseline)
        buckets, span = (abrupt, 250) if sharp else (moving, 750)
        bucket = int(timestamps[i] // span)
        if bucket not in buckets or score > buckets[bucket][0]:
            buckets[bucket] = (score, i)
    for _, index in moving.values():
        selected.add(index)
    for _, index in abrupt.values():
        start, end = index - 1, index + 1
        selected.update(range(max(0, start), min(len(frames), end + 1)))
    return sorted(selected)


def _scene_text(regions):
    # A stale nameplate can survive a portrait cut. The identified person's
    # target name marks that boundary even if every original glyph is unchanged.
    return sorted((r["kind"], r["source_text"].strip(), bool(r["uncertain"]),
                   r["target_text"].strip() if r["kind"] == "nameplate" else "")
                  for r in regions)


class _Recognition:
    def __init__(self, root, source, config, manifest, model, group_size, progress):
        self.root, self.source, self.config, self.manifest = root, source, config, manifest
        self.model, self.group_size, self.progress = model, group_size, progress
        self.initial_requests = getattr(model, "request_count", 0)
        self.regions = {}
        self.cache = {}
        self.groups = self.sent = self.reused = 0
        self.wait_seconds = 0.0

    def inspect(self, indices):
        """Decode once per pass. Keep at most one group of images on disk."""
        from PIL import Image

        required = set(indices) - self.regions.keys()
        if not required:
            return
        media = self.manifest["media"]
        width, height = media["width"], media["height"]
        pending = {}
        def flush():
            if not pending:
                return
            rows = [entry["request"] for entry in pending.values()]
            started = time.monotonic()
            result = self.model.recognize(rows)
            self.wait_seconds += time.monotonic() - started
            self.groups += 1
            self.sent += len(rows)
            if (not isinstance(result, list) or len(result) != len(rows)
                    or any(answer.get("frame_id") != req["frame_id"]
                           or not isinstance(answer.get("regions"), list)
                           for answer, req in zip(result, rows))):
                raise ValueError("The visual model returned an incomplete frame group")
            for (key, entry), answer in zip(pending.items(), result):
                self.cache[key] = answer["regions"]
                for index in entry["indices"]:
                    self.regions[index] = answer["regions"]
                entry["request"]["path"].unlink()
            pending.clear()
            self.progress({"stage": "recognition", "model_calls": self.actual_calls,
                           "observed_frames": len(self.regions)})

        last_needed = max(required)
        frames = engine.raw_frames(self.source, self.config, width, height)
        with contextlib.closing(frames):
            for i, rgb in enumerate(frames):
                if i not in required:
                    continue
                # Full-resolution decoded pixels, not a thumbnail/perceptual hash:
                # one changed nameplate character must invalidate a cached reading.
                key = hashlib.sha256(memoryview(rgb)).digest()
                if key in self.cache:
                    self.regions[i] = self.cache[key]
                    self.reused += 1
                elif key in pending:
                    pending[key]["indices"].append(i)
                    self.reused += 1
                else:
                    frame = self.manifest["frames"][i]
                    path = self.root / f"view-{i:06}.jpg"
                    Image.fromarray(rgb).save(path, "JPEG", quality=94, subsampling=0)
                    pending[key] = {"indices": [i], "request": {
                        "frame_id": frame["frameId"], "index": i, "time_ms": frame["ptsMs"],
                        "width": width, "height": height, "path": path}}
                    if len(pending) == self.group_size:
                        flush()
                if i == last_needed:
                    break
        flush()
        engine.require(required <= self.regions.keys(), "video_localization_frame_count_changed")

    @property
    def actual_calls(self):
        return getattr(self.model, "request_count", self.initial_requests + self.groups) - self.initial_requests

    def refine(self, rounds=2):
        # Clarify the boundary where readings actually differ. This never assumes
        # identical words in two widely spaced frames are a verified cut time.
        for _ in range(rounds):
            ordered = sorted(self.regions)
            extra = [(left + right) // 2 for left, right in zip(ordered, ordered[1:])
                     if right - left > 1
                     and _scene_text(self.regions[left]) != _scene_text(self.regions[right])]
            if not extra:
                return
            self.inspect(extra)


def _issue(frames, media, start, end, code, **details):
    return {"start_frame": start, "end_frame": end, "start_ms": frames[start]["ptsMs"],
            "end_ms": frames[end + 1]["ptsMs"] if end + 1 < len(frames) else media["durationMs"],
            "code": code, **details}


def _distance(box, old):
    x, y, w, h = box
    ox, oy, ow, oh = old
    return math.hypot((x + w/2 - ox - ow/2) / max(1, w, ow),
                      (y + h/2 - oy - oh/2) / max(1, h, oh))


def _segments(manifest, observations):
    """Connect a reading only across adjacent observations and plausible motion."""
    frames, media = manifest["frames"], manifest["media"]
    width, height = media["width"], media["height"]
    ordered = sorted(observations)
    segments, issues, active, name_corrections = [], [], {}, []
    kinds = {"subtitle": "dialogue", "nameplate": "name", "ui": "title"}
    for pos, index in enumerate(ordered):
        end = ordered[pos + 1] - 1 if pos + 1 < len(ordered) else len(frames) - 1
        next_active = {}
        for r in observations[index]:
            text, translation = r["source_text"], r["target_text"]
            box = r["box"]
            if r["uncertain"] or not text.strip() or not translation.strip() or len(translation) > 2000:
                issues.append(_issue(frames, media, index, end, "model_uncertain",
                                     source_text=text, target_text=translation,
                                     box=box, reason=r["reason"]))
                continue
            if r["kind"] == "nameplate" and "portrait_name_corrected" in r["reason"]:
                name_corrections.append(_issue(frames, media, index, end, "portrait_name_corrected",
                                              source_text=text, target_text=translation, reason=r["reason"]))
            if text.strip() == translation.strip():
                continue
            x, y, w, h = box
            if (min(w, h) < 4 or x < 0 or y < 0 or x + w > width or y + h > height
                    or w * h > width * height * .4):
                issues.append(_issue(frames, media, index, end, "box_unsuitable",
                                     source_text=text, target_text=translation, box=box))
                continue
            key = (r["kind"], text.strip(), translation.strip())
            prior = active.get(key, [])
            candidates = [(_distance(box, old_box), i) for i, (_, old_box) in enumerate(prior)]
            best = min(candidates, default=(float("inf"), -1))
            if best[0] <= 1.0:
                segment, _ = prior.pop(best[1])
            else:
                if len(segments) >= engine.MAX_AUTOMATIC_SEGMENTS:
                    issues.append(_issue(frames, media, index, end, "segment_limit",
                                         source_text=text, target_text=translation, box=box))
                    continue
                segment = {"id": f"text_{len(segments) + 1}", "sourceText": text,
                           "translation": translation, "kind": kinds.get(r["kind"], "other"),
                           "startMs": frames[index]["ptsMs"], "keyframes": [],
                           "adaptiveBounds": True, "mask": {"mode": "auto", "dilatePx": 1}}
                segments.append(segment)
                if pos and index - ordered[pos - 1] > 1:
                    issues.append(_issue(frames, media, ordered[pos - 1] + 1, index - 1,
                                         "unverified_onset", source_text=text, target_text=translation))
            segment["endMs"] = (frames[end + 1]["ptsMs"] if end + 1 < len(frames)
                                else media["durationMs"])
            segment["keyframes"].append({"frameId": frames[index]["frameId"],
                "confidence": 1, "box": {"x": x / width, "y": y / height,
                                         "width": w / width, "height": h / height}})
            next_active.setdefault(key, []).append((segment, box))
        active = next_active
    return segments, issues, name_corrections


def _coalesce(rows):
    """Combine repeated readings of the same issue; keep separate locations."""
    grouped = {}
    for row in sorted(rows, key=lambda r: (r["start_ms"], r["end_ms"])):
        key = (row["code"], row.get("source_text"), row.get("target_text"),
               tuple(row.get("box", [])), row.get("reason"), row.get("segment_id"))
        merged = grouped.setdefault(key, [])
        if merged and abs(merged[-1]["end_ms"] - row["start_ms"]) < .001:
            merged[-1]["end_ms"] = row["end_ms"]
            if "end_frame" in row:
                merged[-1]["end_frame"] = row["end_frame"]
        else:
            merged.append(row.copy())
    return sorted((row for group in grouped.values() for row in group), key=lambda r: r["start_ms"])


def _passthrough(root, source, config, manifest, profile):
    """Make a playable, explicitly zero-change deliverable in the selected profile."""
    target = root / "passthrough.mp4"
    media = manifest["media"]
    if profile == "source" and media["videoCodec"] == "h264":
        shutil.copyfile(source, target)
    else:
        args = engine.ffinput(config["ffmpeg"], source) + ["-map", "0:v:0", "-map", "0:a:0?"]
        if profile == "2k120":
            factor = 2560 / max(media["width"], media["height"])
            w, h = [max(2, 2 * round(media[k] * factor / 2)) for k in ("width", "height")]
            args += ["-vf", f"fps=120,scale={w}:{h}:flags=lanczos,setsar=1"]
        else:
            args += ["-fps_mode", "passthrough"]
        args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
                 "-c:a", "copy", "-movflags", "+faststart", str(target)]
        engine.command(args, timeout=3600)
    measured = engine.probe(target, config, input_limit=False)
    duration_tolerance = max(100, 2000 / measured["fps"])
    engine.require(measured["videoCodec"] == "h264" and
                   abs(measured["durationMs"] - media["durationMs"]) <= duration_tolerance,
                   "video_localization_output_geometry_changed")
    audio_hash = engine.verify_full_audio(source, target, media, measured, config)
    return {"file": target.name, "changedFrames": 0, "audioPacketSha256": audio_hash,
            "originalAudioPreserved": True, "profile": profile, "media": measured,
            "sourceFrames": len(manifest["frames"])}


def _publish(src, dest):
    # Use the destination volume for the final commit, including external disks.
    # Same-directory hard linking publishes atomically without overwriting another run.
    fd, name = tempfile.mkstemp(prefix=".localize-", suffix=".mp4", dir=dest.parent)
    os.close(fd)
    staged = Path(name)
    try:
        try:
            os.replace(src, staged)
        except OSError as exc:
            if exc.errno != errno.EXDEV and getattr(exc, "winerror", None) != 17:
                raise
            shutil.copyfile(src, staged)
        try:
            os.link(staged, dest)
        except FileExistsError:
            raise
        except OSError:
            with dest.open("xb"):
                pass
            try:
                os.replace(staged, dest)
            except BaseException:
                dest.unlink(missing_ok=True)
                raise
    finally:
        staged.unlink(missing_ok=True)


def run_video(source: Path, output: Path, model, options: dict | None = None, progress=None) -> dict:
    """Recognize and localize one MP4. Model/API failures propagate to the caller."""
    options = options or {}
    progress = progress or (lambda event: None)
    source, output = Path(source).resolve(strict=True), Path(output).absolute()
    if output.exists() or output.is_symlink() or source == output:
        raise FileExistsError(f"Output already exists: {output.name}")
    if output.suffix.lower() != ".mp4":
        raise ValueError("Output must be an MP4")
    profile, group_size = options.get("profile", "source"), options.get("group_size", 4)
    if profile not in ("source", "2k120") or not isinstance(group_size, int) or not 1 <= group_size <= 4:
        raise ValueError("Expected profile source/2k120 and group_size between 1 and 4")
    font = options.get("font") or options.get("font_sans")
    if not font:
        raise ValueError("A configured font file is required")
    from PIL import ImageFont
    ImageFont.truetype(str(font), 16)
    config = {"ffmpeg": str(options.get("ffmpeg") or "ffmpeg"),
              "ffprobe": str(options.get("ffprobe") or "ffprobe"),
              "fonts": {"sans": str(font), "serif": str(options.get("font_serif") or font)}}
    output.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    def signal(stage):
        progress({"stage": stage})
    with tempfile.TemporaryDirectory(prefix="localizer-", dir=options.get("work_dir") or output.parent) as tmp:
        root = Path(tmp)
        local_source = root / "source.mp4"
        signal("discovery")
        shutil.copyfile(source, local_source)
        media = engine.probe(local_source, config)
        manifest = engine.frame_manifest(local_source, config, media)
        if profile == "source" and not media["constantFrameRate"]:
            raise ValueError("原片为可变帧率。请选择 2k120 输出以保留原始时间线。")
        if profile == "source" and (media["width"] % 2 or media["height"] % 2):
            raise ValueError("原片含奇数像素边长，无法按原尺寸输出 H.264。请选择 2k120。")
        selected = _select_frames(local_source, config, manifest)
        after_plan = time.monotonic()
        signal("recognition")
        recognition = _Recognition(root, local_source, config, manifest, model, group_size, progress)
        recognition.inspect(selected)
        recognition.refine()
        observed = recognition.regions
        manifest.update(discoveryIndices=sorted(observed), discoveryComplete=True)
        engine.write_json(root, "manifest.json", manifest)
        segments, issues, corrections = _segments(manifest, observed)
        after_model = time.monotonic()
        source_by_id = {s["id"]: s for s in segments}
        signal("tracking")
        base = {"source": local_source.name, "uncertaintyPolicy": engine.PRESERVE}
        tracked = None
        if segments:
            try:
                tracked = engine.track(root, dict(base, segments=segments, adaptiveSourceBounds=True,
                                                  validateLayout=True), config)
            except engine.BridgeError as exc:
                if exc.code != "video_localization_no_changes":
                    raise
                for seg in segments:
                    issues.append({"code": "all_tracking_uncertain", "source_text": seg["sourceText"],
                                   "target_text": seg["translation"], "start_ms": seg["startMs"], "end_ms": seg["endMs"]})
        after_track = time.monotonic()
        signal("rendering")
        rendered = None
        if tracked:
            try:
                rendered = engine.render(root, dict(base, profile=profile), config)
            except engine.BridgeError as exc:
                if exc.code != "video_localization_no_changes":
                    raise
                for seg in segments:
                    issues.append({"code": "no_verified_pixel_changes", "source_text": seg["sourceText"],
                                   "target_text": seg["translation"], "start_ms": seg["startMs"], "end_ms": seg["endMs"]})
        if rendered is None:
            rendered = _passthrough(root, local_source, config, manifest, profile)
        for row in rendered.get("preservedRanges", []):
            seg = source_by_id.get(row["segmentId"], {})
            issues.append(_issue(manifest["frames"], media, row["startFrame"], row["endFrame"],
                                 row["code"], source_text=seg.get("sourceText", ""),
                                 target_text=seg.get("translation", "")))
        _publish(root / rendered["file"], output)
        found = sum(len(rows) for rows in observed.values())
        if issues:
            status = "needs_review"
        elif not found:
            status = "no_text_detected"
        elif not segments:
            status = "no_change_needed"
        else:
            status = "completed"
        end = time.monotonic()
        return {"status": status, "source": str(source), "output": str(output), "issues": _coalesce(issues),
                "automatic_name_corrections": _coalesce(corrections),
                "automatic_quality_pass": False, "source_frames": len(manifest["frames"]),
                "observed_frames": len(observed), "initial_observations": len(selected),
                "reused_identical_frames": recognition.reused, "frames_sent": recognition.sent,
                "model_groups": recognition.groups, "model_calls": recognition.actual_calls,
                "segments": len(segments), "changed_frames": rendered["changedFrames"],
                "audio_preserved": rendered["originalAudioPreserved"], "audio_packet_sha256": rendered["audioPacketSha256"],
                "media": rendered["media"], "profile": profile,
                "timings": {"discovery_seconds": round(after_plan - start, 3),
                            "recognition_seconds": round(after_model - after_plan, 3),
                            "model_wait_seconds": round(recognition.wait_seconds, 3),
                            "tracking_seconds": round(after_track - after_model, 3),
                            "render_export_seconds": round(end - after_track, 3),
                            "total_seconds": round(end - start, 3)}}

