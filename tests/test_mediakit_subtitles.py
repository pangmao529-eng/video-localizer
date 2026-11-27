import unittest

from video_localizer.mediakit_subtitles import (
    SubtitleBindingError, bind_subtitles, normalize_source, render_ass, wrap_target,
)


REFERENCES = [
    {"source": "师姐，你的战力怎么提升这么快？", "target": "사저, 전투력 왜 이리 빨리 올라?"},
    {"source": "你以为要练很多种心法？错了。资源只给一个，先拉满。",
     "target": "심법을 여러 개 배워야 한다고? 틀렸어. 자원을 하나에만 몰아 먼저 끝까지 키워."},
    {"source": "只拉满一个？", "target": "하나만 끝까지?"},
]


def row(text, start, end, label="Subtitle", **changes):
    return {"subtitle_text": text, "start_time": start, "end_time": end, "text_label": label,
            "text_location": {"top_left_x": 430, "top_left_y": 630, "bottom_right_x": 850, "bottom_right_y": 660},
            **changes}


def actual_row(text, start, end, label, coordinates):
    names = ("top_left_x", "top_left_y", "bottom_right_x", "bottom_right_y")
    return row(text, start, end, label, text_location=dict(zip(names, coordinates)))


# Exact OCR evidence from the real first-ten-second test; task identifiers omitted.
CLIP9_OCR = [
    actual_row("AI生成", 0, 3.233, "Others", (1024, 623, 1271, 708)),
    actual_row("师姐你的战力怎么提升这么快", .766, 2.933, "Subtitle", (443, 615, 822, 658)),
    actual_row("生成", 3.266, 7.066, "Others", (1030, 626, 1263, 715)),
    actual_row("你以为要练很多种心法", 3.766, 5.5, "Subtitle", (499, 619, 782, 658)),
    actual_row("生成", 6.3, 6.3, "Others", (1118, 626, 1263, 703)),
    actual_row("生成", 6.5, 6.633, "Others", (1110, 626, 1263, 703)),
    actual_row("错了", 6.666, 7.233, "Others", (603, 619, 670, 658)),
    actual_row("生成", 7.033, 7.333, "Others", (1106, 626, 1263, 703)),
    actual_row("A生成", 7.366, 9.966, "Others", (964, 619, 1265, 713)),
    actual_row("资源只给一个先拉满", 7.666, 9.766, "Subtitle", (511, 615, 766, 658)),
]


