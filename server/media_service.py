"""Private media staging and mechanical server jobs for Seedance.

This service never receives an Ark API key and never calls a model. Deploy only
behind the existing HTTPS proxy. Object IDs are SHA-256 hashes of uploaded bytes.
"""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from urllib.parse import parse_qs, urlsplit

VERSION = "1.3.0"
PREFIX = "/codex-media/v1"
MAX_BYTES = 200 * 1024**2
RETENTION = 3 * 86400
MAX_TICKET = 12 * 3600
OBJECT = re.compile(r"^[a-f0-9]{64}$")
ROUTE = re.compile(r"^/codex-media/v1/objects/([a-f0-9]{64})(/ticket|/upload|\.mp4)?$")
JOB_ROUTE = re.compile(r"^/codex-media/v1/jobs(?:/([a-f0-9]{64}))?$")
MAX_JOB_BODY = 64 * 1024


class Failure(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


class Store:
    def __init__(self, root, token, signing_key, origin, *, quota=10 * 1024**3, clock=time.time):
        if not re.fullmatch(r"[a-f0-9]{64}", token) or not re.fullmatch(r"[a-f0-9]{64}", signing_key):
            raise ValueError("invalid credentials")
        parsed = urlsplit(origin)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.port not in (None, 443)
                or parsed.path or parsed.query or parsed.fragment or parsed.username):
            raise ValueError("invalid origin")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.token, self.signing_key, self.origin = token, signing_key.encode(), origin
        self.quota, self.clock = quota, clock
        self.upload_lock = threading.Lock()

    def path(self, object_id):
        if not OBJECT.fullmatch(object_id):
            raise Failure(400, "invalid_id")
        return self.root / (object_id + ".mp4")

    def info(self, object_id):
        path = self.path(object_id)
        try:
            st = path.lstat()
        except FileNotFoundError:
            raise Failure(404, "missing") from None
        if path.is_symlink() or not path.is_file() or st.st_nlink != 1 or not 0 < st.st_size <= MAX_BYTES:
            raise Failure(409, "invalid_object")
        if st.st_mtime + RETENTION <= self.clock():
            raise Failure(410, "expired")
        return {"id": object_id, "sha256": object_id, "bytes": st.st_size}

    def signature(self, object_id, expires):
        message = f"read-v1\n{object_id}\n{expires}".encode()
        return hmac.new(self.signing_key, message, hashlib.sha256).hexdigest()

    def ticket(self, object_id, lifetime=6 * 3600):
        info = self.info(object_id)
        expires = int(self.clock()) + lifetime
        if not 60 <= lifetime <= MAX_TICKET:
            raise Failure(400, "invalid_expiry")
        # An authenticated ticket keeps its source through the entire lease.
        os.utime(self.path(object_id), (self.clock(), self.clock()))
        return {**info, "expires_at": expires,
                "url": f"{self.origin}{PREFIX}/objects/{object_id}.mp4?expires={expires}&signature={self.signature(object_id, expires)}"}

    def authorize_read(self, object_id, query):
        try:
            values = parse_qs(query, keep_blank_values=True, max_num_fields=4)
        except ValueError:
            raise Failure(403, "invalid_signature") from None
        if set(values) != {"expires", "signature"} or any(len(v) != 1 for v in values.values()):
            raise Failure(403, "invalid_signature")
        expiry, signature = values["expires"][0], values["signature"][0]
        if not re.fullmatch(r"[0-9]{10,12}", expiry) or not re.fullmatch(r"[a-f0-9]{64}", signature):
            raise Failure(403, "invalid_signature")
        expiry = int(expiry)
        if not self.clock() < expiry <= self.clock() + MAX_TICKET + 60:
            raise Failure(403, "expired_signature")
        if not hmac.compare_digest(self.signature(object_id, expiry), signature):
            raise Failure(403, "invalid_signature")
        return self.info(object_id)

    def cleanup_and_usage(self):
        total = 0
        for path in self.root.iterdir():
            if re.fullmatch(r"\.partial-[a-f0-9]{64}", path.name) and not path.is_symlink() and path.is_file():
                if path.stat().st_mtime + RETENTION <= self.clock():
                    path.unlink()
                else:
                    total += path.stat().st_size
                continue
            if re.fullmatch(r"\.upload-[a-f0-9]{32}", path.name) and not path.is_symlink() and path.is_file():
                if path.stat().st_mtime + 7200 <= self.clock():
                    path.unlink()
                else:
                    total += path.stat().st_size
                continue
            if not re.fullmatch(r"[a-f0-9]{64}\.mp4", path.name) or path.is_symlink():
                continue
            st = path.stat()
            if st.st_mtime + RETENTION <= self.clock():
                path.unlink()
            else:
                total += st.st_size
        return total


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store, processor=None):
        self.store = store
        self.processor = processor
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # No traceback may disclose request URLs, authorization, or local paths.
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PrivateMedia/1"
    sys_version = ""

    def log_message(self, format, *args):
        pass  # Signed query strings and request credentials must never be logged.

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def json(self, status, value):
        body = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(body)

    def authenticated(self):
        headers = self.headers.get_all("Authorization", [])
        expected = "Bearer " + self.server.store.token
        if len(headers) != 1 or not hmac.compare_digest(headers[0].encode(), expected.encode()):
            raise Failure(401, "unauthorized")

    def route(self):
        if len(self.path) > 1024:
            raise Failure(400, "invalid_path")
        parsed = urlsplit(self.path)
        match = ROUTE.fullmatch(parsed.path)
        if not match or parsed.fragment:
            raise Failure(404, "missing")
        return match[1], match[2], parsed.query

    def handle_one(self):
        if self.command == "GET" and self.path == "/codex-media/healthz":
            processing = {"available": False, "ffmpeg_ready": False, "ffprobe_ready": False}
            if self.server.processor is not None:
                value = self.server.processor.health()
                processing = {name: value[name] for name in ("available", "ffmpeg_ready", "ffprobe_ready",
                    "policy", "max_source_seconds", "max_parts") if name in value}
            return self.json(200, {"ok": True, "version": VERSION, "processing": processing})
        if urlsplit(self.path).path.startswith(PREFIX + "/jobs"):
            return self.processing_job()
        object_id, action, query = self.route()
        store = self.server.store
        if action == ".mp4" and self.command in {"GET", "HEAD"}:
            info = store.authorize_read(object_id, query)
            return self.stream(store.path(object_id), info["bytes"])
        if action == "/upload":
            self.authenticated()
            return self.upload_chunk(object_id, query)
        if query:
            raise Failure(400, "invalid_query")
        self.authenticated()
        if self.command == "GET" and action is None:
            return self.json(200, store.info(object_id))
        if self.command == "POST" and action == "/ticket":
            if self.headers.get("Content-Length", "0") != "0" or self.headers.get("Transfer-Encoding"):
                raise Failure(400, "unexpected_body")
            return self.json(200, store.ticket(object_id))
        if self.command == "PUT" and action is None:
            return self.upload(object_id)
        if self.command == "DELETE" and action is None:
            with store.upload_lock:
                path = store.path(object_id)
                if path.exists():
                    store.info(object_id)
                    path.unlink()
            return self.json(200, {"deleted": True})
        raise Failure(405, "method_not_allowed")

    def processing_job(self):
        self.authenticated()
        parsed = urlsplit(self.path)
        match = JOB_ROUTE.fullmatch(parsed.path)
        if len(self.path) > 1024 or not match or parsed.query or parsed.fragment:
            raise Failure(400, "invalid_job_path")
        processor = self.server.processor
        if processor is None:
            raise Failure(503, "processing_unavailable")
        if self.command == "GET" and match[1]:
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0":
                raise Failure(400, "unexpected_body")
            operation, value = processor.get, match[1]
        elif self.command == "POST" and not match[1]:
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,6}", lengths[0]):
                raise Failure(411, "length_required")
            size = int(lengths[0])
            if not 1 <= size <= MAX_JOB_BODY:
                raise Failure(413, "too_large")
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                raise Failure(415, "invalid_media_type")
            body = self.rfile.read(size)
            if len(body) != size:
                raise Failure(400, "incomplete_body")
            def unique_object(pairs):
                result = {}
                for key, item in pairs:
                    if key in result:
                        raise ValueError("duplicate key")
                    result[key] = item
                return result
            try:
                value = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object)
            except (ValueError, UnicodeError):
                raise Failure(400, "invalid_json") from None
            if not isinstance(value, dict):
                raise Failure(400, "invalid_job")
            fields = {"split": {"kind", "source_id", "policy"},
                      "import": {"kind", "task_id", "url"},
                      "join": {"kind", "source_id", "split_job_id", "parts"},
                      "render_subtitles": {"kind", "source_id", "clean_id", "cues"}}
            kind = value.get("kind")
            actual = set(value) - ({"style"} if kind == "render_subtitles" else set())
            if not isinstance(kind, str) or kind not in fields or actual != fields[kind]:
                raise Failure(400, "invalid_job")
            operation = processor.submit
        else:
            raise Failure(405, "method_not_allowed")
        try:
            result = operation(value)
        except Exception as error:
            # Worker errors are codes, never exception prose or a signed URL.
            code, status = getattr(error, "code", "processing_error"), getattr(error, "status", 500)
            if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
                code = "processing_error"
            if type(status) is not int or not 400 <= status <= 599:
                status = 500
            raise Failure(status, code) from None
        return self.json(202 if self.command == "POST" and result.get("status") in {"queued", "running"} else 200, result)

    def upload_chunk(self, object_id, query):
        store = self.server.store
        partial = store.root / (".partial-" + object_id)
        with store.upload_lock:
            if partial.exists() and (partial.is_symlink() or not partial.is_file() or partial.stat().st_nlink != 1):
                raise Failure(409, "invalid_partial")
            received = partial.stat().st_size if partial.exists() else 0
            if self.command == "GET" and not query:
                return self.json(200, {"id":object_id, "sha256":object_id, "received_bytes":received})
            if self.command != "PUT":
                raise Failure(405, "method_not_allowed")
            values = parse_qs(query, max_num_fields=2)
            if (set(values) != {"offset", "total"} or any(len(v) != 1 or not re.fullmatch(r"[0-9]{1,10}", v[0]) for v in values.values())):
                raise Failure(400, "invalid_range")
            offset, total = int(values["offset"][0]), int(values["total"][0])
            lengths = self.headers.get_all("Content-Length", [])
            if (self.headers.get("Transfer-Encoding") or len(lengths) != 1
                    or not re.fullmatch(r"[0-9]{1,7}", lengths[0])):
                raise Failure(411, "length_required")
            count = int(lengths[0])
            if self.headers.get("Content-Type") != "application/octet-stream":
                raise Failure(415, "invalid_media_type")
            if not 12 <= total <= MAX_BYTES or not 0 <= count <= 256*1024 or offset + count > total:
                raise Failure(413, "too_large")
            # Commit only complete bounded chunks. A lost response is recovered
            # with GET /upload; no model task is involved in this operation.
            blocks, received_now = [], 0
            print(json.dumps({"event":"chunk_receiving","expected_bytes":count}), flush=True)
            try:
                while received_now < count:
                    block = self.rfile.read1(min(64*1024, count-received_now))
                    if not block:
                        raise Failure(400, "incomplete_upload")
                    blocks.append(block)
                    received_now += len(block)
            except (OSError, Failure) as error:
                print(json.dumps({"event":"chunk_interrupted","received_bytes":received_now,
                                  "timeout":isinstance(error, TimeoutError)}), flush=True)
                raise
            data = b"".join(blocks)
            print(json.dumps({"event":"chunk_received","received_bytes":received_now}), flush=True)
            # Drain the authenticated bounded body before a conflict response,
            # otherwise closing over unread bytes can reset the response socket.
            if offset != received or count == 0 and offset != total:
                raise Failure(409, "offset_mismatch")
            # info() reports an expired complete object as 410 while its bytes
            # remain on disk. Remove expired objects before checking for a live
            # destination, so the client's hash-bound reupload can make progress.
            usage = store.cleanup_and_usage()
            if store.path(object_id).exists():
                raise Failure(409, "already_complete")
            if usage + count > store.quota or shutil.disk_usage(store.root).free < count + 2*1024**3:
                raise Failure(507, "storage_full")
            if received and not partial.exists():
                raise Failure(409, "expired_partial")
            with partial.open("ab") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            received += count
            if received == total:
                digest = hashlib.sha256()
                with partial.open("rb") as source:
                    first = source.read(12)
                    source.seek(0)
                    for block in iter(lambda: source.read(256*1024), b""):
                        digest.update(block)
                if first[4:8] != b"ftyp" or digest.hexdigest() != object_id:
                    partial.unlink()
                    raise Failure(422, "hash_mismatch")
                os.replace(partial, store.path(object_id))
                return self.json(200, dict(store.info(object_id), complete=True))
            return self.json(200, {"id":object_id, "sha256":object_id, "received_bytes":received, "complete":False})

    def upload(self, object_id):
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
            raise Failure(411, "length_required")
        value = self.headers["Content-Length"]
        if not value.isdigit() or not 12 <= int(value) <= MAX_BYTES:
            raise Failure(413, "too_large")
        if self.headers.get("Content-Type", "").split(";")[0] != "video/mp4":
            raise Failure(415, "invalid_media_type")
        store, count = self.server.store, int(value)
        with store.upload_lock:
            usage = store.cleanup_and_usage()
            if usage + count > store.quota or shutil.disk_usage(store.root).free < count + 2 * 1024**3:
                raise Failure(507, "storage_full")
            temp = store.root / (".upload-" + secrets.token_hex(16))
            digest, remaining, first = hashlib.sha256(), count, b""
            deadline = time.monotonic() + 600
            try:
                with temp.open("xb") as target:
                    while remaining:
                        if time.monotonic() >= deadline:
                            raise Failure(408, "upload_timeout")
                        block = self.rfile.read(min(256 * 1024, remaining))
                        if not block:
                            raise Failure(400, "incomplete_upload")
                        if not first:
                            first = block[:12]
                        target.write(block)
                        digest.update(block)
                        remaining -= len(block)
                    target.flush()
                    os.fsync(target.fileno())
                if len(first) < 12 or first[4:8] != b"ftyp":
                    raise Failure(415, "invalid_mp4")
                if digest.hexdigest() != object_id:
                    raise Failure(422, "hash_mismatch")
                destination = store.path(object_id)
                try:
                    os.link(temp, destination)
                except FileExistsError:
                    # Same content-addressed identity is safe to resume after a lost reply.
                    existing = store.info(object_id)
                    if existing["bytes"] != count:
                        raise Failure(409, "invalid_object")
                temp.unlink()
                os.utime(destination, None)
                return self.json(200, store.info(object_id))
            finally:
                temp.unlink(missing_ok=True)

    def stream(self, path, size):
        start, end, status = 0, size - 1, 200
        value = self.headers.get("Range")
        if value:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
            if not match or not (match[1] or match[2]):
                raise Failure(416, "invalid_range")
            if not match[1]:
                count = int(match[2])
                if count < 1:
                    raise Failure(416, "invalid_range")
                start = max(0, size - count)
            else:
                start = int(match[1])
                end = min(size - 1, int(match[2])) if match[2] else size - 1
            if start >= size or end < start:
                raise Failure(416, "invalid_range")
            status = 206
        with path.open("rb") as source:
            self.send_response(status)
            for key, value in {"Content-Type": "video/mp4", "Content-Length": str(end-start+1),
                               "Accept-Ranges": "bytes", "Cache-Control": "private, no-store",
                               "X-Content-Type-Options": "nosniff", "Connection": "close"}.items():
                self.send_header(key, value)
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            self.close_connection = True
            if self.command == "HEAD":
                return
            source.seek(start)
            remaining = end-start+1
            while remaining:
                block = source.read(min(256 * 1024, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)

    def dispatch(self):
        self.response_started = False
        try:
            self.handle_one()
        except Failure as error:
            self.json(error.status, {"error": error.code})
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except (OSError, ValueError):
            if self.response_started:
                self.close_connection = True
            else:
                self.json(500, {"error": "storage_error"})

    def end_headers(self):
        self.response_started = True
        super().end_headers()

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = dispatch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    options = parser.parse_args()
    config = json.loads(Path(options.config).read_text())
    store = Store(config["root"], config["upload_token"], config["signing_key"], config["origin"])
    from media_processing import Processor
    processor = Processor(store, ffmpeg=config.get("ffmpeg", "ffmpeg"), ffprobe=config.get("ffprobe", "ffprobe"))
    server = Server(("127.0.0.1", 8791), store, processor)
    def stop(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()
    for name in ("SIGTERM", "SIGINT"):
        signal.signal(getattr(signal, name), stop)
    try:
        server.serve_forever()
    finally:
        processor.close(wait=False)
        server.server_close()


if __name__ == "__main__":
    main()
