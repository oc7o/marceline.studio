"""The third signal's candidate source and its wall (ADR 0040).

`db_default` stays `pdq`, so a worker still running the previous code keeps inserting ordinary
matches after this is applied. The constraint is added as its own, next to ADR 0037's, rather than
widening that one: each signal's invariant should fail with its own name.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("matching", "0020_frame_embeddings"),
    ]

    operations = [
        migrations.AlterField(
            model_name="referencematch",
            name="candidate_source",
            field=models.CharField(
                choices=[("pdq", "Pdq"), ("embedding", "Embedding"), ("metadata", "Metadata")],
                db_default="pdq",
                db_index=True,
                default="pdq",
                help_text=(
                    "pdq = frame evidence; embedding or metadata = a candidate awaiting proof."
                ),
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="referencematch",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("candidate_source", "metadata"), _negated=True),
                    models.Q(
                        ("band", "review"),
                        ("evidence_frames", 0),
                        ("loose_frames", 0),
                        ("matched_frames", 0),
                    ),
                    _connector="OR",
                ),
                name="reference_match_metadata_is_never_evidence",
            ),
        ),
    ]
