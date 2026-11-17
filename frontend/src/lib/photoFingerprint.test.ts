// Parity with the CLI for a photo collection (issue #116, ADR 0053).
//
// `backend/matching/tests/fixtures/photos/collection.json` is written by
// `matching/tests/generate_photo_fixtures.py` from `fingerprint_photos` — the very code that
// imports the operator's own photo sets — and is read here straight out of the backend tree, so
// there is one copy and it cannot drift. `matching/tests/test_photo_fixtures.py` asserts the
// Python side still produces it.
//
// **What this proves, and what it cannot.** Given identical pixels, the browser produces the same
// eight hashes per image, the same quality, the same ordinal rule and — the part that matters most
// — the same collection digest. It says nothing about the *decoder*: a browser's JPEG decoder is
// not Pillow's, which is exactly why the browser recipe carries its own pipeline tag, and the gap
// is measured in a real browser by `make browser-parity PARITY_ARGS="--photos"`.
//
// Never weaken an assertion here: hashes that drift silently look like a matching failure rather
// than a build failure.

import { describe, expect, it } from "vitest";

import { hashDihedral, toHex } from "./pdq";
import {
  BROWSER_PHOTO_PIPELINE,
  FINGERPRINT_KIND_PHOTOS,
  type HashedImage,
  MIN_QUALITY,
  ORIENTATIONS,
  buildPhotoFingerprint,
  collectionSha256,
  collectionTitle,
  imageSha256,
  photoFingerprintProblems,
} from "./photoFingerprint";

interface FixtureImage {
  frame_index: number;
  sha256: string;
  size: number;
  width: number;
  height: number;
  rgb_base64: string;
}

interface FixtureFrame {
  frame_index: number;
  dihedral: number;
  quality: number;
  pdq: string;
}

interface Fixture {
  images: FixtureImage[];
  document: {
    kind: string;
    pipeline: string;
    sha256: string;
    image_count: number;
    frames: FixtureFrame[];
  };
}

const modules = import.meta.glob<Fixture>(
  "../../../backend/matching/tests/fixtures/photos/*.json",
  { eager: true, import: "default" },
);
const fixture = Object.values(modules)[0] as Fixture;

