"""Prospect research (docs/prospect-research.md): attribution, distinct counting, the sample."""

import json
from datetime import date, timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from catalog.models import Creator, HostingRecord, Site, WatchlistEntry
from crawler.models import CrawlTask
from index.models import Media, Page, ProspectSampleCheck
from marceline.prospects import (
    NEVER_IN_AN_APPROACH,
    AttributedPage,
    Blocker,
    Claim,
    ResearchBasis,
    SampleCheck,
    Strength,
    ambiguous_names,
    attributed_pages,
    attribution_strength,
    build_claim,
    headline,
    prospect_report,
    sample_page_ids,
    tally,
)
from matching.models import Cluster, ClusterMember

pytestmark = pytest.mark.django_db


def anna() -> Creator:
    creator = Creator.objects.create(name="Anna Berg", aliases=["Berg Studio"])
    WatchlistEntry.objects.create(
        creator=creator, crawl=True, purpose=WatchlistEntry.Purpose.PROSPECT
    )
    return creator


def page(
    site: Site,
    slug: str,
    *,
    title: str = "",
    tags: tuple[str, ...] = (),
    performers: tuple[str, ...] = (),
    linked: Creator | None = None,
    **fields: object,
) -> Page:
    created = Page.objects.create(
        site=site,
        url=f"https://{site.domain}/videos/{slug}/",
        title=title or slug,
        tags=list(tags),
        raw_extraction={"performers": list(performers), "duration_s": 60},
        **fields,
    )
    if linked is not None:
        created.creators.add(linked)
    return created


def cluster(*pages: Page, status: str = ClusterMember.Status.PROPOSED) -> Cluster:
    made = Cluster.objects.create()
    for member in pages:
        ClusterMember.objects.create(
            cluster=made, page=member, status=status, evidence=ClusterMember.Evidence.PDQ
        )
    return made


# --- attribution strength (pure) ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"performers": ["Anna Berg"]}, Strength.STRONG),
        ({"tags": ["vlog", "berg-studio"]}, Strength.STRONG),  # studio name in aliases
        ({"title": "Anna Berg in the kitchen"}, Strength.TITLE),
        ({"title": "Best of Anna Berg, Mia and Lea"}, Strength.TITLE),  # compilation: title only
        ({"title": "Annaberg Buchholz travel vlog"}, None),  # whole words only
        ({"on_profile_page": True}, Strength.STRONG),
        ({"title": "unrelated", "tags": [1, None]}, None),
    ],
)
def test_attribution_strength(fields: dict[str, object], expected: Strength | None) -> None:
    creator = Creator(name="Anna Berg", aliases=["Berg Studio"])
    arguments: dict[str, object] = {
        "title": "",
        "tags": [],
        "performers": [],
        "on_profile_page": False,
        **fields,
    }
    assert attribution_strength(creator, **arguments) == expected  # type: ignore[arg-type]


def test_attributed_pages_grades_linked_and_profile_page_pages_and_skips_flagged() -> None:
    creator = anna()
    site = Site.objects.create(domain="site02.example")
    credited = page(site, "credited", performers=("Anna Berg",), linked=creator)
    titled = page(site, "titled", title="Anna Berg teaser", linked=creator)
    page(site, "flagged", performers=("Anna Berg",), linked=creator, safety_status="blocked")
    # Listed on her profile page (the adapter's LISTING seed) and its page 2, without her name.
    seed = CrawlTask.objects.create(
        site=site, url="https://site02.example/artist/anna-berg", kind=CrawlTask.Kind.LISTING
    )
    second = CrawlTask.objects.create(
        site=site,
        url="https://site02.example/artist/anna-berg/2",
        kind=CrawlTask.Kind.LISTING,
        discovered_from=seed,
    )
    listed = page(site, "listed")
    CrawlTask.objects.create(
        site=site, url=listed.url, kind=CrawlTask.Kind.DETAIL, discovered_from=second
    )
    # A "related profile" link on her page leads to someone else's catalogue: not hers.
    other = CrawlTask.objects.create(
        site=site,
        url="https://site02.example/artist/mia-else",
        kind=CrawlTask.Kind.LISTING,
        discovered_from=seed,
    )
    foreign = page(site, "foreign")
    CrawlTask.objects.create(
        site=site, url=foreign.url, kind=CrawlTask.Kind.DETAIL, discovered_from=other
    )

    pages, flagged = attributed_pages(creator)

    assert {(p.page_id, p.strength) for p in pages} == {
        (credited.pk, Strength.STRONG),
        (titled.pk, Strength.TITLE),
        (listed.pk, Strength.STRONG),
    }
    assert flagged == 1


# --- distinct counting (pure) ------------------------------------------------------------------


