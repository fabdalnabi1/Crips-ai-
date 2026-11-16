"""Verified failure handling; deliberately not a YouTube access-control bypass.

Installed on the pinned FluxClip gateway. Keeps error codes stable, backs off
on server verification/rate limits, and verifies exports before claiming done.
No cookies, account credentials, proxies, CAPTCHA solvers or paid providers.
"""
import math
import re
import threading
import time
from pathlib import Path

VERSION = 'crips-reliability-2026.11.16.3'
CODES = (
    'YOUTUBE_AGE_RESTRICTED', 'YOUTUBE_VERIFICATION_REQUIRED',
    'YOUTUBE_RATE_LIMITED', 'VIDEO_UNAVAILABLE', 'VIDEO_LIMIT_EXCEEDED',
    'TRANSCRIPTION_FAILED', 'PROCESSING_TIMEOUT', 'INVALID_YOUTUBE_URL',
    'SERVER_BUSY', 'ACCESS_KEY_REQUIRED', 'NO_USABLE_CLIPS',
    'OUTPUT_VERIFICATION_FAILED', 'PROCESSING_FAILED',
)


def classify_error(error):
    """Idempotent: classifying a classified error never loses its meaning."""
    text = str(error)
    upper = text.upper()
    for code in CODES:
        if re.search(r'(?<![A-Z_])' + re.escape(code) + r'(?![A-Z_])', upper):
            return code
    low = text.lower()
    if any(s in low for s in ('age-restricted', 'age restricted', 'confirm your age', 'age_limit')):
        return 'YOUTUBE_AGE_RESTRICTED'
    if '429' in low or 'too many requests' in low:
        return 'YOUTUBE_RATE_LIMITED'
    if 'not a bot' in low or 'sign in to confirm' in low:
        return 'YOUTUBE_VERIFICATION_REQUIRED'
    if any(s in low for s in ('private video', 'video unavailable', 'not available')):
        return 'VIDEO_UNAVAILABLE'
    if any(s in low for s in ('metadata_errors', 'huggingface', 'whisper model')):
        return 'TRANSCRIPTION_FAILED'
    if any(s in low for s in ('duration exceeds', 'file is larger', 'size limit', 'exceeds limit')):
        return 'VIDEO_LIMIT_EXCEEDED'
    if 'timed out' in low or 'timeout' in low:
        return 'PROCESSING_TIMEOUT'
    return 'PROCESSING_FAILED'


class BlockedImport(RuntimeError):
    def __init__(self, code, retry_after):
        super().__init__(code)
        self.code = code
        self.retry_after = max(1, int(math.ceil(retry_after)))


