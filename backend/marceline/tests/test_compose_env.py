"""The settings the customer app depends on reach the container that serves it.

A setting can exist in three places and still do nothing. On 2026-09-18 nobody could sign in to the
customer app because ``DJANGO_CSRF_TRUSTED_ORIGINS`` was read by ``settings.py``, documented in
``.env.example`` and set in the operator's ``.env`` — and never listed in ``infra/compose.yaml``.
Containers only see the variables the compose file names (``.env`` is not mounted), so it was empty
inside ``web``, Django compared the relayed ``Origin`` against an empty list, and **every** POST
from the customer app failed the CSRF check. Nothing failed, anywhere: the value was right in every
file a person would look at.

That class of bug is invisible and silent, so it gets a test rather than a habit. A general rule
(every variable ``settings.py`` reads must be passed to ``web``) is not usable here — 73 of the 84
are for crawlers, discovery, billing or mail and deliberately stay out of that container, and an
exception list that long says nothing. So this pins the short list the *customer request path*
depends on, in all three places, and adds the one general direction that is cheap and exact: a
variable ``web`` refers to must be a variable ``.env.example`` documents, or the operator is
setting a name nothing reads.

No YAML parser: PyYAML is not a backend dependency, and the file's shape (two-space services,
six-space environment keys) is stable and checked here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE = REPO_ROOT / "infra" / "compose.yaml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
SETTINGS = REPO_ROOT / "backend" / "marceline" / "settings.py"

#: What the customer app's own requests need from the ``web`` container, and why.
#:
#: Each of these decides whether a request relayed by the BFF (``frontend/customer/lib/django.ts``)
#: is accepted, or whether the session it creates works. Add one when a new customer-facing setting
#: joins that path; do not add tuning knobs — a wrong value there is visible, a missing passthrough
#: is not.
CUSTOMER_PATH_SETTINGS = {
    # Django compares the relayed Origin with this list. Empty = every customer write is 403.
    "DJANGO_CSRF_TRUSTED_ORIGINS": "the Origin the BFF forwards is checked against it",
    # `Host` is a forbidden header for `fetch`, so Django sees the internal service name.
    "DJANGO_ALLOWED_HOSTS": "the relay reaches Django as `web:8000`, which has to be allowed",
    # ADR 0028: which surface this process answers at all.
    "DJANGO_PUBLIC_SURFACE": "decides whether /api/v1/app/* is served",
    # ADR 0023: the BFF forwards X-Forwarded-For unchanged so the per-IP limit sees the customer.
    "LOGIN_TRUSTED_PROXIES": "0 makes the login limit count the Next container, not the customer",
    "SESSION_COOKIE_SECURE": "the customer session cookie the login sets",
    "CSRF_COOKIE_SECURE": "the csrftoken cookie the BFF hands back to the browser",
}


#: What the crawl worker needs to decide whether it may spend money on an LLM (issue #22,
#: ADR 0042), and why each one has to reach the container.
#:
#: This is the mirror image of the customer-path list above: there a missing passthrough left a
#: feature silently *off*. Here the dangerous direction is the same but the stakes are money —
#: ``CRAWLER_LLM_REPAIR_ENABLED`` defaults to false, so a missing passthrough would look like
#: "the operator turned it on and nothing happened", and the key and the cap would be empty and
#: default in a container nobody can see into.
CRAWLER_LLM_SETTINGS = {
    "CRAWLER_LLM_REPAIR_ENABLED": "the switch; empty = the repair never runs, whatever .env says",
    "OPENROUTER_API_KEY": "without it the repair is refused as unconfigured",
    "CRAWLER_LLM_MODEL": "no model is pinned in code — picking one would be picking a price",
    "CRAWLER_LLM_REPAIR_DAILY_CALLS_PER_SITE": "the per-site daily cost cap",
}

#: The per-site crawler anchor (`&crawler`), which every other `crawler-*` service inherits.
CRAWLER_ANCHOR_SERVICE = "crawler-site01"
#: The crawl runtime's shared environment block (ADR 0030), which the pages lane merges in. It is
#: a YAML anchor rather than a service, so it is read separately — a merge key is invisible to the
#: line-based reader above, and asserting on the lane alone would silently pass for ever.
RUNTIME_ENV_ANCHOR = "x-runtime-env"

#: What the **scheduler** container needs for the daily safety digest (docs/safety.md).
#:
#: The digest is the only mail Marceline sends on a timer, and it is sent by this one container.
#: A missing passthrough here is the same silent failure as the customer one above: the hour would
#: read as its code default forever, whatever `.env` says.
SCHEDULER_MAIL_SETTINGS = {
    "SAFETY_DIGEST_HOUR_UTC": "the UTC hour the one consolidated safety mail goes out at",
    # Issue #107: the catalogue-share monitoring threshold. The `catalogue_share` job runs in this
    # one container, so a missing passthrough would pin it to its code default for ever — and the
    # number is one an operator is likely to want to change. Being silently wrong here is quiet in both directions: too high and nothing is ever
    # reported, too low and the mail is ignored.
    "CATALOGUE_SHARE_ALERT_PERCENT": "the share of a site's catalogue that mails the operator",
}


#: What the **scheduler** needs to delete a customer's data after their pilot (ADR 0045, #85).
#:
#: The fourth instance of the same silent gap, and the one where being silently *off* is a broken
#: promise to a customer rather than a missing feature: the flag defaults to false, so a missing
#: passthrough would look exactly like "the operator turned deletion on and nothing was deleted",
#: and the window would read as its code default whatever `.env` says.
CUSTOMER_DELETION_SETTINGS = {
    "CUSTOMER_DATA_DELETION_ENABLED": "the switch; empty = the job never deletes anything",
    "CUSTOMER_DATA_RETENTION_DAYS": "the 30-day window the operator promised (2026-09-18)",
}

#: What the **scheduler** needs to run safety-incident retention (docs/safety.md, issue #100).
#:
#: The same silent gap in the same guarded-deletion shape as PAGE_RETENTION_ENABLED and
#: CUSTOMER_DATA_DELETION_ENABLED above: the switch defaults to false, so a missing passthrough
#: would look exactly like "the operator turned it on and no case was ever closed or deleted",
#: whatever `.env` says. The two periods stay code constants (index.incidents), so there is no
#: second setting to pin here.
SAFETY_RETENTION_SETTINGS = {
    "SAFETY_RETENTION_ENABLED": "the switch; empty = no case is ever closed or deleted",
}


#: What the processes that refresh a site's robots.txt need to see a TDM/AI reservation (issue
#: #105), and why each one has to reach the container.
#:
#: The same silent gap as the lists above, with a legal edge: the tripwire exists to notice the day
#: a site reserves itself against us. A missing passthrough would leave detection reading its code
#: default in the container — and, on the day the operator decides to switch the pause on, would
#: leave the pause off for ever while `.env` says it is on.
ROBOTS_RESERVATION_SETTINGS = {
    "ROBOTS_RESERVATION_DETECTION_ENABLED": "records what a site's robots.txt reserves; no request",
    "ROBOTS_RESERVATION_PAUSE_ENABLED": "pauses a site whose robots.txt names our own user agent",
}


#: What the **scheduler** needs for retention on the research side (ADR 0048, issues #103, #104).
#:
#: The fifth instance of the same silent gap. The switch is dangerous in the quiet direction (off
#: looks like "the operator turned it on and no query text was ever blanked"),
#: and the three periods are dangerous in the loud one: a period this file
#: names to nobody reads as its code default, and the operator's confirmed number never applies.
RESEARCH_RETENTION_SETTINGS = {
    "RESEARCH_RETENTION_ENABLED": "the switch; empty = nothing is ever scrubbed or deleted",
    "SEARCH_QUERY_RETENTION_DAYS": "how long a query's text lives (the row is the spend ledger)",
    "PROSPECT_RESEARCH_RETENTION_DAYS": "how long prospect research lives (three months)",
    "CREATOR_NAME_RETENTION_DAYS": "how long an unused name lives",
}


#: What the **scheduler** needs to delete an invite that is never redeemed (ADR 0052 amendment
#: 2026-09-20).
#:
#: The sixth instance of the same silent gap: off looks like "the operator turned it on and no
#: abandoned invite was ever deleted", and the address of somebody who never became a customer
#: stays for ever while `.env` says it goes after thirty days.
INVITE_RETENTION_SETTINGS = {
    "INVITE_RETENTION_ENABLED": "the switch; empty = an unredeemed invite is never deleted",
    "ONBOARDING_INVITE_RETENTION_DAYS": "how long an invite lives after it stopped being usable",
}


#: What the **web** container needs for onboarding by invite (issue #115, ADR 0052).
#:
#: The same silent gap once more, and here it has two directions that both hurt. Silently *off*:
#: the operator issues an invite, sends the link, and every route answers the neutral 404 — which
#: is indistinguishable from an expired token, by design, so nothing would ever say why. And
#: silently *empty*: ``ONBOARDING_DOCUMENTS`` missing from the container reads exactly like "no
#: document is published", so the accept step refuses for ever however carefully ``.env`` names
#: the published versions. ``ONBOARDING_INVITE_BASE_URL`` is here because the link the staff
#: tooling prints is built from it in this container; empty means a link that points nowhere.
ONBOARDING_INVITE_SETTINGS = {
    "ONBOARDING_INVITES_ENABLED": "the switch; empty = every /api/v1/app/invite route is a 404",
    "ONBOARDING_INVITE_EXPIRY_DAYS": "how long an issued link stays usable",
    "ONBOARDING_INVITE_BASE_URL": "the link `invite_customer` prints; empty points nowhere",
    "ONBOARDING_DOCUMENTS": "empty reads as 'nothing published' and the accept step refuses",
}

#: What the processes that **send** a customer mail need (ADR 0054, issue #149).
#:
#: Two of them, for two reasons: `web` answers a reset asked for on development's own surface and
#: completes an invite (which asks for the address to be confirmed), and `export` runs
#: `pull_customer_writes`, so it is the process that sends the mail a request accepted on the public
#: host asked for. A container this file does not hand the switch to is silently off, and the
#: symptom is the one this whole table exists for: a creator who never receives the mail, and a
#: neutral answer that cannot say why.
#:
#: The SMTP settings themselves come from `x-mail-env`, which both services now merge — that is the
#: other half, and `test_account_mail_settings_reach_the_senders` checks it.
ACCOUNT_MAIL_SETTINGS = {
    "ACCOUNT_EMAIL_FLOWS_ENABLED": "the switch; empty = no reset and no verification mail",
    "ACCOUNT_MAIL_FROM": "the envelope sender; a role address of ours, never a personal one",
    "ACCOUNT_MAIL_REPLY_TO": "where a reply goes, and the address both mails name",
    "ACCOUNT_PASSWORD_RESET_TTL_MINUTES": "how long a reset link works",
    "ACCOUNT_VERIFICATION_TTL_HOURS": "how long a verification link works",
}
#: The processes that may send one of those two mails, and the only ones. Named apart from
#: `MAIL_SENDING_SERVICES` above, which is the operator report's trio: the two lists are different
#: questions about the same anchor, and one name for both would answer neither.
ACCOUNT_MAIL_SERVICES = ("web", "export")


#: What the **web** container needs for proof of control of a creator's platform account (ADR 0055).
#:
#: The same silent gap as the invite switch above, in the quiet direction: with the variable missing
#: from this file the container reads the code default (off) whatever `.env` says, every
#: /api/v1/app/verification route answers 404, and the creator sees a step that is simply not there
#: — while the operator is looking at a file that says it is on. The public host needs the same
#: value (`infra/prod/compose.yaml`), because the read travels in the signed bundle and the two
#: writes go through the outbox.
ACCOUNT_CONTROL_SETTINGS = {
    "ACCOUNT_CONTROL_VERIFICATION_ENABLED": "the switch; empty = every /api/v1/app/verification "
    "route is a 404 and no code can be issued",
}


def compose_text() -> str:
    return COMPOSE.read_text()


def service_block(name: str) -> str:
    """The lines of one compose service, by indentation.

    The heading may carry a YAML anchor (``crawler-site01: &crawler``), so it is matched rather
    than sliced on a fixed length.
    """
    text = compose_text()
    heading = re.search(rf"^  {re.escape(name)}:.*$", text, re.MULTILINE)
    assert heading is not None, f"infra/compose.yaml has no service `{name}`"
    rest = text[heading.end() + 1 :]
    end = re.search(r"^  \S", rest, re.MULTILINE)
    return rest[: end.start()] if end else rest


def environment_of(service: str) -> dict[str, str]:
    """The ``environment:`` mapping of one service: variable name -> the value compose sets."""
    block = service_block(service)
    start = block.index("    environment:\n") + len("    environment:\n")
    rest = block[start:]
    end = re.search(r"^    \S", rest, re.MULTILINE)
    body = rest[: end.start()] if end else rest
    return dict(re.findall(r"^      ([A-Z][A-Z0-9_]*):[ ]*(.*)$", body, re.MULTILINE))


def documented_in_env_example() -> set[str]:
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", ENV_EXAMPLE.read_text(), re.MULTILINE))


def read_by_settings() -> set[str]:
    return set(
        re.findall(
            r'env_(?:str|bool|int|list|float|path)\(\s*"([A-Z][A-Z0-9_]*)"', SETTINGS.read_text()
        )
    )


@pytest.mark.parametrize("name", sorted(CUSTOMER_PATH_SETTINGS))
def test_customer_path_setting_is_read_by_settings(name: str) -> None:
    assert name in read_by_settings(), (
        f"{name} is pinned as something the customer app needs, but settings.py does not read it"
    )


@pytest.mark.parametrize("name", sorted(CUSTOMER_PATH_SETTINGS))
def test_customer_path_setting_is_documented(name: str) -> None:
    assert name in documented_in_env_example(), (
        f"{name} is not in .env.example, so nobody knows to set it ({CUSTOMER_PATH_SETTINGS[name]})"
    )


@pytest.mark.parametrize("name", sorted(CUSTOMER_PATH_SETTINGS))
def test_customer_path_setting_reaches_the_web_container(name: str) -> None:
    environment = environment_of("web")
    assert name in environment, (
        f"infra/compose.yaml does not pass {name} into the `web` service, so it is empty "
        f"inside the container whatever .env says — {CUSTOMER_PATH_SETTINGS[name]} (2026-09-18)"
    )
    # Passed through from the environment, not pinned to a literal: `.env` has to be able to set it.
    assert "${" + name in environment[name], (
        f"{name} is hard-coded in infra/compose.yaml; it has to come from the environment"
    )


def test_the_web_service_refers_to_no_variable_nobody_documents() -> None:
    """A typo in compose silently becomes the default forever; this is what notices it."""
    block = service_block("web")
    referenced = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", block))
    undocumented = sorted(referenced - documented_in_env_example())
    assert undocumented == [], (
        f"infra/compose.yaml's `web` service reads {undocumented}, which .env.example does not "
        "document — either the name is a typo, or the operator has no way to know about it"
    )


def test_the_environment_blocks_are_shaped_as_this_test_assumes() -> None:
    """If the file's shape changes, the checks above must fail loudly rather than find nothing."""
    environment = environment_of("web")
    assert environment["POSTGRES_HOST"] == "db"
    assert len(environment) > 10