def attributed(
    pk: int,
    site: str = "a.com",
    *,
    cluster_id: int | None = None,
    photo: bool = False,
    strength: Strength = Strength.STRONG,
    availability: str = Page.Availability.PLAYABLE,
) -> AttributedPage:
    now = timezone.now()
    return AttributedPage(
        pk, site, strength, photo, cluster_id, availability, now - timedelta(days=pk), now
    )


def test_tally_counts_clusters_once_and_unclustered_pages_each() -> None:
    pages = [
        attributed(1, "a.com", cluster_id=7),
        attributed(2, "b.com", cluster_id=7),
        attributed(3, "c.com", cluster_id=8),
        attributed(4, "a.com", availability=Page.Availability.REMOVED),
        attributed(5, "a.com", photo=True),
        attributed(6, "b.com", photo=True, cluster_id=9),
        attributed(7, "c.com", photo=True, cluster_id=9),
    ]
    # Cluster 7 has two more copies nobody attributed; they still count as copies.
    result = tally(pages, {7: 4, 8: 1, 9: 2}, {})

    assert result.pages == 7
    assert result.distinct_videos == 3  # cluster 7, cluster 8, page 4
    assert result.photo_sets == 2  # cluster 9, page 5
    assert (result.copies_per_video_median, result.copies_per_video_max) == (1.0, 4)
    assert result.sites == 3
    assert result.pages_by_site == {"a.com": 3, "b.com": 2, "c.com": 2}
    assert (result.playable, result.removed, result.share_online) == (6, 1, round(6 / 7, 3))
    assert result.sites_by_jurisdiction == {"unknown": 3}


def test_a_cluster_with_a_video_page_is_a_video_not_a_photo_set() -> None:
    result = tally([attributed(1, cluster_id=3), attributed(2, cluster_id=3, photo=True)], {}, {})
    assert (result.distinct_videos, result.photo_sets) == (1, 0)


# --- the claim gate -----------------------------------------------------------------------------

AS_OF = date(2026, 9, 18)


def claim_for(
    pages: list[AttributedPage],
    sample: SampleCheck,
    *,
    basis: ResearchBasis = ResearchBasis.LEGAL_PERSON,
    ambiguous: tuple[str, ...] = (),
) -> Claim:
    return build_claim(
        tally(pages, {}, {}), sample, as_of=AS_OF, basis=basis, ambiguous=list(ambiguous)
    )


def two_sites() -> list[AttributedPage]:
    return [attributed(1, "a.com"), attributed(2, "b.com")]


def test_a_number_is_refused_by_default_and_every_reason_is_named() -> None:
    claim = claim_for(two_sites(), SampleCheck(0, [1, 2], unchecked=[1, 2]))
    assert claim.blockers == [Blocker.SAMPLE_UNCHECKED, Blocker.PRECISION_UNKNOWN]
    assert headline(claim) == "no defensible number — sample_unchecked, precision_unknown"

    # No stated research basis is itself a blocker: this is the default of the command.
    unstated = claim_for(
        two_sites(), SampleCheck(0, [1, 2], correct=2), basis=ResearchBasis.NOT_STATED
    )
    assert Blocker.RESEARCH_BASIS_NOT_STATED in unstated.blockers
    assert "12" not in headline(unstated) and "2 site" not in headline(unstated)


@pytest.mark.parametrize(
    "pages, sample, ambiguous, expected",
    [
        ([], SampleCheck(0, []), (), Blocker.NO_STRONG_ATTRIBUTION),
        (
            [attributed(1, "a.com"), attributed(2, "a.com")],
            SampleCheck(0, [1, 2], correct=2),
            (),
            Blocker.ONE_SITE_ONLY,
        ),
        (two_sites(), SampleCheck(0, [1, 2], correct=2), ("Mia",), Blocker.NAME_NOT_DISTINCTIVE),
        (
            two_sites(),
            SampleCheck(0, [1, 2], correct=1, wrong=1),
            (),
            Blocker.PRECISION_BELOW_FLOOR,
        ),
    ],
)
def test_each_gate_blocks_on_its_own(
    pages: list[AttributedPage],
    sample: SampleCheck,
    ambiguous: tuple[str, ...],
    expected: Blocker,
) -> None:
    claim = claim_for(pages, sample, ambiguous=ambiguous)
    assert expected in claim.blockers
    assert not claim.usable
    assert "no defensible number" in headline(claim)


def test_a_measured_60_percent_is_reported_and_still_refused() -> None:
    """The brief's case: if the sample says 60 %, the briefing says so — and blocks the claim."""
    claim = claim_for(two_sites(), SampleCheck(0, list(range(10)), correct=6, wrong=4))
    assert claim.precision == 0.6
    assert claim.blockers == [Blocker.PRECISION_BELOW_FLOOR]


