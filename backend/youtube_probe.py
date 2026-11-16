"""Opt-in, bounded public-source import check. No access-control bypass."""
from __future__ import annotations
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path

SAMPLE = 'https://www.youtube.com/watch?v=PVGeM40dABA'
ATTRIBUTION = '(CC BY 4.0) Blender Foundation | studio.blender.org | Coffee Run'
LICENSE = 'https://studio.blender.org/projects/coffee-run/pages/licensing/'
STATE = {'state': 'not_run', 'test': 'public_youtube_import'}
log = logging.getLogger('crips.import_check')


def classify(message: str) -> str:
    text = str(message).lower()
    if 'age-restricted' in text or 'confirm your age' in text:
        return 'YOUTUBE_AGE_RESTRICTED'
    if 'not a bot' in text or 'sign in to confirm' in text:
        return 'YOUTUBE_VERIFICATION_REQUIRED'
    if '429' in text or 'too many requests' in text:
        return 'YOUTUBE_RATE_LIMITED'
    if 'private video' in text or 'unavailable' in text:
        return 'VIDEO_UNAVAILABLE'
    if 'requested format' in text:
        return 'FORMAT_UNAVAILABLE'
    if 'larger than' in text:
        return 'TEST_SIZE_LIMIT'
    return 'IMPORT_TEST_FAILED'


def check_source() -> dict:
    with tempfile.TemporaryDirectory(prefix='crips-public-probe-') as tmp:
        cmd = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist',
               '--js-runtimes', 'node', '--no-progress', '--no-warnings',
               '--socket-timeout', '15', '--retries', '0', '--fragment-retries', '0',
               '--max-filesize', '64M', '--format', 'bv*[height<=360]+ba/b[height<=360]',
               '--merge-output-format', 'mp4',
               '--match-filters', '!is_live & age_limit<18 & duration<=600',
               '--output', str(Path(tmp) / 'source.%(ext)s'), SAMPLE]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            return {'state': 'failed', 'code': 'IMPORT_TEST_TIMEOUT'}
        if proc.returncode:
            code = classify(proc.stderr)
            return {'state': 'blocked' if code.startswith('YOUTUBE_') else 'failed', 'code': code}
        files = [f for f in Path(tmp).glob('source.*')
                 if f.suffix in {'.mp4', '.webm', '.mkv'} and f.is_file()]
        if not files:
            return {'state': 'failed', 'code': 'NO_MEDIA_RECEIVED'}
        media = max(files, key=lambda f: f.stat().st_size)
        try:
            checked = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                'format=duration:stream=codec_type,width,height', '-of', 'json',
                str(media)], capture_output=True, text=True, timeout=15, check=True)
            data = json.loads(checked.stdout)
            video = next(s for s in data['streams'] if s['codec_type'] == 'video')
        except (subprocess.SubprocessError, ValueError, KeyError, StopIteration):
            return {'state': 'failed', 'code': 'DOWNLOADED_MEDIA_INVALID'}
        return {'state': 'passed', 'video_id': 'PVGeM40dABA',
                'bytes': media.stat().st_size, 'width': video['width'],
                'height': video['height'], 'seconds': float(data['format']['duration']),
                'attribution': ATTRIBUTION, 'license': LICENSE,
                'scope': 'public download only, not age-restricted or AI clipping'}


def install(app) -> None:
    """Disabled by default. An authorized operator explicitly enables diagnostics."""
    previous = app.router.lifespan_context

    def work():
        STATE['state'] = 'running'
        try:
            STATE.update(check_source())
        except Exception as exc:
            STATE.update(state='failed', code='PROBE_EXCEPTION', kind=type(exc).__name__)
        log.warning('CRIPS_PUBLIC_IMPORT_CHECK %s', json.dumps(STATE, ensure_ascii=True))

    @asynccontextmanager
    async def lifecycle(instance):
        async with previous(instance):
            if os.getenv('CRIPS_PUBLIC_IMPORT_CHECK') == 'true':
                threading.Thread(target=work, daemon=True, name='crips-public-probe').start()
            yield
    app.router.lifespan_context = lifecycle