@pytest.mark.parametrize("name", sorted(CRAWLER_LLM_SETTINGS))
def test_crawler_llm_setting_is_read_and_documented(name: str) -> None:
    assert name in read_by_settings(), f"settings.py does not read {name}"
    assert name in documented_in_env_example(), (
        f"{name} is not in .env.example, so nobody knows to set it ({CRAWLER_LLM_SETTINGS[name]})"
    )


def anchor_environment(name: str) -> dict[str, str]:
    """The variables of a top-level `x-…:` environment anchor, e.g. `x-runtime-env`."""
    text = compose_text()
    start = text.index(f"\n{name}:") + 1
    rest = text[start:]
    rest = rest[rest.index("\n") + 1 :]
    end = re.search(r"^\S", rest, re.MULTILINE)
    body = rest[: end.start()] if end else rest
    return dict(re.findall(r"^  ([A-Z][A-Z0-9_]*):[ ]*(.*)$", body, re.MULTILINE))


def _assert_passed_through(where: str, environment: dict[str, str], name: str) -> None:
    assert name in environment, (
        f"infra/compose.yaml does not pass {name} into `{where}`, so it is empty inside the "
        f"container whatever .env says — {CRAWLER_LLM_SETTINGS[name]}"
    )
    assert "${" + name in environment[name], (
        f"{name} is hard-coded in infra/compose.yaml for `{where}`; it has to come from the "
        "environment"
    )


