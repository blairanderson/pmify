# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A Claude Code **skill**, not a runtime application. The deliverable Claude Code loads is `SKILL.md` (frontmatter + prompt). The two Python files under `scripts/` are helpers that prompt calls out to. There is no build step, no package manager, no test suite — edits are validated by running the skill end-to-end on a real video.

When the skill is installed (cloned to `~/.claude/skills/pmify/`), Claude Code activates it when the user says "pmify" or pastes a video path and asks for a PRD/spec.

PMify is a sibling of [SOPify](https://github.com/blairanderson/sopify): same transcribe→sample→describe→merge pipeline, but the synthesis stage does PM work (canonical feature clustering → one PRD per feature) instead of SOP work (steps → decision tree).

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
        Claude synthesizes one PRD per feature + index
                       │
                       ▼
              OVERVIEW.md + PRD_NN_<slug>.md
```

The **vision pass has no separate API**: Claude itself reads each JPEG via the `Read` tool. That's the whole reason the skill works without cloud vision credentials. `extract_frames.py` just picks *which* frames to sample; the description loop lives in the skill prompt, not in code.

`extract_frames.py` combines five signals because pixel-diff alone is naive for screen recordings — a dropdown opening changes ~5% of pixels and slides under any sane scene threshold. The five signals are tagged in `frames.json`'s `source` field (`opening | narration_end | topic_keyword | action_keyword | pause_end | scene_change`) and merged with the `SOURCE_PRIORITY` table shared by `dedupe()` and `cap()` — when two candidates collide within `--gap` seconds, the higher-priority source wins. `topic_keyword` (interview vocabulary: "bug", "issue", "wish"…) is the signal PMify adds over SOPify — when a speaker names a pain point, the screen usually shows it.

The **feature-clustering stage is the skill's reason to exist** and lives entirely in the prompt (SKILL.md Step 6/6b), not in code. Canonical = deduplicated: interviews revisit topics, and each revisit is an extra evidence segment on the *same* `features.json` entry, never a new entry. If you touch the clustering instructions, keep that invariant front and center.

## Working directory contract

Output goes to `<video_dir>/pmify_out/<video_basename>/`, **never** `/tmp/`. The bundle co-locates with the source video so it's findable, version-controllable, and survives reboots. The skill resolves `$WORK` at the top of Step 1 — every later step references `$WORK/`.

Inside the bundle, **`path` fields in `frames_described.json` must be relative** (`frames/frame_NNNN.jpg`), not absolute. The PRDs embed them as `![](frames/...)`, so the bundle stays portable if the user moves it. `extract_frames.py` currently emits absolute paths in `frames.json`; the skill prompt rewrites them to relative when it produces `frames_described.json`. If you change this, change both ends.

## Editing the skill

- `SKILL.md` is the source of truth for behavior. The frontmatter `description` controls when Claude Code activates the skill — keep its trigger phrases ("pmify", video file paths, PRD/spec intent) intact.
- The internal "Decision tree" at the top of the Workflow section mirrors actual control flow. If you add or remove a WAIT point (currently: missing video path, feature-grouping confirmation when 2+ features), update that diagram.
- The PRD template in Step 7 is a **contract**: every PRD has the same sections in the same order. Consistency across PRDs is the product — don't add optional sections without making them unconditional.
- The pitfalls list at the bottom is hard-won. Don't drop entries without a good reason — each one corresponds to a real failure mode.

## Editing the scripts

Constraints, in order of importance:

1. **Python stdlib only.** No numpy, no cv2, no `requests`. The README advertises this; users `pip install` nothing.
2. **`ffmpeg` and `ffprobe` are the only external binaries.** `-hwaccel videotoolbox` is macOS-only — keep it gated on `uname == Darwin` in the skill prompt (the scripts themselves don't use it).
3. **Never re-encode video.** Audio extraction and single-frame JPEG stills only.
4. `extract_frames.py` prints the output JSON path to stdout and a summary line to stderr. Don't swap those — the skill prompt may pipe stdout.
5. The `SOURCE_PRIORITY` table is defined once at module level and used by both `dedupe()` and `cap()`. If you add a signal, add it there and to the docstring/README source lists in the same pass.

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

Then inspect `$WORK/frames/frames.json` — the `source` distribution tells you whether the heuristics fired sensibly. Sanity targets: 8–180 frames, a mix of sources (not all `scene_change`; on real interviews expect a healthy share of `topic_keyword`).

To test the merge in isolation, hand-edit `frames.json` into a `frames_described.json` by adding a `"description"` field to each entry, then:

```bash
python3 scripts/merge_timeline.py "$WORK/audio.json" "$WORK/frames_described.json" "$WORK/timeline.md"
```

## Installation note

The skill lives at `~/.claude/skills/pmify/`. If `<skill-dir>` references in `SKILL.md` ever stop working, that's because the user installed somewhere else — the skill resolves the path from where Claude Code loaded it. Don't hardcode `~/.claude/skills/pmify/` in command examples; keep the `<skill-dir>` placeholder.
