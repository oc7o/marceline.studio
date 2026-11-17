"""The upload relay's refused attestations, and the guard that makes the table append-only.

ADR 0044 §9, amendment 2026-09-20. The trigger is written by hand because Django cannot express
one — the technique of `marceline/0019_deletion_guards`, same SQLSTATE, so Django raises
`IntegrityError`. A counter of refusals that the process holding the refused credential could
delete or wind back would answer nothing, so: no DELETE at all, and the only UPDATE that passes is
this row's own counter going up inside its own hour and reason.
"""

import django.utils.timezone
from django.db import migrations, models

TABLE = "marceline_relayrefusal"

FORWARD = f"""
CREATE FUNCTION marceline_relay_refusal_append_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'relay refusals are append-only' USING ERRCODE = '23000';
    END IF;
    IF NEW.id <> OLD.id
        OR NEW.hour <> OLD.hour
        OR NEW.reason <> OLD.reason
        OR NEW.first_at <> OLD.first_at
        OR NEW.attempts <= OLD.attempts
        OR NEW.last_at < OLD.last_at
    THEN
        RAISE EXCEPTION 'a relay refusal may only count up' USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER {TABLE}_append_only
    BEFORE UPDATE OR DELETE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION marceline_relay_refusal_append_only();
"""

BACKWARD = (
    f"DROP TRIGGER IF EXISTS {TABLE}_append_only ON {TABLE};\n"
    "DROP FUNCTION IF EXISTS marceline_relay_refusal_append_only();\n"
)


class Migration(migrations.Migration):

    dependencies = [
        ('marceline', '0029_data_requests_and_notice_review_date'),
    ]

    operations = [
        migrations.CreateModel(
            name='RelayRefusal',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('hour', models.DateTimeField(help_text='The hour these attempts fell in, truncated (UTC).')),
                ('reason', models.CharField(choices=[('malformed', 'the attestation did not parse'), ('no_membership', 'the attested pair is not a customer of ours'), ('inactive', 'the attested account is not active'), ('unavailable', 'uploads are not available here'), ('spread', 'too many organisations in the window')], max_length=16)),
                ('attempts', models.PositiveIntegerField(default=1)),
                ('first_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('last_at', models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                'verbose_name': 'relay refusal',
                'indexes': [models.Index(fields=['-hour'], name='marceline_r_hour_eb0a73_idx')],
                'constraints': [models.UniqueConstraint(fields=('hour', 'reason'), name='relay_refusal_hour_reason')],
            },
        ),
        migrations.RunSQL(FORWARD, BACKWARD),
    ]