@pytest.mark.parametrize("name", sorted(CRAWLER_LLM_SETTINGS))
def test_crawler_llm_setting_reaches_the_per_site_crawlers(name: str) -> None:
    """A paid feature must not be able to be "on" in `.env` and off in the container."""
    _assert_passed_through(CRAWLER_ANCHOR_SERVICE, environment_of(CRAWLER_ANCHOR_SERVICE), name)


@pytest.mark.parametrize("name", sorted(CRAWLER_LLM_SETTINGS))
def test_crawler_llm_setting_reaches_the_crawl_runtime(name: str) -> None:
    _assert_passed_through(RUNTIME_ENV_ANCHOR, anchor_environment(RUNTIME_ENV_ANCHOR), name)


@pytest.mark.parametrize("name", sorted(ROBOTS_RESERVATION_SETTINGS))
def test_robots_reservation_setting_is_read_and_documented(name: str) -> None:
    assert name in read_by_settings(), f"settings.py does not read {name}"
    assert name in documented_in_env_example(), (
        f"{name} is not in .env.example ({ROBOTS_RESERVATION_SETTINGS[name]})"
    )


@pytest.mark.parametrize("name", sorted(ROBOTS_RESERVATION_SETTINGS))
def test_robots_reservation_setting_reaches_the_processes_that_read_robots_txt(name: str) -> None:
    """Both generations of crawler refresh robots.txt, so both have to receive these."""
    for where, environment in (
        (CRAWLER_ANCHOR_SERVICE, environment_of(CRAWLER_ANCHOR_SERVICE)),
        (RUNTIME_ENV_ANCHOR, anchor_environment(RUNTIME_ENV_ANCHOR)),
    ):
        assert name in environment, (
            f"infra/compose.yaml does not pass {name} into `{where}`, so it is empty inside the "
            f"container whatever .env says — {ROBOTS_RESERVATION_SETTINGS[name]}"
        )
        assert "${" + name in environment[name], (
            f"{name} is hard-coded in infra/compose.yaml for `{where}`"
        )


def test_the_runtime_anchor_is_shaped_as_this_test_assumes() -> None:
    environment = anchor_environment(RUNTIME_ENV_ANCHOR)
    assert environment["DJANGO_ENV"] == "dev"
    assert "RUNTIME_SITES" in environment


#: Frame embeddings, the second content signal (ADR 0037, issue #101). Until 2026-09-18
#: ``settings.py`` had read ``MATCHING_EMBEDDINGS_ENABLED`` since the ADR, ``crawl_runtime.py`` and
#: ``matching/media_worker.py`` acted on it, and no service passed it in — the same silent gap as
#: ``CRAWLER_LLM_REPAIR_ENABLED`` above, but in the quiet direction: a missing passthrough looks
#: exactly like "the operator turned it on and the media lane still never embeds". The model path
#: rides along: without it the lane looks for the file at the container's default cache location.
EMBEDDING_MEDIA_SETTINGS = {
    "MATCHING_EMBEDDINGS_ENABLED": "the switch; empty = the media lane never embeds, whatever "
    ".env says",
    "MATCHING_EMBEDDING_MODEL_PATH": "where the pinned model file is, if not the default cache "
    "location",
}

#: The `&media` anchor (`media-site01`), inherited by every other `media-*` service.
MEDIA_ANCHOR_SERVICE = "media-site01"


@pytest.mark.parametrize("name", sorted(EMBEDDING_MEDIA_SETTINGS))
def test_embedding_setting_is_read_and_documented(name: str) -> None:
    assert name in read_by_settings(), f"settings.py does not read {name}"
    assert name in documented_in_env_example(), (
        f"{name} is not in .env.example, so nobody knows to set it "
        f"({EMBEDDING_MEDIA_SETTINGS[name]})"
    )


@pytest.mark.parametrize("name", sorted(EMBEDDING_MEDIA_SETTINGS))
def test_embedding_setting_reaches_the_per_site_media_workers(name: str) -> None:
    environment = environment_of(MEDIA_ANCHOR_SERVICE)
    assert name in environment, (
        f"infra/compose.yaml does not pass {name} into `{MEDIA_ANCHOR_SERVICE}`, so it is empty "
        f"inside every media-* container whatever .env says — {EMBEDDING_MEDIA_SETTINGS[name]}"
    )
    assert "${" + name in environment[name], (
        f"{name} is hard-coded in infra/compose.yaml for `{MEDIA_ANCHOR_SERVICE}`; it has to come "
        "from the environment"
    )


@pytest.mark.parametrize("name", sorted(EMBEDDING_MEDIA_SETTINGS))
def test_embedding_setting_reaches_the_crawl_runtime(name: str) -> None:
    environment = anchor_environment(RUNTIME_ENV_ANCHOR)
    assert name in environment, (
        f"infra/compose.yaml's `{RUNTIME_ENV_ANCHOR}` anchor does not carry {name}, so it is "
        f"empty inside crawler-pages and crawler-media whatever .env says — "
        f"{EMBEDDING_MEDIA_SETTINGS[name]}"
    )
    assert "${" + name in environment[name], (
        f"{name} is hard-coded in infra/compose.yaml's `{RUNTIME_ENV_ANCHOR}` anchor; it has to "
        "come from the environment"
    )


#: A container path, not an operator value: the mount point of the `embedding-models` volume
#: (issue #101 follow-up to ADR 0037), exactly as `SPOOL_DIR_IN_CONTAINER` names the upload
#: spool's. Before this volume existed, a model fetched with `manage.py fetch_embedding_model`
#: lived in whichever container's writable layer ran the command and was lost on the next
#: `--force-recreate` (`make workers`/`make runtime`, routine after every migration) — which would
#: crash-loop the media lane the moment MATCHING_EMBEDDINGS_ENABLED went on, since both
#: `crawl_runtime.py` and `media_worker` refuse to start without the file.
EMBEDDING_MODEL_DIR_IN_CONTAINER = "/var/lib/marceline/embedding-models"
#: The services that must persist a fetched model across a recreate: every media-* worker (via the
#: `&media` anchor) and the crawl runtime's media lane. The pages lane never embeds
#: (`crawl_runtime.py`'s `embed` is always False for it), so it never mounts this.
EMBEDDING_MODEL_SERVICES = (MEDIA_ANCHOR_SERVICE, "crawler-media")


