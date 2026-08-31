import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


MEDIA_OPERATION_CHOICES = [
    ("submission.create.v3", "Create submission"),
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


def noop(apps, schema_editor):
    pass


def refuse_lossy_media_reverse(apps, schema_editor):
    intent_model = apps.get_model("backend", "MediaUploadIntent")
    image_model = apps.get_model("backend", "Image")
    idempotency_model = apps.get_model("backend", "SubmissionIdempotency")
    if (
        intent_model.objects.exists()
        or image_model.objects.filter(is_managed=True).exists()
        or idempotency_model.objects.filter(media_intent__isnull=False).exists()
    ):
        raise RuntimeError(
            "Cannot reverse owner-bound media migration while managed media evidence exists."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("backend", "0006_m3_submission_creation"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="MediaUploadIntent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("state", models.CharField(choices=[("created", "Created"), ("issued", "Issued"), ("verified", "Verified"), ("attached", "Attached"), ("failed", "Failed"), ("expired", "Expired"), ("cleanup_pending", "Cleanup pending"), ("deleted", "Deleted")], default="created", max_length=24)),
                ("slot", models.PositiveSmallIntegerField()),
                ("storage_identifier", models.CharField(editable=False, max_length=64)),
                ("storage_bucket", models.CharField(editable=False, max_length=255)),
                ("object_key", models.CharField(editable=False, max_length=255, unique=True)),
                ("expected_mime", models.CharField(editable=False, max_length=32)),
                ("declared_byte_size", models.PositiveIntegerField(editable=False)),
                ("declared_sha256", models.CharField(editable=False, max_length=64)),
                ("original_filename", models.CharField(blank=True, default="", max_length=255)),
                ("server_byte_size", models.PositiveIntegerField(blank=True, editable=False, null=True)),
                ("server_sha256", models.CharField(blank=True, default="", editable=False, max_length=64)),
                ("detected_mime", models.CharField(blank=True, default="", editable=False, max_length=32)),
                ("width", models.PositiveIntegerField(blank=True, editable=False, null=True)),
                ("height", models.PositiveIntegerField(blank=True, editable=False, null=True)),
                ("failure_code", models.CharField(blank=True, default="", editable=False, max_length=64)),
                ("failure_at", models.DateTimeField(blank=True, editable=False, null=True)),
                ("verification_attempts", models.PositiveIntegerField(default=0, editable=False)),
                ("last_verification_at", models.DateTimeField(blank=True, editable=False, null=True)),
                ("cleanup_claim_token", models.UUIDField(blank=True, editable=False, null=True)),
                ("cleanup_claimed_at", models.DateTimeField(blank=True, editable=False, null=True)),
                ("cleanup_lease_until", models.DateTimeField(blank=True, editable=False, null=True)),
                ("cleanup_attempts", models.PositiveIntegerField(default=0, editable=False)),
                ("cleanup_last_attempt_at", models.DateTimeField(blank=True, editable=False, null=True)),
                ("cleanup_next_attempt_at", models.DateTimeField(blank=True, editable=False, null=True)),
                ("cleanup_error_code", models.CharField(blank=True, default="", editable=False, max_length=64)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("absolute_expires_at", models.DateTimeField(editable=False)),
                ("issued_at", models.DateTimeField(blank=True, editable=False, null=True)),
                ("presign_expires_at", models.DateTimeField(blank=True, editable=False, null=True)),
                ("verified_at", models.DateTimeField(blank=True, editable=False, null=True)),
                ("attached_at", models.DateTimeField(blank=True, editable=False, null=True)),
                ("deleted_at", models.DateTimeField(blank=True, editable=False, null=True)),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="media_upload_intents", to=settings.AUTH_USER_MODEL)),
                ("submission", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="media_upload_intents", to="backend.request")),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
        migrations.AddConstraint(
            model_name="mediauploadintent",
            constraint=models.CheckConstraint(check=models.Q(("state__in", ["created", "issued", "verified", "attached", "failed", "expired", "cleanup_pending", "deleted"])), name="media_intent_valid_state"),
        ),
        migrations.AddConstraint(
            model_name="mediauploadintent",
            constraint=models.CheckConstraint(check=models.Q(("slot__gte", 0), ("slot__lt", 3)), name="media_intent_slot_range"),
        ),
        migrations.AddConstraint(
            model_name="mediauploadintent",
            constraint=models.CheckConstraint(check=models.Q(("declared_byte_size__gt", 0), ("declared_byte_size__lte", 5000000)), name="media_intent_declared_size_range"),
        ),
        migrations.AddConstraint(
            model_name="mediauploadintent",
            constraint=models.CheckConstraint(check=models.Q(("expected_mime__in", ["image/jpeg", "image/png", "image/webp"])), name="media_intent_expected_mime_allowed"),
        ),
        migrations.AddConstraint(
            model_name="mediauploadintent",
            constraint=models.CheckConstraint(check=models.Q(("declared_sha256__regex", "^[0-9a-f]{64}$")), name="media_intent_declared_sha256_format"),
        ),
        migrations.AddConstraint(
            model_name="mediauploadintent",
            constraint=models.CheckConstraint(check=models.Q(("object_key__regex", "^submission-media/[0-9]+/[0-9a-f]{32}$")), name="media_intent_managed_key_namespace"),
        ),
        migrations.AddConstraint(
            model_name="mediauploadintent",
            constraint=models.CheckConstraint(check=(models.Q(("cleanup_claim_token__isnull", True), ("cleanup_claimed_at__isnull", True), ("cleanup_lease_until__isnull", True)) | models.Q(("cleanup_claim_token__isnull", False), ("cleanup_claimed_at__isnull", False), ("cleanup_lease_until__isnull", False), ("state", "cleanup_pending"))), name="media_intent_cleanup_lease_complete"),
        ),
        migrations.AddConstraint(
            model_name="mediauploadintent",
            constraint=models.CheckConstraint(check=(~models.Q(("state__in", ["issued", "verified", "attached"])) | models.Q(("issued_at__isnull", False), ("presign_expires_at__isnull", False))), name="media_intent_issued_metadata_complete"),
        ),
        migrations.AddConstraint(
            model_name="mediauploadintent",
            constraint=models.CheckConstraint(check=(~models.Q(("state__in", ["verified", "attached"])) | models.Q(("detected_mime__in", ["image/jpeg", "image/png", "image/webp"]), ("height__gt", 0), ("height__lte", 10000), ("server_byte_size__gt", 0), ("server_byte_size__lte", 5000000), ("server_sha256__regex", "^[0-9a-f]{64}$"), ("verified_at__isnull", False), ("width__gt", 0), ("width__lte", 10000))), name="media_intent_verified_metadata_complete"),
        ),
        migrations.AddConstraint(
            model_name="mediauploadintent",
            constraint=models.CheckConstraint(check=(~models.Q(("state", "attached")) | models.Q(("attached_at__isnull", False))), name="media_intent_attached_timestamp_complete"),
        ),
        migrations.AddConstraint(
            model_name="mediauploadintent",
            constraint=models.CheckConstraint(check=(~models.Q(("state", "deleted")) | models.Q(("deleted_at__isnull", False))), name="media_intent_deleted_timestamp_complete"),
        ),
        migrations.AddConstraint(
            model_name="mediauploadintent",
            constraint=models.UniqueConstraint(condition=models.Q(("state__in", ["created", "issued", "verified"])), fields=("submission", "slot"), name="unique_reserving_media_intent_slot"),
        ),
        migrations.AlterField(
            model_name="submissionidempotency",
            name="operation",
            field=models.CharField(choices=MEDIA_OPERATION_CHOICES, max_length=32),
        ),
        migrations.AlterField(
            model_name="submissionlifecycleevent",
            name="operation",
            field=models.CharField(choices=MEDIA_OPERATION_CHOICES, max_length=32),
        ),
        migrations.AddField(
            model_name="submissionidempotency",
            name="media_intent",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="idempotency_records", to="backend.mediauploadintent"),
        ),
        migrations.AddConstraint(
            model_name="submissionidempotency",
            constraint=models.CheckConstraint(
                check=(models.Q(("media_intent__isnull", False), ("operation__in", ["media.intent.create.v3", "media.intent.issue.v3", "media.intent.renew.v3", "media.intent.verify.v3", "media.intent.attach.v3", "media.intent.expire.v3", "media.intent.cleanup.v3"])) | models.Q(("media_intent__isnull", True), ("operation__in", ["submission.create.v3", "submission.finalize.v3", "submission.expire.v3", "submission.withdraw.v4", "submission.approve.v4", "submission.reject.v4"]))),
                name="submission_idempotency_target_matches_operation",
            ),
        ),
        migrations.AddField(model_name="image", name="is_managed", field=models.BooleanField(default=False, editable=False)),
        migrations.AddField(model_name="image", name="intent", field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="attachment", to="backend.mediauploadintent")),
        migrations.AddField(model_name="image", name="owner", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="managed_images", to=settings.AUTH_USER_MODEL)),
        migrations.AddField(model_name="image", name="position", field=models.PositiveSmallIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="image", name="state", field=models.CharField(blank=True, default="", max_length=16)),
        migrations.AddField(model_name="image", name="storage_identifier", field=models.CharField(blank=True, default="", editable=False, max_length=64)),
        migrations.AddField(model_name="image", name="storage_bucket", field=models.CharField(blank=True, default="", editable=False, max_length=255)),
        migrations.AddField(model_name="image", name="storage_key", field=models.CharField(blank=True, default="", editable=False, max_length=255)),
        migrations.AddField(model_name="image", name="byte_size", field=models.PositiveIntegerField(blank=True, editable=False, null=True)),
        migrations.AddField(model_name="image", name="detected_mime", field=models.CharField(blank=True, default="", editable=False, max_length=32)),
        migrations.AddField(model_name="image", name="width", field=models.PositiveIntegerField(blank=True, editable=False, null=True)),
        migrations.AddField(model_name="image", name="height", field=models.PositiveIntegerField(blank=True, editable=False, null=True)),
        migrations.AddField(model_name="image", name="sha256", field=models.CharField(blank=True, default="", editable=False, max_length=64)),
        migrations.AddField(model_name="image", name="attached_at", field=models.DateTimeField(blank=True, editable=False, null=True)),
        migrations.AddConstraint(
            model_name="image",
            constraint=models.UniqueConstraint(condition=models.Q(("is_managed", True), ("state", "attached")), fields=("request", "position"), name="unique_managed_image_position"),
        ),
        migrations.AddConstraint(
            model_name="image",
            constraint=models.UniqueConstraint(condition=models.Q(("is_managed", True), ("state", "attached")), fields=("request", "sha256"), name="unique_managed_image_digest"),
        ),
        migrations.AddConstraint(
            model_name="image",
            constraint=models.CheckConstraint(
                check=(models.Q(("is_managed", False)) | models.Q(("byte_size__gt", 0), ("byte_size__lte", 5000000), ("detected_mime__in", ["image/jpeg", "image/png", "image/webp"]), ("height__gt", 0), ("height__lte", 10000), ("intent__isnull", False), ("is_managed", True), ("owner__isnull", False), ("place__isnull", True), ("position__gte", 0), ("position__lt", 3), ("request__isnull", False), ("sha256__regex", "^[0-9a-f]{64}$"), ("state", "attached"), ("storage_bucket__regex", "^.+$"), ("storage_identifier__regex", "^.+$"), ("storage_key__regex", "^submission-media/[0-9]+/[0-9a-f]{32}$"), ("width__gt", 0), ("width__lte", 10000))),
                name="managed_image_complete_metadata",
            ),
        ),
        migrations.RunPython(noop, refuse_lossy_media_reverse),
    ]
