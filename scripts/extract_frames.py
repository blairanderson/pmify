#!/usr/bin/env python3
"""
extract_frames.py — smart frame sampling for PMify screen recordings.

Combines five signals to pick high-value moments to sample, then extracts
one JPEG per chosen timestamp.

Signals:
  1. Narration boundaries — sample at the END of every Whisper segment
     (so the result of what was just described is on screen).
  2. Topic-keyword cues — when the speaker says "issue / bug / problem /
     broken / error / crash / feature / missing / confusing / annoying /
     frustrating / wish / request / improvement / idea / feedback",
     sample ~0.5s after that word (in a product interview, the screen
     usually shows the thing being complained about or requested).
  3. Action-keyword cues — when the speaker says "click / open / select /
     type / navigate / save / scroll / submit / press / choose / enter /
     paste / drag", sample ~0.7s after that word.
  4. Pause boundaries — when there's >1.5s of silence between segments,
     sample at the end of the pause (the speaker stopped to do something).
  5. Low-threshold scene change — ffmpeg `select='gt(scene,T)'` with T=0.05
     by default (tuned for UI changes, not hard cuts).

Also always emits a frame at t=0 (opening state).

Candidates get sorted, deduped within MIN_GAP (default 0.4s) and capped to
MAX_FRAMES (default 200).

Usage:
    extract_frames.py VIDEO OUT_DIR AUDIO_JSON [--scene THRESH] [--max N]
                      [--gap SECONDS]

Args:
    VIDEO          path to source video
    OUT_DIR        directory to write frame_NNNN.jpg files into
    AUDIO_JSON     Whisper output (--output_format json --word_timestamps True).
                   Pass "-" or "none" to skip narration-driven signals and
                   rely on scene change + opening frame only.
    --scene T      scene-change threshold (default 0.05; lower = more frames)
    --max N        cap on total frames extracted (default 200)
    --gap S        merge candidate timestamps within S seconds (default 0.4)

Output: OUT_DIR/frames.json manifest:
    [{"index": 1, "time": 0.000, "path": ".../frame_0001.jpg",
      "source": "opening|narration_end|topic_keyword|action_keyword|pause_end|scene_change"},
     ...]

No external Python deps. Requires ffmpeg in PATH.
"""

import json
import os
import re
import subprocess
import sys


ACTION_KEYWORDS = {
    "click", "clicks", "clicked", "clicking",
    "press", "presses", "pressed", "pressing",
    "open", "opens", "opened", "opening",
    "close", "closes", "closed", "closing",
    "navigate", "navigates", "navigated", "navigating",
    "select", "selects", "selected", "selecting",
    "type", "types", "typed", "typing",
    "enter", "enters", "entered", "entering",
    "save", "saves", "saved", "saving",
    "submit", "submits", "submitted", "submitting",
    "choose", "chooses", "chose", "choosing",
    "pick", "picks", "picked", "picking",
    "scroll", "scrolls", "scrolled", "scrolling",
    "drag", "drags", "dragged", "dragging",
    "drop", "drops", "dropped", "dropping",
    "copy", "copies", "copied", "copying",
    "paste", "pastes", "pasted", "pasting",
    "highlight", "highlights", "highlighted", "highlighting",
    "hover", "hovers", "hovered", "hovering",
    "fill", "fills", "filled", "filling",
    "upload", "uploads", "uploaded", "uploading",
    "download", "downloads", "downloaded", "downloading",
    "delete", "deletes", "deleted", "deleting",
    "search", "searches", "searched", "searching",
    "toggle", "toggles", "toggled", "toggling",
    "check", "checks", "checked", "checking",
    "uncheck",
    "tap", "taps", "tapped", "tapping",
}

# Product-interview vocabulary: when the speaker names a pain point or a
# request, the screen almost always shows the thing being discussed.
TOPIC_KEYWORDS = {
    "issue", "issues",
    "bug", "bugs", "buggy",
    "problem", "problems",
    "broken", "breaks",
    "error", "errors",
    "crash", "crashes", "crashed", "crashing",
    "feature", "features",
    "missing",
    "confusing", "confused",
    "annoying", "annoys",
    "frustrating", "frustrated",
    "wish", "wishes",
    "request", "requests", "requested",
    "improvement", "improvements", "improve",
    "idea", "ideas",
    "feedback",
    "pain",
}

ACTION_OFFSET = 0.7      # seconds after action keyword timestamp
TOPIC_OFFSET = 0.5       # seconds after topic keyword timestamp
PAUSE_THRESHOLD = 1.5    # silence longer than this triggers a sample
END_OFFSET = 0.3         # sample this many seconds after a segment ends

# Source-priority order used by both dedupe() and cap(). Keep the two call
# sites consistent — see CLAUDE.md.
SOURCE_PRIORITY = {
    "opening": 0,
    "narration_end": 5,
    "topic_keyword": 4,
    "action_keyword": 3,
    "pause_end": 2,
    "scene_change": 1,
}


def usage():
    sys.stderr.write(__doc__)
    sys.exit(2)


def parse_args():
    if len(sys.argv) < 4 or sys.argv[1] in ("-h", "--help"):
        usage()
    video = sys.argv[1]
    out_dir = sys.argv[2]
    audio_json = sys.argv[3]

    scene_thresh = 0.05
    max_frames = 200
    min_gap = 0.4
    i = 4
    while i < len(sys.argv):
        flag = sys.argv[i]
        val = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
        if flag == "--scene":
            scene_thresh = float(val); i += 2
        elif flag == "--max":
            max_frames = int(val); i += 2
        elif flag == "--gap":
            min_gap = float(val); i += 2
        else:
            sys.stderr.write(f"unknown flag: {flag}\n")
            usage()
    return video, out_dir, audio_json, scene_thresh, max_frames, min_gap


