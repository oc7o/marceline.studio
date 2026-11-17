"""Parse `sources.md` (sites and creators) and the reviewed crawl policy per known site."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

AGE_CONFIRMATION_HELP = (
    "Operator approval to send this site's own age self-declaration (ADR 0007, 2026-09-15). "
    'Only for "I am old enough" click-throughs: the cookie the site itself sets when "yes" is clicked. '
    "Never for age verification providers (third-party services, "
    "ID/selfie/credit-card checks), logins, CAPTCHAs or paywalls; those pages are always skipped. "
    "Not needed for client-side overlays when the server already sends the full page. Format: "
    '{"cookies": {"age_verified": "1"}, "approved_by": "operator", '
    '"approved_at": "2026-09-15", "note": "why"}. Leave empty for no approval.'
)
MAX_AGE_COOKIES = 5
_COOKIE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_COOKIE_VALUE = re.compile(r"^[A-Za-z0-9_.:-]{0,128}$")
# Cookies that carry sessions, logins or anti-bot clearance are never an age self-declaration.
_FORBIDDEN_COOKIE_PARTS = (
    "sess",
    "auth",
    "token",
    "login",
    "member",
    "user",
    "pass",
    "cf_",
    "__cf",
)


class AgeConfirmationError(ValueError):
    """`Site.age_confirmation` is present but not a valid operator approval."""


@dataclass(frozen=True)
class AgeConfirmation:
    """An operator-approved age self-declaration: cookies the site sets when "yes" is clicked."""

    cookies: Mapping[str, str]
    approved_by: str
    approved_at: str
    note: str = ""


def parse_age_confirmation(value: Any) -> AgeConfirmation | None:
    """None for no approval (empty value); raises `AgeConfirmationError` when malformed."""
    if value in (None, {}, ""):
        return None
    if not isinstance(value, dict):
        raise AgeConfirmationError("must be a JSON object")
    unknown = value.keys() - {"cookies", "approved_by", "approved_at", "note"}
    if unknown:
        raise AgeConfirmationError(f"unknown keys: {', '.join(sorted(unknown))}")
    cookies = value.get("cookies")
    if not isinstance(cookies, dict) or not cookies:
        raise AgeConfirmationError('"cookies" must be a non-empty object of name: value')
    if len(cookies) > MAX_AGE_COOKIES:
        raise AgeConfirmationError(f'at most {MAX_AGE_COOKIES} "cookies"')
    for name, cookie_value in cookies.items():
        if not _COOKIE_NAME.match(name) or not isinstance(cookie_value, str):
            raise AgeConfirmationError(f"invalid cookie {name!r}")
        if not _COOKIE_VALUE.match(cookie_value):
            raise AgeConfirmationError(f"invalid value for cookie {name!r}")
        if any(part in name.lower() for part in _FORBIDDEN_COOKIE_PARTS):
            raise AgeConfirmationError(
                f"cookie {name!r} looks like a session, login or anti-bot cookie; not allowed"
            )
    approved_by = value.get("approved_by")
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise AgeConfirmationError('"approved_by" is required')
    approved_at = value.get("approved_at")
    if not isinstance(approved_at, str):
        raise AgeConfirmationError('"approved_at" (ISO date) is required')
    try:
        datetime.fromisoformat(approved_at)
    except ValueError as exc:
        raise AgeConfirmationError('"approved_at" must be an ISO date') from exc
    note = value.get("note", "")
    if not isinstance(note, str):
        raise AgeConfirmationError('"note" must be text')
    return AgeConfirmation(dict(cookies), approved_by.strip(), approved_at, note)


#: Requests per rolling 24 h to one CDN host, across every site it serves (ADR 0007 amendment
#: 2026-09-17). Higher than origin budgets: a CDN is built to serve media, the site's own server
#: is not. The largest CDN host measured on 2026-09-16 took 19,445 media requests in the day
#: (fastdelivery.site02.example); override per host in the admin (Site hosts).
CDN_HOST_DAILY_REQUEST_BUDGET = 25_000


@dataclass(frozen=True)
class SitePolicy:
    # Reviewed *crawl* policy, re-applied wholesale by `seed_sources` on every run of
    # `sources.md`. A field that must stay whatever an operator sets in the admin between runs
    # (e.g. `Site.age_confirmation`, `Site.capture_enabled`, `Site.capture_play_click`, issue #78)
    # does not belong here —
    # `seed_sources` would silently stomp it back to this dict's value on the next reseed.
    enabled: bool
    crawl_delay_s: float
    notes: str
    # Real requests per rolling 24 h to the site's own (origin) hosts, per kind (ADR 0007
    # amendment 2026-09-17): pages and robots.txt, and media served from the origin, each with
    # their retries and redirect hops. Two different things: `crawl_delay_s` is the request *rate*
    # a site's operator sees and judges us by, a budget is only how long we keep going each day.
    daily_request_budget: int = 1000
    daily_media_request_budget: int = 1000
    # Subdomains of the site that are really a CDN (they resolve to another network than the
    # site): their requests count against the CDN host budget, not the origin budgets.
    cdn_hosts: tuple[str, ...] = ()


_KVS_ENABLED_NOTE = "Enabled 2026-09-15: KVS template adapter; discovered via source discovery"
_DISCOVERED_ENABLED_NOTE = "Enabled 2026-09-15: operator approved; discovered via source discovery"

# Result of the reconnaissance. Unknown sites stay disabled until reviewed.
# Delays (2026-09-16): 3 s where robots.txt declares no Crawl-delay, which `effective_delay_s`
# still overrides upward wherever a site does declare one (site03: 10 s). Budgets were the real
# limit before this — five sites sat exactly at 1000/1000 fetches per day and then idled, at 6 %
# of what the delay alone allows. Raise budgets before delays: the budget is invisible to the
# site, the delay is the rate its operator sees. No 429, 403 or challenge in the 7 days before
# this change; revisit downward at the first sign of one.
#
# Budgets count real requests since 2026-09-17 (issue #74, ADR 0007 amendment); before, they
# counted page tasks and missed robots.txt, retries, redirect hops and every media request. They
# keep each site's actual origin load where it was, in real requests, measured on 2026-09-16 (the
# last whole day; every page budget was used up):
# - page budget: the old page-task budget + 10 % for robots.txt, retries and redirect hops,
#   rounded up to 500;
# - media budget: the media fetched from the site's own hosts that day + 10 %, rounded up to 500,
#   at least 1,000.
#
# Raised 2026-09-18 (operator): eleven sites sat at exactly 100 % of their page budget for a whole
# day, so coverage was limited by our own policy rather than by the machine or by any site. Pages
# tripled (3,000 -> 9,000; site04 5,500 -> 15,000) on the ten capped sites, and origin media
# with them where the media really comes from the site's own hosts. A budget is not a rate: the
# delay a site's operator sees is unchanged, we simply keep going for more of the day. The ceiling
# is the delay itself - one origin host slot is shared by pages, robots.txt and media, so at 3 s a
# site's own hosts allow ~28,800 requests a day and these budgets sit near 18,000 of it.
# Deliberately not raised: the seven sites that never reached their old budget (raising them
# changes nothing), site02's pages while 2,009 of 5,500 requests are 4xx on deleted videos, and
# site17.example's media while 1,266 of 3,146 fetches are 4xx. No 429, 403 or challenge on any site
# in the 24 h before this change; revisit downward at the first sign of one.
# Media on `cdn_hosts` and on other hosts counts against that host's CDN budget
# (`CDN_HOST_DAILY_REQUEST_BUDGET`). `cdn_hosts` lists only subdomains that resolved to another
# network than the site itself on 2026-09-17. Comments give the 2026-09-16 numbers.
SITE_POLICIES: dict[str, SitePolicy] = {
    "site01.example": SitePolicy(
        True,
        3.0,
        "KVS CMS, no Cloudflare; robots.txt disallows /embed/ and /player/.",
        3000,
        4500,  # 4,010 media on site01.example
    ),
    "site02.example": SitePolicy(
        True,
        3.0,
        "Clean HTML, /artist/ pages, 10 hover frames/video.",
        5500,
        1000,
        # 19,445 media on fastdelivery.site02.example (another network; the site is behind Cloudflare)
        cdn_hosts=("fastdelivery.site02.example",),
    ),
    "site03.example": SitePolicy(
        True,
        10.0,
        "robots.txt Crawl-delay 10; tokenized media URLs expire quickly.",
        4500,  # 2,174 of 4,000 pages used; media on a third-party CDN
        1000,
    ),
    "site04.example": SitePolicy(
        True,
        3.0,
        "Enabled: mirror of site02.example with performer-rich titles and search.",
        15000,
        1000,  # 4,033 media on a third-party CDN
    ),
    "site07.example": SitePolicy(
        True,
        3.0,
        "Enabled 2026-09-16 with the adapter (#11): performer pages and the home listing. "
        "Search stays unused, robots.txt disallows it; metadata is thin (no duration, no date).",
        3000,
        3000,  # 2,362 media on site07.example
    ),
    "site15.example": SitePolicy(
        False, 5.0, "Disabled: Cloudflare 403 for every request. Do not bypass."
    ),
    # KVS sites. robots.txt sets no Crawl-delay on any of them.
    # site19: 8,995 media on imgcdn.site19.example (a CDN; the site is behind Cloudflare)
    "site19.example": SitePolicy(
        True, 3.0, _KVS_ENABLED_NOTE, 9000, 1000, cdn_hosts=("imgcdn.site19.example",)
    ),
    "site18.example": SitePolicy(True, 3.0, _KVS_ENABLED_NOTE, 3000, 3000),  # 2,274 on www.
    "site22.example": SitePolicy(True, 3.0, _KVS_ENABLED_NOTE, 9000, 9000),  # 8,530 on www.
    "site25.example": SitePolicy(
        False,
        5.0,
        "Disabled 2026-09-15: every page loads a third-party age-verification checker; not "
        "bypassed. KVS; discovered via source discovery.",
    ),
    # Own adapters. robots.txt sets no Crawl-delay on either.
    # site05: 2,765 media on static-eu-cdn.site05.example (another network than the site's own)
    "site05.example": SitePolicy(
        True, 3.0, _DISCOVERED_ENABLED_NOTE, 9000, 1000, cdn_hosts=("static-eu-cdn.site05.example",)
    ),
    # site06: media on a third-party CDN
    "site06.example": SitePolicy(True, 3.0, _DISCOVERED_ENABLED_NOTE, 9000, 1000),
    "site11.example": SitePolicy(
        True,
        3.0,
        "Enabled 2026-09-16: bespoke adapter, the first photo-first source (#69). Seeds are "
        "profile URLs; search is POST-only and unusable, so a creator whose slug carries a -N "
        "suffix needs the operator to supply it. Photo posts are stored as PHOTO (#69, index "
        "migration 0008); the poster of a /video/new/ post stays POSTER.",
        9000,
        # 1,771 media on site11.example + 615 on n1.site11.example (same network as the site);
        # cdn.site11.example resolves elsewhere
        9000,
        cdn_hosts=("cdn.site11.example",),
    ),
    "site10.example": SitePolicy(
        True,
        3.0,
        "Enabled 2026-09-16: bespoke adapter, search-only seeds (a name query returns that "
        "performer's own catalogue, so no slug guessing and no tag-page collision). No age gate. "
        "Thin media: lazy cards hide their thumbnail, so usually only the page's AVIF poster "
        "hashes - one image per video.",
        9000,
        1000,  # 174 media on site10 hosts, 2,403 on nine third-party CDN hosts
    ),
    "site09.example": SitePolicy(
        True,
        3.0,
        "Enabled 2026-09-16: bespoke adapter; indexed by FanHub handle, which is exactly the "
        "seed shape we want. No age gate, robots.txt is fully permissive. One WebP frame per "
        "video, so a page alone never reaches the high band (needs two evidence frames).",
        3000,
        1000,
        # 1,609 media on image-cdn.site09.example (resolves outside the site's network)
        cdn_hosts=("image-cdn.site09.example",),
    ),
    "site08.example": SitePolicy(
        True,
        3.0,
        "Enabled 2026-09-16: KVS install with its own adapter (screenshots tab); no age gate. "
        "Search seeds only, the model slug is not derivable from a creator name.",
        9000,
        9000,  # 5,010 media on site08.example
    ),
    "site20.example": SitePolicy(
        True,
        3.0,
        "Enabled 2026-09-16: KVS template instance; no age gate at all. The theme has no "
        "pagination, so depth comes from the number of seeds, not from paging.",
        3000,  # 1,180 of 2,500 pages used
        2500,  # 2,232 media on site20.example
    ),
    "site17.example": SitePolicy(
        True,
        3.0,
        "Enabled 2026-09-16: KVS template instance; no age gate at all (not even the stock KVS "
        "click-through). Seeds use the www host; search paginates with ?from_videos=N.",
        9000,
        # Media held at 5,500: 1,266 of 3,146 fetches were 4xx on 2026-09-17 (40 % rot).
        5500,  # 4,668 media on site17.example
    ),
    "site21.example": SitePolicy(
        True,
        3.0,
        "Enabled 2026-09-16: KVS template instance; no age gate at all. Pagination is plain "
        "paths (/search/{slug}/N/); ?from_videos=N silently serves page 1. robots.txt bars "
        "several named AI crawlers, none of them MarcelineBot, so the * group applies.",
        9000,
        9000,  # 4,432 media on site21.example
    ),
    "site12.example": SitePolicy(
        False,
        5.0,
        "On hold 2026-09-17 (operator): adapter ready, not crawled. "
        "robots.txt allows everything we fetch, no age "
        "gate or challenge. To enable: set this policy to True, seed_sources, move the "
        "crawler-site12/media-site12 compose services from profile hold to workers and add "
        "them to WORKERS in the Makefile.",
        1000,
        1000,
        # Card and player thumbnails come from tn1.site12.example, a CNAME to a third-party CDN on
        # another network than the site itself.
        cdn_hosts=("tn1.site12.example",),
    ),
    "site13.example": SitePolicy(
        True,
        5.0,
        "Onboarded 2026-09-17 (operator go-ahead, candidate review issue #23/#25): bespoke "
        "adapter, enabled 2026-09-17 with a conservative budget until request_report shows "
        "what it really uses. robots.txt sets no "
        "Crawl-delay and allows everything the adapter fetches (creator catalogues, /videos, "
        "/media/videos/); no age gate or challenge. The site's own <form method=post> search "
        "is unusable (a GET with the same query is accepted but ignored, confirmed by "
        "comparing two different queries); seeding instead uses the real GET search "
        "(?q= on the creator index), a substring match over creator handles, since handles use three "
        "unpredictable separator styles (lena-h, kira_r, ninaashton) and are never "
        "guessed from a creator's name. All media seen "
        "is on the site's own host; no CDN subdomain identified yet. Page budget raised to 9,000 "
        "on 2026-09-18 because breadth now has a filter (ADR 0029 amendment): those handle "
        "searches were correct and still stored nothing, so the site is reached through its "
        "catalogue instead — a listing page costs one request and shows 50 card titles, of which "
        "the title gate admits the few that name a creator we search for. The budget therefore buys "
        "listing pages rather than irrelevant detail pages: a day of it covers the whole "
        "catalogue of a few thousand pages (a few hours at the 5 s delay) with ~5,700 requests "
        "left for the details the gate admitted. The media budget stays at 1,000 — it is what "
        "those details really cost in pictures.",
        9000,
        1000,
    ),
    "site14.example": SitePolicy(
        True,
        5.0,
        "Adapter built 2026-09-18 (crawl coverage push), site enabled 2026-09-18 by operator "
        "decision after review. German skin of a network of sites, bespoke adapter. "
        "robots.txt sets no "
        "Crawl-delay and disallows only /fast-report*, which the adapter never touches; no age "
        "gate, challenge or login on any of the 9 recon responses. "
        "Seeds are per creator: the /performer/{slug}/ page "
        "and the real GET search /suchen/?q=. To enable: set this to True, run seed_sources, "
        "move crawler-site14/media-site14 from compose profile hold to workers and "
        "add them to WORKERS in the Makefile. Budgets are the starting numbers of an unmeasured "
        "site (1,000/1,000, as site12.example): one origin request buys 16 cards, so the first day "
        "already covers ~16,000 cards, and request_report says afterwards what it really uses. "
        "Origin media stays near zero — every picture (card frames and the video poster) is on "
        "the network's pics.network.example hosts, which are no subdomain of this site and count "
        "against the CDN host budget instead.",
        1000,
        1000,
    ),
    "site16.example": SitePolicy(
        True,
        5.0,
        "Adapter built 2026-09-18 (crawl coverage push), site enabled 2026-09-18 by operator "
        "decision after review. KVS template instance under a language prefix (/de/ — seeds, "
        "video and model "
        "pages all carry it; the apex serves the original language). robots.txt is the stock KVS "
        "one: no Crawl-delay, and the paths it disallows (/embed/, /login/, /captcha/, the async "
        "block endpoints) are those the template already excludes. No age gate or challenge on "
        "any of the 9 recon responses. To enable: set "
        "this to True, run seed_sources, move crawler-site16/media-site16 "
        "from compose profile hold to workers and add them to WORKERS in the Makefile. Budgets "
        "are the starting numbers of an unmeasured site (1,000/1,000). Media is the heavier half "
        "here and all of it is on the site's own host: a video page yields up to ten screenshot "
        "frames, so 1,000 media requests are ~100 fully fingerprinted pages a day until "
        "request_report shows what the site actually costs.",
        1000,
        1000,
    ),
}
UNREVIEWED_POLICY = SitePolicy(False, 5.0, "Not reviewed yet: enable after reconnaissance.")


@dataclass(frozen=True)
class SourceList:
    sites: tuple[str, ...]
    creators: tuple[str, ...]


def normalize_domain(value: str) -> str:
    value = value.strip()
    host = urlsplit(value if "://" in value else f"https://{value}").hostname or ""
    return host.lower().removeprefix("www.")


def parse_sources(text: str) -> SourceList:
    """Read `Sites:` and `Creators:` bullet lists; everything else is ignored."""
    sections: dict[str, list[str]] = {"sites": [], "creators": []}
    current: list[str] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.endswith(":"):
            current = sections.get(line[:-1].strip().lower())
            continue
        if current is None or not line.startswith(("- ", "* ")):
            continue
        item = line[2:].strip()
        if current is sections["sites"]:
            item = normalize_domain(item)
        if item and item not in current:
            current.append(item)
    return SourceList(sites=tuple(sections["sites"]), creators=tuple(sections["creators"]))