@pytest.mark.parametrize("service", EMBEDDING_MODEL_SERVICES)
def test_embedding_model_volume_is_mounted(service: str) -> None:
    assert f"embedding-models:{EMBEDDING_MODEL_DIR_IN_CONTAINER}" in service_block(service), (
        f"`{service}` does not mount the `embedding-models` volume at "
        f"{EMBEDDING_MODEL_DIR_IN_CONTAINER}, so a fetched model does not survive a recreate "
        "(issue #101 follow-up)"
    )


def test_the_pages_lane_does_not_mount_the_embedding_model_volume() -> None:
    """The pages lane never embeds, so it has no business holding a mount to the model file."""
    assert "embedding-models:" not in service_block("crawler-pages")


def test_the_embedding_model_path_default_points_into_the_volume() -> None:
    """The compose default for MATCHING_EMBEDDING_MODEL_PATH must agree with where the volume is
    mounted, or a model fetched into it is never found by `embedding.model_path`."""
    from matching.embedding import MODEL_FILENAME

    expected = (
        "${MATCHING_EMBEDDING_MODEL_PATH:-"
        + EMBEDDING_MODEL_DIR_IN_CONTAINER
        + "/"
        + MODEL_FILENAME
        + "}"
    )
    for where, environment in (
        (MEDIA_ANCHOR_SERVICE, environment_of(MEDIA_ANCHOR_SERVICE)),
        (RUNTIME_ENV_ANCHOR, anchor_environment(RUNTIME_ENV_ANCHOR)),
    ):
        assert environment.get("MATCHING_EMBEDDING_MODEL_PATH") == expected, (
            f"`{where}`'s MATCHING_EMBEDDING_MODEL_PATH default does not point into the "
            f"`embedding-models` volume mount, so a fetched model is not found there"
        )


#: The dead media URL shape breaker (ADR 0041, issue #113). Until this fix, settings.py had read
#: MEDIA_URL_BREAKER_ENABLED since the ADR, `matching/media_worker.py` acted on it via
#: `catalog.media_shapes.admit`/`record`, and no service passed it in — the same silent gap as the
#: embedding settings above. `web` also reads it directly (`catalog/media_shapes.py`
#: `shape_lines`, via `request_report`) to say "breaker off" on the Site admin page, so a missing
#: passthrough there would leave that line wrong even after the workers received the flag.
MEDIA_URL_BREAKER_SETTINGS = {
    "MEDIA_URL_BREAKER_ENABLED": "the switch; empty = the breaker never suppresses a shape, "
    "whatever .env says, and the admin page's 'breaker off' note would lie",
}


@pytest.mark.parametrize("name", sorted(MEDIA_URL_BREAKER_SETTINGS))
def test_media_url_breaker_setting_is_read_and_documented(name: str) -> None:
    assert name in read_by_settings(), f"settings.py does not read {name}"
    assert name in documented_in_env_example(), (
        f"{name} is not in .env.example, so nobody knows to set it "
        f"({MEDIA_URL_BREAKER_SETTINGS[name]})"
    )


@pytest.mark.parametrize("name", sorted(MEDIA_URL_BREAKER_SETTINGS))
def test_media_url_breaker_setting_reaches_the_web_container(name: str) -> None:
    environment = environment_of("web")
    assert name in environment, (
        f"infra/compose.yaml does not pass {name} into `web`, so it is empty inside the container "
        f"whatever .env says — {MEDIA_URL_BREAKER_SETTINGS[name]}"
    )
    assert "${" + name in environment[name], (
        f"{name} is hard-coded in infra/compose.yaml for `web`; it has to come from the environment"
    )


@pytest.mark.parametrize("name", sorted(MEDIA_URL_BREAKER_SETTINGS))
def test_media_url_breaker_setting_reaches_the_per_site_media_workers(name: str) -> None:
    environment = environment_of(MEDIA_ANCHOR_SERVICE)
    assert name in environment, (
        f"infra/compose.yaml does not pass {name} into `{MEDIA_ANCHOR_SERVICE}`, so it is empty "
        f"inside every media-* container whatever .env says — {MEDIA_URL_BREAKER_SETTINGS[name]}"
    )
    assert "${" + name in environment[name], (
        f"{name} is hard-coded in infra/compose.yaml for `{MEDIA_ANCHOR_SERVICE}`; it has to come "
        "from the environment"
    )


@pytest.mark.parametrize("name", sorted(MEDIA_URL_BREAKER_SETTINGS))
def test_media_url_breaker_setting_reaches_the_crawl_runtime(name: str) -> None:
    environment = anchor_environment(RUNTIME_ENV_ANCHOR)
    assert name in environment, (
        f"infra/compose.yaml's `{RUNTIME_ENV_ANCHOR}` anchor does not carry {name}, so it is "
        f"empty inside crawler-pages and crawler-media whatever .env says — "
        f"{MEDIA_URL_BREAKER_SETTINGS[name]}"
    )
    assert "${" + name in environment[name], (
        f"{name} is hard-coded in infra/compose.yaml's `{RUNTIME_ENV_ANCHOR}` anchor; it has to "
        "come from the environment"
    )


#: Black-bar cropping before hashing (off by default). Found alongside #113 by the same
#: `make status --gaps` sweep: `matching/media_worker.py` is the only reader, and no service ever
#: passed it in — the same silent gap as MEDIA_URL_BREAKER_ENABLED above, and not read by `web`.
CROP_BLACK_BARS_SETTINGS = {
    "MATCHING_CROP_BLACK_BARS": "the switch; empty = media is never cropped before hashing, "
    "whatever .env says",
}


@pytest.mark.parametrize("name", sorted(CROP_BLACK_BARS_SETTINGS))
def test_crop_black_bars_setting_is_read_and_documented(name: str) -> None:
    assert name in read_by_settings(), f"settings.py does not read {name}"
    assert name in documented_in_env_example(), (
        f"{name} is not in .env.example, so nobody knows to set it "
        f"({CROP_BLACK_BARS_SETTINGS[name]})"
    )


@pytest.mark.parametrize("name", sorted(CROP_BLACK_BARS_SETTINGS))
def test_crop_black_bars_setting_reaches_the_per_site_media_workers(name: str) -> None:
    environment = environment_of(MEDIA_ANCHOR_SERVICE)
    assert name in environment, (
        f"infra/compose.yaml does not pass {name} into `{MEDIA_ANCHOR_SERVICE}`, so it is empty "
        f"inside every media-* container whatever .env says — {CROP_BLACK_BARS_SETTINGS[name]}"
    )
    assert "${" + name in environment[name], (
        f"{name} is hard-coded in infra/compose.yaml for `{MEDIA_ANCHOR_SERVICE}`; it has to come "
        "from the environment"
    )


@pytest.mark.parametrize("name", sorted(CROP_BLACK_BARS_SETTINGS))
def test_crop_black_bars_setting_reaches_the_crawl_runtime(name: str) -> None:
    environment = anchor_environment(RUNTIME_ENV_ANCHOR)
    assert name in environment, (
        f"infra/compose.yaml's `{RUNTIME_ENV_ANCHOR}` anchor does not carry {name}, so it is "
        f"empty inside crawler-pages and crawler-media whatever .env says — "
        f"{CROP_BLACK_BARS_SETTINGS[name]}"
    )
    assert "${" + name in environment[name], (
        f"{name} is hard-coded in infra/compose.yaml's `{RUNTIME_ENV_ANCHOR}` anchor; it has to "
        "come from the environment"
    )


