"""PDQ frames for downloaded media bytes, by media kind — without Django.

The media worker hashes in its own thread; the crawl runtime's media lane sends the same call to a
hash child process (`marceline.runtime.hash_pool`), which must not import Django, the database or
the network. Kinds are the string values of `index.Media.Kind` (`test_media_hash.py` keeps them in
step). Bytes live in memory only; ffmpeg's temporary directory is removed by `matching.frames`.
"""

from dataclasses import dataclass

from matching import embedding
from matching.frames import Embedder, hash_frames, hash_video, split_sprite
from matching.pdq import HashedFrame, open_image
from matching.sprite_grid import detect_grid

# Still images: one decode, one PDQ frame — the `matching.frames.hash_poster` path. A photo post
# is the work itself rather than a preview (issue #69), but it decodes like any other image.
STILL_IMAGE_KINDS = frozenset({"poster", "frame", "photo"})
PREVIEW_KIND = "preview"
SPRITE_KIND = "sprite"
# Preview clips (site03 `vidthumb.mp4`, a few hundred KB) are sampled every 0.5 s, spread over
# the clip when it is longer than PREVIEW_MAX_FRAMES / PREVIEW_FPS seconds. Consecutive frames
# within PREVIEW_DUPLICATE_DISTANCE bits of the previous kept frame are the same shot and dropped.
PREVIEW_FPS = 2.0
PREVIEW_MAX_FRAMES = 32
PREVIEW_DUPLICATE_DISTANCE = 8


class UnsupportedMediaError(Exception):
    """The media kind cannot be hashed with the information we have."""


class HasherUnavailableError(Exception):
    """The hasher could not run at all, so these bytes were never judged.

    A property of this machine at this moment — a hash child that timed out, died or was shut
    down (`marceline.runtime.hash_pool.HashPoolError` subclasses this) — never of the bytes.
    `MediaDecodeError` is the opposite: the picture itself is undecodable and a retry is pointless.
    The media worker therefore retries this one instead of failing the row for good; frame hashes
    are the product, and dropping a fetched, decodable media because a child was killed loses
    evidence nothing will ask for again.
    """


@dataclass(frozen=True)
class HashedMedia:
    frames: list[HashedFrame]
    width: int | None = None
    height: int | None = None
    # Which embedding recipe produced the frames' sketches; "" when none were computed.
    embedding_pipeline: str = ""


def embedder(model_path: str | None = None) -> Embedder:
    """The process's sketch function, building its session on first use (ADR 0037).

    Raises `EmbeddingModelMissing` / `EmbeddingUnavailable` loudly rather than returning None:
    a run with embeddings switched on that quietly produced no sketches would leave a silent hole
    in the index that nothing downstream could tell from "this picture has no second signal".
    """
    return embedding.encoder(model_path).sketch


def hash_media_bytes(
    kind: str,
    data: bytes,
    *,
    crop_bars: bool = False,
    embed: bool = False,
    model_path: str | None = None,
    sprite_grid: tuple[int, int] | None = None,
) -> HashedMedia:
    """PDQ frames for media bytes. Raises MediaDecodeError for undecodable input.

    `crop_bars` trims letterbox/pillarbox padding before hashing (`matching.black_bars`); the
    caller passes what `settings.MATCHING_CROP_BLACK_BARS` says, because this module must stay
    Django-free. `width`/`height` stay the media's real dimensions, cropped or not.

    With `embed`, every kept frame also carries its 512-bit sketch (ADR 0037), computed from the
    same picture PDQ saw, in the same call, before the bytes are discarded. `model_path` is what
    `settings.MATCHING_EMBEDDING_MODEL_PATH` says — passed in for the same Django-free reason.

    `sprite_grid` is the sheet's (columns, rows) when an adapter read it off the site itself
    (`index.Media.sprite_columns`/`sprite_rows`, issue #14); only consulted for `kind == "sprite"`.
    Without it, the sheet's own geometry is tried (`matching.sprite_grid.detect_grid`) and, failing
    that too, `UnsupportedMediaError` is raised — refusing rather than hashing a guessed grid,
    which would produce garbage tiles no later step could ever remove from the index.
    """
    sketch = embedder(model_path) if embed else None
    tag = embedding.EMBEDDING_PIPELINE if embed else ""
    if kind in STILL_IMAGE_KINDS:
        image = open_image(data)
        return HashedMedia(
            hash_frames([image], crop_bars=crop_bars, embed=sketch),
            image.width,
            image.height,
            tag,
        )
    if kind == PREVIEW_KIND:
        return HashedMedia(
            hash_video(
                data,
                fps=PREVIEW_FPS,
                max_frames=PREVIEW_MAX_FRAMES,
                max_duplicate_distance=PREVIEW_DUPLICATE_DISTANCE,
                crop_bars=crop_bars,
                embed=sketch,
            ),
            embedding_pipeline=tag,
        )
    if kind == SPRITE_KIND:
        image = open_image(data)
        grid = sprite_grid or detect_grid(image.width, image.height)
        if grid is None:
            raise UnsupportedMediaError("cannot hash sprite media: sprite grid unknown")
        tiles = split_sprite(image, grid=grid)
        return HashedMedia(
            hash_frames(tiles, crop_bars=crop_bars, embed=sketch),
            image.width,
            image.height,
            tag,
        )
    raise UnsupportedMediaError(f"cannot hash {kind} media: unsupported kind")
