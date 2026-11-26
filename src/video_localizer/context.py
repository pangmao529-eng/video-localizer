"""Rebuild a bounded working context without editing the audit conversation.

Only the model supplies semantic notes. This module copies recorded facts and
human decisions; it does not infer translations, coverage or visual approval.
"""
from __future__ import annotations

import copy
import json


def _complete_rounds(messages):
    """Return assistant rounds, or None when safe protocol slicing is impossible."""
    starts = [i for i, message in enumerate(messages) if message.get("role") == "assistant"]
    if any(message.get("role") == "tool" for message in messages[:starts[0] if starts else len(messages)]):
        return None
    rounds = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(messages)
        block = messages[start:end]
        calls = block[0].get("tool_calls") or []
        if not isinstance(calls, list):
            return None
        ids = [call.get("id") for call in calls if isinstance(call, dict)]
        if len(ids) != len(calls) or any(not isinstance(value, str) or not value for value in ids):
            return None
        if len(set(ids)) != len(ids):
            return None
        answered = [message.get("tool_call_id") for message in block[1:] if message.get("role") == "tool"]
        if len(answered) != len(ids) or set(answered) != set(ids):
            return None
        rounds.append(block)
    return rounds


def _original_request(state, messages):
    original = state.get("original_request")
    if isinstance(original, str):
        return {"role": "user", "content": original}
    if isinstance(original, dict):
        if original.get("role") == "user" and "content" in original:
            return copy.deepcopy(original)
        return {"role": "user", "content": json.dumps(original, ensure_ascii=False, separators=(",", ":"))}
    for message in messages:
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return copy.deepcopy(message)
    return None


def _focus_bounds(notes):
    focus = notes.get("focus")
    if not isinstance(focus, dict):
        return None
    start, end = focus.get("start_frame"), focus.get("end_frame")
    if type(start) is int and type(end) is int and 0 <= start < end:
        return start, end
    return None


def _frame_count(value):
    if not isinstance(value, list):
        return 0
    return len({frame for frame in value if type(frame) is int and frame >= 0})


def _authority(state, snapshot):
    notes = state.get("work_notes") or {}
    if not isinstance(notes, dict):
        notes = {}
    plan = snapshot.get("plan") or {}
    segments = plan.get("segments", [])
    focus = _focus_bounds(notes)
    focused = []
    if focus:
        for segment in segments:
            start, end = segment.get("start_frame"), segment.get("end_frame")
            if type(start) is int and type(end) is int and start < focus[1] and end > focus[0]:
                focused.append(segment)
    programs = snapshot.get("programs") or {}
    ledger = state.get("task_ledger") or {}
    open_tasks, done_tasks = {}, []
    for task_id, task in ledger.items():
        if not isinstance(task, dict) or task.get("status") != "done":
            open_tasks[task_id] = task
        else:
            done_tasks.append({"id": task_id, "description": task.get("description", ""),
                               "segment_ids": task.get("segment_ids", []),
                               "evidence_frame_count": _frame_count(task.get("evidence_frames"))})
    result = {
        "type": "authoritative_work_state", "schema_version": 1,
        "context_policy": {
            "source": "Persisted executor and controller records; the full audit history is unchanged.",
            "semantic_notes_available": bool(notes),
            "notes_authority": "work_notes and task_ledger are model-authored memory, not human approval or proof of visual quality.",
            "unresolved_reviews": "Only recorded human decisions resolve review issues. Notes cannot override them.",
            "retrieval": "Use get_work_state to retrieve full plans, region programs and detailed records omitted from this view.",
            "coverage": "Counts describe recorded tool activity only; they do not prove all text or all frames were inspected.",
        },
        "source": snapshot.get("source"), "profile": snapshot.get("profile"),
        "media": snapshot.get("media"), "source_frames": snapshot.get("source_frames"),
        "plan_revision": snapshot.get("plan_revision", 0),
        "plan_summary": plan.get("summary", ""),
        "plan_index": [{key: segment.get(key) for key in
                        ("id", "source_text", "target_text", "start_frame", "end_frame")} for segment in segments],
        "focus_segments": focused[:4],
        "focus_segment_count": len(focused),
        "focus_programs": {segment["id"]: programs[segment["id"]] for segment in focused[:4]
                           if segment.get("id") in programs},
        "program_segment_ids": list(programs),
        "plan_issues": snapshot.get("plan_issues", []),
        "outputs": {key: snapshot.get(key) for key in
                    ("preview_output", "final_output", "output_media", "audio_preserved", "changed_frames")},
        "reviews": state.get("reviews", []),
        "decisions": state.get("decisions", []),
        "task_ledger": {"open": open_tasks, "done": done_tasks},
        "work_notes": notes,
        "phase": state.get("phase", "bootstrap" if not notes else "unknown"),
        "recorded_feedback": {
            "source_frames_extracted_count": _frame_count(state.get("source_frames_seen")),
            "source_frames_delivered_count": _frame_count(state.get("source_frames_delivered")),
            "final_full_frames_reviewed_count": _frame_count(state.get("final_frames_seen")),
            **{key: state.get(key) for key in ("inspected_preview_revision", "inspected_final_revision",
                                              "source_seen_turn", "preview_seen_turn", "final_seen_turn")},
        },
        "archived_observations": {"material_rows": state.get("material_rows", [])},
    }
    if not notes:
        result["context_policy"]["bootstrap"] = (
            "No semantic work notes exist for this saved task. Earlier source-image observations are archived, "
            "not reconstructed as text judgments. Reinspect the relevant source frames before continuing uncertain "
            "work and record your own notes. Replayed archive images do not count as a new render review.")
        result["context_policy"]["source_image_history_requires_reinspection"] = True
    return result


def _images(messages):
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        label = "Archived image"
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                label = part.get("text", label)
            elif part.get("type") == "local_image":
                yield part, part.get("label", label)


def build_context(state, snapshot, system, *, compact=False, recent_turns=2):
    """Return fresh messages, retaining local_image references for caller hydration.

    An unfinished/malformed tool round falls back to the original conversation.
    Archived image supplements carry ``archived_context=True`` and
    ``review_evidence=False``; hydration must not count them as fresh inspection.
    """
    messages = state.get("messages", [])
    if not compact:
        return copy.deepcopy(messages)
    if type(recent_turns) is not int or recent_turns < 1:
        raise ValueError("recent_turns must be a positive integer")
    rounds = _complete_rounds(messages)
    if rounds is None:
        return copy.deepcopy(messages)
    recent = [message for block in rounds[-recent_turns:] for message in block]
    result = [{"role": "system", "content": system}]
    original = _original_request(state, messages)
    if original is not None:
        result.append(original)
    result.append({"role": "user", "content": json.dumps(_authority(state, snapshot),
                   ensure_ascii=False, separators=(",", ":"))})
    result.extend(copy.deepcopy(recent))
    if not any(_images(recent)):
        archived = list(_images(messages))[-4:]
        if archived:
            parts = [{"type": "text", "text": "Archived images for orientation only. These are not newly inspected "
                      "outputs and cannot establish a new preview/final review. Retrieve current frames for that purpose."}]
            for part, label in archived:
                image = copy.deepcopy(part)
                image.update(archived_context=True, review_evidence=False)
                parts.extend([{"type": "text", "text": label}, image])
            result.append({"role": "user", "content": parts})
    return result