class MediaKitSubtitleTests(unittest.TestCase):
    def bind(self, rows, translations=REFERENCES, **options):
        return bind_subtitles({"duration": 10, "subtitles": rows}, translations,
                              width=1280, height=720, **options)

    def test_dialogue_parts_bind_without_inserting_absent_translations(self):
        result = self.bind([
            row("AI生成", 0, 10, "Others"),
            row("师姐 你的战力怎么提升这么快", .8, 3.2),
            row("你以为要练很多种心法", 3.8, 5.6),
            row("错了", 6.8, 7.3),
            row("资源只给一个先拉满", 7.8, 9.7),
        ])
        self.assertEqual(result["status"], "ready")
        self.assertEqual([cue["start_time"] for cue in result["cues"]], [.8, 3.8, 6.8, 7.8])
        self.assertEqual("".join(cue["target_text"] for cue in result["cues"][1:]), REFERENCES[1]["target"])
        self.assertEqual(result["unused_translation_indices"], [2])
        self.assertEqual(result["ignored"][0]["text_label"], "Others")
        script = render_ass(result)
        self.assertEqual(script.count("Dialogue:"), 4)
        self.assertNotIn("하나만 끝까지?", script)
        self.assertNotIn("AI生成", script)
        self.assertIn("0:00:06.80,0:00:07.30", script)

    def test_real_clip9_recovers_exact_mislabeled_clause_and_preserves_watermark(self):
        result = self.bind(CLIP9_OCR)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(result["groups"]), 2)
        self.assertEqual(len(result["cues"]), 4)
        self.assertEqual(result["unmatched"], [])
        self.assertEqual(len(result["ignored"]), 6)
        self.assertEqual(result["ignored"][2]["reason"], "zero_duration_not_subtitle")
        override = result["label_overrides"][0]
        self.assertEqual(override["subtitle_text"], "错了")
        self.assertEqual(override["text_label"], "Others")
        self.assertEqual(override["label_override_reason"], "exact_reference_clause")
        self.assertEqual((result["cues"][2]["start_time"], result["cues"][2]["end_time"]), (6.666, 7.233))
        self.assertEqual(result["cues"][2]["target_text"], "틀렸어. ")
        self.assertEqual("".join(cue["target_text"] for cue in result["cues"][1:]), REFERENCES[1]["target"])
        self.assertEqual(result["unused_translation_indices"], [2])
        self.assertEqual(render_ass(result).count("Dialogue:"), 4)

    def test_others_override_requires_unique_clause_and_both_neighbouring_locations(self):
        scenarios = [
            ([*REFERENCES, {"source": "错了。其他。", "target": "다른 문장."}], CLIP9_OCR),
            (REFERENCES, [dict(item, text_location={"top_left_x": 10, "top_left_y": 10,
                "bottom_right_x": 100, "bottom_right_y": 50}) if i == 6 else item for i, item in enumerate(CLIP9_OCR)]),
            (REFERENCES, [dict(item, text_location=None) if i == 6 else item for i, item in enumerate(CLIP9_OCR)]),
            (REFERENCES, [dict(item, start_time=6.666, end_time=6.666) if i == 6 else item
                          for i, item in enumerate(CLIP9_OCR)]),
        ]
        for refs, rows in scenarios:
            with self.subTest(refs=refs, rows=rows):
                result = self.bind(rows, refs)
                self.assertEqual(result["label_overrides"], [])
                self.assertEqual(result["status"], "needs_review")
                self.assertEqual(len(result["groups"]), 1)

    def test_ocr_typo_is_not_silently_corrected(self):
        result = self.bind([row("师姐你的战力怎么提开这么快", .8, 3.2)])
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(len(result["unmatched"]), 1)
        with self.assertRaises(SubtitleBindingError):
            render_ass(result)

    def test_partial_reference_does_not_display_unseen_later_dialogue(self):
        result = self.bind([row("你以为要练很多种心法", 3.8, 5.6), row("错了", 6.8, 7.3)])
        self.assertFalse(result["cues"])
        self.assertEqual(len(result["unmatched"]), 2)

    def test_large_gap_cannot_join_two_unrelated_subtitles(self):
        refs = [{"source": "甲乙", "target": "가나"}]
        result = self.bind([row("甲", 0, 1), row("乙", 8, 9)], refs)
        self.assertEqual(result["status"], "needs_review")

    def test_whole_reference_fallback_preserves_target_when_sentence_counts_differ(self):
        refs = [{"source": "甲？乙。", "target": "가 나."}]
        result = self.bind([row("甲", 1, 2), row("乙", 2.5, 3)], refs)
        self.assertEqual(result["cues"][0]["target_text"], "가 나.")
        self.assertEqual(result["cues"][0]["timing_mode"], "whole_reference")

    def test_ambiguous_source_reference_and_invalid_numeric_evidence_fail(self):
        for refs in [[{"source": "甲", "target": "가"}, {"source": "甲。", "target": "나"}]]:
            with self.assertRaises(SubtitleBindingError):
                self.bind([], refs)
        for changed in [dict(start_time=True), dict(end_time=float("nan")), dict(end_time=11),
                        dict(text_location={"top_left_x": -1, "top_left_y": 1, "bottom_right_x": 3, "bottom_right_y": 5})]:
            with self.subTest(changed=changed), self.assertRaises(SubtitleBindingError):
                self.bind([row("只拉满一个", 1, 2, **changed)])

    def test_ocr_envelope_must_be_completed(self):
        for state in ["running", "failed"]:
            with self.subTest(state=state), self.assertRaises(SubtitleBindingError):
                bind_subtitles({"status": state, "result": {"duration": 10, "subtitles": []}},
                               REFERENCES, width=1280, height=720)

    def test_overlapping_independent_subtitles_are_not_rendered_together(self):
        result = self.bind([row("师姐你的战力怎么提升这么快", 1, 3), row("只拉满一个", 2, 4)])
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["unmatched"][0]["reason"], "overlapping_subtitle_interval")

    def test_reserved_ass_syntax_is_rejected_instead_of_rewriting_target(self):
        for text in [r"{\pos(1,2)}가", r"가\N나", "가\n나"]:
            result = self.bind([row("甲", 1, 2)], [{"source": "甲", "target": text}])
            with self.subTest(text=text), self.assertRaises(SubtitleBindingError):
                render_ass(result)

    def test_wrapping_does_not_change_approved_characters(self):
        text = REFERENCES[1]["target"]
        lines = wrap_target(text, 38)
        self.assertGreater(len(lines), 1)
        self.assertEqual("".join(lines), text)
        result = self.bind([row("甲", 1, 2)], [{"source": "甲", "target": text}])
        with self.assertRaises(SubtitleBindingError):
            render_ass(result, wrap_units=10, max_lines=1)

    def test_normalization_keeps_meaningful_characters(self):
        self.assertEqual(normalize_source("师姐，你好？ 123"), "师姐你好123")
        self.assertNotEqual(normalize_source("攻击+10"), normalize_source("攻击10"))


if __name__ == "__main__":
    unittest.main()
