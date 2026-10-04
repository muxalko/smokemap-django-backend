"""Serialized, auditable submission lifecycle transitions.

M4 lifecycle writes live here rather than in GraphQL resolvers.  Every service
locks the target aggregate, re-reads the actor from the database, validates the
transition, and writes the state change and immutable lifecycle evidence in one
transaction.
"""

from dataclasses import dataclass
import unicodedata

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .media import _expire_locked_intent
from .models import (
    CustomUser,
    Image,
    Location,
    MediaUploadIntent,
    Place,
    PublicMediaRendition,
    Request,
    RequestTag,
    SubmissionIdempotency,
    SubmissionLifecycleEvent,
    SubmissionOperation,
    Tag,
)
from .permissions import is_administrator, is_moderator
from .submissions import (
    DuplicateSubmission,
    MediaNotReady,
    SubmissionAuthenticationRequired,
    SubmissionInputError,
    SubmissionNotFound,
    SubmissionOperationError,
    SubmissionStateError,
    _current_submission_snapshot,
    _hash_payload,
    _replayed_record,
    _record_operation,
    _require_ready_media,
    acquire_canonical_name_lock,
    canonical_place_name,
    validate_idempotency_key,
    validate_submission_input,
)


WITHDRAW_OPERATION = SubmissionOperation.WITHDRAW
APPROVE_OPERATION = SubmissionOperation.APPROVE
REJECT_OPERATION = SubmissionOperation.REJECT
MAX_REVIEW_COMMENT_LENGTH = 2_000


class ModerationPermissionDenied(SubmissionOperationError):
    code = "FORBIDDEN"


class ModerationMediaCleanupRequired(SubmissionOperationError):
    code = "MEDIA_CLEANUP_REQUIRED"


@dataclass(frozen=True)
class ModerationResult:
    submission: Request
    replayed: bool
    place: Place | None = None


def _validate_actor_shape(actor):
    if not getattr(actor, "is_authenticated", False) or not getattr(
        actor, "is_active", False
    ):
        raise SubmissionAuthenticationRequired("active authentication is required")


def _locked_actor(actor):
    try:
        locked = CustomUser.objects.select_for_update().get(pk=actor.pk)
    except (CustomUser.DoesNotExist, TypeError, ValueError) as error:
        raise SubmissionAuthenticationRequired(
            "active authentication is required"
        ) from error
    if not locked.is_active:
        raise SubmissionAuthenticationRequired("active authentication is required")
    return locked


def _locked_submission(submission_id, *, owner_id=None):
    queryset = Request.objects.select_for_update(of=("self",)).select_related(
        "address", "category", "owner"
    )
    if owner_id is not None:
        queryset = queryset.filter(owner_id=owner_id)
    try:
        return queryset.get(pk=submission_id)
    except (Request.DoesNotExist, ValidationError, TypeError, ValueError) as error:
        raise SubmissionNotFound("submission not found") from error


