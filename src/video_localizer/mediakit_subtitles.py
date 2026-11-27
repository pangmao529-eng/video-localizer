"""Bind MediaKit OCR to human translations and emit deterministic ASS text.

This module does no OCR, translation, video processing or network access.  The
input schema is MediaKit's video-ocr ``result`` object (times are seconds and
Detailed locations are pixels).  Unknown text is left for review, never guessed.
"""

import hashlib
import json
import math
import re
import unicodedata


class SubtitleBindingError(ValueError):
    """Invalid evidence or a subtitle plan that needs review."""


def _text(value, field, *, max_length=20000):
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise SubtitleBindingError(f"{field} must be nonempty bounded text")
    if any(unicodedata.category(c) == "Cc" and c not in "\n\r\t" for c in value):
        raise SubtitleBindingError(f"{field} contains control characters")
    return value


def normalize_source(text):
    """Remove whitespace/punctuation only; do not correct OCR or change words."""
    _text(text, "source text")
    return "".join(c for c in text if not c.isspace() and not unicodedata.category(c).startswith("P"))


def _number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SubtitleBindingError(f"{field} must be a finite number")
    return float(value)


def _dimension(value, field):
    if type(value) is not int or not 1 <= value <= 16384:
        raise SubtitleBindingError(f"{field} must be an integer in 1..16384")
    return value


def _location(value, width, height):
    if value is None:
        return None
    names = ("top_left_x", "top_left_y", "bottom_right_x", "bottom_right_y")
    if not isinstance(value, dict) or any(type(value.get(name)) is not int for name in names):
        raise SubtitleBindingError("text_location must contain integer pixel coordinates")
    x0, y0, x1, y1 = (value[name] for name in names)
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise SubtitleBindingError("text_location is outside the source frame")
    return dict(zip(names, (x0, y0, x1, y1)))


def _union_location(rows):
    boxes = [row["text_location"] for row in rows if row["text_location"] is not None]
    if not boxes:
        return None
    return {"top_left_x": min(box["top_left_x"] for box in boxes),
            "top_left_y": min(box["top_left_y"] for box in boxes),
            "bottom_right_x": max(box["bottom_right_x"] for box in boxes),
            "bottom_right_y": max(box["bottom_right_y"] for box in boxes)}


def _sentence_parts(text):
    """Split only on explicit sentence punctuation, preserving every character."""
    parts = []
    start = 0
    for match in re.finditer(r"(?:[。！？!?]|\.(?=\s|$))\s*", text):
        parts.append(text[start:match.end()])
        start = match.end()
    if start < len(text):
        parts.append(text[start:])
    return parts


def _cue(reference, index, rows, target, source, timing_mode):
    return {"translation_index": index, "source_text": source, "target_text": target,
            "start_time": rows[0]["start_time"], "end_time": rows[-1]["end_time"],
            "ocr_indices": [row["ocr_index"] for row in rows],
            "ocr_texts": [row["subtitle_text"] for row in rows],
            "text_location": _union_location(rows), "timing_mode": timing_mode,
            "label_overrides": [dict(row) for row in rows if "label_override_reason" in row]}


def _same_subtitle_band(candidate, neighbour):
    """Require overlapping horizontal extent and closely matching vertical edges."""
    box, other = candidate["text_location"], neighbour["text_location"]
    if box is None or other is None:
        return False
    height = max(box["bottom_right_y"] - box["top_left_y"],
                 other["bottom_right_y"] - other["top_left_y"])
    return (max(box["top_left_x"], other["top_left_x"]) < min(box["bottom_right_x"], other["bottom_right_x"])
            and abs(box["top_left_y"] - other["top_left_y"]) <= height * .35
            and abs(box["bottom_right_y"] - other["bottom_right_y"]) <= height * .35)


