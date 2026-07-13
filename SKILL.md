---
name: pmify
description: Understand the product feedback in a screen recording (product interview, feedback session, bug walkthrough) and produce consistent PRD/spec markdown documents with embedded screenshots — one PRD per canonical feature/issue discussed. Use when the user mentions "pmify", pastes a video file path (e.g. .mp4, .mov, .webm) and wants a PRD or spec, or attaches a recording of a product interview or feedback session.
---

# PMify

Turn a product-feedback video (a customer interview, a feedback session, a bug walkthrough) into **consistent PRD/spec markdown documents**. The skill **hears** what the speaker says (Whisper transcript), **sees** what they show (frame samples described inline), interleaves both into a raw timeline, then does the PM work: it clusters the discussion into **canonical features/issues** — a single interview typically covers 3–5 distinct topics, often revisiting the same one twice — and writes one uniformly-structured PRD per feature, plus an `OVERVIEW.md` index.

## Inputs

- A video file path (the user will provide it; otherwise ask).
- Optional: frame-sampling overrides for Step 3 — `--scene <N>` (pixel-diff sensitivity, default `0.05`, lower = more frames), `--max <N>` (cap on total frames, default `200`), `--gap <N>` (merge sampling candidates within N seconds, default `0.4`).
- Optional: product/app name and interviewee name (otherwise inferred or asked).

## Tooling (use only the fastest path)

- **WhisperX:** `whisperx --model tiny.en --output_format json --compute_type int8 --device cpu` (built on faster-whisper + wav2vec2 forced alignment — word-level timestamps are tight (~±20ms) and on by default, no flag needed). For non-English: `--model base --language <code>`. Install once with `uv tool install whisperx`. First run downloads ~75 MB ASR model + ~360 MB English alignment model; both cache under `~/.cache/`.
- **ffmpeg:** add `-hwaccel videotoolbox` for decode on macOS. We never re-encode video — we only extract audio and JPEG stills.
- **Python 3** (stdlib only — no numpy, no cv2, no cloud APIs).
- **Scripts:** `<skill-dir>/scripts/` (where `<skill-dir>` is the directory containing this SKILL.md — typically `~/.claude/skills/pmify/`)
  - `extract_frames.py` — smart frame sampling combining 5 signals (narration boundaries, topic keywords, action keywords, pause endpoints, scene change) → JPEGs + `frames.json` manifest
  - `merge_timeline.py` — interleave Whisper segments + described frames → `timeline.md`

### Running long commands (whisperx, ffmpeg)

`whisperx` is the slow step — expect **30s–3min on `tiny.en`** depending on video length, plus a one-time ~360 MB alignment-model download on the first run. Two acceptable patterns; **never mix them**:

- **Foreground (preferred).** Call Bash with `timeout: 600000` (10 min). The skill blocks until whisperx finishes; no polling needed.
- **Background.** Call Bash with `run_in_background: true`. The harness auto-notifies on completion — **do not** chain `sleep N && cat <task.output>`; the harness blocks that. If you genuinely need to check progress before the auto-notification fires, use the `Monitor` tool with an `until` loop, not `sleep`.

Same rule applies to `ffmpeg` on long videos and to `extract_frames.py` on dense recordings.

Working dir: `<source_video_dir>/pmify_out/<video_basename>/` (mkdir at start, persistent — every intermediate artifact lives here so the user can audit each step). Never use `/tmp/` for pmify outputs. The bundle co-locates with the source video so it's findable, version-controllable, and survives reboots.

Resolve it at the top of Step 1:

```bash
VIDEO_DIR="$(cd "$(dirname "$VIDEO")" && pwd)"
VIDEO_BASE="$(basename "$VIDEO" | sed 's/\.[^.]*$//')"
WORK="$VIDEO_DIR/pmify_out/$VIDEO_BASE"
```

All subsequent steps reference `$WORK/` — not `/tmp/pmify/`. Example tree for `~/dev/brokerage/interviews/acme-q3-feedback.mp4`:

```
~/dev/brokerage/interviews/pmify_out/acme-q3-feedback/
├── audio.wav
├── audio.json
├── frames/
│   ├── frames.json
│   └── frame_0001.jpg … frame_NNNN.jpg
├── frames_described.json
├── timeline.md
├── features.json
├── OVERVIEW.md
├── PRD_01_bulk-invoice-export.md
├── PRD_02_search-timeout.md
└── PRD_03_mobile-nav-redesign.md
```

---

## Workflow

### Decision tree (read this first)

```
START
  │
  ▼
[Has video path?] ── No ──> Ask user. WAIT.
  │
  Yes
  ▼
Step 1: ffmpeg              →  $WORK/audio.wav
Step 2: whisperx            →  $WORK/audio.json
Step 3: extract_frames.py   →  $WORK/frames/, $WORK/frames/frames.json
  │
  ▼
[Frame count?]
  ├─ <8       ──> Offer retry: --scene 0.02 --gap 0.25
  ├─ >180     ──> Offer retry: --max 120 --gap 0.8
  └─ 8 – 180  ──> Continue.
  │
  ▼
Step 4: Vision pass (Read every frame, describe in 1–3 sentences)
        →  $WORK/frames_described.json  (path fields RELATIVE)
Step 5: merge_timeline.py   →  $WORK/timeline.md
  │
  ▼
Step 6: Cluster timeline into canonical features
  │
  ▼
[How many features?]
  ├─ 1        ──> Continue. Single PRD, skip confirmation.
  └─ 2 +      ──> Present grouping. Confirm/edit with user. WAIT.
  │
  ▼
Step 6b: Finalize features   →  $WORK/features.json
Step 7: Compose PRDs + index →  $WORK/PRD_NN_<slug>.md, $WORK/OVERVIEW.md
Step 8: Print paths + open. Done.
```

Two WAIT points only — missing video path, and feature-grouping confirmation in Step 6 (skipped when the video covers a single topic). Everything else flows straight through; the heuristic branches in Step 3 are *offered* to the user but don't block — if the count is in range, proceed without asking.

### Step 1 — Set up & extract audio

```bash
VIDEO_DIR="$(cd "$(dirname "$VIDEO")" && pwd)"
VIDEO_BASE="$(basename "$VIDEO" | sed 's/\.[^.]*$//')"
WORK="$VIDEO_DIR/pmify_out/$VIDEO_BASE"
mkdir -p "$WORK/frames"

# Hardware-decode flag is macOS-only; omit on Linux.
HWACCEL=""
[ "$(uname)" = "Darwin" ] && HWACCEL="-hwaccel videotoolbox"

ffmpeg -y $HWACCEL -i "$VIDEO" -vn -ac 1 -ar 16000 "$WORK/audio.wav"
```

### Step 2 — Transcribe

Run synchronously with a 10-minute timeout (`timeout: 600000` on the Bash call). Do not background + sleep — see "Running long commands" above.

```bash
whisperx "$WORK/audio.wav" --model tiny.en --output_format json --compute_type int8 --device cpu --output_dir "$WORK"
```

