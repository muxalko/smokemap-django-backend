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
from django.db import connection, transaction

from .models import (
    Address,
    Category,
    CustomUser,
    Image,
    MediaUploadIntent,
    Place,
    Request,
    RequestTag,
    SubmissionIdempotency,
    SubmissionLifecycleEvent,
    SubmissionOperation,
)
from .tagging import attach_request_tags, normalize_submission_tags


IDEMPOTENCY_KEY_MAX_LENGTH = 255
CREATE_OPERATION = SubmissionOperation.CREATE
EDIT_OPERATION = SubmissionOperation.EDIT
FINALIZE_OPERATION = SubmissionOperation.FINALIZE

# The adopted contract fixes the inclusive duplicate radius in metres and the
# owner-facing states that participate in the owner-scoped half of the check.
DUPLICATE_RADIUS_METRES = 25.0
OWNER_DUPLICATE_STATES = (Request.State.DRAFT, Request.State.PENDING)
MAX_RETAINED_ATTACHMENTS = 3

# Namespacing the advisory-lock derivation keeps this serialization domain from
# colliding with any other transaction-scoped advisory lock in the application.
CANONICAL_NAME_LOCK_NAMESPACE = b"smokemap.submission.canonical_name.v3\x00"

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


class SubmissionOperationError(ValueError):
    """Base class for stable, non-sensitive submission operation outcomes."""

    code = "SUBMISSION_OPERATION_FAILED"


class SubmissionNotFound(SubmissionOperationError):
    """Another owner's submission and a missing submission share this surface."""

    code = "NOT_FOUND"


class SubmissionAuthenticationRequired(SubmissionOperationError):
    code = "UNAUTHENTICATED"


class SubmissionStateError(SubmissionOperationError):
    code = "INVALID_SUBMISSION_STATE"


class DuplicateSubmission(SubmissionOperationError):
    code = "DUPLICATE_SUBMISSION"


class MediaNotReady(SubmissionOperationError):
    code = "MEDIA_NOT_READY"


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


@dataclass(frozen=True)
class SubmissionSnapshot:
    """Immutable v3 operation result stored in durable idempotency evidence."""

    submission_id: int
    state: str
    name: str
    category_slug: str
    longitude: float
    latitude: float
    address_label: Optional[str]
    tags: Tuple[str, ...]
    description: Optional[str]
    website: Optional[str]

    @property
    def id(self):
        return self.submission_id

    @property
    def pk(self):
        return self.submission_id

    def __int__(self):
        return int(self.submission_id)

    def as_result(self):
        return {
            "snapshot_version": 1,
            "submission_id": self.submission_id,
            "state": str(self.state),
            "name": self.name,
            "category_slug": self.category_slug,
            "longitude": self.longitude,
            "latitude": self.latitude,
            "address_label": self.address_label,
            "tags": list(self.tags),
            "description": self.description,
            "website": self.website,
        }

    @classmethod
    def from_result(cls, result):
        if not isinstance(result, dict) or result.get("snapshot_version") != 1:
            raise ValueError("invalid submission idempotency snapshot")
        return cls(
            submission_id=int(result["submission_id"]),
            state=str(result["state"]),
            name=str(result["name"]),
            category_slug=str(result["category_slug"]),
            longitude=float(result["longitude"]),
            latitude=float(result["latitude"]),
            address_label=result.get("address_label"),
            tags=tuple(result.get("tags") or ()),
            description=result.get("description"),
            website=result.get("website"),
        )


def submission_snapshot(validated, submission_id, state):
    return SubmissionSnapshot(
        submission_id=int(submission_id),
        state=str(state),
        name=validated.name,
        category_slug=validated.category.slug,
        longitude=validated.longitude,
        latitude=validated.latitude,
        address_label=validated.address_label,
        tags=tuple(tag.display for tag in validated.tags),
        description=validated.description,
        website=validated.website,
    )


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


