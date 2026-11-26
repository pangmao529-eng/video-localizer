"""CPU video operations extracted from the project's 0.52 local media bridge.
All work uses the original source-frame indices and presentation timestamps.
"""

from __future__ import annotations
import bisect
from collections import defaultdict, OrderedDict
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time

PROTOCOL = 1
FILE_LIMIT = 512 * 1024**2
OUTPUT_LIMIT = 2 * 1024**3
SCRATCH_LIMIT = 12 * 1024**3
RESERVE = 1024**3
MAX_FRAMES = 36_100
MAX_AUTOMATIC_SEGMENTS = 2048
JSON_LIMIT = 16 * 1024**2
TRACKING_JSON_LIMIT = 64 * 1024**2
SAFE_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,90}\.(mp4|png|jpg|json|zip|srt|txt)")
FRAME_ID = re.compile(r"f([0-9]{6})")


class BridgeError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(value, code="video_localization_invalid"):
    if not value:
        raise BridgeError(code)


def finite(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def regular(path, limit=OUTPUT_LIMIT):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and not path.is_symlink() and info.st_nlink == 1, "video_localization_unsafe_path")
    require(0 < info.st_size <= limit, "video_localization_file_limit")
    return info.st_size


def local(root, name, existing=True):
    require(isinstance(name, str) and SAFE_NAME.fullmatch(name), "video_localization_unsafe_path")
    path = root / name
    require(not path.is_symlink() and path.parent.resolve() == root, "video_localization_unsafe_path")
    if existing:
        regular(path)
    else:
        require(not path.exists(), "video_localization_output_exists")
    return path


def disk(root):
    total = 0
    entries = list(root.iterdir())
    require(len(entries) <= 6000, "video_localization_scratch_limit")
    for path in entries:
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and not path.is_symlink() and info.st_nlink == 1, "video_localization_unsafe_path")
        total += info.st_size
    require(total <= SCRATCH_LIMIT, "video_localization_scratch_limit")
    require(shutil.disk_usage(root).free >= RESERVE, "video_localization_disk_reserve")


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024**2), b""):
            result.update(block)
    return result.hexdigest()


def write_json(root, name, value, limit=JSON_LIMIT):
    target = local(root, name, False)
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf8")
    require(len(raw) <= limit, "video_localization_response_limit")
    with target.open("xb") as stream:
        os.chmod(target, 0o600)
        stream.write(raw)
    return name


def command(args, timeout=120, output_limit=JSON_LIMIT, env=None):
    with tempfile.TemporaryFile() as output:
        child = subprocess.Popen(args, shell=False, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL, env=env)
        started = time.monotonic()
        try:
            while child.poll() is None:
                require(time.monotonic() - started < timeout, "video_localization_timeout")
                require(os.fstat(output.fileno()).st_size <= output_limit, "video_localization_response_limit")
                time.sleep(.05)
            require(child.returncode == 0, "video_localization_media_failed")
            require(os.fstat(output.fileno()).st_size <= output_limit, "video_localization_response_limit")
            output.seek(0)
            return output.read(output_limit)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()


def ffinput(ffmpeg, source):
    # Protocol allowlist prevents media playlists/references from opening URLs.
    return [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "file,pipe", "-i", str(source)]


def ratio(value):
    try:
        numerator, denominator = str(value).split("/")
        return float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        return 0


def probe(source, config, input_limit=True):
    size = regular(source, FILE_LIMIT if input_limit else OUTPUT_LIMIT)
    with source.open("rb") as stream:
        header = stream.read(32)
    require(len(header) >= 12 and header[4:8] == b"ftyp", "video_localization_invalid_media")
    data = json.loads(command([config["ffprobe"], "-v", "error", "-protocol_whitelist", "file,pipe", "-show_streams", "-show_format", "-of", "json", str(source)]))
    streams = data.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    require(len(videos) == 1 and len(audios) <= 1, "video_localization_unsupported_streams")
    require("mp4" in data.get("format", {}).get("format_name", "").split(","), "video_localization_invalid_media")
    v = videos[0]
    width, height = int(v.get("width", 0)), int(v.get("height", 0))
    rotation = float(v.get("tags", {}).get("rotate", 0))
    for side in v.get("side_data_list", []):
        rotation = float(side.get("rotation", rotation))
    require(finite(rotation) and rotation % 90 == 0, "video_localization_unsupported_rotation")
    if abs(rotation) % 180 == 90:
        width, height = height, width
    require(v.get("sample_aspect_ratio", "1:1") in ("1:1", "N/A", "0:1"), "video_localization_unsupported_aspect")
    duration = float(v.get("duration", data.get("format", {}).get("duration", 0)))
    fps = ratio(v.get("avg_frame_rate"))
    require(finite(duration) and 0 < duration <= 301, "video_localization_duration_limit")
    require(finite(fps) and 0 < fps <= 120.01, "video_localization_invalid_framerate")
    require(width > 0 and height > 0, "video_localization_invalid_media")
    if input_limit:
        require(duration <= 300 and min(width, height) <= 1080 and max(width, height) <= 1920, "video_localization_input_limit")
    return {"durationMs": round(duration * 1000, 6), "width": width, "height": height, "fps": fps,
            "fpsRatio": v.get("avg_frame_rate"), "hasAudio": bool(audios), "videoCodec": v.get("codec_name"),
            "audioCodec": audios[0].get("codec_name") if audios else None, "bytes": size}


def frame_manifest(source, config, media):
    data = json.loads(command([config["ffprobe"], "-v", "error", "-protocol_whitelist", "file,pipe", "-select_streams", "v:0", "-show_frames",
                               "-show_entries", "frame=best_effort_timestamp_time,pkt_duration_time", "-of", "json", str(source)], timeout=300))
    rows = data.get("frames", [])
    require(0 < len(rows) <= MAX_FRAMES, "video_localization_frame_limit")
    origin = float(rows[0].get("best_effort_timestamp_time", "nan")) * 1000
    require(finite(origin), "video_localization_invalid_timestamps")
    frames = []
    for index, row in enumerate(rows):
        pts = float(row.get("best_effort_timestamp_time", "nan")) * 1000 - origin
        require(finite(pts) and pts >= 0 and (not frames or pts > frames[-1]["ptsMs"]), "video_localization_invalid_timestamps")
        frames.append({"frameId": f"f{index:06d}", "index": index, "ptsMs": round(pts, 6)})
    for index, frame in enumerate(frames):
        duration = frames[index + 1]["ptsMs"] - frame["ptsMs"] if index + 1 < len(frames) else float(rows[index].get("pkt_duration_time", 1 / media["fps"])) * 1000
        require(finite(duration) and 0 < duration <= 5000, "video_localization_invalid_timestamps")
        frame["durationMs"] = round(duration, 6)
    require(frames[-1]["ptsMs"] + frames[-1]["durationMs"] <= 300_100, "video_localization_duration_limit")
    # Preserve the actual final packet/frame duration, not a rounded fps clock.
    media["durationMs"] = round(frames[-1]["ptsMs"] + frames[-1]["durationMs"], 6)
    media["constantFrameRate"] = all(abs(frame["durationMs"] - 1000 / media["fps"]) <= max(.1, 2 / media["fps"]) for frame in frames)
    return {"protocol": PROTOCOL, "source": source.name, "sourceSha256": digest(source), "sourceStartPtsMs": origin, "media": media, "frames": frames}


def load_manifest(root, source):
    path = local(root, "manifest.json")
    regular(path, JSON_LIMIT)
    data = json.loads(path.read_text("utf8"))
    require(data.get("protocol") == PROTOCOL and data.get("source") == source.name and data.get("sourceSha256") == digest(source), "video_localization_source_changed")
    return data