This writes `$WORK/audio.json` with segments and forced-aligned word-level timestamps (~±20ms vs. openai/whisper's ~±200ms). On Apple Silicon, `--compute_type int8 --device cpu` is the right combo — `faster-whisper` doesn't use MPS. Word timestamps come from WhisperX's alignment pass and are on by default; don't pass `--no_align` or `extract_frames.py`'s keyword cues will go dead.

### Step 3 — Smart frame sampling (5 signals combined)

Pixel-diff alone is naive for screen recordings: a click that opens a small dropdown only changes ~5% of the frame and slides under `scene=0.3`. `extract_frames.py` combines **five signals** so we sample at moments the interview actually advanced:

1. **Narration boundaries** — sample at the end of every Whisper segment (the thing just described is on screen now).
2. **Topic-keyword cues** — when the speaker says `issue / bug / problem / broken / error / crash / feature / missing / confusing / annoying / frustrating / wish / request / improvement / idea / feedback / pain`, sample ~0.5s after that word — in a product interview, the screen usually shows the thing being complained about or requested.
3. **Action-keyword cues** — when the speaker says `click / open / select / type / navigate / save / scroll / submit / press / choose / enter / paste / drag / hover / fill / upload / search / toggle / check / tap`, sample ~0.7s after that word's timestamp.
4. **Pause boundaries** — silences >1.5s between segments mean the speaker stopped to *show* something; sample at the end of the pause.
5. **Low-threshold scene change** — `select='gt(scene,0.05)'` (default), tuned for UI changes, not video cuts.

```bash
python3 <skill-dir>/scripts/extract_frames.py "$VIDEO" "$WORK/frames" "$WORK/audio.json"
```

Optional flags: `--scene 0.05` (sensitivity), `--max 200` (cap), `--gap 0.4` (merge candidates within N seconds). Pass `none` instead of `audio.json` to fall back to scene-change + opening frame only.

The manifest at `$WORK/frames/frames.json` includes a `source` field per frame (`opening | narration_end | topic_keyword | action_keyword | pause_end | scene_change`) so you can see *why* each frame was picked.

Report the frame count to the user. Heuristics:

- **>180 frames** → the video is dense or thresholds are too sensitive. Offer to retry with `--max 120 --gap 0.8`.
- **<8 frames** → the speaker is quiet and the screen is static. Offer to retry with `--scene 0.02 --gap 0.25`.
- **8–180** → proceed.

### Step 4 — Describe each frame (vision pass)

Read `$WORK/frames/frames.json`. For each entry, use the `Read` tool on the frame's `path` so you actually see the image, then write a 1–3 sentence description focused on:

- Which app, page, or screen is visible (specific names — "Stripe Dashboard → Customers" beats "a dashboard").
- What UI element is highlighted, focused, or being pointed at.
- Where the cursor is and what it is hovering near.
- What state has changed since the previous frame (modal opened, error banner appeared, row added).
- **Any problem or request being demonstrated** — error states, broken layouts, empty states, slow-loading spinners, the workaround the speaker is showing, or feature language like "I wish it could…", "it should…", "the problem is…", "another thing…". When you spot the *start of a new topic* or fresh evidence for one, append a candidate entry to `$WORK/features.json` as you go — see Step 6b for the schema. This is your working tmp file; it gets consolidated later.

Format each description as a single string, e.g.:

> User is on the QuickBooks dashboard, on the "Sales → Invoices" page. A red error toast reads roughly "export failed" (small text). The cursor hovers over the Export button — this appears to be the bulk-export failure the speaker is describing.

Save the augmented manifest to `$WORK/frames_described.json` — same JSON shape as `frames.json` plus a `"description"` field on each entry. **Important:** rewrite the `path` field on each entry from absolute to relative (`frames/frame_NNNN.jpg`, not `$WORK/frames/...` or `/tmp/...`). This matches the relative paths the final PRDs will embed (`![](frames/frame_0001.jpg)`) and keeps the bundle portable if the user moves it.

If two consecutive frames look essentially identical, **drop the redundant entry from `frames_described.json` before running merge_timeline.py.** The script doesn't dedupe — pre-filtering the manifest is the cleanest fix. If you're unsure whether two frames are truly identical, keep both; the synthesis step in Step 7 can still embed only one screenshot per piece of evidence.

### Step 5 — Build the raw timeline

```bash
python3 <skill-dir>/scripts/merge_timeline.py "$WORK/audio.json" "$WORK/frames_described.json" "$WORK/timeline.md"
```

This produces `$WORK/timeline.md` — chronological blocks of `## [HH:MM:SS] Visual` (with embedded `![](frames/frame_NNNN.jpg)` and `Visual: ...`) and `## [HH:MM:SS] Narration` (with verbatim transcript quotes). This is the auditable record — don't skip it.

### Step 6 — Cluster into canonical features

This is the PM step, and the reason pmify exists. Read `timeline.md` end to end and group the discussion into **canonical features/issues**:

- **Canonical means deduplicated.** Interviews circle back: the same export bug mentioned at 02:10 and again at 11:30 is **one** feature with two evidence segments — not two features. Merge every revisit into the topic's single entry.
- **Name each feature the way the product team would**, anchored to the app area shown on screen ("Invoicing → bulk export", not "the thing she complained about first").
- **Classify each** as `bug | feature | improvement | question`.
- Expect **3–8 features** for a typical interview. If you have 15+, you're splitting one topic into its sub-complaints — cut hard. If you have 1, the video is a single-topic walkthrough; skip the confirmation below.

Present the grouping to the user before writing PRDs:

> I see 4 canonical features in this interview:
> 1. **[bug] Bulk invoice export fails for >100 rows** — Invoicing (02:10–04:32, revisited 11:30–12:05)
> 2. **[feature] Saved search filters** — Search (04:32–07:15)
> 3. **[improvement] Mobile nav is 3 taps too deep** — Navigation (07:15–09:40)
> 4. **[question] Unclear how permissions interact with exports** — Admin (09:40–11:30)
>
> Want me to write one PRD per feature (recommended), merge/split any of these, or drop any?

WAIT for the answer, then adjust the grouping accordingly. Skip the question only when there's a single feature.

### Step 6b — Finalize the feature list

Consolidate into `$WORK/features.json` — a JSON list of objects. Merge the candidates you appended during Step 4, dedupe revisits into single entries, and order by first appearance:

```json
[
  {
    "id": 1,
    "slug": "bulk-invoice-export",
    "title": "Bulk invoice export fails for >100 rows",
    "type": "bug",
    "area": "Invoicing",
    "segments": [
      { "start": "00:02:10", "end": "00:04:32" },
      { "start": "00:11:30", "end": "00:12:05" }
    ],
    "summary": "Export silently fails when the selection exceeds ~100 invoices; user re-exports in batches as a workaround.",
    "severity_signals": ["\"this blocks me every single week\"", "user demonstrated a manual batching workaround"],
    "key_frames": ["frames/frame_0012.jpg", "frames/frame_0047.jpg"]
  }
]
```

- `slug` is kebab-case and becomes the PRD filename (`PRD_01_bulk-invoice-export.md`).
- `segments` lists **every** timeline span where the topic came up, including revisits.
- `severity_signals` captures verbatim urgency/frequency language ("every week", "we almost churned over this") — this is the prioritization evidence, don't paraphrase it away.
- `key_frames` uses relative paths, same as `frames_described.json`.

### Step 7 — Compose the PRDs and overview

For each entry in `features.json`, compose `$WORK/PRD_NN_<slug>.md` (`NN` = zero-padded id) with **exactly this structure** — consistency across PRDs is the product; a team should be able to diff two pmify PRDs and see only content vary, never shape:

```markdown
# PRD: <Feature Title>

| | |
|---|---|
| **Type** | Bug / Feature / Improvement / Question |
| **Area** | <canonical app area> |
| **Source** | <video filename> — 00:02:10–00:04:32, 00:11:30–00:12:05 |
| **Reported by** | <interviewee name, or "(not stated)"> |

## Problem
1–3 sentences: the user's problem in their terms. What hurts, how often, and what it costs them. Not the solution.

## Evidence

![](frames/frame_0012.jpg)

> "Verbatim quote from the transcript that shows the pain." (02:14)

What the screen shows at that moment, per the vision pass. Repeat quote+screenshot pairs for each evidence segment, including revisits.

## Current behavior
What the product does today, as demonstrated on screen. Stick to what was actually shown.

## Desired behavior
What the user asked for or implied. If they proposed a specific solution, record it here **as their proposal** — don't silently promote it to a requirement.

## Requirements
1. Numbered, testable "The system shall…" statements derived from the problem. Tag anything the interviewee didn't actually state as "(inferred)".
2. ...

## Out of scope
What this PRD deliberately does not cover (adjacent asks that belong to other PRDs in this bundle — cross-reference them by number).

## Open questions
- Anything ambiguous from the interview that needs a follow-up before building.
```

Embed only **key** screenshots — typically one per evidence segment, at the moment the problem is most visible (the error toast, the missing button, the workaround mid-flight). All frames are still on disk under `$WORK/frames/` if the reader wants more.

#### Overview index (`OVERVIEW.md`)

Alongside the PRDs, compose `$WORK/OVERVIEW.md`:

```markdown
# <Interview/Video Title> — Feature Overview

**Source:** <video filename> (<duration>) · **Interviewee:** <name or "(not stated)"> · **Date processed:** <date>

| # | Feature | Type | Area | Severity signals | PRD |
|---|---------|------|------|------------------|-----|
| 1 | Bulk invoice export fails for >100 rows | bug | Invoicing | "blocks me every week" | [PRD_01](PRD_01_bulk-invoice-export.md) |
| 2 | ... | | | | |

## Summary
One paragraph: who was interviewed, what product, and the dominant theme across the features.

## Suggested priority order
Ranked list with a one-line justification each, grounded in the severity signals — not your own product opinions.
```

If there's a single feature, still write `OVERVIEW.md` — a one-row table is fine; it's the stable entry point for every pmify bundle.

### Step 8 — Deliver

- Print all paths on separate lines so the user knows where everything is:
  - **Overview:** `$WORK/OVERVIEW.md`
  - **PRDs:** `$WORK/PRD_NN_<slug>.md` (one line each)
  - **Audit timeline:** `$WORK/timeline.md`
  - **Full bundle:** `$WORK/` (also contains `audio.wav`, `audio.json`, `frames/`, `frames_described.json`, `features.json` — every intermediate the model produced, kept for auditing)
- `open "$WORK/OVERVIEW.md"` (and the PRDs if there are ≤3).
- Offer to:
  - Re-run with a different scene threshold (`--scene 0.02` for more frames, `--scene 0.05 --gap 0.8` for fewer) if a PRD is missing visual evidence or has too many redundant frames.
  - Merge or split features if the grouping in Step 6 was answered wrong.
  - Delete `$WORK/audio.wav` once the user has finished auditing — it's the largest file in the bundle.

---

## Pitfalls (lessons — don't repeat)

- **Canonical means deduplicated.** The most common failure is treating each mention as a new feature. Interviews revisit topics; merge revisits into one entry with multiple evidence segments. If two "features" would have the same fix, they're one feature.
- **Problem ≠ solution.** Users narrate solutions ("just add a checkbox"). Capture the proposal under Desired behavior *as their proposal*, but derive Requirements from the underlying problem. Don't let an interviewee's UI sketch become "The system shall add a checkbox".
- **Don't invent requirements the interviewee didn't state.** Anything you derive purely from frames or your own product sense gets tagged "(inferred)" so the reader knows.
- **Severity signals are verbatim or nothing.** "This blocks me every week" is prioritization gold; "user seemed frustrated" is your opinion. Quote, don't characterize.
- **Cursor position matters in screen recordings.** Always call out where the cursor is and what it's near — that's often the only signal of what the speaker is pointing at.
- **Verbatim quotes > paraphrasing** when the speaker is precise. When the speaker is rambling, paraphrase — but keep at least one verbatim quote per evidence block.
- **State the plan in one line, then act.** Don't narrate every frame description iteration.
- **`path` fields in `frames_described.json` are relative, not absolute.** The bundle is portable — `pmify_out/<video>/` can be moved to another machine. Absolute `/tmp/...` or `$WORK/...` paths break this; always emit `frames/frame_NNNN.jpg`.
- **Describe what you see, not what you guess the screenshot says.** Low-res screen recordings invite OCR-style hallucination (claiming the error reads "Export Failed: Timeout" when it actually reads "Export failed"). When text is small or blurry, describe the UI element ("a red error toast near the top-right") instead of inventing the label.
- **Every PRD has the same shape.** Same sections, same order, same table header — even when a section is thin ("Out of scope: nothing adjacent discussed."). Consistency is what makes a stack of pmify PRDs scannable.
- **PRDs cross-reference, they don't overlap.** When two features touch (permissions × exports), pick one owner for the shared concern and cross-reference from the other's Out of scope section.
- **Don't `sleep N && cat <task.output>` to wait for a background bash task.** The harness blocks this. Either run the long command in the foreground with `timeout: 600000`, or run it backgrounded and wait for the auto-notification. If you must poll, use `Monitor` with an `until` loop — never a leading `sleep`.
