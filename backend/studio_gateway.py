"""Crips AI hosted adapter for pinned MIT FluxClip; no paid providers."""
from __future__ import annotations
import gc
import json
import logging
import math
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse, parse_qs

os.environ.setdefault('OMP_NUM_THREADS', '1')
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
os.environ.setdefault('FLUXCLIP_DEVICE', 'cpu')
os.environ.setdefault('FLUXCLIP_COMPUTE_TYPE', 'int8')
os.environ.setdefault('FLUXCLIP_DATA', '/tmp/crips-studio')
os.environ.setdefault('FLUXCLIP_CACHE', '/tmp/crips-cache')
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from fluxclip.server.app import app, _project_out
from fluxclip.server import db, jobs
from fluxclip import captions
from fluxclip.transcribe import Word, Segment, Transcript
from fluxclip.utils import FluxClipError, probe

VERSION = 'crips-2026.11.16.2'
MAX_BYTES = 500_000_000
MAX_SECONDS = 3600
CHECK = {'state': 'not_run', 'version': VERSION}
log = logging.getLogger('crips')
_ORIGINAL_LIFESPAN = app.router.lifespan_context
_CREATE_LOCK = threading.Lock()
_BLOCK_UNTIL = 0.0


def command(argv, timeout=300, cwd=None):
    result = subprocess.run([str(a) for a in argv], cwd=cwd, capture_output=True,
                            text=True, timeout=timeout)
    if result.returncode:
        raise FluxClipError(result.stderr[-2500:])
    return result


def normalize_youtube(value: str) -> str:
    try:
        u = urlparse(value.strip())
        if u.scheme != 'https' or u.hostname not in {
            'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be', 'www.youtu.be'
        } or u.username or u.password or u.port:
            raise ValueError()
        p = u.path.strip('/').split('/')
        if u.hostname.endswith('youtu.be'):
            video_id = p[0] if len(p) == 1 else ''
        elif p[0] in ('live', 'shorts', 'embed') and len(p) == 2:
            video_id = p[1]
        elif u.path == '/watch':
            video_id = parse_qs(u.query).get('v', [''])[0]
        else:
            video_id = ''
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
            raise ValueError()
        return 'https://www.youtube.com/watch?v=' + video_id
    except (ValueError, TypeError):
        raise HTTPException(422, 'INVALID_YOUTUBE_URL')


def safe_error(error: str) -> str:
    text = str(error).lower()
    if 'age-restricted' in text or 'confirm your age' in text or 'age_limit' in text:
        return 'YOUTUBE_AGE_RESTRICTED'
    if 'not a bot' in text or 'sign in to confirm' in text:
        return 'YOUTUBE_VERIFICATION_REQUIRED'
    if '429' in text or 'too many requests' in text or 'youtube_rate_limited' in text:
        return 'YOUTUBE_RATE_LIMITED'
    if 'private video' in text or 'video unavailable' in text or 'not available' in text:
        return 'VIDEO_UNAVAILABLE'
    if 'model' in text or 'huggingface' in text:
        return 'TRANSCRIPTION_FAILED'
    if 'duration' in text or 'file is larger' in text or 'size limit' in text:
        return 'VIDEO_LIMIT_EXCEEDED'
    if 'timed out' in text or 'timeout' in text:
        return 'PROCESSING_TIMEOUT'
    return 'PROCESSING_FAILED'