def _reference_clause_overrides(rows, ignored, references, max_gap_seconds):
    """Correct a narrow provider label error using exact, local API evidence.

    An Others item must uniquely equal a complete clause of a multi-sentence
    approved reference, and sit between two Subtitle items in the same band.
    The normal complete-reference matcher still has to accept the full sequence.
    """
    clauses = {}
    for reference_index, reference in enumerate(references):
        parts = _sentence_parts(reference["source"])
        if len(parts) > 1:
            for clause_index, clause in enumerate(parts):
                normalized = normalize_source(clause)
                if normalized:
                    clauses.setdefault(normalized, []).append((reference_index, clause_index))
    overrides, remaining = [], []
    for row in ignored:
        matches = clauses.get(row["normalized"], [])
        eligible = row["text_label"] == "Others" and len(matches) == 1 and row["start_time"] < row["end_time"]
        previous = [item for item in rows if item["end_time"] <= row["start_time"]]
        following = [item for item in rows if item["start_time"] >= row["end_time"]]
        if eligible and previous and following:
            before = max(previous, key=lambda item: item["end_time"])
            after = min(following, key=lambda item: item["start_time"])
            eligible = (row["start_time"] - before["end_time"] <= max_gap_seconds
                        and after["start_time"] - row["end_time"] <= max_gap_seconds
                        and _same_subtitle_band(row, before) and _same_subtitle_band(row, after)
                        and not any(item["start_time"] < row["end_time"] and item["end_time"] > row["start_time"]
                                    for item in rows))
        else:
            eligible = False
        if eligible:
            promoted = {key: value for key, value in row.items() if key != "reason"}
            promoted.update(label_override_reason="exact_reference_clause",
                            reference_index=matches[0][0], reference_clause_index=matches[0][1])
            overrides.append(promoted)
        else:
            remaining.append(row)
    return rows + overrides, remaining, overrides


def _display_cues(reference, index, rows, split_sentences):
    source_parts = _sentence_parts(reference["source"])
    target_parts = _sentence_parts(reference["target"])
    if split_sentences and len(source_parts) == len(target_parts) and len(source_parts) > 1:
        result, position = [], 0
        for source, target in zip(source_parts, target_parts):
            expected = normalize_source(source)
            observed, selected = "", []
            while position < len(rows) and expected.startswith(observed + rows[position]["normalized"]):
                selected.append(rows[position])
                observed += rows[position]["normalized"]
                position += 1
                if observed == expected:
                    break
            if not selected or observed != expected:
                break
            result.append(_cue(reference, index, selected, target, source, "exact_sentence_boundary"))
        else:
            if position == len(rows) and "".join(cue["target_text"] for cue in result) == reference["target"]:
                return result
    return [_cue(reference, index, rows, reference["target"], reference["source"], "whole_reference")]


