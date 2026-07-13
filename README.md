# PMify

A [Claude Code](https://claude.com/claude-code) skill that turns product-feedback videos into consistent PRD/spec markdown documents.

Point it at a screen recording of a product interview, feedback session, or bug walkthrough and it will:

1. **Hear what they say** — transcribes the audio with [WhisperX](https://github.com/m-bain/whisperX) (faster-whisper + wav2vec2 forced alignment, ~±20ms word-level timestamps).
2. **See what they show** — combines five sampling signals (narration boundaries, topic keywords, action keywords, pause endpoints, scene change) so it catches the error toast and the dropdown alike, then Claude reads each frame as an image to describe what's on screen, where the cursor is, and what problem is being demonstrated.
3. **Interleave both** into a raw timestamped timeline (`timeline.md`) — auditable, chronological.
4. **Cluster the discussion into canonical features** (`features.json`) — a single interview typically covers 3–5 distinct issues/features, often revisiting the same one twice; PMify merges revisits into one entry per topic and confirms the grouping with you.
5. **Write one uniformly-structured PRD per feature** — Problem, Evidence (verbatim quotes + screenshots), Current behavior, Desired behavior, Requirements, Out of scope, Open questions.
6. **Index everything in `OVERVIEW.md`** — a table of all features with types, areas, severity signals, and a suggested priority order grounded in the interviewee's own words.

No cloud vision APIs. Frame description uses Claude's native multimodal `Read` tool. Runs entirely on your machine.

PMify is a sibling of [SOPify](https://github.com/blairanderson/sopify) — same pipeline, different PM: SOPify turns *how-to* recordings into Standard Operating Procedures; PMify turns *feedback* recordings into PRDs.

## Why this exists

Customer interviews are dense: 20 minutes of video covers 3–5 issues, circles back, mixes bugs with wishes. Turning that into tickets your team can act on is slow, and every PM writes them up differently. PMify closes the gap: record once, get a consistent stack of PRDs with screenshots and verbatim evidence in minutes.

## Requirements

- macOS, Linux, or Windows. On macOS the skill auto-enables VideoToolbox hardware decode (`-hwaccel videotoolbox`); on other platforms it falls back to software decode automatically — no edits needed.
- [Claude Code](https://claude.com/claude-code)
- `ffmpeg` and `ffprobe` (`brew install ffmpeg`)
- [`whisperx`](https://github.com/m-bain/whisperX) — recommended install: `uv tool install whisperx` (or `pip install whisperx`). First run downloads a ~75 MB ASR model plus a ~360 MB English alignment model into `~/.cache/`.
- Python 3 (stdlib only — no numpy, no cv2, no cloud APIs)

## Install

```bash
git clone git@github.com:blairanderson/pmify.git ~/.claude/skills/pmify
```

Restart Claude Code. The skill activates whenever you mention "pmify" or paste a video path and ask for a PRD/spec.

## Usage

In Claude Code:

```
pmify ~/Desktop/acme-q3-feedback.mov
```

The skill will:

1. Extract audio and transcribe with WhisperX (forced-aligned, word-level timestamps).
2. Smart-sample frames using the 5-signal heuristic (see below) into `<video_dir>/pmify_out/<video_basename>/frames/`.
3. Read each frame and write a 1–3 sentence visual description, flagging candidate features/issues as it goes.
4. Build `timeline.md` — the raw interleaved transcript + visuals.
5. Cluster the timeline into canonical features and confirm the grouping with you (merged revisits, product-team naming, bug/feature/improvement/question classification).
6. Consolidate the confirmed grouping into `features.json`.
7. Synthesize one `PRD_NN_<slug>.md` per feature (embedded screenshots, verbatim evidence) plus an `OVERVIEW.md` index with a suggested priority order.
8. `open` the results.

All artifacts live under `<video_dir>/pmify_out/<video_basename>/` — co-located with the source video so they're findable, version-controllable, and survive reboots. Nothing goes to `/tmp/`.

## How the visual pass works

There's no separate vision API. Frame description uses Claude's multimodal `Read` tool — Claude actually sees each image and writes a 1–3 sentence description (which app, what page, where the cursor is, what problem is being demonstrated). A small Python script merges those descriptions with the WhisperX transcript into a single chronological markdown file.

### Smart sampling (not just scene change)

Pixel-diff alone is naive for screen recordings — a click that opens a dropdown changes ~5% of the frame and never triggers a "scene." So we combine **five signals**:

1. **Narration boundaries** — sample at the end of every WhisperX segment (the thing just described is on screen).
2. **Topic-keyword cues** — when the speaker says `issue / bug / problem / broken / error / crash / feature / missing / confusing / annoying / frustrating / wish / request / improvement / idea / feedback / pain`, sample ~0.5s after the word — in a product interview, the screen usually shows the thing being complained about or requested.
3. **Action-keyword cues** — when the speaker says `click / press / open / close / navigate / select / type / enter / save / submit / choose / pick / scroll / drag / drop / copy / paste / highlight / hover / fill / upload / download / delete / search / toggle / check / uncheck / tap`, sample ~0.7s after the word.
4. **Pause boundaries** — silences >1.5s mean the speaker stopped to show something; sample at the end of the pause.
5. **Low-threshold scene change** — ffmpeg `scene=0.05` (tuned for UI changes, not hard cuts).

Each frame in `frames.json` gets a `source` tag (`narration_end`, `topic_keyword`, `action_keyword`, `pause_end`, `scene_change`, `opening`) so you can see *why* it was sampled. When two signals collide within `--gap` seconds (default `0.4`), the higher-priority source wins (`narration_end` > `topic_keyword` > `action_keyword` > `pause_end` > `scene_change` > `opening`).

Tunable flags on `extract_frames.py`:

- `--scene <T>` — pixel-diff sensitivity (default `0.05`, lower = more frames)
- `--max <N>` — cap on total frames (default `200`)
- `--gap <S>` — merge candidate timestamps within S seconds (default `0.4`)

Pass `none` instead of `audio.json` to fall back to scene change + opening frame only (no narration-driven signals).

## Repo structure

```
pmify/
├── SKILL.md                  # the skill prompt Claude Code reads
├── CLAUDE.md                 # contributor guide for Claude Code working on this repo
├── scripts/
│   ├── extract_frames.py     # 5-signal smart frame sampling → JSON manifest
│   └── merge_timeline.py     # interleave WhisperX segments + described frames → timeline.md
├── README.md
├── LICENSE
└── .gitignore
```

## Output structure

For an input video at `~/videos/acme-q3-feedback.mov`, outputs land at `~/videos/pmify_out/acme-q3-feedback/`:

```
<video_dir>/pmify_out/<video_basename>/
├── audio.wav                 # mono 16 kHz, fed to WhisperX
├── audio.json                # WhisperX output: segments + forced-aligned word-level timestamps
├── frames/
│   ├── frame_0001.jpg        # opening state (always)
│   ├── frame_0002.jpg        # next sampled moment
│   ├── ...
│   └── frames.json           # manifest: index, time, path, source (why it was sampled)
├── frames_described.json     # manifest + Claude's 1–3 sentence descriptions (paths relative)
├── features.json             # canonical feature/issue grouping: segments, quotes, key frames
├── timeline.md               # raw interleaved transcript + visuals (auditable)
├── OVERVIEW.md               # index of all features + suggested priority order
├── PRD_01_<slug>.md          # one consistent PRD per canonical feature
├── PRD_02_<slug>.md
└── ...
```

## License

MIT — see [LICENSE](LICENSE).
