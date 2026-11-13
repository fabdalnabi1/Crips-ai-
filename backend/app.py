"""Crips AI prototype backend. Only process videos you own or have rights to use."""
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from backend.highlight_candidates import candidates

app = FastAPI(title="Crips AI Processing Prototype")
JOBS = {}
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
MAX_DURATION = 3600
MAX_FILE_BYTES = 150_000_000
PROCESSING_LOCK = threading.BoundedSemaphore(1)
JOB_TTL_SECONDS = 600

class HighlightRequest(BaseModel):
    url: str
    count: int = Field(default=15, ge=5, le=15)
    duration_seconds: int = Field(default=30, ge=15, le=60)

class ClipRequest(BaseModel):
    url: str
    caption: str = ""
    start_seconds: float = 0
    duration_seconds: float = 25

def validate_youtube_url(value: str) -> str:
    u = urlparse(value)
    if u.scheme != "https" or u.hostname not in ALLOWED_HOSTS or u.username or u.password or u.port:
        raise HTTPException(400, "Only HTTPS YouTube video links are accepted")
    parts = u.path.strip("/").split("/")
    if u.hostname in ("youtu.be", "www.youtu.be"):
        video_id = parts[0]
    elif parts[0] in ("live", "shorts", "embed") and len(parts) > 1:
        video_id = parts[1]
    else:
        video_id = parse_qs(u.query).get("v", [""])[0]
    if not VIDEO_ID.fullmatch(video_id):
        raise HTTPException(400, "Invalid YouTube video ID")
    return "https://www.youtube.com/watch?v=" + video_id

def run(command, timeout=180):
    return subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)

@app.get("/health")
def health():
    return {"status": "ok", "ffmpeg": bool(shutil.which("ffmpeg")), "yt_dlp": bool(shutil.which("yt-dlp"))}

@app.post("/highlights/preview")
def preview_highlights(req: HighlightRequest):
    """Restricted proof of concept: returns visual activity candidate timestamps, not finished reels."""
    if os.getenv("ENABLE_PROCESSING") != "true":
        raise HTTPException(503, "Processing disabled until access controls and limits are configured")
    if not PROCESSING_LOCK.acquire(blocking=False):
        raise HTTPException(429, "Server is busy")
    folder = Path(tempfile.mkdtemp(prefix="crips_preview_"))
    try:
        url = validate_youtube_url(req.url)
        source = folder / "source.mp4"
        run(["yt-dlp", "--no-playlist", "--max-filesize", "150M",
             "--socket-timeout", "20", "--retries", "1", "--no-progress",
             "-f", "best[ext=mp4][height<=720]/best[height<=720]",
             "-o", str(source), url], timeout=180)
        if not source.exists() or source.stat().st_size > MAX_FILE_BYTES:
            raise RuntimeError("Video missing or exceeds size limit")
        segments = candidates(source, req.count, req.duration_seconds)
        return {"candidates": segments, "count": len(segments),
                "method": "visual scene activity heuristic (not semantic AI)",
                "note": "Preview only; no MP4 clips generated"}
    except (subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        raise HTTPException(502, "Unable to analyze this video") from exc
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        PROCESSING_LOCK.release()

@app.post("/clips")
def create_clip(req: ClipRequest):
    """Synchronous prototype: one manually selected segment, not automatic AI highlights."""
    if os.getenv("ENABLE_PROCESSING") != "true":
        raise HTTPException(503, "Processing disabled until access controls and limits are configured")
    if not (0 <= req.start_seconds <= MAX_DURATION) or not (3 <= req.duration_seconds <= 60):
        raise HTTPException(400, "Start must be 0-3600 seconds; duration 3-60 seconds")
    if len(req.caption) > 100:
        raise HTTPException(400, "Caption too long")
    if not shutil.which("ffmpeg") or not shutil.which("yt-dlp"):
        raise HTTPException(503, "FFmpeg and yt-dlp must be installed on the processing server")
    url = validate_youtube_url(req.url)
    job = uuid.uuid4().hex
    if not PROCESSING_LOCK.acquire(blocking=False):
        raise HTTPException(429, "Server is busy. Try again later.")
    for old_id, old in list(JOBS.items()):
        if time.time() - old["created"] > JOB_TTL_SECONDS:
            shutil.rmtree(Path(old["path"]).parent, ignore_errors=True)
            JOBS.pop(old_id, None)
    folder = Path(tempfile.mkdtemp(prefix="crips_"))
    source = folder / "source.mp4"
    output = folder / "short.mp4"
    try:
        run(["yt-dlp", "--no-playlist", "--max-filesize", "150M",
             "--socket-timeout", "20", "--retries", "1", "--no-progress",
             "-f", "best[ext=mp4][height<=720]/best[height<=720]",
             "-o", str(source), url], timeout=180)
        if not source.exists() or source.stat().st_size > MAX_FILE_BYTES:
            raise RuntimeError("Video download missing or too large")
        # 9:16 crop; optional small centered user-supplied caption.
        vf = "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280"
        if req.caption.strip():
            caption_file = folder / "caption.txt"
            caption_file.write_text(req.caption.strip(), encoding="utf-8")
            vf += ",drawtext=textfile=" + str(caption_file) + ":fontcolor=white:fontsize=27:borderw=3:bordercolor=black:x=(w-text_w)/2:y=(h-text_h)/2"
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-ss", str(req.start_seconds), "-i", str(source),
             "-t", str(req.duration_seconds), "-vf",
             vf,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
             "-c:a", "aac", "-movflags", "+faststart", str(output)], timeout=240)
        if not output.exists() or output.stat().st_size == 0:
            raise RuntimeError("No clip was generated")
        source.unlink(missing_ok=True)
        JOBS[job] = {"path": str(output), "created": time.time()}
        return {"job_id": job, "download_url": "/clips/" + job, "note": "Manual segment with optional caption; automatic highlights not yet available"}
    except (subprocess.SubprocessError, RuntimeError) as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(502, "Video processing failed; verify access, permissions, format and server limits") from exc
    finally:
        PROCESSING_LOCK.release()

@app.get("/clips/{job_id}")
def download_clip(job_id: str):
    item = JOBS.get(job_id)
    if not item or time.time() - item["created"] > JOB_TTL_SECONDS or not Path(item["path"]).is_file():
        raise HTTPException(404, "Clip not found")
    return FileResponse(item["path"], media_type="video/mp4", filename="crips-short.mp4")
