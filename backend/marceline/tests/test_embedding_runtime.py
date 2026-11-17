"""Turning the second signal on must not be able to break the media lane (ADR 0037).

The crawl runtime is where embeddings run, so it is where the two failure modes live: a missing
model (which would silently produce no sketches) and a memory budget the hash children do not fit
into (which would get the lane OOM-killed, as the research battery already was once).
"""

from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from pytest_django.fixtures import Settings

from marceline.runtime.hash_pool import DEFAULT_MAX_RSS_MB, HashPool
from matching.embedding import MODEL_RSS_MB


def test_the_media_lane_refuses_to_start_without_the_model(
    settings: Settings, tmp_path: Path
) -> None:
    settings.MATCHING_EMBEDDINGS_ENABLED = True
    settings.MATCHING_EMBEDDING_MODEL_PATH = str(tmp_path / "absent.onnx")
    settings.RUNTIME_SITES = ["example.test"]
    with pytest.raises(CommandError, match="fetch_embedding_model"):
        call_command("crawl_runtime", "--lane", "media", "--once", stdout=StringIO())


def test_the_pages_lane_does_not_need_the_model(settings: Settings, tmp_path: Path) -> None:
    """Only the media lane hashes pictures, so only it needs an encoder."""
    settings.MATCHING_EMBEDDINGS_ENABLED = True
    settings.MATCHING_EMBEDDING_MODEL_PATH = str(tmp_path / "absent.onnx")
    settings.RUNTIME_SITES = []
    # No sites configured, so it stops for that reason — never for the missing model.
    with pytest.raises(CommandError, match="no sites to serve"):
        call_command("crawl_runtime", "--lane", "pages", "--once", stdout=StringIO())


def test_the_hash_pool_raises_its_memory_limit_for_the_model() -> None:
    """A child holding a 160 MB model is not a leaking child, so its ceiling moves with it."""
    plain = HashPool(1, deadline_s=5.0)
    try:
        assert plain._max_rss_mb == DEFAULT_MAX_RSS_MB
    finally:
        plain.close()
    embedded = HashPool(1, deadline_s=5.0, embed=True, model_path="/nonexistent.onnx")
    try:
        assert embedded._max_rss_mb == DEFAULT_MAX_RSS_MB + MODEL_RSS_MB
    finally:
        embedded.close()


def test_the_media_worker_does_not_embed_just_because_the_setting_is_on(
    settings: Settings,
) -> None:
    """Twenty legacy per-site workers holding a model each would not fit; `--embed` is explicit."""
    from core.http import PoliteClient
    from matching.media_worker import MediaWorker

    settings.MATCHING_EMBEDDINGS_ENABLED = True
    client = PoliteClient("test-agent")
    try:
        assert MediaWorker(client, embed=False)._embed is False
        # The setting still reaches the worker when nobody decides for it (the runtime's path).
        assert MediaWorker(client)._embed is True
    finally:
        client.close()
