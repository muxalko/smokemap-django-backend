import unicodedata
from dataclasses import dataclass

from django.core.exceptions import ValidationError


MAX_SUBMISSION_TAGS = 10
MIN_TAG_LENGTH = 3
MAX_TAG_LENGTH = 50


@dataclass(frozen=True)
class NormalizedTag:
    display: str
    canonical: str


def normalize_tag_text(value):
    if value is None:
        raise ValidationError({"tags": "Tags cannot contain null values."})
    if not isinstance(value, str):
        raise ValidationError({"tags": "Every tag must be a string."})

    display = " ".join(unicodedata.normalize("NFKC", value).split())
    if not MIN_TAG_LENGTH <= len(display) <= MAX_TAG_LENGTH:
        raise ValidationError(
            {
                "tags": (
                    f"Every tag must contain {MIN_TAG_LENGTH} through "
                    f"{MAX_TAG_LENGTH} characters after normalization."
                )
            }
        )

    canonical = display.casefold()
    if len(canonical) > MAX_TAG_LENGTH:
        raise ValidationError(
            {
                "tags": (
                    "A tag's case-folded canonical value must not exceed "
                    f"{MAX_TAG_LENGTH} characters."
                )
            }
        )
    return NormalizedTag(display=display, canonical=canonical)


def normalize_submission_tags(values):
    if values is None:
        return []

    normalized = []
    seen = set()
    for value in values:
        tag = normalize_tag_text(value)
        if tag.canonical in seen:
            raise ValidationError(
                {"tags": "A submission cannot contain duplicate tags."}
            )
        seen.add(tag.canonical)
        normalized.append(tag)

    if len(normalized) > MAX_SUBMISSION_TAGS:
        raise ValidationError(
            {"tags": f"A submission can contain at most {MAX_SUBMISSION_TAGS} tags."}
        )
    return normalized


def attach_request_tags(request, normalized_tags):
    from .models import RequestTag, Tag

    links = []
    for position, normalized in enumerate(normalized_tags):
        tag, _created = Tag.objects.get_or_create(
            canonical=normalized.canonical,
            defaults={
                "name": normalized.display,
                "is_public": False,
            },
        )
        links.append(
            RequestTag(
                request=request,
                tag=tag,
                display=normalized.display,
                position=position,
            )
        )
    RequestTag.objects.bulk_create(links)
    return links