def _hash_payload(payload):
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_request_hash(validated, target_submission_id=None):
    """Hash the canonical payload, binding it to a target when one exists.

    Creation has no prior target, so its hash is unchanged. Every operation on an
    existing submission includes the target so that replaying one key against a
    different submission is an idempotency conflict rather than a second edit.
    """
    payload = validated.canonical_payload()
    if target_submission_id is not None:
        payload = {**payload, "submission_id": str(target_submission_id)}
    return _hash_payload(payload)


def canonical_place_name(value):
    """NFKC-normalize, collapse whitespace, and case-fold a display name."""
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


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


def _locked_owned_submission(actor, submission_id):
    """Lock one submission row and hide every submission this actor cannot see.

    ``of=("self",)`` keeps the row lock on the submission alone so that reading
    the related address and category never locks rows shared with public records.
    """
    try:
        submission = (
            Request.objects.select_for_update(of=("self",))
            .select_related("address", "category")
            .get(pk=submission_id, owner_id=actor.pk)
        )
    except (Request.DoesNotExist, ValidationError, ValueError, TypeError) as error:
        raise SubmissionNotFound("submission not found") from error
    return submission


def _locked_active_actor(actor):
    try:
        locked_actor = CustomUser.objects.select_for_update().get(pk=actor.pk)
    except (CustomUser.DoesNotExist, ValueError, TypeError) as error:
        raise SubmissionAuthenticationRequired(
            "active authentication is required"
        ) from error
    if not locked_actor.is_active:
        raise SubmissionAuthenticationRequired("active authentication is required")
    return locked_actor


def _replayed_record(actor, operation, key, request_hash, submission):
    existing = (
        SubmissionIdempotency.objects.select_related("submission")
        .filter(actor=actor, operation=operation, key=key)
        .first()
    )
    if existing is None:
        return None
    if existing.request_hash != request_hash or existing.submission_id != submission.pk:
        raise IdempotencyConflict(
            "idempotency key was already used with a different request"
        )
    return existing


def _record_operation(*, actor, operation, key, request_hash, submission, result,
                      from_state, to_state):
    idempotency = SubmissionIdempotency.objects.create(
        actor=actor,
        operation=operation,
        key=key,
        request_hash=request_hash,
        submission=submission,
        original_result=result,
    )
    SubmissionLifecycleEvent.objects.create(
        submission=submission,
        actor=actor,
        operation=operation,
        from_state=from_state,
        to_state=to_state,
        outcome=SubmissionLifecycleEvent.Outcome.SUCCEEDED,
        idempotency=idempotency,
    )
    return idempotency


def _replace_address_if_changed(submission, validated):
    """Point the submission at a fresh address row when its content changes.

    Address rows may be shared with legacy or public records, so an edit never
    mutates one in place. The caller removes the previous row only when it is
    provably orphaned after the reassignment commits inside this transaction.
    """
    address = submission.address
    point = Point(validated.longitude, validated.latitude, srid=4326)
    if (
        address.addressString == validated.address_label
        and address.location.srid == point.srid
        and address.location.x == point.x
        and address.location.y == point.y
    ):
        return None
    submission.address = Address.objects.create(
        addressString=validated.address_label,
        location=point,
    )
    return address.pk


def _discard_orphaned_address(address_id):
    if address_id is None:
        return False
    locked = Address.objects.select_for_update().filter(pk=address_id).first()
    if locked is None:
        return False
    if (
        Request.objects.filter(address_id=address_id).exists()
        or Place.objects.filter(address_id=address_id).exists()
    ):
        return False
    locked.delete()
    return True


def _current_submission_snapshot(submission):
    """Re-read the stored proposal as raw input for the shared validation rules."""
    location = submission.address.location
    return {
        "name": submission.name,
        "category_slug": submission.category.slug,
        "longitude": location.x,
        "latitude": location.y,
        "address_label": submission.address.addressString,
        "tags": [
            link.display
            for link in submission.request_tags.order_by("position", "pk")
        ],
        "description": submission.description,
        "website": submission.website,
    }


