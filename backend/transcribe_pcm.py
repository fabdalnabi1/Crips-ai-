"""PCM audio adapter: avoid PyAV decode API incompatibilities.

FFmpeg creates a bounded 60-second PCM16/16kHz/mono WAV. Whisper receives
its documented float32 waveform input, not a filename for PyAV to decode.
"""
from __future__ import annotations
import gc
import math
import os
import tempfile
import wave
from pathlib import Path
import numpy as np

from backend.studio_gateway import (
    app, jobs, command, probe, Word, Segment, Transcript, FluxClipError, MAX_SECONDS
)


def read_pcm(path):
    with wave.open(str(path), 'rb') as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000):
            raise ValueError('Expected mono PCM16 at 16000 Hz')
        if wav.getnframes() > 61 * 16000:
            raise ValueError('Audio chunk exceeds the memory bound')
        raw = wav.readframes(wav.getnframes())
    return np.frombuffer(raw, dtype='<i2').astype(np.float32) / 32768.0


def transcribe(video, model_size=None, language=None, on_progress=None, **kwargs):
    from faster_whisper import WhisperModel
    vi = probe(video)
    if vi.duration > MAX_SECONDS:
        raise FluxClipError('VIDEO_LIMIT_EXCEEDED')
    if not vi.has_audio:
        return Transcript(language or 'en', vi.duration, [])
    model = WhisperModel(os.getenv('CRIPS_MODEL_PATH', '/opt/models/tiny'),
                         device='cpu', compute_type='int8', cpu_threads=1, num_workers=1)
    result, detected = [], language
    try:
        with tempfile.TemporaryDirectory(prefix='crips-pcm-') as tmp:
            wav = Path(tmp) / 'chunk.wav'
            for offset in range(0, math.ceil(vi.duration), 60):
                command(['ffmpeg', '-nostdin', '-y', '-v', 'error', '-threads', '1',
                         '-ss', offset, '-i', video, '-t', '60', '-vn', '-ac', '1',
                         '-ar', '16000', '-c:a', 'pcm_s16le', wav], timeout=120)
                audio = read_pcm(wav)
                stream, info = model.transcribe(audio, language=detected,
                    word_timestamps=True, beam_size=1, vad_filter=True,
                    condition_on_previous_text=False)
                detected = detected or info.language
                for seg in stream:
                    words = [Word(w.start + offset, w.end + offset, w.word)
                             for w in (seg.words or [])]
                    if words:
                        result.append(Segment(seg.start + offset, seg.end + offset,
                                              seg.text, words))
                del audio
                if on_progress:
                    on_progress(min(1, (offset + 60) / vi.duration))
    finally:
        del model
        gc.collect()
    return Transcript(detected or 'en', vi.duration, result)


jobs.transcribe = transcribe

# Only runs when an operator explicitly opts in; normal processing is unchanged.
from backend.youtube_probe import install as install_import_diagnostic
install_import_diagnostic(app)

# Registered, public, non-age-restricted sources only. Provider URLs stay server-side.
from backend.imported_sources import install as install_approved_imports
install_approved_imports(app)