#: What the **scheduler** needs so `match_references` can act on the second and third signals
#: (ADR 0037, ADR 0039) — the same shape as `MATCHING_METADATA_CANDIDATES_ENABLED` above.
#: `MATCHING_REFERENCE_CROP_GRID` was found alongside the embeddings gap while investigating issue
#: #101: `settings.py` had read it since ADR 0039 and no service ever passed it in either.
SCHEDULER_SIGNAL_SETTINGS = {
    "MATCHING_EMBEDDINGS_ENABLED": "match_references refreshes the second signal's own "
    "candidates when this is true; no model is needed here, only stored sketches",
    "MATCHING_REFERENCE_CROP_GRID": "match_references consults the crop grid blobs when this is "
    "true",
}


@pytest.mark.parametrize("name", sorted(SCHEDULER_SIGNAL_SETTINGS))
def test_scheduler_signal_setting_is_read_documented_and_passed_through(name: str) -> None:
    assert name in read_by_settings(), f"settings.py does not read {name}"
    assert name in documented_in_env_example(), f"{name} is not in .env.example"
    environment = environment_of("scheduler")
    assert name in environment, (
        f"infra/compose.yaml does not pass {name} into the `scheduler` service, so it is empty "
        f"inside the container whatever .env says — {SCHEDULER_SIGNAL_SETTINGS[name]}"
    )
    assert "${" + name in environment[name], (
        f"{name} is hard-coded in infra/compose.yaml; it has to come from the environment"
    )


#: Cloud fingerprinting (issue #59, ADR 0044). The switch is the dangerous one in the *quiet*
#: direction — it defaults to false, so a missing passthrough looks like "the operator turned it on
#: and every upload route still answers 404". The caps are dangerous in the loud direction: a cap
#: that never reaches the container is the code default, on the disk Postgres lives on.
CLOUD_SETTINGS = {
    "CLOUD_FINGERPRINTING_ENABLED": "the switch; empty = every upload route is 404, whatever "
    ".env says",
    "CLOUD_UPLOAD_MAX_BYTES": "the largest single file",
    "CLOUD_UPLOAD_SPOOL_MAX_BYTES": "the cap that stops the spool filling the database's disk",
    "CLOUD_UPLOAD_TTL_HOURS": "the hard limit on how long bytes may sit in the spool",
}
#: A container path, not an operator value: it names the mount point of the `upload-spool` volume,
#: exactly as `RIGHTS_VAULT_DIR` names the rights vault's. Pinned here so a change has to be made
#: in both services at once.
SPOOL_DIR_IN_CONTAINER = "/var/lib/marceline/upload-spool"
#: The three services that touch the spool: `web` writes the chunks a browser on this machine sends,
#: `export` writes the chunks the public host's relay forwards (ADR 0044 §9, amendment 2026-09-19),
#: and the worker reads and deletes them. Nothing else may mount it.
CLOUD_SERVICES = ("web", "export", "fingerprint-worker")

#: The numeric cloud settings above, and the two more that reach only `web` (the chunk size and
#: the per-organisation daily quota — the worker never needs either), used to reach every
#: container as `${VAR:-}` — an empty string. Harmless: `env_int`/`env_float` in `settings.py`
#: fall back to the same code default on an empty value. But it hid the effective number from
#: anyone reading this file, unlike every other numeric passthrough here, which spells its
#: default out (e.g. `PROSPECT_DAILY_SEED_CAP: ${PROSPECT_DAILY_SEED_CAP:-4}`). Name -> (the
#: default as `settings.py` reads it, and so as compose must spell it; the services that carry
#: it) so the two cannot drift apart again.
CLOUD_UPLOAD_NUMERIC_DEFAULTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "CLOUD_UPLOAD_CHUNK_BYTES": (str(8 * 1024 * 1024), ("web", "export")),
    "CLOUD_UPLOAD_MAX_BYTES": (str(10 * 1024 * 1024 * 1024), CLOUD_SERVICES),
    "CLOUD_UPLOAD_SPOOL_MAX_BYTES": (str(40 * 1024 * 1024 * 1024), CLOUD_SERVICES),
    "CLOUD_UPLOAD_DAILY_BYTES_PER_ORG": (str(60 * 1024 * 1024 * 1024), ("web", "export")),
    "CLOUD_UPLOAD_TTL_HOURS": ("6", CLOUD_SERVICES),
}


@pytest.mark.parametrize("name", sorted(CLOUD_SETTINGS))
def test_cloud_fingerprinting_setting_is_read_and_documented(name: str) -> None:
    assert name in read_by_settings(), f"settings.py does not read {name}"
    assert name in documented_in_env_example(), (
        f"{name} is not in .env.example, so nobody knows to set it ({CLOUD_SETTINGS[name]})"
    )


@pytest.mark.parametrize("service", CLOUD_SERVICES)
@pytest.mark.parametrize("name", sorted(CLOUD_SETTINGS))
def test_cloud_fingerprinting_setting_reaches_the_container(service: str, name: str) -> None:
    environment = environment_of(service)
    assert name in environment, (
        f"infra/compose.yaml does not pass {name} into `{service}`, so it is empty inside the "
        f"container whatever .env says — {CLOUD_SETTINGS[name]}"
    )
    assert "${" + name in environment[name], (
        f"{name} is hard-coded in infra/compose.yaml for `{service}`; it has to come from the "
        "environment"
    )


@pytest.mark.parametrize("service", CLOUD_SERVICES)
def test_the_spool_directory_is_the_same_mount_point_everywhere(service: str) -> None:
    environment = environment_of(service)
    assert environment.get("CLOUD_UPLOAD_SPOOL_DIR") == SPOOL_DIR_IN_CONTAINER
    assert f"upload-spool:{SPOOL_DIR_IN_CONTAINER}" in service_block(service), (
        f"`{service}` names the spool directory but does not mount the `upload-spool` volume there"
    )


def test_only_those_services_mount_the_spool() -> None:
    """Uploaded media has exactly these writers and readers. One more mount is how it ends up
    somewhere it is backed up, served or forgotten (ADR 0044)."""
    mounting = [line for line in compose_text().splitlines() if "- upload-spool:" in line.strip()]
    assert len(mounting) == len(CLOUD_SERVICES), (
        f"{len(mounting)} services mount the upload spool, expected {len(CLOUD_SERVICES)}: "
        f"{mounting}"
    )


@pytest.mark.parametrize("name", sorted(CLOUD_UPLOAD_NUMERIC_DEFAULTS))
def test_cloud_upload_numeric_default_is_spelled_out_not_left_empty(name: str) -> None:
    """`${VAR:-}` and `${VAR:-8388608}` behave identically today (`env_int` falls back to the
    same code default either way), but only the second tells a person reading this file what a
    container actually runs with — every other numeric passthrough here does that, and this one
    used to be the exception (issue #59 trial)."""
    default, services = CLOUD_UPLOAD_NUMERIC_DEFAULTS[name]
    expected = "${" + name + ":-" + default + "}"
    for service in services:
        environment = environment_of(service)
        assert environment.get(name) == expected, (
            f"`{service}`'s {name} is {environment.get(name)!r}, expected {expected!r} to match "
            "the default settings.py reads"
        )


def test_the_decoder_container_has_no_route_to_the_internet() -> None:
    """ADR 0044: `fingerprint-worker` is on the internal `uploads` network and nothing else, so
    ffmpeg cannot fetch anything even if it were talked into trying."""
    block = service_block("fingerprint-worker")
    assert "networks: [uploads]" in block
    assert "read_only: true" in block
    assert "cap_drop: [ALL]" in block
    assert 'security_opt: ["no-new-privileges:true"]' in block
    assert re.search(r"^  uploads:\n    internal: true$", compose_text(), re.MULTILINE), (
        "the `uploads` network must be declared internal, or the decoder has the internet"
    )


