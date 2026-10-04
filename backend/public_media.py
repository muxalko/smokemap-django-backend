import hashlib
import uuid
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone

from .media import MAX_MEDIA_BYTES, _expire_locked_intent
from .media_storage import (
    StorageObjectNotFound,
    StorageOperationError,
    configured_media_storage,
)
from .models import (
    CustomUser,
    Image,
    MediaUploadIntent,
    ModerationAudit,
    PublicMediaRendition,
    Request,
    SubmissionIdempotency,
    SubmissionOperation,
)
from .permissions import is_administrator
from .moderation import ModerationPermissionDenied
from .submissions import (
    IdempotencyConflict,
    SubmissionAuthenticationRequired,
    SubmissionNotFound,
    SubmissionStateError,
    _hash_payload,
    validate_idempotency_key,
)


class PublicMediaUnavailable(Exception):
    pass


@dataclass(frozen=True)
class PublicMediaPayload:
    body: bytes
    mime_type: str
    byte_size: int


@dataclass(frozen=True)
class PublicMediaRevocationResult:
    rendition: PublicMediaRendition
    replayed: bool


def published_media_prefetch():
    """Return the public-only, stable-order place media prefetch."""
    return Prefetch(
        "public_media",
        queryset=PublicMediaRendition.objects.filter(
            state=PublicMediaRendition.State.PUBLISHED
        ).order_by("position", "pk"),
        to_attr="_published_media",
    )


def _public_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise SubmissionNotFound("public media not found") from error


def _published_binding(public_id):
    rendition = (
        PublicMediaRendition.objects.select_related("intent__submission")
        .filter(public_id=public_id, state=PublicMediaRendition.State.PUBLISHED)
        .first()
    )
    if rendition is None:
        raise SubmissionNotFound("public media not found")
    intent = rendition.intent
    return rendition, intent.storage_bucket, intent.rendition_object_key


def _read_exact_rendition(storage, *, bucket, key, expected_size, expected_sha256):
    digest = hashlib.sha256()
    chunks = []
    byte_size = 0
    try:
        opened = storage.open_object(bucket=bucket, key=key)
        with opened as body:
            while True:
                chunk = body.read(64 * 1024)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise PublicMediaUnavailable("public media could not be read")
                byte_size += len(chunk)
                if byte_size > MAX_MEDIA_BYTES:
                    raise PublicMediaUnavailable("public media could not be read")
                digest.update(chunk)
                chunks.append(chunk)
    except (StorageObjectNotFound, StorageOperationError) as error:
        raise PublicMediaUnavailable("public media could not be read") from error
    if byte_size != expected_size or digest.hexdigest() != expected_sha256:
        raise PublicMediaUnavailable("public media could not be read")
    return b"".join(chunks)


def retrieve_public_media(public_id, *, storage=None):
    """Read private rendition bytes, then linearize current public authorization."""
    identifier = _public_uuid(public_id)
    preliminary, bucket, key = _published_binding(identifier)
    intent = preliminary.intent
    if (
        not key
        or not intent.rendition_byte_size
        or not intent.rendition_sha256
        or preliminary.byte_size != intent.rendition_byte_size
        or preliminary.mime_type != intent.rendition_mime
    ):
        raise SubmissionNotFound("public media not found")

    storage = storage or configured_media_storage()
    body = _read_exact_rendition(
        storage,
        bucket=bucket,
        key=key,
        expected_size=intent.rendition_byte_size,
        expected_sha256=intent.rendition_sha256,
    )

    with transaction.atomic():
        submission = Request.objects.select_for_update().filter(
            pk=intent.submission_id
        ).first()
        locked_intent = MediaUploadIntent.objects.select_for_update().filter(
            pk=intent.pk,
            submission=submission,
        ).first()
        image = Image.objects.select_for_update().filter(
            intent=locked_intent,
            is_managed=True,
            state="attached",
        ).first()
        rendition = PublicMediaRendition.objects.select_for_update().filter(
            public_id=identifier,
            intent=locked_intent,
        ).first()
        approval_linked = bool(
            rendition
            and SubmissionIdempotency.objects.select_for_update().filter(
                submission=submission,
                operation=SubmissionOperation.APPROVE,
                original_result__place_id=rendition.place_id,
            ).exists()
        )
        if (
            submission is None
            or locked_intent is None
            or image is None
            or rendition is None
            or submission.state != Request.State.APPROVED
            or not submission.approved
            or locked_intent.state != MediaUploadIntent.State.ATTACHED
            or rendition.state != PublicMediaRendition.State.PUBLISHED
            or rendition.place_id is None
            or not approval_linked
            or rendition.source_image_id != image.pk
            or image.request_id != submission.pk
            or image.storage_key != locked_intent.sealed_object_key
            or locked_intent.storage_bucket != bucket
            or locked_intent.rendition_object_key != key
            or rendition.byte_size != locked_intent.rendition_byte_size
            or rendition.mime_type != locked_intent.rendition_mime
        ):
            raise SubmissionNotFound("public media not found")
        return PublicMediaPayload(
            body=body,
            mime_type=rendition.mime_type,
            byte_size=rendition.byte_size,
        )


