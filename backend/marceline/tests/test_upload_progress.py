"""Fingerprinting progress for a customer upload (issue #59, ADR 0044 amendment 2026-09-18).

The bar the customer watches is written by the *parent* worker from two integers its contained
child reports over the pipe it already has — the child still holds no database credential and this
process still never opens the file. Two claims are worth keeping executable:

1. **It is throttled.** The decoder reports every sampled moment; a 45-minute original at 1 fps is
   2,700 of them, and each one must not be an `UPDATE`.
2. **It is numbers.** `ProgressWriter` takes two integers and nothing else, so there is no route by
   which a file name, a path or a decoder's complaint about the customer's bytes could reach a row.
"""

from __future__ import annotations

from typing import Any

import pytest

from accounts.models import Organisation
from marceline.models import MediaUpload
from marceline.tests.app_helpers import add_member, make_organisation
from marceline.uploads.service import ProgressWriter


class Clock:
    """A hand-wound monotonic clock, so a throttle is tested in a millisecond."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def a_job(**overrides: Any) -> MediaUpload:
    """An unsaved job row: the writer under test is handed its own `write`, so nothing is stored."""
    fields: dict[str, Any] = {
        "declared_bytes": 1024,
        "title": "An original",
        "creators": ["A Creator"],
        "frame_fps": 1.0,
        "chunk_bytes": 1024,
        "rights_text_version": "v1",
        "rights_language": "en",
    }
    fields.update(overrides)
    return MediaUpload(**fields)


def recorder() -> tuple[list[tuple[int, int | None]], Any]:
    written: list[tuple[int, int | None]] = []
    return written, lambda frames, total: written.append((frames, total))


def test_a_whole_percent_is_the_most_that_is_ever_written() -> None:
    written, write = recorder()
    clock = Clock()
    progress = ProgressWriter(a_job(), clock=clock, min_interval_s=0.0, write=write)

    for frame in range(0, 300):
        progress(frame, 300)

    # 300 moments, one percent each, so at most 100 updates and not 300.
    assert len(written) <= 100
    assert written[0] == (0, 300)
    assert [total for _, total in written] == [300] * len(written)


def test_two_updates_in_the_same_second_are_one() -> None:
    written, write = recorder()
    clock = Clock()
    progress = ProgressWriter(a_job(), clock=clock, min_interval_s=1.0, write=write)

    progress(0, 100)
    progress(1, 100)  # a whole percent later, but in the same second
    progress(2, 100)
    assert written == [(0, 100)]

    clock.now += 1.0
    progress(3, 100)
    assert written == [(0, 100), (3, 100)]


def test_a_total_nobody_knows_yet_is_written_as_null_and_never_invented() -> None:
    written, write = recorder()
    clock = Clock()
    progress = ProgressWriter(a_job(), clock=clock, min_interval_s=1.0, write=write)

    progress(0, None)
    clock.now += 1.0
    progress(7, None)
    clock.now += 1.0
    progress(9, 60)

    assert written == [(0, None), (7, None), (9, 60)]


def test_the_frame_count_is_not_written_backwards_by_a_repeated_line() -> None:
    written, write = recorder()
    clock = Clock()
    progress = ProgressWriter(a_job(), clock=clock, min_interval_s=0.0, write=write)

    progress(50, 100)
    progress(50, 100)
    progress(50, 100)

    assert written == [(50, 100)]


def test_flush_writes_the_final_count_even_inside_the_throttle_window() -> None:
    """The trial's first defect: the decode's own last update can land in the same throttled
    second as the one before it and be dropped, so a finished job settles on a stale count (23 of
    30). `run_job` calls `flush` once, after `hash_file` has returned, with the real total — this
    is the one write allowed to ignore the throttle."""
    written, write = recorder()
    clock = Clock()
    progress = ProgressWriter(a_job(), clock=clock, min_interval_s=1.0, write=write)

    progress(0, 30)
    progress(29, 30)  # same second as the line above: dropped by the throttle, as the trial found
    assert written == [(0, 30)]

    progress.flush(30, 30)
    assert written[-1] == (30, 30)


def test_flush_is_not_itself_throttled_by_a_later_repeat() -> None:
    written, write = recorder()
    clock = Clock()
    progress = ProgressWriter(a_job(), clock=clock, min_interval_s=1.0, write=write)

    progress.flush(30, 30)
    progress.flush(30, 30)

    assert written == [(30, 30), (30, 30)]


@pytest.mark.django_db
def test_the_counters_land_on_the_job_row() -> None:
    organisation: Organisation = make_organisation("Studio P")
    job = MediaUpload.objects.create(
        organisation=organisation,
        created_by=add_member(organisation, "customer-p"),
        declared_bytes=1024,
        title="An original",
        creators=["A Creator"],
        frame_fps=1.0,
        chunk_bytes=1024,
        rights_text_version="v1",
        rights_language="en",
        status=MediaUpload.Status.HASHING,
    )

    progress = ProgressWriter(job, clock=Clock(), min_interval_s=0.0)
    progress(12, 60)

    job.refresh_from_db()
    assert (job.frames_done, job.frames_total) == (12, 60)


@pytest.mark.django_db
def test_claiming_a_job_starts_its_bar_at_nothing() -> None:
    from marceline.uploads import service

    organisation: Organisation = make_organisation("Studio Q")
    MediaUpload.objects.create(
        organisation=organisation,
        created_by=add_member(organisation, "customer-q"),
        declared_bytes=1024,
        title="An original",
        creators=["A Creator"],
        frame_fps=1.0,
        chunk_bytes=1024,
        rights_text_version="v1",
        rights_language="en",
        status=MediaUpload.Status.QUEUED,
        frames_done=99,
        frames_total=99,
    )

    claimed = service.claim()
    assert claimed is not None
    claimed.refresh_from_db()
    assert (claimed.frames_done, claimed.frames_total) == (0, None)
