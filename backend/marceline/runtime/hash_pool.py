"""PDQ hashing in child processes for the media lane (ADR 0030 § 1).

Decoding a 1080p poster holds the GIL for ~75 ms, and a decode bomb or a native crash in PIL,
pdqhash or ffmpeg must not take the fetch threads with it. `HashPool` keeps a few `spawn`ed
children that import only `matching.media_hash` (no Django, no database, no network): a thread
sends `(kind, bytes)` over the child's pipe and gets frames back. The bytes stay in memory — the
pipe is not a file, and ffmpeg's temporary directory is removed by `matching.frames` as before.

- A job over `deadline_s` kills its child and starts a new one; the caller gets `HashPoolError`
  and fails the media row (`hash error`), never leaving it RUNNING.
- A child that dies mid-job is replaced the same way.
- Children are recycled after `max_jobs` jobs or once their RSS passes `max_rss_mb`.
- Undecodable input raises `MediaDecodeError` in the caller, as in-thread hashing does; a sprite
  whose grid could not be determined raises `UnsupportedMediaError` the same way (issue #14).
- With `embed` (ADR 0037) each child also loads the embedding model **once**, on its first job,
  and every frame carries its sketch. That is why `max_rss_mb` is raised by the model's measured
  resident set here, and why `matching.embedding.hash_processes` decides how many children may
  exist at all: memory, not CPU, is the limit on the development box.

This module must stay importable without Django: the children import it.
"""

import contextlib
import logging
import multiprocessing
import queue
import threading
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess

from matching.embedding import MODEL_RSS_MB
from matching.media_hash import (
    HashedMedia,
    HasherUnavailableError,
    UnsupportedMediaError,
    hash_media_bytes,
)
from matching.pdq import HashedFrame, MediaDecodeError

logger = logging.getLogger(__name__)

DEFAULT_MAX_JOBS = 500
DEFAULT_MAX_RSS_MB = 300.0
STOP_TIMEOUT_S = 5.0


class HashPoolError(HasherUnavailableError):
    """A hash child timed out, died or failed unexpectedly; these bytes were never judged.

    `HasherUnavailableError` is what says so to the media worker, which retries the row rather
    than failing it for good: the child is what broke, not the media.
    """


def rss_mb() -> float:
    """This process's resident set size in MiB (Linux `/proc`; 0 where unavailable)."""
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        return 0.0
    return 0.0


def _child_main(connection: Connection) -> None:
    """A hash child: jobs in, frames out, until the pipe closes or `None` arrives."""
    while True:
        try:
            job = connection.recv()
        except (EOFError, OSError):
            return
        if job is None:
            return
        kind, data, crop_bars, embed, model_path, sprite_grid = job
        try:
            hashed = hash_media_bytes(
                kind,
                data,
                crop_bars=crop_bars,
                embed=embed,
                model_path=model_path,
                sprite_grid=sprite_grid,
            )
            reply: tuple[str, object, float] = (
                "ok",
                (
                    [tuple(frame) for frame in hashed.frames],
                    hashed.width,
                    hashed.height,
                    hashed.embedding_pipeline,
                ),
                rss_mb(),
            )
        except UnsupportedMediaError as exc:
            reply = ("unsupported", str(exc), rss_mb())
        except MediaDecodeError as exc:
            reply = ("decode", str(exc), rss_mb())
        except Exception as exc:
            reply = ("error", f"{type(exc).__name__}: {exc}", rss_mb())
        del data, job
        connection.send(reply)


@dataclass
class _Child:
    process: BaseProcess
    connection: Connection
    jobs: int = 0


