"""Catalog workflows."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from django.conf import settings
from django.db import transaction

from catalog import do_not_contact
from catalog.models import Creator, DoNotContactRecord, Site
from catalog.request_accounting import apply_policy_cdn_hosts
from catalog.sources import SITE_POLICIES, UNREVIEWED_POLICY, SourceList


@dataclass(frozen=True)
class SeedReport:
    sites_created: int
    creators_created: int


def effective_crawl_delay_s(site: Site) -> float:
    """Minimum seconds between requests: max of the site setting and robots.txt Crawl-delay."""
    return max(site.crawl_delay_s, site.robots_crawl_delay_s or 0.0)


def record_robots_crawl_delay(site: Site, delay_s: float | None) -> None:
    """Persist robots.txt's Crawl-delay so processes that don't read robots.txt honour it too."""
    if site.robots_crawl_delay_s != delay_s:
        site.robots_crawl_delay_s = delay_s
        Site.objects.filter(pk=site.pk).update(robots_crawl_delay_s=delay_s)


def seed_sources(sources: SourceList) -> SeedReport:
    """Upsert sites and creators. Reviewed policies are (re)applied; aliases are kept."""
    sites_created = creators_created = 0
    with transaction.atomic():
        for domain in sources.sites:
            policy = SITE_POLICIES.get(domain)
            defaults = {
                "enabled": (policy or UNREVIEWED_POLICY).enabled,
                "crawl_delay_s": (policy or UNREVIEWED_POLICY).crawl_delay_s,
                "notes": (policy or UNREVIEWED_POLICY).notes,
                "daily_request_budget": (policy or UNREVIEWED_POLICY).daily_request_budget,
                "daily_media_request_budget": (
                    policy or UNREVIEWED_POLICY
                ).daily_media_request_budget,
            }
            if policy is None:
                _, created = Site.objects.get_or_create(
                    domain=domain, defaults={"name": domain, **defaults}
                )
            else:
                site, created = Site.objects.update_or_create(
                    domain=domain, defaults=defaults, create_defaults={"name": domain, **defaults}
                )
                apply_policy_cdn_hosts(site, policy.cdn_hosts)
            sites_created += created
        for name in sources.creators:
            _, created = Creator.objects.get_or_create(name=name)
            creators_created += created
    return SeedReport(sites_created=sites_created, creators_created=creators_created)


# --- Do-not-contact record (issue #98, ADR 0047, docs/do-not-contact.md) ------------------------
#
# The two operations the record exists for: write one "no" down, and ask before writing to someone.
# The rules that turn what was typed into a hash are pure and live in `catalog/do_not_contact.py`;
# what needs Django — the secret and the table — is here.


def do_not_contact_secret() -> str:
    """The HMAC key, from the environment. Empty is a refusal, never an empty key.

    It is **never rotated** without a migration plan: every stored hash is keyed with it, and a new
    key silently makes every recorded "no" unfindable while the check goes on answering "allowed".
    See docs/do-not-contact.md, "The secret".
    """
    return str(getattr(settings, "DO_NOT_CONTACT_HMAC_KEY", "") or "")


def record_no_contact(
    *,
    kind: str,
    identifier: str,
    received_on: date,
    channel: str,
    recorded_by: str,
    creator: Creator | None = None,
    note: str = "",
    secret: str | None = None,
) -> tuple[DoNotContactRecord, bool]:
    """Write one "no" down. Returns (row, created); recording the same identifier twice is a no-op.

    `identifier` is hashed here and never stored, logged or returned. Raises `MissingSecret` when
    the key is unset and `UnusableIdentifier` when the input is not an identifier of that kind.
    """
    key = secret if secret is not None else do_not_contact_secret()
    hashed = do_not_contact.digest(kind, identifier, key)
    return DoNotContactRecord.objects.get_or_create(
        identifier_hash=hashed,
        defaults={
            "identifier_kind": kind,
            "received_on": received_on,
            "channel": channel,
            "recorded_by": recorded_by,
            "creator": creator,
            "note": note,
        },
    )


def contact_check(kind: str, identifiers: Sequence[str], secret: str | None = None) -> list[bool]:
    """May we contact each of these? `True` = blocked, by position. Identifiers are never stored.

    One query, whatever the number of inputs. Raises `MissingSecret` rather than answering with an
    empty key — a check that cannot see the records would wave everything through.
    """
    key = secret if secret is not None else do_not_contact_secret()
    candidates = do_not_contact.digests(kind, identifiers, key)
    recorded = DoNotContactRecord.objects.filter(identifier_hash__in=candidates).values_list(
        "identifier_hash", flat=True
    )
    return do_not_contact.blocked_positions(candidates, recorded)