def probe_duration(video: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", video],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


def candidates_from_audio(audio_json_path: str):
    """Yield (time, source) tuples from Whisper JSON."""
    if audio_json_path in ("-", "none", ""):
        return []
    if not os.path.isfile(audio_json_path):
        sys.stderr.write(f"warn: audio json not found, skipping: {audio_json_path}\n")
        return []

    with open(audio_json_path) as f:
        data = json.load(f)

    cands = []
    segments = data.get("segments", [])

    # 1. End-of-segment samples
    for seg in segments:
        end = float(seg.get("end", 0.0))
        cands.append((end + END_OFFSET, "narration_end"))

    # 2. Topic + action keyword cues (need word-level timestamps)
    for seg in segments:
        for w in seg.get("words", []) or []:
            token = re.sub(r"[^a-z]", "", (w.get("word") or "").lower())
            t = float(w.get("end", w.get("start", 0.0)))
            if token in TOPIC_KEYWORDS:
                cands.append((t + TOPIC_OFFSET, "topic_keyword"))
            elif token in ACTION_KEYWORDS:
                cands.append((t + ACTION_OFFSET, "action_keyword"))

    # 3. Pause endpoints
    for prev, nxt in zip(segments, segments[1:]):
        gap = float(nxt.get("start", 0.0)) - float(prev.get("end", 0.0))
        if gap >= PAUSE_THRESHOLD:
            cands.append((float(nxt.get("start", 0.0)) - 0.1, "pause_end"))

    return cands


def candidates_from_scene(video: str, threshold: float):
    """Use ffmpeg select+showinfo to enumerate scene-change timestamps."""
    proc = subprocess.run(
        ["ffmpeg", "-i", video,
         "-vf", f"select='gt(scene,{threshold})',showinfo",
         "-vsync", "vfr", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    cands = []
    for line in proc.stderr.splitlines():
        m = re.search(r"pts_time:([0-9.]+)", line)
        if m:
            cands.append((float(m.group(1)), "scene_change"))
    return cands


def dedupe(cands, min_gap: float):
    """Sort by time, then drop any candidate within min_gap of the previous one.

    When collapsing, we keep the highest-priority source label (see
    SOURCE_PRIORITY) for each surviving timestamp.
    """
    cands = sorted(cands, key=lambda c: c[0])
    kept = []
    for t, src in cands:
        if kept and (t - kept[-1][0]) < min_gap:
            if SOURCE_PRIORITY[src] > SOURCE_PRIORITY[kept[-1][1]]:
                kept[-1] = (kept[-1][0], src)
            continue
        kept.append((t, src))
    return kept


def cap(cands, max_frames: int):
    """If we have more than max_frames candidates, keep the highest-priority
    ones first, then evenly spaced fills."""
    if len(cands) <= max_frames:
        return cands
    # Keep top max_frames by priority, then re-sort by time.
    ranked = sorted(cands, key=lambda c: (-SOURCE_PRIORITY[c[1]], c[0]))[:max_frames]
    return sorted(ranked, key=lambda c: c[0])


def extract(video: str, out_dir: str, timestamps):
    """Run ffmpeg once per timestamp (fast seek). Returns the list of
    successfully extracted (index, time, path) triples."""
    results = []
    for i, (t, _src) in enumerate(timestamps, start=1):
        path = os.path.join(out_dir, f"frame_{i:04d}.jpg")
        rc = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-ss", f"{max(t, 0):.3f}",
             "-i", video,
             "-frames:v", "1", "-q:v", "3",
             path],
        ).returncode
        if rc == 0 and os.path.isfile(path):
            results.append((i, t, path))
    return results


def main():
    video, out_dir, audio_json, scene_thresh, max_frames, min_gap = parse_args()

    if not os.path.isfile(video):
        sys.stderr.write(f"error: video not found: {video}\n")
        sys.exit(1)

    os.makedirs(out_dir, exist_ok=True)

    duration = probe_duration(video)

    cands = [(0.0, "opening")]
    cands += candidates_from_audio(audio_json)
    cands += candidates_from_scene(video, scene_thresh)

    cands = [(t, s) for t, s in cands if 0.0 <= t <= max(duration - 0.05, 0.0)]
    cands = dedupe(cands, min_gap)
    cands = cap(cands, max_frames)

    extracted = extract(video, out_dir, cands)

    src_lookup = {round(t, 4): s for t, s in cands}
    manifest = []
    for new_idx, (_orig_idx, t, path) in enumerate(extracted, start=1):
        manifest.append({
            "index": new_idx,
            "time": round(t, 3),
            "path": path,
            "source": src_lookup.get(round(t, 4), "unknown"),
        })

    out_json = os.path.join(out_dir, "frames.json")
    with open(out_json, "w") as f:
        json.dump(manifest, f, indent=2)

    by_source = {}
    for m in manifest:
        by_source[m["source"]] = by_source.get(m["source"], 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(by_source.items()))
    sys.stderr.write(
        f"pmify/extract_frames: wrote {len(manifest)} frames "
        f"(scene_thresh={scene_thresh}, max={max_frames}, gap={min_gap}s) "
        f"[{summary}] -> {out_json}\n"
    )
    print(out_json)


if __name__ == "__main__":
    main()