def test_production_states_the_refusal_rather_than_leaving_it_out() -> None:
    """A setting a compose file does not name is silently empty — which would be the right value
    here by luck. ADR 0044 wants it visible instead, so nobody enables it by editing an .env.

    Since the amendment of 2026-09-19 the public host may hold the flag, but only on `web` and
    only with relay mode; the shared anchor still spells the refusal, `internal` inherits it,
    and there is still no spool anywhere in that file. `infra/prod/check_config.py` is where the
    combination is judged (mutation tests in `test_prod_compose_invariants.py`)."""
    prod = (REPO_ROOT / "infra" / "prod" / "compose.yaml").read_text()
    assert 'CLOUD_FINGERPRINTING_ENABLED: "false"' in prod
    assert "CLOUD_UPLOAD_RELAY_MODE: ${CLOUD_UPLOAD_RELAY_MODE:-false}" in prod
    assert "CLOUD_UPLOAD_SPOOL_DIR" not in prod
    assert "upload-spool" not in prod


#: The do-not-contact record (issue #98, ADR 0047). The admin screen that records a "no" runs in
#: `web`, and without the key there it refuses — the safe direction, but it would be refusing while
#: `.env` looks right, and the operator would work around it by keeping the list somewhere else.
DO_NOT_CONTACT_SETTINGS = {
    "DO_NOT_CONTACT_HMAC_KEY": "the key every recorded objection is hashed with; empty = the "
    "admin refuses to record one",
}


@pytest.mark.parametrize("name", sorted(DO_NOT_CONTACT_SETTINGS))
def test_do_not_contact_key_is_read_documented_and_reaches_the_web_container(name: str) -> None:
    assert name in read_by_settings(), f"settings.py does not read {name}"
    assert name in documented_in_env_example(), (
        f"{name} is not in .env.example ({DO_NOT_CONTACT_SETTINGS[name]})"
    )
    environment = environment_of("web")
    assert name in environment, (
        f"infra/compose.yaml does not pass {name} into `web`, so it is empty inside the container "
        f"whatever .env says — {DO_NOT_CONTACT_SETTINGS[name]}"
    )
    assert "${" + name in environment[name], (
        f"{name} is hard-coded in infra/compose.yaml; it has to come from the environment"
    )


@pytest.mark.parametrize("name", sorted(SCHEDULER_MAIL_SETTINGS))
def test_scheduler_setting_is_read_documented_and_passed_through(name: str) -> None:
    assert name in read_by_settings(), f"settings.py does not read {name}"
    assert name in documented_in_env_example(), f"{name} is not in .env.example"
    environment = environment_of("scheduler")
    assert name in environment, (
        f"infra/compose.yaml does not pass {name} into the `scheduler` service, so it is empty "
        f"inside the container whatever .env says — {SCHEDULER_MAIL_SETTINGS[name]}"
    )
    assert "${" + name in environment[name], (
        f"{name} is hard-coded in infra/compose.yaml; it has to come from the environment"
    )


@pytest.mark.parametrize("name", sorted(CUSTOMER_DELETION_SETTINGS))
def test_customer_deletion_setting_is_read_documented_and_passed_through(name: str) -> None:
    assert name in read_by_settings(), f"settings.py does not read {name}"
    assert name in documented_in_env_example(), f"{name} is not in .env.example"
    environment = environment_of("scheduler")
    assert name in environment, (
        f"infra/compose.yaml does not pass {name} into the `scheduler` service, so it is empty "
        f"inside the container whatever .env says - {CUSTOMER_DELETION_SETTINGS[name]}"
    )
    assert "${" + name in environment[name], (
        f"{name} is hard-coded in infra/compose.yaml; it has to come from the environment"
    )


@pytest.mark.parametrize("name", sorted(SAFETY_RETENTION_SETTINGS))
def test_safety_retention_setting_is_read_documented_and_passed_through(name: str) -> None:
    _assert_scheduler_setting(name, SAFETY_RETENTION_SETTINGS[name])


@pytest.mark.parametrize("name", sorted(RESEARCH_RETENTION_SETTINGS))
def test_research_retention_setting_is_read_documented_and_passed_through(name: str) -> None:
    _assert_scheduler_setting(name, RESEARCH_RETENTION_SETTINGS[name])


@pytest.mark.parametrize("name", sorted(INVITE_RETENTION_SETTINGS))
def test_invite_retention_setting_is_read_documented_and_passed_through(name: str) -> None:
    _assert_scheduler_setting(name, INVITE_RETENTION_SETTINGS[name])


def _assert_scheduler_setting(name: str, why: str) -> None:
    """The same three checks for every scheduler-only setting: read, documented, passed through."""
    assert name in read_by_settings(), f"settings.py does not read {name}"
    assert name in documented_in_env_example(), f"{name} is not in .env.example"
    environment = environment_of("scheduler")
    assert name in environment, (
        f"infra/compose.yaml does not pass {name} into the `scheduler` service, so it is empty "
        f"inside the container whatever .env says - {why}"
    )
    assert "${" + name in environment[name], (
        f"{name} is hard-coded in infra/compose.yaml; it has to come from the environment"
    )


def test_the_scheduler_still_gets_the_mail_settings() -> None:
    """The digest has no recipient, sender or transport without the shared mail anchor."""
    assert re.search(r"<<:\s*(\*mail-env|\[[^\]]*\*mail-env)", service_block("scheduler")), (
        "the scheduler no longer merges x-mail-env, so the daily safety digest would be "
        "addressed to nobody inside the container (docs/safety.md)"
    )


#: Operator mail consolidation (2026-09-19, core/operator_mail.py, docs/safety.md "Daily operator
#: report"): three event mails (an incident-rule block, a new/changed TDM/AI reservation, a broken
#: adapter) fold into the one daily report by default, and this is the escape hatch back to
#: mailing on its own without a code change. It has to reach every process that decides whether to
#: send one of those three mails, or a name in `.env` would do nothing wherever it is missing —
#: the same silent-gap shape as every list above.
OPERATOR_MAIL_IMMEDIATE_SETTINGS = {
    "OPERATOR_MAIL_IMMEDIATE": "comma list of kinds that still mail on their own; empty = none do",
}

#: The shared mail anchor: `x-mail-env` is the map key `anchor_environment` reads its variables
#: from, `mail-env` is the YAML alias (`&mail-env`) the services below merge with `<<:`. Merged by
#: the per-site crawlers, the crawl runtime's pages lane and the scheduler — the three processes
#: that decide whether `safety_incident`, `tdm_reservation` or `adapter_health` mails immediately.
MAIL_ENV_ANCHOR = "x-mail-env"
MAIL_ENV_ALIAS = "mail-env"
MAIL_SENDING_SERVICES = (CRAWLER_ANCHOR_SERVICE, "crawler-pages", "scheduler")


@pytest.mark.parametrize("name", sorted(OPERATOR_MAIL_IMMEDIATE_SETTINGS))
def test_operator_mail_immediate_setting_is_read_and_documented(name: str) -> None:
    assert name in read_by_settings(), f"settings.py does not read {name}"
    assert name in documented_in_env_example(), (
        f"{name} is not in .env.example ({OPERATOR_MAIL_IMMEDIATE_SETTINGS[name]})"
    )


