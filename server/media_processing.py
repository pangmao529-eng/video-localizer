"""Bounded server-side cuts, HTTPS artifact imports and stream-copy assembly.

Only mechanical media processing lives here. Ark credentials, model calls,
translation and visual interpretation never enter this service.
"""
from fractions import Fraction
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import socket
import ssl
import stat
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit


POLICY = "fixed-15s-v1"
MAX_BYTES = 200 * 1024**2
MAX_PARTS = 20
MAX_SECONDS = 300
RESERVE = 2 * 1024**3
RETENTION = 3 * 86400
ID = re.compile(r"[a-f0-9]{64}\Z")
TASK = re.compile(r"cgt-[A-Za-z0-9][A-Za-z0-9_-]{0,195}\Z")
MEDIAKIT_TASK = re.compile(r"amk-[A-Za-z0-9][A-Za-z0-9_-]{0,195}\Z")
KINDS = {"split", "import", "join", "render_subtitles"}


def render_request(payload):
    """Validate only supplied text/timing; no translation or visual inference."""
    require(isinstance(payload, dict) and {"kind", "source_id", "clean_id", "cues"} <= set(payload)
            and set(payload) <= {"kind", "source_id", "clean_id", "cues", "style"}, "invalid_request")
    for key in ("source_id", "clean_id"):
        require(isinstance(payload[key], str) and ID.fullmatch(payload[key]), "invalid_object_id")
    cues = payload["cues"]
    require(isinstance(cues, list) and 1 <= len(cues) <= 100, "invalid_cues")
    previous = -1
    for cue in cues:
        require(isinstance(cue, dict) and {"start", "end", "text"} <= set(cue)
                and set(cue) <= {"start", "end", "text", "box"}, "invalid_cues")
        require(all(type(cue[k]) in (int, float) and math.isfinite(cue[k]) for k in ("start", "end"))
                and 0 <= cue["start"] < cue["end"] <= MAX_SECONDS and cue["start"] >= previous, "invalid_cue_time")
        previous = cue["start"]
        text = cue["text"]
        require(isinstance(text, str) and 1 <= len(text) <= 2000 and text.strip()
                and not any(c in text for c in "{}\\")
                and not any(ord(c) < 32 and c != "\n" for c in text), "invalid_subtitle_text")
        if "box" in cue:
            box = cue["box"]
            require(isinstance(box, dict) and set(box) == {"x", "y", "width", "height"}
                    and all(type(n) is int for n in box.values())
                    and 0 <= box["x"] < 8192 and 0 <= box["y"] < 8192
                    and 0 < box["width"] <= 8192 and 0 < box["height"] <= 8192, "invalid_cue_box")
    require(all("box" in cue for cue in cues) or all("box" not in cue for cue in cues), "mixed_cue_boxes")
    style = payload.get("style", {})
    require(isinstance(style, dict) and set(style) <= {"font_name", "font_size", "outline", "margin_v", "margin_x"}, "invalid_subtitle_style")
    require(isinstance(style.get("font_name", "Noto Sans CJK KR"), str)
            and re.fullmatch(r"[A-Za-z0-9 -]{1,80}", style.get("font_name", "Noto Sans CJK KR")), "invalid_subtitle_style")
    for key, low, high in (("font_size", 8, 120), ("outline", 0, 8), ("margin_v", 0, 500), ("margin_x", 0, 1920)):
        require(key not in style or type(style[key]) is int and low <= style[key] <= high, "invalid_subtitle_style")
    return payload


def subtitle_ass(cues, width, height, style):
    """Serialize validated literal text; ASS commands cannot come from cues."""
    size = style.get("font_size", max(16, round(height * .045)))
    margin = style.get("margin_v", round(height * .07))
    margin_x = style.get("margin_x", round(width * .25))
    font = style.get("font_name", "Noto Sans CJK KR")
    outline = style.get("outline", 2)
    header = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\nWrapStyle: 0\n"
              "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
              "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
              f"Style: Default,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,{outline},0,2,{margin_x},{margin_x},{margin},1\n\n[Events]\n"
              "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    def stamp(seconds):
        ticks = round(seconds * 100)
        return f"{ticks // 360000}:{ticks // 6000 % 60:02d}:{ticks // 100 % 60:02d}.{ticks % 100:02d}"
    rows = []
    for cue in cues:
        text = cue["text"].replace("\n", r"\N")
        # The erasure ROI does not constrain translated line width. A short
        # Chinese OCR box could otherwise turn its longer Korean into a column.
        left = right = vertical = 0
        if "box" in cue:
            box = cue["box"]
            left = right = margin_x
            vertical = max(0, height - box["y"] - box["height"])
        rows.append(f"Dialogue: 0,{stamp(cue['start'])},{stamp(cue['end'])},Default,,{left},{right},{vertical},,{text}\n")
    return header + "".join(rows)


