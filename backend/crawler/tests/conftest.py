from collections.abc import Generator

import pytest

from crawler import alerts


@pytest.fixture(autouse=True)
def _reset_safety_alert_state() -> Generator[None]:
    """`crawler.alerts` keeps its pending batch and last-sent time in process memory by design
    (docs/safety.md); isolate that module-level state between tests."""
    alerts.reset_batch_state()
    yield
    alerts.reset_batch_state()