def _normalize_comment(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise SubmissionInputError("comment", "comment must be a string")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized:
        return None
    if len(normalized) > MAX_REVIEW_COMMENT_LENGTH:
        raise SubmissionInputError(
            "comment",
            f"comment must not exceed {MAX_REVIEW_COMMENT_LENGTH} characters",
        )
    return normalized


def _request_hash(submission_id, comment=None):
    payload = {"submission_id": str(submission_id)}
    if comment is not None:
        payload["comment"] = comment
    return _hash_payload(payload)


def _result_payload(submission, *, place=None):
    result = {
        "result_version": 1,
        "submission_id": submission.pk,
        "state": str(submission.state),
    }
    if place is not None:
        result["place_id"] = place.pk
    return result


def _result_from_replay(submission, record):
    place_id = record.original_result.get("place_id")
    place = Place.objects.filter(pk=place_id).first() if place_id is not None else None
    return ModerationResult(submission=submission, place=place, replayed=True)


def _lock_submission_media(submission):
    """Lock the aggregate's media in the established parent/intents/images order."""
    intents = list(
        MediaUploadIntent.objects.select_for_update()
        .filter(submission=submission)
        .order_by("slot", "id")
    )
    images = list(
        Image.objects.select_for_update()
        .filter(request=submission, is_managed=True)
        .order_by("position", "pk")
    )
    return intents, images


def _retire_submission_media(submission, *, failure_code):
    """Atomically detach retained media and hand exact objects to cleanup."""
    intents, images = _lock_submission_media(submission)
    now = timezone.now()
    for image in images:
        image.delete()
    for intent in intents:
        if intent.state == MediaUploadIntent.State.DELETED:
            continue
        # A cleanup worker deliberately releases the aggregate lock while it
        # deletes exact object keys. Preserve an existing claim and its retry
        # schedule so a concurrent lifecycle transition cannot invalidate the
        # worker's durable completion after storage I/O has already begun.
        if intent.state == MediaUploadIntent.State.CLEANUP_PENDING:
            continue
        _expire_locked_intent(intent, now, failure_code=failure_code)


def _assert_no_nearby_public_duplicate(submission, canonical):
    """Revalidate the public duplicate invariant while the name lock is held."""
    location = submission.address.location
    # Kept local to the service so approval never applies finalization's
    # owner-scoped proposal check to the moderator's own unrelated drafts.
    from .submissions import _nearby_public_place_names

    for candidate in _nearby_public_place_names(location.x, location.y):
        if candidate is not None and canonical_place_name(candidate) == canonical:
            raise DuplicateSubmission(
                "a matching place already exists within 25 metres"
            )


def _promote_tags_and_materialize_place(submission, managed_images):
    links = list(
        RequestTag.objects.select_for_update(of=("self",))
        .select_related("tag")
        .filter(request=submission)
        .order_by("position", "pk")
    )
    tag_ids = [link.tag_id for link in links]
    tags = {
        tag.pk: tag
        for tag in Tag.objects.select_for_update()
        .filter(pk__in=tag_ids)
        .order_by("pk")
    }
    for link in links:
        tag = tags[link.tag_id]
        if not tag.is_public:
            tag.name = link.display
            tag.is_public = True
            tag.save(update_fields=["name", "is_public"])

    place = Place.objects.create(
        name=submission.name,
        category=submission.category,
        description=submission.description,
        address=submission.address,
        website=submission.website,
    )
    if tag_ids:
        place.tags.add(*tag_ids)

    # Managed attachments remain private, request-bound media.  Only legacy
    # unmanaged image metadata is linked to the public place; its URL was
    # already part of the legacy public contract.  Lock every candidate so a
    # concurrent administrative edit cannot produce a partial publication.
    legacy_images = list(
        Image.objects.select_for_update()
        .filter(request=submission, is_managed=False)
        .order_by("pk")
    )
    if any(image.place_id is not None for image in legacy_images):
        raise SubmissionStateError(
            "submission image metadata is already attached to a place"
        )
    for image in legacy_images:
        image.place = place
        image.save(update_fields=["place"])

    for image in managed_images:
        intent = image.intent
        if (
            intent is None
            or intent.state != MediaUploadIntent.State.ATTACHED
            or image.state != "attached"
            or image.place_id is not None
            or image.storage_key != intent.sealed_object_key
            or not intent.rendition_object_key
            or not intent.rendition_byte_size
            or not intent.rendition_sha256
            or intent.rendition_mime not in {"image/jpeg", "image/png", "image/webp"}
            or PublicMediaRendition.objects.filter(intent=intent).exists()
        ):
            raise MediaNotReady("submission media has no publishable rendition")
        PublicMediaRendition.objects.create(
            place=place,
            source_image=image,
            intent=intent,
            position=image.position,
            mime_type=intent.rendition_mime,
            byte_size=intent.rendition_byte_size,
            width=intent.width,
            height=intent.height,
        )

    # ``Location`` is the bounded legacy map compatibility record.  The v2
    # viewport reads authoritative Address geometry, but older clients still
    # require this denormalized row to become visible in the same transaction.
    Location.objects.create(
        place_id=str(place.pk),
        name=place.name,
        category=place.category_id,
        info=(place.description or "")[:254],
        address=(place.address.addressString or "")[:254],
        tags=",".join(tags[link.tag_id].name for link in links)[:254],
        geom=place.address.location,
    )
    return place


def withdraw_submission(actor, submission_id, idempotency_key):
    """Withdraw an owner-held draft or pending submission atomically."""
    _validate_actor_shape(actor)
    key = validate_idempotency_key(idempotency_key)
    request_hash = _request_hash(submission_id)

    with transaction.atomic():
        submission = _locked_submission(submission_id, owner_id=actor.pk)
        locked_actor = _locked_actor(actor)
        if submission.owner_id != locked_actor.pk:
            raise SubmissionNotFound("submission not found")
        existing = _replayed_record(
            locked_actor, WITHDRAW_OPERATION, key, request_hash, submission
        )
        if existing is not None:
            return _result_from_replay(submission, existing)
        if submission.state not in (Request.State.DRAFT, Request.State.PENDING):
            raise SubmissionStateError(
                "only a draft or pending submission can be withdrawn"
            )

        from_state = submission.state
        _retire_submission_media(submission, failure_code="submission_withdrawn")
        submission.state = Request.State.WITHDRAWN
        submission.approved = False
        submission.reviewed_by = None
        submission.date_approved = None
        submission.approved_comment = None
        submission.save(
            update_fields=[
                "state",
                "approved",
                "reviewed_by",
                "date_approved",
                "approved_comment",
                "date_updated",
            ]
        )
        _record_operation(
            actor=locked_actor,
            operation=WITHDRAW_OPERATION,
            key=key,
            request_hash=request_hash,
            submission=submission,
            result=_result_payload(submission),
            from_state=from_state,
            to_state=Request.State.WITHDRAWN,
        )
        return ModerationResult(submission=submission, replayed=False)


def _review_submission(actor, submission_id, idempotency_key, comment, operation):
    _validate_actor_shape(actor)
    # Reject non-reviewers before resolving a protected target. Role membership
    # is revalidated from the locked database row below before any write.
    if not is_moderator(actor):
        raise ModerationPermissionDenied("moderator permission required")
    key = validate_idempotency_key(idempotency_key)
    normalized_comment = _normalize_comment(comment)
    request_hash = _request_hash(submission_id, normalized_comment)

    with transaction.atomic():
        submission = _locked_submission(submission_id)
        locked_actor = _locked_actor(actor)
        if not is_moderator(locked_actor):
            raise ModerationPermissionDenied("moderator permission required")
        if submission.owner_id == locked_actor.pk:
            raise ModerationPermissionDenied(
                "reviewers cannot review their own submission"
            )

        existing = _replayed_record(
            locked_actor, operation, key, request_hash, submission
        )
        if existing is not None:
            return _result_from_replay(submission, existing)
        if submission.state != Request.State.PENDING:
            raise SubmissionStateError("only a pending submission can be reviewed")

        place = None
        if operation == APPROVE_OPERATION:
            validated = validate_submission_input(
                _current_submission_snapshot(submission)
            )
            canonical = canonical_place_name(validated.name)
            acquire_canonical_name_lock(canonical)
            _assert_no_nearby_public_duplicate(submission, canonical)
            # Revalidate and lock retained media before publishing. Pending media
            # is immutable through the API, but this also fails closed on corrupt
            # historical rows and serializes with autonomous source cleanup.
            managed_images = _require_ready_media(submission.owner, submission)
            place = _promote_tags_and_materialize_place(
                submission, managed_images
            )
            submission.state = Request.State.APPROVED
            submission.approved = True
            submission.date_approved = timezone.now()
        else:
            _retire_submission_media(submission, failure_code="submission_rejected")
            submission.state = Request.State.REJECTED
            submission.approved = False
            submission.date_approved = None

        submission.reviewed_by = locked_actor
        submission.approved_comment = normalized_comment
        submission.save(
            update_fields=[
                "state",
                "approved",
                "reviewed_by",
                "date_approved",
                "approved_comment",
                "date_updated",
            ]
        )
        _record_operation(
            actor=locked_actor,
            operation=operation,
            key=key,
            request_hash=request_hash,
            submission=submission,
            result=_result_payload(submission, place=place),
            from_state=Request.State.PENDING,
            to_state=submission.state,
            comment=normalized_comment,
        )
        return ModerationResult(
            submission=submission, place=place, replayed=False
        )


def approve_submission(actor, submission_id, idempotency_key, comment=None):
    return _review_submission(
        actor, submission_id, idempotency_key, comment, APPROVE_OPERATION
    )


def reject_submission(actor, submission_id, idempotency_key, comment=None):
    return _review_submission(
        actor, submission_id, idempotency_key, comment, REJECT_OPERATION
    )


def hard_delete_submission(actor, submission_id):
    """Perform the exceptional administrator-only, durably audited deletion.

    Object storage is never touched inside the transaction. A submission with
    managed-media evidence must first reach the existing durable cleanup path;
    this prevents deletion from orphaning exact bound objects.
    """
    _validate_actor_shape(actor)
    if not is_administrator(actor):
        raise ModerationPermissionDenied("administrator permission required")

    with transaction.atomic():
        submission = _locked_submission(submission_id)
        locked_actor = _locked_actor(actor)
        if not is_administrator(locked_actor):
            raise ModerationPermissionDenied("administrator permission required")
        if submission.state == Request.State.APPROVED:
            raise SubmissionNotFound("submission not found")
        intents, _managed_images = _lock_submission_media(submission)
        images = list(
            Image.objects.select_for_update()
            .filter(request=submission)
            .order_by("pk")
        )
        if images or any(
            intent.state != MediaUploadIntent.State.DELETED for intent in intents
        ):
            raise ModerationMediaCleanupRequired(
                "submission image metadata and managed media must be cleaned "
                "before hard deletion"
            )

        # A lifecycle event is immutable evidence, and its idempotency record
        # supplies the exact operation identity and original result.  Neither
        # may be cascaded away merely because the mutable submission aggregate
        # is eligible for exceptional deletion.  The protected foreign keys are
        # the database backstop; this explicit refusal keeps the service error
        # stable and makes the policy visible at the authorization boundary.
        if (
            SubmissionLifecycleEvent.objects.select_for_update()
            .filter(submission=submission)
            .exists()
            or SubmissionIdempotency.objects.select_for_update()
            .filter(submission=submission, media_intent__isnull=True)
            .exists()
        ):
            raise SubmissionStateError(
                "submission audit evidence prevents hard deletion"
            )

        # Deleted intents have no remaining object. Their operation evidence is
        # not a lifecycle audit event and can be retired before the exceptional
        # aggregate deletion; the standalone moderation audit remains durable.
        if intents:
            SubmissionIdempotency.objects.filter(media_intent__in=intents).delete()
            MediaUploadIntent.objects.filter(
                pk__in=[intent.pk for intent in intents]
            ).delete()

        from .models import ModerationAudit

        target_id = submission.pk
        ModerationAudit.objects.create(
            actor=locked_actor,
            action=ModerationAudit.Action.HARD_DELETE,
            target_type="request",
            target_id=target_id,
        )
        submission.delete()
        return target_id


def hard_delete_place(actor, place_id):
    """Delete an unreferenced public place without deleting stored objects.

    Approved submission results and places with image metadata are retained.
    The administrator must use an explicit media workflow before deletion; an
    object URL or private storage key is never interpreted as deletion authority.
    """
    _validate_actor_shape(actor)
    if not is_administrator(actor):
        raise ModerationPermissionDenied("administrator permission required")

    with transaction.atomic():
        try:
            place = Place.objects.select_for_update().get(pk=place_id)
        except (Place.DoesNotExist, ValidationError, TypeError, ValueError) as error:
            raise SubmissionNotFound("place not found") from error
        locked_actor = _locked_actor(actor)
        if not is_administrator(locked_actor):
            raise ModerationPermissionDenied("administrator permission required")

        if SubmissionIdempotency.objects.select_for_update().filter(
            operation=APPROVE_OPERATION,
            original_result__place_id=place.pk,
        ).exists():
            raise SubmissionStateError(
                "a place produced by an approved submission cannot be hard deleted"
            )
        # Older approved rows predate idempotency evidence and have no explicit
        # place foreign key.  Conservatively retain an exact name/address match
        # rather than allowing the compatibility endpoint to split that result.
        if Request.objects.select_for_update().filter(
            state=Request.State.APPROVED,
            name=place.name,
            address_id=place.address_id,
        ).exists():
            raise SubmissionStateError(
                "a place produced by an approved submission cannot be hard deleted"
            )

        images = list(
            Image.objects.select_for_update().filter(place=place).order_by("pk")
        )
        if images:
            raise ModerationMediaCleanupRequired(
                "place image metadata must be removed before hard deletion"
            )

        locations = list(
            Location.objects.select_for_update()
            .filter(place_id=str(place.pk))
            .order_by("pk")
        )

        from .models import ModerationAudit

        target_id = place.pk
        ModerationAudit.objects.create(
            actor=locked_actor,
            action=ModerationAudit.Action.HARD_DELETE,
            target_type="place",
            target_id=target_id,
        )
        if locations:
            Location.objects.filter(
                pk__in=[location.pk for location in locations]
            ).delete()
        place.delete()
        return target_id
