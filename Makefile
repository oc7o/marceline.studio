SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE := docker compose -f infra/compose.yaml $(if $(wildcard .env),--env-file .env)

# The dev servers bind to $(WEB_BIND) so they are reachable over Tailscale. Read it from .env when
# the shell does not export it: on 2026-09-18 `make customer-dev` run from a non-interactive shell
# bound to 127.0.0.1 and the app was unreachable, while the operator's own shell exports WEB_BIND
# and it worked for him. A target that behaves differently for two people is a target that lies.
ifneq ($(wildcard .env),)
WEB_BIND ?= $(shell sed -n 's/^WEB_BIND=//p' .env)
endif
WEB_BIND ?= 127.0.0.1
export WEB_BIND
# Every checkout addresses the same compose project, but a git worktree has no .env, so compose
# started there recreates the main tree's containers with different settings (2026-09-16: killed a
# running fingerprint import mid-run). Compose targets refuse to run without .env; in a worktree
# run tests with COMPOSE=true, which turns the db target into a no-op.
ENV_GUARD = @if [ ! -f .env ] && [ "$(COMPOSE)" != "true" ]; then \
		echo "Refusing: no .env in $$PWD. Compose targets belong in the main checkout;" >&2; \
		echo "in a worktree use: POSTGRES_DB=marceline_<task> make COMPOSE=true <target>" >&2; \
		exit 1; \
	fi
WORKERS := crawler-site01 crawler-site02 crawler-site03 crawler-site04 \
	crawler-site19 crawler-site18 crawler-site22 crawler-site05 crawler-site06 \
	crawler-site07 crawler-site08 crawler-site20 crawler-site17 crawler-site21 \
	crawler-site09 crawler-site10 crawler-site11 crawler-site13 \
	media-site01 media-site02 media-site03 media-site04 \
	media-site19 media-site18 media-site22 media-site05 media-site06 media-site07 media-site08 media-site20 \
	media-site17 media-site21 media-site09 media-site10 media-site11 \
	media-site13 \
	scheduler
# Per-site workers `make workers` leaves alone, e.g. the sites the crawl runtime serves during its
# soak: make workers WORKERS_SKIP="crawler-site20 media-site20 crawler-site06 media-site06"
WORKERS_SKIP ?=
# The crawl runtime (ADR 0030): one container per lane, serving RUNTIME_SITES.
RUNTIME := crawler-pages crawler-media
MANAGE := cd backend && uv run python manage.py
OPENAPI_EXPORT := export_openapi_schema --api marceline.api.api --indent 2

# Rebuild only when the image contents would actually change, not on every deploy. The worker
# containers bind-mount backend/ (see ENV_GUARD's neighbour above and CLAUDE.md "Restart workers
# after every migration"), so a restart already runs current code; only backend/pyproject.toml,
# backend/uv.lock (dependencies — the pdqhash compile is the expensive part) or backend/Dockerfile
# changing the image contents needs a rebuild. `up`/`workers`/`runtime` used to pass `--build`
# unconditionally, rebuilding all ~30 worker images on every deploy; on 2026-09-16 that pushed this
# 4-core server's load average to 14 and made it briefly unreachable, twice in one evening. A stamp
# file per target records the backend/ state as of its last build; REBUILD=1 (or the matching
# `-build` target) forces a rebuild regardless, which is required after touching any BUILD_INPUTS.
BUILD_INPUTS := backend/pyproject.toml backend/uv.lock backend/Dockerfile
UP_BUILD_STAMP := .make/up-build.stamp
WORKERS_BUILD_STAMP := .make/workers-build.stamp
RUNTIME_BUILD_STAMP := .make/runtime-build.stamp
REBUILD ?=

