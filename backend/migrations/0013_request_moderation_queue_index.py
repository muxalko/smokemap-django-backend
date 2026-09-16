from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("backend", "0012_draft_media_resume_and_reorder"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="request",
            index=models.Index(
                fields=["state", "date_created", "id"],
                name="request_mod_queue_idx",
            ),
        ),
    ]
