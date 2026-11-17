"""Opaque public ids: what a customer may see and type instead of a primary key (ADR 0033).

A sequential primary key in a customer URL or payload tells the customer how many rows exist
system-wide and how fast they grow (issue #77). Anything the customer API names therefore carries
a second, random identifier, and only that one ever leaves through `/api/v1/app/…`. Internal and
staff APIs keep their integer ids.

**Format:** `<prefix>_<32 lowercase hex>`, e.g. `orig_3f9c…`. The hex part is a random UUIDv4 —
122 random bits from the operating system's CSPRNG (`uuid.uuid4`) or from Postgres's
`gen_random_uuid()`, which produce the same shape. The prefix names the kind of object, so an
organisation id pasted where an original id belongs is simply unknown, never a different row.

**Adopting it for a new model** (e.g. invoices):

    from core.public_ids import public_id_field

    class Invoice(models.Model):
        public_id = public_id_field("inv")

and `make makemigrations`. The field is unique, not editable, generated in Python on create and
**also by the database** (`db_default`): adding the column to a table that already has rows fills
every row with its own random id in the same statement (no data migration), and a stale worker
process or a raw `INSERT` that does not know the column still gets one.

**Looking one up:** check the shape first with `is_valid(prefix, value)` and answer a malformed id
exactly like an unknown one — the customer API's 404 rule (ADR 0021) makes "malformed", "unknown"
and "someone else's" the same answer. Scope the query by the owner, never by the id alone.

`missing(prefix)` is a well-formed id that is never generated (a UUIDv4 always has version nibble
4, and this one is all zeros): render it to get the exact "not found" answer for a kind.
"""

import re
import uuid
from functools import partial
from typing import Any

from django.db import models
from django.db.models import Func

HEX_LENGTH = 32
PREFIX = re.compile(r"^[a-z]{2,8}$")
#: Longest prefix (8) + `_` + 32 hex, with room to spare.
MAX_LENGTH = 48


def _checked(prefix: str) -> str:
    if not PREFIX.match(prefix):
        raise ValueError(f"public id prefix must be 2-8 lowercase letters, not {prefix!r}")
    return prefix


def generate(prefix: str) -> str:
    """A new public id: `<prefix>_` and the 32 hex digits of a random UUIDv4."""
    return f"{_checked(prefix)}_{uuid.uuid4().hex}"


def missing(prefix: str) -> str:
    """A well-formed id of this kind that no row ever has."""
    return f"{_checked(prefix)}_{'0' * HEX_LENGTH}"


def pattern(prefix: str) -> str:
    """The regular expression (unanchored) matching ids of this kind."""
    return f"{_checked(prefix)}_[0-9a-f]{{{HEX_LENGTH}}}"


#: Any public id of any kind (unanchored), for code that stores ids it does not interpret.
ANY_PATTERN = f"[a-z]{{2,8}}_[0-9a-f]{{{HEX_LENGTH}}}"


def is_valid(prefix: str, value: object) -> bool:
    """Whether `value` has the shape of an id of this kind. Says nothing about whether it exists."""
    return isinstance(value, str) and re.fullmatch(pattern(prefix), value) is not None


class RandomPublicId(Func):
    """The database's twin of `generate`, for `db_default`: one fresh id per inserted row."""

    output_field = models.CharField(max_length=MAX_LENGTH)

    def __init__(self, prefix: str, **extra: Any) -> None:
        # The prefix is checked against `PREFIX` (lowercase letters only), so it is safe as a
        # literal in the SQL template.
        template = f"('{_checked(prefix)}_' || replace(gen_random_uuid()::text, '-', ''))"
        super().__init__(template=template, **extra)


def public_id_field(prefix: str, *, help_text: str = "") -> models.CharField[str, str]:
    """A unique, random, non-editable public id column for ids of this kind."""
    return models.CharField(
        max_length=MAX_LENGTH,
        unique=True,
        editable=False,
        default=partial(generate, _checked(prefix)),
        db_default=RandomPublicId(prefix),
        help_text=help_text
        or "Opaque id shown to customers instead of the primary key (ADR 0033). Never reused.",
    )
