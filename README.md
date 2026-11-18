# Marceline

**Archived.** Marceline was sunset on 2026-09-22 and nothing runs any more. This repository is a
read-only snapshot of the code, kept as a record of the project.

**Demo:** <https://marceline.studio> shows the customer app with sample data. It is a static page;
there is no backend behind it and nothing you enter is sent anywhere.

## What it was

Leak detection for content creators. Marceline crawled video piracy sites, indexed what it found,
and matched it against a creator's own videos and photos using perceptual fingerprints (PDQ), so
that a copy could be recognised without the creator's name appearing anywhere near it. For every
confirmed copy it kept a case file (where the copy is hosted, who to notify) and prepared the
takedown notice.

It was built by coding agents between 2026-09-13 and 2026-09-22, in roughly 1,750 commits. The
project stopped because it found no customers within its window, so it never launched.

## What is in here

| Path | What |
| --- | --- |
| `backend/` | Django project: crawler, index, fingerprint matching, catalogue, accounts, billing, notice delivery |
| `frontend/src/` | Vite + React app: the internal moderation UI and the first customer app |
| `frontend/customer/` | Next.js customer app that replaced it |
| `frontend/wasm/` | PDQ fingerprinting compiled to WebAssembly, so media is hashed in the browser |
| `client/` | Command-line fingerprinting client |
| `website/` | The static marketing site that ran at marceline.studio |
| `infra/` | Compose files, reverse proxy, backup and release scripts |
| `demo/` | The static demo: the real Next.js customer app on in-browser sample data |
| `docs/adr/` | 57 architecture decision records |
| `docs/conventions/`, `CLAUDE.md` | The rules the agents worked under |

## What this snapshot is not

This is an edited export, not the working repository.

- The git history, issues and pull requests are not included; the snapshot starts from a single
  commit.
- The `@marceline.studio` mail addresses that appear in code and documents are out of service.
- The crawled sites are anonymised as `site01` … `site25`, and example names, hosts and addresses
  are placeholders. The site adapters therefore document an approach; they do not run against
  anything.
- Captured third-party pages that the crawler tests were written against are not included, and
  neither are the per-site tests. Tests that depended on them will not pass.
- Internal documents (legal, research, runbooks, plans) are not included. ADRs and code comments
  still cite some of them by path or by issue number; those references lead nowhere here.

## Licence

No licence is granted. The code is published for reading; all rights reserved.

Third-party components keep their own licences: the vendored PDQ implementation under
`frontend/wasm/pdq/vendor/` and the test vectors under
`backend/matching/tests/fixtures/pdq-threatexchange/` (BSD, Meta Platforms), and the bundled fonts
(SIL Open Font License 1.1, see the `LICENSES.txt` next to them).
