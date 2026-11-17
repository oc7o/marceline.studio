"""Choosing, inheriting and deliberately changing an original's owner on the command line.

`import_fingerprints` / `add_reference --organisation`, inheritance from an earlier reference of the
same file (the rule the cast already follows), and `assign_originals` (issue #76, ADR 0031).
"""

import io
import json
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from accounts.models import Organisation
from catalog.models import Creator
from marceline.app.ownership import owned_references, owner_of
from marceline.models import OriginalOwnership
from marceline.tests.app_helpers import make_organisation, make_reference, own
from matching.fingerprint import PIPELINE_V0, to_json
from matching.models import Reference
from matching.tests.test_fingerprint import synthetic, write
from matching.tests.test_frames import make_video

pytestmark = pytest.mark.django_db


def call(name: str, *args: str) -> str:
    output = io.StringIO()
    try:
        call_command(name, *args, stdout=output)
    except CommandError as exc:
        raise CommandError(f"{exc}\n{output.getvalue()}") from exc
    return output.getvalue()


@pytest.fixture
def creator() -> Creator:
    return Creator.objects.create(name="Synthetic Creator", aliases=["Alias"])


@pytest.fixture
def alpha() -> Organisation:
    return make_organisation("Alpha Studio")


@pytest.fixture
def bravo() -> Organisation:
    return make_organisation("Bravo Creator")


def write_v0(path: Path) -> Path:
    """The same synthetic file on the superseded recipe: a twin of `synthetic()` (issue #70)."""
    old = to_json(synthetic())
    old["pipeline"] = PIPELINE_V0
    path.write_text(json.dumps(old))
    return path


# import_fingerprints


def test_an_import_without_organisation_stays_unassigned(tmp_path: Path, creator: Creator) -> None:
    output = call("import_fingerprints", str(write(tmp_path / "one.json", synthetic())))

    reference = Reference.objects.get()
    assert owner_of(reference.pk) is None
    assert "owner unassigned" in output


def test_an_import_with_organisation_is_owned_by_it(
    tmp_path: Path, creator: Creator, alpha: Organisation
) -> None:
    path = write(tmp_path / "one.json", synthetic())

    dry = call("import_fingerprints", "--dry-run", "--organisation", "alpha studio", str(path))
    assert not Reference.objects.exists()
    assert f"owner {alpha.pk} (Alpha Studio)" in dry

    call("import_fingerprints", "--organisation", "Alpha Studio", str(path))
    reference = Reference.objects.get()
    assert owner_of(reference.pk) == alpha
    assert OriginalOwnership.objects.get().assigned_via == "import"


def test_an_unknown_organisation_stops_before_any_file(tmp_path: Path, creator: Creator) -> None:
    path = write(tmp_path / "one.json", synthetic())

    with pytest.raises(CommandError, match="no organisation named"):
        call("import_fingerprints", "--organisation", "Nobody", str(path))

    assert not Reference.objects.exists()


def test_a_re_fingerprint_inherits_the_owner_like_the_cast(
    tmp_path: Path, creator: Creator, alpha: Organisation
) -> None:
    call(
        "import_fingerprints", "--organisation", "Alpha Studio", str(write_v0(tmp_path / "v0.json"))
    )
    v0 = Reference.objects.get()

    output = call("import_fingerprints", str(write(tmp_path / "v1.json", synthetic())))

    v1 = Reference.objects.exclude(pk=v0.pk).get()
    assert owner_of(v1.pk) == alpha
    assert OriginalOwnership.objects.get(pk=v1.pk).assigned_via == "inherited"
    # Never silent: the line names the reference the owner came from.
    assert f"owner from reference {v0.pk}" in output
    assert list(owned_references(alpha).order_by("pk")) == [v0, v1]


def test_an_unassigned_earlier_reference_passes_nothing_on(
    tmp_path: Path, creator: Creator
) -> None:
    call("import_fingerprints", str(write_v0(tmp_path / "v0.json")))

    output = call("import_fingerprints", str(write(tmp_path / "v1.json", synthetic())))

    assert not OriginalOwnership.objects.exists()
    assert "owner from reference" not in output


def test_an_organisation_contradicting_the_earlier_owner_is_refused_per_file(
    tmp_path: Path, creator: Creator, alpha: Organisation, bravo: Organisation
) -> None:
    call(
        "import_fingerprints", "--organisation", "Alpha Studio", str(write_v0(tmp_path / "v0.json"))
    )
    other_file = write(tmp_path / "other.json", synthetic(sha256="cd" * 32))
    contradicting = write(tmp_path / "v1.json", synthetic())

    with pytest.raises(CommandError, match="1 fingerprint file"):
        call("import_fingerprints", "--organisation", "Bravo Creator", str(tmp_path))

    # The contradicting file imported nothing; the unrelated one went to Bravo as asked.
    assert Reference.objects.filter(sha256=synthetic().sha256).count() == 1
    assert owner_of(Reference.objects.get(sha256=other_file_sha(other_file)).pk) == bravo
    assert contradicting.exists()