class ImportBackoff:
    """Bounded per-instance backoff, not a workaround for access restrictions."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.global_block = None
        self.source_blocks = {}
        self.last = {'state': 'not_tested'}

    def check(self, source):
        with self.lock:
            now = self.clock()
            for key, (_, expires) in list(self.source_blocks.items()):
                if expires <= now:
                    del self.source_blocks[key]
            if self.global_block and self.global_block[1] <= now:
                self.global_block = None
            item = self.source_blocks.get(source) or self.global_block
            if item:
                raise BlockedImport(item[0], item[1] - now)

    def failed(self, source, error):
        code = classify_error(error)
        with self.lock:
            now = self.clock()
            self.last = {'state': 'blocked' if code.startswith('YOUTUBE_') else 'failed', 'code': code}
            if code in ('YOUTUBE_RATE_LIMITED', 'YOUTUBE_VERIFICATION_REQUIRED'):
                self.global_block = (code, now + 300)
            elif code in ('YOUTUBE_AGE_RESTRICTED', 'VIDEO_UNAVAILABLE'):
                if len(self.source_blocks) >= 128:
                    del self.source_blocks[next(iter(self.source_blocks))]
                self.source_blocks[source] = (code, now + 900)
        return code

    def passed(self):
        with self.lock:
            self.last = {'state': 'passed', 'scope': 'one imported video, not all links'}

    def snapshot(self):
        with self.lock:
            out = dict(self.last)
            out['retry_after'] = max(0, int(math.ceil(self.global_block[1] - self.clock()))) if self.global_block else 0
            return out


def verify_project_exports(pid, db, probe):
    """Use actual files and ffprobe, never just a 'ready' database string."""
    root = Path(db.project_dir(pid)).resolve()
    ready = 0
    rows = db.list_clips(pid)
    for clip in rows:
        if clip.get('status') != 'ready':
            continue
        try:
            rel = clip.get('file')
            if not isinstance(rel, str) or not rel:
                raise ValueError('Missing output')
            path = (root / rel).resolve()
            if root not in path.parents or path.suffix.lower() != '.mp4':
                raise ValueError('Invalid output path')
            if not path.is_file() or path.stat().st_size < 1000:
                raise ValueError('Missing or empty media')
            info = probe(path)
            if not math.isfinite(info.duration) or not 1 <= info.duration <= 61:
                raise ValueError('Invalid output duration')
            if (info.width, info.height) != (720, 1280):
                raise ValueError('Unexpected dimensions')
        except Exception:
            db.update_clip(clip['id'], status='failed', file=None, error='OUTPUT_VERIFICATION_FAILED')
        else:
            ready += 1
    return ready, len(rows)


def completion_fields(pid, fields, db, probe):
    """No success state until at least one real playable output is verified."""
    out = dict(fields)
    if out.get('status') != 'done':
        if out.get('status') == 'failed' and out.get('error'):
            out['error'] = classify_error(out['error'])
        return out
    ready, total = verify_project_exports(pid, db, probe)
    if not ready:
        out.update(status='failed', stage='rendering', progress=0.0,
                   error='PROCESSING_FAILED', message='No verified MP4 clips were produced.')
    else:
        message = f'{ready} verified MP4 clips ready'
        if ready < total:
            message += f'; {total-ready} clips failed'
        out.update(message=message, error=None)
    return out


def install(app):
    """Patch only the adapter's own service, never the upstream project."""
    if getattr(app.state, 'crips_reliability_installed', False):
        return
    from backend import studio_gateway as gateway
    from fastapi import HTTPException
    db, jobs, probe = gateway.db, gateway.jobs, gateway.probe
    backoff = ImportBackoff()
    previous_resolve = jobs.resolve_input
    previous_set = jobs._set
    gateway.safe_error = classify_error

    def resolve(source, workdir=None, info_out=None):
        canonical = gateway.normalize_youtube(source)
        try:
            backoff.check(canonical)
            path = previous_resolve(canonical, workdir=workdir, info_out=info_out)
        except BlockedImport as exc:
            raise gateway.FluxClipError(exc.code) from None
        except Exception as exc:
            raise gateway.FluxClipError(backoff.failed(canonical, exc)) from None
        backoff.passed()
        return path

    def checked_set(pid, **fields):
        return previous_set(pid, **completion_fields(pid, fields, db, probe))

    jobs.resolve_input = resolve
    jobs._set = checked_set
    # Reject known-blocked repeated submissions before creating another job.
    app.router.routes = [route for route in app.router.routes if not (
        getattr(route, 'path', '') == '/api/projects'
        and 'POST' in (getattr(route, 'methods', set()) or set()))]

    def create_checked(body: gateway.Create):
        canonical = gateway.normalize_youtube(body.source)
        try:
            backoff.check(canonical)
        except BlockedImport as exc:
            status = 409 if exc.code == 'YOUTUBE_AGE_RESTRICTED' else 429
            raise HTTPException(status, exc.code, headers={'Retry-After': str(exc.retry_after)}) from None
        return gateway.create(body)

    app.add_api_route('/api/projects', create_checked, methods=['POST'], status_code=201)

    @app.get('/api/readiness')
    def readiness():
        return {
            'version': VERSION,
            'engine_test': gateway.CHECK.get('state', 'not_run'),
            'youtube_import': backoff.snapshot(),
            'age_restricted_import': 'not_supported',
            'all_youtube_links_verified': False,
            'paid_apis': False,
        }
    app.state.crips_reliability_installed = True
