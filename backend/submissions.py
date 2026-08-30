import hashlib
import ipaddress
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import idna
from django.contrib.gis.geos import Point
from django.core.exceptions import ValidationError
from django.db import transaction

from .models import (
    Address,
    Category,
    CustomUser,
    Request,
    SubmissionIdempotency,
    SubmissionLifecycleEvent,
    SubmissionOperation,
)
from .tagging import attach_request_tags, normalize_submission_tags


IDEMPOTENCY_KEY_MAX_LENGTH = 255
CREATE_OPERATION = SubmissionOperation.CREATE

# Version 1 is deliberately code-owned. Exact names and every subdomain are denied.
RESERVED_HOSTNAMES_V1 = frozenset(
    {
        "alt",
        "example",
        "example.com",
        "example.net",
        "example.org",
        "home.arpa",
        "in-addr.arpa",
        "internal",
        "invalid",
        "ip6.arpa",
        "ipv4only.arpa",
        "local",
        "localdomain",
        "localhost",
        "localhost.localdomain",
        "onion",
        "resolver.arpa",
        "test",
    }
)

_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MALFORMED_PERCENT = re.compile(r"%(?![0-9a-fA-F]{2})")
_ENCODED_CONTROL = re.compile(r"%(?:0[0-9a-fA-F]|1[0-9a-fA-F]|7[fF])")
_PERCENT_ESCAPE = re.compile(r"%([0-9a-fA-F]{2})")


class SubmissionInputError(ValueError):
    def __init__(self, field, message):
        super().__init__(message)
        self.field = field


