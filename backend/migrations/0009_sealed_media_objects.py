import uuid

from django.db import migrations, models
from django.db.models import F


def populate_sealed_keys(apps, schema_editor):
    intent_model = apps.get_model("backend", "MediaUploadIntent")
    image_model = apps.get_model("backend", "Image")
    if (
        intent_model.objects.filter(state__in=["verified", "attached"]).exists()
        or image_model.objects.filter(is_managed=True).exists()
    ):
        raise RuntimeError(
            "Cannot introduce sealed media keys while legacy verified or attached "
            "managed media exists; those bytes cannot be safely sealed in a DB migration."
        )
    for intent in intent_model.objects.filter(sealed_object_key__isnull=True).iterator():
        intent.sealed_object_key = (
            f"submission-media-sealed/{intent.submission_id}/{uuid.uuid4().hex}"
        )
        intent.save(update_fields=["sealed_object_key"])


def refuse_lossy_sealed_reverse(apps, schema_editor):
    intent_model = apps.get_model("backend", "MediaUploadIntent")
    image_model = apps.get_model("backend", "Image")
    if intent_model.objects.exists() or image_model.objects.filter(is_managed=True).exists():
        raise RuntimeError(
            "Cannot reverse sealed media migration while managed media evidence exists."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("backend", "0008_managed_image_area_limit"),
    ]

    operations = [
        migrations.AddField(
            model_name="mediauploadintent",
            name="sealed_object_key",
            field=models.CharField(
                editable=False, max_length=255, null=True, unique=True
            ),
        ),
        migrations.AddField(
            model_name="mediauploadintent",
            name="upload_cleanup_pending",
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.AddField(
            model_name="mediauploadintent",
            name="upload_cleanup_attempts",
            field=models.PositiveIntegerField(default=0, editable=False),
        ),
        migrations.AddField(
            model_name="mediauploadintent",
            name="upload_cleanup_last_attempt_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="mediauploadintent",
            name="upload_cleanup_next_attempt_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="mediauploadintent",
            name="upload_cleanup_error_code",
            field=models.CharField(
                blank=True, default="", editable=False, max_length=64
            ),
        ),
        migrations.RunPython(populate_sealed_keys, refuse_lossy_sealed_reverse),
        migrations.AlterField(
            model_name="mediauploadintent",
            name="sealed_object_key",
            field=models.CharField(editable=False, max_length=255, unique=True),
        ),
        migrations.AddConstraint(
            model_name="mediauploadintent",
            constraint=models.CheckConstraint(
                check=models.Q(
                    sealed_object_key__regex=(
                        "^submission-media-sealed/[0-9]+/[0-9a-f]{32}$"
                    )
                ),
                name="media_intent_sealed_key_namespace",
            ),
        ),
        migrations.AddConstraint(
            model_name="mediauploadintent",
            constraint=models.CheckConstraint(
                check=~models.Q(sealed_object_key=F("object_key")),
                name="media_intent_upload_sealed_keys_distinct",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="image",
            name="managed_image_complete_metadata",
        ),
        migrations.AddConstraint(
            model_name="image",
            constraint=models.CheckConstraint(
                check=(
                    models.Q(is_managed=False)
                    | models.Q(
                        is_managed=True,
                        state="attached",
                        request__isnull=False,
                        place__isnull=True,
                        intent__isnull=False,
                        owner__isnull=False,
                        position__gte=0,
                        position__lt=3,
                        byte_size__gt=0,
                        byte_size__lte=5_000_000,
                        detected_mime__in=["image/jpeg", "image/png", "image/webp"],
                        storage_identifier__regex="^.+$",
                        storage_bucket__regex="^.+$",
                        storage_key__regex=(
                            "^submission-media-sealed/[0-9]+/[0-9a-f]{32}$"
                        ),
                        sha256__regex="^[0-9a-f]{64}$",
                        width__gt=0,
                        width__lte=10_000,
                        height__gt=0,
                        height__lte=10_000,
                    )
                ),
                name="managed_image_complete_metadata",
            ),
        ),
    ]
