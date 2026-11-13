"""Crips AI prototype backend. Only process videos you own or have rights to use."""
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import threading
import time
import secrets
import zipfile
import logging
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from backend.highlight_candidates import candidates

app = FastAPI(title="Crips AI Processing Prototype")
JOBS = {}
BATCHES = {}
logger = logging.getLogger("crips")
app.add_middleware(CORSMiddleware, allow_origins=["https://fabdalnabi1.github.io"], allow_methods=["GET", "POST"], allow_headers=["Content-Type", "X-Access-Key"])
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
DRIVE_HOSTS = {"drive.google.com", "www.drive.google.com"}
DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]{10,100}$")
ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
MAX_DURATION = 3600
MAX_FILE_BYTES = 150_000_000
PROCESSING_LOCK = threading.BoundedSemaphore(1)
JOB_TTL_SECONDS = 3600

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

def validate_source_url(value: str) -> tuple[str, str]:
    u = urlparse(value)
    if u.scheme == "https" and u.hostname in DRIVE_HOSTS and not u.username and not u.password and not u.port:
        parts = u.path.strip("/").split("/")
        file_id = parts[2] if len(parts) >= 3 and parts[:2] == ["file", "d"] else parse_qs(u.query).get("id", [""])[0] if parts[0] == "uc" else ""
        if not DRIVE_ID.fullmatch(file_id):
            raise HTTPException(400, "Use a public Google Drive file sharing link")
        return "drive", file_id
    return "youtube", validate_youtube_url(value)

def fetch_source(value: str, target: Path):
    kind, identifier = validate_source_url(value)
    if kind == "drive":
        run(["python", "-m", "gdown", "--id", identifier, "-O", str(target)], timeout=180)
    else:
        run(["yt-dlp", "--js-runtimes", "node", "--no-playlist", "--max-filesize", "150M",
             "--socket-timeout", "20", "--retries", "1", "--no-progress",
             "-f", "best[ext=mp4][height<=720]/best[height<=720]",
             "-o", str(target), identifier], timeout=180)
    if not target.is_file() or target.stat().st_size > MAX_FILE_BYTES:
        raise RuntimeError("Source missing or exceeds 150 MB")
    with target.open("rb") as f:
        header = f.read(4096)
    if b"ftyp" not in header[:64]:
        raise RuntimeError("File is not a supported MP4 video")

def run(command, timeout=180):
    return subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)

@app.get("/health")
def health():
    return {"status": "ok", "ffmpeg": bool(shutil.which("ffmpeg")), "yt_dlp": bool(shutil.which("yt-dlp"))}

def authorize(key):
    secret = os.getenv("CRIPS_ACCESS_KEY", "")
    if os.getenv("ENABLE_PROCESSING") != "true" or not secret:
        raise HTTPException(503, "Processing is not enabled")
    if not secrets.compare_digest(key or "", secret):
        raise HTTPException(401, "Invalid access key")

def generate_batch(job, req, uploaded_path=None):
    folder = Path(tempfile.mkdtemp(prefix="crips_batch_"))
    try:
        BATCHES[job]["state"] = "downloading"
        source = folder / "source.mp4"
        if uploaded_path:
            shutil.move(str(uploaded_path), str(source))
        else:
            fetch_source(req.url, source)
        if not source.exists() or source.stat().st_size > MAX_FILE_BYTES:
            raise RuntimeError("Source is unavailable or too large")
        BATCHES[job]["state"] = "analyzing"
        segments = candidates(source, req.count, req.duration_seconds)
        BATCHES[job]["segments"] = segments
        if not segments:
            raise RuntimeError("No eligible highlights")
        archive = folder / "crips-reels.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as z:
            for i, seg in enumerate(segments, 1):
                BATCHES[job]["state"] = "rendering"
                BATCHES[job]["progress"] = i - 1
                clip = folder / ("reel-%02d.mp4" % i)
                run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                     "-ss", str(seg["start_seconds"]), "-i", str(source),
                     "-t", str(seg["duration_seconds"]), "-vf",
                     "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280",
                     "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                     "-c:a", "aac", "-movflags", "+faststart", str(clip)], timeout=180)
                z.write(clip, clip.name)
                BATCHES[job]["progress"] = i
        source.unlink(missing_ok=True)
        BATCHES[job].update(state="ready", path=str(archive), count=len(segments))
    except Exception as exc:
        logger.exception("Batch %s failed at stage %s; extractor stderr: %.1600s", job, BATCHES[job].get("state"), (exc.stderr or b"").decode("utf-8", errors="replace") if isinstance(exc, subprocess.CalledProcessError) else "")
        BATCHES[job]["state"] = "failed"
        if isinstance(exc, subprocess.CalledProcessError):
            details = (exc.stderr or b"").decode("utf-8", errors="replace").lower()
            if "sign in" in details or "bot" in details or "cookies" in details:
                reason = "YouTube blocked this server with an anti-bot verification. This cannot be resolved by changing your access key. Use the MP4 upload option for a video you have permission to process."
            elif "requested format" in details:
                reason = "YouTube did not offer a compatible video format."
            elif "file is larger" in details or "max-filesize" in details:
                reason = "This video exceeds the 150 MB free-server download limit."
            elif "not available" in details or "private" in details:
                reason = "This video is unavailable or restricted to this server."
            else:
                reason = "Video processing failed at " + BATCHES[job].get("state", "unknown") + ". Check server logs."
        elif isinstance(exc, subprocess.TimeoutExpired):
            reason = "The free server timed out during " + BATCHES[job].get("state", "processing") + "."
        else:
            reason = "Video processing failed at " + BATCHES[job].get("state", "unknown") + ". Check server logs."
        BATCHES[job]["error"] = reason
        shutil.rmtree(folder, ignore_errors=True)
    finally:
        if uploaded_path:
            Path(uploaded_path).unlink(missing_ok=True)
        PROCESSING_LOCK.release()