# $(1) = stamp file, $(2) = target name (for messages and the "-build" escape hatch),
# $(3) = extra compose flags (e.g. --profile workers), $(4) = services to build.
define maybe_build
	@mkdir -p $(dir $(1)); \
	if [ -n "$(REBUILD)" ] || [ ! -f $(1) ] || [ -n "$$(find $(BUILD_INPUTS) -newer $(1) 2>/dev/null)" ]; then \
		echo "==> rebuilding image(s) for '$(2)' (backend/pyproject.toml, uv.lock or Dockerfile changed, or no prior build)"; \
		$(COMPOSE) $(3) build $(4) && touch $(1); \
	else \
		echo "==> image(s) for '$(2)' unchanged since the last build; skipping rebuild (force with 'make $(2)-build' or REBUILD=1)"; \
	fi
endef

.PHONY: help check-posted sync up up-build workers workers-build workers-stop runtime runtime-build runtime-stop down db migrate makemigrations shell test fmt lint typecheck imports \
	migrations-check openapi openapi-check check remote-check hooks frontend-install frontend-dev frontend-build frontend-api \
	frontend-api-check frontend-check customer-install customer-dev customer-build customer-check \
	browser-parity browser-speed backup backup-check prod-config prod-surface-check customer-smoke status \
	website-check

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

sync: ## Install backend dependencies into backend/.venv
	cd backend && uv sync

# Derived project state (issue #79, CLAUDE.md "Prove it before reporting it"). Read-only: it
# writes nothing, anywhere — see the command's docstring. No ENV_GUARD and no compose, because it
# reads the running containers with `docker inspect` instead of starting anything; it is therefore
# safe in a worktree, and each section degrades by itself with docker, the database or the network
# down. Pass flags through ARGS, e.g. `make status ARGS=--gaps`.
status: ## Current project state, derived from git/docker/db/GitHub/docs (read-only, writes nothing)
	@$(MANAGE) project_status $(ARGS)

up: ## Start db + web (http://127.0.0.1:8000); no crawling; always restarts web with current code, rebuilds its image only if backend deps/Dockerfile changed (REBUILD=1 or 'make up-build' to force)
	$(ENV_GUARD)
	$(call maybe_build,$(UP_BUILD_STAMP),up,,web)
	$(COMPOSE) up -d --force-recreate web

up-build: ## Force-rebuild the web image, then start (same as REBUILD=1 make up)
	$(ENV_GUARD)
	$(MAKE) REBUILD=1 up

workers: ## Start live crawler + media workers; always restarts them with current code (run after every migration), rebuilds images only if backend deps/Dockerfile changed (REBUILD=1 or 'make workers-build' to force)
	$(ENV_GUARD)
	$(call maybe_build,$(WORKERS_BUILD_STAMP),workers,--profile workers,$(filter-out $(WORKERS_SKIP),$(WORKERS)))
	$(COMPOSE) --profile workers up -d --force-recreate $(filter-out $(WORKERS_SKIP),$(WORKERS))

workers-build: ## Force-rebuild worker images, then start (same as REBUILD=1 make workers)
	$(ENV_GUARD)
	$(MAKE) REBUILD=1 workers

workers-stop: ## Stop crawler + media workers
	$(ENV_GUARD)
	$(COMPOSE) --profile workers stop $(WORKERS)

runtime: ## Start the crawl runtime lanes for RUNTIME_SITES; always restarts them with current code, rebuilds images only if backend deps/Dockerfile changed (REBUILD=1 or 'make runtime-build' to force; stop those sites' workers first). The lanes also self-restart on a migration without this.
	$(ENV_GUARD)
	$(call maybe_build,$(RUNTIME_BUILD_STAMP),runtime,--profile runtime,$(RUNTIME))
	$(COMPOSE) --profile runtime up -d --force-recreate $(RUNTIME)

runtime-build: ## Force-rebuild the runtime images, then start (same as REBUILD=1 make runtime)
	$(ENV_GUARD)
	$(MAKE) REBUILD=1 runtime

runtime-stop: ## Stop the crawl runtime lanes (in-flight tasks drain or are handed back)
	$(ENV_GUARD)
	$(COMPOSE) --profile runtime stop $(RUNTIME)

