"""Synthetic-only checks for mechanical cuts and copying cloud video streams."""

import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from video_localizer import seedance_segments as segments


MEDIA = all(os.environ.get(name) for name in
            ("VIDEO_LOCALIZER_TEST_FFMPEG", "VIDEO_LOCALIZER_TEST_FFPROBE"))


@unittest.skipUnless(MEDIA, "Set VIDEO_LOCALIZER_TEST_FFMPEG/FFPROBE for synthetic segment tests")
class SeedanceSegmentMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name).resolve()
        cls.options = {"ffmpeg": os.environ["VIDEO_LOCALIZER_TEST_FFMPEG"],
                       "ffprobe": os.environ["VIDEO_LOCALIZER_TEST_FFPROBE"]}
        cls.source = cls.root / "synthetic-original.mp4"
        segments.engine.command([
            cls.options["ffmpeg"], "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=blue:s=720x1280:r=30",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "20.1", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k", "-shortest", str(cls.source),
        ], timeout=120)
        cls.source_info = dict(segments.engine.probe(cls.source, cls.options, input_limit=False),
                               sha256=segments.engine.digest(cls.source))
        cls.baseline = cls.root / "baseline"
        cls.manifest = segments.prepare_segments(cls.source, cls.baseline, cls.options, cls.source_info)

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="work ' quoted ", dir=self.root))
        shutil.copytree(self.baseline / "seedance-segments", self.work / "seedance-segments")
        manifest = json.loads(json.dumps(self.manifest))
        for part in manifest["parts"]:
            part["path"] = str(self.work / "seedance-segments" / Path(part["path"]).name)
        self.manifest_path = self.work / "seedance-segments" / "manifest.json"
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.current = manifest

    def cloud_candidates(self):
        folder = self.work / "cloud candidates ' quoted"
        folder.mkdir()
        candidates = []
        for part in self.current["parts"]:
            candidate = folder / f"candidate-{part['index']}.mp4"
            segments.engine.command(segments.engine.ffinput(self.options["ffmpeg"], Path(part["path"])) + [
                "-map", "0:v:0", "-c:v", "copy", "-an", str(candidate),
            ], timeout=90)
            candidates.append(candidate)
        return candidates

    def test_exact_frame_ranges_join_copy_and_original_audio_are_preserved(self):
        self.assertEqual(self.current["source_frame_count"], 603)
        self.assertEqual([(part["start_frame"], part["end_frame"]) for part in self.current["parts"]],
                         [(0, 301), (301, 603)])
        self.assertEqual(sum(part["frame_count"] for part in self.current["parts"]), 603)
        self.assertTrue(all(4 <= part["duration_seconds"] <= 15 for part in self.current["parts"]))
        for part in self.current["parts"]:
            self.assertEqual(segments._video(Path(part["path"]), self.options)["frame_count"], part["frame_count"])
            self.assertEqual(segments.engine.digest(Path(part["path"])), part["sha256"])
        candidates = self.cloud_candidates()
        before = [segments.engine.digest(path) for path in candidates]
        output = self.work / "joined candidate.mp4"
        result = segments.join_candidates(self.source, candidates, output, self.options)
        self.assertEqual(result["frame_count"], 603)
        self.assertEqual(result["media"]["fps"], 30)
        self.assertEqual((result["media"]["width"], result["media"]["height"]), (720, 1280))
        self.assertEqual(result["source_sha256"], self.source_info["sha256"])
        self.assertEqual(result["visual_acceptance"], "not_performed")
        self.assertEqual(segments.engine.audio_hash(output, self.options),
                         segments.engine.audio_hash(self.source, self.options))
        self.assertEqual(before, [segments.engine.digest(path) for path in candidates])
        self.assertEqual(segments.engine.digest(self.source), self.source_info["sha256"])

    def test_resume_verifies_parts_without_reencoding(self):
        before = {part["path"]: Path(part["path"]).stat().st_mtime_ns for part in self.current["parts"]}
        with patch.object(segments.engine, "command", wraps=segments.engine.command) as command:
            resumed = segments.prepare_segments(self.source, self.work, self.options, self.source_info)
        self.assertEqual(resumed, self.current)
        self.assertFalse(any("-vf" in call.args[0] for call in command.call_args_list))
        self.assertEqual(before, {part["path"]: Path(part["path"]).stat().st_mtime_ns for part in resumed["parts"]})

    def test_partial_checkpoint_encodes_only_the_missing_planned_part(self):
        first = Path(self.current["parts"][0]["path"])
        original_time = first.stat().st_mtime_ns
        self.current["parts"][1]["sha256"] = None
        Path(self.current["parts"][1]["path"]).unlink()
        self.manifest_path.write_text(json.dumps(self.current), encoding="utf-8")
        with patch.object(segments.engine, "command", wraps=segments.engine.command) as command:
            resumed = segments.prepare_segments(self.source, self.work, self.options, self.source_info)
        self.assertEqual(sum("-vf" in call.args[0] for call in command.call_args_list), 1)
        self.assertEqual(first.stat().st_mtime_ns, original_time)
        self.assertTrue(resumed["parts"][1]["sha256"])
        self.assertEqual(resumed["parts"][1]["frame_count"], 302)

    def test_source_within_fifteen_seconds_uses_one_exact_frame_part(self):
        source = self.work / "short-original.mp4"
        segments.engine.command(segments.engine.ffinput(self.options["ffmpeg"], self.source) +
                               ["-t", "4", "-c", "copy", str(source)], timeout=90)
        info = dict(segments.engine.probe(source, self.options, input_limit=False), sha256=segments.engine.digest(source))
        manifest = segments.prepare_segments(source, self.work / "short-work", self.options, info)
        self.assertEqual(len(manifest["parts"]), 1)
        self.assertEqual((manifest["parts"][0]["start_frame"], manifest["parts"][0]["end_frame"]), (0, 120))
        self.assertEqual(manifest["parts"][0]["duration_seconds"], 4)

    def test_changed_part_and_changed_original_are_rejected(self):
        part = Path(self.current["parts"][0]["path"])
        with part.open("ab") as stream:
            stream.write(b"synthetic tamper")
        with self.assertRaisesRegex(ValueError, "分段文件哈希变化"):
            segments.prepare_segments(self.source, self.work, self.options, self.source_info)
        changed_source = self.work / "changed-source.mp4"
        shutil.copyfile(self.source, changed_source)
        with changed_source.open("ab") as stream:
            stream.write(b"synthetic changed source")
        with self.assertRaisesRegex(ValueError, "原视频哈希已变化"):
            segments.prepare_segments(changed_source, self.work / "other", self.options, self.source_info)

    def test_tampered_manifest_range_or_output_path_is_rejected(self):
        for key, value in (("end_frame", 300), ("path", str(self.work / "escaped.mp4"))):
            with self.subTest(field=key):
                changed = json.loads(json.dumps(self.current))
                changed["parts"][0][key] = value
                self.manifest_path.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "清单路径或帧范围"):
                    segments.prepare_segments(self.source, self.work, self.options, self.source_info)

    def test_mismatched_cloud_fps_rejected_and_raw_candidates_retained(self):
        candidates = self.cloud_candidates()
        changed = candidates[1].parent / "changed-fps.mp4"
        segments.engine.command(segments.engine.ffinput(self.options["ffmpeg"], candidates[1]) + [
            "-vf", "fps=24", "-c:v", "libx264", "-preset", "ultrafast", "-an", str(changed),
        ], timeout=120)
        inputs = [candidates[0], changed]
        before = [segments.engine.digest(path) for path in inputs]
        output = self.work / "mismatch.mp4"
        with self.assertRaisesRegex(ValueError, "不兼容|帧率与原片不同"):
            segments.join_candidates(self.source, inputs, output, self.options)
        self.assertFalse(output.exists())
        self.assertEqual(before, [segments.engine.digest(path) for path in inputs])

    def test_compatible_changed_cloud_fps_and_duration_join_without_retiming(self):
        from video_localizer.seedance_batch import technical_review

        folder = self.work / "simulated cloud 24fps"
        folder.mkdir()
        candidates = []
        for part in self.current["parts"]:
            candidate = folder / f"cloud-{part['index']}.mp4"
            # Simulate remote output with different timing. Joining must keep it.
            segments.engine.command(segments.engine.ffinput(self.options["ffmpeg"], Path(part["path"])) + [
                "-vf", "fps=24", "-t", "10", "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-an", str(candidate),
            ], timeout=90)
            candidates.append(candidate)
        before = [segments.engine.digest(path) for path in candidates]
        output = self.work / "reviewable-24fps.mp4"
        joined = segments.join_candidates(self.source, candidates, output, self.options)
        self.assertEqual(joined["source_frame_count"], 603)
        self.assertEqual(joined["candidate_frame_count"], 480)
        self.assertEqual(joined["media"]["fps"], 24)
        self.assertAlmostEqual(joined["media"]["durationMs"], 20000, delta=1)
        self.assertEqual(joined["visual_acceptance"], "not_performed")
        review = technical_review(self.source, output, self.options, self.source_info)
        self.assertEqual(review["status"], "failed")
        self.assertFalse(review["checks"]["frame_rate_preserved"])
        self.assertFalse(review["checks"]["duration_preserved"])
        self.assertTrue(review["checks"]["original_audio_preserved"])
        self.assertEqual(review["visual_acceptance"], "not_performed")
        self.assertEqual(before, [segments.engine.digest(path) for path in candidates])

    def test_existing_output_and_external_candidate_are_never_overwritten(self):
        output = self.work / "protected.mp4"
        output.write_bytes(b"user-owned existing output")
        with self.assertRaisesRegex(ValueError, "尚不存在"):
            segments.join_candidates(self.source, [Path(self.current["parts"][0]["path"])], output, self.options)
        self.assertEqual(output.read_bytes(), b"user-owned existing output")
        with self.assertRaisesRegex(ValueError, "指定输出目录内"):
            segments.join_candidates(self.source, [self.source], self.work / "new.mp4", self.options)


class SeedanceSegmentPathTests(unittest.TestCase):
    def test_publish_is_atomic_and_does_not_replace_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            temporary, target = root / "temporary.mp4", root / "existing.mp4"
            temporary.write_bytes(b"new bytes")
            target.write_bytes(b"old bytes")
            with self.assertRaises(FileExistsError):
                segments._publish(temporary, target)
            self.assertEqual(target.read_bytes(), b"old bytes")
            self.assertEqual(temporary.read_bytes(), b"new bytes")


if __name__ == "__main__":
    unittest.main()
