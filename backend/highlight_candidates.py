"""Heuristic highlight candidates for authorized gaming footage.
Not an AI model: detects visual cuts, scores activity, avoids overlap.
"""
import json
import subprocess

def candidates(video_path, count=15, clip_seconds=30):
    if not 5 <= count <= 15:
        raise ValueError("count must be between 5 and 15")
    probe = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","json",str(video_path)],capture_output=True,text=True,check=True,timeout=20)
    duration = float(json.loads(probe.stdout)["format"]["duration"])
    if duration < 15:
        return []
    # Low-resolution scene score from FFmpeg's scene detector.
    proc = subprocess.run(["ffmpeg","-hide_banner","-i",str(video_path),"-vf","fps=1,scale=320:-1,select='gt(scene,0.15)',metadata=print:file=-","-an","-f","null","-"],capture_output=True,text=True,timeout=180)
    import re
    events=[]
    for match in re.finditer(r"pts_time:([0-9.]+).*?lavfi.scene_score=([0-9.]+)",proc.stdout,re.S):
        events.append((float(match.group(1)),float(match.group(2))))
    # Bin cuts into 30-second windows; high visual activity is only a proxy for excitement.
    window=max(15,min(60,int(clip_seconds)))
    buckets={}
    for t,score in events:
        start=max(0,min(duration-window,round(t/window)*window))
        buckets[start]=buckets.get(start,0)+score
    if not buckets:
        buckets={float(t):0 for t in range(0,max(1,int(duration-window)+1),window)}
    ranked=sorted(buckets.items(),key=lambda p:p[1],reverse=True)
    chosen=[]
    for start,score in ranked:
        if all(abs(start-old["start_seconds"])>=window for old in chosen):
            chosen.append({"start_seconds":round(start,2),"duration_seconds":min(window,round(duration-start,2)),"activity_score":round(score,3)})
        if len(chosen)>=count:
            break
    return sorted(chosen,key=lambda x:x["start_seconds"])
