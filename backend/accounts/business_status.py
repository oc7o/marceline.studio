"""Is this organisation evidenced as a business, and what happens when it is not (issue #92).

The pure half of `accounts.BusinessStatus`: which facts are missing, what an EU member state is,
and the one guard that reads the answer. It imports no model, so `accounts/models.py` can import
it without a cycle; every function takes the organisation object and follows the relation.

**Why this exists.** A signed *Unternehmer* declaration alone does not make a customer a business:
a tick-box converts nobody. A B2B-only pilot rests on the objective evidence we looked at and kept
a record of, at the time we signed — which is what `BusinessStatus` stores.

**The EU flag is not a blocker.** The pilot is EU-only (operator decision on issue #95, item 7),
but a Swiss company's commercial register extract is not *missing* evidence — it is complete
evidence of a party outside the pilot's scope. Scope is a commercial decision, so a non-EU country
is recorded, reported and warned about, and never refused here.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist

if TYPE_CHECKING:  # pragma: no cover - typing only
    from accounts.models import Organisation

#: ISO 3166-1 alpha-2 codes of the 27 EU member states. The pilot is EU-only (issue #95, item 7).
#: Greece is `GR` here: that is the ISO code. Its VAT identification numbers start `EL`, which is
#: a VAT prefix and not a country code, so it never belongs in this set.
EU_MEMBER_STATES: frozenset[str] = frozenset(
    {
        "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
        "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
    }
)  # fmt: skip

#: A country code is exactly two capital letters. Deliberately not a list of every country: a
#: closed list would have to be maintained, and the point of the field is that a country outside
#: the EU can still be *recorded*.
COUNTRY_CODE = re.compile(r"^[A-Z]{2}$")

#: What is missing, as codes. Never a sentence: the words belong to the caller (the admin message,
#: the management command, a future report), the codes are what tests and callers compare.
NO_RECORD = "no_business_status_record"
NO_EVIDENCE_KIND = "no_evidence_kind"
EVIDENCE_NOTE_MISSING = "evidence_note_missing"
VAT_ID_MISSING = "vat_id_missing"
NOT_CHECKED = "not_checked"
COUNTRY_MISSING = "country_missing"
DECLARATION_MISSING = "declaration_missing"
#: No declaration of majority and full legal capacity on file (issue #123). A gap like the others:
#: a contract with a minor is voidable, and nothing here can tell that from the outside.
AGE_DECLARATION_MISSING = "age_declaration_missing"

#: Not a gap in the evidence — a party outside the pilot's scope. Reported, never refused.
OUTSIDE_EU = "country_outside_eu"

#: Every code, in the order a message lists them. A test pins the order so a new code has to be
#: placed deliberately.
CODES: tuple[str, ...] = (
    NO_RECORD,
    NO_EVIDENCE_KIND,
    EVIDENCE_NOTE_MISSING,
    VAT_ID_MISSING,
    NOT_CHECKED,
    COUNTRY_MISSING,
    DECLARATION_MISSING,
    AGE_DECLARATION_MISSING,
    OUTSIDE_EU,
)

#: Codes that mean "we have not evidenced this". `OUTSIDE_EU` is deliberately not one of them.
MISSING_CODES: frozenset[str] = frozenset(CODES) - {OUTSIDE_EU}

GUARD_WARN = "warn"
GUARD_REFUSE = "refuse"


def guard_mode() -> str:
    """`warn` (the default) or `refuse`. Anything else is read as `warn`.

    An unreadable value must not silently turn a guard *on*: a typo in `.env` that refused every
    membership would look exactly like a bug in the admin.
    """
    configured = str(getattr(settings, "ONBOARDING_BUSINESS_STATUS_GUARD", GUARD_WARN)).strip()
    return GUARD_REFUSE if configured.lower() == GUARD_REFUSE else GUARD_WARN


def business_status_codes(organisation: Organisation) -> list[str]:
    """What is missing or noteworthy about this organisation's business status, in `CODES` order.

    A sandbox organisation (ADR 0035: the demo dataset, and nothing else) answers with nothing at
    all. It is not a contracting party, there is no signature behind it, and demanding a commercial
    register extract for fictional data would only teach the operator to ignore the warning.
    """
    if organisation.is_sandbox:
        return []
    try:
        record = organisation.business_status
    except ObjectDoesNotExist:
        return [NO_RECORD]
    return record.codes()


def business_status_evidenced(organisation: Organisation) -> bool:
    """Did a human look at objective evidence and record what they saw?

    True for a sandbox organisation: there is nothing to evidence and nothing is blocked by it.
    """
    return not (set(business_status_codes(organisation)) & MISSING_CODES)


def business_status_message(organisation: Organisation) -> str | None:
    """One line for a human, or `None` when there is nothing to say.

    Identifiers only — the organisation's name, its id and the codes. Never the evidence note,
    which is staff's own prose about a document.
    """
    codes = business_status_codes(organisation)
    if not codes:
        return None
    return (
        f"organisation {organisation.pk} ({organisation.name}): business status "
        f"{', '.join(codes)} — the pilot is B2B-only and EU-only (issue #92); record the evidence "
        f"in admin *Accounts → Organisations → business status* before the customer gets access"
    )


class BusinessStatusNotEvidenced(Exception):
    """Raised instead of granting access while `ONBOARDING_BUSINESS_STATUS_GUARD=refuse`."""


def check_before_access(organisation: Organisation, *, enforce: bool | None = None) -> str | None:
    """The guard on the staff-side step that gives a customer access to the app.

    Access is the `Membership` row: `GET /api/v1/app/me` reports `can_add_originals` exactly when
    one exists and this machine takes writes (`marceline/app/account.py`), so creating the
    membership *is* the grant, and this is the moment the evidence has to be on file.

    Returns the message to show, and raises `BusinessStatusNotEvidenced` when the guard is set to
    refuse **and** evidence is actually missing. A country outside the EU is always only a message:
    see the module docstring.

    `enforce` overrides the setting. `seed_test_customer` passes `False` — it is the development
    test account, never a contracting party, and a seeding command that can be refused by a
    production-shaped setting is a trap rather than a guard.
    """
    message = business_status_message(organisation)
    if message is None:
        return None
    refusing = guard_mode() == GUARD_REFUSE if enforce is None else enforce
    if refusing and not business_status_evidenced(organisation):
        raise BusinessStatusNotEvidenced(message)
    return message
