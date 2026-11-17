# 0003 — Django + Django Ninja for the backend

Status: accepted (2026-09-14)

## Context
The core (PDQ, ffmpeg, later PyTorch models) is Python. The solo operator needs an internal UI for
reviewing crawl results and labeling match pairs long before a customer frontend exists. The
frontend needs a typed contract.

## Decision
Django 6.1 for ORM, migrations, auth and admin. Django Ninja for REST endpoints with Pydantic
schemas and an auto-generated OpenAPI document committed as `backend/openapi.json`.
Tooling: uv, ruff, mypy strict with django-stubs, pytest-django, import-linter.

## Consequences
- The Django admin is the ops and labeling tool for phase 1 — no custom UI needed.
- The TypeScript client is generated from `openapi.json`; `make check` fails on drift.
- Django's typing is weaker than SQLAlchemy's; django-stubs + strict mypy compensates.
- FastAPI + SQLAlchemy was rejected: no admin, and auth/migrations would be assembled by hand.