@app.post("/generate")
def generate(req: HighlightRequest, x_access_key: str = Header(default="")):
    authorize(x_access_key)
    validate_source_url(req.url)
    if not PROCESSING_LOCK.acquire(blocking=False):
        raise HTTPException(429, "Server busy. Try later.")
    for old, data in list(BATCHES.items()):
        if time.time() - data["created"] > JOB_TTL_SECONDS:
            if data.get("path"):
                shutil.rmtree(Path(data["path"]).parent, ignore_errors=True)
            BATCHES.pop(old, None)
    job = uuid.uuid4().hex
    BATCHES[job] = {"created": time.time(), "state": "queued", "progress": 0, "count": req.count}
    threading.Thread(target=generate_batch, args=(job, req), daemon=True).start()
    return {"job_id": job}

@app.post("/generate-upload")
async def generate_upload(request: Request, x_access_key: str = Header(default="")):
    authorize(x_access_key)
    try:
        count = int(request.query_params.get("count", "5"))
        duration = int(request.query_params.get("duration_seconds", "30"))
        req = HighlightRequest(url="uploaded", count=count, duration_seconds=duration)
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid clip settings")
    if not PROCESSING_LOCK.acquire(blocking=False):
        raise HTTPException(429, "Server busy. Try later.")
    fd, temp_name = tempfile.mkstemp(prefix="crips_upload_", suffix=".mp4")
    size = 0
    try:
        with os.fdopen(fd, "wb") as output:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise HTTPException(413, "Video exceeds 150 MB")
                output.write(chunk)
        with open(temp_name, "rb") as source:
            if b"ftyp" not in source.read(64):
                raise HTTPException(400, "Only MP4 videos are supported")
        job = uuid.uuid4().hex
        BATCHES[job] = {"created": time.time(), "state": "queued", "progress": 0, "count": count}
        threading.Thread(target=generate_batch, args=(job, req, temp_name), daemon=True).start()
        return {"job_id": job}
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        PROCESSING_LOCK.release()
        raise

@app.get("/generate/{job}")
def generate_status(job: str, x_access_key: str = Header(default="")):
    authorize(x_access_key)
    data = BATCHES.get(job)
    if not data or time.time() - data["created"] > JOB_TTL_SECONDS:
        raise HTTPException(404, "Job expired")
    return {"state": data["state"], "progress": data["progress"], "count": data["count"], "error": data.get("error"), "segments": data.get("segments", []) if data["state"] in ("rendering", "ready") else []}

@app.post("/generate/{job}/download")
def generate_download(job: str, x_access_key: str = Header(default="")):
    authorize(x_access_key)
    data = BATCHES.get(job)
    if not data or data["state"] != "ready" or time.time() - data["created"] > JOB_TTL_SECONDS:
        raise HTTPException(404, "Download unavailable")
    return FileResponse(data["path"], media_type="application/zip", filename="crips-reels.zip")

@app.post("/generate/{job}/clip/{number}")
def download_single_clip(job: str, number: int, x_access_key: str = Header(default="")):
    authorize(x_access_key)
    data = BATCHES.get(job)
    if not data or data["state"] != "ready" or time.time() - data["created"] > JOB_TTL_SECONDS:
        raise HTTPException(404, "Clip unavailable")
    if number < 1 or number > data["count"]:
        raise HTTPException(404, "Invalid clip number")
    path = Path(data["path"]).parent / ("reel-%02d.mp4" % number)
    if not path.is_file():
        raise HTTPException(404, "Clip file missing")
    return FileResponse(str(path), media_type="video/mp4", filename=path.name)

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
        run(["yt-dlp", "--js-runtimes", "node", "--no-playlist", "--max-filesize", "150M",
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
        run(["yt-dlp", "--js-runtimes", "node", "--no-playlist", "--max-filesize", "150M",
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