@pytest.mark.parametrize("name", sorted(OPERATOR_MAIL_IMMEDIATE_SETTINGS))
def test_operator_mail_immediate_setting_is_in_the_mail_anchor(name: str) -> None:
    environment = anchor_environment(MAIL_ENV_ANCHOR)
    assert name in environment, (
        f"{MAIL_ENV_ANCHOR} does not set {name}, so it is empty in every container that merges "
        f"it whatever .env says — {OPERATOR_MAIL_IMMEDIATE_SETTINGS[name]}"
    )
    assert "${" + name in environment[name], (
        f"{name} is hard-coded in the {MAIL_ENV_ANCHOR} anchor; it has to come from the environment"
    )


@pytest.mark.parametrize("service", MAIL_SENDING_SERVICES)
def test_operator_mail_immediate_setting_reaches_the_processes_that_send(service: str) -> None:
    """Every process that decides whether `safety_incident`, `tdm_reservation` or `adapter_health`
    mails on its own merges the shared mail anchor — a missing merge would leave the switch
    reading empty (never immediate) in that one container, whatever `.env` says."""
    assert re.search(
        rf"<<:\s*(\*{MAIL_ENV_ALIAS}|\[[^\]]*\*{MAIL_ENV_ALIAS})", service_block(service)
    ), (
        f"`{service}` no longer merges {MAIL_ENV_ANCHOR}, so OPERATOR_MAIL_IMMEDIATE would be "
        "empty inside that container whatever .env says (docs/safety.md)"
    )


#: Error reporting (issue #37, `core/sentry.py`), and the containers that must be able to use it.
#:
#: The third instance of the same silent gap, and the one that hides every other failure: until
#: 2026-09-18 ``SENTRY_DSN`` was read by ``settings.py`` and documented in ``.env.example`` while
#: ``infra/compose.yaml`` passed it to nobody, so the unattended processes — the per-site crawlers,
#: both crawl-runtime lanes, the scheduler, the export listener — could not report an error however
#: the operator set it. Issue #37 asks for Sentry "for web, workers and scheduler"; these are the
#: services that word names. ``SENTRY_ENVIRONMENT`` rides along: without it every deployment's
#: events land in one bucket.
SENTRY_SETTINGS = {
    "SENTRY_DSN": "empty = reporting off; a container that never sees it can never report",
    "SENTRY_ENVIRONMENT": "which deployment an event came from; empty falls back to DJANGO_ENV",
    "MARCELINE_RELEASE": "which code an event came from; documented since 2026-09-17, passed to "
    "no container until 2026-09-18",
}

#: The shared anchor those variables live in, and every service that merges it. A worker family is
#: represented by its anchor service (``&crawler``, ``&media``); the rest inherit it.
SENTRY_ENV_ANCHOR = "x-sentry-env"
SENTRY_SERVICES = (
    "web",
    CRAWLER_ANCHOR_SERVICE,
    "media-site01",
    "crawler-pages",
    "crawler-media",
    "scheduler",
    "export",
)


@pytest.mark.parametrize("name", sorted(SENTRY_SETTINGS))
def test_sentry_setting_is_read_documented_and_passed_through(name: str) -> None:
    assert name in read_by_settings(), f"settings.py does not read {name}"
    assert name in documented_in_env_example(), (
        f"{name} is not in .env.example, so nobody knows to set it ({SENTRY_SETTINGS[name]})"
    )
    environment = anchor_environment(SENTRY_ENV_ANCHOR)
    assert name in environment, (
        f"infra/compose.yaml's `{SENTRY_ENV_ANCHOR}` anchor does not carry {name}, so it is empty "
        f"inside every container whatever .env says — {SENTRY_SETTINGS[name]}"
    )
    assert "${" + name in environment[name], (
        f"{name} is hard-coded in infra/compose.yaml; it has to come from the environment"
    )


@pytest.mark.parametrize("service", SENTRY_SERVICES)
def test_every_django_container_can_report_errors(service: str) -> None:
    """An error reporter that is silently off in the unattended processes reports nothing."""
    block = service_block(service)
    assert re.search(r"<<:\s*(\*sentry-env|\[[^\]]*\*sentry-env)", block), (
        f"`{service}` does not merge *sentry-env, so SENTRY_DSN is empty inside it however .env "
        "is set — and issue #37 asks for Sentry in web, workers and scheduler"
    )


@pytest.mark.parametrize("name", sorted(ONBOARDING_INVITE_SETTINGS))
def test_onboarding_invite_setting_is_read_and_documented(name: str) -> None:
    assert name in read_by_settings(), f"settings.py does not read {name}"
    assert name in documented_in_env_example(), (
        f"{name} is not in .env.example ({ONBOARDING_INVITE_SETTINGS[name]})"
    )


@pytest.mark.parametrize("name", sorted(ONBOARDING_INVITE_SETTINGS))
def test_onboarding_invite_setting_reaches_the_web_container(name: str) -> None:
    """The redeem routes, the admin action and `invite_customer` all run in `web`."""
    environment = environment_of("web")
    assert name in environment, (
        f"infra/compose.yaml does not pass {name} into `web`, so it is empty inside the container "
        f"whatever .env says — {ONBOARDING_INVITE_SETTINGS[name]}"
    )
    assert "${" + name in environment[name], (
        f"{name} is hard-coded in infra/compose.yaml for `web`; it has to come from the environment"
    )


@pytest.mark.parametrize("name", sorted(ACCOUNT_CONTROL_SETTINGS))
def test_account_control_setting_is_read_documented_and_reaches_web(name: str) -> None:
    """ADR 0055: the flow lives in `web` — the customer routes and the admin screens both."""
    assert name in read_by_settings(), f"settings.py does not read {name}"
    assert name in documented_in_env_example(), (
        f"{name} is not in .env.example ({ACCOUNT_CONTROL_SETTINGS[name]})"
    )
    environment = environment_of("web")
    assert name in environment, (
        f"infra/compose.yaml does not pass {name} into `web`, so it is empty inside the container "
        f"whatever .env says — {ACCOUNT_CONTROL_SETTINGS[name]}"
    )
    assert "${" + name in environment[name], (
        f"{name} is hard-coded in infra/compose.yaml; it has to come from the environment"
    )


@pytest.mark.parametrize("name", sorted(ACCOUNT_CONTROL_SETTINGS))
def test_account_control_setting_reaches_the_public_host_too(name: str) -> None:
    """ADR 0055 reaches production like rights and notices: the status read comes from the signed
    bundle, the two writes enter the outbox. So the public host needs the switch passed through —
    off by default, and settable, unlike `CLOUD_FINGERPRINTING_ENABLED`, which is pinned `"false"`
    there because customer media may never touch that host at all."""
    prod = (REPO_ROOT / "infra" / "prod" / "compose.yaml").read_text()
    assert f"{name}: ${{{name}:-false}}" in prod, (
        f"infra/prod/compose.yaml does not pass {name} through, so the step is 404 on the public "
        "host however development is configured"
    )


#: What the deletion journal needs, and which containers need it (ADR 0057, DPIA R24, issue #137).
#:
#: The seventh instance of the same silent gap, and the one where being silently empty **is** the
#: risk the feature exists against: `web` and `scheduler` would keep deleting, the journal would
#: stay empty, and a restore months later would resurrect every suppressed name and every scrubbed
#: page with nothing anywhere to say what to re-apply.
DELETION_JOURNAL_SERVICES = ("web", "scheduler")
DELETION_JOURNAL_SETTINGS = {
    "DELETION_JOURNAL_DIR": "empty = nothing is journalled and a restore cannot be repaired",
    "DELETION_JOURNAL_SEGMENT": "which file this process appends to; one shared file cannot work",
}
#: The bind mount: the host directory at the same path inside the container.
JOURNAL_MOUNT = (
    "- ${DELETION_JOURNAL_DIR:-/var/lib/marceline/deletion-journal}"
    ":${DELETION_JOURNAL_DIR:-/var/lib/marceline/deletion-journal}"
)


