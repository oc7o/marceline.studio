"""Seed (or remove) a fully synthetic demo dataset for the customer app (docs/demo-dataset.md).

Everything here is invented for the purpose of showing the product without a single real piece of
data: a fictional organisation, fictional studios and creators, fictional sites on the reserved
`.example` TLD (RFC 2606 — guaranteed never to resolve), synthetic durations, dates and PDQ hashes.
**No pirated media is ever fetched or stored** — there is no `index.Media` row anywhere in here,
only text, dates and 32-byte hashes generated from `hashlib.sha256`, never from a real picture.

Two things keep this data from ever touching the real crawler once it exists in the database:

- every demo `Site` is created `enabled=False` with a zero request budget, so no worker claims a
  task for it;
- every demo `Creator` is created (and kept) `needs_review=True`. `catalog.Creator.objects.
  usable()` — the single read rule `marceline.targets.targets()` goes through — excludes it, so an
  owned original naming a demo creator as its rights holder never becomes a real crawl target
  (docs/suppression.md's neighbouring rule, reused here for the same reason: an unreviewed name
  must not steer the crawler). Never flip that flag for a demo creator.

The organisation is created `is_sandbox=True` (`accounts.Organisation.is_sandbox`, ADR 0035): it
can never mark a real notice as sent, and no real provider-cooperation statistics are ever fed by
it, whatever screen someone drives by hand.

Idempotent: run it as often as you like. Each entity is looked up by a fixed, deterministic
identity (organisation name, creator name, site domain, a Reference's (sha256, pipeline, fps))
before anything is created, and the multi-step "showcase" record's history is only ever replayed
once — on the run that first creates its page. `--remove` deletes everything this command owns, in
dependency order, and is itself idempotent (nothing to do on a second run).

Refuses on a production environment (`DJANGO_ENV=prod` or `MARCELINE_ROLE=prod`): this is
showcase data for the operator's own dev/demo environment, never something to run against the
public customer cache.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.utils import timezone

from accounts.models import Membership, Organisation
from catalog.models import Creator, Site
from index.models import Page
from marceline.app.cases import sync_cases
from marceline.app.ownership import OwnedOriginal, Via, owned_originals, owner_of, register
from marceline.notices.models import NoticeDraft, NoticeStatus, ProviderResponseOutcome
from matching.copy_extent import CLASSIFIER_VERSION
from matching.creators import set_creators
from matching.fingerprint import PIPELINE
from matching.models import Reference, ReferenceFrameHash, ReferenceMatch

ORG_NAME = "Northlight Demo Studio"
DEMO_USERNAME = "demo@marceline.studio"

# Three fictional production studios (rights holders) and five fictional individual creators
# (co-performers). Every name here is invented; none names a real person or company.
STUDIO_NAMES = (
    "Northlight Frame Studio",
    "Driftwood Media Collective",
    "Paper Lantern Productions",
)
CREATOR_NAMES = (
    "Wren Ashcombe",
    "Sage Marlowe",
    "Isla Thorncroft",
    "Rowan Delacroix",
    "Juniper Vale",
)
DEMO_CREATOR_NAMES = STUDIO_NAMES + CREATOR_NAMES

# Reserved for documentation and examples (RFC 2606 / RFC 6761): `.example` never resolves.
SITE_DOMAINS = (
    "clipvault.example",
    "mirrorden.example",
    "streamcache.example",
    "fanbin.example",
)

FRAME_QUALITY = 90
SOURCE_NOTE = "Synthetic demo original — no real media (docs/demo-dataset.md)."
SITE_NOTE = "Synthetic demo site — never crawled, never resolves (docs/demo-dataset.md)."


def fake_sha256(label: str) -> str:
    """A deterministic, obviously-synthetic digest: never a real file's hash."""
    return hashlib.sha256(f"demo-dataset:{label}".encode()).hexdigest()


def fake_pdq(label: str, index: int) -> bytes:
    """A deterministic 32-byte value in the shape of a PDQ hash — never computed from a picture."""
    return hashlib.sha256(f"demo-dataset:{label}:{index}".encode()).digest()


def ago(days: float) -> datetime:
    return timezone.now() - timedelta(days=days)


@dataclass(frozen=True)
class FindingSpec:
    """One matching copy to place on a fictional site, at a fixed confirmed state."""

    site_domain: str
    path: str
    title: str
    availability: str
    checked_days_ago: float
    confirmed_days_ago: float
    match_fields: dict[str, Any] = field(default_factory=dict)


