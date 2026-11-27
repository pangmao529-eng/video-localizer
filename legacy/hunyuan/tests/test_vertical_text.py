"""Synthetic renderer/protocol tests only; no user media or model requests."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import unicodedata
from unittest.mock import patch

from video_localizer import _engine as engine
from video_localizer.executor import STYLE, VideoExecutor


FONT = os.environ.get("VIDEO_LOCALIZER_TEST_FONT")
MEDIA = all(os.environ.get(name) for name in
            ("VIDEO_LOCALIZER_TEST_FONT", "VIDEO_LOCALIZER_TEST_FFMPEG", "VIDEO_LOCALIZER_TEST_FFPROBE"))


def style(mode="vertical_ttb", size=32, stroke=0, align="center"):
    return {"font": "sans", "fontSize": size, "color": "#ffffff", "outlineColor": "#ff0000",
            "outlineWidth": stroke, "align": align, "writingMode": mode}


def layer(text, *, box=(0, 0, 80, 180), text_style=None, layout_only=False):
    return engine.text_layer({"translation": text, "style": text_style or style()}, box,
                             {"fonts": {"sans": FONT, "serif": FONT}}, layout_only=layout_only)


def independent_glyph(char, size=32):
    """Glyph shape from Pillow itself, independent of the column placement code."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    scale = 2
    image = Image.new("L", (size*3*scale, size*3*scale))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(FONT, size*scale)
    bound = draw.textbbox((0, 0), char, font=font)
    draw.text((size*scale-bound[0], size*scale-bound[1]), char, font=font, fill=255)
    image = image.resize((size*3, size*3), Image.Resampling.LANCZOS)
    image = image.crop(image.getbbox())
    return np.asarray(image).astype("float32")


