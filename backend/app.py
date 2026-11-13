"""Crips AI prototype backend. Only process videos you own or have rights to use."""
import ipaddress
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="Crips AI Processing Prototype")
JOBS = {}
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
MAX_DURATION = 3600
MAX_FILE_BYTES = 750_000_000

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
    folder = Path(tempfile.mkdtemp(prefix="crips_"))
    source = folder / "source.mp4"
    output = folder / "short.mp4"
    try:
        run(["yt-dlp", "--no-playlist", "--max-filesize", "750M",
             "--socket-timeout", "20", "--retries", "1", "--no-progress",
             "-f", "best[ext=mp4][height<=720]/best[height<=720]",
             "-o", str(source), url], timeout=180)
        if not source.exists() or source.stat().st_size > MAX_FILE_BYTES:
            raise RuntimeError("Video download missing or too large")
        # Fixed 9:16 crop with audio; captions are a future feature.
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-ss", str(req.start_seconds), "-i", str(source),
             "-t", str(req.duration_seconds), "-vf",
             "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
             "-c:a", "aac", "-movflags", "+faststart", str(output)], timeout=240)
        if not output.exists() or output.stat().st_size == 0:
            raise RuntimeError("No clip was generated")
        source.unlink(missing_ok=True)
        JOBS[job] = str(output)
        return {"job_id": job, "download_url": "/clips/" + job, "note": "Manual segment only; no AI highlight detection or captions yet"}
    except (subprocess.SubprocessError, RuntimeError) as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(502, "Video processing failed; verify access, permissions, format and server limits") from exc

@app.get("/clips/{job_id}")
def download_clip(job_id: str):
    path = JOBS.get(job_id)
    if not path or not Path(path).is_file():
        raise HTTPException(404, "Clip not found")
    return FileResponse(path, media_type="video/mp4", filename="crips-short.mp4")