class ProcessingError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def require(value, code, status=400):
    if not value:
        raise ProcessingError(code, status)


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(256 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def regular(path, limit=MAX_BYTES):
    try:
        row = path.lstat()
        require(stat.S_ISREG(row.st_mode) and not path.is_symlink() and row.st_nlink == 1
                and 0 < row.st_size <= limit, "unsafe_file")
        return row.st_size
    except OSError:
        raise ProcessingError("missing_object", 404) from None


def atomic_json(path, value):
    require(not path.is_symlink(), "unsafe_file")
    temporary = path.with_name(".state-" + secrets.token_hex(16))
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":"), ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def frame_ranges(count, fps):
    """Cut at 15 seconds; borrow from the prior segment for a <4-second tail."""
    fps = Fraction(fps)
    require(type(count) is int and 4 <= Fraction(count, 1) / fps <= MAX_SECONDS
            and 24 <= fps <= 60, "source_limits")
    maximum, minimum = int(15 * fps), math.ceil(4 * fps)
    ends = list(range(maximum, count, maximum)) + [count]
    if len(ends) > 1 and count - ends[-2] < minimum:
        ends[-2] = count - minimum
    require(1 <= len(ends) <= MAX_PARTS, "source_limits")
    starts = [0, *ends[:-1]]
    result = []
    for start, end in zip(starts, ends):
        require(minimum <= end - start <= maximum, "source_limits")
        result.append((start, end))
    return result


def checked_url(value):
    try:
        require(isinstance(value, str) and 1 <= len(value) <= 16384
                and "\\" not in value and not any(ord(c) < 33 or ord(c) > 126 for c in value), "unsafe_download")
        url = urlsplit(value)
        require(url.scheme == "https" and url.hostname and url.port in (None, 443)
                and url.username is None and url.password is None and not url.fragment, "unsafe_download")
        host = url.hostname.lower().rstrip(".")
        require(host != "localhost" and "." in host and "%" not in host
                and not host.endswith((".localhost", ".local", ".internal")), "unsafe_download")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        require(address is None or address.is_global and not address.is_multicast, "unsafe_download")
        return url
    except (ValueError, TypeError):
        raise ProcessingError("unsafe_download") from None


