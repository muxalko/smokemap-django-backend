from django.db import migrations, models


OPERATION_CHOICES = [
    ("submission.create.v3", "Create submission"),
    ("submission.edit.v3", "Edit submission"),
    ("submission.finalize.v3", "Finalize submission"),
    ("submission.expire.v3", "Expire submission"),
    ("submission.withdraw.v4", "Withdraw submission"),
    ("submission.approve.v4", "Approve submission"),
    ("submission.reject.v4", "Reject submission"),
    ("media.intent.create.v3", "Create media intent"),
    ("media.intent.issue.v3", "Issue media upload"),
    ("media.intent.renew.v3", "Renew media upload"),
    ("media.intent.verify.v3", "Verify media upload"),
    ("media.intent.attach.v3", "Attach verified media"),
    ("media.intent.expire.v3", "Expire media intent"),
    ("media.intent.cleanup.v3", "Clean up media object"),
]

MEDIA_OPERATIONS = [
    "media.intent.create.v3",
    "media.intent.issue.v3",
    "media.intent.renew.v3",
    "media.intent.verify.v3",
    "media.intent.attach.v3",
    "media.intent.expire.v3",
    "media.intent.cleanup.v3",
]

SUBMISSION_OPERATIONS = [
    "submission.create.v3",
    "submission.edit.v3",
    "submission.finalize.v3",
    "submission.expire.v3",
    "submission.withdraw.v4",
    "submission.approve.v4",
    "submission.reject.v4",
]


def noop(apps, schema_editor):
    pass


def refuse_lossy_edit_reverse(apps, schema_editor):
    """Reversal narrows both constraints, so refuse to orphan edit evidence."""
    idempotency_model = apps.get_model("backend", "SubmissionIdempotency")
    lifecycle_model = apps.get_model("backend", "SubmissionLifecycleEvent")
    if (
        idempotency_model.objects.filter(operation="submission.edit.v3").exists()
        or lifecycle_model.objects.filter(operation="submission.edit.v3").exists()
    ):
        raise RuntimeError(
            "Cannot reverse the draft edit migration while draft edit evidence exists."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("backend", "0009_sealed_media_objects"),
    ]

    operations = [
        migrations.AlterField(
            model_name="submissionidempotency",
            name="operation",
            field=models.CharField(choices=OPERATION_CHOICES, max_length=32),
        ),
        migrations.AlterField(
            model_name="submissionlifecycleevent",
            name="operation",
            field=models.CharField(choices=OPERATION_CHOICES, max_length=32),
        ),
        migrations.RemoveConstraint(
            model_name="submissionidempotency",
            name="submission_idempotency_target_matches_operation",
        ),
        migrations.AddConstraint(
            model_name="submissionidempotency",
            constraint=models.CheckConstraint(
                check=(
                    models.Q(
                        operation__in=MEDIA_OPERATIONS,
                        media_intent__isnull=False,
                    )
                    | models.Q(
                        operation__in=SUBMISSION_OPERATIONS,
                        media_intent__isnull=True,
                    )
                ),
                name="submission_idempotency_target_matches_operation",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="submissionlifecycleevent",
            name="submission_event_valid_transition",
        ),
        migrations.AddConstraint(
            model_name="submissionlifecycleevent",
            constraint=models.CheckConstraint(
                check=(
                    models.Q(
                        operation="submission.create.v3",
                        from_state__isnull=True,
                        to_state="draft",
                    )
                    | models.Q(
                        operation="submission.edit.v3",
                        from_state="draft",
                        to_state="draft",
                    )
                    | models.Q(
                        operation="submission.finalize.v3",
                        from_state="draft",
                        to_state="pending",
                    )
                    | models.Q(
                        operation="submission.expire.v3",
                        from_state="draft",
                        to_state="expired",
                    )
                    | models.Q(
                        operation="submission.withdraw.v4",
                        from_state__in=["draft", "pending"],
                        to_state="withdrawn",
                    )
                    | models.Q(
                        operation="submission.approve.v4",
                        from_state="pending",
                        to_state="approved",
                    )
                    | models.Q(
                        operation="submission.reject.v4",
                        from_state="pending",
                        to_state="rejected",
                    )
                ),
                name="submission_event_valid_transition",
            ),
        ),
        # Placed last so that a reversal reaches this guard before either
        # constraint is narrowed back to the pre-edit operation set.
        migrations.RunPython(noop, refuse_lossy_edit_reverse),
    ]
