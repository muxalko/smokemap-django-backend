from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


EXPIRE_OPERATION = "submission.expire.v3"
SYSTEM_ACTOR = "draft-expiry.v3"


def refuse_legacy_actor_expiry(apps, schema_editor):
    lifecycle_model = apps.get_model("backend", "SubmissionLifecycleEvent")
    idempotency_model = apps.get_model("backend", "SubmissionIdempotency")
    if (
        lifecycle_model.objects.filter(operation=EXPIRE_OPERATION).exists()
        or idempotency_model.objects.filter(operation=EXPIRE_OPERATION).exists()
    ):
        raise RuntimeError(
            "Cannot enable system draft expiry while legacy actor-attributed "
            "expiry evidence exists; refusing to invent or discard audit evidence."
        )


def refuse_lossy_system_expiry_reverse(apps, schema_editor):
    lifecycle_model = apps.get_model("backend", "SubmissionLifecycleEvent")
    if lifecycle_model.objects.filter(
        operation=EXPIRE_OPERATION,
        system_actor=SYSTEM_ACTOR,
    ).exists():
        raise RuntimeError(
            "Cannot reverse system draft expiry while system expiry evidence exists."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("backend", "0010_submission_draft_edit"),
    ]

    operations = [
        migrations.RunPython(
            refuse_legacy_actor_expiry,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="submissionlifecycleevent",
            name="actor",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="submission_lifecycle_events",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="submissionlifecycleevent",
            name="system_actor",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AlterField(
            model_name="submissionlifecycleevent",
            name="idempotency",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="lifecycle_event",
                to="backend.submissionidempotency",
            ),
        ),
        migrations.AddIndex(
            model_name="request",
            index=models.Index(
                fields=["state", "date_updated", "id"],
                name="request_expiry_scan_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="submissionidempotency",
            constraint=models.CheckConstraint(
                check=~models.Q(operation=EXPIRE_OPERATION),
                name="submission_idempotency_not_system_expiry",
            ),
        ),
        migrations.AddConstraint(
            model_name="submissionlifecycleevent",
            constraint=models.CheckConstraint(
                check=(
                    models.Q(
                        operation=EXPIRE_OPERATION,
                        actor__isnull=True,
                        system_actor=SYSTEM_ACTOR,
                        idempotency__isnull=True,
                        outcome="succeeded",
                    )
                    | (
                        ~models.Q(operation=EXPIRE_OPERATION)
                        & models.Q(
                            actor__isnull=False,
                            system_actor__isnull=True,
                            idempotency__isnull=False,
                        )
                    )
                ),
                name="submission_event_actor_evidence",
            ),
        ),
        migrations.AddConstraint(
            model_name="submissionlifecycleevent",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    operation=EXPIRE_OPERATION,
                    outcome="succeeded",
                ),
                fields=("submission",),
                name="unique_successful_submission_expiry",
            ),
        ),
        # Reverse executes this first, before nullable system evidence is lost.
        migrations.RunPython(
            migrations.RunPython.noop,
            refuse_lossy_system_expiry_reverse,
        ),
    ]