@pytest.mark.parametrize("name", sorted(DELETION_JOURNAL_SETTINGS))
def test_deletion_journal_setting_is_read_and_documented(name: str) -> None:
    assert name in read_by_settings(), f"settings.py does not read {name}"
    assert name in documented_in_env_example(), (
        f"{name} is not in .env.example ({DELETION_JOURNAL_SETTINGS[name]})"
    )


@pytest.mark.parametrize("service", DELETION_JOURNAL_SERVICES)
def test_the_journal_directory_reaches_the_containers_that_delete(service: str) -> None:
    environment = environment_of(service)
    assert "DELETION_JOURNAL_DIR" in environment, (
        f"infra/compose.yaml does not pass DELETION_JOURNAL_DIR into `{service}`, so every "
        "deletion it performs is unjournalled and a restore would silently undo it (ADR 0057)"
    )
    assert "${DELETION_JOURNAL_DIR" in environment["DELETION_JOURNAL_DIR"], (
        "the journal directory has to come from the environment, not be hard-coded"
    )


@pytest.mark.parametrize("service", DELETION_JOURNAL_SERVICES)
def test_each_journal_writer_has_its_own_segment(service: str) -> None:
    """Two processes cannot share one file: the host-run commands write as the operator and these
    containers as root, so whichever created the file would own it."""
    assert environment_of(service).get("DELETION_JOURNAL_SEGMENT") == service


@pytest.mark.parametrize("service", DELETION_JOURNAL_SERVICES)
def test_the_journal_is_mounted_at_the_same_path_inside_the_container(service: str) -> None:
    """One `.env` value is both the mount source and what Django reads, on the host and in the
    container, so the commands and the containers append into one directory."""
    assert JOURNAL_MOUNT in service_block(service), (
        f"`{service}` reads DELETION_JOURNAL_DIR but does not bind-mount that host directory at "
        "the same path"
    )


def test_no_other_service_mounts_the_journal() -> None:
    """A container that parses untrusted HTML must not hold a writable mount into the operator's
    backup directory, and it runs no journalled deletion path (ADR 0057)."""
    mounting = [
        line
        for line in compose_text().splitlines()
        if line.strip().startswith("- ${DELETION_JOURNAL_DIR")
    ]
    assert len(mounting) == len(DELETION_JOURNAL_SERVICES), (
        f"{len(mounting)} services mount the deletion journal, expected "
        f"{len(DELETION_JOURNAL_SERVICES)}"
    )


#: What the creator's own data requests need, and where (ADR 0056, issue #133).
#:
#: The same silent gap as the two switches above, and here it has three homes rather than one:
#: `web` serves the customer routes and the admin screen, the **public host** serves the screen the
#: feature exists for, and the `export` service is where `pull_customer_writes` applies a request
#: the public host accepted — `customer_writes.py` re-reads the switch before it writes anything, so
#: a missing value there turns every production request into a quiet `data_requests_unavailable`.
DATA_REQUEST_SETTINGS = {
    "CUSTOMER_DATA_REQUESTS_ENABLED": "the switch; empty = every /api/v1/app/data-requests route "
    "is a 404 and a request pulled from production is refused instead of recorded",
}


@pytest.mark.parametrize("name", sorted(DATA_REQUEST_SETTINGS))
def test_data_request_setting_is_read_documented_and_reaches_web(name: str) -> None:
    """ADR 0056: the routes and the admin screen both live in `web`."""
    assert name in read_by_settings(), f"settings.py does not read {name}"
    assert name in documented_in_env_example(), (
        f"{name} is not in .env.example ({DATA_REQUEST_SETTINGS[name]})"
    )
    environment = environment_of("web")
    assert name in environment, (
        f"infra/compose.yaml does not pass {name} into `web`, so it is empty inside the container "
        f"whatever .env says — {DATA_REQUEST_SETTINGS[name]}"
    )
    assert "${" + name in environment[name], (
        f"{name} is hard-coded in infra/compose.yaml; it has to come from the environment"
    )


@pytest.mark.parametrize("name", sorted(DATA_REQUEST_SETTINGS))
def test_data_request_setting_reaches_the_service_that_applies_the_write(name: str) -> None:
    """`manage.py pull_customer_writes` runs in the `export` service (ADR 0028 write path).

    A request a creator makes on the public host is an outbox item; this is the process that turns
    it into a row somebody can answer. With the switch missing here the pull would succeed and the
    apply would refuse, which is the worst of the three outcomes: the creator is told it was
    accepted and no staff member ever sees it.
    """
    environment = environment_of("export")
    assert name in environment, (
        f"infra/compose.yaml does not pass {name} into `export`, so every data request pulled from "
        "production is refused rather than recorded"
    )
    assert "${" + name in environment[name], (
        f"{name} is hard-coded for `export` in infra/compose.yaml; it has to come from the "
        "environment"
    )


@pytest.mark.parametrize("name", sorted(DATA_REQUEST_SETTINGS))
def test_data_request_setting_reaches_the_public_host_too(name: str) -> None:
    """The public host is the reason ADR 0056 exists: `can_export_data` is false there, so the two
    requests are the only way a creator who never sees development can exercise Art. 15 or Art. 17.
    The read comes from the signed bundle and both writes enter the outbox, so the switch is passed
    through rather than pinned `"false"` the way `CLOUD_FINGERPRINTING_ENABLED` is."""
    prod = (REPO_ROOT / "infra" / "prod" / "compose.yaml").read_text()
    assert f"{name}: ${{{name}:-false}}" in prod, (
        f"infra/prod/compose.yaml does not pass {name} through, so the screen is 404 on the public "
        "host however development is configured"
    )


@pytest.mark.parametrize("name", sorted(ACCOUNT_MAIL_SETTINGS))
@pytest.mark.parametrize("service", ACCOUNT_MAIL_SERVICES)
def test_account_mail_setting_reaches_the_senders(service: str, name: str) -> None:
    """ADR 0054: the two processes that send a customer mail must be handed the switch.

    `web` for a request on development's own surface and for a redeemed invite, `export` because
    `pull_customer_writes` applies the requests the public host accepted. Without the variable here
    the flow is silently off however `.env` is set, and the answer is neutral either way.
    """
    assert name in environment_of(service), ACCOUNT_MAIL_SETTINGS[name]


@pytest.mark.parametrize("name", sorted(ACCOUNT_MAIL_SETTINGS))
def test_account_mail_setting_is_read_and_documented(name: str) -> None:
    assert name in SETTINGS.read_text(), f"{name} is not read by settings.py"
    assert name in documented_in_env_example(), f"{name} is not in .env.example"


@pytest.mark.parametrize("service", ACCOUNT_MAIL_SERVICES)
def test_the_senders_get_the_transport_too(service: str) -> None:
    """The switch without SMTP settings mints tokens nobody can receive (ADR 0054 §1).

    The transport comes from the `mail-env` anchor, which both services merge — `environment_of`
    reads only the literal lines of a block, so this reads the merge itself, with the expression
    the operator report's own check uses.
    """
    assert re.search(
        rf"<<:\s*(\*{MAIL_ENV_ALIAS}|\[[^\]]*\*{MAIL_ENV_ALIAS})", service_block(service)
    ), f"{service} cannot be given a mail transport (ADR 0054 §1)"


@pytest.mark.parametrize("service", ACCOUNT_MAIL_SERVICES)
def test_the_senders_know_the_public_origin(service: str) -> None:
    """Every mail carries one link, and it points at the customer app the creator can reach."""
    assert "ONBOARDING_INVITE_BASE_URL" in environment_of(service)
