FROM node:22-bookworm-slim AS node_runtime
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY backend /app/backend
ENV PORT=8000
EXPOSE 8000
CMD ["sh","-c","uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
