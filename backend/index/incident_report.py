"""The hotline report draft for one safety incident, in German and English (issue #66).

A human copies this text into the hotline's web form (docs/safety.md: the eco/FSM complaints form
at internet-beschwerdestelle.de). Nothing here submits anything — submission stays human
— and nothing here fetches anything: the draft is built from the incident record
alone.

**What a draft contains, and why that much.** The wording follows fixed German and English
report templates: a *machine-generated suspicion*, never an assertion that the content is
unlawful. The facts are exactly the ones docs/safety.md allows a report to carry:

- the URL(s) of the case — the one place URLs leave the system, because this *is* the report;
  further URLs the case holds because the same content was found there (deduplication evidence),
- the site's domain, when the case was first detected and last seen,
- the matched safety **category** — never the matched term: docs/safety.md says a report contains
  "the page URL, the site, when it was seen and the matched category", and the term list is our
  own detection rule set,
- whether media of the case was ever requested, stated truthfully,
- our internal incident reference, so a hotline follow-up can be matched to the record.

**Never** a title, tags, extracted text, a media URL, a thumbnail, or anything about the media
beyond that one truthful fetch statement.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from django.db.models import Q

from index.incidents import ACTIVE_STATUSES
from index.models import Media, SafetyIncident, SafetyIncidentPage

#: Recorded on the incident when it is marked reported, so the wording a report used can be told
#: apart from later template changes.
TEMPLATE_VERSION = "hotline-report-v1"
#: The operator's reporting channel (docs/safety.md, decided 2026-09-16).
REPORT_CHANNEL = "internet-beschwerdestelle.de (eco/FSM complaints web form)"
REPORTER_NAME = "Marceline \u2013 Safety Reporting"
Language = Literal["de", "en"]
LANGUAGES: tuple[Language, ...] = ("de", "en")
#: Only a running case gets a draft: a settled one (closed, false positive) must not be reported.
DRAFTABLE_STATUSES = ACTIVE_STATUSES

Evidence = SafetyIncidentPage.Evidence
#: Rows that are the case's own address; everything else is the same content found elsewhere.
PRIMARY_EVIDENCE = (Evidence.ORIGIN, Evidence.SAME_URL, Evidence.TERM_CHANGE)


class MediaFetch(StrEnum):
    """What we can truthfully say about media requests for the pages of a case."""

    NONE = "none"  # every page row still exists and none of its media was ever requested
    FETCHED = "fetched"  # media of at least one page was requested (before it was flagged)
    UNKNOWN = "unknown"  # at least one page row is gone, so its fetch history is gone too


@dataclass(frozen=True)
class DraftUrl:
    url: str
    site: str
    evidence: str
    first_seen_at: datetime
    last_seen_at: datetime
    primary: bool
    new_since_report: bool


@dataclass(frozen=True)
class DraftFacts:
    """Everything a draft may say. Built from the incident record only — no page content."""

    incident_id: int
    status: str
    categories: tuple[str, ...]
    detected_at: datetime
    last_seen_at: datetime
    reported_at: datetime | None
    hotline_reference: str
    media_fetch: MediaFetch
    urls: tuple[DraftUrl, ...]
    reply_to: str

    @property
    def reference_id(self) -> str:
        return reference_id(self.incident_id)


@dataclass(frozen=True)
class Draft:
    language: Language
    subject: str
    body: str


def reference_id(incident_id: int) -> str:
    """Our reference for a case, as a hotline would quote it back."""
    return f"MARCELINE-SI-{incident_id}"


def utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def categories(incident: SafetyIncident) -> tuple[str, ...]:
    ordered = [incident.category] if incident.category else []
    for term in incident.terms if isinstance(incident.terms, list) else []:
        category = str(term.get("category", "")) if isinstance(term, dict) else ""
        if category and category not in ordered:
            ordered.append(category)
    return tuple(ordered)


def _media_fetch(rows: list[SafetyIncidentPage]) -> MediaFetch:
    page_ids = [row.page_id for row in rows if row.page_id is not None]
    # `fetched_at` is stamped whenever the media worker made the request (even when it then failed
    # or skipped); a capture row means a browser loaded the page's player (ADR 0016).
    requested = Media.objects.filter(page_id__in=page_ids).filter(
        Q(fetched_at__isnull=False) | Q(kind=Media.Kind.CAPTURE)
    )
    if page_ids and requested.exists():
        return MediaFetch.FETCHED
    if len(page_ids) < len(rows):
        return MediaFetch.UNKNOWN
    return MediaFetch.NONE


def incident_facts(incident: SafetyIncident, *, reply_to: str) -> DraftFacts:
    rows = list(incident.incident_pages.order_by("first_seen_at", "pk"))
    reported_at = incident.reported_at
    urls = sorted(
        (
            DraftUrl(
                url=row.url,
                site=row.site_domain,
                evidence=row.evidence,
                first_seen_at=row.first_seen_at,
                last_seen_at=row.last_seen_at,
                primary=row.evidence in PRIMARY_EVIDENCE,
                new_since_report=reported_at is not None and row.first_seen_at > reported_at,
            )
            for row in rows
        ),
        key=lambda item: (not item.primary, item.first_seen_at, item.url),
    )
    return DraftFacts(
        incident_id=incident.pk,
        status=incident.status,
        categories=categories(incident),
        detected_at=incident.detected_at,
        last_seen_at=incident.last_seen_at,
        reported_at=reported_at,
        hotline_reference=incident.reference,
        media_fetch=_media_fetch(rows),
        urls=tuple(urls),
        reply_to=reply_to,
    )


# --- Wording. Kept next to each other so the two languages cannot drift apart unnoticed. ---

CATEGORY_TEXT: dict[str, dict[str, str]] = {
    "minor": {
        "de": "Textmerkmale, die auf die Beteiligung einer minderjährigen Person hindeuten können",
        "en": "text indicators that a minor may be involved",
    },
    "non_consent": {
        "de": "Textmerkmale, die auf eine nicht einvernehmliche Aufnahme hindeuten können",
        "en": "text indicators of a possibly non-consensual recording",
    },
    "other": {
        "de": "sonstige Textmerkmale möglicherweise rechtswidriger Inhalte",
        "en": "other text indicators of possibly unlawful content",
    },
    "": {
        "de": "manuell gekennzeichnet nach Prüfung des Seitentextes",
        "en": "flagged manually after a review of the page text",
    },
}

EVIDENCE_TEXT: dict[str, dict[str, str]] = {
    Evidence.MIRROR: {
        "de": "gleicher Inhalt, erkannt an einem identischen Vorschaubild-Pfad",
        "en": "same content, recognised by an identical preview image path",
    },
    Evidence.SHA256: {
        "de": "gleicher Inhalt, erkannt an einer byte-identischen Mediendatei (SHA-256)",
        "en": "same content, recognised by a byte-identical media file (SHA-256)",
    },
    Evidence.CLUSTER: {
        "de": "gleicher Inhalt, erkannt an übereinstimmenden Bild-Fingerabdrücken (PDQ)",
        "en": "same content, recognised by matching image fingerprints (PDQ)",
    },
    # Deliberately the same sentence as CLUSTER: for a hotline both are "the pictures match", and
    # reusing the v1 wording verbatim keeps the template version unchanged.
    Evidence.PDQ: {
        "de": "gleicher Inhalt, erkannt an übereinstimmenden Bild-Fingerabdrücken (PDQ)",
        "en": "same content, recognised by matching image fingerprints (PDQ)",
    },
    Evidence.MANUAL: {
        "de": "von uns demselben Fall zugeordnet",
        "en": "attached to this case by our staff",
    },
}

MEDIA_FETCH_TEXT: dict[MediaFetch, dict[str, str]] = {
    MediaFetch.NONE: {
        "de": "Kein Medienabruf erfolgt.",
        "en": "No media request was made.",
    },
    MediaFetch.FETCHED: {
        "de": (
            "Vor der Kennzeichnung wurden Medien mindestens einer der genannten URLs automatisiert "
            "abgerufen und ausschließlich maschinell zur Berechnung von Bild-Prüfsummen "
            "verarbeitet. Kein Mensch hat sie geöffnet oder angesehen; es wurden keine Bild- oder "
            "Videodateien gespeichert."
        ),
        "en": (
            "Before the flag was raised, media of at least one of the URLs above was requested "
            "automatically and processed by machine only, to compute image checksums. No human "
            "opened or viewed it; no image or video files were retained."
        ),
    },
    MediaFetch.UNKNOWN: {
        "de": (
            "Für mindestens eine der genannten URLs ist nicht mehr feststellbar, ob vor der "
            "Kennzeichnung Medien automatisiert abgerufen wurden. Es wurden keine Bild- oder "
            "Videodateien gespeichert, und kein Mensch hat Medien geöffnet oder angesehen."
        ),
        "en": (
            "For at least one of the URLs above it can no longer be determined whether media was "
            "requested automatically before the flag was raised. No image or video files were "
            "retained, and no human opened or viewed any media."
        ),
    },
}

TEXT: dict[str, dict[str, str]] = {
    "de": {
        "subject": (
            "Maschinell erzeugter Verdacht auf möglicherweise rechtswidrigen Inhalt "
            "\u2013 URL-Meldung \u2013 nicht visuell verifiziert \u2013 {ref}"
        ),
        "supplement_subject": "Ergänzung: ",
        "greeting": "Guten Tag,",
        "intro": (
            "ich melde einen maschinell erzeugten Verdacht. Mit dieser Meldung wird nicht "
            "behauptet, dass der Inhalt tatsächlich rechtswidrig ist."
        ),
        "supplement": "Dies ist eine Ergänzung zu unserer Meldung vom {date} ({hotline_ref}).",
        "hotline_ref": "Ihr Zeichen: {reference}",
        "no_hotline_ref": "ohne Aktenzeichen",
        "our_ref": "Unser Zeichen (interne Vorfall-ID):",
        "url_one": "URL:",
        "url_many": "URLs:",
        "more_urls": "Weitere URLs mit demselben Inhalt:",
        "url_line": "(Website: {site}; erstmals gesehen {first}; zuletzt gesehen {last}{extra})",
        "reason_line": "; Grund der Zuordnung: {reason}",
        "new_line": "; neu seit unserer Meldung",
        "detected": "Erstmalig festgestellt:",
        "last_seen": "Zuletzt gesehen:",
        "flag": "Grund der automatischen Kennzeichnung:",
        "category": "- {text} (Kategorie: {code})",
        "detection": (
            "Erkennung:\nEin automatisiertes textbasiertes Sicherheitssystem hat die Seite "
            "gekennzeichnet. Der verdächtige Medieninhalt wurde nicht durch einen Menschen "
            "geöffnet, angesehen, als Screenshot gesichert oder gespeichert."
        ),
        "media": "Medienabruf für diesen Vorfall:",
        "not_operator": (
            "Wir sind weder Betreiber noch Hosting-Provider noch Uploader des gemeldeten "
            "Angebots. Wir bitten um eine unabhängige Prüfung durch Ihre Stelle."
        ),
        "data": (
            "Übermittelte Daten:\n- URL(s)\n- Zeitstempel\n- maschineller Kennzeichnungsgrund\n"
            "- interne Vorfall-ID\n- keine Bild- oder Videodateien\n- keine Screenshots"
        ),
        "reporter": "Reporter:",
        "project": "Unabhängiges Monitoring-Projekt.",
        "contact_use": (
            "Die Kontaktdaten des Reporters werden ausschließlich für Rückfragen übermittelt. "
            "Bitte nicht an das gemeldete Angebot oder dessen Provider weitergeben, soweit keine "
            "rechtliche Verpflichtung hierzu besteht."
        ),
        "closing": "Mit freundlichen Grüßen",
    },
    "en": {
        "subject": (
            "Machine-generated suspicion of potentially unlawful content — URL report — not "
            "visually verified — {ref}"
        ),
        "supplement_subject": "Supplement: ",
        "greeting": "Hello,",
        "intro": (
            "I am reporting a machine-generated suspicion. This report does not assert that the "
            "material is in fact unlawful."
        ),
        "supplement": "This supplements our report of {date} ({hotline_ref}).",
        "hotline_ref": "your reference: {reference}",
        "no_hotline_ref": "no reference number was issued",
        "our_ref": "Our reference (internal incident ID):",
        "url_one": "URL:",
        "url_many": "URLs:",
        "more_urls": "Further URLs with the same content:",
        "url_line": "(site: {site}; first seen {first}; last seen {last}{extra})",
        "reason_line": "; linked because: {reason}",
        "new_line": "; new since our report",
        "detected": "First detected:",
        "last_seen": "Last seen:",
        "flag": "Reason for automated flag:",
        "category": "- {text} (category: {code})",
        "detection": (
            "Detection:\nAn automated text-based safety system flagged this page. No human "
            "opened, viewed, screenshotted or retained the suspected media."
        ),
        "media": "Media-fetch status for this incident:",
        "not_operator": (
            "We are not the operator, hosting provider or uploader of the reported service. "
            "Please assess the URL independently."
        ),
        "data": (
            "Data supplied:\n- URL(s)\n- timestamps\n- machine-generated flag reason\n"
            "- internal incident ID\n- no image or video files\n- no screenshots"
        ),
        "reporter": "Reporter:",
        "project": "Independent monitoring project.",
        "contact_use": (
            "Reporter contact information is supplied solely for case follow-up. Please do not "
            "disclose it to the reported service or its hosting provider unless legally required."
        ),
        "closing": "Kind regards,",
    },
}


def _url_block(item: DraftUrl, words: dict[str, str], language: str) -> str:
    extra = ""
    if not item.primary:
        reason = EVIDENCE_TEXT.get(item.evidence, EVIDENCE_TEXT[Evidence.MANUAL])[language]
        extra += words["reason_line"].format(reason=reason)
    if item.new_since_report:
        extra += words["new_line"]
    line = words["url_line"].format(
        site=item.site or "—",
        first=utc(item.first_seen_at),
        last=utc(item.last_seen_at),
        extra=extra,
    )
    return f"{item.url}\n{line}"


def render(facts: DraftFacts, language: Language) -> Draft:
    """The plain-text draft in one language. Pure: the same facts give the same text."""
    if language not in LANGUAGES:
        raise ValueError(f"unsupported language {language!r}")
    words = TEXT[language]
    supplement = facts.reported_at is not None
    subject = words["subject"].format(ref=facts.reference_id)
    if supplement:
        subject = words["supplement_subject"] + subject

    primary = [item for item in facts.urls if item.primary]
    further = [item for item in facts.urls if not item.primary]
    if not primary:
        # A case whose origin row is gone still has to name its addresses first.
        primary, further = further[:1], further[1:]

    blocks: list[str] = [words["greeting"], words["intro"]]
    if supplement and facts.reported_at is not None:
        hotline_ref = (
            words["hotline_ref"].format(reference=facts.hotline_reference)
            if facts.hotline_reference
            else words["no_hotline_ref"]
        )
        blocks.append(
            words["supplement"].format(
                date=facts.reported_at.astimezone(UTC).date().isoformat(), hotline_ref=hotline_ref
            )
        )
    blocks.append(f"{words['our_ref']}\n{facts.reference_id}")
    heading = words["url_one"] if len(primary) == 1 else words["url_many"]
    blocks.append("\n".join([heading, *(_url_block(item, words, language) for item in primary)]))
    if further:
        blocks.append(
            "\n".join(
                [words["more_urls"], *(_url_block(item, words, language) for item in further)]
            )
        )
    blocks.append(f"{words['detected']}\n{utc(facts.detected_at)}")
    blocks.append(f"{words['last_seen']}\n{utc(facts.last_seen_at)}")
    categories = facts.categories or ("",)
    blocks.append(
        "\n".join(
            [
                words["flag"],
                *(
                    words["category"].format(
                        text=CATEGORY_TEXT.get(code, CATEGORY_TEXT["other"])[language],
                        code=code or "manual",
                    )
                    for code in categories
                ),
            ]
        )
    )
    blocks.append(words["detection"])
    blocks.append(f"{words['media']}\n{MEDIA_FETCH_TEXT[facts.media_fetch][language]}")
    blocks.append(words["not_operator"])
    blocks.append(words["data"])
    reporter = [words["reporter"], REPORTER_NAME]
    if facts.reply_to:
        reporter.append(facts.reply_to)
    reporter.append("Germany")
    blocks.append("\n".join(reporter))
    blocks.append(words["project"])
    blocks.append(words["contact_use"])
    blocks.append(f"{words['closing']}\n{REPORTER_NAME}")
    return Draft(language=language, subject=subject, body="\n\n".join(blocks) + "\n")