down: ## Stop all services
	$(ENV_GUARD)
	$(COMPOSE) --profile workers --profile runtime down

db: ## Start Postgres and wait until it is healthy
	$(ENV_GUARD)
	$(COMPOSE) up -d --wait db

migrate: db ## Apply migrations
	$(MANAGE) migrate

makemigrations: db ## Generate migrations after model changes
	$(MANAGE) makemigrations

shell: db ## Django shell
	$(MANAGE) shell

test: db ## Run pytest against the compose Postgres
	cd backend && uv run pytest

fmt: ## Auto-fix lint issues and format code
	cd backend && uv run ruff check --fix . && uv run ruff format .

lint: ## Ruff lint + format check
	cd backend && uv run ruff check . && uv run ruff format --check .

typecheck: ## mypy (strict)
	cd backend && uv run mypy .

imports: ## Enforce module boundaries (import-linter)
	cd backend && uv run lint-imports

# Two failures this catches in under a second, both of which otherwise surface only once the test
# run reaches `migrate` — after lint, mypy and import-linter, minutes in, and not at all when a
# check is interrupted. Parallel agents have produced both repeatedly: two migrations numbered the
# same ("multiple leaf nodes in the migration graph"), and a model changed without its migration.
# Needs no database: a missing connection is a warning here, not an error.
migrations-check: ## Fail on a migration-graph conflict or a model change with no migration
	$(MANAGE) makemigrations --check --dry-run

openapi: ## Regenerate backend/openapi.json
	$(MANAGE) $(OPENAPI_EXPORT) --output openapi.json

openapi-check: ## Fail if backend/openapi.json is out of date
	@tmp=$$(mktemp); \
	if (cd backend && uv run python manage.py $(OPENAPI_EXPORT) --output $$tmp) && diff -u backend/openapi.json $$tmp; then \
		rm -f $$tmp; echo "openapi.json up to date"; \
	else \
		rm -f $$tmp; echo "openapi.json is out of date: run 'make openapi'"; exit 1; \
	fi

frontend-install: ## Install frontend dependencies (npm ci)
	cd frontend && npm ci

frontend-dev: ## Vite dev server on $${WEB_BIND:-127.0.0.1}:5173, /api proxied to Django :8000
	cd frontend && npm run dev

frontend-build: ## Typecheck + production build into frontend/dist
	cd frontend && npm run build

frontend-api: ## Regenerate frontend/src/api/schema.d.ts from backend/openapi.json
	cd frontend && npm run api

frontend-api-check: ## Fail if the generated API types are out of date
	@tmp=$$(mktemp --suffix=.d.ts); \
	if (cd frontend && npx openapi-typescript ../backend/openapi.json -o $$tmp >/dev/null 2>&1) && diff -u frontend/src/api/schema.d.ts $$tmp; then \
		rm -f $$tmp; echo "schema.d.ts up to date"; \
	else \
		rm -f $$tmp; echo "schema.d.ts is out of date: run 'make openapi frontend-api'"; exit 1; \
	fi

frontend-check: frontend-api-check ## Frontend typecheck, eslint, vitest, bundle-size budget
	cd frontend && npx tsc -b && npx eslint . && npx vitest run && npx vite build && npm run check:bundle

# The Next.js customer app (ADR 0036). Its own project under frontend/customer/, its own
# node_modules; it shares the words and the pure logic with the SPA through @shared/*.
customer-install: ## npm ci in frontend/customer (Node >= 20.19)
	cd frontend/customer && npm ci