class HashPool:
    """A fixed number of hash children shared by the media lane's threads."""

    def __init__(
        self,
        processes: int,
        *,
        deadline_s: float,
        max_jobs: int = DEFAULT_MAX_JOBS,
        max_rss_mb: float = DEFAULT_MAX_RSS_MB,
        embed: bool = False,
        model_path: str | None = None,
    ) -> None:
        self._context = multiprocessing.get_context("spawn")
        self._deadline_s = deadline_s
        self._embed = embed
        self._model_path = model_path
        # A child holding the embedding model sits ~160 MB higher (measured, ADR 0037), which the
        # ordinary recycling limit would read as a leak and restart on every single job.
        if embed:
            max_rss_mb += MODEL_RSS_MB
        self._max_jobs = max_jobs
        self._max_rss_mb = max_rss_mb
        self._idle: queue.Queue[_Child] = queue.Queue()
        self._size = max(processes, 1)
        self._closed = threading.Event()
        self.restarts = 0
        for _ in range(self._size):
            self._idle.put(self._spawn())

    def hash(
        self,
        kind: str,
        data: bytes,
        *,
        crop_bars: bool = False,
        sprite_grid: tuple[int, int] | None = None,
    ) -> HashedMedia:
        """Hash in a child; blocks while every child is busy."""
        if self._closed.is_set():
            raise HashPoolError("hash pool is closed")
        child = self._idle.get()
        try:
            try:
                child.connection.send(
                    (kind, data, crop_bars, self._embed, self._model_path, sprite_grid)
                )
                if not child.connection.poll(self._deadline_s):
                    child = self._replace(child, "timed out")
                    raise HashPoolError(f"hash timed out after {self._deadline_s:g} s")
                status, payload, child_rss = child.connection.recv()
            except (EOFError, OSError) as exc:
                child = self._replace(child, f"died: {type(exc).__name__}")
                raise HashPoolError(f"hash child died: {type(exc).__name__}") from exc
            child.jobs += 1
            if child.jobs >= self._max_jobs or float(child_rss) > self._max_rss_mb:
                child = self._replace(child, None)
        finally:
            self._idle.put(child)
        if status == "decode":
            raise MediaDecodeError(str(payload))
        if status == "unsupported":
            raise UnsupportedMediaError(str(payload))
        if status != "ok":
            raise HashPoolError(str(payload))
        frames, width, height, embedding_pipeline = payload
        return HashedMedia(
            [HashedFrame(*frame) for frame in frames], width, height, embedding_pipeline
        )

    def close(self) -> None:
        """Stop every child (idle ones ask to exit, stragglers are killed)."""
        self._closed.set()
        children: list[_Child] = []
        for _ in range(self._size):
            try:
                children.append(self._idle.get(timeout=STOP_TIMEOUT_S))
            except queue.Empty:
                break
        for child in children:
            with contextlib.suppress(OSError, ValueError):
                child.connection.send(None)
        for child in children:
            child.process.join(STOP_TIMEOUT_S)
            self._kill(child)

    def pids(self) -> list[int]:
        """Process ids of the idle children (tests)."""
        children = []
        while True:
            try:
                children.append(self._idle.get_nowait())
            except queue.Empty:
                break
        for child in children:
            self._idle.put(child)
        return [child.process.pid or 0 for child in children]

    def _spawn(self) -> _Child:
        parent_end, child_end = self._context.Pipe()
        process = self._context.Process(
            target=_child_main, args=(child_end,), name="hash-child", daemon=True
        )
        process.start()
        child_end.close()
        return _Child(process, parent_end)

    def _replace(self, child: _Child, reason: str | None) -> _Child:
        if reason is not None:
            self.restarts += 1
            logger.warning("hash child %s %s; starting a new one", child.process.pid, reason)
        else:
            try:
                child.connection.send(None)
                child.process.join(STOP_TIMEOUT_S)
            except (OSError, ValueError):
                pass
        self._kill(child)
        return self._spawn()

    @staticmethod
    def _kill(child: _Child) -> None:
        if child.process.is_alive():
            child.process.kill()
            child.process.join(STOP_TIMEOUT_S)
        child.connection.close()
