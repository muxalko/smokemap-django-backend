import unicodedata

from django.db import migrations, models
import django.db.models.deletion


MIN_TAG_LENGTH = 3
MAX_TAG_LENGTH = 50
MAX_SUBMISSION_TAGS = 10


def normalize_legacy_tag(value, context):
    if value is None or not isinstance(value, str):
        raise RuntimeError(f"Cannot migrate {context}: tag values must be strings.")
    display = " ".join(unicodedata.normalize("NFKC", value).split())
    if not MIN_TAG_LENGTH <= len(display) <= MAX_TAG_LENGTH:
        raise RuntimeError(
            f"Cannot migrate {context}: normalized tag {display!r} must contain "
            f"{MIN_TAG_LENGTH} through {MAX_TAG_LENGTH} characters."
        )
    canonical = display.casefold()
    if len(canonical) > MAX_TAG_LENGTH:
        raise RuntimeError(
            f"Cannot migrate {context}: canonical tag {canonical!r} exceeds "
            f"{MAX_TAG_LENGTH} characters."
        )
    return display, canonical


def migrate_legacy_tags(apps, schema_editor):
    tag_model = apps.get_model("backend", "Tag")
    request_model = apps.get_model("backend", "Request")
    request_tag_model = apps.get_model("backend", "RequestTag")

    existing_by_canonical = {}
    normalized_existing = []
    for tag in tag_model.objects.order_by("pk").iterator():
        display, canonical = normalize_legacy_tag(tag.name, f"Tag {tag.pk}")
        conflicting = existing_by_canonical.get(canonical)
        if conflicting is not None:
            raise RuntimeError(
                "Cannot migrate canonical tag collision between "
                f"Tag {conflicting.pk} ({conflicting.name!r}) and "
                f"Tag {tag.pk} ({tag.name!r})."
            )
        existing_by_canonical[canonical] = tag
        normalized_existing.append((tag, display, canonical))

    for tag, display, canonical in normalized_existing:
        tag.name = display
        tag.canonical = canonical
        tag.is_public = True
        tag.save(update_fields=("name", "canonical", "is_public"))

    for request in request_model.objects.order_by("pk").iterator():
        if request.legacy_tags is None:
            raise RuntimeError(
                f"Cannot migrate Request {request.pk}: the legacy tag array is null."
            )

        normalized_request_tags = []
        seen = set()
        for value in request.legacy_tags:
            display, canonical = normalize_legacy_tag(
                value,
                f"Request {request.pk}",
            )
            if canonical in seen:
                raise RuntimeError(
                    f"Cannot migrate Request {request.pk}: duplicate canonical tag "
                    f"{canonical!r} in the legacy tag array."
                )
            seen.add(canonical)
            normalized_request_tags.append((display, canonical))

        if len(normalized_request_tags) > MAX_SUBMISSION_TAGS:
            raise RuntimeError(
                f"Cannot migrate Request {request.pk}: more than "
                f"{MAX_SUBMISSION_TAGS} distinct tags."
            )

        links = []
        for position, (display, canonical) in enumerate(normalized_request_tags):
            tag = existing_by_canonical.get(canonical)
            if tag is None:
                tag = tag_model.objects.create(
                    name=display,
                    canonical=canonical,
                    is_public=False,
                )
                existing_by_canonical[canonical] = tag
            links.append(
                request_tag_model(
                    request_id=request.pk,
                    tag_id=tag.pk,
                    display=display,
                    position=position,
                )
            )
        request_tag_model.objects.bulk_create(links)


def restore_legacy_request_tags(apps, schema_editor):
    request_model = apps.get_model("backend", "Request")
    request_tag_model = apps.get_model("backend", "RequestTag")
    for request in request_model.objects.order_by("pk").iterator():
        request.legacy_tags = list(
            request_tag_model.objects.filter(request_id=request.pk)
            .order_by("position")
            .values_list("display", flat=True)
        )
        request.save(update_fields=("legacy_tags",))


class Migration(migrations.Migration):
    dependencies = [
        ("backend", "0004_category_slug"),
    ]

    operations = [
        migrations.AddField(
            model_name="tag",
            name="canonical",
            field=models.CharField(max_length=50, null=True),
        ),
        migrations.AddField(
            model_name="tag",
            name="is_public",
            field=models.BooleanField(default=False),
        ),
        migrations.RenameField(
            model_name="request",
            old_name="tags",
            new_name="legacy_tags",
        ),
        migrations.CreateModel(
            name="RequestTag",
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
                ("display", models.CharField(max_length=50)),
                ("position", models.PositiveSmallIntegerField()),
                (
                    "request",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="request_tags",
                        to="backend.request",
                    ),
                ),
                (
                    "tag",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="request_tags",
                        to="backend.tag",
                    ),
                ),
            ],
            options={
                "ordering": ["position"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("request", "tag"),
                        name="unique_request_tag",
                    ),
                    models.UniqueConstraint(
                        fields=("request", "position"),
                        name="unique_request_tag_position",
                    ),
                    models.CheckConstraint(
                        check=models.Q(("position__gte", 0), ("position__lt", 10)),
                        name="request_tag_position_range",
                    ),
                    models.CheckConstraint(
                        check=models.Q(("display__regex", r"^.{3,50}$")),
                        name="request_tag_display_length",
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name="request",
            name="tags",
            field=models.ManyToManyField(
                related_name="requests",
                through="backend.RequestTag",
                to="backend.tag",
            ),
        ),
        migrations.RunPython(migrate_legacy_tags, restore_legacy_request_tags),
        migrations.RemoveField(
            model_name="request",
            name="legacy_tags",
        ),
        migrations.AlterField(
            model_name="tag",
            name="canonical",
            field=models.CharField(editable=False, max_length=50, unique=True),
        ),
    ]