class IdempotencyConflict(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedSubmission:
    name: str
    category: Category
    longitude: float
    latitude: float
    address_label: Optional[str]
    tags: Tuple
    description: Optional[str]
    website: Optional[str]

    def canonical_payload(self):
        return {
            "address_label": self.address_label,
            "category_slug": self.category.slug,
            "description": self.description,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "name": self.name,
            "tags": [
                {"canonical": tag.canonical, "display": tag.display}
                for tag in self.tags
            ],
            "website": self.website,
        }


def _normalize_plain_text(value, field, minimum, maximum, optional=False):
    if value is None:
        if optional:
            return None
        raise SubmissionInputError(field, f"{field} is required")
    if not isinstance(value, str):
        raise SubmissionInputError(field, f"{field} must be a string")

    normalized = unicodedata.normalize("NFKC", value)
    if any(
        unicodedata.category(character).startswith("C")
        and not character.isspace()
        for character in normalized
    ):
        raise SubmissionInputError(field, f"{field} contains unsupported characters")
    normalized = " ".join(normalized.split())
    if optional and not normalized:
        return None
    if not minimum <= len(normalized) <= maximum:
        raise SubmissionInputError(
            field,
            f"{field} must contain {minimum} through {maximum} characters after normalization",
        )
    return normalized


def _normalize_coordinate(value, field, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SubmissionInputError(field, f"{field} must be a finite number")
    coordinate = float(value)
    if not math.isfinite(coordinate):
        raise SubmissionInputError(field, f"{field} must be a finite number")
    if not minimum <= coordinate <= maximum:
        raise SubmissionInputError(
            field, f"{field} must be between {minimum} and {maximum}"
        )
    # Treat signed zero as the same canonical coordinate.
    return 0.0 if coordinate == 0 else coordinate


def _is_reserved_hostname(hostname):
    return any(
        hostname == reserved or hostname.endswith("." + reserved)
        for reserved in RESERVED_HOSTNAMES_V1
    )


def _numeric_host_component(component):
    try:
        if component.lower().startswith("0x"):
            int(component[2:], 16)
        elif len(component) > 1 and component.startswith("0"):
            int(component, 8)
        else:
            int(component, 10)
    except ValueError:
        return False
    return True


def _looks_like_ip_representation(hostname):
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return True
    components = hostname.split(".")
    return bool(components) and all(
        _numeric_host_component(component) for component in components
    )


def normalize_website(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise SubmissionInputError("website", "website must be a string")

    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized:
        return None
    if len(normalized) > 255:
        raise SubmissionInputError("website", "website must not exceed 255 characters")
    if any(character.isspace() or unicodedata.category(character).startswith("C") for character in normalized):
        raise SubmissionInputError("website", "website contains invalid characters")
    if "\\" in normalized:
        raise SubmissionInputError("website", "website contains invalid characters")
    if _MALFORMED_PERCENT.search(normalized):
        raise SubmissionInputError("website", "website contains malformed percent escapes")
    if _ENCODED_CONTROL.search(normalized):
        raise SubmissionInputError("website", "website contains encoded control characters")
    if "#" in normalized:
        raise SubmissionInputError("website", "website fragments are not allowed")

    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError as error:
        raise SubmissionInputError("website", "website is malformed") from error
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise SubmissionInputError("website", "website must be an absolute HTTPS URL")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise SubmissionInputError("website", "website credentials are not allowed")
    if parsed.netloc.endswith(":"):
        raise SubmissionInputError("website", "website is malformed")
    if port not in (None, 443):
        raise SubmissionInputError("website", "website uses a non-default port")

    hostname = parsed.hostname
    if not hostname:
        raise SubmissionInputError("website", "website must contain a hostname")
    if hostname.endswith("."):
        hostname = hostname[:-1]
    if not hostname or hostname.endswith("."):
        raise SubmissionInputError("website", "website hostname is malformed")
    try:
        ascii_hostname = idna.encode(
            hostname,
            uts46=True,
            std3_rules=True,
        ).decode("ascii").lower()
    except idna.IDNAError as error:
        raise SubmissionInputError("website", "website hostname is malformed") from error

    labels = ascii_hostname.split(".")
    if (
        len(labels) < 2
        or len(ascii_hostname) > 253
        or any(not _DNS_LABEL.fullmatch(label) for label in labels)
        or labels[-1].isdigit()
    ):
        raise SubmissionInputError("website", "website must use a valid multi-label DNS hostname")
    if _looks_like_ip_representation(ascii_hostname):
        raise SubmissionInputError("website", "website IP addresses are not allowed")
    if _is_reserved_hostname(ascii_hostname):
        raise SubmissionInputError("website", "website hostname is reserved")

    canonical_path = _PERCENT_ESCAPE.sub(
        lambda match: "%" + match.group(1).upper(), parsed.path
    )
    canonical_query = _PERCENT_ESCAPE.sub(
        lambda match: "%" + match.group(1).upper(), parsed.query
    )
    canonical = urlunsplit(
        ("https", ascii_hostname, canonical_path, canonical_query, "")
    )
    if len(canonical) > 255:
        raise SubmissionInputError("website", "website must not exceed 255 characters")
    return canonical


def validate_submission_input(raw):
    name = _normalize_plain_text(raw.get("name"), "name", 2, 100)
    category_slug = raw.get("category_slug")
    if not isinstance(category_slug, str):
        raise SubmissionInputError("category_slug", "category_slug is required")
    category = Category.objects.filter(slug=category_slug).first()
    if category is None:
        raise SubmissionInputError("category_slug", "category_slug is invalid")

    try:
        normalized_tags = tuple(normalize_submission_tags(raw.get("tags")))
    except ValidationError as error:
        # The tag module raises Django ValidationError with contract-safe messages.
        messages = getattr(error, "messages", None)
        message = messages[0] if messages else "tags are invalid"
        raise SubmissionInputError("tags", message) from error

    return ValidatedSubmission(
        name=name,
        category=category,
        longitude=_normalize_coordinate(raw.get("longitude"), "longitude", -180, 180),
        latitude=_normalize_coordinate(raw.get("latitude"), "latitude", -90, 90),
        address_label=_normalize_plain_text(
            raw.get("address_label"), "address_label", 1, 255, optional=True
        ),
        tags=normalized_tags,
        description=_normalize_plain_text(
            raw.get("description"), "description", 1, 255, optional=True
        ),
        website=normalize_website(raw.get("website")),
    )


def validate_idempotency_key(value):
    if not isinstance(value, str) or not 1 <= len(value) <= IDEMPOTENCY_KEY_MAX_LENGTH:
        raise SubmissionInputError(
            "idempotency_key",
            f"idempotency_key must contain 1 through {IDEMPOTENCY_KEY_MAX_LENGTH} characters",
        )
    if "\x00" in value:
        raise SubmissionInputError(
            "idempotency_key", "idempotency_key contains an unsupported character"
        )
    return value


def canonical_request_hash(validated):
    encoded = json.dumps(
        validated.canonical_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@transaction.atomic
def create_submission(actor, idempotency_key, raw_input):
    """Create or replay one owner-bound draft aggregate in a single transaction."""
    key = validate_idempotency_key(idempotency_key)
    validated = validate_submission_input(raw_input)
    request_hash = canonical_request_hash(validated)

    # Serializing on the actor makes same-scope races deterministic even before the
    # idempotency row exists. The unique constraint remains the durable invariant.
    locked_actor = CustomUser.objects.select_for_update().get(pk=actor.pk)
    if not locked_actor.is_active:
        raise SubmissionInputError("authentication", "active authentication is required")

    existing = (
        SubmissionIdempotency.objects.select_related("submission")
        .filter(actor=locked_actor, operation=CREATE_OPERATION, key=key)
        .first()
    )
    if existing is not None:
        if existing.request_hash != request_hash:
            raise IdempotencyConflict(
                "idempotency key was already used with a different request"
            )
        return existing.submission, True

    address = Address.objects.create(
        addressString=validated.address_label,
        location=Point(
            validated.longitude,
            validated.latitude,
            srid=4326,
        ),
    )
    submission = Request.objects.create(
        name=validated.name,
        category=validated.category,
        description=validated.description,
        address=address,
        website=validated.website,
        owner=locked_actor,
        state=Request.State.DRAFT,
        approved=False,
    )
    attach_request_tags(submission, validated.tags)

    original_result = {
        "state": Request.State.DRAFT,
        "submission_id": submission.pk,
    }
    idempotency = SubmissionIdempotency.objects.create(
        actor=locked_actor,
        operation=CREATE_OPERATION,
        key=key,
        request_hash=request_hash,
        submission=submission,
        original_result=original_result,
    )
    SubmissionLifecycleEvent.objects.create(
        submission=submission,
        actor=locked_actor,
        operation=CREATE_OPERATION,
        from_state=None,
        to_state=Request.State.DRAFT,
        outcome=SubmissionLifecycleEvent.Outcome.SUCCEEDED,
        idempotency=idempotency,
    )
    return submission, False