def bind_subtitles(ocr_result, translations, *, width, height, duration=None,
                   max_gap_seconds=2.0, split_sentences=True):
    """Return cues and an auditable report; no fuzzy text matching is performed.

    ``ocr_result`` may be the provider's result object or a completed response
    containing it.  Translation entries are exactly ``source``/``target``.
    Others labels are ignored except unique exact reference clauses bracketed
    by Subtitle evidence in the same band. Every override is reported. Missing
    labels are accepted for the provider's Subtitle-only OCR mode.
    """
    width, height = _dimension(width, "width"), _dimension(height, "height")
    if not isinstance(ocr_result, dict):
        raise SubtitleBindingError("OCR result must be an object")
    if "result" in ocr_result:
        if ocr_result.get("success") is False or ocr_result.get("status", "completed") != "completed":
            raise SubtitleBindingError("OCR task has not completed successfully")
        ocr_result = ocr_result["result"]
    if not isinstance(ocr_result, dict):
        raise SubtitleBindingError("OCR result must be an object")
    provider_duration = _number(ocr_result.get("duration"), "OCR duration")
    duration = provider_duration if duration is None else _number(duration, "source duration")
    if duration <= 0 or abs(provider_duration - duration) > 0.1:
        raise SubtitleBindingError("OCR duration does not match the source")
    max_gap_seconds = _number(max_gap_seconds, "max_gap_seconds")
    if not 0 <= max_gap_seconds <= 10 or type(split_sentences) is not bool:
        raise SubtitleBindingError("invalid binding options")
    if not isinstance(translations, list) or not translations or len(translations) > 10000:
        raise SubtitleBindingError("translations must be a nonempty bounded list")
    references, seen = [], set()
    for item in translations:
        if not isinstance(item, dict):
            raise SubtitleBindingError("translation must be an object")
        source, target = _text(item.get("source"), "source"), _text(item.get("target"), "target")
        normalized = normalize_source(source)
        if not normalized or normalized in seen:
            raise SubtitleBindingError("translation sources must be nonempty and unambiguous after normalization")
        seen.add(normalized)
        references.append({"source": source, "target": target, "normalized": normalized})
    raw_rows = ocr_result.get("subtitles")
    if not isinstance(raw_rows, list) or len(raw_rows) > 10000:
        raise SubtitleBindingError("OCR subtitles must be a bounded list")
    rows, ignored = [], []
    for index, item in enumerate(raw_rows):
        if not isinstance(item, dict):
            raise SubtitleBindingError("OCR subtitle must be an object")
        text = _text(item.get("subtitle_text"), "subtitle_text")
        start = _number(item.get("start_time"), "start_time")
        end = _number(item.get("end_time"), "end_time")
        label = item.get("text_label", "Subtitle")
        if not 0 <= start <= end <= duration or (start == end and label == "Subtitle"):
            raise SubtitleBindingError("OCR subtitle interval is outside the source")
        row = {"ocr_index": index, "subtitle_text": text, "start_time": start, "end_time": end,
               "text_location": _location(item.get("text_location"), width, height),
               "normalized": normalize_source(text)}
        if label != "Subtitle":
            ignored.append({**row, "reason": "zero_duration_not_subtitle" if start == end else "not_subtitle",
                            "text_label": label})
        else:
            rows.append(row)
    rows, ignored, overrides = _reference_clause_overrides(rows, ignored, references, max_gap_seconds)
    rows.sort(key=lambda row: (row["start_time"], row["end_time"], row["ocr_index"]))
    groups, cues, unmatched, used = [], [], [], set()
    cursor = 0
    while cursor < len(rows):
        normalized, matches = "", []
        for end_index in range(cursor, len(rows)):
            row = rows[end_index]
            if end_index > cursor:
                previous = rows[end_index - 1]
                if row["start_time"] < previous["end_time"] - 0.001:
                    break
                if row["start_time"] - previous["end_time"] > max_gap_seconds:
                    break
            normalized += row["normalized"]
            candidates = [(i, ref) for i, ref in enumerate(references) if ref["normalized"].startswith(normalized)]
            if not candidates:
                break
            matches.extend((end_index, i, ref) for i, ref in candidates if ref["normalized"] == normalized)
        if not matches:
            unmatched.append({**rows[cursor], "reason": "no_complete_exact_reference_match"})
            cursor += 1
            continue
        end_index, reference_index, reference = max(matches, key=lambda item: item[0])
        selected = rows[cursor:end_index + 1]
        next_cues = _display_cues(reference, reference_index, selected, split_sentences)
        if cues and next_cues[0]["start_time"] < cues[-1]["end_time"] - 0.001:
            unmatched.extend({**row, "reason": "overlapping_subtitle_interval"} for row in selected)
        else:
            groups.append({"translation_index": reference_index, "source_text": reference["source"],
                "target_text": reference["target"], "ocr_indices": [row["ocr_index"] for row in selected],
                "start_time": selected[0]["start_time"], "end_time": selected[-1]["end_time"],
                "target_preserved": "".join(cue["target_text"] for cue in next_cues) == reference["target"]})
            cues.extend(next_cues)
            used.add(reference_index)
        cursor = end_index + 1
    fingerprint = hashlib.sha256(json.dumps(translations, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest()
    return {"schema": "mediakit-subtitles-v1", "status": "ready" if cues and not unmatched else "needs_review",
            "width": width, "height": height, "duration": duration,
            "reference_sha256": fingerprint, "reference_count": len(references),
            "groups": groups, "cues": cues, "unmatched": unmatched, "ignored": ignored,
            "label_overrides": overrides,
            "unused_translation_indices": sorted(set(range(len(references))) - used),
            "normalization": "remove_unicode_punctuation_and_whitespace_only",
            "host_generated_translation": False}


def _units(text):
    return sum(2 if unicodedata.east_asian_width(c) in ("F", "W") else 1 for c in text)


def wrap_target(text, max_units):
    """Insert display breaks, retaining characters: ''.join(lines) == text."""
    _text(text, "target")
    if type(max_units) is not int or not 4 <= max_units <= 1000:
        raise SubtitleBindingError("max_units is outside 4..1000")
    if any(c in text for c in "\r\n\t"):
        raise SubtitleBindingError("target must use spaces, not embedded layout controls")
    lines, remaining = [], text
    while _units(remaining) > max_units:
        count, limit = 0, 0
        for index, char in enumerate(remaining):
            count += _units(char)
            if count > max_units:
                break
            limit = index + 1
        whitespace = [match.end() for match in re.finditer(r"\s+", remaining[:limit])]
        boundary = whitespace[-1] if whitespace and whitespace[-1] > 0 else limit
        lines.append(remaining[:boundary])
        remaining = remaining[boundary:]
    if remaining:
        lines.append(remaining)
    return lines


def _ass_time(seconds):
    centiseconds = int(math.floor(seconds * 100 + 0.5))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    whole_seconds, fractional = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{fractional:02d}"


def _ass_text(text):
    # ASS has ambiguous literal escapes across renderers.  Reject reserved text
    # rather than silently rewriting approved translations or injecting tags.
    if any(char in text for char in "\\{}\r\n\t"):
        raise SubtitleBindingError("target contains reserved ASS syntax or layout controls")
    return text


def render_ass(binding, *, font_name="Noto Sans CJK KR", font_size=32, margin_x=48,
               bottom_y=None, outline=2, max_lines=2, wrap_units=None):
    """Render a ready binding as ASS. Font metrics must be checked at rendering.

    Newline placement is mechanical and does not modify the approved characters.
    The line-width estimate is conservative but is not a substitute for glyph
    measurement by the server renderer.  A plan with unmatched OCR is rejected.
    """
    if not isinstance(binding, dict) or binding.get("status") != "ready" or binding.get("unmatched"):
        raise SubtitleBindingError("subtitle binding needs review before rendering")
    width = _dimension(binding.get("width"), "width")
    height = _dimension(binding.get("height"), "height")
    duration = _number(binding.get("duration"), "duration")
    if not isinstance(font_name, str) or not font_name.strip() or len(font_name) > 100 or re.search(r"[,\\{}\r\n]", font_name):
        raise SubtitleBindingError("invalid ASS font name")
    if type(font_size) is not int or not 8 <= font_size <= 150:
        raise SubtitleBindingError("font_size is outside 8..150")
    if type(margin_x) is not int or not 0 <= margin_x < width / 2:
        raise SubtitleBindingError("invalid horizontal margin")
    if type(outline) is not int or not 0 <= outline <= 10 or type(max_lines) is not int or not 1 <= max_lines <= 5:
        raise SubtitleBindingError("invalid subtitle style")
    if bottom_y is not None and (type(bottom_y) is not int or not 0 < bottom_y <= height):
        raise SubtitleBindingError("invalid subtitle anchor")
    available_units = max(4, int((width - 2 * margin_x - 2 * outline) * 2 / font_size))
    if wrap_units is None:
        wrap_units = available_units
    if type(wrap_units) is not int or not 4 <= wrap_units <= available_units:
        raise SubtitleBindingError("wrap_units exceeds the available frame width")
    header = ("[Script Info]\nScriptType: v4.00+\n"
              f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n"
              "\n[V4+ Styles]\n"
              "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
              "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
              "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
              f"Style: Korean,{font_name},{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
              f"0,0,0,0,100,100,0,0,1,{outline},0,2,{margin_x},{margin_x},20,1\n"
              "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    events, previous_end = [], 0.0
    cues = binding.get("cues")
    if not isinstance(cues, list) or not cues:
        raise SubtitleBindingError("binding contains no cues")
    for cue in cues:
        start, end = _number(cue.get("start_time"), "cue start"), _number(cue.get("end_time"), "cue end")
        if not 0 <= start < end <= duration or start < previous_end - 0.001 or _ass_time(start) == _ass_time(end):
            raise SubtitleBindingError("invalid or overlapping render interval")
        previous_end = end
        target = _ass_text(_text(cue.get("target_text"), "target_text"))
        lines = wrap_target(target, wrap_units)
        if len(lines) > max_lines or "".join(lines) != target:
            raise SubtitleBindingError("target exceeds the configured subtitle layout")
        box = _location(cue.get("text_location"), width, height)
        y = bottom_y if bottom_y is not None else (box["bottom_right_y"] if box else height - 36)
        if y - math.ceil(font_size * 1.3 * len(lines)) - outline < 0 or y + outline > height:
            raise SubtitleBindingError("subtitle layout is outside the source frame")
        display = r"\N".join(lines)
        position = "{\\an2\\pos(" + str(width // 2) + "," + str(y) + ")\\q2}"
        events.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Korean,,0,0,0,,{position}{display}\n")
    return header + "".join(events)