def public_addresses(host):
    try:
        addresses = list(dict.fromkeys(row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
        require(addresses and all(ipaddress.ip_address(ip).is_global
                                  and not ipaddress.ip_address(ip).is_multicast for ip in addresses), "unsafe_download")
        return addresses
    except (OSError, ValueError):
        raise ProcessingError("download_failed", 502) from None


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout=30):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def download(url, destination):
    parsed = checked_url(url)
    addresses = public_addresses(parsed.hostname)
    connection = PinnedHTTPS(parsed.hostname, addresses[0])
    deadline = time.monotonic() + 600
    try:
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection.request("GET", path, headers={"Accept": "video/mp4", "Accept-Encoding": "identity"})
        response = connection.getresponse()
        require(response.status == 200, "download_failed", 502)
        require(response.getheader("Content-Encoding", "identity") in {"identity", ""}, "download_failed", 502)
        declared = response.getheader("Content-Length")
        require(declared is None or declared.isdigit() and 12 <= int(declared) <= MAX_BYTES, "download_too_large", 413)
        count = 0
        with destination.open("xb") as output:
            while True:
                require(time.monotonic() < deadline, "download_failed", 504)
                block = response.read(256 * 1024)
                if not block:
                    break
                count += len(block)
                require(count <= MAX_BYTES, "download_too_large", 413)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        require(count >= 12 and (declared is None or count == int(declared)), "download_failed", 502)
        with destination.open("rb") as check:
            require(check.read(12)[4:8] == b"ftyp", "invalid_media")
    except (OSError, http.client.HTTPException):
        raise ProcessingError("download_failed", 502) from None
    finally:
        connection.close()


class Processor:
    def __init__(self, store, ffmpeg="ffmpeg", ffprobe="ffprobe"):
        self.store = store
        self.ffmpeg, self.ffprobe = str(ffmpeg), str(ffprobe)
        self.root = store.root / ".processing"
        require(not self.root.is_symlink(), "unsafe_file")
        self.root.mkdir(exist_ok=True, mode=0o700)
        require(self.root.resolve().parent == store.root.resolve(), "unsafe_file")
        self.lock = threading.RLock()
        self.pending = queue.Queue(maxsize=20)
        self.closed = False
        self.active = set()
        # Old queued/running jobs never retain the temporary import URL. They
        # become safely resubmittable by the same deterministic operation ID.
        for folder in self.root.iterdir():
            if ID.fullmatch(folder.name) and folder.is_dir() and not folder.is_symlink():
                state = self._read(folder.name, missing=True)
                if state and state.get("status") in {"queued", "running"}:
                    state.update(status="failed", error="interrupted", updated_at=time.time())
                    self._write(state)
        self.worker = threading.Thread(target=self._worker, name="private-media-processing", daemon=True)
        self.worker.start()

    def health(self):
        ffmpeg = bool(shutil.which(self.ffmpeg))
        ffprobe = bool(shutil.which(self.ffprobe))
        return {"available": not self.closed and self.worker.is_alive() and ffmpeg and ffprobe, "policy": POLICY,
                "max_source_seconds": MAX_SECONDS, "max_parts": MAX_PARTS,
                "ffmpeg_ready": ffmpeg, "ffprobe_ready": ffprobe}

    def close(self, wait=False):
        with self.lock:
            self.closed = True
        try:
            self.pending.put_nowait(None)
        except queue.Full:
            pass
        if wait:
            self.worker.join(timeout=30)

    def _folder(self, job_id):
        require(isinstance(job_id, str) and ID.fullmatch(job_id), "invalid_job_id")
        folder = self.root / job_id
        require(not folder.is_symlink(), "unsafe_file")
        return folder

    def _read(self, job_id, missing=False):
        path = self._folder(job_id) / "state.json"
        if not path.exists() and missing:
            return None
        try:
            regular(path, 1024**2)
            state = json.loads(path.read_text(encoding="utf-8"))
            require(isinstance(state, dict) and state.get("id") == job_id
                    and state.get("kind") in KINDS
                    and state.get("status") in {"queued", "running", "succeeded", "failed"}, "invalid_job")
            return state
        except (OSError, ValueError):
            raise ProcessingError("invalid_job") from None

    def _write(self, state):
        folder = self._folder(state["id"])
        folder.mkdir(exist_ok=True, mode=0o700)
        atomic_json(folder / "state.json", state)

    @staticmethod
    def _public(state):
        return json.loads(json.dumps({key: state[key] for key in
            ("id", "kind", "status", "result", "error", "created_at", "updated_at") if key in state}))

    def _validate(self, payload):
        require(isinstance(payload, dict), "invalid_request")
        kind = payload.get("kind")
        require(isinstance(kind, str) and kind in KINDS, "invalid_request")
        if kind == "render_subtitles":
            return json.loads(json.dumps(render_request(payload), allow_nan=False))
        fields = {"split": {"kind", "source_id", "policy"}, "import": {"kind", "task_id", "url"},
                  "join": {"kind", "source_id", "split_job_id", "parts"}}[kind]
        require(set(payload) == fields, "invalid_request")
        clean = json.loads(json.dumps(payload))
        if kind == "import":
            require(isinstance(payload["task_id"], str)
                    and (TASK.fullmatch(payload["task_id"]) or MEDIAKIT_TASK.fullmatch(payload["task_id"])), "invalid_task_id")
            checked_url(payload["url"])
            clean.pop("url")
        else:
            require(isinstance(payload["source_id"], str) and ID.fullmatch(payload["source_id"]), "invalid_object_id")
            if kind == "split":
                require(payload["policy"] == POLICY, "unsupported_policy")
            else:
                require(isinstance(payload["split_job_id"], str) and ID.fullmatch(payload["split_job_id"]), "invalid_job_id")
                parts = payload["parts"]
                require(isinstance(parts, list) and 1 <= len(parts) <= MAX_PARTS, "invalid_parts")
                for index, part in enumerate(parts, 1):
                    require(isinstance(part, dict) and set(part) == {"index", "task_id", "object_id"}
                            and type(part["index"]) is int and part["index"] == index
                            and isinstance(part["task_id"], str) and TASK.fullmatch(part["task_id"])
                            and isinstance(part["object_id"], str) and ID.fullmatch(part["object_id"]), "invalid_parts")
                require(len({p["task_id"] for p in parts}) == len(parts), "invalid_parts")
        return clean

    @staticmethod
    def identity(payload):
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def submit(self, payload):
        clean = self._validate(payload)
        job_id = self.identity(clean)
        with self.lock:
            require(not self.closed, "unavailable", 503)
            require(self.health()["available"], "runtime_missing", 503)
            current = self._read(job_id, missing=True)
            if current:
                require(current.get("request") == clean, "job_conflict", 409)
                current = self._refresh(current)
                if current["status"] != "failed":
                    return self._public(current)
            require(not self.pending.full(), "processing_busy", 429)
            self._cleanup()
            self._space()
            now = time.time()
            state = {"id": job_id, "kind": clean["kind"], "request": clean, "status": "queued",
                     "created_at": current.get("created_at", now) if current else now, "updated_at": now}
            self._write(state)
            self.active.add(job_id)
            # The URL exists solely in this queued object, never state.json.
            self.pending.put_nowait((job_id, payload.get("url")))
            return self._public(state)

    def _refresh(self, state):
        if state["status"] == "succeeded":
            result = state.get("result", {})
            ids = ([part["id"] for part in result.get("parts", [])] if state["kind"] == "split"
                   else [result.get("id")] if state["kind"] == "import" else [result.get("output", {}).get("id")])
            if state["kind"] in {"split", "join", "render_subtitles"}:
                ids.append(state["request"]["source_id"])
            if state["kind"] == "render_subtitles":
                ids.append(state["request"]["clean_id"])
            try:
                for object_id in ids:
                    self.store.info(object_id)
            except Exception:
                state.update(status="failed", error="result_expired", updated_at=time.time())
                state.pop("result", None)
                self._write(state)
        return state

    def get(self, job_id):
        with self.lock:
            state = self._read(job_id, missing=True)
            require(state is not None, "missing_job", 404)
            return self._public(self._refresh(state))

    def _cleanup(self):
        # Scope deletion to our own generated job names and regular files only.
        now = time.time()
        for folder in self.root.iterdir():
            if not ID.fullmatch(folder.name) or folder.is_symlink() or not folder.is_dir() or folder.name in self.active:
                continue
            state = self._read(folder.name, missing=True)
            if state and state.get("updated_at", now) + RETENTION < now:
                entries = list(folder.iterdir())
                if all(path.is_file() and not path.is_symlink() and path.stat().st_nlink == 1 for path in entries):
                    for path in entries:
                        path.unlink()
                    folder.rmdir()

    def _space(self, extra=0):
        with self.store.upload_lock:
            usage = self.store.cleanup_and_usage()
            own = 0
            for folder in self.root.iterdir():
                if not ID.fullmatch(folder.name):
                    continue
                try:
                    entry = folder.lstat()
                    require(stat.S_ISDIR(entry.st_mode) and not stat.S_ISLNK(entry.st_mode), "unsafe_file")
                    entries = list(folder.iterdir())
                except FileNotFoundError:
                    continue
                for path in entries:
                    try:
                        row = path.lstat()
                    except FileNotFoundError:
                        # A concurrently running job may atomically publish or
                        # remove scratch, or replace its state file, here.
                        continue
                    require(stat.S_ISREG(row.st_mode) and not stat.S_ISLNK(row.st_mode)
                            and row.st_nlink == 1, "unsafe_file")
                    own += row.st_size
            require(usage + own + extra <= self.store.quota
                    and shutil.disk_usage(self.store.root).free >= extra + RESERVE, "storage_full", 507)

    def _worker(self):
        while True:
            if self.closed:
                break
            item = self.pending.get()
            if item is None:
                self.pending.task_done()
                break
            job_id, url = item
            with self.lock:
                if self.closed:
                    self.active.discard(job_id)
                    self.pending.task_done()
                    continue
                state = self._read(job_id)
                state.update(status="running", updated_at=time.time())
                self._write(state)
            folder = self._folder(job_id)
            try:
                # Scratch from an interrupted process is never treated as output.
                for path in folder.iterdir():
                    if path.name != "state.json":
                        require(path.is_file() and not path.is_symlink() and path.stat().st_nlink == 1, "unsafe_file")
                        path.unlink()
                self._space()
                result = getattr(self, "_" + state["kind"])(state["request"], folder, url)
                state.update(status="succeeded", result=result)
                state.pop("error", None)
            except Exception as error:
                state.update(status="failed", error=error.code if isinstance(error, ProcessingError) else "processing_failed")
                state.pop("result", None)
            finally:
                url = None
                for path in folder.iterdir():
                    if path.name != "state.json" and path.is_file() and not path.is_symlink() and path.stat().st_nlink == 1:
                        path.unlink()
                with self.lock:
                    state["updated_at"] = time.time()
                    self._write(state)
                    self.active.discard(job_id)
                self.pending.task_done()

    def _command(self, args, *, timeout=900, output_limit=64 * 1024**2, cwd=None):
        self._space()
        try:
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                                        timeout=timeout, check=False, cwd=cwd)
                require(result.returncode == 0 and output.tell() <= output_limit, "media_command_failed")
                output.seek(0)
                return output.read()
        except (OSError, subprocess.TimeoutExpired):
            raise ProcessingError("media_command_failed") from None

    def _input(self, path):
        return [self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "2",
                "-filter_threads", "1", "-protocol_whitelist", "file,pipe", "-noautorotate", "-i", str(path)]

    def _object(self, object_id):
        require(isinstance(object_id, str) and ID.fullmatch(object_id), "invalid_object_id")
        try:
            self.store.info(object_id)
        except Exception:
            raise ProcessingError("missing_object", 404) from None
        path = self.store.path(object_id)
        regular(path)
        require(digest(path) == object_id, "hash_mismatch")
        os.utime(path, None)
        return path

    def _probe(self, path):
        size = regular(path)
        with path.open("rb") as source:
            require(source.read(12)[4:8] == b"ftyp", "invalid_media")
        raw = json.loads(self._command([self.ffprobe, "-v", "error", "-threads", "2", "-protocol_whitelist", "file,pipe",
            "-count_frames", "-show_streams", "-show_format", "-show_data_hash", "sha256", "-of", "json", str(path)]))
        videos = [s for s in raw.get("streams", []) if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
        audios = [s for s in raw.get("streams", []) if s.get("codec_type") == "audio"]
        require(len(videos) == 1 and len(audios) <= 1 and "mp4" in raw.get("format", {}).get("format_name", "").split(","), "unsupported_streams")
        video = videos[0]
        fps, count = Fraction(video["avg_frame_rate"]), int(video["nb_read_frames"])
        width, height = int(video["width"]), int(video["height"])
        require(0 < fps <= 120 and 0 < count <= 36100 and width > 0 and height > 0, "source_limits")
        require(video.get("sample_aspect_ratio", "1:1") in {"1:1", "0:1", "N/A"}, "unsupported_aspect")
        rotation = float(video.get("tags", {}).get("rotate", 0))
        for side in video.get("side_data_list", []):
            rotation = float(side.get("rotation", rotation))
        require(math.isfinite(rotation) and rotation % 360 == 0, "unsupported_rotation")
        duration = float(video.get("duration", "nan"))
        require(math.isfinite(duration) and duration > 0, "invalid_timestamps")
        media = {"durationMs": round(duration * 1000, 6), "width": width, "height": height,
                 "fps": float(fps), "fpsRatio": f"{fps.numerator}/{fps.denominator}", "hasAudio": bool(audios),
                 "videoCodec": video.get("codec_name"), "audioCodec": audios[0].get("codec_name") if audios else None,
                 "bytes": size, "sha256": digest(path), "frame_count": count}
        return media, video, audios[0] if audios else None

    def _cfr(self, path, media):
        data = json.loads(self._command([self.ffprobe, "-v", "error", "-threads", "2", "-protocol_whitelist", "file,pipe",
            "-select_streams", "v:0", "-show_frames", "-show_entries", "frame=best_effort_timestamp_time,pkt_duration_time",
            "-of", "json", str(path)]))
        frames = data.get("frames", [])
        require(len(frames) == media["frame_count"], "invalid_timestamps")
        points = [float(frame.get("best_effort_timestamp_time", "nan")) for frame in frames]
        step = 1 / media["fps"]
        tolerance = max(.0001, .002 / media["fps"])
        require(all(math.isfinite(point) and abs(point - points[0] - index * step) <= tolerance
                    for index, point in enumerate(points)), "variable_frame_rate")
        require(abs(media["durationMs"] - len(frames) * step * 1000) <= tolerance * 1000, "invalid_timestamps")

    def _decode(self, path):
        self._command(self._input(path) + ["-xerror", "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"], output_limit=1024)

    def _publish(self, path):
        size = regular(path)
        object_id = digest(path)
        self._space(size)
        target = self.store.path(object_id)
        with self.store.upload_lock:
            usage = self.store.cleanup_and_usage()
            require(usage + size <= self.store.quota and shutil.disk_usage(self.store.root).free >= RESERVE + size, "storage_full", 507)
            if target.exists():
                require(regular(target) == size and digest(target) == object_id, "hash_mismatch")
                os.utime(target, None)
            else:
                with path.open("r+b") as stream:
                    os.fsync(stream.fileno())
                # Same filesystem, then unlink scratch so the published object
                # has one link before it becomes visible to store.info().
                if os.name == "nt":
                    os.rename(path, target)  # Windows refuses an existing target.
                else:
                    os.link(path, target)
                    path.unlink()
                os.utime(target, None)
        return {"id": object_id, "sha256": object_id, "bytes": size}

    def _split(self, payload, folder, url=None):
        source = self._object(payload["source_id"])
        info, _, _ = self._probe(source)
        require(4000 <= info["durationMs"] <= MAX_SECONDS * 1000 and 24 <= info["fps"] <= 60
                and 300 <= min(info["width"], info["height"]) and max(info["width"], info["height"]) <= 6000
                and .4 <= info["width"] / info["height"] <= 2.5
                and 407696 <= info["width"] * info["height"] <= 8295044
                and info["width"] % 2 == 0 and info["height"] % 2 == 0
                and info["videoCodec"] in {"h264", "hevc"} and info["audioCodec"] in {None, "aac", "mp3"}, "source_limits")
        self._cfr(source, info)
        parts = []
        fps = Fraction(info["fpsRatio"])
        for index, (start, end) in enumerate(frame_ranges(info["frame_count"], fps), 1):
            temporary = folder / f"part-{index:03d}.mp4"
            first, last = float(Fraction(start, 1) / fps), float(Fraction(end, 1) / fps)
            args = self._input(source) + ["-n", "-map", "0:v:0", "-vf", f"trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS",
                "-fps_mode", "passthrough", "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "-crf", "18",
                "-pix_fmt", "yuv420p", "-map_metadata", "-1"]
            if info["hasAudio"]:
                args += ["-map", "0:a:0", "-af", f"atrim=start={first:.12f}:end={last:.12f},asetpts=PTS-STARTPTS", "-c:a", "aac", "-b:a", "192k"]
            args += ["-fs", str(MAX_BYTES), "-movflags", "+faststart", str(temporary)]
            self._command(args)
            measured, _, _ = self._probe(temporary)
            require(measured["frame_count"] == end - start and measured["fpsRatio"] == info["fpsRatio"]
                    and (measured["width"], measured["height"]) == (info["width"], info["height"]), "split_mismatch")
            self._cfr(temporary, measured)
            receipt = self._publish(temporary)
            temporary.unlink(missing_ok=True)
            parts.append({**receipt, "index": index, "start_frame": start, "end_frame": end,
                          "frame_count": end - start, "start_seconds": first,
                          "duration_seconds": float(Fraction(end - start, 1) / fps)})
        require(digest(source) == info["sha256"], "source_changed")
        return {"source_id": payload["source_id"], "source_media": info, "policy": POLICY, "parts": parts}

    def _import(self, payload, folder, url):
        require(url is not None, "interrupted")
        temporary = folder / "import.mp4"
        download(url, temporary)
        media, _, _ = self._probe(temporary)
        self._cfr(temporary, media)
        self._decode(temporary)
        receipt = self._publish(temporary)
        return {**receipt, "media": media}

    def _audio_hash(self, path):
        value = self._command(self._input(path) + ["-map", "0:a:0", "-c:a", "copy", "-f", "hash", "-hash", "sha256", "pipe:1"], output_limit=1024).decode().strip()
        require(re.fullmatch(r"SHA256=[a-fA-F0-9]{64}", value), "audio_validation_failed")
        return value.split("=", 1)[1].lower()

    def _video_hash(self, path):
        value = self._command(self._input(path) + ["-map", "0:v:0", "-c:v", "copy", "-an",
            "-f", "hash", "-hash", "sha256", "pipe:1"], output_limit=1024).decode().strip()
        require(re.fullmatch(r"SHA256=[a-fA-F0-9]{64}", value), "video_validation_failed")
        return value.split("=", 1)[1].lower()

    def _render_subtitles(self, payload, folder, url=None):
        source, clean = (self._object(payload[key]) for key in ("source_id", "clean_id"))
        original, source_video, source_audio = self._probe(source)
        erased, _, _ = self._probe(clean)
        require(0 < original["durationMs"] <= MAX_SECONDS * 1000
                and max(original["width"], original["height"]) <= 3840
                and original["width"] % 2 == 0 and original["height"] % 2 == 0
                and original["audioCodec"] in {None, "aac", "mp3"}, "source_limits")
        # Never stretch, duplicate, drop, resize, or silently trim cloud frames.
        require(all(erased[k] == original[k] for k in ("frame_count", "fpsRatio", "width", "height"))
                and abs(erased["durationMs"] - original["durationMs"]) <= .1, "clean_source_mismatch")
        require(abs(float(source_video.get("start_time", "nan"))) <= .00001, "unsupported_start_time")
        self._cfr(source, original)
        self._cfr(clean, erased)
        cues, style = payload["cues"], payload.get("style", {})
        width, height = original["width"], original["height"]
        require(style.get("margin_x", round(width * .25)) * 2 < width, "invalid_subtitle_style")
        roi = "box" in cues[0]
        for cue in cues:
            require(cue["end"] <= original["durationMs"] / 1000 + .000001, "cue_outside_source")
            if roi:
                box = cue["box"]
                require(box["x"] + box["width"] <= width and box["y"] + box["height"] <= height,
                        "cue_outside_source")
        (folder / "subtitles.ass").write_text(subtitle_ass(cues, width, height, style), encoding="utf-8")
        graph = []
        if roi:
            graph.append("[0:v]setpts=PTS-STARTPTS[base0]")
            labels = "".join(f"[clean{i}]" for i in range(len(cues)))
            graph.append(f"[1:v]setpts=PTS-STARTPTS,split={len(cues)}{labels}")
            for index, cue in enumerate(cues):
                box = cue["box"]
                # Outward even-pixel rounding avoids losing odd-edge 4:2:0 chroma.
                x, y = box["x"] // 2 * 2, box["y"] // 2 * 2
                right = min(width, math.ceil((box["x"] + box["width"]) / 2) * 2)
                bottom = min(height, math.ceil((box["y"] + box["height"]) / 2) * 2)
                graph.append(f"[clean{index}]crop={right-x}:{bottom-y}:{x}:{y}[patch{index}]")
                graph.append(f"[base{index}][patch{index}]overlay=x={x}:y={y}:eof_action=pass:"
                             f"enable='gte(t,{cue['start']:.8f})*lt(t,{cue['end']:.8f})'[base{index+1}]")
            base = f"base{len(cues)}"
        else:
            graph.append("[1:v]setpts=PTS-STARTPTS[base]")
            base = "base"
        # The file is a fixed scratch basename, never a caller-supplied path/filter.
        graph.append(f"[{base}]ass=filename=subtitles.ass[out]")
        temporary = folder / "subtitled.mp4"
        args = self._input(source) + ["-copyts", "-protocol_whitelist", "file,pipe", "-noautorotate", "-i", str(clean),
            "-n", "-filter_complex_threads", "1", "-filter_complex", ";".join(graph),
            "-map", "[out]", "-map", "0:a:0?", "-fps_mode", "passthrough", "-c:v", "libx264",
            "-threads", "2", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy",
            "-map_metadata", "-1", "-avoid_negative_ts", "disabled", "-fs", str(MAX_BYTES),
            "-movflags", "+faststart", str(temporary)]
        self._command(args, cwd=folder)
        measured, output_video, output_audio = self._probe(temporary)
        self._cfr(temporary, measured)
        self._decode(temporary)
        audio_ok = measured["hasAudio"] == original["hasAudio"] and measured["audioCodec"] == original["audioCodec"]
        if audio_ok and source_audio:
            try:
                old_offset = float(source_audio["start_time"]) - float(source_video["start_time"])
                new_offset = float(output_audio["start_time"]) - float(output_video["start_time"])
                audio_ok = abs(old_offset-new_offset) <= .003 and self._audio_hash(source) == self._audio_hash(temporary)
            except (ValueError, KeyError, ProcessingError):
                audio_ok = False
        checks = {"full_decode": True,
            "source_unchanged": digest(source) == original["sha256"] and digest(clean) == erased["sha256"],
            "dimensions_preserved": (measured["width"], measured["height"]) == (width, height),
            "frame_count_preserved": measured["frame_count"] == original["frame_count"],
            "frame_rate_preserved": measured["fpsRatio"] == original["fpsRatio"],
            "duration_preserved": abs(measured["durationMs"] - original["durationMs"]) <= .1,
            "audio_presence_preserved": measured["hasAudio"] == original["hasAudio"],
            "original_audio_preserved": audio_ok}
        require(all(checks.values()), "render_source_mismatch")
        receipt = self._publish(temporary)
        return {"source_id": payload["source_id"], "clean_id": payload["clean_id"], "output": receipt,
            "technical_review": {"status": "passed", "checks": checks, "output_media": measured,
                "output_sha256": receipt["sha256"], "visual_acceptance": "not_performed", "model_reviewed": False,
                "source_audio_reused": True, "composition_mode": "source_roi" if roi else "full_clean",
                "pixel_preservation_guaranteed": False, "video_reencoded": True, "cue_count": len(cues)}}

    def _join(self, payload, folder, url=None):
        split = self.get(payload["split_job_id"])
        require(split["kind"] == "split" and split["status"] == "succeeded"
                and split["result"]["source_id"] == payload["source_id"], "invalid_split")
        require(len(payload["parts"]) == len(split["result"]["parts"]), "invalid_parts")
        source = self._object(payload["source_id"])
        source_info, source_video, source_audio = self._probe(source)
        require(source_info == split["result"]["source_media"], "source_changed")
        paths, concat_paths, data = [], [], []
        for part in payload["parts"]:
            imported_id = self.identity({"kind": "import", "task_id": part["task_id"]})
            imported = self.get(imported_id)
            require(imported["kind"] == "import" and imported["status"] == "succeeded"
                    and imported["result"]["id"] == part["object_id"], "invalid_import")
            path = self._object(part["object_id"])
            info, video, audio = self._probe(path)
            self._cfr(path, info)
            paths.append(path)
            data.append((info, video))
            if audio:
                # An unused AAC stream can still make concat's demuxer add its
                # encoder priming offset to the video. Isolate unchanged video
                # packets before concat, then use only the original audio.
                video_only = folder / f"video-only-{part['index']:03d}.mp4"
                self._command(self._input(path) + ["-n", "-copyts", "-map", "0:v:0", "-c:v", "copy", "-an",
                    "-map_metadata", "-1", "-avoid_negative_ts", "disabled", "-fs", str(MAX_BYTES),
                    "-movflags", "+faststart", str(video_only)])
                stripped, stripped_video, stripped_audio = self._probe(video_only)
                preserved = ("frame_count", "fpsRatio", "width", "height", "durationMs", "videoCodec")
                require(stripped_audio is None and all(stripped[key] == info[key] for key in preserved)
                        and abs(float(stripped_video.get("start_time", "nan")) - float(video.get("start_time", "nan"))) <= .00001
                        and self._video_hash(video_only) == self._video_hash(path), "video_copy_changed")
                self._cfr(video_only, stripped)
                concat_paths.append(video_only)
            else:
                concat_paths.append(path)
        keys = ("codec_name", "profile", "level", "width", "height", "pix_fmt", "time_base", "sample_aspect_ratio", "field_order", "extradata_hash", "avg_frame_rate")
        signature = tuple(data[0][1].get(key) for key in keys)
        require(data[0][1].get("codec_name") in {"h264", "hevc"} and data[0][1].get("extradata_hash")
                and all(tuple(video.get(key) for key in keys) == signature for _, video in data), "incompatible_candidates")
        listing = folder / "inputs.ffconcat"
        lines = ["ffconcat version 1.0"]
        for path, (info, _) in zip(concat_paths, data):
            lines += ["file '" + path.as_posix().replace("'", "'\\''") + "'", f"duration {info['frame_count'] / info['fps']:.12f}"]
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temporary = folder / "joined.mp4"
        self._command([self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-n", "-threads", "2",
            "-protocol_whitelist", "file,pipe", "-f", "concat", "-safe", "0", "-i", str(listing),
            "-protocol_whitelist", "file,pipe", "-i", str(source), "-map", "0:v:0", "-map", "1:a:0?",
            "-c:v", "copy", "-c:a", "copy", "-map_metadata", "-1", "-fs", str(MAX_BYTES), "-movflags", "+faststart", str(temporary)])
        measured, output_video, output_audio = self._probe(temporary)
        count = sum(info["frame_count"] for info, _ in data)
        require(measured["frame_count"] == count and measured["fpsRatio"] == data[0][0]["fpsRatio"]
                and abs(measured["durationMs"] - count * 1000 / measured["fps"]) <= 500 / measured["fps"], "join_mismatch")
        self._cfr(temporary, measured)
        self._decode(temporary)
        audio_ok = source_info["hasAudio"] == measured["hasAudio"] and source_info["audioCodec"] == measured["audioCodec"]
        if audio_ok and source_audio:
            try:
                original_offset = float(source_audio["start_time"]) - float(source_video["start_time"])
                new_offset = float(output_audio["start_time"]) - float(output_video["start_time"])
                audio_ok = abs(original_offset - new_offset) <= .003 and self._audio_hash(source) == self._audio_hash(temporary)
            except (ValueError, KeyError, ProcessingError):
                audio_ok = False
        require(digest(source) == source_info["sha256"]
                and all(digest(path) == info["sha256"] for path, (info, _) in zip(paths, data)), "source_changed")
        checks = {"full_decode": True, "source_unchanged": True,
                  "dimensions_preserved": (measured["width"], measured["height"]) == (source_info["width"], source_info["height"]),
                  "duration_preserved": abs(measured["durationMs"] - source_info["durationMs"]) <= 500 / source_info["fps"],
                  "frame_rate_preserved": abs(measured["fps"] - source_info["fps"]) < .001,
                  "audio_presence_preserved": measured["hasAudio"] == source_info["hasAudio"],
                  "original_audio_preserved": audio_ok}
        receipt = self._publish(temporary)
        review = {"status": "passed" if all(checks.values()) else "failed", "checks": checks,
                  "output_media": measured, "output_sha256": receipt["sha256"], "visual_acceptance": "not_performed",
                  "model_reviewed": False, "source_audio_reused": True, "segment_count": len(paths),
                  "boundary_visual_review": "not_performed"}
        return {"output": receipt, "technical_review": review, "parts": payload["parts"]}
