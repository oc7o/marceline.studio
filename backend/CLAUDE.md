# Backend conventions

Django 6.1 + Django Ninja, Postgres 17, Python 3.13, managed with `uv` (`uv add <pkg>`, never pip).

## Subsystem conventions live next to the code
**Before changing code in one of these directories, read its `CLAUDE.md`** — it holds the rules that
used to be sections of this file (each topic's invariants, the traps already hit, the ADR behind them).
A tool that reads files through the shell does not load them by itself, so open the file deliberately.
- `backend/accounts/CLAUDE.md` — Tenancy, Unternehmer evidence at onboarding, Login throttling
- `backend/crawler/CLAUDE.md` — Crawler, Content safety
- `backend/catalog/CLAUDE.md` — Index suppression, Hoster intelligence
- `backend/matching/CLAUDE.md` — Matching
- `backend/billing/CLAUDE.md` — Billing
- `backend/delivery/CLAUDE.md` — Production read-through cache
- `backend/marceline/CLAUDE.md` — Targeted indexing, Prospect research, Customer API and tenancy, Production settings, Customer login, The customer case file
- `backend/marceline/capture/CLAUDE.md` — Browser capture: the worker (ADR 0016); its triage stays in `backend/matching/CLAUDE.md`
- `backend/marceline/rights/CLAUDE.md` — Rights model
- `backend/marceline/notices/CLAUDE.md` — Notice drafts
- `backend/marceline/deletion/CLAUDE.md` — Customer data deletion

## Where code goes
- `models.py` holds data: fields, constraints, indexes, `__str__`. No business logic, no network.
- Logic lives in plain modules inside the app: `services.py` (DB workflows), and focused modules
  such as `robots.py`, `worker.py`, `pdq.py`, `frames.py`, `hamming.py`. Pure functions where
  possible.
- Long-running processes are management commands in `<app>/management/commands/` that only wire
  modules together (e.g. `crawl_worker`, `media_worker`, `scheduler`).
- `admin.py` is the internal ops UI. Use `list_select_related`, `raw_id_fields` for big FKs,
  read-only admins for audit data.
- API endpoints live in `marceline/api.py` (or app-level `api.py` routers added to it). Every
  endpoint has typed Ninja `Schema` input/output. Run `make openapi` after changing the API.

## Data rules
- Constraints belong in the database (`UniqueConstraint`, `CheckConstraint`), not only in Python.
- Queue tables (e.g. `CrawlTask`) are claimed with `select_for_update(skip_locked=True)` inside
  `transaction.atomic()`.
- PDQ hashes are 32-byte `BinaryField`s; similarity search happens in memory (numpy), not SQL.
- Postgres-specific features are fine (JSONB, `pg_trgm`, `SKIP LOCKED`); SQLite is not supported.
- Run `make makemigrations` right after a model change. Generated migrations may be edited only to
  add operations Django can't infer (e.g. `TrigramExtension()`); never edit applied migrations.

## Typing and style
- mypy runs in strict mode with the django-stubs plugin: annotate every function, including
  tests (`-> None`). Generic admin classes: `admin.ModelAdmin[Model]`,
  `admin.TabularInline[Child, Parent]`.
- Untyped third-party packages get a narrow `ignore_missing_imports` override (e.g. `pdqhash`).
- Ruff (line length 100) handles lint, import sorting and formatting — run `make fmt`.
- Prefer tuples for admin/Meta option sequences.

## Tests
- pytest + pytest-django, tests in `<app>/tests/test_*.py`, shared fixtures in `backend/conftest.py`.
- **Tests never read feature flags from `.env`.** `core.env.load_dotenv` copies the operator's
  `.env` into the environment before settings load, so the autouse fixture
  `_pin_operational_settings` in `backend/conftest.py` pins every operational switch
  (`OPERATIONAL_DEFAULTS`: `TARGETED_INDEXING_ENABLED`, `PROSPECT_DAILY_SEED_CAP`, `BROWSER_CAPTURE_*`,
  `SAFETY_ALERT_*`, `MARCELINE_ROLE`, `CACHE_*`, `OUTBOX_*`, `BILLING_*`) to its
  code default. A test that needs another value sets it with the `settings` fixture or
  `override_settings`. A new operational flag goes into `OPERATIONAL_DEFAULTS` in the same change.
- Tests hit the real compose Postgres (`make test` starts it). Use `transaction.atomic()` around
  statements expected to raise `IntegrityError`.
- **The suite runs in parallel by default** (`pytest -n auto --dist load`, `backend/pyproject.toml`
  `addopts`; pytest-xdist). Any `django_db(transaction=True)` test truncates every table in its
  teardown, including `catalog.SafetyTerm`'s 34 starter rows, seeded once by a data migration when
  the test database is built — a data migration never runs again. The autouse
  `_ensure_safety_terms` fixture in `backend/conftest.py` restores them (`get_or_create`, idempotent)
  before any test that touches the database, so this does not depend on test order, worker or
  `--dist` mode. A new session-scoped seed (another data migration whose rows a test asserts on
  directly) needs the same treatment, not a fixture that merely loads it once.
- No network: mock HTTP (`httpx.MockTransport`) and LLM clients; adapters are tested against
  fixtures via `crawler.tests.helpers.load_fixture`.
- Matching tests use synthetic images (Pillow/numpy) and ffmpeg `testsrc` videos — never real
  media.
