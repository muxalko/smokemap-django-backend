from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("backend", "0014_moderation_deletion_safety"),
    ]

    operations = [
        migrations.AlterField(
            model_name="submissionidempotency",
            name="submission",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="idempotency_records",
                to="backend.request",
            ),
        ),
        migrations.AlterField(
            model_name="submissionlifecycleevent",
            name="idempotency",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="lifecycle_event",
                to="backend.submissionidempotency",
            ),
        ),
        migrations.AlterField(
            model_name="submissionlifecycleevent",
            name="submission",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="lifecycle_events",
                to="backend.request",
            ),
        ),
    ]