class Command(BaseCommand):
    help = (
        "Seed a fully synthetic demo dataset for the customer app: one organisation, three "
        "studios, five creators, fictional .example sites and matching copies in varied states "
        "(docs/demo-dataset.md). Idempotent; refuses on a production environment. --remove undoes "
        "it."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--remove", action="store_true", help="Remove the demo dataset instead of seeding it."
        )
        parser.add_argument("--dry-run", action="store_true", help="Roll everything back.")

    def handle(self, *args: Any, **options: Any) -> None:
        if settings.DJANGO_ENV == "prod" or settings.MARCELINE_ROLE == "prod":
            raise CommandError(
                "refusing to run on a production environment (DJANGO_ENV="
                f"{settings.DJANGO_ENV!r}, MARCELINE_ROLE={settings.MARCELINE_ROLE!r}); "
                "this command is for a dev/demo environment only"
            )
        with transaction.atomic():
            if options["remove"]:
                self.remove()
            else:
                self.seed()
            if options["dry_run"]:
                transaction.set_rollback(True)
                self.stdout.write("dry run: rolled back, nothing written")

    # -- seeding -------------------------------------------------------------------------------

    def seed(self) -> None:
        organisation = self.organisation()
        self.user(organisation)
        studios = {name: self.creator(name) for name in STUDIO_NAMES}
        creators = {name: self.creator(name) for name in CREATOR_NAMES}
        sites = {domain: self.site(domain) for domain in SITE_DOMAINS}

        northlight, driftwood, paper_lantern = (studios[name] for name in STUDIO_NAMES)
        wren, sage, isla, rowan, juniper = (creators[name] for name in CREATOR_NAMES)

        showcase = self.reference(
            organisation,
            slug="rehearsal-reel-spring",
            title="Studio Rehearsal Reel — Spring Collection",
            duration_s=642.0,
            primary=northlight,
            cast=(wren,),
        )
        online_only = self.reference(
            organisation,
            slug="product-launch-bts",
            title="Behind the Scenes: Product Launch Day",
            duration_s=1150.0,
            primary=northlight,
            cast=(),
        )
        removed_only = self.reference(
            organisation,
            slug="morning-routine-ep12",
            title="Morning Routine Vlog — Episode 12",
            duration_s=483.0,
            primary=driftwood,
            cast=(sage,),
        )
        mixed = self.reference(
            organisation,
            slug="dance-practice-take3",
            title="Dance Practice Session, Take 3",
            duration_s=275.0,
            primary=driftwood,
            cast=(isla, rowan),
        )
        under_review = self.reference(
            organisation,
            slug="podcast-outtakes",
            title="Podcast Outtakes Compilation",
            duration_s=812.0,
            primary=paper_lantern,
            cast=(juniper,),
        )
        # No page is ever created for it: an original with nothing found yet.
        self.reference(
            organisation,
            slug="autumn-photoshoot-diary",
            title="Autumn Photoshoot BTS Diary",
            duration_s=390.0,
            primary=paper_lantern,
            cast=(wren, juniper),
        )

        # One finding, online, no removal: the plain "confirmed" state.
        self.findings(
            organisation,
            online_only,
            [
                FindingSpec(
                    site_domain="streamcache.example",
                    path="clips/product-launch-bts",
                    title="Behind the Scenes: Product Launch Day (re-upload)",
                    availability=Page.Availability.PLAYABLE,
                    checked_days_ago=2,
                    confirmed_days_ago=15,
                )
            ],
        )

        # One finding, removed and never back: "removed", handled.
        self.findings(
            organisation,
            removed_only,
            [
                FindingSpec(
                    site_domain="fanbin.example",
                    path="v/morning-routine-ep12",
                    title="Morning Routine Vlog Ep. 12",
                    availability=Page.Availability.REMOVED,
                    checked_days_ago=4,
                    confirmed_days_ago=20,
                )
            ],
        )

        # Two findings on one record: one plain online copy, one that was removed and came back
        # (a re-upload) — the case itself never fully clears because the first copy stays up.
        self.findings(
            organisation,
            mixed,
            [
                FindingSpec(
                    site_domain="clipvault.example",
                    path="videos/dance-practice-take3",
                    title="Dance Practice Session, Take 3",
                    availability=Page.Availability.PLAYABLE,
                    checked_days_ago=3,
                    confirmed_days_ago=17,
                )
            ],
        )
        self.reappearing_finding(
            organisation,
            mixed,
            site=sites["mirrorden.example"],
            path="mirror/dance-practice-take3",
            title="Dance Practice Session, Take 3 (mirror)",
            removed_days_ago=14,
            reappeared_days_ago=6,
        )

        # A proposed, unconfirmed candidate: nothing a customer ever sees (findings_for only
        # counts a confirmed decision), but real staff-review-queue-shaped data underneath.
        self.candidate(
            organisation,
            under_review,
            site=sites["fanbin.example"],
            path="clips/podcast-outtakes-maybe",
            title="possible podcast outtakes clip",
        )

        # `monitoring_only` gets no page at all: an original with nothing found yet.

        # The showcase record: online -> removed -> reappeared -> notice drafted, sent,
        # acknowledged -> removed again. Ends with every timeline step done and a real removal
        # history (reopened once). See docs/demo-dataset.md "The showcase record".
        self.showcase(organisation, showcase, sites["clipvault.example"])

        self.stdout.write(self.style.SUCCESS("demo dataset seeded"))

    def organisation(self) -> Organisation:
        organisation, created = Organisation.objects.get_or_create(
            name=ORG_NAME,
            defaults={"segment": Organisation.Segment.PROFESSIONAL, "is_sandbox": True},
        )
        if created:
            self.stdout.write(f"created organisation {organisation.pk}: {ORG_NAME}")
        elif not organisation.is_sandbox:
            organisation.is_sandbox = True
            organisation.save(update_fields=["is_sandbox"])
        return organisation

    def user(self, organisation: Organisation) -> User:
        user, created = User.objects.get_or_create(
            username=DEMO_USERNAME, defaults={"email": DEMO_USERNAME}
        )
        if created:
            self.stdout.write(f"created user {DEMO_USERNAME} (no usable password)")
        Membership.objects.get_or_create(
            user=user, defaults={"organisation": organisation, "role": Membership.Role.OWNER}
        )
        return user

    def creator(self, name: str) -> Creator:
        creator, created = Creator.objects.get_or_create(name=name, defaults={"needs_review": True})
        if created:
            self.stdout.write(f"created creator {creator.pk}: {name}")
        elif not creator.needs_review:
            # Never let a demo name become a real crawl target (marceline.targets.targets()
            # excludes needs_review creators through Creator.objects.usable()).
            creator.needs_review = True
            creator.save(update_fields=["needs_review"])
        return creator

    def site(self, domain: str) -> Site:
        site, created = Site.objects.get_or_create(
            domain=domain,
            defaults={
                "name": domain.split(".")[0].title(),
                "enabled": False,
                "daily_request_budget": 0,
                "daily_media_request_budget": 0,
                "notes": SITE_NOTE,
            },
        )
        if created:
            self.stdout.write(f"created site {site.pk}: {domain} (disabled)")
        elif site.enabled:
            site.enabled = False
            site.daily_request_budget = 0
            site.daily_media_request_budget = 0
            site.save(
                update_fields=["enabled", "daily_request_budget", "daily_media_request_budget"]
            )
        return site

    def reference(
        self,
        organisation: Organisation,
        *,
        slug: str,
        title: str,
        duration_s: float,
        primary: Creator,
        cast: tuple[Creator, ...],
    ) -> Reference:
        sha256 = fake_sha256(slug)
        reference, created = Reference.objects.get_or_create(
            sha256=sha256,
            pipeline=PIPELINE,
            frame_fps=1.0,
            defaults={
                "creator": primary,
                "title": title,
                "duration_s": duration_s,
                "source_note": SOURCE_NOTE,
                "client": {"source": "seed_demo_dataset"},
            },
        )
        if created:
            self.stdout.write(f"created original {reference.pk}: {title}")
        set_creators(reference, [primary, *cast])
        if not reference.frames.exists():
            span = max(int(duration_s), 1)
            positions = sorted(
                {0, span // 5, 2 * span // 5, 3 * span // 5, 4 * span // 5, span - 1}
            )
            ReferenceFrameHash.objects.bulk_create(
                [
                    ReferenceFrameHash(
                        reference=reference,
                        frame_index=position,
                        timestamp_s=float(position),
                        pdq=fake_pdq(slug, position),
                        quality=FRAME_QUALITY,
                    )
                    for position in positions
                ]
            )
        if owner_of(reference.pk) is None:
            register(reference, organisation, via=Via.COMMAND)
        return reference

    def original_of(self, organisation: Organisation, reference: Reference) -> OwnedOriginal:
        return next(o for o in owned_originals(organisation) if o.pk == reference.pk)

    def page(
        self, site: Site, *, path: str, title: str, availability: str, checked_at: datetime
    ) -> tuple[Page, bool]:
        url = f"https://{site.domain}/{path}"
        return Page.objects.get_or_create(
            url=url,
            defaults={
                "site": site,
                "title": title,
                "safety_score": 1.0,
                "availability": availability,
                "availability_checked_at": checked_at,
            },
        )

    def confirmed_match(
        self,
        reference: Reference,
        page: Page,
        *,
        confirmed_at: datetime,
        **fields: Any,
    ) -> tuple[ReferenceMatch, bool]:
        """A `ReferenceMatch` row, confirmed and human-decided by default — pass `status=` /
        `band=` to build the one unconfirmed candidate instead (see `candidate()`)."""
        existing = ReferenceMatch.objects.filter(reference=reference, page=page).first()
        if existing is not None:
            return existing, False
        values: dict[str, Any] = {
            "best_distance": 6,
            "matched_frames": 5,
            "page_frames": 8,
            "evidence_frames": 4,
            "best_reference_timestamp_s": min(30.0, (reference.duration_s or 60.0) / 3),
            "confidence": 0.93,
            "band": ReferenceMatch.Band.HIGH,
            "strength": ReferenceMatch.Strength.STRONG,
            "status": ReferenceMatch.Status.CONFIRMED,
            "decided_by": ReferenceMatch.DecidedBy.HUMAN,
            **fields,
        }
        match = ReferenceMatch.objects.create(reference=reference, page=page, **values)
        # `created_at`/`updated_at` are auto-managed on `save()`; backdate them once, directly in
        # the database, so the demo's confirmation date is the story's, not "whenever this ran".
        ReferenceMatch.objects.filter(pk=match.pk).update(
            created_at=confirmed_at, updated_at=confirmed_at
        )
        return match, True

    def findings(
        self, organisation: Organisation, reference: Reference, specs: list[FindingSpec]
    ) -> None:
        """Confirmed matching copies for one original, reconciled into its record in one go."""
        any_created = False
        latest_check: datetime | None = None
        for spec in specs:
            site = Site.objects.get(domain=spec.site_domain)
            page, _ = self.page(
                site,
                path=spec.path,
                title=spec.title,
                availability=spec.availability,
                checked_at=ago(spec.checked_days_ago),
            )
            _, created = self.confirmed_match(
                reference, page, confirmed_at=ago(spec.confirmed_days_ago), **spec.match_fields
            )
            any_created = any_created or created
            check_at = ago(spec.checked_days_ago)
            latest_check = check_at if latest_check is None else max(latest_check, check_at)
        if any_created and latest_check is not None:
            sync_cases([self.original_of(organisation, reference)], now=latest_check)

    def reappearing_finding(
        self,
        organisation: Organisation,
        reference: Reference,
        *,
        site: Site,
        path: str,
        title: str,
        removed_days_ago: float,
        reappeared_days_ago: float,
    ) -> None:
        """A copy that was removed and came back online — a re-upload, seen from the record."""
        removed_at = ago(removed_days_ago)
        reappeared_at = ago(reappeared_days_ago)
        page, page_created = self.page(
            site,
            path=path,
            title=title,
            availability=Page.Availability.REMOVED,
            checked_at=removed_at,
        )
        if not page_created:
            return  # already seeded on an earlier run
        _, match_created = self.confirmed_match(reference, page, confirmed_at=removed_at)
        if not match_created:
            return
        original = self.original_of(organisation, reference)
        sync_cases([original], now=removed_at)
        page.availability = Page.Availability.PLAYABLE
        page.availability_checked_at = reappeared_at
        page.save(update_fields=["availability", "availability_checked_at"])
        sync_cases([original], now=reappeared_at)

    def candidate(
        self, organisation: Organisation, reference: Reference, *, site: Site, path: str, title: str
    ) -> None:
        """A proposed, unconfirmed match: never a customer finding (`findings_for` needs a
        confirmed decision), real backstage review-queue data."""
        page, _ = self.page(
            site,
            path=path,
            title=title,
            availability=Page.Availability.UNKNOWN,
            checked_at=ago(1),
        )
        self.confirmed_match(
            reference,
            page,
            confirmed_at=ago(1),
            status=ReferenceMatch.Status.PROPOSED,
            band=ReferenceMatch.Band.REVIEW,
            decided_by="",
        )

    def showcase(self, organisation: Organisation, reference: Reference, site: Site) -> None:
        """One record whose history exercises every timeline step (docs/demo-dataset.md)."""
        path = "originals/rehearsal-reel-spring"
        url = f"https://{site.domain}/{path}"
        if Page.objects.filter(url=url).exists():
            return  # already told, on an earlier run
        found_at = ago(42)
        removed_at = ago(33)
        reappeared_at = ago(25)
        prepared_at = ago(18)
        sent_at = ago(12)
        acknowledged_at = ago(7)
        removed_again_at = ago(5)

        page = Page.objects.create(
            site=site,
            url=url,
            title="Studio Rehearsal Reel — Spring Collection (full upload)",
            safety_score=1.0,
            availability=Page.Availability.PLAYABLE,
            availability_checked_at=found_at,
        )
        self.confirmed_match(
            reference,
            page,
            confirmed_at=found_at,
            copy_extent="full_copy",
            copy_extent_reason="full_span",
            copy_span_s=(reference.duration_s or 0) * 0.92,
            copy_coverage=0.92,
            copy_extent_version=CLASSIFIER_VERSION,
        )
        original = self.original_of(organisation, reference)
        sync_cases([original], now=found_at)

        page.availability = Page.Availability.REMOVED
        page.availability_checked_at = removed_at
        page.save(update_fields=["availability", "availability_checked_at"])
        sync_cases([original], now=removed_at)

        page.availability = Page.Availability.PLAYABLE
        page.availability_checked_at = reappeared_at
        page.save(update_fields=["availability", "availability_checked_at"])
        sync_cases([original], now=reappeared_at)

        approved_text = (
            f"Notice draft for '{reference.title}', prepared for {site.domain} "
            "(synthetic demo record, docs/demo-dataset.md)."
        )
        NoticeDraft.objects.create(
            organisation=organisation,
            is_sandbox=organisation.is_sandbox,
            language="en",
            rendered_variant="dsa_art16",
            recipient_hostname=site.domain,
            recipient_provider_name=f"{site.name} hosting",
            channel_kind="abuse_email",
            recipient_email=f"abuse@{site.domain}",
            sha256=reference.sha256,
            original_public_id=original.public_id,
            work_title=reference.title,
            claimant_name=organisation.name,
            claimant_capacity="owner",
            claimant_email="rights@northlight.example",
            licensed_partner_check_confirmed=True,
            exception_or_fair_use_confirmed=True,
            approved_text=approved_text,
            approved_text_sha256=hashlib.sha256(approved_text.encode()).hexdigest(),
            approved_at=prepared_at,
            marked_sent_at=sent_at,
            responded_at=acknowledged_at,
            outcome=ProviderResponseOutcome.REMOVED,
            status=NoticeStatus.PROVIDER_RESPONDED,
            created_at=prepared_at,
        )

        page.availability = Page.Availability.REMOVED
        page.availability_checked_at = removed_again_at
        page.save(update_fields=["availability", "availability_checked_at"])
        sync_cases([original], now=removed_again_at)

    # -- removal ---------------------------------------------------------------------------------

    def remove(self) -> None:
        organisation = Organisation.objects.filter(name=ORG_NAME).first()
        if organisation is None:
            self.stdout.write("nothing to remove: no demo organisation")
            return
        drafts = NoticeDraft.objects.filter(organisation=organisation).delete()
        references = Reference.objects.filter(creator__name__in=DEMO_CREATOR_NAMES)
        reference_count = references.count()
        references.delete()
        pages = Page.objects.filter(site__domain__in=SITE_DOMAINS)
        page_count = pages.count()
        pages.delete()
        sites = Site.objects.filter(domain__in=SITE_DOMAINS)
        site_count = sites.count()
        sites.delete()
        creators = Creator.objects.filter(name__in=DEMO_CREATOR_NAMES)
        creator_count = creators.count()
        creators.delete()
        Membership.objects.filter(organisation=organisation).delete()
        User.objects.filter(username=DEMO_USERNAME).delete()
        organisation.delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"removed demo dataset: organisation {ORG_NAME}, {reference_count} originals, "
                f"{page_count} pages, {site_count} sites, {creator_count} creators, "
                f"{drafts[0]} notice-related rows"
            )
        )
