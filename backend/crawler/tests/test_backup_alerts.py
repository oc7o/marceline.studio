"""Backup failure alert (issue #39): crawler/backup_alerts.py, docs/runbooks/backup-restore.md."""

from collections.abc import Generator
from pathlib import Path

import pytest
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from pytest_django.fixtures import Settings

from crawler import backup_alerts

# Django's test environment swaps every MAILERS alias to locmem, so mail.outbox works without a
# network and without any EMAIL_* variable being set (same as test_safety_alerts.py).


@pytest.fixture(autouse=True)
def _clean_alert_env(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Generator[None]:
    """The alert reads its addresses from the environment, which the developer's shell or .env may
    already carry; start every test from "unconfigured"."""
    monkeypatch.delenv("BACKUP_ALERT_TO", raising=False)
    monkeypatch.delenv("BACKUP_ALERT_FROM", raising=False)
    settings.SAFETY_ALERT_TO = ""
    settings.SAFETY_ALERT_FROM = ""
    yield


def configure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKUP_ALERT_TO", "ops@example.test")
    monkeypatch.setenv("BACKUP_ALERT_FROM", "marceline@example.test")


def test_off_by_default() -> None:
    assert backup_alerts.configured() is False
    assert backup_alerts.send_backup_alert("backup failed", "pg_dump exploded") is False
    assert mail.outbox == []


def test_sends_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch)

    assert backup_alerts.send_backup_alert("run failed", "FAIL backup: pg_dump exploded") is True

    [sent] = mail.outbox
    assert sent.subject == "Marceline backup: run failed"
    assert sent.to == ["ops@example.test"]
    assert sent.from_email == "marceline@example.test"
    assert "pg_dump exploded" in sent.body
    assert "docs/runbooks/backup-restore.md" in sent.body


def test_falls_back_to_the_safety_alert_mailbox(settings: Settings) -> None:
    """One operator, one mailbox: BACKUP_ALERT_* is optional on top of SAFETY_ALERT_* (#40)."""
    settings.SAFETY_ALERT_TO = "ops@example.test, second@example.test"
    settings.SAFETY_ALERT_FROM = "marceline@example.test"

    assert backup_alerts.send_backup_alert("overdue", "no backup for 40h") is True

    [sent] = mail.outbox
    assert sent.to == ["ops@example.test", "second@example.test"]


def test_needs_both_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKUP_ALERT_TO", "ops@example.test")

    assert backup_alerts.configured() is False
    assert backup_alerts.send_backup_alert("run failed", "boom") is False
    assert mail.outbox == []


def test_subject_is_one_capped_line(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch)

    backup_alerts.send_backup_alert("a " * 200 + "\nsecond line", "body")

    [sent] = mail.outbox
    assert "\n" not in sent.subject
    assert len(sent.subject) <= len(backup_alerts.SUBJECT_PREFIX) + backup_alerts.MAX_SUBJECT_CHARS


def test_long_body_keeps_the_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    """The error that ended the run is the last line of a script's output, never the first."""
    configure(monkeypatch)
    body = "\n".join(f"line {i}" for i in range(2000)) + "\nFAIL restic: repository unreachable"

    backup_alerts.send_backup_alert("run failed", body)

    [sent] = mail.outbox
    assert "FAIL restic: repository unreachable" in sent.body
    assert "line 0\n" not in sent.body
    assert "earlier output omitted" in sent.body


def test_a_send_failure_is_logged_and_never_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    configure(monkeypatch)

    class BoomingMessage:
        def __init__(self, **kwargs: object) -> None:
            pass

        def send(self, **kwargs: object) -> int:
            raise RuntimeError("smtp exploded")

    monkeypatch.setattr(backup_alerts, "EmailMessage", BoomingMessage)

    with caplog.at_level("ERROR"):
        assert backup_alerts.send_backup_alert("run failed", "boom") is False

    assert "backup alert" in caplog.text.lower()


def test_command_sends_a_body_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    configure(monkeypatch)
    body_file = tmp_path / "out.log"
    body_file.write_text("FAIL backup: dump is empty")

    call_command("backup_alert", "--subject", "run failed", "--body-file", str(body_file))

    [sent] = mail.outbox
    assert "dump is empty" in sent.body


def test_command_is_quiet_and_successful_when_unconfigured(tmp_path: Path) -> None:
    """The backup scripts call this on every failure; an unconfigured alert must not turn into a
    second failure on top of the one being reported."""
    call_command("backup_alert", "--subject", "run failed", "--body", "boom")

    assert mail.outbox == []


def test_command_rejects_a_missing_body(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch)

    with pytest.raises(CommandError):
        call_command("backup_alert", "--subject", "run failed")
