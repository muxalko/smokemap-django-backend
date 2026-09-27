from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("backend", "0013_request_moderation_queue_index"),
    ]

    operations = [
        migrations.AddField(
            model_name="submissionlifecycleevent",
            name="comment",
            field=models.TextField(blank=True, editable=False, null=True),
        ),
        migrations.AlterField(
            model_name="image",
            name="place",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="backend.place",
            ),
        ),
        migrations.AlterField(
            model_name="image",
            name="request",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="backend.request",
            ),
        ),
    ]
