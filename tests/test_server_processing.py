"""Original synthetic media and mock HTTPS only; no cloud API or credentials."""
from fractions import Fraction
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location("server_processing_core", Path(__file__).resolve().parents[1] / "server/media_processing.py")
processing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(processing)


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.upload_lock = threading.Lock()
        self.quota = 10 * 1024**3

    def path(self, key):
        if not isinstance(key, str) or not processing.ID.fullmatch(key):
            raise ValueError("bad id")
        return self.root / (key + ".mp4")

    def info(self, key):
        path = self.path(key)
        size = processing.regular(path)
        if path.stat().st_mtime + processing.RETENTION <= time.time():
            raise ValueError("expired")
        return {"id": key, "sha256": key, "bytes": size}

    def cleanup_and_usage(self):
        return sum(p.stat().st_size for p in self.root.glob("*.mp4") if p.is_file())

    def add(self, source):
        key = processing.digest(source)
        target = self.path(key)
        if not target.exists():
            shutil.copyfile(source, target)
        return key


def wait(processor, job, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = processor.get(job["id"])
        if state["status"] in {"failed", "succeeded"}:
            return state
        time.sleep(.02)
    raise AssertionError("worker timeout")


class ProcessingCoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(temporary.name)
        self.processor = processing.Processor(self.store, sys.executable, sys.executable)
        self.addCleanup(self.processor.close, True)

    def fake_object(self):
        path = self.store.root / "fixture.bin"
        path.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"test" * 100)
        key = self.store.add(path)
        path.unlink()
        return self.store.info(key)

    def test_fixed_fifteen_second_ranges_cover_source_without_overlap(self):
        self.assertEqual(processing.frame_ranges(2604, 60), [(0, 900), (900, 1800), (1800, 2604)])
        self.assertEqual(processing.frame_ranges(31 * 24, 24), [(0, 360), (360, 648), (648, 744)])
        self.assertEqual(processing.frame_ranges(16 * 24, 24), [(0, 288), (288, 384)])
        self.assertEqual(len(processing.frame_ranges(300 * 60, 60)), 20)
        for count in (4 * 24, 361, 719, 744, 7200):
            ranges = processing.frame_ranges(count, 24)
            self.assertEqual(ranges[0][0], 0)
            self.assertEqual(ranges[-1][1], count)
            self.assertTrue(all(4 <= (end - start) / 24 <= 15 for start, end in ranges))
            self.assertEqual(sum(end - start for start, end in ranges), count)
        with self.assertRaises(processing.ProcessingError):
            processing.frame_ranges(301 * 60, 60)

    def test_rejects_unsafe_urls_dns_and_redirects_without_sending_credentials(self):
        for url in ("http://example.com/a", "https://127.0.0.1/a", "https://169.254.169.254/a",
                    "https://localhost/a", "https://user:secret@example.com/a", "https://example.com:444/a",
                    "https://example.internal/a", "https://example.com/a#fragment"):
            with self.subTest(url=url), self.assertRaises(processing.ProcessingError):
                processing.checked_url(url)
        with patch.object(processing.socket, "getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaisesRegex(processing.ProcessingError, "unsafe_download"):
                processing.public_addresses("example.com")
        conn = Mock()
        conn.getresponse.return_value.status = 302
        with patch.object(processing, "public_addresses", return_value=["93.184.216.34"]), \
             patch.object(processing, "PinnedHTTPS", return_value=conn):
            with self.assertRaises(processing.ProcessingError):
                processing.download("https://example.com/video?secret=temporary", self.store.root / "new.mp4")
        headers = conn.request.call_args.kwargs["headers"]
        self.assertNotIn("Authorization", headers)
        self.assertEqual(conn.request.call_count, 1)

    def test_https_connection_uses_checked_ip_and_original_tls_name(self):
        context, raw = Mock(), Mock()
        with patch.object(processing.ssl, "create_default_context", return_value=context), \
             patch.object(processing.socket, "create_connection", return_value=raw) as connect:
            conn = processing.PinnedHTTPS("example.com", "93.184.216.34")
            conn.connect()
        connect.assert_called_once_with(("93.184.216.34", 443), 30)
        context.wrap_socket.assert_called_once_with(raw, server_hostname="example.com")

    def test_import_is_idempotent_url_not_saved_and_expired_result_can_resume(self):
        receipt = self.fake_object()
        entered, release = threading.Event(), threading.Event()
        def fake_import(payload, folder, url):
            entered.set()
            self.assertTrue(release.wait(10))
            return dict(receipt, media={})
        with patch.object(self.processor, "_import", side_effect=fake_import) as run:
            request = {"kind": "import", "task_id": "cgt-synthetic-1", "url": "https://example.com/video?token=private-one"}
            first = self.processor.submit(request)
            self.assertTrue(entered.wait(5))
            second = self.processor.submit(dict(request, url="https://example.com/video?token=private-two"))
            self.assertEqual(first["id"], second["id"])
            state_text = (self.processor.root / first["id"] / "state.json").read_text()
            self.assertNotIn("private-", state_text)
            self.assertNotIn("https://", state_text)
            release.set()
            self.assertEqual(wait(self.processor, first)["status"], "succeeded")
            self.processor.submit(request)
            self.assertEqual(run.call_count, 1)
        self.store.path(receipt["id"]).unlink()
        self.assertEqual(self.processor.get(first["id"])["error"], "result_expired")
        receipt = self.fake_object()
        with patch.object(self.processor, "_import", return_value=dict(receipt, media={})):
            resumed = self.processor.submit(request)
            self.assertEqual(wait(self.processor, resumed)["status"], "succeeded")
            self.assertEqual(first["id"], resumed["id"])

    def test_restart_marks_incomplete_jobs_interrupted_and_resubmits_same_id(self):
        receipt = self.fake_object()
        request = {"kind": "split", "source_id": receipt["id"], "policy": processing.POLICY}
        job_id = self.processor.identity(request)
        self.processor.close(True)
        self.processor._write({"id": job_id, "kind": "split", "request": request, "status": "running",
                               "created_at": time.time(), "updated_at": time.time()})
        newer = processing.Processor(self.store, sys.executable, sys.executable)
        self.addCleanup(newer.close, True)
        self.assertEqual(newer.get(job_id)["error"], "interrupted")
        result = {"source_id": receipt["id"], "parts": [receipt]}
        with patch.object(newer, "_split", return_value=result):
            resumed = newer.submit(request)
            self.assertEqual(wait(newer, resumed)["status"], "succeeded")
            self.assertEqual(resumed["id"], job_id)

    def test_failed_job_exposes_only_safe_code(self):
        with patch.object(self.processor, "_import", side_effect=RuntimeError("https://private.invalid?signature=secret")):
            value = wait(self.processor, self.processor.submit({"kind": "import", "task_id": "cgt-synthetic-failure",
                                    "url": "https://example.com/video?token=private"}))
        self.assertEqual(value["error"], "processing_failed")
        self.assertNotIn("secret", json.dumps(value))
        self.assertNotIn("https://", json.dumps(value))

    def test_queue_is_bounded_and_closed_processor_rejects_new_work(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(payload, folder, url):
            entered.set()
            release.wait(10)
            raise processing.ProcessingError("synthetic_failure")
        try:
            with patch.object(self.processor, "_import", side_effect=blocked):
                self.processor.submit({"kind": "import", "task_id": "cgt-queue-active", "url": "https://example.com/a"})
                self.assertTrue(entered.wait(5))
                for index in range(20):
                    self.processor.submit({"kind": "import", "task_id": f"cgt-queue-{index}", "url": "https://example.com/a"})
                with self.assertRaisesRegex(processing.ProcessingError, "processing_busy"):
                    self.processor.submit({"kind": "import", "task_id": "cgt-queue-overflow", "url": "https://example.com/a"})
                self.processor.close()
                with self.assertRaisesRegex(processing.ProcessingError, "unavailable"):
                    self.processor.submit({"kind": "import", "task_id": "cgt-after-close", "url": "https://example.com/a"})
                release.set()
        finally:
            release.set()


MEDIA = all(os.environ.get(name) for name in ("VIDEO_LOCALIZER_TEST_FFMPEG", "VIDEO_LOCALIZER_TEST_FFPROBE"))


@unittest.skipUnless(MEDIA, "Set VIDEO_LOCALIZER_TEST_FFMPEG/FFPROBE")
class ServerProcessingMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name).resolve()
        cls.ffmpeg, cls.ffprobe = (os.environ["VIDEO_LOCALIZER_TEST_" + key] for key in ("FFMPEG", "FFPROBE"))
        cls.source = cls.root / "new-synthetic-43s.mp4"
        subprocess.run([cls.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=640x640:r=60",
            "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000", "-t", "43.4", "-c:v", "libx264", "-threads", "2",
            "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(cls.source)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
        cls.store = Store(cls.root / "objects")
        cls.processor = processing.Processor(cls.store, cls.ffmpeg, cls.ffprobe)
        cls.addClassCleanup(cls.processor.close, True)
        cls.source_id = cls.store.add(cls.source)
        cls.split = wait(cls.processor, cls.processor.submit({"kind": "split", "source_id": cls.source_id, "policy": processing.POLICY}))
        if cls.split["status"] != "succeeded":
            raise AssertionError(cls.split)

    def candidates(self, suffix, extra_frame=False, context_audio=False):
        parts = []
        for part in self.split["result"]["parts"]:
            cloud = self.root / f"cloud-{suffix}-{part['index']}.mp4"
            args = [self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "2", "-filter_threads", "1",
                    "-i", str(self.store.path(part["id"])), "-map", "0:v:0"]
            args += ["-map", "0:a:0?", "-c:a", "copy"] if context_audio else ["-an"]
            if extra_frame:
                args += ["-vf", "tpad=stop_mode=clone:stop=1", "-c:v", "libx264", "-threads", "2", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
            else:
                args += ["-c:v", "copy"]
            subprocess.run(args + [str(cloud)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
            task_id = f"cgt-synthetic-{suffix}-{part['index']}"
            with patch.object(processing, "download", side_effect=lambda url, target, src=cloud: shutil.copyfile(src, target)):
                imported = wait(self.processor, self.processor.submit({"kind": "import", "task_id": task_id,
                    "url": f"https://example.com/cloud/{suffix}/{part['index']}?token=temporary"}))
            self.assertEqual(imported["status"], "succeeded", imported)
            parts.append({"index": part["index"], "task_id": task_id, "object_id": imported["result"]["id"]})
        return parts

    def test_exact_server_split_join_and_original_audio_without_local_segment_download(self):
        parts = self.split["result"]["parts"]
        self.assertEqual([(p["start_frame"], p["end_frame"]) for p in parts], [(0, 900), (900, 1800), (1800, 2604)])
        self.assertEqual([p["duration_seconds"] for p in parts], [15.0, 15.0, 13.4])
        request = {"kind": "join", "source_id": self.source_id, "split_job_id": self.split["id"], "parts": self.candidates("exact")}
        joined = wait(self.processor, self.processor.submit(request))
        self.assertEqual(joined["status"], "succeeded", joined)
        review = joined["result"]["technical_review"]
        self.assertEqual(review["status"], "passed", review)
        self.assertEqual(review["output_media"]["frame_count"], 2604)
        self.assertEqual(review["visual_acceptance"], "not_performed")
        self.assertTrue(review["checks"]["original_audio_preserved"])
        self.assertEqual(processing.digest(self.source), self.source_id)
        self.assertEqual(self.processor.submit(request)["id"], joined["id"])

    def test_cloud_extra_frames_are_kept_and_fail_strict_duration_review(self):
        request = {"kind": "join", "source_id": self.source_id, "split_job_id": self.split["id"], "parts": self.candidates("drift", True)}
        joined = wait(self.processor, self.processor.submit(request))
        self.assertEqual(joined["status"], "succeeded", joined)
        review = joined["result"]["technical_review"]
        self.assertEqual(review["status"], "failed")
        self.assertFalse(review["checks"]["duration_preserved"])
        self.assertTrue(review["checks"]["original_audio_preserved"])
        self.assertEqual(review["output_media"]["frame_count"], 2607)

    def test_candidate_context_audio_cannot_shift_original_audio_sync(self):
        request = {"kind": "join", "source_id": self.source_id, "split_job_id": self.split["id"],
                   "parts": self.candidates("with-aac", context_audio=True)}
        joined = wait(self.processor, self.processor.submit(request))
        self.assertEqual(joined["status"], "succeeded", joined)
        review = joined["result"]["technical_review"]
        self.assertEqual(review["status"], "passed", review)
        self.assertTrue(review["checks"]["original_audio_preserved"])
        self.assertEqual(review["output_media"]["frame_count"], 2604)

    def test_join_rejects_import_task_object_substitution(self):
        parts = [{"index": p["index"], "task_id": f"cgt-not-imported-{p['index']}", "object_id": p["id"]}
                 for p in self.split["result"]["parts"]]
        joined = wait(self.processor, self.processor.submit({"kind": "join", "source_id": self.source_id,
                         "split_job_id": self.split["id"], "parts": parts}))
        self.assertEqual(joined["status"], "failed")
        self.assertIn(joined["error"], {"missing_job", "invalid_import"})


if __name__ == "__main__":
    unittest.main()
