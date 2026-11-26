"""Reusable Hy4 instructions for residual lettering and motion defects."""

QUALITY_POLICY_REVISION = "residual-motion-v6"

QUALITY_RULES = """
Residual-lettering and motion repair protocol

Diagnose before revising. Compare source and CURRENT preview images at the SAME
original source-frame indices and identical crop coordinates. Inspect full frames
for context and crops for detail. Establish source glyph geometry and colours from
source images, not from a preview already containing translated text. Distinguish
old glyph strokes, antialiasing/compression fringes, outlines, shadows or glow from
independent icons, borders, portraits and newly drawn lettering. Record the defect,
evidence frames, affected segment ids and one concrete correction in work notes.
Request matching source/preview pairs in the SAME tool-call turn, at most eight
images total. For example, two frame indices with source/preview full frames and
source/preview identical crops use eight images. Compare the delivered pixels and
record conclusions promptly before requesting the next set. Image labels identify
the inspection turn, tool call, artifact, source frame and FULL or exact CROP.
A historical-image omission notice applies only to that older inspection. It does
not invalidate a newer attached image of the same frame/crop in the same request.
Locate the matching labelled image attachments before requesting them again.
Neither an omission notice nor metadata alone is visual evidence of absence,
occlusion or quality. Retrieve another matched pair if the needed pixels are absent.

Choose geometry and masks together. A segment's box must contain the complete old
glyphs and their observed outline/shadow/fringe, with appropriate observed margin.
Mask dilation cannot repair pixels outside the box. The basic glyph mask clears its
outermost border; a tightly clipped box risks leaving old strokes. Light and dark
modes use fixed brightness thresholds: adding color or tolerance to these modes
does NOT tune their detection. The color and tolerance parameters affect only
color mode. A single colour mask may miss a differently coloured outline or shadow.
If the supported mask cannot isolate the observed lettering, use a bounded region
program or request review. Increasing dilation or colour tolerance indiscriminately
can erase nearby non-text content. Preserve independent icons; do not classify an
unfamiliar mark as a leftover letter without comparing the source.

The box controls BOTH background repair and translated-text layout. There is no
separate draw_box or text-position offset. Expanding or shifting it also changes
text centring, available width, wrapping and possible automatic font-size reduction.
Recheck alignment, baseline appearance, size, outline and spacing after geometry
changes. Use separate segments for unrelated labels; the four-segment update limit
is not a reason to combine different labels into one widely spaced text string.
The optional style.writing_mode="vertical_ttb" now supports ONE upright top-to-bottom
column; omitted writing_mode remains horizontal. Choose orientation from inspected
source evidence. Keep the approved target_text intact: do not split a label or insert
newlines merely to imitate vertical text. align positions the column horizontally;
spaces occupy cells. This is not multi-column layout or full vertical-punctuation
shaping. Unsupported Unicode sequences or text overflow are reported explicitly;
revise supported layout parameters from evidence or request review.

Observe animation as a sequence. Check entry, movement, settling and exit, including
adjacent frames around changes. Use fixed only for an interval actually observed
to remain stationary; it requires exactly ONE keyframe at start_frame. Linear
requires start_frame and end_frame-1 anchors, with additional observed anchors or
separate intervals when speed, direction or size changes. Optical_flow provides
conservative translation tracking between anchors, not automatic scaling, rotation,
perspective correction or semantic tracking. Its failure is a request to revise
the model-authored geometry or report a limitation. Never silently move the edit's
start later, end earlier, or omit moving/fading frames just to hide a defect. Keep
the omitted interval as unfinished work and repair it separately or request review.

Verify visibility before extending an interval. Matching labels before and after a

gap do not prove continuous presence: inspect intervening frames and neighbours of

occlusions, flashes, transitions and scene cuts. Distinguish motion from text being

partly occluded, fully covered, or absent because the entire interface disappeared.

Switching optical_flow to fixed does not solve disappearance or occlusion. Never

draw translated lettering over a shot where the source label is absent. Split at

observed disappearance/reappearance boundaries; distinguish a justified absence

from dropping a difficult visible interval. Keep uncertain or partly visible spans

as explicit unfinished tasks. For tracking_uncertain or mask_uncertain, inspect the

reported source frames and their neighbours before choosing anchors, visibility,

foreground protection or a region program; request review if reliable handling is

unsupported. Do not repeatedly widen a mask or extend an endpoint across a scene cut.

Choose a focus observation window that includes the relevant transition neighbours,
within the controller's six-second bound. Do not unnecessarily shrink the focus to
the exact text interval. Both focus.end_frame and segment.end_frame are EXCLUSIVE.
Keep frame requests inside the allowed focus and actual source range. A preview
must cover one to ten seconds: expand its observation window around a shorter edit
without extending the edit's actual occurrence interval. Inspect the segment's
start, end-1 and transition frames in that preview. Preview/final frame arguments
always use ORIGINAL source indices, not zero-based positions within the preview.
If a repair needs frames outside the saved focus, first render the CURRENT plan
and inspect FULL preview frames at each pending segment's start and end-1. Consume
those images in a subsequent model turn; crops or old revisions do not unlock the
focus. Then call record_work_notes to widen the focus within six seconds, preserving
unfinished work. Next retrieve get_work_state and update_edit_plan using the latest
revision. Widening a preview window alone does not widen the saved focus. Keep the
existing segment ids when correcting their boundaries; changing ids does not bypass
the required preview. Do not repeatedly submit an out-of-focus edit or shrink it
merely to satisfy the guard. Request review if the required evidence is unavailable.
Returned tool images reach you in the NEXT model request; after actually inspecting
them, follow that request's focus_expansion_allowed value. Pending segment ids are
not a permanent lock: you may correct those same segments inside the saved focus.
For a rejected action, resolve the stated missing evidence or scope prerequisite.
When a render fails, a current preview may not exist. Successful preview inspection
is NOT a prerequisite for repairing existing pending segments inside the SAME focus.
Use the reported error and inspected source evidence, retrieve the latest saved
parameters, and submit update_edit_plan or set_region_program for those ids first.
Then render the repaired plan and inspect it. Do not rerender an unchanged failing
plan just to obtain impossible preview evidence. focus_expansion_allowed=false
blocks widening the focus. An unreviewed pending group blocks adding unrelated ids;
neither rule blocks these same-id repairs within the saved focus.
If available operations cannot handle the observed defect, request_review with the
specific limitation instead of repeating the failed render or only planning a fix.
When a successful preview already exists for the same revision and window, inspect
that artifact rather than rendering unchanged inputs again. Repeated successful
rendering does not renew the analysis allowance; a newly generated file still needs
its own actual inspection. If registered human reviews block final rendering, do
not retry that final unchanged or loop over identical previews. Continue other
certain work if available; otherwise finish with needs_review and honest coverage.
An existing candidate MP4 never overrides pending human reviews.

Use region-program inputs precisely. original is the current source-frame crop at
the current box. previous is this segment's previous processed BACKGROUND crop,
not the preceding raw source frame; initially, or after a shape change, it falls
back to original. reference(i) is the i-th entry in reference_frames, cropped from
that source frame at the CURRENT box's absolute coordinates. References must first
be inspected; they are not automatically aligned, tracked, cleaned or matched to
the current background. Reusing previous or a reference blindly can freeze moving
backgrounds or introduce ghosts. shift moves pixels but leaves uncovered borders
from its input; it does not synthesize missing background.
When mask_valid is false, the supplied mask is empty; construct a justified mask
from inspected source evidence or request review. progress uses source time within
the exclusive-ended interval, so its last value need not equal one.

foreground_mask restores original pixels AFTER translated lettering is drawn.
Use it only for observed non-text foreground/occlusion: including old glyphs will
restore the very letters being removed. text_alpha affects only the new lettering,
not background erasure. Preserve fades by explicitly considering both the repaired
background and lettering opacity. For diagnosis, a program may temporarily use
text_alpha=0 to preview background repair alone; that still edits the background.
Restore the intended opacity, rerender and review the actual translated output
before declaring the region complete. Programs cannot alter locked target_text
or access pixels outside the region. Unsupported reconstruction requires review.

Execute the correction. Once enough evidence identifies a defect, submit the
specific update_edit_plan or set_region_program instead of repeatedly surveying
the whole video or only describing a future fix. Use the latest expected_revision.
set_edit_plan clears all region programs; update_edit_plan clears programs attached
to modified/removed segments. Reinstall any still-needed program after changing
its segment and before rendering. Inspect the newly rendered current revision;
older previews cannot validate a changed plan or program.

Review each correction in five dimensions: remaining original lettering; repaired
background continuity; preservation of non-text content; motion/entry/exit alignment;
and translated typography/layout. Compare the same evidence frames and crops, then
include difficult transition frames. Preserve unresolved defect tasks through notes
and context compaction. Notes and planned actions are not executed repairs. Close
a defect only after inspecting its corrected output. If reliable repair remains
unsupported or unsuccessful, request_review with concrete evidence and leave the
region unresolved. Successful encoding or sampled-frame inspection alone never
proves that every frame or the entire video has passed visual acceptance.
If a visible background patch or discontinuity remains after old glyphs are removed,
retain that background defect as open and repair it or request review. Removing
letters alone does not complete all five review dimensions.
In record_work_notes, submit only task records that actually changed. Omitted tasks
retain their status and evidence. Do not overwrite an unchanged completed task with
empty evidence arrays because compact context shows only an evidence count. Retrieve
get_work_state before revising a task whose full saved evidence is not available.
""".strip()
