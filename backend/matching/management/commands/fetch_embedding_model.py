"""Fetch the pinned ONNX image encoder once per machine (ADR 0037).

The model is 45 MB of weights, so it is deliberately **not** in the repository and not in the
container image: it is downloaded here, checked against the digest pinned in
`matching.embedding`, and written atomically. A file that does not match the pin is never used
and never left behind.
"""

import shutil
import tempfile
from pathlib import Path
from typing import Any

import httpx
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from catalog.request_accounting import MODEL, RequestAccounting
from core.http import PoliteClient, RequestLabel, ResponseTooLargeError
from matching import embedding


class Command(BaseCommand):
    help = "Download and verify the frame-embedding model (ADR 0037); prints where it landed."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--path", help="Where to write it (default: MATCHING_EMBEDDING_MODEL_PATH)."
        )
        parser.add_argument(
            "--check", action="store_true", help="Only verify an existing file; download nothing."
        )
        parser.add_argument(
            "--force", action="store_true", help="Download again even if a valid file is there."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        target = embedding.model_path(
            options["path"] or settings.MATCHING_EMBEDDING_MODEL_PATH or None
        )
        if target.is_file() and not options["force"]:
            try:
                embedding.verify_model(target)
            except embedding.EmbeddingModelInvalid as exc:
                raise CommandError(f"{exc}\nDelete it or pass --force to download again.") from exc
            self.stdout.write(f"{target} is the pinned model ({embedding.MODEL_SHA256}).")
            return
        if options["check"]:
            raise CommandError(embedding.missing_model_message(target))
        self.stdout.write(f"Downloading {embedding.MODEL_URL}\n  -> {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        # Counted for the record as traffic of no site, like RDAP; no budget applies.
        with PoliteClient(
            settings.CRAWLER_USER_AGENT,
            max_bytes=embedding.MODEL_BYTES,
            timeout_s=120.0,
            observer=RequestAccounting(),
            label=RequestLabel(kind=MODEL, enforce=False),
        ) as client:
            try:
                response = client.get(embedding.MODEL_URL, max_bytes=embedding.MODEL_BYTES)
            except (httpx.HTTPError, ResponseTooLargeError) as exc:
                raise CommandError(f"could not download the model: {exc}") from exc
        if response.status != 200:
            raise CommandError(f"{embedding.MODEL_URL} answered {response.status}")
        self._write(target, response.body)
        embedding.verify_model(target)
        self.stdout.write(
            self.style.SUCCESS(
                f"{target}: {len(response.body)} bytes, sha256 {embedding.MODEL_SHA256} — "
                f"pipeline tag {embedding.EMBEDDING_PIPELINE}"
            )
        )

    def _write(self, target: Path, body: bytes) -> None:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
            handle.write(body)
            staged = Path(handle.name)
        try:
            embedding.verify_model(staged)
        except embedding.EmbeddingModelInvalid as exc:
            staged.unlink(missing_ok=True)
            raise CommandError(f"the downloaded file is not the pinned model: {exc}") from exc
        shutil.move(str(staged), target)
