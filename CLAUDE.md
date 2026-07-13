# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A Claude Code **skill**, not a runtime application. The deliverable Claude Code loads is `SKILL.md` (frontmatter + prompt). The two Python files under `scripts/` are helpers the prompt calls out to. There is no build step, no package manager, no test suite — edits are validated by running the skill end-to-end on a real interview recording.

When the skill is installed (cloned to `~/.claude/skills/pmify/`), Claude Code activates it when the user says "pmify" or pastes a video path and asks for a PRD/spec.

PMify is a sibling of [SOPify](https://github.com/blairanderson/sopify): same transcribe → sample → describe → merge pipeline, but the synthesis stage does **product-management work** — clustering an interview into canonical features and writing one consistent PRD per feature — instead of SOP work.

## Product-management invariants (the reason this skill exists)

These live in the prompt (SKILL.md Steps 6–7), not in code. They are the part that makes pmify pmify; protect them when editing:

1. **Canonical = deduplicated.** A product interview covers 3–5 topics and circles back — the export bug at 02:10 and again at 11:30 is **one** `features.json` entry with two evidence segments, never two entries. If two "features" would have the same fix, they're one feature. This is the most common failure mode; the pitfalls list hammers it.
2. **The PRD template is a contract.** Every PRD has the same sections in the same order (Problem / Evidence / Current behavior / Desired behavior / Requirements / Out of scope / Open questions), even when a section is thin. A team should be able to diff two pmify PRDs and see only content vary, never shape. Don't add optional sections — make them unconditional or leave them out.
3. **Problem ≠ solution.** Interviewees narrate solutions ("just add a checkbox"). Their proposal is recorded under Desired behavior *as their proposal*; Requirements derive from the underlying problem. Anything not actually stated by the interviewee is tagged "(inferred)".
4. **Severity signals are verbatim.** Prioritization evidence is the interviewee's own words ("this blocks me every week"), quoted — never characterized ("user seemed frustrated"). `OVERVIEW.md`'s suggested priority order must be grounded in those quotes, not in product opinions.
5. **`OVERVIEW.md` is always written**, even for a single-feature video — it's the stable entry point of every bundle.
6. **PRDs cross-reference, they don't overlap.** Shared concerns get one owning PRD; the other points to it from Out of scope.
7. **One WAIT point for scope:** when 2+ features are detected, the proposed grouping (title, type, area, timestamps incl. revisits) is confirmed with the user *before* any PRD is written. Single-feature videos skip it. If you change this, update the decision-tree diagram at the top of SKILL.md's Workflow section.

## Architecture: how the pieces fit

The pipeline is intentionally split so each stage is auditable in isolation:

```
video ──► ffmpeg ──► audio.wav
                       │
                       ▼
                  whisperx (tiny.en, forced alignment) ──► audio.json
                       │
                       ▼
   extract_frames.py (5-signal smart sampling) ──► frames/*.jpg + frames.json
                       │
                       ▼
       Claude reads each frame via Read tool, writes description
                       │
                       ▼
                  frames_described.json   ──┐
                                            ├──► merge_timeline.py ──► timeline.md
                  audio.json              ──┘
                       │
                       ▼
     Claude clusters timeline into canonical features ──► features.json
                       │
                       ▼
              OVERVIEW.md + PRD_NN_<slug>.md (one per feature)
```

The **vision pass has no separate API**: Claude itself reads each JPEG via the `Read` tool — that's why the skill needs no cloud vision credentials. `extract_frames.py` just picks *which* frames to sample; the description loop and the feature clustering live in the skill prompt, not in code.

The PM-relevant sampling detail: on top of SOPify's four signals, `extract_frames.py` adds **`topic_keyword`** — interview vocabulary (`bug / issue / problem / broken / wish / request / …`). When a speaker names a pain point, the screen usually shows it, so those moments become PRD evidence screenshots. All five signals are tagged in `frames.json`'s `source` field and merged via the module-level `SOURCE_PRIORITY` table shared by `dedupe()` and `cap()`; if you add a signal, add it there and to the docstring/README lists in the same pass.

## Working directory contract

Output goes to `<video_dir>/pmify_out/<video_basename>/`, **never** `/tmp/`. The bundle co-locates with the source video so it's findable, version-controllable, and survives reboots. The skill resolves `$WORK` at the top of Step 1 — every later step references `$WORK/`.

Inside the bundle, **`path` fields in `frames_described.json` and `features.json` (`key_frames`) must be relative** (`frames/frame_NNNN.jpg`), not absolute. The PRDs embed them as `![](frames/...)`, so the bundle stays portable if the user moves it. `extract_frames.py` currently emits absolute paths in `frames.json`; the skill prompt rewrites them to relative when it produces `frames_described.json`. If you change this, change both ends.

## Editing the skill

- `SKILL.md` is the source of truth for behavior. The frontmatter `description` controls when Claude Code activates the skill — keep its trigger phrases ("pmify", video file paths, PRD/spec intent) intact.
- The internal "Decision tree" at the top of the Workflow section mirrors actual control flow. Two WAIT points today: missing video path, and feature-grouping confirmation (2+ features). Keep diagram and prose in sync.
- The pitfalls list at the bottom is hard-won. Don't drop entries without a good reason — each one corresponds to a real failure mode.

## Editing the scripts

Constraints, in order of importance:

1. **Python stdlib only.** No numpy, no cv2, no `requests`. The README advertises this; users `pip install` nothing.
2. **`ffmpeg` and `ffprobe` are the only external binaries.** `-hwaccel videotoolbox` is macOS-only — keep it gated on `uname == Darwin` in the skill prompt (the scripts themselves don't use it).
3. **Never re-encode video.** Audio extraction and single-frame JPEG stills only.
4. `extract_frames.py` prints the output JSON path to stdout and a summary line to stderr. Don't swap those — the skill prompt may pipe stdout.

## Testing changes

There's no automated test. To validate an edit:

```bash
# Pick a short product-interview or feedback recording you have on disk
VIDEO=~/path/to/some_interview.mov
VIDEO_DIR="$(cd "$(dirname "$VIDEO")" && pwd)"
VIDEO_BASE="$(basename "$VIDEO" | sed 's/\.[^.]*$//')"
WORK="$VIDEO_DIR/pmify_out/$VIDEO_BASE"
mkdir -p "$WORK/frames"

ffmpeg -y -hwaccel videotoolbox -i "$VIDEO" -vn -ac 1 -ar 16000 "$WORK/audio.wav"
whisperx "$WORK/audio.wav" --model tiny.en --output_format json --compute_type int8 --device cpu --output_dir "$WORK"
python3 scripts/extract_frames.py "$VIDEO" "$WORK/frames" "$WORK/audio.json"
```

Then inspect `$WORK/frames/frames.json` — the `source` distribution tells you whether the heuristics fired sensibly. Sanity targets: 8–180 frames, a mix of sources (on real interviews expect a healthy share of `topic_keyword`, not all `scene_change`).

To test the merge in isolation, hand-edit `frames.json` into a `frames_described.json` by adding a `"description"` field to each entry, then:

```bash
python3 scripts/merge_timeline.py "$WORK/audio.json" "$WORK/frames_described.json" "$WORK/timeline.md"
```

The PM stage (clustering → PRDs) has no script to test — judge it by reading `features.json` against `timeline.md`: every revisit merged, every severity signal verbatim, every PRD shaped identically.

## Installation note

The skill lives at `~/.claude/skills/pmify/`. If `<skill-dir>` references in `SKILL.md` ever stop working, that's because the user installed somewhere else — the skill resolves the path from where Claude Code loaded it. Don't hardcode `~/.claude/skills/pmify/` in command examples; keep the `<skill-dir>` placeholder.
