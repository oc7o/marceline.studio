"""`seed_demo_dataset`: the fully synthetic showcase dataset for the customer app.

Three things matter here beyond "it creates data": it must never run where a real customer or
crawler could be affected (`DJANGO_ENV`/`MARCELINE_ROLE=prod`, and every demo creator kept
`needs_review=True` so `marceline.targets.targets()` never seeds a real site for a fictional
name), it must be safe to run more than once, and `--remove` must really undo it.
"""

from io import StringIO

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from pytest_django.fixtures import Settings

from accounts.models import Membership, Organisation
from catalog.models import Creator, Site
from index.models import Media, Page
from marceline.app.cases import case_views, timeline_of
from marceline.app.ownership import owned_originals
from marceline.management.commands.seed_demo_dataset import (
    DEMO_CREATOR_NAMES,
    DEMO_USERNAME,
    ORG_NAME,
    SITE_DOMAINS,
)
from marceline.notices.models import NoticeDraft
from marceline.targets import targets
from matching.models import Reference

SHOWCASE_TITLE = "Studio Rehearsal Reel — Spring Collection"


def run(*args: str) -> str:
    out = StringIO()
    call_command("seed_demo_dataset", *args, stdout=out)
    return out.getvalue()


def test_seeds_the_organisation_studios_creators_and_sites(db: None) -> None:
    run()

    organisation = Organisation.objects.get(name=ORG_NAME)
    assert organisation.is_sandbox, "never a real notice send or provider statistic"
    user = User.objects.get(username=DEMO_USERNAME)
    membership = Membership.objects.get(user=user)
    assert membership.organisation == organisation

    creators = Creator.objects.filter(name__in=DEMO_CREATOR_NAMES)
    assert creators.count() == len(DEMO_CREATOR_NAMES) == 8
    assert all(creator.needs_review for creator in creators), (
        "a demo creator must never become a real crawl target"
    )

    sites = Site.objects.filter(domain__in=SITE_DOMAINS)
    assert sites.count() == len(SITE_DOMAINS) == 4
    assert all(not site.enabled for site in sites)
    assert all(site.daily_request_budget == 0 for site in sites)
    for domain in SITE_DOMAINS:
        assert domain.endswith(".example"), "reserved, never-resolving (RFC 2606)"


def test_demo_creators_are_never_real_crawl_targets(db: None) -> None:
    run()

    hunted_names = {target.creator.name for target in targets()}

    assert hunted_names.isdisjoint(DEMO_CREATOR_NAMES)


def test_originals_and_matching_copies_are_owned_by_the_demo_organisation(db: None) -> None:
    run()

    organisation = Organisation.objects.get(name=ORG_NAME)
    originals = owned_originals(organisation)
    assert len(originals) == 6

    references = Reference.objects.filter(pk__in=[o.pk for o in originals])
    assert references.count() == 6
    assert all((reference.title or "").strip() != "" for reference in references)


def test_the_showcase_record_exercises_every_timeline_step(db: None) -> None:
    run()

    organisation = Organisation.objects.get(name=ORG_NAME)
    showcase = Reference.objects.get(title=SHOWCASE_TITLE)
    [original] = [o for o in owned_originals(organisation) if o.pk == showcase.pk]
    [view] = case_views([original])

    assert view.case is not None
    assert view.case.reopen_count == 1, "a real removal history: removed, back, removed again"
    assert view.case.reopened_at is not None
    assert view.case.removed_at is not None

    steps = {step.step: step.status for step in timeline_of(view)}
    assert steps == {
        "confirmed": "done",
        "notice_prepared": "done",
        "notice_sent": "done",
        "notice_acknowledged": "done",
        "removed": "done",
        "rechecked": "done",
    }

    draft = NoticeDraft.objects.get(organisation=organisation, sha256=showcase.sha256)
    assert draft.is_sandbox, "the demo organisation's drafts must never look real"
    assert draft.approved_at is not None
    assert draft.marked_sent_at is not None
    assert draft.responded_at is not None