function rgbOf(image: FixtureImage): Uint8Array {
  const binary = atob(image.rgb_base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/** The CLI's hashes for one image, in orientation order; empty below the quality floor. */
function storedFrames(index: number): FixtureFrame[] {
  return fixture.document.frames
    .filter((frame) => frame.frame_index === index)
    .sort((left, right) => left.dihedral - right.dihedral);
}

/** What the Worker would hand back for one fixture image. */
async function hashImage(image: FixtureImage): Promise<HashedImage> {
  const { hashes, quality } = await hashDihedral(rgbOf(image), image.width, image.height);
  return { sha256: image.sha256, size: image.size, quality, hashes: hashes.map(toHex) };
}

describe("the photo fixture", () => {
  it("is present and is a photo collection on the operator's own recipe", () => {
    expect(fixture).toBeDefined();
    expect(fixture.images.length).toBeGreaterThanOrEqual(4);
    expect(fixture.document.kind).toBe(FINGERPRINT_KIND_PHOTOS);
    for (const image of fixture.images) {
      expect(rgbOf(image)).toHaveLength(image.width * image.height * 3);
    }
  });
});

describe("the browser recipe against the CLI's", () => {
  it("returns the CLI's eight hashes and quality for every image", async () => {
    for (const image of fixture.images) {
      const hashed = await hashImage(image);
      const stored = storedFrames(image.frame_index);
      if (stored.length === 0) {
        // A featureless image: the CLI dropped it, so the browser must drop it too.
        expect(hashed.quality).toBeLessThan(MIN_QUALITY);
        continue;
      }
      expect(stored).toHaveLength(ORIENTATIONS);
      expect(hashed.hashes).toEqual(stored.map((frame) => frame.pdq));
      expect(new Set(stored.map((frame) => frame.quality))).toEqual(new Set([hashed.quality]));
    }
  });

  it("gives the collection the CLI's identity, and builds the CLI's document", async () => {
    const images = await Promise.all(fixture.images.map(hashImage));

    const document = buildPhotoFingerprint({
      images,
      title: "Synthetic set",
      creators: ["Alias"],
      client: { tool: "test" },
    });

    // The identity is the whole point: same images, same digest, whatever the recipe.
    expect(document.sha256).toBe(fixture.document.sha256);
    expect(document.image_count).toBe(fixture.document.image_count);
    expect(document.file_size).toBe(fixture.images.reduce((total, i) => total + i.size, 0));
    // Its own tag, because the decoder is not Pillow's (ADR 0022's rule).
    expect(document.pipeline).toBe(BROWSER_PHOTO_PIPELINE);
    expect(document.kind).toBe(FINGERPRINT_KIND_PHOTOS);
    expect(photoFingerprintProblems(document)).toEqual([]);
    // Every hash the CLI stored is in the browser's document too, for the same image.
    const byHash = new Map(document.frames.map((frame) => [frame.pdq, frame]));
    for (const frame of fixture.document.frames) {
      expect(byHash.get(frame.pdq)?.quality).toBe(frame.quality);
    }
    expect(document.frames).toHaveLength(fixture.document.frames.length);
  });

  it("orders images by digest and leaves a gap where an image was dropped", async () => {
    const images = await Promise.all(fixture.images.map(hashImage));

    const document = buildPhotoFingerprint({
      images,
      title: "Synthetic set",
      creators: [],
      client: {},
    });

    const ordered = [...images].sort((left, right) => (left.sha256 < right.sha256 ? -1 : 1));
    for (const [index, image] of ordered.entries()) {
      const rows = document.frames.filter((frame) => frame.frame_index === index);
      // Below the floor the ordinal stays taken and carries nothing, so nothing after it shifts.
      expect(rows).toHaveLength(image.quality >= MIN_QUALITY ? ORIENTATIONS : 0);
    }
    expect(document.image_count).toBe(ordered.length);
  });

  it("sends no file name, no path and no time anywhere", async () => {
    const images = await Promise.all(fixture.images.map(hashImage));

    const document = buildPhotoFingerprint({
      images,
      title: "Beach set",
      creators: ["Alias"],
      client: { tool: "marceline-browser 0.2.0", agent: "Mozilla/5.0" },
    });

    // The exact body shape: no other key may ever appear in it.
    expect(Object.keys(document).sort()).toEqual([
      "client",
      "creator",
      "dihedral",
      "file_size",
      "format",
      "frames",
      "image_count",
      "kind",
      "pipeline",
      "sha256",
      "title",
      "version",
    ]);
    for (const frame of document.frames) {
      expect(Object.keys(frame).sort()).toEqual(["dihedral", "frame_index", "pdq", "quality"]);
    }
    const body = JSON.stringify(document);
    for (const name of ["01-gradient.png", "02-noise.png", ".png", "timestamp"]) {
      expect(body).not.toContain(name);
    }
  });
});

describe("the collection digest", () => {
  it("is order-independent, duplicate-sensitive and domain-separated", () => {
    const a = "a".repeat(64);
    const b = "b".repeat(64);

    expect(collectionSha256([a, b])).toBe(collectionSha256([b, a]));
    expect(collectionSha256([a, a])).not.toBe(collectionSha256([a]));
    // A one-image collection's id is not that image's own digest: the prefix separates them.
    expect(collectionSha256([a])).not.toBe(a);
  });

  it("refuses an empty set and anything that is not a digest", () => {
    expect(() => collectionSha256([])).toThrow(RangeError);
    expect(() => collectionSha256(["nope"])).toThrow(RangeError);
    expect(() => collectionSha256(["A".repeat(64)])).toThrow(RangeError);
  });

  it("digests bytes the way the server does", () => {
    expect(imageSha256(new Uint8Array())).toBe(
      "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    );
  });
});

describe("the default title", () => {
  it("is the common part of the file names, never a path", () => {
    expect(collectionTitle(["beach-01.jpg", "beach-02.jpg", "beach-03.jpg"])).toBe("beach");
    expect(collectionTitle(["/home/someone/sets/beach/01.jpg"])).toBe("01");
    expect(collectionTitle([])).toBe("photos");
  });
});

describe("the structural checks", () => {
  it("name what the server would refuse", () => {
    const frames = Array.from({ length: ORIENTATIONS }, (_, dihedral) => ({
      frame_index: 0,
      dihedral,
      quality: 90,
      pdq: "0".repeat(64),
    }));
    const document = {
      format: "marceline-fingerprint",
      version: 3,
      kind: FINGERPRINT_KIND_PHOTOS,
      pipeline: BROWSER_PHOTO_PIPELINE,
      sha256: "0".repeat(64),
      file_size: 10,
      image_count: 1,
      dihedral: true,
      title: "set",
      creator: [],
      client: {},
      frames,
    };

    expect(photoFingerprintProblems(document)).toEqual([]);
    expect(photoFingerprintProblems({ ...document, frames: [] })).toContain(
      "no image reached the quality threshold",
    );
    expect(
      photoFingerprintProblems({ ...document, frames: frames.slice(0, 7) }),
    ).toContain(`image 0 does not have all ${ORIENTATIONS} orientations`);
    const below = frames.map((frame) => ({ ...frame, quality: MIN_QUALITY - 1 }));
    expect(photoFingerprintProblems({ ...document, frames: below })).toContain(
      `image 0: quality ${MIN_QUALITY - 1} outside ${MIN_QUALITY}..100`,
    );
  });
});