def raw_frames(source, config, width, height, timeout=900):
    import numpy as np
    args = ffinput(config["ffmpeg"], source) + ["-map", "0:v:0", "-an", "-vf", f"scale={width}:{height}", "-fps_mode", "passthrough", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"]
    child = subprocess.Popen(args, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    timer = threading.Timer(timeout, child.kill)
    timer.daemon = True
    timer.start()
    size = width * height * 3
    try:
        while True:
            block = bytearray()
            while len(block) < size:
                chunk = child.stdout.read(size - len(block))
                if not chunk:
                    break
                block.extend(chunk)
            if not block:
                break
            require(len(block) == size, "video_localization_decode_failed")
            yield np.frombuffer(block, dtype=np.uint8).reshape((height, width, 3)).copy()
        require(child.wait(timeout=10) == 0, "video_localization_decode_failed")
    finally:
        timer.cancel()
        if child.poll() is None:
            child.kill()
        child.wait()
        child.stdout.close()


def pixel_box(box, media):
    require(isinstance(box, dict) and all(finite(box.get(k)) for k in ("x", "y", "width", "height")))
    x, y, w, h = (box[k] for k in ("x", "y", "width", "height"))
    require(0 <= x < 1 and 0 <= y < 1 and w > 0 and h > 0 and x + w <= 1.000001 and y + h <= 1.000001)
    left, top = int(x * media["width"]), int(y * media["height"])
    right, bottom = min(media["width"], math.ceil((x + w) * media["width"])), min(media["height"], math.ceil((y + h) * media["height"]))
    require(right - left >= 4 and bottom - top >= 4 and (right-left)*(bottom-top) <= media["width"] * media["height"] * .4, "video_localization_region_limit")
    return [left, top, right - left, bottom - top]


def infer_glyph_ink(rgb):
    """Use sharp, recurring stroke interiors; busy or mixed-color regions need a reviewer."""
    import cv2
    import numpy as np
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    bright = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kernel)
    dark = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, kernel)
    options = []
    for contrast in (bright, dark):
        strokes = contrast >= max(40, np.percentile(contrast, 85))
        if np.count_nonzero(strokes) < max(12, gray.size * .006):
            continue
        pixels = rgb[strokes].astype(np.int16)
        # Select a repeated ink color, then measure its support across the ROI.
        bins, counts = np.unique(pixels // 32, axis=0, return_counts=True)
        seed = bins[np.argmax(counts)] * 32 + 16
        near = np.max(np.abs(pixels - seed), axis=1) <= 40
        if near.mean() < .52:
            continue
        ink = np.median(pixels[near], axis=0).astype(int)
        distance = np.max(np.abs(rgb.astype(np.int16) - ink), axis=2)
        glyphs = (distance <= 40) & strokes
        yy, xx = np.nonzero(glyphs)
        if len(xx) < 12 or np.ptp(xx) < rgb.shape[1] * .22 or np.ptp(yy) < rgb.shape[0] * .16:
            continue
        # A solid background or wide decoration isn't a glyph sequence.
        if not .005 <= np.mean(distance <= 40) <= .42:
            continue
        options.append((int(np.sum(contrast[glyphs], dtype=np.int64)), ink))
    options.sort(key=lambda option: option[0], reverse=True)
    require(options and (len(options) == 1 or options[0][0] >= 1.45 * options[1][0]), "video_localization_mask_uncertain")
    chosen = options[0][1]
    return "#" + "".join(f"{channel:02x}" for channel in chosen)


def contrast_strokes(rgb, polarity):
    """Select fine localized strokes when ink is translucent or has a contrasting outline."""
    import cv2
    import numpy as np
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    require(min(w,h) >= 4 and float(np.std(gray)) >= 3, "video_localization_mask_uncertain")
    gx = cv2.Sobel(gray,cv2.CV_32F,1,0)
    gy = cv2.Sobel(gray,cv2.CV_32F,0,1)
    edge_x, edge_y = np.abs(gx).mean(), np.abs(gy).mean()
    fine_edges = np.abs(cv2.Laplacian(gray,cv2.CV_32F,ksize=3)).mean()
    # Camera blur and the straight edges of an opening panel can yield contrast
    # but lack well-defined strokes in both directions. Do not erase those edges.
    require(min(edge_x,edge_y) >= .25*max(edge_x,edge_y) and
            fine_edges >= max(16.,.47*np.hypot(gx,gy).mean()), "video_localization_mask_uncertain")
    kernel_size = min(11, max(3, (h//8)*2+1))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size,kernel_size))
    contrast = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT if polarity == "light" else cv2.MORPH_BLACKHAT, kernel)
    # Set the cutoff from local stroke-to-background contrast, not global RGB color:
    # translucent strokes show different absolute colors over different backgrounds.
    otsu = cv2.threshold(contrast,0,255,cv2.THRESH_BINARY | cv2.THRESH_OTSU)[0]
    binary = (contrast >= max(7.,otsu)).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary,8)
    keep = np.zeros(count,dtype=bool)
    for idx,(_,_,bw,bh,area) in enumerate(stats[1:],1):
        if area < 2 or area > .3*w*h or bw > .65*w and bh < .12*h or bh > .8*h and bw < .045*w:
            continue
        if area > 25 and area/(bw*bh) > .9 and min(bw,bh) > max(4,.20*h):
            continue
        keep[idx] = True
    strokes = keep[labels]
    yy,xx = np.nonzero(strokes)
    require(keep.sum() >= 2 and len(xx) >= 8 and .008 <= len(xx)/(w*h) <= .46 and
            np.ptp(xx) >= w*.24 and np.ptp(yy) >= h*.16, "video_localization_mask_uncertain")
    # Broad blocks and slowly varying gradients lack the alternating edges in type.
    ink_width = cv2.distanceTransform(strokes.astype(np.uint8),cv2.DIST_L2,3)[strokes]
    require(np.percentile(ink_width,90) <= max(3.5,h*.15), "video_localization_mask_uncertain")
    return strokes.astype(np.uint8)*255,contrast


def glyph_mask(rgb, policy):
    import cv2
    import numpy as np
    mode = policy.get("mode", "light")
    require(mode in ("light", "dark", "color", "strokes", "verified_clusters"), "video_localization_invalid_mask")
    if mode == "light":
        mask = (rgb.min(axis=2) > 155) & ((rgb.max(axis=2).astype(float) - rgb.min(axis=2)) < 100)
    elif mode == "dark":
        mask = rgb.max(axis=2) < 90
    elif mode == "verified_clusters":
        mask = dense_glyphs(rgb, policy["glyphCount"], policy["polarity"])
    elif mode == "strokes":
        polarity = policy.get("polarity")
        require(polarity in ("light","dark"), "video_localization_invalid_mask")
        mask = contrast_strokes(rgb,polarity)[0] != 0
    else:
        color = policy.get("color", "")
        tolerance = policy.get("tolerance", 55)
        require(isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color) and finite(tolerance) and 10 <= tolerance <= 100, "video_localization_invalid_mask")
        channels = np.array([int(color[i:i+2], 16) for i in (1, 3, 5)])
        mask = np.max(np.abs(rgb.astype(float) - channels), axis=2) <= tolerance
    mask = mask.astype(np.uint8) * 255
    density = float(np.count_nonzero(mask)) / mask.size
    require(.004 <= density <= .55, "video_localization_mask_uncertain")
    dilation = policy.get("dilatePx", 1)
    require(isinstance(dilation, int) and 0 <= dilation <= 3, "video_localization_invalid_mask")
    if dilation:
        mask = cv2.dilate(mask, np.ones((dilation * 2 + 1, dilation * 2 + 1), np.uint8))
    # Inpainting never touches the unobserved outside of the approved text box.
    mask[0, :] = mask[-1, :] = mask[:, 0] = mask[:, -1] = 0
    require(np.count_nonzero(mask) / mask.size <= .7, "video_localization_mask_uncertain")
    return mask


def track_step(previous, current, box, policy):
    import cv2
    import numpy as np
    x, y, w, h = box
    patch = previous[y:y+h, x:x+w]
    mask = glyph_mask(patch, policy)
    before = cv2.cvtColor(previous, cv2.COLOR_RGB2GRAY)
    after = cv2.cvtColor(current, cv2.COLOR_RGB2GRAY)
    margin = max(4, min(24, round(max(current.shape[:2]) * .025)))
    sx, sy = max(0, x-margin), max(0, y-margin)
    ex, ey = min(current.shape[1], x+w+margin), min(current.shape[0], y+h+margin)
    template = before[y:y+h, x:x+w]
    require(float(np.std(template)) >= 5, "video_localization_tracking_uncertain")
    scores = cv2.matchTemplate(after[sy:ey, sx:ex], template, cv2.TM_CCOEFF_NORMED)
    _, score, _, point = cv2.minMaxLoc(scores)
    dx, dy = sx + point[0] - x, sy + point[1] - y
    require(math.isfinite(score) and score >= .86, "video_localization_tracking_uncertain")
    features = cv2.goodFeaturesToTrack(template, maxCorners=80, qualityLevel=.03, minDistance=3, mask=mask)
    flow_ok = False
    if features is not None and len(features) >= 4:
        features += np.array([[[x, y]]], dtype=np.float32)
        nxt, status, _ = cv2.calcOpticalFlowPyrLK(before, after, features, None, winSize=(15, 15), maxLevel=2)
        if nxt is not None:
            back, back_status, _ = cv2.calcOpticalFlowPyrLK(after, before, nxt, None, winSize=(15, 15), maxLevel=2)
            if back is not None:
                valid = (status.reshape(-1) == 1) & (back_status.reshape(-1) == 1) & (np.linalg.norm(back-features, axis=2).reshape(-1) < 1.2)
                if int(valid.sum()) >= 4 and float(valid.mean()) >= .65:
                    movement = (nxt-features).reshape(-1, 2)[valid]
                    median = np.median(movement, axis=0)
                    spread = np.median(np.linalg.norm(movement-median, axis=1))
                    flow_ok = spread < 1.5 and np.linalg.norm(median - np.array([dx, dy])) < 2.5
    # Without stable text features, accept only nearly unchanged, high-score patches.
    require(flow_ok or (score >= .965 and abs(dx) <= 1 and abs(dy) <= 1), "video_localization_tracking_uncertain")
    result = [x+dx, y+dy, w, h]
    glyph_mask(current[y+dy:y+dy+h, x+dx:x+dx+w], policy)
    return result, round(score, 5), "template_flow" if flow_ok else "stationary_template"


def source_boundary_index(timestamps, instant):
    """First frame at/after a boundary; adding PTS and duration can round above it."""
    pos = bisect.bisect_left(timestamps, instant)
    if pos:
        previous = timestamps[pos-1]
        if instant-previous <= 2*max(math.ulp(instant),math.ulp(previous)):
            return pos-1
    return pos


def validate_segments(segments, manifest, geometry_issues=None, preserve=False):
    require(isinstance(segments, list) and 0 < len(segments) <= MAX_AUTOMATIC_SEGMENTS, "video_localization_region_limit")
    seen = set()
    media = manifest["media"]
    frames = {f["frameId"]: f for f in manifest["frames"]}
    timestamps = [frame["ptsMs"] for frame in manifest["frames"]]
    result = []
    for segment in segments:
        require(isinstance(segment, dict) and isinstance(segment.get("id"), str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", segment["id"]) and segment["id"] not in seen)
        seen.add(segment["id"])
        require(all(isinstance(segment.get(k), str) and len(segment[k]) <= 2000 for k in ("sourceText", "translation")))
        require(segment["translation"].strip() and not any(ord(c) < 32 and c != "\n" for c in segment["translation"]), "video_localization_invalid_text")
        start, end = segment.get("startMs"), segment.get("endMs")
        require(finite(start) and finite(end) and 0 <= start < end <= media["durationMs"] + .1)
        first = source_boundary_index(timestamps, start)
        last = source_boundary_index(timestamps, end) - 1
        require(first <= last, "video_localization_unknown_frame")
        keyframes = segment.get("keyframes")
        require(isinstance(keyframes, list) and 0 < len(keyframes) <= len(manifest["frames"]))
        anchors = {}
        seen_keyframes = set()
        invalid = []
        for keyframe in keyframes:
            require(isinstance(keyframe, dict) and keyframe.get("frameId") in frames and finite(keyframe.get("confidence")) and .8 <= keyframe["confidence"] <= 1, "video_localization_keyframe_uncertain")
            frame = frames[keyframe["frameId"]]
            require(first <= frame["index"] <= last and frame["index"] not in seen_keyframes, "video_localization_unknown_frame")
            seen_keyframes.add(frame["index"])
            try:
                anchors[frame["index"]] = pixel_box(keyframe["box"], media)
            except BridgeError as error:
                if geometry_issues is None or not segment.get("adaptiveBounds") or error.code != "video_localization_region_limit":
                    raise
                invalid.append({"segmentId":segment["id"],"frameId":frame["frameId"],"ptsMs":frame["ptsMs"],
                                "code":error.code,"blocking":True})
        require(first in seen_keyframes, "video_localization_boundary_keyframe_required")
        if invalid:
            geometry_issues.extend(invalid)
            if not preserve:
                continue
        policy = segment.get("mask", {"mode": "light", "dilatePx": 1})
        require(isinstance(policy, dict))
        verified = {frames[key["frameId"]]["index"] for key in keyframes if key.get("visualVerified") is True}
        result.append({**segment, "mask": policy, "anchors": anchors, "_verified": verified,
                       "_invalidAnchors": {int(i["frameId"][1:]) for i in invalid},
                       "first": first, "last": last, "frames": []})
    return result


PRESERVE = "preserve_unresolved"
RECOVERABLE_GEOMETRY = {"video_localization_keyframe_uncertain","video_localization_tracking_uncertain",
                        "video_localization_mask_uncertain","video_localization_text_overflow",
                        "video_localization_model_uncertain","video_localization_region_limit"}


def preservation_ranges(manifest, spans):
    """Merge adjacent intervals with the same cause, retaining the source PTS clock."""
    frames = manifest["frames"]
    merged = []
    for segment,first,last,code in sorted(spans):
        require(isinstance(segment,str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,64}",segment))
        require(isinstance(code,str) and re.fullmatch(r"video_localization_[a-z_]{1,60}",code))
        require(type(first) is int and type(last) is int and 0 <= first <= last < len(frames))
        if merged and merged[-1]["segmentId"]==segment and merged[-1]["code"]==code and first<=merged[-1]["endFrame"]+1:
            last = max(last,merged[-1]["endFrame"])
            merged[-1].update(endFrame=last,endMs=frames[last]["ptsMs"]+frames[last]["durationMs"])
        else:
            merged.append(dict(segmentId=segment,startFrame=first,endFrame=last,startMs=frames[first]["ptsMs"],
                               endMs=frames[last]["ptsMs"]+frames[last]["durationMs"],code=code))
    require(len(merged)<=36000,"video_localization_response_limit")
    return merged


def record_untracked(runs,index,reason):
    if runs and runs[-1][1]+1==index and runs[-1][2]==reason:
        runs[-1][1]=index
    else:
        runs.append([index,index,reason])


def overlapping_text_frames(segments):
    """Two incompatible replacements cannot occupy the same source glyph area."""
    positions=defaultdict(list)
    for segment in segments:
        for frame in segment["frames"]:
            positions[frame["frameId"]].append((segment,frame["box"]))
    conflicts=defaultdict(set)
    for fid,rows in positions.items():
        # Sort by x before considering y. Distant labels never need comparison.
        rows.sort(key=lambda row:row[1][0])
        for i,(left,(x,y,w,h)) in enumerate(rows):
            for j in range(i+1,len(rows)):
                right,(xx,yy,ww,hh)=rows[j]
                if xx>=x+w:
                    break
                intersection=(min(x+w,xx+ww)-xx)*max(0,min(y+h,yy+hh)-max(y,yy))
                identical=((x,y,w,h)==(xx,yy,ww,hh) and left["sourceText"]==right["sourceText"] and left["translation"]==right["translation"]
                           and left.get("style")==right.get("style"))
                if intersection*5 >= 3*min(w*h,ww*hh) and not identical:
                    index=int(fid[1:])
                    conflicts[left["id"]].add(index)
                    conflicts[right["id"]].add(index)
    return conflicts


def track(root, payload, config, *, fixed_regions=False):
    source = local(root, payload["source"])
    manifest = load_manifest(root, source)
    issues, previous, count = [], None, 0
    adaptive = payload.get("adaptiveSourceBounds") is True and not fixed_regions
    preserve = payload.get("uncertaintyPolicy") == PRESERVE
    require(not preserve or adaptive and payload.get("validateLayout") is True and "previewWindow" not in payload)
    excluded = defaultdict(list)
    spans = []
    if preserve:
        rows = payload.get("unresolvedRanges", [])
        require(isinstance(rows,list) and len(rows)<=36000)
        for row in rows:
            require(isinstance(row,dict) and all(k in row for k in ("segmentId","startFrame","endFrame","code")))
            spans.append(tuple(row[k] for k in ("segmentId","startFrame","endFrame","code")))
        for row in preservation_ranges(manifest,spans):
            excluded[row["segmentId"]].append((row["startFrame"],row["endFrame"]))
        require(payload.get("segments"), "video_localization_no_changes")
    segments = validate_segments(payload.get("segments"), manifest, issues if adaptive else None, preserve)
    if preserve:
        issues.clear()  # Invalid anchors are recorded over their affected frame interval below.
    blocked = set()
    preview = payload.get("previewWindow")
    limit = len(manifest["frames"])
    if preview is not None:
        require(isinstance(preview, dict) and finite(preview.get("startMs")) and finite(preview.get("durationMs")) and
                0 <= preview["startMs"] and 1000 <= preview["durationMs"] <= 10000 and
                preview["startMs"] + preview["durationMs"] <= manifest["media"]["durationMs"] + .1, "video_localization_invalid_preview")
        limit = bisect.bisect_left([frame["ptsMs"] for frame in manifest["frames"]], preview["startMs"] + preview["durationMs"])
    if manifest.get("discoveryComplete") is not True:
        # An extracted frame is not proof that a model or human inspected it.
        reviewed = payload.get("reviewedFrameIds", [])
        require(isinstance(reviewed, list) and len(reviewed) <= MAX_FRAMES)
        missing = set(manifest.get("discoveryCoverage", {}).get("missingFrameIds", [])) - set(reviewed)
        if preview is not None:
            missing &= {frame["frameId"] for frame in manifest["frames"][:limit] if frame["ptsMs"] >= preview["startMs"]}
        if missing:
            issues.append({"segmentId": "discovery", "code": "video_localization_discovery_incomplete", "blocking": True,
                           "missingFrames": len(missing)})
    for segment in segments:
        if segment.get("manualReview"):
            blocked.add(segment["id"])
            issues.append({"segmentId": segment["id"], "code": "video_localization_model_uncertain", "blocking": True})
    media = manifest["media"]
    if adaptive:
        unverified = prepare_stroke_anchors(source, config, manifest, segments) or set()
        for segment in segments:
            if segment["id"] in unverified or not segment["anchors"]:
                blocked.add(segment["id"])
                issues.append({"segmentId":segment["id"],"frameId":manifest["frames"][segment["first"]]["frameId"],
                               "code":"video_localization_mask_uncertain","blocking":True})
            keys = sorted(segment["anchors"])
            segment["_next"] = dict(zip(keys, keys[1:]))
            if preserve:
                segment["_lead"] = segment["first"]
                segment["_missing"] = []
                segment["_layoutMissing"] = []
    starts, ends = defaultdict(list), defaultdict(list)
    for segment in segments:
        starts[segment.get("_lead", segment["first"])].append(segment)
        ends[segment["last"] + 1].append(segment)
    active = {}
    for index, rgb in enumerate(raw_frames(source, config, media["width"], media["height"], timeout=1800)):
        if index >= limit:
            break
        require(index < len(manifest["frames"]), "video_localization_frame_count_changed")
        frame = manifest["frames"][index]
        for ended in ends.get(index, ()):
            active.pop(ended["id"], None)
            ended.pop("_reference", None)
            ended.pop("_leadRef", None)
        for started in starts.get(index, ()):
            active[started["id"]] = started
        strokes = stroke_image(rgb) if adaptive and active else None
        for segment in active.values():
            if preserve:
                if segment.get("manualReview"):
                    continue
                if any(lo<=index<=hi for lo,hi in excluded.get(segment["id"],())):
                    segment["_waitForAnchor"] = "video_localization_keyframe_uncertain"
                    continue
                if index in segment["_invalidAnchors"]:
                    segment["_waitForAnchor"] = "video_localization_region_limit"
                if segment.get("_waitForAnchor"):
                    if index not in segment["anchors"]:
                        record_untracked(segment["_missing"],index,segment["_waitForAnchor"])
                        continue
                    for key in ("_waitForAnchor","_reference","_box","_anchor","_ink"):
                        segment.pop(key,None)
            if segment["id"] in blocked:
                continue
            try:
                if adaptive and segment.get("adaptiveBounds"):
                    if preserve and index < segment["_clearStart"]:
                        record_untracked(segment["_missing"],index,"video_localization_mask_uncertain")
                        continue
                    source_frame(segment,index,frame["frameId"],rgb,strokes,previous)
                    if preserve and (not segment["frames"] or segment["frames"][-1]["frameId"]!=frame["frameId"]):
                        segment["_waitForAnchor"] = "video_localization_tracking_uncertain"
                        record_untracked(segment["_missing"],index,segment["_waitForAnchor"])
                    continue
                if fixed_regions or index in segment["anchors"]:
                    box = segment["anchors"][segment["first"] if fixed_regions else index]
                    x, y, w, h = box
                    if segment["mask"].get("mode") == "auto":
                        ink = infer_glyph_ink(rgb[y:y+h, x:x+w])
                        segment["mask"] = {"mode": "color", "color": ink, "tolerance": 40, "dilatePx": 1}
                        segment["style"] = {**segment.get("style", {}), "color": ink}
                    # A model-authored bounded region program may construct its
                    # own mask; geometry and text layout are still validated.
                    if segment["id"] not in config.get("region_program_segments", ()):
                        glyph_mask(rgb[y:y+h, x:x+w], segment["mask"])
                    score, method = 1.0, "manual_fixed_box" if fixed_regions else "model_keyframe"
                else:
                    require(previous is not None and segment["frames"], "video_localization_tracking_uncertain")
                    box, score, method = track_step(previous, rgb, segment["frames"][-1]["box"], segment["mask"])
                segment["frames"].append({"frameId": frame["frameId"], "box": box, "score": score, "method": method})
            except BridgeError as error:
                if preserve and error.code in RECOVERABLE_GEOMETRY:
                    segment["_waitForAnchor"] = error.code
                    record_untracked(segment["_missing"],index,error.code)
                    continue
                blocked.add(segment["id"])
                issues.append({"segmentId": segment["id"], "frameId": frame["frameId"], "ptsMs": frame["ptsMs"], "code": error.code, "blocking": True})
        previous = rgb
        count += 1
        if count % 256 == 0 or count == limit:
            # Bound in-memory rows before serializing the private tracking ledger.
            require(sum(len(s["frames"]) for s in segments) <= 240_000, "video_localization_response_limit")
    require(count == limit, "video_localization_frame_count_changed")
    if payload.get("validateLayout") is True:
        for segment in segments:
            if segment["id"] in blocked:
                continue
            if not segment["frames"]:
                frame = manifest["frames"][segment["first"]]
                issues.append({"segmentId":segment["id"],"frameId":frame["frameId"],"ptsMs":frame["ptsMs"],
                               "code":"video_localization_tracking_uncertain","blocking":True})
                continue
            # Test actual tracked sizes in source order. No font shrinking, clipping
            # or omission that is not also allowed in the final rendering algorithm.
            checked, overflow = set(), set()
            for tracked in segment["frames"]:
                box = tracked["box"]
                dimensions = tuple(box[2:])
                if dimensions in checked:
                    continue
                checked.add(dimensions)
                try:
                    text_layer(segment, box, config, layout_only=True)
                except BridgeError as error:
                    if error.code != "video_localization_text_overflow":
                        raise
                    if preserve:
                        overflow.add(dimensions)
                        continue
                    index = int(tracked["frameId"][1:])
                    issues.append({"segmentId":segment["id"],"frameId":tracked["frameId"],"ptsMs":manifest["frames"][index]["ptsMs"],
                                   "code":error.code,"blocking":True})
                    break
            if preserve and overflow:
                for tracked in segment["frames"]:
                    if tuple(tracked["box"][2:]) in overflow:
                        record_untracked(segment["_layoutMissing"],int(tracked["frameId"][1:]),"video_localization_text_overflow")
                segment["frames"] = [row for row in segment["frames"] if tuple(row["box"][2:]) not in overflow]
    if preserve:
        for issue in issues:
            if issue["code"] not in RECOVERABLE_GEOMETRY:
                raise BridgeError(issue["code"])
        reasons = {issue["segmentId"]:issue["code"] for issue in issues}
        issues = []
        conflicts=overlapping_text_frames(segments)
        for segment in segments:
            sid=segment["id"]
            disputed=conflicts.get(sid,set())
            if disputed:
                segment["frames"]=[row for row in segment["frames"] if int(row["frameId"][1:]) not in disputed]
            drawn = {int(row["frameId"][1:]) for row in segment["frames"]}
            missing=sorted(segment["_missing"]+segment["_layoutMissing"])
            pos=0
            last_code = None
            for i in range(segment["first"],segment["last"]+2):
                while pos<len(missing) and i>missing[pos][1]:
                    pos+=1
                code = None
                if i<=segment["last"] and i not in drawn and not any(lo<=i<=hi for lo,hi in excluded.get(sid,())):
                    if i in disputed:
                        code="video_localization_tracking_uncertain"
                    elif pos<len(missing) and i>=missing[pos][0]:
                        code=missing[pos][2]
                    else:
                        code=reasons.get(sid,"video_localization_tracking_uncertain")
                if code != last_code:
                    if last_code is not None:
                        spans.append((sid,lo_missing,i-1,last_code))
                    if code is not None:
                        lo_missing=i
                    last_code=code
        segments=[segment for segment in segments if segment["frames"] and not segment.get("manualReview")]
        require(segments,"video_localization_no_changes")
    preservation = {"uncertaintyPolicy":PRESERVE,"preservedRanges":preservation_ranges(manifest,spans)} if preserve else {}
    for segment in segments:
        for key in list(segment):
            if key.startswith("_"):
                del segment[key]
        del segment["anchors"]
        del segment["first"]
        del segment["last"]
    name = payload.get("output", "tracking.json")
    require(isinstance(name, str) and name.endswith(".json"))
    write_json(root, name, {**preservation,"protocol": PROTOCOL, "sourceSha256": manifest["sourceSha256"], "segments": segments, "issues": issues, "readyForRender": not issues,
                            "qualityStatus": "manual_review_required", "maskMethod": "tight_glyph_mask_opencv_inpaint",
                            "trackingMethod": "manual_fixed_box" if fixed_regions else "conservative_template_optical_flow"}, limit=TRACKING_JSON_LIMIT)
    return {**preservation,"tracking": name, "issues": issues, "readyForRender": not issues, "qualityStatus": "manual_review_required"}


def stroke_image(rgb):
    import cv2
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    # Signed high frequencies retain the glyph outlines on moving or fading backgrounds.
    return cv2.addWeighted(gray, 1, cv2.GaussianBlur(gray, (0, 0), 1.65), -1, 128)


def match_strokes(reference, strokes, box):
    import cv2
    import numpy as np
    x, y, w, h = box
    if reference.shape != (h, w):
        reference = cv2.resize(reference, (w, h))
    if np.std(reference) < 2:
        return 0, box
    margin = max(8, min(36, round(h*.4)))
    sx, sy = max(0, x-margin), max(0, y-margin)
    region = strokes[sy:min(strokes.shape[0], y+h+margin), sx:min(strokes.shape[1], x+w+margin)]
    _, score, _, point = cv2.minMaxLoc(cv2.matchTemplate(region, reference, cv2.TM_CCOEFF_NORMED))
    return (score if finite(score) else 0), [sx+point[0], sy+point[1], w, h]


def anchor_ink(patch):
    import numpy as np
    try:
        ink = infer_glyph_ink(patch)
        policy = {"mode":"color", "color":ink, "tolerance":40, "dilatePx":1}
        glyph_mask(patch, policy)
        return policy
    except BridgeError as error:
        if error.code != "video_localization_mask_uncertain":
            raise
    candidates = []
    for polarity in ("light","dark"):
        try:
            mask,contrast = contrast_strokes(patch,polarity)
            support = mask != 0
            # Favor a stroke palette recurring throughout the region over random edges.
            pixels = patch[support]
            _,counts = np.unique(pixels//48,axis=0,return_counts=True)
            recurrence = np.sort(counts)[-3:].sum()/len(pixels)
            strength = float(np.mean(contrast[support])) * recurrence * np.sqrt(len(pixels))
            if recurrence < .28 or strength <= 0:
                continue
            channels = np.median(pixels[contrast[support] >= np.median(contrast[support])],axis=0)
            color = "#" + "".join(f"{round(float(c)):02x}" for c in channels)
            policy = {"mode":"strokes","polarity":polarity,"color":color,"dilatePx":1}
            try:
                glyph_mask(patch,policy)
            except BridgeError:
                # Tight small labels can already include their antialiased edges.
                policy["dilatePx"] = 0
                glyph_mask(patch,policy)
            candidates.append((strength,policy))
        except BridgeError as error:
            if error.code != "video_localization_mask_uncertain":
                raise
    require(candidates,"video_localization_mask_uncertain")
    return max(candidates,key=lambda item:item[0])[1]


def dense_glyphs(rgb, count, polarity):
    """Bounded high-contrast connected glyphs for an already verified single line of CJK."""
    import cv2
    import numpy as np
    require(type(count) is int and 2 <= count <= 10 and polarity in ("light","dark"), "video_localization_invalid_mask")
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    height, width = gray.shape
    mode = cv2.THRESH_BINARY if polarity == "light" else cv2.THRESH_BINARY_INV
    binary = cv2.threshold(gray, 0, 255, mode | cv2.THRESH_OTSU)[1]
    foreground = binary != 0
    require(foreground.any() and not foreground.all(), "video_localization_mask_uncertain")
    contrast = abs(float(np.median(gray[foreground])) - float(np.median(gray[~foreground])))
    require(contrast >= 85, "video_localization_mask_uncertain")
    _, labels, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
    glyphs = []
    for idx, (x,y,w,h,area) in enumerate(stats[1:], 1):
        if (h >= max(6,height*.27) and .52*h <= w <= 1.55*h and .20 <= area/(w*h) <= .82
                and min(x,y) >= 2 and x+w <= width-2 and y+h <= height-2):
            glyphs.append((int(x),int(y),int(w),int(h),idx))
    require(len(glyphs) == count, "video_localization_mask_uncertain")
    glyphs.sort()
    typical = np.median([h for x,y,w,h,idx in glyphs])
    require(all(abs(h-typical) <= .20*typical for x,y,w,h,idx in glyphs)
            and np.ptp([y+h/2 for x,y,w,h,idx in glyphs]) <= .15*typical
            and all(-.05*typical <= right[0]-left[0]-left[2] <= .60*typical for left,right in zip(glyphs,glyphs[1:])),
            "video_localization_mask_uncertain")
    return np.isin(labels, [row[-1] for row in glyphs])


def measured_ink(rgb, segment, index):
    import numpy as np
    x,y,w,h = segment["anchors"][index]
    try:
        return anchor_ink(rgb[y:y+h,x:x+w])
    except BridgeError as error:
        text = segment["sourceText"].strip()
        if (error.code != "video_localization_mask_uncertain" or index not in segment.get("_verified", set())
                or not re.fullmatch("[\u3400-\u9fff]{2,10}",text)):
            raise
    # A small OCR crop can cut through a blurred border stroke. The halo is
    # searched for the actual ends of the characters; background is never a mask.
    pad = min(14, round(.2*h))
    left, top = max(0,x-pad), max(0,y-pad)
    patch = rgb[top:min(rgb.shape[0],y+h+pad),left:min(rgb.shape[1],x+w+pad)]
    accepted = []
    for polarity in ("light", "dark"):
        try:
            mask = dense_glyphs(patch, len(text), polarity)
            ys, xs = np.nonzero(mask)
            gx, gy = left+int(xs.min()), top+int(ys.min())
            right, bottom = left+int(xs.max())+1, top+int(ys.max())+1
            area = (right-gx)*(bottom-gy)
            inside = max(0,min(right,x+w)-max(gx,x))*max(0,min(bottom,y+h)-max(gy,y))
            require(inside >= .82*area and area >= .22*w*h, "video_localization_mask_uncertain")
            gutter = min(12,max(3,round((bottom-gy)*.20)))
            a,b = max(0,gx-gutter),max(0,gy-gutter)
            c,d = min(rgb.shape[1],right+gutter),min(rgb.shape[0],bottom+gutter)
            color = np.median(patch[mask],axis=0).astype(int)
            policy = {"mode":"verified_clusters","glyphCount":len(text),"polarity":polarity,
                      "color":"#"+ "".join(f"{channel:02x}" for channel in color),"dilatePx":1}
            glyph_mask(rgb[b:d,a:c], policy)
            accepted.append(([a,b,c-a,d-b],policy))
        except BridgeError as error:
            if error.code != "video_localization_mask_uncertain":
                raise
    require(len(accepted) == 1, "video_localization_mask_uncertain")
    segment["anchors"][index], policy = accepted[0]
    return policy


def prepare_stroke_anchors(source, config, manifest, segments):
    import cv2
    from collections import defaultdict
    sampled = manifest.get("discoveryIndices", [])
    starts = defaultdict(list)
    for segment in segments:
        if not segment.get("adaptiveBounds"):
            continue
        first = segment["first"]
        pos = bisect.bisect_left(sampled, first)
        # Only seek back into the gap after the preceding observed frame. A manual
        # boundary or an old manifest without this evidence remains authoritative.
        segment["_lead"] = sampled[pos-1]+1 if pos and pos < len(sampled) and sampled[pos] == first else first
        for idx in segment["anchors"]:
            starts[idx].append(segment)
    if not starts:
        return
    media = manifest["media"]
    stop = max(starts)
    waiting = {segment["id"] for segment in segments if segment.get("adaptiveBounds")}
    frames = raw_frames(source, config, media["width"], media["height"], timeout=1800)
    with contextlib.closing(frames):
        for index, rgb in enumerate(frames):
            if index in starts:
                strokes = stroke_image(rgb)
                for segment in starts[index]:
                    if segment["id"] not in waiting:
                        continue
                    try:
                        policy = measured_ink(rgb, segment, index)
                    except BridgeError as error:
                        if error.code == "video_localization_mask_uncertain":
                            continue
                        raise
                    x, y, w, h = segment["anchors"][index]
                    ok, encoded = cv2.imencode(".png", strokes[y:y+h, x:x+w])
                    require(ok, "video_localization_decode_failed")
                    segment["_leadRef"] = encoded
                    segment["_leadInk"], segment["_clearStart"] = policy,index
                    waiting.remove(segment["id"])
            if index == stop or not waiting:
                break
    for segment in segments:
        if segment.get("adaptiveBounds") and segment["id"] not in waiting:
            if manifest["frames"][segment["_clearStart"]]["ptsMs"]-manifest["frames"][segment["first"]]["ptsMs"] > 300:
                segment["_uncertainStart"] = True
    return waiting


def source_frame(segment, index, frame_id, rgb, strokes, previous):
    import cv2
    import numpy as np
    anchors = segment["anchors"]
    if index < segment["_clearStart"]:
        if "_reference" not in segment:
            segment["_reference"] = cv2.imdecode(segment.pop("_leadRef"), cv2.IMREAD_GRAYSCALE)
        score, box = match_strokes(segment["_reference"], strokes, anchors[segment["_clearStart"]])
        # A leading extension is valid only if it stays continuous into the known anchor.
        if score < .66:
            segment["frames"].clear()
            if index >= segment["first"]:
                require(not segment.get("_uncertainStart"), "video_localization_tracking_uncertain")
                segment["openingFramesWithoutStrokes"] = segment.get("openingFramesWithoutStrokes",0)+1
            return
        x, y, w, h = box
        policy = segment["_leadInk"]
        try:
            glyph_mask(rgb[y:y+h, x:x+w], policy)
        except BridgeError:
            segment["frames"].clear()
            if index >= segment["first"]:
                require(not segment.get("_uncertainStart"), "video_localization_mask_uncertain")
                segment["openingFramesWithoutStrokes"] = segment.get("openingFramesWithoutStrokes",0)+1
            return
        segment["frames"].append({"frameId":frame_id, "box":box, "score":round(score,5), "method":"backward_strokes", "mask":policy})
        return
    if index in anchors:
        box = anchors[index]
        x, y, w, h = box
        try:
            policy = segment["_leadInk"] if index == segment["_clearStart"] else measured_ink(rgb, segment, index)
            box = anchors[index]
            x, y, w, h = box
        except BridgeError as error:
            if error.code != "video_localization_mask_uncertain" or "_box" not in segment:
                raise
            score, box = match_strokes(segment["_reference"], strokes, box)
            policy = segment["_ink"]
            x,y,w,h = box
            require(score >= .61, "video_localization_tracking_uncertain")
            glyph_mask(rgb[y:y+h,x:x+w],policy)
        segment["_reference"] = strokes[y:y+h, x:x+w].copy()
        segment["_anchor"], segment["_box"], segment["_ink"] = index, box, policy
        if index == segment["_clearStart"]:
            segment["mask"] = policy
            segment["style"] = {**segment.get("style", {}), "color":policy.get("color", "#ffffff")}
        score, method = 1, "model_keyframe"
    else:
        future = segment["_next"].get(segment["_anchor"])
        forecast = segment["_box"]
        if future is not None:
            factor = (index-segment["_anchor"])/(future-segment["_anchor"])
            forecast = [round(a+(b-a)*factor) for a,b in zip(anchors[segment["_anchor"]], anchors[future])]
        score, box = match_strokes(segment["_reference"], strokes, forecast)
        if forecast != segment["_box"] and score < .75:
            last_score, last_box = match_strokes(segment["_reference"], strokes, segment["_box"])
            if last_score > score:
                score, box = last_score, last_box
        policy = segment["_ink"]
        method = "matched_strokes"
        if score < .57:
            # Outside the last observed anchor, a new unrelated caption commonly
            # occupies exactly the same box until the next sampled frame. A low
            # identity score limits that extrapolation; it never invalidates the anchor.
            if future is None and score < .48:
                segment["trimmedTailFrames"] = segment.get("trimmedTailFrames", 0) + 1
                return
            x,y,w,h = box
            values = rgb[y:y+h,x:x+w]
            energy = np.mean(np.abs(strokes[y:y+h,x:x+w].astype(np.int16)-128))
            reference_energy = np.mean(np.abs(segment["_reference"].astype(np.int16)-128))
            median = np.median(cv2.cvtColor(values, cv2.COLOR_RGB2GRAY))
            if (median > 226 or median < 22) and energy < .30*reference_energy:
                segment["occludedFrames"] = segment.get("occludedFrames", 0) + 1
                return
            require(previous is not None and segment["frames"], "video_localization_tracking_uncertain")
            box, flow_score, method = track_step(previous, rgb, segment["_box"], policy)
            require(score >= .45, "video_localization_tracking_uncertain")
            score = min(score, flow_score)
        x,y,w,h = box
        patch = rgb[y:y+h,x:x+w]
        try:
            glyph_mask(patch, policy)
        except BridgeError:
            # Exposure or an animated highlight can change ink color without changing
            # glyph identity; relearn color only after the original strokes matched.
            require(score >= .57, "video_localization_mask_uncertain")
            policy = anchor_ink(patch)
        segment["_box"] = box
    segment["frames"].append({"frameId":frame_id, "box":box, "score":round(score,5), "method":method, "mask":policy})


def flat_label_repair(patch, mask):
    """Fill stroke edges only when the surrounding sign has one measured colour."""
    import cv2
    import numpy as np
    # Antialiased edges and video compression extend beyond the detected ink.
    fringe = cv2.dilate(mask, np.ones((5, 5), np.uint8)) != 0
    surround = patch[~fringe]
    if len(surround) < max(32, mask.size // 5):
        return None
    colour = np.median(surround, axis=0).astype(np.uint8)
    deviation = np.max(np.abs(surround.astype(np.int16) - colour), axis=1)
    if np.count_nonzero(deviation > 5) > len(surround) * .01:
        return None
    return fringe, colour


def project_glyph_margin(original, box, policy, mask):
    """Recover a clipped stroke fringe while excluding neighbouring unapproved glyphs."""
    import cv2
    import numpy as np
    x,y,w,h=box
    crop=original[y:y+h,x:x+w]
    if not (mask[:4].any() or mask[-4:].any() or mask[:,:4].any() or mask[:,-4:].any()):
        return box,crop,mask,None
    x1,y1=max(0,x-5),max(0,y-5)
    x2,y2=min(original.shape[1],x+w+5),min(original.shape[0],y+h+5)
    crop=original[y1:y2,x1:x2]
    core=glyph_mask(crop,{**policy,"dilatePx":0})
    count,labels=cv2.connectedComponents(core,connectivity=8)
    chosen=np.zeros(count,np.uint8)
    chosen[np.unique(labels[y-y1:y-y1+h,x-x1:x-x1+w])]=255
    chosen[0]=0
    selected=chosen[labels]
    require(selected.any(),"video_localization_mask_uncertain")
    protect=cv2.dilate(core & ~selected,np.ones((5,5),np.uint8))!=0
    r=policy.get("dilatePx",1)
    expanded=cv2.dilate(selected,np.ones((2*r+1,2*r+1),np.uint8))
    expanded[protect]=0
    return (x1,y1,x2-x1,y2-y1),crop,expanded,protect


def temporal_label_repair(patch, mask, previous, observed):
    """Copy visible same-shot pixels only when the surrounding texture still agrees."""
    import cv2
    import numpy as np
    fringe = cv2.dilate(mask,np.ones((5,5),np.uint8)) != 0
    surround = ~fringe & observed
    if not np.all(observed[fringe]) or np.count_nonzero(surround) < max(40,mask.size*.25):
        return None
    difference = np.max(np.abs(patch.astype(np.int16)-previous.astype(np.int16)),axis=2)
    if np.median(difference[surround]) > 4 or np.quantile(difference[surround],.95) > 13:
        return None
    # A late start annotation can leave the same lettering in an ostensibly empty donor.
    core = cv2.erode(mask,np.ones((3,3),np.uint8)) != 0
    if not np.any(core) or np.mean(difference[core] > 25) < .6:
        return None
    return fringe,previous[fringe]


def vertical_cells(text):
    """Return supported single-codepoint cells, never split complex graphemes.

    NFC is applied only to this ephemeral rendering value. The approved plan
    text remains byte-for-byte unchanged. This is a deliberately restricted
    repertoire, not a general Unicode grapheme segmenter or vertical shaper.
    """
    import unicodedata
    require(isinstance(text, str) and text.strip(), "video_localization_vertical_text_unsupported")
    cells = list(unicodedata.normalize("NFC", text))
    for char in cells:
        if char == " ":
            continue
        point = ord(char)
        category = unicodedata.category(char)
        # Reject even otherwise supported blocks' nonspacing/format characters.
        # Halfwidth dakuten/handakuten are Lm, but extend the preceding kana.
        require(not category.startswith(("M", "C")) and not char.isspace()
                and point not in (0xFF9E, 0xFF9F), "video_localization_vertical_text_unsupported")
        cjk = any(start <= point <= end for start, end in (
            (0x2E80, 0x303F), (0x3040, 0x30FF), (0x3130, 0x318F), (0x31F0, 0x31FF),
            (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xAC00, 0xD7A3), (0xF900, 0xFAFF),
            (0xFF01, 0xFF60), (0xFF66, 0xFF9D), (0x20000, 0x323AF)))
        simple = (cjk or unicodedata.name(char, "").startswith("LATIN ")
                  or category.startswith("P") or category in ("Nd", "Sm", "Sc"))
        require(simple, "video_localization_vertical_text_unsupported")
    return cells


def _vertical_text_layout(draw, font, cells, stroke, spacing, candidate):
    bounds = [draw.textbbox((0, 0), char, font=font, stroke_width=stroke) for char in cells]
    width = max(row[2]-row[0] for row in bounds)
    cell_height = max(candidate+2*stroke, max(row[3]-row[1] for row in bounds))
    height = len(cells)*cell_height+(len(cells)-1)*spacing
    return bounds, width, height, cell_height


def text_layer(segment, box, config, *, layout_only=False):
    from PIL import Image, ImageDraw, ImageFont
    w, h = box[2:]
    style = segment.get("style", {})
    require(isinstance(style, dict))
    font_name = style.get("font", "sans")
    require(font_name in ("sans", "serif"))
    font_path = Path(config["fonts"][font_name])
    require(font_path.is_file(), "video_localization_korean_font_missing")
    size = style.get("fontSize", min(120, max(10, round(h * .93))))
    stroke = style.get("outlineWidth", 1)
    require(finite(size) and 6 <= size <= 240 and isinstance(stroke, int) and 0 <= stroke <= 6)
    color, outline = style.get("color", "#ffffff"), style.get("outlineColor", "#101216")
    require(all(isinstance(c, str) and re.fullmatch(r"#[a-fA-F0-9]{6}", c) for c in (color, outline)))
    align = style.get("align", "center")
    require(align in ("left", "center", "right"))
    writing_mode = style.get("writingMode", "horizontal")
    require(writing_mode in ("horizontal", "vertical_ttb"), "video_localization_writing_mode_unsupported")
    scale = 2
    layer = Image.new("RGBA", (1, 1) if layout_only else (w*scale, h*scale))
    draw = ImageDraw.Draw(layer)
    text = segment["translation"]

    if writing_mode == "vertical_ttb":
        cells = vertical_cells(text)
        padding, spacing = 2*scale, 2*scale
        for candidate in range(round(size*scale), 11, -1):
            font = ImageFont.truetype(str(font_path), candidate)
            bounds, tw, th, cell_height = _vertical_text_layout(draw, font, cells, stroke*scale, spacing, candidate)
            if tw <= w*scale-2*padding and th <= h*scale-2*padding:
                break
        else:
            raise BridgeError("video_localization_text_overflow")
        if layout_only:
            return None
        x = padding if align == "left" else w*scale-tw-padding if align == "right" else (w*scale-tw)/2
        top = (h*scale-th)/2
        for index, (char, bound) in enumerate(zip(cells, bounds)):
            gw, gh = bound[2]-bound[0], bound[3]-bound[1]
            draw.text((x+(tw-gw)/2-bound[0], top+index*(cell_height+spacing)+(cell_height-gh)/2-bound[1]),
                      char, font=font, fill=color, stroke_width=stroke*scale, stroke_fill=outline)
        return layer.resize((w, h), Image.Resampling.LANCZOS)

    def wrap(font):
        # Prefer Korean word boundaries, with character wrapping for unspaced text.
        limit = w*scale-4-2*stroke*scale
        rows = []
        for paragraph in text.split("\n"):
            line = ""
            for word in re.findall(r"\s+|\S+", paragraph):
                if line and not word.isspace() and font.getlength(line+word) > limit:
                    rows.append(line.rstrip())
                    line = ""
                if not line and word.isspace():
                    continue
                if font.getlength(word) <= limit:
                    line += word
                    continue
                for char in word:
                    if line and font.getlength(line+char) > limit:
                        rows.append(line.rstrip())
                        line = ""
                    line += char
            rows.append(line.rstrip())
        return "\n".join(rows)

    for candidate in range(round(size*scale), 11, -1):
        font = ImageFont.truetype(str(font_path), candidate)
        lines = wrap(font)
        bounds = draw.multiline_textbbox((0, 0), lines, font=font, spacing=2*scale, stroke_width=stroke*scale, align=align)
        if bounds[2]-bounds[0] <= w*scale-4 and bounds[3]-bounds[1] <= h*scale-4:
            break
    else:
        raise BridgeError("video_localization_text_overflow")
    if layout_only:
        return None
    tw, th = bounds[2]-bounds[0], bounds[3]-bounds[1]
    x = 2 if align == "left" else w*scale-tw-2 if align == "right" else (w*scale-tw)/2
    draw.multiline_text((x-bounds[0], (h*scale-th)/2-bounds[1]), lines, font=font, fill=color, stroke_width=stroke*scale, stroke_fill=outline, spacing=2*scale, align=align)
    return layer.resize((w, h), Image.Resampling.LANCZOS)


def audio_hash(source, config):
    raw = command(ffinput(config["ffmpeg"], source) + ["-map", "0:a:0", "-c:a", "copy", "-f", "hash", "-hash", "sha256", "pipe:1"], timeout=300, output_limit=1024).decode("ascii").strip()
    require(re.fullmatch(r"SHA256=[0-9a-fA-F]{64}", raw), "video_localization_audio_verification_failed")
    return raw.split("=", 1)[1].lower()


def verify_full_audio(source, output, media, measured, config):
    require(measured["hasAudio"] == media["hasAudio"] and measured["audioCodec"] == media["audioCodec"], "video_localization_output_audio_changed")
    if not media["hasAudio"]:
        return None
    def audio_offset(path):
        data = json.loads(command([config["ffprobe"], "-v", "error", "-protocol_whitelist", "file,pipe", "-show_entries", "stream=codec_type,start_time:stream_disposition=attached_pic", "-of", "json", str(path)]))
        starts = {s["codec_type"]: float(s["start_time"]) for s in data["streams"] if not s.get("disposition", {}).get("attached_pic") and s.get("start_time") not in (None, "N/A")}
        require("video" in starts and "audio" in starts, "video_localization_audio_verification_failed")
        return starts["audio"] - starts["video"]
    require(abs(audio_offset(source) - audio_offset(output)) <= .003, "video_localization_output_audio_changed")
    packet_hash = audio_hash(source, config)
    require(packet_hash == audio_hash(output, config), "video_localization_output_audio_changed")
    return packet_hash


def render(root, payload, config, *, erase_only=False):
    import cv2
    import numpy as np
    from PIL import Image
    source = local(root, payload["source"])
    manifest = load_manifest(root, source)
    tracking_path = local(root, payload.get("tracking", "tracking.json"))
    regular(tracking_path, TRACKING_JSON_LIMIT)
    tracking = json.loads(tracking_path.read_text("utf8"))
    require(tracking.get("protocol") == PROTOCOL and tracking.get("sourceSha256") == manifest["sourceSha256"], "video_localization_source_changed")
    require(tracking.get("readyForRender") is True and tracking.get("issues") == [], "video_localization_review_required")
    preserve = payload.get("uncertaintyPolicy") == PRESERVE
    require(preserve == (tracking.get("uncertaintyPolicy") == PRESERVE) and not (preserve and erase_only),"video_localization_review_required")
    spans = [(r["segmentId"],r["startFrame"],r["endFrame"],r["code"]) for r in tracking.get("preservedRanges",[])] if preserve else []
    media = manifest["media"]
    profile = payload.get("profile", "source")
    require(profile in ("source", "2k120"))
    if profile == "source":
        require(media["constantFrameRate"], "video_localization_vfr_source_profile_unsupported")
    if profile == "source":
        require(media["width"] % 2 == 0 and media["height"] % 2 == 0, "video_localization_odd_dimensions_unsupported")
    output = local(root, payload.get("output", "localized.mp4"), False)
    require(output.suffix == ".mp4")
    w, h = media["width"], media["height"]
    preview = payload.get("previewWindow")
    first_index, last_index = 0, len(manifest["frames"]) - 1
    window_start, window_end = 0.0, media["durationMs"]
    preview_report = None
    if preview is not None:
        require(isinstance(preview, dict) and set(preview) == {"startMs", "durationMs"} and finite(preview.get("startMs")) and finite(preview.get("durationMs")), "video_localization_invalid_preview")
        require(0 <= preview["startMs"] < media["durationMs"] and 1000 <= preview["durationMs"] <= 10000 and preview["startMs"] + preview["durationMs"] <= media["durationMs"] + .1, "video_localization_invalid_preview")
        timestamps = [frame["ptsMs"] for frame in manifest["frames"]]
        first_index = bisect.bisect_left(timestamps, preview["startMs"])
        last_index = bisect.bisect_left(timestamps, preview["startMs"] + preview["durationMs"]) - 1
        require(0 <= first_index <= last_index < len(timestamps), "video_localization_invalid_preview")
        window_start = timestamps[first_index]
        final_frame = manifest["frames"][last_index]
        window_end = final_frame["ptsMs"] + final_frame["durationMs"]
        preview_report = {"requestedStartMs": preview["startMs"], "requestedDurationMs": preview["durationMs"], "actualStartMs": window_start, "actualDurationMs": window_end-window_start,
                          "firstSourceFrameId": manifest["frames"][first_index]["frameId"], "lastSourceFrameId": final_frame["frameId"], "audioPolicy": "trim_original_audio_reencode_aac", "fullAudioPacketHashVerified": False}
    target_fps = 120 if profile == "2k120" else media["fps"]
    output_w, output_h = w, h
    if profile == "2k120":
        scale = 2560 / max(w, h)
        output_w, output_h = 2*round(w*scale/2), 2*round(h*scale/2)
    by_frame, layers = {}, OrderedDict()
    layer_bytes = 0
    for segment in tracking.get("segments", []):
        require(not segment.get("manualReview") and segment.get("frames"), "video_localization_review_required")
        for frame in segment["frames"]:
            match = FRAME_ID.fullmatch(frame["frameId"])
            require(match is not None and int(match[1]) < len(manifest["frames"]), "video_localization_unknown_frame")
            index = int(match[1])
            box = frame["box"]
            require(isinstance(box, list) and len(box) == 4 and all(isinstance(v, int) for v in box) and min(box) >= 0 and box[2] > 0 and box[3] > 0 and box[0]+box[2] <= w and box[1]+box[3] <= h)
            by_frame.setdefault(index, []).append((segment, frame.get("mask", segment["mask"]), box))
    require(by_frame, "video_localization_no_regions")
    rate = "120" if profile == "2k120" else media["fpsRatio"]
    args = [config["ffmpeg"], "-nostdin", "-hide_banner", "-loglevel", "error"]
    if preview is not None:
        args += ["-copyts"]
    args += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-video_size", f"{w}x{h}", "-framerate", rate, "-i", "pipe:0",
             "-protocol_whitelist", "file,pipe", "-i", str(source), "-map", "0:v:0", "-map", "1:a:0?", "-map_metadata", "-1", "-vf", f"scale={output_w}:{output_h}:flags=lanczos,setsar=1", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
    if preview is not None and media["hasAudio"]:
        audio_start = (manifest["sourceStartPtsMs"] + window_start) / 1000
        audio_end = (manifest["sourceStartPtsMs"] + window_end) / 1000
        args += ["-af", f"atrim=start={audio_start:.9f}:end={audio_end:.9f},asetpts=PTS-STARTPTS", "-c:a", "aac", "-b:a", "192k"]
    else:
        args += ["-c:a", "copy"]
    args += ["-movflags", "+faststart", str(output)]
    encoder = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False)
    timer = threading.Timer(3600, encoder.kill)
    timer.daemon = True
    timer.start()
    count, written, changed_frames = 0, 0, 0
    previous_base = previous_observed = previous_thumbnail = None
    repairs = {"flatRegions":0,"temporalRegions":0}
    decoder = raw_frames(source, config, w, h, timeout=3600)
    try:
        for index, rgb in enumerate(decoder):
            require(index < len(manifest["frames"]), "video_localization_frame_count_changed")
            if index < first_index:
                continue
            if index > last_index:
                break
            # All masks derive from the source frame, before Korean text is drawn.
            original = rgb
            combined = np.zeros((h, w), dtype=np.uint8)
            replacements = []
            drawable = []
            program_controls = {}
            foreground_regions = []
            rendered_labels = set()
            if erase_only:
                observed = np.ones((h,w),dtype=bool)
                thumbnail = cv2.resize(original,(48,32),interpolation=cv2.INTER_AREA).astype(np.int16)
                same_shot = previous_thumbnail is not None and np.quantile(np.abs(thumbnail-previous_thumbnail),.8) < 25
            for segment, policy, box in by_frame.get(index, []):
                if preserve:
                    label=(tuple(box),segment["sourceText"],segment["translation"])
                    if label in rendered_labels:
                        continue
                x, y, bw, bh = box
                patch = original[y:y+bh, x:x+bw]
                try:
                    program = config.get("region_program")
                    custom = program(segment,index,original,box) if program is not None and not erase_only else None
                    if custom is not None:
                        background = custom["background"]
                        require(background.shape == patch.shape and background.dtype == np.uint8,
                                "video_localization_invalid_region_program")
                        full = np.ones((bh,bw),dtype=bool)
                        replacements.append((x,y,bw,bh,full,background[full]))
                        program_controls[segment["id"]] = custom["text_alpha"]
                        if np.any(custom["foreground_mask"]):
                            foreground_regions.append((x,y,bw,bh,custom["foreground_mask"]))
                        drawable.append((segment,box))
                        continue
                    if erase_only and policy.get("mode") == "auto":
                        policy = {"mode":"color","color":infer_glyph_ink(patch),"tolerance":40,"dilatePx":1}
                    mask = glyph_mask(patch,policy)
                    protected=None
                    if erase_only:
                        (x,y,bw,bh),patch,mask,protected=project_glyph_margin(original,box,policy,mask)
                    solid = flat_label_repair(patch,mask)
                    if solid is not None:
                        if protected is not None:
                            solid=(solid[0] & ~protected,solid[1])
                        replacements.append((x,y,bw,bh,*solid))
                        if erase_only:
                            repairs["flatRegions"] += 1
                            observed[y:y+bh,x:x+bw][solid[0]] = False
                    elif erase_only:
                        temporal = None
                        if same_shot:
                            temporal = temporal_label_repair(patch,mask,previous_base[y:y+bh,x:x+bw],previous_observed[y:y+bh,x:x+bw])
                        require(temporal is not None,"video_localization_background_uncertain")
                        if protected is not None:
                            use=~protected[temporal[0]]
                            temporal=(temporal[0]&~protected,temporal[1][use])
                        replacements.append((x,y,bw,bh,*temporal))
                        repairs["temporalRegions"] += 1
                    else:
                        combined[y:y+bh,x:x+bw] |= mask
                    drawable.append((segment,box))
                    if preserve:
                        rendered_labels.add(label)
                except BridgeError as error:
                    if preserve and error.code in RECOVERABLE_GEOMETRY:
                        spans.append((segment["id"],index,index,error.code))
                        continue
                    if erase_only:
                        frame = manifest["frames"][index]
                        error.location = {"segmentId":segment["id"],"frameId":frame["frameId"],"ptsMs":frame["ptsMs"],"code":error.code}
                    raise
            if np.any(combined):
                rgb = cv2.inpaint(rgb,combined,3,cv2.INPAINT_TELEA)
            elif replacements:
                rgb = original.copy()
            for x,y,bw,bh,fringe,colours in replacements:
                rgb[y:y+bh,x:x+bw][fringe] = colours
            if erase_only:
                previous_base,previous_observed,previous_thumbnail = rgb,observed,thumbnail
            canvas = Image.fromarray(rgb).convert("RGBA")
            if not erase_only:
                for segment, box in drawable:
                    key = (segment["id"], box[2], box[3])
                    if key in layers:
                        layer = layers[key]
                        layers.move_to_end(key)
                    else:
                        layer = text_layer(segment, box, config)
                        layers[key] = layer
                        layer_bytes += layer.width * layer.height * 4
                        while layer_bytes > 64 * 1024**2 and len(layers) > 1:
                            _, evicted = layers.popitem(last=False)
                            layer_bytes -= evicted.width * evicted.height * 4
                    if segment["id"] in program_controls:
                        alpha = program_controls[segment["id"]]
                        adjusted = layer.copy()
                        adjusted.putalpha(Image.fromarray(np.clip(np.asarray(layer.getchannel("A"),dtype=float)*alpha,0,255).astype(np.uint8)))
                        canvas.alpha_composite(adjusted, (box[0], box[1]))
                    else:
                        canvas.alpha_composite(layer, (box[0], box[1]))
            pixels = np.asarray(canvas.convert("RGB"))
            if foreground_regions:
                pixels = pixels.copy()
                for x,y,bw,bh,foreground in foreground_regions:
                    pixels[y:y+bh,x:x+bw][foreground] = original[y:y+bh,x:x+bw][foreground]
            if index in by_frame and np.any(original != pixels):
                changed_frames += 1
            block = pixels.tobytes()
            if profile == "source":
                repeats = 1
            else:
                current = manifest["frames"][index]
                end = current["ptsMs"] + current["durationMs"] - window_start
                # Sample-and-hold at 120Hz. No optical-flow interpolation is used.
                repeats = max(0, math.ceil(end*target_fps/1000-1e-7) - written)
            for _ in range(repeats):
                encoder.stdin.write(block)
                written += 1
            count += 1
            if count % 120 == 0:
                disk(root)
                require(not output.exists() or output.stat().st_size <= OUTPUT_LIMIT, "video_localization_output_limit")
        require(count == last_index-first_index+1, "video_localization_frame_count_changed")
        require(changed_frames > 0, "video_localization_no_changes")
        encoder.stdin.close()
        require(encoder.wait(timeout=180) == 0, "video_localization_encode_failed")
    except Exception:
        if encoder.poll() is None:
            encoder.kill()
        encoder.wait()
        if output.exists() and not output.is_symlink():
            output.unlink()
        raise
    finally:
        decoder.close()
        timer.cancel()
        if encoder.poll() is None:
            encoder.kill()
        encoder.wait()
        with contextlib.suppress(Exception):
            encoder.stdin.close()
    os.chmod(output, 0o600)
    measured = probe(output, config, input_limit=False)
    require(measured["width"] == output_w and measured["height"] == output_h and abs(measured["fps"]-target_fps) <= .02, "video_localization_output_geometry_changed")
    require(abs(measured["durationMs"] - (window_end-window_start)) <= max(100, 2000/target_fps), "video_localization_output_duration_changed")
    expected_audio_codec = "aac" if preview is not None and media["hasAudio"] else media["audioCodec"]
    require(measured["hasAudio"] == media["hasAudio"] and measured["audioCodec"] == expected_audio_codec, "video_localization_output_audio_changed")
    audio_digest = verify_full_audio(source, output, media, measured, config) if preview is None else None
    result = {"file": output.name, "bytes": regular(output), "sha256": digest(output), "media": measured, "sourceSha256": manifest["sourceSha256"], "profile": profile,
              "framePolicy": "source_frames" if profile == "source" else "repeat_frames_no_motion_interpolation", "sourceFrames": count, "encodedFrames": written,
              "changedFrames": changed_frames, "originalAudioPreserved": preview is None, "audioPacketSha256": audio_digest, "qualityStatus": "manual_review_required", "automaticQualityPass": False}
    if preserve:
        result.update(uncertaintyPolicy=PRESERVE,preservedRanges=preservation_ranges(manifest,spans))
    if erase_only:
        result["repairCounts"] = repairs
    if preview_report is not None:
        result["previewWindow"] = preview_report
    write_json(root, payload.get("report", "render-report.json"), result)
    return result
