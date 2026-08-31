from django.db import migrations, models
from django.db.models import F, Value
from django.db.models.lookups import LessThanOrEqual


class Migration(migrations.Migration):
    dependencies = [
        ("backend", "0007_owner_bound_media"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="image",
            constraint=models.CheckConstraint(
                check=(
                    models.Q(is_managed=False)
                    | LessThanOrEqual(F("width") * F("height"), Value(25_000_000))
                ),
                name="managed_image_area_limit",
            ),
        ),
    ]
