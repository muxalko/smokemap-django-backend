import django.contrib.postgres.indexes
import django.db.models.functions.text
from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("backend", "0015_protect_submission_audit_evidence"),
    ]

    operations = [
        TrigramExtension(),
        migrations.AddIndex(
            model_name="place",
            index=models.Index(
                django.contrib.postgres.indexes.OpClass(
                    django.db.models.functions.text.Lower("name"),
                    name="varchar_pattern_ops",
                ),
                name="place_name_lower_prefix_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="place",
            index=django.contrib.postgres.indexes.GinIndex(
                django.contrib.postgres.indexes.OpClass(
                    django.db.models.functions.text.Lower("name"),
                    name="gin_trgm_ops",
                ),
                name="place_name_lower_trgm_idx",
            ),
        ),
    ]
