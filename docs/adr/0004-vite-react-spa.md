# 0004 — Vite + React SPA for the customer frontend

Status: accepted (2026-09-14), not built yet

## Context
Creators will need a dashboard (matches, evidence, takedown status). Agents produce React +
TypeScript most reliably. Server-rendering frameworks add version churn and client/server
boundaries that agents often get wrong.

## Decision
A separate `frontend/` Vite + React app: TypeScript strict, Tailwind + shadcn/ui,
TanStack Router + TanStack Query, API client generated from `backend/openapi.json`
(`openapi-typescript` + `openapi-fetch`). Same-domain session-cookie auth behind Caddy
(`/api` → Django, `/` → SPA). Tests with vitest; Playwright for critical flows.
Marketing pages, when needed, are a separate static site.

## Consequences
- No SSR/SEO for the app itself (not needed behind login).
- Until creators log in, the Django admin is the only UI.
- Media previews are blurred by default; nothing public exposes content.
