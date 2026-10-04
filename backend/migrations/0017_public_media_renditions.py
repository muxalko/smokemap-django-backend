import uuid

from django.db import migrations, models
from django.db.models import F, Value
from django.db.models.lookups import LessThanOrEqual
import django.db.models.deletion
import django.utils.timezone


OPERATIONS = [
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
    ("media.intent.remove.v3", "Remove attached media"),
    ("media.intent.expire.v3", "Expire media intent"),
    ("media.intent.cleanup.v3", "Clean up media object"),
    ("submission.reorder_media.v3", "Reorder submission media"),
    ("media.revoke.v4", "Revoke public media"),
]


def assign_private_rendition_keys(apps, schema_editor):
    intent_model = apps.get_model("backend", "MediaUploadIntent")
    for intent in intent_model.objects.filter(rendition_object_key__isnull=True).iterator():
        intent.rendition_object_key = (
            f"submission-media-renditions/{intent.submission_id}/{uuid.uuid4().hex}"
        )
        intent.save(update_fields=["rendition_object_key"])


class Migration(migrations.Migration):
    dependencies = [("backend", "0016_place_search_indexes")]

    operations = [
        migrations.AddField(
            model_name="mediauploadintent",
            name="rendition_byte_size",
            field=models.PositiveIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="mediauploadintent",
            name="rendition_mime",
            field=models.CharField(blank=True, default="", editable=False, max_length=32),
        ),
        migrations.AddField(
            model_name="mediauploadintent",
            name="rendition_object_key",
            field=models.CharField(
                editable=False, max_length=255, null=True, unique=True
            ),
        ),
        migrations.AddField(
            model_name="mediauploadintent",
            name="rendition_sha256",
            field=models.CharField(blank=True, default="", editable=False, max_length=64),
        ),
        migrations.RunPython(assign_private_rendition_keys, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="mediauploadintent",
            name="rendition_object_key",
            field=models.CharField(editable=False, max_length=255, unique=True),
        ),
        migrations.AddConstraint(
            model_name="mediauploadintent",
            constraint=models.CheckConstraint(
                check=models.Q(
                    rendition_object_key__regex=(
                        r"^submission-media-renditions/[0-9]+/[0-9a-f]{32}$"
                    )
                ),
                name="media_intent_rendition_key_namespace",
            ),
        ),
        migrations.AddConstraint(
            model_name="mediauploadintent",
            constraint=models.CheckConstraint(
                check=(
                    models.Q(
                        rendition_byte_size__isnull=True,
                        rendition_sha256="",
                        rendition_mime="",
                    )
                    | models.Q(
                        rendition_byte_size__gt=0,
                        rendition_byte_size__lte=5_000_000,
                        rendition_sha256__regex=r"^[0-9a-f]{64}$",
                        rendition_mime__in=["image/jpeg", "image/png", "image/webp"],
                    )
                ),
                name="media_intent_rendition_metadata_complete",
            ),
        ),
        migrations.CreateModel(
            name="PublicMediaRendition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("public_id", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("state", models.CharField(choices=[("published", "Published"), ("revoked", "Revoked")], default="published", max_length=16)),
                ("position", models.PositiveSmallIntegerField()),
                ("mime_type", models.CharField(editable=False, max_length=32)),
                ("byte_size", models.PositiveIntegerField(editable=False)),
                ("width", models.PositiveIntegerField(editable=False)),
                ("height", models.PositiveIntegerField(editable=False)),
                ("published_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("revoked_at", models.DateTimeField(blank=True, editable=False, null=True)),
                ("intent", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="public_rendition", to="backend.mediauploadintent")),
                ("place", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="public_media", to="backend.place")),
                ("source_image", models.OneToOneField(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="public_rendition", to="backend.image")),
            ],
            options={"ordering": ["position", "pk"]},
        ),
        migrations.AddConstraint(
            model_name="publicmediarendition",
            constraint=models.UniqueConstraint(condition=models.Q(state="published"), fields=("place", "position"), name="unique_published_media_position"),
        ),
        migrations.AddConstraint(
            model_name="publicmediarendition",
            constraint=models.CheckConstraint(check=models.Q(position__gte=0, position__lt=3), name="public_media_position_range"),
        ),
        migrations.AddConstraint(
            model_name="publicmediarendition",
            constraint=models.CheckConstraint(check=models.Q(mime_type__in=["image/jpeg", "image/png", "image/webp"]), name="public_media_mime_allowed"),
        ),
        migrations.AddConstraint(
            model_name="publicmediarendition",
            constraint=models.CheckConstraint(check=models.Q(byte_size__gt=0, byte_size__lte=5_000_000), name="public_media_size_range"),
        ),
        migrations.AddConstraint(
            model_name="publicmediarendition",
            constraint=models.CheckConstraint(
                check=(
                    models.Q(width__gt=0, width__lte=10_000, height__gt=0, height__lte=10_000)
                    & LessThanOrEqual(F("width") * F("height"), Value(25_000_000))
                ),
                name="public_media_dimensions_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="publicmediarendition",
            constraint=models.CheckConstraint(
                check=(
                    models.Q(state="published", place__isnull=False, source_image__isnull=False, revoked_at__isnull=True)
                    | models.Q(state="revoked", revoked_at__isnull=False)
                ),
                name="public_media_state_evidence",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="submissionidempotency",
            name="submission_idempotency_target_matches_operation",
        ),
        migrations.AlterField(
            model_name="submissionidempotency",
            name="operation",
            field=models.CharField(choices=OPERATIONS, max_length=32),
        ),
        migrations.AlterField(
            model_name="submissionlifecycleevent",
            name="operation",
            field=models.CharField(choices=OPERATIONS, max_length=32),
        ),
        migrations.AddConstraint(
            model_name="submissionidempotency",
            constraint=models.CheckConstraint(
                check=(
                    models.Q(
                        media_intent__isnull=False,
                        operation__in=[
                            "media.intent.create.v3",
                            "media.intent.issue.v3",
                            "media.intent.renew.v3",
                            "media.intent.verify.v3",
                            "media.intent.attach.v3",
                            "media.intent.remove.v3",
                            "media.intent.expire.v3",
                            "media.intent.cleanup.v3",
                            "media.revoke.v4",
                        ],
                    )
                    | models.Q(
                        media_intent__isnull=True,
                        operation__in=[
                            "submission.create.v3",
                            "submission.edit.v3",
                            "submission.finalize.v3",
                            "submission.expire.v3",
                            "submission.withdraw.v4",
                            "submission.approve.v4",
                            "submission.reject.v4",
                            "submission.reorder_media.v3",
                        ],
                    )
                ),
                name="submission_idempotency_target_matches_operation",
            ),
        ),
        migrations.AlterField(
            model_name="moderationaudit",
            name="action",
            field=models.CharField(choices=[("approve", "Approve"), ("hard_delete", "Hard delete"), ("revoke_media", "Revoke public media")], max_length=32),
        ),
    ]
