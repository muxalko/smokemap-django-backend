from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def clear_legacy_actor_strings(apps, schema_editor):
    request_model = apps.get_model("backend", "Request")
    request_model.objects.update(requested_by=None, approved_by=None)


class Migration(migrations.Migration):
    dependencies = [
        ("backend", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="request",
            name="owner",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="submissions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="request",
            name="reviewed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="reviewed_submissions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.CreateModel(
            name="ModerationAudit",
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
                    "action",
                    models.CharField(
                        choices=[
                            ("approve", "Approve"),
                            ("hard_delete", "Hard delete"),
                        ],
                        max_length=32,
                    ),
                ),
                ("target_type", models.CharField(max_length=32)),
                ("target_id", models.PositiveBigIntegerField()),
                ("outcome", models.CharField(default="succeeded", max_length=32)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="moderation_audits",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.RunPython(
            clear_legacy_actor_strings,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