customer-dev: ## Next dev server for /app on $${WEB_BIND:-127.0.0.1}:3000, Django on :8000
	cd frontend/customer && \
		CUSTOMER_DJANGO_BASE_URL=$${CUSTOMER_DJANGO_BASE_URL:-http://$${WEB_BIND:-127.0.0.1}:8000} \
		NEXT_TELEMETRY_DISABLED=1 npx next dev --webpack --hostname $${WEB_BIND:-127.0.0.1} --port 3000

customer-build: ## Production build of the customer app (standalone server in .next/standalone)
	cd frontend/customer && NEXT_TELEMETRY_DISABLED=1 npm run build

customer-check: ## Customer app typecheck, eslint, vitest, build, bundle-size budget
	cd frontend/customer && npx tsc --noEmit && npx eslint . && npx vitest run \
		&& NEXT_TELEMETRY_DISABLED=1 npm run build && npm run check:bundle

website-check: ## Validate the public marketing site (HTML, links, no foreign origin, no banned wording)
	python3 website/check.py

check: website-check lint typecheck imports migrations-check test openapi-check frontend-check customer-check ## Definition of done: everything must pass

# The check runner (docs/runbooks/check-runner.md): a dedicated host so `make check` doesn't queue
# on this server's shared check lock. CHECK_NAME defaults to the current directory's basename, so
# a worktree gets its own name for free; override either variable to point elsewhere or run several
# checks in parallel from one machine.
# 2026-09-19: the runner VM was deleted when the project was downscaled, so there is no default
# host any more. Rebuild one with docs/runbooks/check-runner.md and pass CHECK_HOST=… to use it.
CHECK_HOST ?=
CHECK_NAME ?= $(notdir $(CURDIR))
CHECK_DIR := /srv/checks/$(CHECK_NAME)

# POST_STATUS=1 additionally posts the `check` commit status for HEAD (success on a green run,
# failure otherwise) — the first of the three gates a promotion PR needs, docs/runbooks/release.md.
# Opt-in on purpose: check-posted is the everyday command now (the check-runner VM was deleted
# 2026-09-19), its HEAD is usually a local commit GitHub has never seen (the status would be
# refused, or worse, land on the wrong commit), and it would otherwise make a network call on every
# run. Turn it on for a promotion PR's own check. The status is posted with --soft, so bookkeeping
# never turns a green check red or a red one green: `make check-posted`/`make remote-check` always
# exit with the check's own status.
#
# For a promotion: run this from a checkout AT THE RELEASE BRANCH'S HEAD (`git checkout
# release/YYYY-MM-DD`, docs/runbooks/release.md "Cutting a release branch"), not from `development`
# or a worktree that merely shares its tree — the status attaches to whatever `git rev-parse HEAD`
# is in the checkout that runs this command, and a promotion's `check` gate must be posted against
# the exact commit `infra/release/cut.sh` cut and the PR names, or the ruleset will never see it.
POST_STATUS ?=

# One full check at a time on this server: it is RAM-bound (stop the workers and the runtime first).
CHECK_LOCK ?= /tmp/marceline-check.lock

check-posted: ## `make check` here under the shared check lock; POST_STATUS=1 also posts the `check` commit status for HEAD
	@flock $(CHECK_LOCK) $(MAKE) check; rc=$$?; \
	if [ -n "$(POST_STATUS)" ]; then \
		if [ $$rc -eq 0 ]; then state=success; text="make check passed locally"; \
		else state=failure; text="make check failed locally (exit $$rc)"; fi; \
		infra/release/post-status.sh --soft --context check --state $$state --description "$$text"; \
	fi; exit $$rc

remote-check: ## Run `make check` on the check-runner host instead of here (docs/runbooks/check-runner.md); POST_STATUS=1 also posts the `check` commit status for HEAD
	@if [ -z "$(CHECK_HOST)" ]; then \
		echo "remote-check: there is no check runner any more (deleted 2026-09-19)."; \
		echo "  Run the check here instead:  make workers-stop runtime-stop && make check-posted"; \
		echo "  (one check at a time on this server; docs/runbooks/check-runner.md rebuilds a runner)"; \
		exit 2; fi
	infra/check-runner/sync.sh . $(CHECK_HOST) $(CHECK_DIR)
	@echo "ssh $(CHECK_HOST) 'cd $(CHECK_DIR) && bash infra/check-runner/run-check.sh $(CHECK_NAME)'"; \
	ssh $(CHECK_HOST) 'cd $(CHECK_DIR) && bash infra/check-runner/run-check.sh $(CHECK_NAME)'; \
	rc=$$?; \
	if [ -n "$(POST_STATUS)" ]; then \
		if [ $$rc -eq 0 ]; then state=success; text="make check passed on $(CHECK_NAME)"; \
		else state=failure; text="make check failed on $(CHECK_NAME) (exit $$rc)"; fi; \
		infra/release/post-status.sh --soft --context check --state $$state --description "$$text"; \
	fi; \
	exit $$rc

browser-parity: ## Measure browser vs CLI fingerprint distance in a real browser (on demand, NOT in check)
	uv run frontend/parity/parity.py $(PARITY_ARGS)

browser-speed: ## Measure how long browser fingerprinting takes, throttled (on demand, NOT in check)
	cd frontend/parity && node speed.mjs $(SPEED_ARGS)

backup: ## Dump the database + encrypted off-host copy; mails the operator if it fails
	infra/backup/backup-run.sh

backup-check: ## Fail (and mail) when a backup, off-host copy or restore check is overdue
	infra/backup/backup-check.sh

# Production customer cache (ADR 0028, docs/runbooks/production.md). These only render and test
# locally; nothing here builds for, copies to or deploys on a remote host.
PROD_COMPOSE := docker compose -f infra/prod/compose.yaml --env-file infra/prod/prod.env.example
# The standalone edge for the public marketing site (operator, 2026-09-18): its own project, one
# Caddy, no upstream. Rendered both ways, because the IPv6 ports are a second -f file and the
# invariants have to hold for what the host actually starts.
SITE_COMPOSE := docker compose -f infra/prod/site/compose.yaml --env-file infra/prod/site/site.env.example
SITE_COMPOSE6 := docker compose -f infra/prod/site/compose.yaml -f infra/prod/site/compose.ipv6.yaml --env-file infra/prod/site/site.env.example

prod-config: ## Render + validate the production compose files (placeholder env; starts nothing)
	$(PROD_COMPOSE) config --quiet
	$(PROD_COMPOSE) config --format json | python3 infra/prod/check_config.py
	# The same file as the cut-over will run it (one Caddy, both names — operator 2026-09-19):
	# PUBLIC_SITE=on, the public address on 80/443, the tailnet address on the purge site, a public
	# issuer. The rules that only apply then are checked against what the host will really start.
	# Placeholder addresses: RFC 5737 TEST-NET-3 and the tailnet's own range.
	PUBLIC_SITE=on CADDY_BIND=203.0.113.10 TAILSCALE_BIND=100.64.0.2 CADDY_TLS=acme \
	  $(PROD_COMPOSE) config --format json | python3 infra/prod/check_config.py
	$(SITE_COMPOSE) config --quiet
	$(SITE_COMPOSE) config --format json | python3 infra/prod/site/check_config.py
	# SITE_BIND6 stays commented out in site.env.example (no AAAA record yet, the live deploy is
	# IPv4-only) — supplied here only so this rendering keeps exercising compose.ipv6.yaml's
	# invariants; documentation placeholder, RFC 3849, same as the commented line.
	SITE_BIND6=2001:db8::10 $(SITE_COMPOSE6) config --format json | python3 infra/prod/site/check_config.py

prod-surface-check: ## Exercise the production Caddyfile against a stub upstream (docker; on demand)
	infra/prod/check-surface.sh

customer-smoke: ## Run the customer app's production image against a stub Django (docker; on demand)
	infra/prod/check-customer.sh

hooks: ## Install git pre-commit hook (ruff lint + format check)
	@hook=$$(git rev-parse --git-path hooks)/pre-commit; \
	printf '#!/bin/sh\nset -e\ncd "$$(git rev-parse --show-toplevel)/backend"\nuv run ruff check .\nuv run ruff format --check .\n' > $$hook; \
	chmod +x $$hook; echo "installed $$hook"
