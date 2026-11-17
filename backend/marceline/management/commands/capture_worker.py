"""Capture frames from queued candidate pages with a headless browser (ADR 0016).

Deliberately not part of `make workers`: capture is off unless the operator sets
BROWSER_CAPTURE_ENABLED, runs only inside BROWSER_CAPTURE_WINDOW_UTC, and needs the Playwright
install from `frontend/parity` on the host.
"""

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser

from catalog.request_accounting import RequestAccounting
from core.http import PoliteClient
from marceline.capture.runner import NodePlaywrightRunner, browser_available, playwright_dir
from marceline.capture.worker import (
    CaptureWorker,
    plan_from_settings,
    refusal_report,
    single_capture_lock,
)


class Command(BaseCommand):
    help = (
        "Sample frames from queued browser-capture candidates and store PDQ hashes only "
        "(ADR 0016). Never runs on safety-flagged, gated, removed or disabled-site pages, and "
        "never works around a login, CAPTCHA, paywall or age gate."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--site", dest="site_domain", help="Only candidates of this site.")
        parser.add_argument("--limit", type=int, help="Stop after N visits.")
        parser.add_argument(
            "--refusal-report",
            action="store_true",
            help="Print which hosts and pages past visits were refused for, and stop (issue #78 "
            "amendment): read-only, no browser, works even with BROWSER_CAPTURE_ENABLED unset.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options.get("refusal_report"):
            for line in refusal_report(site_domain=options.get("site_domain")):
                self.stdout.write(line)
            return
        if not getattr(settings, "BROWSER_CAPTURE_ENABLED", False):
            self.stdout.write(
                "Browser capture is disabled. Set BROWSER_CAPTURE_ENABLED=true to enable it "
                "(ADR 0016); triage keeps running either way."
            )
            return
        if not browser_available():
            self.stdout.write(
                f"No browser available: Playwright ({playwright_dir()}) or the "
                f"{settings.BROWSER_CAPTURE_ENGINE} build is not installed. Once, from "
                "frontend/parity: npm install && npx playwright install chromium"
            )
            return
        plan = plan_from_settings()
        with single_capture_lock() as acquired:
            if not acquired:
                self.stdout.write("Another capture worker is running; nothing to do.")
                return
            self.stdout.write(
                f"Caps per capture: {plan.max_frames} frames, {plan.max_seconds:g} s sampling, "
                f"{plan.max_bytes / 1_048_576:.0f} MiB, {plan.timeout_s:g} s watchdog"
            )
            with PoliteClient(
                str(settings.CRAWLER_USER_AGENT), observer=RequestAccounting()
            ) as client:
                worker = CaptureWorker(NodePlaywrightRunner(), client, plan=plan)
                report = worker.run(
                    site_domain=options.get("site_domain"), limit=options.get("limit")
                )
        for line in report.lines():
            self.stdout.write(line)
