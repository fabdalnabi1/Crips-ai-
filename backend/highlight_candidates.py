"""Free, CPU-only gaming highlight selection: visual activity plus audio peaks."""
import json
import math
import re
import subprocess

def _run(args, timeout=180):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=True)

def candidates(video_path, count=15, clip_seconds=30):
    if not 5 <= count <= 15:
        raise ValueError("count must be between 5 and 15")
    duration = float(json.loads(_run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video_path)], 20).stdout)["format"]["duration"])
    window = max(15, min(60, int(clip_seconds)))
    if duration < window:
        return []
    limit = duration - window
    buckets = {float(t): 0.0 for t in range(0, int(limit) + 1, window)}
    if not buckets:
        buckets = {0.0: 0.0}
    # Sample one frame each second and score scene changes.
    try:
        proc = _run(["ffmpeg", "-hide_banner", "-loglevel", "info", "-i", str(video_path), "-vf", "fps=1,scale=320:-1,select='gt(scene,0.12)',metadata=print:file=-", "-an", "-f", "null", "-"], 180)
        for block in re.split(r"(?=frame:\\d+)", proc.stdout):
            time_match = re.search(r"pts_time:([0-9.]+)", block)
            score_match = re.search(r"lavfi.scene_score=([0-9.]+)", block)
            if time_match and score_match:
                t = float(time_match.group(1))
                start = min(buckets, key=lambda x: abs(x - max(0, min(limit, t - window / 2))))
                buckets[start] += float(score_match.group(1)) * 2
    except (subprocess.SubprocessError, ValueError):
        pass
    # Audio RMS levels detect intense moments even when gameplay has few scene cuts.
    try:
        proc = _run(["ffmpeg", "-hide_banner", "-loglevel", "info", "-i", str(video_path), "-vn", "-af", "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-", "-f", "null", "-"], 180)
        for block in re.split(r"(?=frame:\\d+)", proc.stdout):
            time_match = re.search(r"pts_time:([0-9.]+)", block)
            level_match = re.search(r"lavfi.astats.Overall.RMS_level=(-?[0-9.]+)", block)
            if time_match and level_match:
                t, db = float(time_match.group(1)), float(level_match.group(1))
                if math.isfinite(db):
                    start = min(buckets, key=lambda x: abs(x - max(0, min(limit, t - window / 2))))
                    buckets[start] += max(0.0, min(1.0, (db + 45) / 45)) * 0.15
    except (subprocess.SubprocessError, ValueError):
        pass
    ranked = sorted(buckets.items(), key=lambda p: (-p[1], p[0]))
    chosen = []
    for start, score in ranked:
        if all(abs(start - old["start_seconds"]) >= window for old in chosen):
            chosen.append({"start_seconds": round(start, 2), "duration_seconds": window, "activity_score": round(score, 3)})
        if len(chosen) >= count:
            break
    return sorted(chosen, key=lambda x: x["start_seconds"])
