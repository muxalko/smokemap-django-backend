from django.db import migrations, models


INITIAL_CATEGORIES = (
    ("indoors", "Indoors"),
    ("outdoors", "Outdoors"),
    ("rooftop", "Rooftop"),
    ("underground", "Underground"),
    ("on-the-water", "On the water"),
    ("underwater", "Underwater"),
    ("in-the-air", "In the air"),
    ("other", "Other"),
)


def backfill_category_slugs(apps, schema_editor):
    category_model = apps.get_model("backend", "Category")
    expected_names = {name for _slug, name in INITIAL_CATEGORIES}
    existing_names = set(category_model.objects.values_list("name", flat=True))
    missing_names = expected_names - existing_names
    unexpected_names = existing_names - expected_names
    if missing_names or unexpected_names:
        raise RuntimeError(
            "Cannot issue baseline category slugs: "
            f"missing names={sorted(missing_names)!r}; "
            f"unexpected names={sorted(unexpected_names)!r}."
        )

    for slug, name in INITIAL_CATEGORIES:
        category = category_model.objects.get(name=name)
        if category.slug not in (None, slug):
            raise RuntimeError(
                f"Category {name!r} already has conflicting slug {category.slug!r}."
            )
        conflicting_name = (
            category_model.objects.filter(slug=slug)
            .exclude(pk=category.pk)
            .values_list("name", flat=True)
            .first()
        )
        if conflicting_name is not None:
            raise RuntimeError(
                f"Slug {slug!r} is already assigned to category {conflicting_name!r}."
            )
        category_model.objects.filter(pk=category.pk).update(slug=slug)


def clear_category_slugs(apps, schema_editor):
    category_model = apps.get_model("backend", "Category")
    for slug, name in INITIAL_CATEGORIES:
        category_model.objects.filter(name=name, slug=slug).update(slug=None)


class Migration(migrations.Migration):
    dependencies = [
        ("backend", "0003_refresh_token_lifecycle"),
    ]

    operations = [
        migrations.AddField(
            model_name="category",
            name="slug",
            field=models.SlugField(db_index=False, max_length=50, null=True),
        ),
        migrations.RunPython(backfill_category_slugs, clear_category_slugs),
        migrations.AlterField(
            model_name="category",
            name="slug",
            field=models.SlugField(max_length=50, unique=True),
        ),
    ]