def _require_ready_media(actor, submission):
    """Recheck retained media under the established parent-first lock order.

    The submission row is already locked by the caller; intents are locked before
    attachments exactly as the media services do. No object-store call is made
    here, so no network I/O runs while these locks are held.
    """
    intents = list(
        MediaUploadIntent.objects.select_for_update()
        .filter(submission=submission)
        .order_by("slot", "id")
    )
    attachments = list(
        Image.objects.select_for_update()
        .filter(request=submission, is_managed=True)
        .order_by("position", "pk")
    )
    allowed_intent_states = {
        MediaUploadIntent.State.ATTACHED,
        MediaUploadIntent.State.DELETED,
    }
    if any(intent.state not in allowed_intent_states for intent in intents):
        raise MediaNotReady("submission media is not ready for finalization")
    if len(attachments) > MAX_RETAINED_ATTACHMENTS:
        raise MediaNotReady("submission media is not ready for finalization")

    intents_by_id = {intent.pk: intent for intent in intents}
    positions = set()
    digests = set()
    attached_intent_ids = set()
    for image in attachments:
        intent = intents_by_id.get(image.intent_id)
        if (
            image.state != "attached"
            or image.owner_id != actor.pk
            or image.request_id != submission.pk
            or image.place_id is not None
            or image.position is None
            or not 0 <= image.position < MAX_RETAINED_ATTACHMENTS
            or image.position in positions
            or not image.sha256
            or image.sha256 in digests
            or intent is None
            or intent.state != MediaUploadIntent.State.ATTACHED
            or intent.owner_id != actor.pk
            or intent.submission_id != submission.pk
            or image.storage_key != intent.sealed_object_key
            or image.sha256 != intent.server_sha256
        ):
            raise MediaNotReady("submission media is not ready for finalization")
        positions.add(image.position)
        digests.add(image.sha256)
        attached_intent_ids.add(intent.pk)

    expected_attached_intent_ids = {
        intent.pk
        for intent in intents
        if intent.state == MediaUploadIntent.State.ATTACHED
    }
    if attached_intent_ids != expected_attached_intent_ids:
        raise MediaNotReady("submission media is not ready for finalization")
    return attachments