def test_a_passing_claim_carries_approximately_the_as_of_date_and_the_precision() -> None:
    claim = claim_for(two_sites(), SampleCheck(0, [1, 2], correct=2))
    assert claim.usable
    assert headline(claim) == (
        "approximately 2 distinct video(s) with matching copies on 2 site(s), "
        "as of 2026-09-18 (sample precision 100% on 2 of 2 sampled pages checked by hand; "
        "not verified as theirs and not verified as unauthorised)"
    )
    assert "at least" not in headline(claim)


def test_no_headline_can_state_a_count_without_its_precision_and_date() -> None:
    """Structural, not stylistic: there is no branch that formats the number on its own."""
    samples = [
        SampleCheck(0, []),
        SampleCheck(0, [1, 2], unchecked=[1, 2]),
        SampleCheck(0, [1, 2], correct=1, wrong=1),
        SampleCheck(0, [1, 2], correct=2),
        SampleCheck(0, [1, 2], correct=1, unsure=1),
    ]
    for pages in ([], [attributed(1, "a.com")], two_sites()):
        for sample in samples:
            for basis in ResearchBasis:
                line = headline(claim_for(pages, sample, basis=basis))
                if "no defensible number" in line:
                    continue
                assert "sample precision" in line and f"as of {AS_OF.isoformat()}" in line


def test_ambiguous_names_flags_short_tokens_and_names_we_already_know() -> None:
    assert ambiguous_names(["Mia"], []) == ["Mia"]
    assert ambiguous_names(["Anna Berg"], []) == []
    assert ambiguous_names(["Bergsted"], []) == []
    # A name another creator we may use already carries: two people, one stage name.
    assert ambiguous_names(["Anna Berg"], ["anna berg", "Someone Else"]) == ["Anna Berg"]


# --- sample -------------------------------------------------------------------------------------


def test_sample_is_deterministic_for_a_seed_and_mostly_stable_as_pages_arrive() -> None:
    ids = list(range(1, 201))
    first = sample_page_ids(ids, seed=42)

    assert len(first) == 10
    assert set(first) <= set(ids)
    assert sample_page_ids(reversed(ids), seed=42) == first
    assert sample_page_ids(ids, seed=43) != first
    grown = sample_page_ids([*ids, *range(201, 211)], seed=42)
    assert len(set(grown) & set(first)) >= 5
    assert sample_page_ids([3, 1, 2], seed=0) == [1, 2, 3]


# --- the report -------------------------------------------------------------------------------


def build_world() -> tuple[Creator, list[Page]]:
    creator = anna()
    eu = Site.objects.create(domain="eu-video.test")
    us = Site.objects.create(domain="us-video.test")
    HostingRecord.objects.create(
        hostname="eu-video.test",
        status="complete",
        asn=24940,
        country="DE",
        looked_up_at=timezone.now(),
    )
    HostingRecord.objects.create(
        hostname="us-video.test",
        status="complete",
        asn=13335,
        country="US",
        looked_up_at=timezone.now(),
    )
    strong = [
        page(eu, f"s{i}", performers=("Anna Berg",), linked=creator, availability="playable")
        for i in range(12)
    ]
    copy = page(us, "copy", tags=("Berg Studio",), linked=creator, availability="removed")
    cluster(strong[0], copy, page(us, "unattributed-copy"))
    photo = page(eu, "photo", tags=("anna berg",), linked=creator)
    Media.objects.create(page=photo, kind=Media.Kind.PHOTO, url="https://cdn.eu-video.test/p.jpg")
    page(us, "title-only", title="Anna Berg lookalike", linked=creator)
    return creator, [*strong, copy, photo]


def test_prospect_report_claim_loose_totals_hosting_and_sample_precision() -> None:
    creator, strong_pages = build_world()
    first = prospect_report(creator, seed=7, basis=ResearchBasis.LEGAL_PERSON)
    sample = first.sample.page_ids
    assert len(sample) == 10
    assert set(sample) <= {p.pk for p in strong_pages}
    ProspectSampleCheck.objects.create(creator=creator, page_id=sample[0], verdict="correct")
    ProspectSampleCheck.objects.create(creator=creator, page_id=sample[1], verdict="wrong")
    ProspectSampleCheck.objects.create(creator=creator, page_id=sample[2], verdict="unsure")

    report = prospect_report(creator, seed=7, basis=ResearchBasis.LEGAL_PERSON)

    # 12 strong video pages + 1 copy in one cluster with page 0 -> 12 videos; 1 photo post.
    assert (report.strong.distinct_videos, report.strong.photo_sets) == (12, 1)
    assert report.strong.sites == 2 and report.strong.hosters == 2
    assert report.strong.sites_by_jurisdiction == {"eu": 1, "us": 1}
    assert report.strong.copies_per_video_max == 3
    assert (report.loose.distinct_videos, report.loose.pages) == (13, 15)
    assert report.title_only_by_site == {"us-video.test": 1}
    # Seven of the ten sampled pages carry no verdict, so there is no number at all — not the
    # loose total, not the strong one, not with a caveat.
    assert report.claim.blockers == [Blocker.SAMPLE_UNCHECKED, Blocker.PRECISION_BELOW_FLOOR]
    assert report.headline == ("no defensible number — sample_unchecked, precision_below_floor")
    assert report.claim.as_of == timezone.now().date().isoformat()
    assert report.sample.page_ids == sample  # same seed, same sample
    assert report.purpose == "prospect" and report.crawl

    for page_id in report.sample.unchecked:
        ProspectSampleCheck.objects.create(creator=creator, page_id=page_id, verdict="correct")
    passed = prospect_report(creator, seed=7, basis=ResearchBasis.LEGAL_PERSON)
    assert passed.claim.usable
    assert passed.headline.startswith(
        "approximately 12 distinct video(s) and 1 photo set(s) with matching copies on 2 site(s)"
    )
    assert "sample precision 89% on 9 of 10 sampled pages checked by hand" in passed.headline


