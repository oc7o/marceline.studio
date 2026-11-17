# 0008 — Container image with ffmpeg, two-stage build for pdqhash

Status: accepted (2026-09-14)

## Context
Matching uses `pdqhash` (Meta's PDQ C++ code behind a Cython wrapper), which ships no Linux wheels
and must be compiled — this needs `g++` (and Python headers locally). Preview clips are sampled with
`ffmpeg`/`ffprobe` via subprocess. All processes share one image (ADR 0001).

## Decision
- `backend/Dockerfile` has two stages: a build stage with `g++` runs `uv sync --frozen` into
  `/opt/venv`; the runtime stage copies the venv and installs only `ffmpeg`.
- Local development requires `g++`, `python3-dev` and `ffmpeg` (listed in root `CLAUDE.md`).
- Every process (web, crawler, media) uses the same image, even though only the media worker needs
  ffmpeg.

## Consequences
- The image is ~930 MB, mostly ffmpeg. Acceptable for now.
- No compiler in the runtime image.
- If image size or deploy time hurts, split a slim image for web/crawler from a media image.
