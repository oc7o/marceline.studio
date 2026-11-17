# 0001 — Modular monolith with several process types

Status: accepted (2026-09-14)

## Context
One founder, no manual coding; all code is written by agents. Crawling, hashing and matching have
different scaling needs, but microservices add network contracts, deployments and distributed
debugging — work agents handle poorly and a solo operator can't afford.

## Decision
One Python codebase (Django project in `backend/`), one container image, started as different
processes via management commands: `web`, `scheduler`, `crawler`, `media`, `matcher`.
Module boundaries between Django apps are enforced with import-linter.
A separate service is only created for a different runtime (GPU model, headless-browser pool) or
for isolation.

## Consequences
- Workers scale independently by running more processes; they share models and the database.
- Refactors across modules are single commits with one test suite.
- Crawler/media processes can still be deployed on a separate host/IP.
- Discipline on boundaries depends on the import-linter contracts staying strict.