def test_no_media_of_any_kind_is_ever_created(db: None) -> None:
    run()

    demo_pages = Page.objects.filter(site__domain__in=SITE_DOMAINS)
    assert demo_pages.exists()
    assert not Media.objects.filter(page__in=demo_pages).exists()


def test_running_it_twice_changes_nothing(db: None) -> None:
    run()
    before = {
        "organisations": Organisation.objects.count(),
        "creators": Creator.objects.filter(name__in=DEMO_CREATOR_NAMES).count(),
        "sites": Site.objects.filter(domain__in=SITE_DOMAINS).count(),
        "pages": Page.objects.filter(site__domain__in=SITE_DOMAINS).count(),
        "references": Reference.objects.filter(creator__name__in=DEMO_CREATOR_NAMES).count(),
        "drafts": NoticeDraft.objects.count(),
    }
    showcase = Reference.objects.get(title=SHOWCASE_TITLE)
    organisation = Organisation.objects.get(name=ORG_NAME)
    [original] = [o for o in owned_originals(organisation) if o.pk == showcase.pk]
    [view_before] = case_views([original])
    reopen_count_before = view_before.case.reopen_count if view_before.case else None

    run()

    after = {
        "organisations": Organisation.objects.count(),
        "creators": Creator.objects.filter(name__in=DEMO_CREATOR_NAMES).count(),
        "sites": Site.objects.filter(domain__in=SITE_DOMAINS).count(),
        "pages": Page.objects.filter(site__domain__in=SITE_DOMAINS).count(),
        "references": Reference.objects.filter(creator__name__in=DEMO_CREATOR_NAMES).count(),
        "drafts": NoticeDraft.objects.count(),
    }
    assert after == before
    [view_after] = case_views([original])
    reopen_count_after = view_after.case.reopen_count if view_after.case else None
    assert reopen_count_after == reopen_count_before, "a rerun must not replay the showcase story"


def test_dry_run_writes_nothing(db: None) -> None:
    output = run("--dry-run")

    assert "rolled back" in output
    assert not Organisation.objects.filter(name=ORG_NAME).exists()
    assert not Creator.objects.filter(name__in=DEMO_CREATOR_NAMES).exists()
    assert not Site.objects.filter(domain__in=SITE_DOMAINS).exists()


def test_refuses_on_django_env_prod(db: None, settings: Settings) -> None:
    settings.DJANGO_ENV = "prod"

    with pytest.raises(CommandError, match="production"):
        run()

    assert not Organisation.objects.filter(name=ORG_NAME).exists()


def test_refuses_on_marceline_role_prod(db: None, settings: Settings) -> None:
    settings.MARCELINE_ROLE = "prod"

    with pytest.raises(CommandError, match="production"):
        run()

    assert not Organisation.objects.filter(name=ORG_NAME).exists()


def test_remove_undoes_the_whole_dataset(db: None) -> None:
    run()

    output = run("--remove")

    assert "removed demo dataset" in output
    assert not Organisation.objects.filter(name=ORG_NAME).exists()
    assert not User.objects.filter(username=DEMO_USERNAME).exists()
    assert not Creator.objects.filter(name__in=DEMO_CREATOR_NAMES).exists()
    assert not Site.objects.filter(domain__in=SITE_DOMAINS).exists()
    assert not Page.objects.filter(site__domain__in=SITE_DOMAINS).exists()
    assert not Reference.objects.filter(creator__name__in=DEMO_CREATOR_NAMES).exists()
    assert not NoticeDraft.objects.exists()


def test_remove_is_idempotent(db: None) -> None:
    run()
    run("--remove")

    output = run("--remove")

    assert "nothing to remove" in output


def test_remove_without_a_seed_is_a_noop(db: None) -> None:
    output = run("--remove")

    assert "nothing to remove" in output
