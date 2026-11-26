"""Optional real-FFmpeg test using a generated, non-user video fixture."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from video_localizer.executor import VideoExecutor


@unittest.skipUnless(all(os.environ.get(k) for k in ("VIDEO_LOCALIZER_TEST_FFMPEG", "VIDEO_LOCALIZER_TEST_FFPROBE", "VIDEO_LOCALIZER_TEST_FONT")),
                     "Set VIDEO_LOCALIZER_TEST_FFMPEG/FFPROBE/FONT to run the media integration test")
class ExecutorMediaTests(unittest.TestCase):
    def test_real_preview_final_audio_and_frame_feedback(self):
        from PIL import Image, ImageDraw, ImageFont
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ffmpeg = os.environ["VIDEO_LOCALIZER_TEST_FFMPEG"]
            options = {"ffmpeg": ffmpeg, "ffprobe": os.environ["VIDEO_LOCALIZER_TEST_FFPROBE"],
                       "font": os.environ["VIDEO_LOCALIZER_TEST_FONT"], "profile": "2k120"}
            image = Image.new("RGB", (320, 180), (35, 60, 100))
            ImageDraw.Draw(image).text((70, 65), "PLAY", fill="white", font=ImageFont.truetype(options["font"], 30))
            ImageDraw.Draw(image).text((220, 65), "GO", fill="white", font=ImageFont.truetype(options["font"], 24))
            ImageDraw.Draw(image).rectangle((65,65,69,69),fill=(255,0,0))
            frame = root / "test.png"
            image.save(frame)
            source = root / "source-input.mp4"
            subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-loop", "1", "-framerate", "30", "-i", str(frame),
                            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "2",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-y", str(source)], check=True)
            executor = VideoExecutor(source, root / "task", options)
            self.assertTrue(executor.call("inspect_video", {})["result"]["ok"])
            source_frames = executor.state["source_frames"]
            self.assertEqual(source_frames, 60)
            original = executor.call("inspect_frames", {"artifact": "source", "frames": [0, 29]})
            self.assertEqual(len(original["images"]), 2)
            plan = executor.call("set_edit_plan", {"expected_revision": 0, "summary": "Synthetic fixture translation",
                "segments": [{"id": "play", "source_text": "PLAY", "target_text": "시작", "kind": "ui",
                              "portrait_match": "not_applicable", "start_frame": 0, "end_frame": 60,
                              "keyframes": [{"frame": 0, "box": [60, 60, 130, 50]}], "motion": "fixed",
                              "mask": {"mode": "light", "dilate_px": 1},
                              "style": {"font": "sans", "font_size": 30, "color": "#ffffff", "outline_color": "#000000",
                                        "outline_width": 0, "align": "center"}},
                             {"id": "go", "source_text": "GO", "target_text": "가자", "kind": "ui",
                              "portrait_match": "not_applicable", "start_frame": 0, "end_frame": 60,
                              "keyframes": [{"frame": 0, "box": [210,60,85,50]}], "motion": "fixed",
                              "mask": {"mode": "light", "dilate_px": 1},
                              "style": {"font": "sans", "font_size": 24, "color": "#ffffff", "outline_color": "#000000",
                                        "outline_width": 0, "align": "center"}}]})
            self.assertTrue(plan["result"]["ok"], plan)
            program=executor.call("set_region_program",{"expected_revision":1,"segment_id":"play",
                "code":"background=solid([35,60,100])\nforeground_mask=color_mask(original,[255,0,0],80)\ntext_alpha=1"})
            self.assertTrue(program["result"]["ok"],program)
            preview = executor.call("render_preview", {"expected_revision": 2, "start_frame": 15, "end_frame": 45})
            self.assertTrue(preview["result"]["ok"], preview)
            shown_preview = executor.call("inspect_frames", {"artifact": "preview", "frames": [15, 44]})
            self.assertEqual(len(shown_preview["images"]), 2, shown_preview)
            import numpy as np
            preview_pixels=np.asarray(Image.open(shown_preview["images"][0]["path"]).convert("RGB"))
            foreground=preview_pixels[65:70,65:70].mean(axis=(0,1))
            self.assertGreater(foreground[0],foreground[1]+50)
            self.assertGreater(foreground[0],foreground[2]+30)
            final = executor.call("render_final", {"expected_revision": 2})
            self.assertTrue(final["result"]["ok"], final)
            self.assertTrue(executor.state["audio_preserved"])
            self.assertEqual(executor.state["output_media"]["fps"], 120)
            shown_final = executor.call("inspect_frames", {"artifact": "final", "frames": [0, 29, 59]})
            self.assertEqual(len(shown_final["images"]), 3, shown_final)
            self.assertEqual([row["output_frame"] for row in shown_final["images"]], [0, 116, 236])
            self.assertEqual(shown_final["result"]["metadata"]["inspected_frames"], [0, 29, 59])
            self.assertFalse(executor.state["automatic_quality_pass"])
            reopened = VideoExecutor(source, executor.root, options)
            self.assertEqual(reopened.state["final_output"], executor.state["final_output"])


if __name__ == "__main__":
    unittest.main()