def resolve_youtube(source, workdir=None, info_out=None):
    """Public extraction only. Stop on age/auth/rate restrictions."""
    global _BLOCK_UNTIL
    if time.time() < _BLOCK_UNTIL:
        raise FluxClipError('YOUTUBE_RATE_LIMITED')
    import yt_dlp
    folder = Path(workdir)
    folder.mkdir(parents=True, exist_ok=True)
    url = normalize_youtube(source)

    def match(info, *, incomplete=False):
        if info.get('is_live'):
            return 'Live streams are not supported'
        if (info.get('age_limit') or 0) >= 18:
            return 'age-restricted'
        if (info.get('duration') or 0) > MAX_SECONDS:
            return 'duration exceeds limit'
        return None

    def progress(data):
        total = data.get('downloaded_bytes') or 0
        if total > MAX_BYTES or sum(f.stat().st_size for f in folder.glob('*') if f.is_file()) > MAX_BYTES:
            raise FluxClipError('source size limit exceeded')

    options = {
        'outtmpl': str(folder / 'source.%(ext)s'), 'noplaylist': True,
        'format': 'bv*[height<=480]+ba/b[height<=480]', 'merge_output_format': 'mp4',
        'js_runtimes': {'node': {'path': '/usr/local/bin/node'}},
        'retries': 1, 'fragment_retries': 1, 'socket_timeout': 20,
        'concurrent_fragment_downloads': 1, 'max_filesize': MAX_BYTES,
        'quiet': True, 'noprogress': True, 'no_warnings': True,
        'match_filter': match, 'progress_hooks': [progress],
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                raise FluxClipError('VIDEO_UNAVAILABLE')
    except Exception as exc:
        code = safe_error(exc)
        if code in ('YOUTUBE_RATE_LIMITED', 'YOUTUBE_VERIFICATION_REQUIRED'):
            _BLOCK_UNTIL = time.time() + 180
        raise FluxClipError(code) from None
    files = [p for p in folder.glob('source.*') if p.suffix.lower() in {'.mp4', '.webm', '.mkv', '.mov'}]
    if not files:
        raise FluxClipError('VIDEO_UNAVAILABLE')
    target = max(files, key=lambda p: p.stat().st_size)
    if target.stat().st_size > MAX_BYTES:
        raise FluxClipError('VIDEO_LIMIT_EXCEEDED')
    if info_out is not None:
        info_out.update({'title': info.get('title', 'Video')})
    return target


def transcribe_bounded(video, model_size=None, language=None, on_progress=None, **kwargs):
    """Decode 60s at a time to bound memory on the free server."""
    from faster_whisper import WhisperModel
    vi = probe(video)
    if vi.duration > MAX_SECONDS:
        raise FluxClipError('VIDEO_LIMIT_EXCEEDED')
    if not vi.has_audio:
        return Transcript(language or 'en', vi.duration, [])
    model_path = os.environ.get('CRIPS_MODEL_PATH', '/opt/models/tiny')
    model = WhisperModel(model_path, device='cpu', compute_type='int8', cpu_threads=1, num_workers=1)
    segments = []
    detected = language
    try:
        with tempfile.TemporaryDirectory(prefix='crips-audio-') as td:
            wav = Path(td) / 'chunk.wav'
            for offset in range(0, math.ceil(vi.duration), 60):
                command(['ffmpeg', '-nostdin', '-y', '-v', 'error', '-threads', '1',
                         '-ss', str(offset), '-i', video, '-t', '60', '-vn',
                         '-ac', '1', '-ar', '16000', wav], timeout=120)
                stream, info = model.transcribe(str(wav), language=detected, word_timestamps=True,
                    beam_size=1, vad_filter=True, condition_on_previous_text=False)
                detected = detected or info.language
                for s in stream:
                    words = [Word(float(w.start) + offset, float(w.end) + offset, w.word)
                             for w in (s.words or [])]
                    if words:
                        segments.append(Segment(s.start + offset, s.end + offset, s.text, words))
                if on_progress:
                    on_progress(min(1, (offset + 60) / vi.duration))
    finally:
        del model
        gc.collect()
    return Transcript(detected or 'en', vi.duration, segments)


def render_bounded(video, start, end, output, transcript=None, opts=None, **kwargs):
    """One-thread H.264 export; whole gameplay visible; small centered captions."""
    vi = probe(video)
    start, end = max(0.0, float(start)), min(float(end), vi.duration)
    if end - start < 1:
        raise FluxClipError('Clip too short')
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    w, h = 720, 1280
    words = transcript.words_between(start, end) if transcript else []
    with tempfile.TemporaryDirectory(prefix='crips-render-') as td:
        caption_filter = ''
        if words and getattr(opts, 'caption_style', 'minimal') != 'none':
            style = getattr(opts, 'caption_style', 'minimal')
            if style not in captions.THEMES:
                style = 'minimal'
            original = captions.THEMES[style]
            captions.THEMES[style] = replace(original, size_ratio=0.038, outline_w=2)
            try:
                ass = captions.build_ass(words, start, end, w, h, theme=style,
                    font='DejaVu Sans', max_words=4, position=0.50)
            finally:
                captions.THEMES[style] = original
            (Path(td) / 'captions.ass').write_text(ass, encoding='utf-8')
            caption_filter = ',ass=captions.ass'
        vf = (f'scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,'
              f'pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30' + caption_filter)
        command(['ffmpeg', '-nostdin', '-y', '-v', 'error', '-threads', '1',
                 '-ss', str(start), '-i', Path(video).resolve(), '-t', str(end-start),
                 '-map', '0:v:0', '-map', '0:a:0?', '-vf', vf, '-filter_threads', '1',
                 '-c:v', 'libx264', '-threads', '1', '-preset', 'ultrafast', '-crf', '27',
                 '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '96k',
                 '-movflags', '+faststart', output], timeout=900, cwd=td)
    check = probe(output)
    if output.stat().st_size < 1000 or check.width != 720 or check.height != 1280:
        raise FluxClipError('Output verification failed')
    if transcript:
        output.with_suffix('.srt').write_text(captions.build_srt(words, start, end), encoding='utf-8')
    return {'path': str(output), 'start': start, 'end': end, 'duration': end-start,
            'layout': 'fit', 'size': '720x1280'}


jobs.resolve_input = resolve_youtube
jobs.transcribe = transcribe_bounded
jobs.render_clip = render_bounded

class Settings(BaseModel):
    num_clips: int = Field(15, ge=5, le=15)
    min_len: float = Field(15, ge=15, le=59)
    max_len: float = Field(60, ge=16, le=60)
    caption_style: str = 'minimal'
    @model_validator(mode='after')
    def bounds(self):
        if self.min_len >= self.max_len:
            raise ValueError('Minimum must be below maximum')
        if self.caption_style not in ('minimal', 'bold', 'boxed', 'none'):
            raise ValueError('Unknown caption style')
        return self

class Create(BaseModel):
    source: str = Field(max_length=2048)
    settings: Settings = Field(default_factory=Settings)

app.router.routes = [r for r in app.router.routes if not (
    getattr(r, 'path', '') in {'/api/settings', '/api/projects/upload'} or
    (getattr(r, 'path', '') == '/api/projects' and 'POST' in (getattr(r, 'methods', set()) or set()))
)]

@app.middleware('http')
async def access_gate(request: Request, call_next):
    public = request.url.path in {'/health', '/api/health'} or request.method == 'OPTIONS'
    if not public:
        secret = os.environ.get('CRIPS_ACCESS_KEY', '')
        key = request.headers.get('x-access-key', '')
        if not secret or not secrets.compare_digest(key.encode(), secret.encode()):
            return JSONResponse({'detail': 'ACCESS_KEY_REQUIRED'}, status_code=401,
                headers={'Access-Control-Allow-Origin': 'https://crips-ai-mobile.onrender.com'})
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

@app.get('/health')
def health_alias():
    return {'ok': True, 'version': VERSION, 'ffmpeg': bool(shutil.which('ffmpeg')),
            'node': bool(shutil.which('node')), 'selftest': CHECK['state'], 'paid_apis': False}

@app.get('/api/checks')
def checks():
    return CHECK

@app.post('/api/projects', status_code=201)
def create(body: Create):
    source = normalize_youtube(body.source)
    with _CREATE_LOCK:
        if any(p['status'] in ('queued', 'processing') for p in db.list_projects()):
            raise HTTPException(429, 'SERVER_BUSY')
        cfg = body.settings.model_dump()
        cfg.update(quality='tiny', layout='fit', aspect='9:16', headline=False)
        p = db.create_project(source, cfg, title='')
        jobs.enqueue_project(p['id'])
        return _project_out(db.get_project(p['id']))


def integration_check():
    """Real synthesized speech -> ASR -> selection -> captions -> MP4 test."""
    global CHECK
    CHECK = {'state': 'running', 'version': VERSION}
    try:
        folder = Path(tempfile.mkdtemp(prefix='crips-check-'))
        text = ('Welcome to our gaming test. The player reaches the final checkpoint. '
                'Watch the car turn around the corner, and look at that amazing finish. '
                'Here is another moment from the game. The team works together, avoids the obstacle, '
                'and wins the final round. These captions come from the speech in this test video. '
                'The final score is now on screen. Thanks for watching this demonstration.')
        (folder / 'speech.txt').write_text(text)
        command(['espeak-ng', '-s', '135', '-f', folder/'speech.txt', '-w', folder/'speech.wav'])
        command(['ffmpeg', '-nostdin', '-y', '-v', 'error', '-f', 'lavfi', '-i',
                 'testsrc2=size=640x360:rate=15', '-i', folder/'speech.wav', '-shortest',
                 '-c:v', 'libx264', '-threads', '1', '-preset', 'ultrafast',
                 '-pix_fmt', 'yuv420p', '-c:a', 'aac', folder/'source.mp4'])
        p = db.create_project('selftest:synthetic',
            {'num_clips': 1, 'min_len': 15, 'max_len': 40, 'quality': 'tiny',
             'layout': 'fit', 'aspect': '9:16', 'caption_style': 'minimal', 'headline': False},
            title='Crips AI - synthetic integration test')
        dest = db.project_dir(p['id'])/'source.mp4'
        shutil.move(str(folder/'source.mp4'), dest)
        shutil.rmtree(folder, ignore_errors=True)
        db.update_project(p['id'], source_path=str(dest))
        jobs.enqueue_project(p['id'])
        CHECK.update(project_id=p['id'])
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            now = db.get_project(p['id'])
            if now['status'] in ('done', 'failed'):
                break
            time.sleep(2)
        else:
            raise RuntimeError('SELFTEST_TIMEOUT')
        ready = [c for c in db.list_clips(p['id']) if c['status'] == 'ready']
        if now['status'] != 'done' or not ready:
            raise RuntimeError(now.get('error') or 'NO_CLIPS_RENDERED')
        clip = db.project_dir(p['id'])/ready[0]['file']
        vi = probe(clip)
        srt = clip.with_suffix('.srt')
        if not srt.is_file() or '-->' not in srt.read_text():
            raise RuntimeError('CAPTIONS_NOT_VERIFIED')
        import urllib.request
        file_path = '/files/' + p['id'] + '/' + ready[0]['file']
        req = urllib.request.Request('http://127.0.0.1:' + os.getenv('PORT', '10000') + file_path,
            headers={'X-Access-Key': os.environ.get('CRIPS_ACCESS_KEY', '')})
        with urllib.request.urlopen(req, timeout=30) as response:
            signature = response.read(64)
            if response.status != 200 or b'ftyp' not in signature:
                raise RuntimeError('DOWNLOAD_NOT_VERIFIED')
        CHECK.update(state='passed', clips=len(ready), width=vi.width, height=vi.height,
            seconds=round(vi.duration, 2), captions_verified=True, download_verified=True)
        log.warning('CRIPS_SELFTEST_PASS %s', json.dumps(CHECK))
    except Exception as exc:
        CHECK.update(state='failed', error=safe_error(str(exc)))
        log.warning('CRIPS_SELFTEST_FAILED type=%s code=%s', type(exc).__name__, CHECK['error'])

@asynccontextmanager
async def lifespan(instance):
    db.conn()
    db.set_settings({'provider': 'heuristic', 'quality': 'tiny',
                     'anthropic_api_key': None, 'openai_api_key': None})
    async with _ORIGINAL_LIFESPAN(instance):
        if os.getenv('CRIPS_SELFTEST', 'true') == 'true':
            threading.Thread(target=integration_check, daemon=True, name='crips-selftest').start()
        yield
app.router.lifespan_context = lifespan
