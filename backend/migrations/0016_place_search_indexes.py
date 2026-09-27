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
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        'CREATE INDEX "place_name_lower_prefix_idx" '
                        'ON "backend_place" ((LOWER("name")) varchar_pattern_ops)'
                    ),
                    reverse_sql='DROP INDEX "place_name_lower_prefix_idx"',
                ),
            ],
            state_operations=[
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
            ],
        ),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    # Approved places are written rarely, so skip the GIN
                    # pending list; unflushed entries make the planner fall
                    # back to sequential scans until autovacuum runs.
                    sql=(
                        'CREATE INDEX "place_name_lower_trgm_idx" '
                        'ON "backend_place" USING gin '
                        '((LOWER("name")) gin_trgm_ops) '
                        "WITH (fastupdate = off)"
                    ),
                    reverse_sql='DROP INDEX "place_name_lower_trgm_idx"',
                ),
            ],
            state_operations=[
                migrations.AddIndex(
                    model_name="place",
                    index=django.contrib.postgres.indexes.GinIndex(
                        django.contrib.postgres.indexes.OpClass(
                            django.db.models.functions.text.Lower("name"),
                            name="gin_trgm_ops",
                        ),
                        name="place_name_lower_trgm_idx",
                        fastupdate=False,
                    ),
                ),
            ],
        ),
    ]
