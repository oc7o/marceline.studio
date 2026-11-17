"""Breadth-first listing walks with a title gate (ADR 0029 amendment 2026-09-18). Pure.

A site's broad catalogue (`SiteAdapter.listing_tasks`, enqueued only by `crawl_seed --listings`)
is the only way to reach a leak nobody can search for. site13.example is the case that
forced this: its only usable search is a substring match over *uploader handles*, while our
targets are performers named in the *title* of somebody else's upload — every search correctly
answered "no such uploader", zero pages. Its catalogue, on the other hand, shows 50 card titles per
request across a few thousand pages.

So a walk reads the cards and enqueues a detail task only for those whose visible card text
plausibly names a creator we know; the rest are never fetched. Two pure rules, no I/O, no clock:

- **The gate is the `linked` rule of ADR 0029, applied one step earlier.** `TitleGate.admits`
  matches with `crawler.sites.common.names_person`, the same whole-word, case- and
  separator-insensitive normalisation `crawler.services.match_creators` links a creator by (plus
  the separator-less handle spelling, so it is strictly the wider of the two). A card it admits is
  a page the crawler would link that creator to, and ADR 0029's active-page rule then keeps or
  deactivates the page exactly as it does for any other page. False positives are cheap — one
  fetched page about nobody; a false negative is a leak we never see, so the gate errs towards
  fetching: a card with no text at all is admitted.
- **One frontier per walk.** A chain of thousands of pages may not enter the queue at once, so `walk_links`
  keeps a single pagination link, the nearest page ahead (paginator order, which is the order
  adapters emit it in), and drops the paginator's jumps to the far end. The walk is then one
  pending task that re-creates its successor each time it is fetched: it advances exactly as far
  as the site's delay and daily budget allow, and the next day it carries on where it stopped
  instead of walking from page 1 again. The queue is the whole state — like `crawl_recheck`,
  which reads `availability_checked_at` rather than keeping a cursor of its own.

The walk a task belongs to is its start URL, kept in `CrawlTask.context` (`WALK_KEY`) and handed
down to each successor by `crawler.services.store_parse_result`; `crawler.services
.enqueue_listing_walks` starts one, and re-queues the frontier of a walk that died (skipped by
`deactivate_untargeted --stop-broad-listings`, or failed).
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from catalog.models import Creator
from crawler.models import CrawlTask
from crawler.sites.base import TaskSpec
from crawler.sites.common import creator_names, name_keys, names_person, normalize_name

# `CrawlTask.context` key: the walk's start URL, or absent for an ordinary task.
WALK_KEY = "listing_walk"
# Pagination links a walk keeps per page: the next one. See the module docstring.
PAGES_AHEAD = 1


@dataclass(frozen=True)
class TitleGate:
    """Whose names admit a listing card. Compiled once per run from the creators we know."""

    keys: tuple[str, ...]

    @classmethod
    def for_creators(cls, creators: Iterable[Creator]) -> "TitleGate":
        """The gate for these creators, their aliases included."""
        return cls(
            tuple(name_keys(name for creator in creators for name in creator_names(creator)))
        )

    def admits(self, text: str) -> bool:
        """Does this card text plausibly name one of them?

        `names_person` twice: once on the whole card text (its whole-word rule, the one the
        crawler links creators by) and once on each word of it, which is the same function's
        second case — a name written without separators, the way site handles spell it. A card
        reads `Handle - Title` on the sites this is for, so the handle is a word of a longer
        string rather than the whole of it, and "SashaMeyer - clip" must not be missed.

        Empty text is admitted: an adapter that declares `card_text` and hands back none has hit
        something unexpected, and a page fetched for nothing costs less than a leak missed. With
        no creators at all nothing is admitted — there is nobody to hunt for.
        """
        if not text.strip():
            return True
        return names_person(text, self.keys) or any(
            names_person(word, self.keys) for word in normalize_name(text).split()
        )


@dataclass(frozen=True)
class WalkPlan:
    """What one listing page of a walk contributes: the links to enqueue, and why."""

    links: tuple[TaskSpec, ...]
    cards: int  # detail links the page offered
    admitted: int  # of those, the ones the gate let through
    ahead: int  # pagination links kept (at most PAGES_AHEAD)


def walk_links(links: Iterable[TaskSpec], gate: TitleGate) -> WalkPlan:
    """The links of a walked listing page: gated details, and the next page only."""
    details: list[TaskSpec] = []
    ahead: list[TaskSpec] = []
    for spec in links:
        (details if spec.kind == CrawlTask.Kind.DETAIL else ahead).append(spec)
    admitted = [spec for spec in details if gate.admits(spec.text)]
    kept_ahead = ahead[:PAGES_AHEAD]
    return WalkPlan(
        links=(*admitted, *kept_ahead),
        cards=len(details),
        admitted=len(admitted),
        ahead=len(kept_ahead),
    )


def walk_context(walk: str) -> dict[str, object]:
    """The `CrawlTask.context` entry that makes a task part of this walk."""
    return {WALK_KEY: walk}


def walk_of(context: object) -> str | None:
    """The walk a task belongs to, or None for an ordinary task."""
    if not isinstance(context, Mapping):
        return None
    walk = context.get(WALK_KEY)
    return walk if isinstance(walk, str) and walk else None