def other_file_sha(path: Path) -> str:
    sha256: str = json.loads(path.read_text())["sha256"]
    return sha256


# add_reference


def test_add_reference_records_the_chosen_organisation(
    tmp_path: Path, creator: Creator, alpha: Organisation
) -> None:
    make_video(tmp_path, "testsrc2=duration=2:size=160x120:rate=10")
    clip = tmp_path / "clip.mp4"

    output = call(
        "add_reference",
        "--creator",
        creator.name,
        "--file",
        str(clip),
        "--no-dihedral",
        "--organisation",
        "Alpha Studio",
    )

    reference = Reference.objects.get()
    assert owner_of(reference.pk) == alpha
    assert f"owner {alpha.pk} (Alpha Studio)" in output


def test_add_reference_refuses_a_contradicting_organisation_before_hashing(
    tmp_path: Path, creator: Creator, alpha: Organisation, bravo: Organisation
) -> None:
    make_video(tmp_path, "testsrc2=duration=2:size=160x120:rate=10")
    clip = tmp_path / "clip.mp4"
    call("add_reference", "--creator", creator.name, "--file", str(clip), "--no-dihedral")
    earlier = Reference.objects.get()
    own(earlier, alpha)

    with pytest.raises(CommandError, match="move it with assign_originals"):
        call(
            "add_reference",
            *("--creator", creator.name, "--file", str(clip), "--fps", "2", "--no-dihedral"),
            *("--organisation", "Bravo Creator"),
        )

    assert Reference.objects.count() == 1


# assign_originals


def test_assign_originals_dry_run_names_every_move_and_writes_nothing(
    alpha: Organisation, bravo: Organisation
) -> None:
    imported = make_reference(None, title="Operator import")
    theirs = make_reference(None, title="Alpha's", organisation=alpha)

    output = call(
        "assign_originals",
        *("--organisation", "Bravo Creator", "--reference", str(imported.pk)),
        *("--reference", str(theirs.pk), "--dry-run"),
    )

    assert f"original {imported.pk} 'Operator import': would move from unassigned to" in output
    assert f"would move from {alpha.pk} (Alpha Studio) to {bravo.pk} (Bravo Creator)" in output
    assert "would change 2 of 2" in output
    assert owner_of(imported.pk) is None
    assert owner_of(theirs.pk) == alpha


def test_assign_originals_moves_twins_together_and_is_idempotent(
    alpha: Organisation, bravo: Organisation
) -> None:
    v0 = make_reference(None, title="Original", organisation=alpha)
    v1 = Reference.objects.create(
        creator=v0.creator, title=v0.title, sha256=v0.sha256, frame_fps=2, pipeline="v1"
    )
    own(v1, alpha)

    output = call("assign_originals", "--organisation", "Bravo Creator", "--reference", str(v0.pk))

    assert f"original {v1.pk} 'Original': moved from" in output
    assert "(same file as a named original)" in output
    assert list(owned_references(bravo).order_by("pk")) == [v0, v1]
    assert list(owned_references(alpha)) == []
    assert OriginalOwnership.objects.get(pk=v0.pk).assigned_via == "command"

    again = call("assign_originals", "--organisation", "Bravo Creator", "--reference", str(v0.pk))
    assert "changed 0 of 2" in again


def test_assign_originals_by_creator_and_unassign(alpha: Organisation) -> None:
    creator = Creator.objects.create(name="Rights Holder")
    co_star = Creator.objects.create(name="Co Star")
    primary = Reference.objects.create(creator=creator, title="A", sha256="1" * 64, frame_fps=1)
    primary.creators.add(creator)
    featured = make_reference(None, title="B")
    featured.creators.add(co_star)
    elsewhere = make_reference(None, title="C")

    call("assign_originals", "--organisation", "Alpha Studio", "--creator", "Rights Holder")
    call("assign_originals", "--organisation", "Alpha Studio", "--creator", "co star")

    assert list(owned_references(alpha).order_by("pk")) == [primary, featured]
    assert owner_of(elsewhere.pk) is None

    call("assign_originals", "--unassign", "--reference", str(primary.pk))
    assert owner_of(primary.pk) is None


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--organisation", "Alpha Studio"), "name the originals"),
        (("--organisation", "Nobody", "--reference", "1"), "no organisation named"),
        (("--organisation", "Alpha Studio", "--reference", "999999"), "no original with id"),
        (("--organisation", "Alpha Studio", "--creator", "Nobody"), "Unknown or ambiguous"),
    ],
)
def test_assign_originals_refuses_vague_requests(
    alpha: Organisation, args: tuple[str, ...], message: str
) -> None:
    with pytest.raises(CommandError, match=message):
        call("assign_originals", *args)
    assert not OriginalOwnership.objects.exists()


def test_assign_originals_needs_exactly_one_target() -> None:
    with pytest.raises(CommandError):
        call("assign_originals", "--reference", "1")
