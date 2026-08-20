from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def delete_legacy_refresh_tokens(apps, schema_editor):
    legacy_refresh_token = apps.get_model("refresh_token", "RefreshToken")
    legacy_refresh_token.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("backend", "0002_submission_ownership_and_moderation_audit"),
        ("refresh_token", "0002_auto_20190130_0900"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="RefreshTokenFamily",
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
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("compromised_at", models.DateTimeField(blank=True, null=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="refresh_token_families",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="RefreshTokenCredential",
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
                    "token_digest",
                    models.CharField(editable=False, max_length=64, unique=True),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                (
                    "family",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="credentials",
                        to="backend.refreshtokenfamily",
                    ),
                ),
                (
                    "successor",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="predecessor",
                        to="backend.refreshtokencredential",
                    ),
                ),
            ],
        ),
        migrations.RunPython(
            delete_legacy_refresh_tokens,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