def glyph_locations(alpha, text, size=32, binary=False):
    import cv2
    import numpy as np
    source = np.asarray(alpha).astype("float32")
    if binary:
        source = (source > 127).astype("float32")
    result = []
    for char in text:
        template = independent_glyph(char, size)
        if binary:
            template = (template > 127).astype("float32")
        scores = cv2.matchTemplate(source, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, position = cv2.minMaxLoc(scores)
        result.append((score, position))
    return result


@unittest.skipUnless(FONT and Path(FONT).is_file(), "Set VIDEO_LOCALIZER_TEST_FONT")
class VerticalTextLayerTests(unittest.TestCase):
    def test_upright_korean_glyph_order_and_single_column(self):
        import numpy as np
        image = layer("가나다")
        matches = glyph_locations(image.getchannel("A"), "가나다")
        for score, _ in matches:
            self.assertGreater(score, .90)
        positions = [position for _, position in matches]
        self.assertLess(max(p[0] for p in positions)-min(p[0] for p in positions), 4)
        self.assertGreater(positions[1][1]-positions[0][1], 25)
        self.assertGreater(positions[2][1]-positions[1][1], 25)
        alpha = np.asarray(image.getchannel("A"))
        ys, xs = np.where(alpha > 20)
        self.assertGreater(ys.max()-ys.min(), 2.5*(xs.max()-xs.min()))

    def test_space_retains_one_empty_cell(self):
        tight = [p for _, p in glyph_locations(layer("가나").getchannel("A"), "가나")]
        spaced = [p for _, p in glyph_locations(layer("가 나").getchannel("A"), "가나")]
        self.assertAlmostEqual(spaced[1][1]-spaced[0][1], 2*(tight[1][1]-tight[0][1]), delta=1)

    def test_nfc_nfd_and_composable_accent_render_identically(self):
        for text in ("각나다", "éA"):
            with self.subTest(text=text):
                self.assertEqual(layer(text).tobytes(), layer(unicodedata.normalize("NFD", text)).tobytes())
        self.assertEqual(layer("각").tobytes(), layer("각").tobytes())

    def test_outline_and_alignment_stay_inside_the_box(self):
        import numpy as np
        plain = np.asarray(layer("가나", text_style=style(stroke=0)))
        outlined = np.asarray(layer("가나", text_style=style(stroke=2)))
        self.assertGreater((outlined[:, :, 3] > 127).sum(), (plain[:, :, 3] > 127).sum()*1.4)
        self.assertGreater(((outlined[:, :, 0] > 200) & (outlined[:, :, 1] < 40) & (outlined[:, :, 3] > 127)).sum(), 50)
        centers = []
        for align in ("left", "center", "right"):
            image = layer("가나", text_style=style(stroke=2, align=align))
            alpha = np.asarray(image.getchannel("A"))
            ys, xs = np.where(alpha > 20)
            self.assertGreaterEqual(xs.min(), 1)
            self.assertLessEqual(xs.max(), image.width-2)
            self.assertGreaterEqual(ys.min(), 1)
            self.assertLessEqual(ys.max(), image.height-2)
            centers.append((xs.min()+xs.max())/2)
        self.assertGreater(centers[1]-centers[0], 10)
        self.assertGreater(centers[2]-centers[1], 10)

    def test_preflight_and_actual_drawing_agree_on_fit_and_overflow(self):
        self.assertIsNone(layer("가나다", layout_only=True))
        self.assertIsNotNone(layer("가나다"))
        for preflight in (True, False):
            with self.assertRaisesRegex(engine.BridgeError, "video_localization_text_overflow"):
                layer("가"*40, box=(0, 0, 15, 30), layout_only=preflight)

    def test_complex_or_out_of_scope_sequences_are_rejected_before_drawing(self):
        unsupported = ("가\u0301", "👩\u200d💻", "한\ufe0f", "👍🏽", "🇰🇷", "1\ufe0f\u20e3",
                       "ᄀ", "ᆨ", "가\n나", "가\u2028나", "가\u2029나", "가\u00a0나", "가\u0085나",
                       "\ud800", "ｶﾞ", "ﾊﾟ", "กำ", "ກຳ", "ൎക", "𑇂𑆃")
        for text in unsupported:
            for preflight in (True, False):
                with self.subTest(text=ascii(text), preflight=preflight), patch("PIL.ImageDraw.ImageDraw.text") as draw:
                    with self.assertRaisesRegex(engine.BridgeError, "vertical_text_unsupported"):
                        layer(text, layout_only=preflight)
                    draw.assert_not_called()

    def test_horizontal_pixels_match_the_frozen_engine(self):
        from PIL import ImageFont, __version__ as pillow_version
        golden = json.loads((Path(__file__).resolve().parent/"fixtures/horizontal_rc7_golden.json").read_text(encoding="utf-8"))
        if (hashlib.sha256(Path(FONT).read_bytes()).hexdigest() != golden["font_sha256"]
                or pillow_version != golden["pillow_version"]
                or int(ImageFont.truetype(FONT, 64).layout_engine) != golden["layout_engine"]):
            self.skipTest("Frozen horizontal pixels require the pinned font/Pillow/layout engine")
        config = {"fonts": {"sans": FONT, "serif": FONT}}
        for sample in golden["samples"]:
            for mode in (None, "horizontal"):
                current = dict(golden["style"])
                if mode is not None:
                    current["writingMode"] = mode
                image = engine.text_layer({"translation": sample["text"], "style": current}, golden["box"], config)
                self.assertEqual(hashlib.sha256(image.tobytes()).hexdigest(), sample["rgba_sha256"])


class VerticalProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root/"input.mp4"
        self.source.write_bytes(b"synthetic protocol fixture")
        self.font = self.root/"font.ttf"
        self.font.write_bytes(b"protocol only")
        self.options = {"font": str(self.font)}
        self.executor = VideoExecutor(self.source, self.root/"task", self.options)
        self.executor.manifest = {"media": {"width": 320, "height": 240, "durationMs": 200, "fps": 10},
            "frames": [{"frameId": f"f{i:06}", "index": i, "ptsMs": i*100, "durationMs": 100} for i in range(2)]}
        self.executor.state["source_sha256"] = engine.digest(self.source)
        self.target = unicodedata.normalize("NFD", "각나다")
        self.segment = {"id": "synthetic", "source_text": "ABC", "target_text": self.target, "kind": "ui",
            "portrait_match": "not_applicable", "start_frame": 0, "end_frame": 2,
            "keyframes": [{"frame": 0, "box": [100, 10, 60, 220]}], "motion": "fixed",
            "mask": {"mode": "light", "dilate_px": 1},
            "style": {"font": "sans", "font_size": 30, "color": "#ffffff", "outline_color": "#000000",
                      "outline_width": 1, "align": "center", "writing_mode": "vertical_ttb"}}

    def tearDown(self):
        self.temp.cleanup()

    def submit(self, segment):
        return self.executor.call("set_edit_plan", {"expected_revision": 0, "summary": "Synthetic protocol",
                                                   "segments": [segment]})

    def test_optional_schema_and_exact_text_persistence(self):
        self.assertNotIn("writing_mode", STYLE["required"])
        self.assertFalse(STYLE["additionalProperties"])
        result = self.submit(self.segment)
        self.assertTrue(result["result"]["ok"], result)
        compiled = json.loads((self.executor.root/self.executor.state["compiled_plan_file"]).read_text(encoding="utf-8"))
        self.assertEqual(compiled["segments"][0]["style"]["writingMode"], "vertical_ttb")
        self.assertEqual(compiled["segments"][0]["translation"], self.target)
        reopened = VideoExecutor(self.source, self.executor.root, self.options)
        self.assertEqual(reopened.state["plan"]["segments"][0]["target_text"], self.target)
        self.assertEqual(reopened.state["plan"]["segments"][0]["style"]["writing_mode"], "vertical_ttb")

    def test_old_style_compiles_without_new_fields(self):
        del self.segment["style"]["writing_mode"]
        result = self.submit(self.segment)
        self.assertTrue(result["result"]["ok"], result)
        compiled = json.loads((self.executor.root/self.executor.state["compiled_plan_file"]).read_text(encoding="utf-8"))
        self.assertNotIn("writingMode", compiled["segments"][0]["style"])

    def test_invalid_modes_extra_fields_and_sequences_do_not_save_a_plan(self):
        cases = []
        for value in (None, True, "vertical_rl", [], "sideways"):
            row = copy.deepcopy(self.segment)
            row["style"]["writing_mode"] = value
            cases.append(row)
        row = copy.deepcopy(self.segment)
        row["style"]["column_count"] = 2
        cases.append(row)
        row = copy.deepcopy(self.segment)
        row["target_text"] = "가\n나"
        cases.append(row)
        for row in cases:
            with self.subTest(style=row["style"]):
                self.assertFalse(self.submit(row)["result"]["ok"])
                self.assertEqual(self.executor.state["plan_revision"], 0)


@unittest.skipUnless(MEDIA, "Set VIDEO_LOCALIZER_TEST_FONT/FFMPEG/FFPROBE")
class VerticalSyntheticMediaTests(unittest.TestCase):
    def test_preview_and_final_have_upright_ordered_glyphs_and_unchanged_target(self):
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            options = {"ffmpeg": os.environ["VIDEO_LOCALIZER_TEST_FFMPEG"],
                       "ffprobe": os.environ["VIDEO_LOCALIZER_TEST_FFPROBE"], "font": FONT, "profile": "source"}
            source_image = Image.new("RGB", (320, 240), (30, 50, 90))
            draw = ImageDraw.Draw(source_image)
            for y, char in zip((45, 100, 155), "ABC"):
                draw.text((126, y), char, fill="white", font=ImageFont.truetype(FONT, 32))
            frame = root/"synthetic.png"
            source_image.save(frame)
            source = root/"source.mp4"
            subprocess.run([options["ffmpeg"], "-v", "error", "-nostdin", "-loop", "1", "-framerate", "30",
                "-i", str(frame), "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-y", str(source)], check=True)
            executor = VideoExecutor(source, root/"task", options)
            self.assertTrue(executor.call("inspect_video", {})["result"]["ok"])
            target = unicodedata.normalize("NFD", "가나다")
            plan = {"id": "upright", "source_text": "ABC", "target_text": target, "kind": "ui",
                "portrait_match": "not_applicable", "start_frame": 0, "end_frame": 30,
                "keyframes": [{"frame": 0, "box": [110, 8, 80, 222]}], "motion": "fixed",
                "mask": {"mode": "light", "dilate_px": 1},
                "style": {"font": "sans", "font_size": 32, "color": "#ffffff", "outline_color": "#000000",
                          "outline_width": 0, "align": "center", "writing_mode": "vertical_ttb"}}
            self.assertTrue(executor.call("set_edit_plan", {"expected_revision": 0, "summary": "Synthetic upright lettering", "segments": [plan]})["result"]["ok"])
            self.assertTrue(executor.call("set_region_program", {"expected_revision": 1, "segment_id": "upright", "code": "background=solid([30,50,90])"})["result"]["ok"])
            for kind in ("preview", "final"):
                arguments = {"expected_revision": 2}
                if kind == "preview":
                    arguments.update(start_frame=0, end_frame=30)
                result = executor.call("render_"+kind, arguments)
                self.assertTrue(result["result"]["ok"], result)
                shown = executor.call("inspect_frames", {"artifact": kind, "frames": [0, 15, 29]})
                self.assertEqual(len(shown["images"]), 3, shown)
                for rendered in shown["images"]:
                    pixels = np.asarray(Image.open(rendered["path"]).convert("RGB"))
                    # Recover alpha from the fixture's known red background
                    # channel. Thresholding compressed video and the unencoded
                    # font at different opacities distorts the expected strokes.
                    alpha = np.clip((pixels[8:230, 110:190, 0].astype("float32")-30)*(255/225), 0, 255)
                    matches = glyph_locations(alpha, "가나다")
                    for score, _ in matches:
                        self.assertGreater(score, .90, (kind, matches))
                    positions = [p for _, p in matches]
                    self.assertGreater(positions[1][1]-positions[0][1], 25)
                    self.assertGreater(positions[2][1]-positions[1][1], 25)
                    self.assertLess(max(p[0] for p in positions)-min(p[0] for p in positions), 4)
            self.assertTrue(executor.state["audio_preserved"])
            self.assertFalse(executor.state["automatic_quality_pass"])
            self.assertEqual(executor.state["plan"]["segments"][0]["target_text"], target)


if __name__ == "__main__":
    unittest.main()
