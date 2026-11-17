"""`manage.py rights_housekeeping`: expiry then retention, dry run first (ADR 0034)."""

from datetime import date
from io import StringIO

import pytest
from django.core.management import call_command

from marceline.rights.models import WorkRights
from marceline.tests.app_helpers import make_organisation
from marceline.tests.rights_helpers import documents_complete, mandate, staff_user


@pytest.mark.django_db
def test_dry_run_reports_and_the_real_run_expires() -> None:
    organisation = make_organisation("Agency")
    work = documents_complete(organisation, "9" * 64, staff_user())
    WorkRights.objects.filter(pk=work.pk).update(
        mandate=mandate(organisation, valid_until=date(2026, 9, 1))
    )

    dry = StringIO()
    call_command("rights_housekeeping", "--dry-run", "--date", "2026-09-17", stdout=dry)
    assert "would expire 1 works" in dry.getvalue()
    assert WorkRights.objects.get(pk=work.pk).status == "documents_complete"

    real = StringIO()
    call_command("rights_housekeeping", "--date", "2026-09-17", stdout=real)
    assert "expire 1 works" in real.getvalue()
    assert "delete works [], mandates [], proofs []" in real.getvalue()
    assert WorkRights.objects.get(pk=work.pk).status == "expired"
