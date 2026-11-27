"""Mechanical render contract and isolated synthetic media; no cloud calls."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from test_server_processing import Store, processing, wait
from video_localizer.selfhost_media import MediaClient
from video_localizer.seedance_client import SeedanceError


def request(source="a" * 64, clean="b" * 64):
    return {"kind": "render_subtitles", "source_id": source, "clean_id": clean,
            "cues": [{"start": .2, "end": 1.8, "text": "사저, 전투력 왜 이리 빨리 올라?",
                      "box": {"x": 50, "y": 210, "width": 220, "height": 40}}],
            "style": {"font_name": "Noto Sans CJK KR", "font_size": 18, "outline": 2}}


class RenderContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(temporary.name)
        self.processor = processing.Processor(self.store, sys.executable, sys.executable)
        self.addCleanup(self.processor.close, True)

    def objects(self):
        found = []
        for name in ("source", "clean", "output"):
            path = self.store.root / (name + ".bin")
            path.write_bytes(b"\x00\x00\x00\x18ftypisom" + name.encode() * 100)
            found.append(self.store.add(path))
            path.unlink()
        return found

    def test_real_mediakit_id_import_only_seedance_join_validation_unchanged(self):
        for task in ("cgt-before", "amk-actual-20261127"):
            payload = {"kind": "import", "task_id": task, "url": "https://example.com/video.mp4"}
            self.assertEqual(self.processor._validate(payload), {"kind": "import", "task_id": task})
            self.assertEqual(MediaClient._job_payload(payload), payload)
        payload = {"kind": "join", "source_id": "a" * 64, "split_job_id": "b" * 64,
                   "parts": [{"index": 1, "task_id": "amk-not-seedance", "object_id": "c" * 64}]}
        with self.assertRaises(processing.ProcessingError):
            self.processor._validate(payload)
        with self.assertRaises(SeedanceError):
            MediaClient._job_payload(payload)

    def test_invalid_timing_boxes_styles_and_ass_injection_rejected_at_both_ends(self):
        bad = []
        for key, value in (("start", float("nan")), ("start", True), ("end", 301), ("end", .1),
                           ("text", r"{\pos(1,1)}bad"), ("text", "bad\x00text")):
            payload = request()
            payload["cues"][0][key] = value
            bad.append(payload)
        payload = request()
        payload["cues"].append({"start": 2, "end": 3, "text": "다음"})
        bad.append(payload)
        for box in ({"x": -1, "y": 1, "width": 2, "height": 2},
                    {"x": 1, "y": 1, "width": 0, "height": 2}, {"x": 1}):
            payload = request()
            payload["cues"][0]["box"] = box
            bad.append(payload)
        for style in ({"font_name": "font:evil"}, {"font_size": True}, {"font_size": 300}, {"filter": "movie=x"}):
            payload = request()
            payload["style"] = style
            bad.append(payload)
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(processing.ProcessingError):
                self.processor._validate(payload)
            with self.subTest(payload=payload), self.assertRaises(SeedanceError):
                MediaClient._job_payload(payload)

    def test_deterministic_job_text_bound_and_result_expiry_checks_both_sources(self):
        source, clean, output = self.objects()
        payload = request(source, clean)
        result = {"source_id": source, "clean_id": clean, "output": self.store.info(output), "technical_review": {}}
        with patch.object(self.processor, "_render_subtitles", return_value=result) as run:
            first = wait(self.processor, self.processor.submit(payload))
            second = self.processor.submit(copy.deepcopy(payload))
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(run.call_count, 1)
            changed = copy.deepcopy(payload)
            changed["cues"][0]["text"] = "다른 번역"
            self.assertNotEqual(self.processor.identity(changed), first["id"])
        self.store.path(clean).unlink()
        self.assertEqual(self.processor.get(first["id"])["error"], "result_expired")

    def test_interrupted_render_resumes_same_identity(self):
        source, clean, output = self.objects()
        payload = request(source, clean)
        job_id = self.processor.identity(payload)
        self.processor.close(True)
        self.processor._write({"id": job_id, "kind": "render_subtitles", "request": payload,
                               "status": "running", "created_at": time.time(), "updated_at": time.time()})
        newer = processing.Processor(self.store, sys.executable, sys.executable)
        self.addCleanup(newer.close, True)
        self.assertEqual(newer.get(job_id)["error"], "interrupted")
        with patch.object(newer, "_render_subtitles", return_value={"output": self.store.info(output)}):
            self.assertEqual(wait(newer, newer.submit(payload))["status"], "succeeded")

    def test_mismatched_cloud_frames_fail_before_encoding(self):
        source, clean, _ = self.objects()
        info = {"width": 320, "height": 320, "durationMs": 2000, "fpsRatio": "30/1",
                "frame_count": 60, "audioCodec": None}
        with patch.object(self.processor, "_probe", side_effect=[(info, {}, None),
                (dict(info, frame_count=61), {}, None)]), patch.object(self.processor, "_command") as command:
            with self.assertRaisesRegex(processing.ProcessingError, "clean_source_mismatch"):
                self.processor._render_subtitles(request(source, clean), self.store.root)
        command.assert_not_called()

    def test_ass_keeps_korean_literal_and_valid_boundaries(self):
        payload = request()
        rendered = processing.subtitle_ass(payload["cues"], 320, 320, payload["style"])
        self.assertIn(payload["cues"][0]["text"], rendered)
        self.assertIn("0:00:00.20,0:00:01.80", rendered)
        self.assertIn("Noto Sans CJK KR", rendered)

    def test_narrow_erasure_box_does_not_turn_korean_into_vertical_column(self):
        cue = {"start": 4, "end": 5, "text": "틀렸어.",
               "box": {"x": 605, "y": 450, "width": 67, "height": 40}}
        rendered = processing.subtitle_ass([cue], 1280, 720, {"font_size": 32})
        event = next(line for line in rendered.splitlines() if line.startswith("Dialogue:"))
        columns = event.split(",", 9)
        self.assertEqual(columns[5:8], ["320", "320", "230"])
        self.assertEqual(columns[9], "틀렸어.")
        self.assertGreater(1280 - int(columns[5]) - int(columns[6]), cue["box"]["width"] * 8)
        customized = processing.subtitle_ass([cue], 1280, 720, {"margin_x": 200})
        self.assertIn("Default,,200,200,230,,틀렸어.", customized)

    def test_client_preserves_honest_composition_report_and_checks_binding(self):
        client = MediaClient.__new__(MediaClient)
        client.transport = MagicMock()
        client.transport._key = "synthetic-key"
        receipt = {"id": "c" * 64, "sha256": "c" * 64, "bytes": 1000}
        media = {"width": 320, "height": 320, "durationMs": 2000, "fps": 30, "fpsRatio": "30/1",
                 "frame_count": 60, "hasAudio": False, "bytes": 1000, "videoCodec": "h264", "audioCodec": None}
        review = {"status": "passed", "checks": {"frame_count_preserved": True}, "output_media": media,
                  "source_audio_reused": True, "composition_mode": "source_roi", "video_reencoded": True,
                  "pixel_preservation_guaranteed": False, "cue_count": 1}
        result = {"source_id": "a" * 64, "clean_id": "b" * 64, "output": receipt, "technical_review": review}
        response = {"id": "d" * 64, "kind": "render_subtitles", "status": "succeeded", "result": result}
        client.transport._request.return_value = response
        returned = client.submit_job(request())
        self.assertFalse(returned["result"]["technical_review"]["pixel_preservation_guaranteed"])
        result["source_id"] = "e" * 64
        with self.assertRaises(SeedanceError):
            client.submit_job(request())


MEDIA = all(os.environ.get(name) for name in ("VIDEO_LOCALIZER_TEST_FFMPEG", "VIDEO_LOCALIZER_TEST_FFPROBE"))


@unittest.skipUnless(MEDIA, "Set VIDEO_LOCALIZER_TEST_FFMPEG/FFPROBE")
class RenderSyntheticMediaTests(unittest.TestCase):
    def test_roi_render_preserves_original_audio_frame_binding_and_untouched_regions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ffmpeg, ffprobe = (os.environ["VIDEO_LOCALIZER_TEST_" + key] for key in ("FFMPEG", "FFPROBE"))
            for name, color, audio in (("source", "blue", True), ("clean", "red", False)):
                command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                           f"color=c={color}:s=320x320:r=30"]
                if audio:
                    command += ["-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000"]
                command += ["-t", "2", "-c:v", "libx264", "-threads", "2", "-preset", "ultrafast",
                            "-pix_fmt", "yuv420p", "-c:a", "aac", str(root / (name + ".mp4"))]
                subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
            store = Store(root / "objects")
            processor = processing.Processor(store, ffmpeg, ffprobe)
            try:
                source, clean = (store.add(root / (name + ".mp4")) for name in ("source", "clean"))
                payload = request(source, clean)
                job = wait(processor, processor.submit(payload))
                self.assertEqual(job["status"], "succeeded", job)
                review = job["result"]["technical_review"]
                self.assertEqual(review["status"], "passed")
                self.assertEqual(review["output_media"]["frame_count"], 60)
                self.assertTrue(review["checks"]["original_audio_preserved"])
                self.assertEqual(review["composition_mode"], "source_roi")
                output = store.path(job["result"]["output"]["id"])
                def pixel(at, x, y):
                    command = [ffmpeg, "-v", "error", "-i", str(output), "-ss", str(at), "-frames:v", "1",
                               "-vf", f"crop=2:2:{x}:{y},format=rgb24", "-f", "rawvideo", "pipe:1"]
                    return subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          timeout=30).stdout[:3]
                outside = pixel(1, 0, 0)
                inside = pixel(1, 52, 212)
                before = pixel(0, 52, 212)
                self.assertGreater(outside[2], outside[0] + 100)
                self.assertGreater(inside[0], inside[2] + 100)
                self.assertGreater(before[2], before[0] + 100)
            finally:
                processor.close(True)


if __name__ == "__main__":
    unittest.main()
