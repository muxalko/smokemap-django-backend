from django.conf import settings
from django.contrib.gis.db import models as gis_models
from django.db import migrations, models
import django.db.models.deletion


STATES = [
    "draft",
    "pending",
    "withdrawn",
    "expired",
    "approved",
    "rejected",
]

STATE_CHOICES = [
    ("draft", "Draft"),
    ("pending", "Pending"),
    ("withdrawn", "Withdrawn"),
    ("expired", "Expired"),
    ("approved", "Approved"),
    ("rejected", "Rejected"),
]

OPERATION_CHOICES = [
    ("submission.create.v3", "Create submission"),
    ("submission.finalize.v3", "Finalize submission"),
    ("submission.expire.v3", "Expire submission"),
    ("submission.withdraw.v4", "Withdraw submission"),
    ("submission.approve.v4", "Approve submission"),
    ("submission.reject.v4", "Reject submission"),
]

OUTCOME_CHOICES = [
    ("succeeded", "Succeeded"),
]


def backfill_legacy_request_state(apps, schema_editor):
    request_model = apps.get_model("backend", "Request")
    ownerless_ids = list(
        request_model.objects.filter(owner_id__isnull=True)
        .order_by("pk")
        .values_list("pk", flat=True)[:20]
    )
    if ownerless_ids:
        raise RuntimeError(
            "Cannot migrate ownerless legacy submissions; refusing to invent an "
            f"audit identity (request IDs include {ownerless_ids!r})."
        )
    request_model.objects.filter(approved=True).update(state="approved")
    request_model.objects.filter(approved=False).update(state="pending")


def restore_legacy_approved(apps, schema_editor):
    request_model = apps.get_model("backend", "Request")
    request_model.objects.update(approved=False)
    request_model.objects.filter(state="approved").update(approved=True)


def assert_reverse_is_lossless(apps, schema_editor):
    lifecycle_model = apps.get_model("backend", "SubmissionLifecycleEvent")
    idempotency_model = apps.get_model("backend", "SubmissionIdempotency")
    request_model = apps.get_model("backend", "Request")
    address_model = apps.get_model("backend", "Address")
    place_model = apps.get_model("backend", "Place")

    if lifecycle_model.objects.exists() or idempotency_model.objects.exists():
        raise RuntimeError(
            "Cannot reverse the M3 submission migration while lifecycle or "
            "idempotency evidence exists."
        )
    unsupported_states = list(
        request_model.objects.exclude(state__in=("pending", "approved"))
        .order_by("pk")
        .values_list("pk", "state")[:20]
    )
    if unsupported_states:
        raise RuntimeError(
            "Cannot reverse M3 lifecycle states without data loss: "
            f"{unsupported_states!r}."
        )
    if address_model.objects.filter(addressString__isnull=True).exists():
        raise RuntimeError("Cannot reverse M3 while an address label is null.")
    duplicate_address = (
        address_model.objects.values("addressString")
        .annotate(row_count=models.Count("pk"))
        .filter(row_count__gt=1)
        .order_by("addressString")
        .values_list("addressString", flat=True)
        .first()
    )
    if duplicate_address is not None:
        raise RuntimeError(
            "Cannot reverse M3 while duplicate address labels exist."
        )
    duplicate_place = (
        place_model.objects.values("name")
        .annotate(row_count=models.Count("pk"))
        .filter(row_count__gt=1)
        .order_by("name")
        .values_list("name", flat=True)
        .first()
    )
    if duplicate_place is not None:
        raise RuntimeError("Cannot reverse M3 while duplicate place names exist.")


class Migration(migrations.Migration):
    dependencies = [
        ("backend", "0005_canonical_request_tags"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="address",
            name="addressString",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AlterField(
            model_name="address",
            name="location",
            field=gis_models.PointField(srid=4326),
        ),
        migrations.AlterField(
            model_name="place",
            name="name",
            field=models.CharField(max_length=255),
        ),
        migrations.AddField(
            model_name="request",
            name="state",
            field=models.CharField(
                choices=STATE_CHOICES,
                max_length=16,
                null=True,
            ),
        ),
        migrations.RunPython(
            backfill_legacy_request_state,
            restore_legacy_approved,
        ),
        migrations.AlterField(
            model_name="request",
            name="state",
            field=models.CharField(
                choices=STATE_CHOICES,
                default="draft",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="request",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="submissions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="request",
            name="description",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddConstraint(
            model_name="request",
            constraint=models.CheckConstraint(
                check=models.Q(state__in=STATES),
                name="request_valid_lifecycle_state",
            ),
        ),
        migrations.AddConstraint(
            model_name="request",
            constraint=models.CheckConstraint(
                check=(
                    models.Q(approved=True, state="approved")
                    | (~models.Q(state="approved") & models.Q(approved=False))
                ),
                name="request_state_legacy_approved_consistent",
            ),
        ),
        migrations.CreateModel(
            name="SubmissionIdempotency",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "operation",
                    models.CharField(
                        choices=OPERATION_CHOICES,
                        max_length=32,
                    ),
                ),
                ("key", models.CharField(max_length=255)),
                ("request_hash", models.CharField(editable=False, max_length=64)),
                ("original_result", models.JSONField(editable=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="submission_idempotency_records",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "submission",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="idempotency_records",
                        to="backend.request",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="submissionidempotency",
            constraint=models.UniqueConstraint(
                fields=("actor", "operation", "key"),
                name="unique_submission_idempotency_scope",
            ),
        ),
        migrations.AddConstraint(
            model_name="submissionidempotency",
            constraint=models.CheckConstraint(
                check=~models.Q(key=""),
                name="submission_idempotency_key_not_empty",
            ),
        ),
        migrations.CreateModel(
            name="SubmissionLifecycleEvent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "operation",
                    models.CharField(
                        choices=OPERATION_CHOICES,
                        max_length=32,
                    ),
                ),
                (
                    "from_state",
                    models.CharField(
                        blank=True,
                        choices=STATE_CHOICES,
                        max_length=16,
                        null=True,
                    ),
                ),
                (
                    "to_state",
                    models.CharField(
                        choices=STATE_CHOICES,
                        max_length=16,
                    ),
                ),
                (
                    "outcome",
                    models.CharField(
                        choices=OUTCOME_CHOICES,
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="submission_lifecycle_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "idempotency",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="lifecycle_event",
                        to="backend.submissionidempotency",
                    ),
                ),
                (
                    "submission",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="lifecycle_events",
                        to="backend.request",
                    ),
                ),
            ],
            options={"ordering": ["created_at", "pk"]},
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
        migrations.RunPython(
            migrations.RunPython.noop,
            assert_reverse_is_lossless,
        ),
    ]
