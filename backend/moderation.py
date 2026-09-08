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
    MediaUploadIntent,
    Place,
    Request,
    RequestTag,
    SubmissionIdempotency,
    SubmissionOperation,
    Tag,
)
from .permissions import is_administrator, is_moderator
from .submissions import (
    DuplicateSubmission,
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


def _promote_tags_and_materialize_place(submission):
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
            _require_ready_media(submission.owner, submission)
            place = _promote_tags_and_materialize_place(submission)
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
        intents, images = _lock_submission_media(submission)
        if images or any(
            intent.state != MediaUploadIntent.State.DELETED for intent in intents
        ):
            raise ModerationMediaCleanupRequired(
                "submission media must be cleaned before hard deletion"
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
