# Crips AI processing prototype

This is **backend source code, not a deployed service**. It supports one manually timed vertical clip from a permitted YouTube video, using yt-dlp and FFmpeg. It does **not** automatically select highlights or add subtitles/captions yet.

## Requirements
Python 3.11+, ffmpeg, yt-dlp, and backend/requirements.txt.

## Run locally
```sh
pip install -r backend/requirements.txt
uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

## API
`GET /health` checks dependencies.
`POST /clips` accepts `{"url":"https://youtu.be/VIDEO_ID","start_seconds":0,"duration_seconds":25}` and returns a download path.
`GET /clips/{job_id}` downloads the output.

## Not production-ready
The API has no authentication, queue, persistence, automatic deletion, usage quotas, rate limits, background workers, caption renderer, or AI highlight selection. Do not expose it publicly before adding these protections. The current GitHub Pages UI does not call this backend.

Video retrieval may be blocked by YouTube or by hosting policies. Only process media you own or are authorized to use. Avoid storing private video URLs in public logs.