def test_command_text_and_json_carry_ids_domains_and_counts_only() -> None:
    creator, _ = build_world()
    WatchlistEntry.objects.create(
        creator=Creator.objects.create(name="Other Prospect"),
        purpose=WatchlistEntry.Purpose.PROSPECT,
    )
    text = StringIO()
    call_command("prospect_report", all_prospects=True, stdout=text)
    output = text.getvalue()

    assert "Prospect: Anna Berg (watchlist prospect, crawl on)" in output
    assert "Prospect: Other Prospect (watchlist prospect, crawl off)" in output
    assert "eu-video.test: 13 / 0" in output
    assert "https://" not in output and "lookalike" not in output
    # No --basis, so the default refuses before anything else is even weighed.
    assert "research basis stated: not_stated" in output
    assert "Claim: no defensible number — research_basis_not_stated" in output

    data = StringIO()
    call_command("prospect_report", creators=[creator.name], format="json", stdout=data)
    [report] = json.loads(data.getvalue())
    assert report["strong"]["distinct_videos"] == 12
    assert len(report["sample"]["page_ids"]) == 10
    assert report["sample"]["precision"] is None
    assert report["claim"]["usable"] is False
    assert "research_basis_not_stated" in report["claim"]["blockers"]
    assert "https://" not in data.getvalue()


def test_the_briefing_refuses_an_approach_and_names_what_would_have_to_change() -> None:
    creator, _ = build_world()
    out = StringIO()
    call_command("prospect_report", creators=[creator.name], briefing=True, stdout=out)
    output = out.getvalue()

    assert "OUTREACH BRIEFING — Anna Berg" in output
    assert "NO APPROACH." in output
    assert "research_basis_not_stated: state the basis with --basis" in output
    assert "sample_unchecked: record a verdict for every sampled page id" in output
    # The briefing is the page that could survive being read by the prospect: counts only.
    assert "eu-video.test" not in output and "us-video.test" not in output
    assert "Verification sample (seed" not in output  # the id list is research, not a briefing
    assert "https://" not in output and "lookalike" not in output
    # The words of the intermediate register appear only in the list of words we may not use.
    without_the_prohibition = "\n".join(
        line
        for line in output.split("\n")
        if not any(item in line for item in NEVER_IN_AN_APPROACH)
    )
    for forbidden in ("leak", "infring", "stolen", "pirated", "illegal"):
        assert forbidden not in without_the_prohibition.lower()


def test_a_passing_briefing_states_the_one_sentence_with_its_date_and_precision() -> None:
    creator, _ = build_world()
    report = prospect_report(creator, seed=7)
    for page_id in report.sample.page_ids:
        ProspectSampleCheck.objects.create(creator=creator, page_id=page_id, verdict="correct")
    out = StringIO()
    call_command(
        "prospect_report",
        creators=[creator.name],
        briefing=True,
        seed=7,
        basis="legal_person",
        stdout=out,
    )
    output = out.getvalue()

    assert "An approach may be prepared." in output
    assert "approximately 12 distinct video(s) and 1 photo set(s)" in output
    today = timezone.now().date().isoformat()
    assert f"As of {today}, on the sites we monitor we identified approximately" in output
    assert "We reviewed the stored text of 10 of those pages by hand; 10 appeared" in output
    assert "not verified findings" in output
    assert "we have not confirmed that the content is theirs" in output
    assert "May not be used without their prior express consent" in output
    assert "Art. 14 GDPR information" in output


def test_command_needs_a_creator_or_all_prospects() -> None:
    with pytest.raises(CommandError):
        call_command("prospect_report", stdout=StringIO())
    with pytest.raises(CommandError, match="Nobody"):
        call_command("prospect_report", creators=["Nobody"], stdout=StringIO())