def _locked_active_administrator(actor):
    if not getattr(actor, "is_authenticated", False) or not getattr(
        actor, "is_active", False
    ):
        raise SubmissionAuthenticationRequired("active authentication is required")
    if not is_administrator(actor):
        raise ModerationPermissionDenied("administrator permission required")
    try:
        locked = CustomUser.objects.select_for_update().get(pk=actor.pk)
    except (CustomUser.DoesNotExist, TypeError, ValueError) as error:
        raise SubmissionAuthenticationRequired(
            "active authentication is required"
        ) from error
    if not locked.is_active or not is_administrator(locked):
        raise ModerationPermissionDenied("administrator permission required")
    return locked


def revoke_public_media(actor, public_id, idempotency_key):
    if not getattr(actor, "is_authenticated", False) or not getattr(
        actor, "is_active", False
    ):
        raise SubmissionAuthenticationRequired("active authentication is required")
    if not is_administrator(actor):
        raise ModerationPermissionDenied("administrator permission required")
    identifier = _public_uuid(public_id)
    key = validate_idempotency_key(idempotency_key)
    request_hash = _hash_payload({"public_id": str(identifier)})
    binding = PublicMediaRendition.objects.filter(public_id=identifier).values(
        "intent_id", "intent__submission_id"
    ).first()
    if binding is None:
        raise SubmissionNotFound("public media not found")

    with transaction.atomic():
        submission = Request.objects.select_for_update().filter(
            pk=binding["intent__submission_id"]
        ).first()
        locked_actor = _locked_active_administrator(actor)
        intent = MediaUploadIntent.objects.select_for_update().filter(
            pk=binding["intent_id"], submission=submission
        ).first()
        image = Image.objects.select_for_update().filter(
            intent=intent, is_managed=True, state="attached"
        ).first()
        rendition = PublicMediaRendition.objects.select_for_update().filter(
            public_id=identifier, intent=intent
        ).first()
        if submission is None or intent is None or rendition is None:
            raise SubmissionNotFound("public media not found")

        existing = SubmissionIdempotency.objects.filter(
            actor=locked_actor,
            operation=SubmissionOperation.MEDIA_REVOKE,
            key=key,
        ).first()
        if existing is not None:
            if (
                existing.request_hash != request_hash
                or existing.submission_id != submission.pk
                or existing.media_intent_id != intent.pk
            ):
                raise IdempotencyConflict(
                    "idempotency key was already used with a different request"
                )
            return PublicMediaRevocationResult(rendition=rendition, replayed=True)

        if (
            submission.state != Request.State.APPROVED
            or not submission.approved
            or intent.state != MediaUploadIntent.State.ATTACHED
            or image is None
            or rendition.state != PublicMediaRendition.State.PUBLISHED
            or rendition.source_image_id != image.pk
            or rendition.place_id is None
            or not SubmissionIdempotency.objects.select_for_update().filter(
                submission=submission,
                operation=SubmissionOperation.APPROVE,
                original_result__place_id=rendition.place_id,
            ).exists()
        ):
            raise SubmissionStateError("public media is not currently published")

        now = timezone.now()
        ModerationAudit.objects.create(
            actor=locked_actor,
            action=ModerationAudit.Action.REVOKE_MEDIA,
            target_type="public_media_rendition",
            target_id=rendition.pk,
        )
        rendition.state = PublicMediaRendition.State.REVOKED
        rendition.revoked_at = now
        rendition.save(update_fields=["state", "revoked_at"])
        image.delete()
        _expire_locked_intent(intent, now, failure_code="public_media_revoked")
        SubmissionIdempotency.objects.create(
            actor=locked_actor,
            operation=SubmissionOperation.MEDIA_REVOKE,
            key=key,
            request_hash=request_hash,
            submission=submission,
            media_intent=intent,
            original_result={
                "result_version": 1,
                "public_id": str(rendition.public_id),
                "state": rendition.state,
            },
        )
        return PublicMediaRevocationResult(rendition=rendition, replayed=False)