def canonical_name_lock_key(canonical):
    digest = hashlib.sha256(
        CANONICAL_NAME_LOCK_NAMESPACE + canonical.encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def acquire_canonical_name_lock(canonical):
    """Serialize duplicate revalidation on the FULL canonical name.

    The lock is taken whether or not a matching row exists, so an absent row
    cannot let two concurrent finalizations bypass the spatial check. It is
    transaction-scoped and released when the surrounding transaction ends.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s)", [canonical_name_lock_key(canonical)]
        )


def _nearby_names_sql(table, alias, extra_where=""):
    quote = connection.ops.quote_name
    return (
        f"SELECT {alias}.name FROM {quote(table)} AS {alias} "
        f"JOIN {quote(Address._meta.db_table)} AS a ON a.id = {alias}.address_id "
        f"WHERE {extra_where}ST_DWithin("
        "a.location::geography, "
        "ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)"
    )


def _nearby_public_place_names(longitude, latitude):
    with connection.cursor() as cursor:
        cursor.execute(
            _nearby_names_sql(Place._meta.db_table, "p"),
            [longitude, latitude, DUPLICATE_RADIUS_METRES],
        )
        return [row[0] for row in cursor.fetchall()]


def _nearby_owner_proposal_names(owner_id, exclude_submission_id, longitude, latitude):
    """Only this owner's other non-terminal proposals; never another owner's."""
    states = ", ".join(["%s"] * len(OWNER_DUPLICATE_STATES))
    sql = _nearby_names_sql(
        Request._meta.db_table,
        "r",
        extra_where=f"r.owner_id = %s AND r.id <> %s AND r.state IN ({states}) AND ",
    )
    parameters = [owner_id, exclude_submission_id]
    parameters += [str(state) for state in OWNER_DUPLICATE_STATES]
    parameters += [longitude, latitude, DUPLICATE_RADIUS_METRES]
    with connection.cursor() as cursor:
        cursor.execute(sql, parameters)
        return [row[0] for row in cursor.fetchall()]


def _assert_no_duplicate(actor, submission, validated, canonical):
    candidates = _nearby_public_place_names(validated.longitude, validated.latitude)
    candidates += _nearby_owner_proposal_names(
        actor.pk, submission.pk, validated.longitude, validated.latitude
    )
    for candidate in candidates:
        if candidate is not None and canonical_place_name(candidate) == canonical:
            raise DuplicateSubmission(
                "a matching place already exists within 25 metres"
            )


def edit_submission(actor, submission_id, idempotency_key, raw_input):
    """Replace every proposed content field of one owner-held draft atomically."""
    key = validate_idempotency_key(idempotency_key)
    validated = validate_submission_input(raw_input)

    with transaction.atomic():
        submission = _locked_owned_submission(actor, submission_id)
        locked_actor = _locked_active_actor(actor)
        if submission.owner_id != locked_actor.pk:
            raise SubmissionNotFound("submission not found")

        request_hash = canonical_request_hash(
            validated, target_submission_id=submission.pk
        )
        existing = _replayed_record(
            locked_actor, EDIT_OPERATION, key, request_hash, submission
        )
        if existing is not None:
            return SubmissionSnapshot.from_result(existing.original_result), True
        if submission.state != Request.State.DRAFT:
            raise SubmissionStateError("only a draft submission can be edited")

        previous_address_id = _replace_address_if_changed(submission, validated)
        submission.name = validated.name
        submission.category = validated.category
        submission.description = validated.description
        submission.website = validated.website
        submission.save(
            update_fields=[
                "name",
                "category",
                "description",
                "website",
                "address",
                "date_updated",
            ]
        )
        RequestTag.objects.filter(request=submission).delete()
        attach_request_tags(submission, validated.tags)
        _discard_orphaned_address(previous_address_id)

        result = submission_snapshot(
            validated, submission.pk, Request.State.DRAFT
        )
        _record_operation(
            actor=locked_actor,
            operation=EDIT_OPERATION,
            key=key,
            request_hash=request_hash,
            submission=submission,
            result=result.as_result(),
            from_state=Request.State.DRAFT,
            to_state=Request.State.DRAFT,
        )
        return result, False


def finalize_submission(actor, submission_id, idempotency_key):
    """Move one owner-held draft to pending after a full revalidation."""
    key = validate_idempotency_key(idempotency_key)

    with transaction.atomic():
        submission = _locked_owned_submission(actor, submission_id)
        locked_actor = _locked_active_actor(actor)
        if submission.owner_id != locked_actor.pk:
            raise SubmissionNotFound("submission not found")

        request_hash = _hash_payload({"submission_id": str(submission.pk)})
        existing = _replayed_record(
            locked_actor, FINALIZE_OPERATION, key, request_hash, submission
        )
        if existing is not None:
            return SubmissionSnapshot.from_result(existing.original_result), True
        if submission.state != Request.State.DRAFT:
            # A new key against an already-pending submission is an invalid
            # transition, never a second lifecycle event.
            raise SubmissionStateError("only a draft submission can be finalized")

        validated = validate_submission_input(_current_submission_snapshot(submission))
        _require_ready_media(locked_actor, submission)

        canonical = canonical_place_name(validated.name)
        acquire_canonical_name_lock(canonical)
        _assert_no_duplicate(locked_actor, submission, validated, canonical)

        submission.state = Request.State.PENDING
        submission.save(update_fields=["state", "date_updated"])
        result = submission_snapshot(
            validated, submission.pk, Request.State.PENDING
        )
        _record_operation(
            actor=locked_actor,
            operation=FINALIZE_OPERATION,
            key=key,
            request_hash=request_hash,
            submission=submission,
            result=result.as_result(),
            from_state=Request.State.DRAFT,
            to_state=Request.State.PENDING,
        )
        return result, False
