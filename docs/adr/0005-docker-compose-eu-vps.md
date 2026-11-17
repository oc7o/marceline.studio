# 0005 — Docker Compose on EU VPS hosting

Status: accepted (2026-09-14)

## Context
Solo operator, low early load, GDPR relevance, and hosting providers that may restrict
crawling traffic. Agents work well with Compose; Kubernetes and Terraform
add surface without benefit at this size.

## Decision
Docker Compose in development (`infra/compose.yaml`) and production (prod override file added
before first deploy). One EU VPS runs web + Postgres; a separate small VPS runs crawler + media
workers so blocks or abuse complaints never affect the app. Nightly `pg_dump` to off-host storage.
The provider's acceptable-use policy (crawling) is verified before signup.

## Consequences
- Dev and prod have the same shape; `docker compose up` is the whole environment.
- Scaling is vertical or by adding worker hosts; no orchestration layer.
- Containers stay provider-neutral so moving hosts is cheap.
