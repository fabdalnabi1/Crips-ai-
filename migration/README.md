# Crips AI — FluxClip migration

Status: EXPERIMENTAL. Not deployed or end-to-end tested.

This folder stages an upstream FluxClip Studio deployment, not a completed branded replacement. The current Crips AI site and Render backend are deliberately unchanged.

## Source and license
FluxClip: https://github.com/shahid2866/fluxclip (MIT license; retain upstream copyright and license when distributing substantial portions).

## Requirements
Docker Compose with support for Git build contexts; internet access during image builds and model downloads; sufficient disk/RAM for CPU Whisper processing.

## Try
```sh
docker compose -f migration/docker-compose.fluxclip.yml up --build
```
Open http://localhost:3000. The upstream application UI will still be FluxClip; branding and integration are not complete.

## Free processing
The upstream engine offers offline heuristic clip selection and local transcription, without paid AI API keys. It can be slow on CPU and downloads speech models on first run. YouTube links still rely on yt-dlp and may fail anti-bot checks.

## Next verification gates
1. Verify Docker build of both upstream services.
2. Process a permitted sample MP4 end-to-end and download clips.
3. Benchmark memory and CPU use against Render free limits.
4. Brand and secure the web UI as Crips AI.
5. Validate YouTube URL failure behavior and mobile Safari.
6. Deploy only after these checks pass.

No paid infrastructure or subscriptions have been purchased.
