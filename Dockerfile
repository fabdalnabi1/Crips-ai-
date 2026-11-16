FROM node:22-bookworm-slim AS node_runtime
FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HF_HUB_DISABLE_XET=1 \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 FLUXCLIP_DEVICE=cpu \
    FLUXCLIP_COMPUTE_TYPE=int8 FLUXCLIP_DATA=/tmp/crips-studio \
    FLUXCLIP_CACHE=/tmp/crips-cache CRIPS_MODEL_PATH=/opt/models/tiny
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg git ca-certificates fontconfig fonts-dejavu-core espeak-ng \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
RUN git clone https://github.com/shahid2866/fluxclip.git /opt/fluxclip \
    && cd /opt/fluxclip && git checkout a9a28a4b4aca2900bd54e77207b748bbb0da9a24 \
    && pip install --no-cache-dir '.[studio]' 'yt-dlp[default]>=2025.10' \
    && cp LICENSE /app/FLUXCLIP-LICENSE \
    && rm -rf /opt/fluxclip/.git
RUN python -c "from faster_whisper.utils import download_model; download_model('tiny', output_dir='/opt/models/tiny')"
COPY backend /app/backend
RUN python -m py_compile /app/backend/studio_gateway.py /app/backend/transcribe_pcm.py \
    && python -c "from backend.transcribe_pcm import app; print('Crips PCM API import OK')"
EXPOSE 10000
CMD ["sh","-c","exec uvicorn backend.transcribe_pcm:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1 --no-access-log"]
