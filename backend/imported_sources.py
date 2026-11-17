"""Operator-approved PUBLIC imports. Signed provider URLs stay in server env.

This does not call vidIQ, bypass YouTube authentication, or unlock restricted
videos. The assistant imports a public source through the connected provider,
then registers that exact source here. Unregistered URLs keep the normal path.
"""
from __future__ import annotations
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse

LOG = logging.getLogger('crips.import_bridge')
ID = re.compile(r'^[A-Za-z0-9_-]{11}$')
ASSET = re.compile(r'^/vidiq-user-videos/[0-9a-fA-F-]{36}/[0-9a-fA-F-]{36}\.mp4$')
MAX_BYTES = 500_000_000
PROOF = {'state': 'not_run', 'scope': 'registered_public_source_only'}
LOCAL = threading.local()

class ImportFailure(RuntimeError):
    pass


def record_for(video_id: str):
    try:
        data = json.loads(os.getenv('CRIPS_IMPORTED_SOURCES', '{}'))
    except (TypeError, ValueError):
        raise ImportFailure('IMPORTED_SOURCE_CONFIG_INVALID') from None
    if not isinstance(data, dict) or len(data) > 64:
        raise ImportFailure('IMPORTED_SOURCE_CONFIG_INVALID')
    item = data.get(video_id)
    if item is None:
        return None
    if not ID.fullmatch(video_id) or not isinstance(item, dict):
        raise ImportFailure('IMPORTED_SOURCE_CONFIG_INVALID')
    if item.get('public_non_age_restricted') is not True:
        raise ImportFailure('IMPORTED_SOURCE_NOT_APPROVED')
    if not item.get('rights_basis') or item.get('provider') != 'vidiq':
        raise ImportFailure('IMPORTED_SOURCE_NOT_APPROVED')
    url = item.get('url')
    if not isinstance(url, str) or len(url) > 8192:
        raise ImportFailure('IMPORTED_SOURCE_CONFIG_INVALID')
    try:
        u = urlparse(url)
        if (u.scheme != 'https' or u.hostname != 'storage.googleapis.com' or
            u.username or u.password or u.port or u.fragment or not ASSET.fullmatch(u.path)):
            raise ValueError()
        query = parse_qs(u.query)
        expiry = int(query.get('Expires', ['0'])[0])
        if not query.get('Signature') or not query.get('GoogleAccessId'):
            raise ValueError()
    except (TypeError, ValueError):
        raise ImportFailure('IMPORTED_SOURCE_CONFIG_INVALID') from None
    if expiry <= int(time.time()) + 30:
        raise ImportFailure('IMPORTED_SOURCE_EXPIRED')
    return dict(item, expires_at=expiry)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download_approved(item: dict, folder: Path, probe):
    """Streaming bounded download, no redirects or credentials in error logs."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    partial, target = folder / 'import.part', folder / 'source.mp4'
    opener = urllib.request.build_opener(NoRedirect())
    digest = hashlib.sha256()
    count = 0
    began = time.monotonic()
    try:
        req = urllib.request.Request(item['url'], headers={'Accept': 'video/mp4'})
        with opener.open(req, timeout=30) as response:
            if response.status != 200:
                raise ImportFailure('IMPORTED_SOURCE_DOWNLOAD_FAILED')
            declared = response.headers.get('Content-Length')
            if declared and int(declared) > MAX_BYTES:
                raise ImportFailure('VIDEO_LIMIT_EXCEEDED')
            with partial.open('wb') as out:
                while True:
                    if time.monotonic() - began > 180:
                        raise ImportFailure('PROCESSING_TIMEOUT')
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > MAX_BYTES:
                        raise ImportFailure('VIDEO_LIMIT_EXCEEDED')
                    out.write(chunk)
                    digest.update(chunk)
            if declared and count != int(declared):
                raise ImportFailure('IMPORTED_SOURCE_INCOMPLETE')
        with partial.open('rb') as check:
            signature = check.read(64)
        if count < 1000 or b'ftyp' not in signature:
            raise ImportFailure('IMPORTED_SOURCE_INVALID_VIDEO')
        partial.replace(target)
        info = probe(target)
        if (not math.isfinite(info.duration) or not 1 <= info.duration <= 3600 or
            info.width <= 0 or info.height <= 0 or info.width * info.height > 3840 * 2160):
            raise ImportFailure('VIDEO_LIMIT_EXCEEDED')
        LOG.warning('CRIPS_APPROVED_IMPORT_OK bytes=%d duration=%.2f sha256=%s',
                    count, info.duration, digest.hexdigest())
        return target
    except ImportFailure:
        partial.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise
    except Exception:
        partial.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise ImportFailure('IMPORTED_SOURCE_DOWNLOAD_FAILED') from None


def rank_windows(samples, duration, count, minimum, maximum, occupied=()):
    """Select non-overlapping activity windows; not a semantic virality score."""
    if count <= 0 or minimum <= 0 or maximum < minimum:
        return []
    length = min(float(maximum), max(float(minimum), 20.0))
    if not math.isfinite(duration) or duration < length:
        return []
    windows = []
    for i in range(int((duration - length) // 5) + 1):
        start, end = float(i * 5), float(i * 5) + length
        score = sum(max(0.0, s) for t, s in samples if start <= t < end)
        windows.append((score, start, end))
    selected = []
    for score, start, end in sorted(windows, key=lambda x: (-x[0], x[1])):
        if any(max(start, a) < min(end, b) for a, b in [*occupied, *[(s, e) for _, s, e in selected]]):
            continue
        selected.append((score, start, end))
        if len(selected) == count:
            break
    return sorted(selected, key=lambda x: x[1])


def visual_candidates(video, duration, count, minimum, maximum, command, occupied=()):
    result = command(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1', '-i', video,
        '-vf', "fps=1,scale=160:-2,select='gte(scene,0)',metadata=print:file=-",
        '-filter_threads', '1', '-an', '-f', 'null', '-'], timeout=300)
    samples, stamp = [], None
    for line in result.stdout.splitlines():
        m = re.search(r'pts_time:([0-9.]+)', line)
        if m:
            stamp = float(m.group(1))
        m = re.search(r'lavfi.scene_score=([0-9.eE+-]+)', line)
        if m and stamp is not None:
            score = float(m.group(1))
            if math.isfinite(score):
                samples.append((stamp, score))
    if not samples:
        return []
    from fluxclip.highlights import Clip
    return [Clip(start=s, end=e, title='Visual activity %02d' % (i+1),
        topic='Visual activity', reason='Scene-change activity fallback, not semantic AI.',
        score=0.0, caption='')
        for i, (_, s, e) in enumerate(rank_windows(samples, duration, count, minimum, maximum, occupied))]


def install(app):
    if getattr(app.state, 'crips_imports_installed', False):
        return
    app.state.crips_imports_installed = True
    from backend import studio_gateway as g
    original_resolve, original_tx, original_pick = g.jobs.resolve_input, g.jobs.transcribe, g.jobs.find_highlights

    def resolve(source, workdir=None, info_out=None):
        normalized = g.normalize_youtube(source)
        video_id = parse_qs(urlparse(normalized).query)['v'][0]
        item = record_for(video_id)
        if item is None:
            return original_resolve(source, workdir=workdir, info_out=info_out)
        result = download_approved(item, Path(workdir), g.probe)
        if info_out is not None:
            info_out.update(title=item.get('title', 'Imported public video'), id=video_id)
        return result

    def transcribe(video, *args, **kwargs):
        LOCAL.video = str(video)
        return original_tx(video, *args, **kwargs)

    def pick(transcript, n=8, min_len=20, max_len=60, *args, **kwargs):
        chosen = original_pick(transcript, n, min_len, max_len, *args, **kwargs)
        chosen = [c for c in chosen if min_len <= c.end-c.start <= max_len][:n]
        if len(chosen) >= n or not getattr(LOCAL, 'video', None):
            return chosen
        extra = visual_candidates(LOCAL.video, transcript.duration, n-len(chosen), min_len, max_len,
                                  g.command, [(c.start,c.end) for c in chosen])
        if extra:
            LOG.warning('CRIPS_VISUAL_FALLBACK clips=%d', len(extra))
        return sorted(chosen + extra, key=lambda c:c.start)

    g.jobs.resolve_input, g.jobs.transcribe, g.jobs.find_highlights = resolve, transcribe, pick

    @app.get('/api/imported-check')
    def imported_check():
        return PROOF

    def prove(video_id):
        PROOF.update(state='running', video_id=video_id)
        try:
            item = record_for(video_id)
            if item is None:
                raise ImportFailure('IMPORTED_SOURCE_NOT_REGISTERED')
            deadline = time.monotonic() + 900
            while any(p['status'] in ('queued','processing') for p in g.db.list_projects()):
                if time.monotonic() > deadline:
                    raise ImportFailure('SERVER_BUSY')
                time.sleep(3)
            with g._CREATE_LOCK:
                p = g.db.create_project('https://www.youtube.com/watch?v='+video_id,
                    {'num_clips':5,'min_len':15,'max_len':20,'caption_style':'minimal',
                     'aspect':'9:16','layout':'fit','headline':False,'quality':'tiny'},
                    title='Public YouTube import proof - '+item.get('title','Video'))
                g.jobs.enqueue_project(p['id'])
            PROOF['project_id'] = p['id']
            while time.monotonic() < deadline:
                now = g.db.get_project(p['id'])
                if now and now['status'] in ('done','failed'):
                    break
                time.sleep(3)
            else:
                raise ImportFailure('PROCESSING_TIMEOUT')
            ready = [c for c in g.db.list_clips(p['id']) if c['status']=='ready']
            if now['status']!='done' or not ready:
                raise ImportFailure('NO_CLIPS_RENDERED')
            clips = []
            for clip in ready:
                path = g.db.project_dir(p['id']) / clip['file']
                vi = g.probe(path)
                if not (14.8 <= vi.duration <= 20.5 and vi.width==720 and vi.height==1280):
                    raise ImportFailure('OUTPUT_NOT_VERIFIED')
                clips.append({'duration':round(vi.duration,2),'bytes':path.stat().st_size})
            PROOF.update(state='passed', clips=clips, count=len(clips),
                         attribution=item.get('attribution',''),
                         note='Only this registered public source is verified. Restricted sources remain unsupported.')
            LOG.warning('CRIPS_IMPORTED_PIPELINE_PASS %s', json.dumps(PROOF))
        except Exception as exc:
            code = str(exc) if isinstance(exc, ImportFailure) else type(exc).__name__
            PROOF.update(state='failed', code=code)
            LOG.warning('CRIPS_IMPORTED_PIPELINE_FAILED %s', json.dumps(PROOF))

    previous = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(instance):
        async with previous(instance):
            test_id = os.getenv('CRIPS_IMPORT_PROOF_ID','')
            if ID.fullmatch(test_id):
                threading.Thread(target=prove,args=(test_id,),daemon=True,name='crips-import-proof').start()
            yield
    app.router.lifespan_context = lifespan
