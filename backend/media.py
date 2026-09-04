import hashlib
import json
import re
import tempfile
import unicodedata
import uuid
import warnings
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from PIL import Image as PillowImage
from PIL import UnidentifiedImageError

from .media_storage import (
    StorageObjectNotFound,
    StorageOperationError,
    configured_media_storage,
)
from .models import (
    Image,
    CustomUser,
    MediaUploadIntent,
    Request,
    SubmissionIdempotency,
    SubmissionOperation,
)
from .submissions import IdempotencyConflict, SubmissionInputError, validate_idempotency_key


ALLOWED_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
MAX_MEDIA_BYTES = 5_000_000
MAX_IMAGE_EDGE = 10_000
MAX_IMAGE_AREA = 25_000_000
INTENT_LIFETIME = timedelta(hours=24)
PRESIGN_LIFETIME_SECONDS = 600
# The M3 media policy bounds a preview capability to at most 10 minutes.
MEDIA_PREVIEW_LIFETIME_SECONDS = 600
CLEANUP_LEASE = timedelta(minutes=5)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FORMAT_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}

# Pillow raises on highly suspicious decoded pixel counts; the stricter adopted
# 25MP application limit is enforced explicitly below so it has a stable outcome.
PillowImage.MAX_IMAGE_PIXELS = MAX_IMAGE_AREA * 2


class MediaInputError(ValueError):
    def __init__(self, field, message, code="INVALID_MEDIA"):
        super().__init__(message)
        self.field = field
        self.code = code


class MediaStateConflict(ValueError):
    def __init__(self, message, code="MEDIA_STATE_CONFLICT"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class InspectedMedia:
    byte_size: int
    sha256: str
    detected_mime: str
    width: int
    height: int


@dataclass(frozen=True)
class InspectionOutcome:
    media: InspectedMedia = None
    failure_code: str = ""
    sealed: bool = False


@dataclass(frozen=True)
class MediaBinding:
    submission_id: int
    owner_id: int
    storage_identifier: str
    bucket: str
    upload_key: str
    sealed_key: str
    expected_mime: str
    expected_size: int
    expected_sha256: str
    absolute_expires_at: object


@dataclass(frozen=True)
class CleanupClaim:
    intent_id: uuid.UUID
    token: uuid.UUID
    bucket: str
    upload_key: str
    sealed_key: str
    submission_id: int
    owner_id: int


@dataclass(frozen=True)
class UploadCleanupClaim:
    intent_id: uuid.UUID
    bucket: str
    upload_key: str
    sealed_key: str


@dataclass(frozen=True)
class StaleSealCleanupClaim:
    intent_id: uuid.UUID
    token: uuid.UUID
    binding: MediaBinding
    restore_deleted: bool


@dataclass
class MediaCleanupCounts:
    expired: int = 0
    claimed: int = 0
    deleted: int = 0
    failed: int = 0
    skipped: int = 0
    upload_claimed: int = 0
    upload_deleted: int = 0
    redacted: int = 0


def _canonical_hash(payload):
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_actor_submission(actor, submission, *, require_draft=True):
    if not getattr(actor, "is_authenticated", False) or not actor.is_active:
        raise MediaStateConflict("active authentication is required", "UNAUTHENTICATED")
    if submission.owner_id != actor.pk:
        raise MediaStateConflict("submission not found", "NOT_FOUND")
    if require_draft and submission.state != Request.State.DRAFT:
        raise MediaStateConflict("media can only be changed on a draft submission")


def _media_idempotency_key(value):
    try:
        return validate_idempotency_key(value)
    except SubmissionInputError as error:
        raise MediaInputError(error.field, str(error)) from error


def _validate_create_input(mime_type, declared_byte_size, declared_sha256, original_filename, slot):
    if mime_type not in ALLOWED_MIME_TYPES:
        raise MediaInputError("mime_type", "mime_type must be image/jpeg, image/png, or image/webp")
    if isinstance(declared_byte_size, bool) or not isinstance(declared_byte_size, int):
        raise MediaInputError("declared_byte_size", "declared_byte_size must be an integer")
    if not 1 <= declared_byte_size <= MAX_MEDIA_BYTES:
        raise MediaInputError("declared_byte_size", "declared_byte_size must be between 1 and 5000000")
    if not isinstance(declared_sha256, str) or SHA256_RE.fullmatch(declared_sha256) is None:
        raise MediaInputError("declared_sha256", "declared_sha256 must be exactly 64 lowercase hexadecimal characters")
    if slot is not None and (isinstance(slot, bool) or not isinstance(slot, int) or not 0 <= slot < 3):
        raise MediaInputError("slot", "slot must be 0, 1, or 2")
    if original_filename is None:
        return ""
    if not isinstance(original_filename, str):
        raise MediaInputError("original_filename", "original_filename must be a string")
    filename = unicodedata.normalize("NFKC", original_filename)
    if len(filename) > 255 or any(unicodedata.category(char).startswith("C") for char in filename):
        raise MediaInputError("original_filename", "original_filename is invalid")
    return filename


def _configured_binding():
    identifier = getattr(settings, "MEDIA_STORAGE_IDENTIFIER", "")
    bucket = getattr(settings, "MEDIA_STORAGE_BUCKET_NAME", "")
    legacy_bucket = getattr(settings, "AWS_STORAGE_BUCKET_NAME", "")
    if (
        not isinstance(identifier, str)
        or not isinstance(bucket, str)
        or not 1 <= len(identifier) <= 64
        or not 1 <= len(bucket) <= 255
    ):
        raise MediaStateConflict("private media storage is not configured", "MEDIA_STORAGE_UNAVAILABLE")
    if isinstance(legacy_bucket, str) and legacy_bucket and bucket == legacy_bucket:
        raise MediaStateConflict(
            "private media storage must use a separate bucket",
            "MEDIA_STORAGE_UNAVAILABLE",
        )
    return identifier, bucket


def _binding_for_intent(intent):
    return MediaBinding(
        submission_id=intent.submission_id,
        owner_id=intent.owner_id,
        storage_identifier=intent.storage_identifier,
        bucket=intent.storage_bucket,
        upload_key=intent.object_key,
        sealed_key=intent.sealed_object_key,
        expected_mime=intent.expected_mime,
        expected_size=intent.declared_byte_size,
        expected_sha256=intent.declared_sha256,
        absolute_expires_at=intent.absolute_expires_at,
    )


def _redact_expired_upload_result(record, *, now=None):
    result = record.original_result
    if not isinstance(result, dict) or "upload" not in result:
        return False
    encoded_expiry = result.get("upload_expires_at")
    expires_at = parse_datetime(encoded_expiry) if isinstance(encoded_expiry, str) else None
    check_time = now or timezone.now()
    if expires_at is not None and expires_at > check_time:
        return False
    redacted = dict(result)
    redacted.pop("upload", None)
    redacted["upload_authorization_expired"] = True
    record.original_result = redacted
    record.save(update_fields=["original_result"])
    return True


def _idempotency_replay(actor, operation, key, request_hash):
    existing = SubmissionIdempotency.objects.filter(
        actor=actor,
        operation=operation,
        key=key,
    ).first()
    if existing is None:
        return None
    if existing.request_hash != request_hash:
        raise IdempotencyConflict("idempotency key was already used with a different request")
    if operation in {SubmissionOperation.MEDIA_ISSUE, SubmissionOperation.MEDIA_RENEW}:
        _redact_expired_upload_result(existing)
    return existing


def _record_idempotency(*, actor, operation, key, request_hash, submission, intent, result):
    return SubmissionIdempotency.objects.create(
        actor=actor,
        operation=operation,
        key=key,
        request_hash=request_hash,
        submission=submission,
        media_intent=intent,
        original_result=result,
    )


def _locked_media_rows(actor, intent_id):
    try:
        binding = MediaUploadIntent.objects.only("submission_id").get(pk=intent_id)
    except (MediaUploadIntent.DoesNotExist, ValidationError, ValueError, TypeError) as error:
        raise MediaStateConflict("media intent not found", "NOT_FOUND") from error
    try:
        submission = Request.objects.select_for_update().get(pk=binding.submission_id)
    except Request.DoesNotExist as error:
        raise MediaStateConflict("media intent not found", "NOT_FOUND") from error
    try:
        locked_actor = CustomUser.objects.select_for_update().get(pk=actor.pk)
    except (CustomUser.DoesNotExist, TypeError, ValueError) as error:
        raise MediaStateConflict("active authentication is required", "UNAUTHENTICATED") from error
    try:
        intent = MediaUploadIntent.objects.select_for_update().get(pk=intent_id)
    except (MediaUploadIntent.DoesNotExist, ValidationError, ValueError, TypeError) as error:
        raise MediaStateConflict("media intent not found", "NOT_FOUND") from error
    return submission, intent, locked_actor


def _locked_parent_and_intent(actor, intent_id, *, require_draft=True):
    submission, intent, locked_actor = _locked_media_rows(actor, intent_id)
    _validate_actor_submission(locked_actor, submission, require_draft=require_draft)
    if intent.submission_id != submission.pk or intent.owner_id != locked_actor.pk:
        raise MediaStateConflict("media intent not found", "NOT_FOUND")
    return submission, intent


def _result_for_intent(intent):
    return {
        "intent_id": str(intent.pk),
        "state": intent.state,
        "failure_code": intent.failure_code,
    }


def _upload_replay(result):
    upload = dict(result["upload"])
    upload["expires_at"] = parse_datetime(result["upload_expires_at"])
    return upload


def create_upload_intent(
    actor,
    submission_id,
    idempotency_key,
    *,
    mime_type,
    declared_byte_size,
    declared_sha256,
    original_filename="",
    slot=None,
):
    key = _media_idempotency_key(idempotency_key)
    filename = _validate_create_input(
        mime_type, declared_byte_size, declared_sha256, original_filename, slot
    )
    storage_identifier, storage_bucket = _configured_binding()
    payload = {
        "declared_byte_size": declared_byte_size,
        "declared_sha256": declared_sha256,
        "mime_type": mime_type,
        "original_filename": filename,
        "slot": slot,
        "submission_id": str(submission_id),
    }
    request_hash = _canonical_hash(payload)

    with transaction.atomic():
        try:
            submission = Request.objects.select_for_update().get(pk=submission_id)
        except (Request.DoesNotExist, ValueError, TypeError) as error:
            raise MediaStateConflict("submission not found", "NOT_FOUND") from error
        try:
            locked_actor = CustomUser.objects.select_for_update().get(pk=actor.pk)
        except (CustomUser.DoesNotExist, TypeError, ValueError) as error:
            raise MediaStateConflict("active authentication is required", "UNAUTHENTICATED") from error
        _validate_actor_submission(locked_actor, submission, require_draft=False)
        existing = _idempotency_replay(actor, SubmissionOperation.MEDIA_CREATE, key, request_hash)
        if existing is not None:
            return existing.media_intent, True
        if submission.state != Request.State.DRAFT:
            raise MediaStateConflict("media can only be changed on a draft submission")

        reserving = list(
            MediaUploadIntent.objects.select_for_update()
            .filter(submission=submission, state__in=MediaUploadIntent.RESERVING_STATES)
            .order_by("slot", "created_at")
        )
        attachments = list(
            Image.objects.select_for_update()
            .filter(request=submission, is_managed=True, state="attached")
            .order_by("position", "pk")
        )
        if len(attachments) + len(reserving) >= 3:
            raise MediaStateConflict("submission already has three media slots", "MEDIA_LIMIT_REACHED")
        used_slots = {image.position for image in attachments} | {intent.slot for intent in reserving}
        allocated_slot = slot
        if allocated_slot is None:
            allocated_slot = next(candidate for candidate in range(3) if candidate not in used_slots)
        elif allocated_slot in used_slots:
            raise MediaStateConflict("media slot is already reserved", "MEDIA_SLOT_CONFLICT")

        intent_uuid = uuid.uuid4()
        intent_created_at = timezone.now()
        intent = MediaUploadIntent.objects.create(
            id=intent_uuid,
            submission=submission,
            owner=locked_actor,
            state=MediaUploadIntent.State.CREATED,
            slot=allocated_slot,
            storage_identifier=storage_identifier,
            storage_bucket=storage_bucket,
            object_key=f"submission-media/{submission.pk}/{uuid.uuid4().hex}",
            sealed_object_key=(
                f"submission-media-sealed/{submission.pk}/{uuid.uuid4().hex}"
            ),
            expected_mime=mime_type,
            declared_byte_size=declared_byte_size,
            declared_sha256=declared_sha256,
            original_filename=filename,
            created_at=intent_created_at,
            absolute_expires_at=intent_created_at + INTENT_LIFETIME,
        )
        _record_idempotency(
            actor=locked_actor,
            operation=SubmissionOperation.MEDIA_CREATE,
            key=key,
            request_hash=request_hash,
            submission=submission,
            intent=intent,
            result=_result_for_intent(intent),
        )
        return intent, False


def _expire_locked_intent(intent, now, failure_code="intent_expired"):
    intent.state = MediaUploadIntent.State.CLEANUP_PENDING
    if not intent.failure_code:
        intent.failure_code = failure_code
        intent.failure_at = now
    intent.cleanup_claim_token = None
    intent.cleanup_claimed_at = None
    intent.cleanup_lease_until = None
    intent.save(
        update_fields=[
            "state", "failure_code", "failure_at", "cleanup_claim_token",
            "cleanup_claimed_at", "cleanup_lease_until", "updated_at",
        ]
    )


def issue_upload(actor, intent_id, idempotency_key, *, renew=False, storage=None):
    key = _media_idempotency_key(idempotency_key)
    operation = SubmissionOperation.MEDIA_RENEW if renew else SubmissionOperation.MEDIA_ISSUE
    payload = {"intent_id": str(intent_id)}
    request_hash = _canonical_hash(payload)
    expired_code = ""
    issuable_states = {MediaUploadIntent.State.CREATED}
    if renew:
        issuable_states.add(MediaUploadIntent.State.ISSUED)
    # Client construction can consult environment/configuration, so keep it out
    # of the locked transaction for new issuance. A known replay needs no client;
    # this preserves a stable expired-replay outcome during storage outages.
    known_replay = SubmissionIdempotency.objects.filter(
        actor=actor, operation=operation, key=key
    ).exists()
    signer = storage
    if signer is None and not known_replay:
        signer = configured_media_storage()
    with transaction.atomic():
        submission, intent = _locked_parent_and_intent(actor, intent_id, require_draft=False)
        existing = _idempotency_replay(actor, operation, key, request_hash)
        if existing is not None:
            if "upload" in existing.original_result:
                return intent, _upload_replay(existing.original_result), True
            expired_code = (
                "UPLOAD_AUTHORIZATION_EXPIRED"
                if existing.original_result.get("upload_authorization_expired")
                else "MEDIA_INTENT_EXPIRED"
            )
        elif submission.state != Request.State.DRAFT:
            raise MediaStateConflict("media can only be changed on a draft submission")
        elif intent.cleanup_claim_token or intent.state not in issuable_states:
            raise MediaStateConflict("media intent cannot issue an upload in its current state")
        else:
            issued_at = timezone.now()
            if issued_at >= intent.absolute_expires_at:
                _expire_locked_intent(intent, issued_at)
                _record_idempotency(
                    actor=actor, operation=operation, key=key, request_hash=request_hash,
                    submission=submission, intent=intent, result=_result_for_intent(intent),
                )
                expired_code = "MEDIA_INTENT_EXPIRED"
            else:
                expires_in = min(
                    PRESIGN_LIFETIME_SECONDS,
                    int((intent.absolute_expires_at - issued_at).total_seconds()),
                )
                if expires_in < 1:
                    raise MediaStateConflict(
                        "media intent is too close to expiry", "MEDIA_INTENT_EXPIRED"
                    )
                if signer is None:
                    raise StorageOperationError("private media storage is unavailable")
                upload = signer.issue_upload(
                    bucket=intent.storage_bucket,
                    key=intent.object_key,
                    mime_type=intent.expected_mime,
                    maximum_size=intent.declared_byte_size,
                    expires_in=expires_in,
                )
                if (
                    not isinstance(upload, dict)
                    or not isinstance(upload.get("url"), str)
                    or not isinstance(upload.get("fields"), dict)
                ):
                    raise StorageOperationError("upload authorization response was invalid")
                presign_expires_at = issued_at + timedelta(seconds=expires_in)
                if presign_expires_at > intent.absolute_expires_at:
                    presign_expires_at = intent.absolute_expires_at
                intent.state = MediaUploadIntent.State.ISSUED
                intent.issued_at = issued_at
                intent.presign_expires_at = presign_expires_at
                intent.save(update_fields=[
                    "state", "issued_at", "presign_expires_at", "updated_at",
                ])
                authorization = {
                    "url": upload["url"],
                    "fields": upload["fields"],
                    "expires_at": presign_expires_at,
                }
                result = {
                    **_result_for_intent(intent),
                    "upload": {"url": upload["url"], "fields": upload["fields"]},
                    "upload_expires_at": presign_expires_at.isoformat(),
                }
                _record_idempotency(
                    actor=actor, operation=operation, key=key, request_hash=request_hash,
                    submission=submission, intent=intent, result=result,
                )
    if expired_code:
        if expired_code == "MEDIA_INTENT_EXPIRED":
            raise MediaStateConflict("media intent has expired", expired_code)
        raise MediaStateConflict(
            "upload authorization has expired and is no longer available",
            expired_code,
        )
    return intent, authorization, False


def issue_media_preview(actor, attachment_id, *, storage=None):
    """Fail-closed, exact-object GET authorization for one attached managed image.

    Only the verified sealed object bound to an ``is_managed`` ``attached``
    image can be previewed: that state is reached exclusively through
    ``attach_verified_media``, so it already carries the invariants
    ``managed_image_complete_metadata`` enforces (owner, sealed storage key,
    verified dimensions/digest). The owner may preview their own draft or
    pending submission's media; a moderator/administrator may preview a
    pending submission's media regardless of owner. Every other outcome -
    missing attachment, wrong state, unmoderated cross-owner access, a
    moderator viewing someone else's draft - collapses to the same NOT_FOUND
    so existence cannot be distinguished from denial.
    """
    if not getattr(actor, "is_authenticated", False) or not getattr(actor, "is_active", False):
        raise MediaStateConflict("active authentication is required", "UNAUTHENTICATED")
    try:
        image = (
            Image.objects.select_related("request")
            .get(pk=attachment_id, is_managed=True, state="attached")
        )
    except (Image.DoesNotExist, ValidationError, ValueError, TypeError, OverflowError) as error:
        raise MediaStateConflict("media attachment not found", "NOT_FOUND") from error

    submission = image.request
    is_reviewer = bool(
        getattr(actor, "is_staff", False) or getattr(actor, "is_superuser", False)
    )
    if submission is not None and submission.owner_id == actor.pk:
        allowed = submission.state in (Request.State.DRAFT, Request.State.PENDING)
    elif submission is not None and is_reviewer:
        allowed = submission.state == Request.State.PENDING
    else:
        allowed = False
    if not allowed:
        raise MediaStateConflict("media attachment not found", "NOT_FOUND")

    signer = storage or configured_media_storage()
    expires_in = MEDIA_PREVIEW_LIFETIME_SECONDS
    url = signer.issue_preview(
        bucket=image.storage_bucket, key=image.storage_key, expires_in=expires_in
    )
    if not isinstance(url, str) or not url:
        raise StorageOperationError("preview authorization response was invalid")
    return image, url, timezone.now() + timedelta(seconds=expires_in)


def _signature_mime(header):
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return ""


def inspect_uploaded_object(
    storage,
    *,
    bucket,
    key,
    sealed_key,
    expected_size,
    expected_sha256,
    expected_mime,
):
    digest = hashlib.sha256()
    byte_size = 0
    header = b""
    temporary = tempfile.SpooledTemporaryFile(max_size=MAX_MEDIA_BYTES + 1)
    try:
        try:
            opened = storage.open_object(bucket=bucket, key=key)
            with opened as body:
                while True:
                    chunk = body.read(64 * 1024)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        return InspectionOutcome(failure_code="object_read_invalid")
                    byte_size += len(chunk)
                    if len(header) < 16:
                        header += chunk[: 16 - len(header)]
                    digest.update(chunk)
                    if byte_size > MAX_MEDIA_BYTES:
                        return InspectionOutcome(failure_code="byte_size_mismatch")
                    temporary.write(chunk)
        except StorageObjectNotFound:
            return InspectionOutcome(failure_code="object_not_found")
        except StorageOperationError:
            return InspectionOutcome(failure_code="object_read_failed")

        server_sha256 = digest.hexdigest()
        if byte_size != expected_size:
            return InspectionOutcome(failure_code="byte_size_mismatch")
        if server_sha256 != expected_sha256:
            return InspectionOutcome(failure_code="sha256_mismatch")
        signature_mime = _signature_mime(header)
        if not signature_mime:
            return InspectionOutcome(failure_code="unsupported_signature")
        if signature_mime != expected_mime:
            return InspectionOutcome(failure_code="mime_mismatch")

        temporary.seek(0)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", PillowImage.DecompressionBombWarning)
                with PillowImage.open(temporary) as decoded:
                    detected_mime = FORMAT_MIME.get(decoded.format, "")
                    width, height = decoded.size
                    if (
                        width <= 0
                        or height <= 0
                        or width > MAX_IMAGE_EDGE
                        or height > MAX_IMAGE_EDGE
                    ):
                        return InspectionOutcome(
                            failure_code="image_dimensions_exceeded"
                        )
                    if width * height > MAX_IMAGE_AREA:
                        return InspectionOutcome(failure_code="image_area_exceeded")
                    decoded.verify()
                temporary.seek(0)
                with PillowImage.open(temporary) as decoded:
                    decoded.load()
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            SyntaxError,
            MemoryError,
            OverflowError,
            PillowImage.DecompressionBombWarning,
            PillowImage.DecompressionBombError,
        ):
            return InspectionOutcome(failure_code="image_decode_failed")
        if detected_mime != signature_mime or detected_mime != expected_mime:
            return InspectionOutcome(failure_code="mime_mismatch")
        temporary.seek(0)
        sealed = False
        try:
            storage.seal_object(
                bucket=bucket,
                key=sealed_key,
                body=temporary,
                content_type=detected_mime,
                content_length=byte_size,
            )
            sealed = True
            if storage.object_size(bucket=bucket, key=sealed_key) != byte_size:
                return InspectionOutcome(
                    failure_code="sealed_object_size_mismatch", sealed=True
                )
        except (StorageObjectNotFound, StorageOperationError):
            return InspectionOutcome(
                failure_code="object_seal_failed", sealed=sealed
            )
        return InspectionOutcome(
            media=InspectedMedia(
                byte_size=byte_size,
                sha256=server_sha256,
                detected_mime=detected_mime,
                width=width,
                height=height,
            ),
            sealed=True,
        )
    finally:
        temporary.close()


def _cleanup_upload_source_after_verification(intent_id, binding, storage):
    deleted = False
    error_code = ""
    try:
        storage.delete_object(bucket=binding.bucket, key=binding.upload_key)
        deleted = storage.object_is_absent(
            bucket=binding.bucket, key=binding.upload_key
        )
        if not deleted:
            error_code = "upload_object_still_present"
    except StorageOperationError:
        error_code = "upload_cleanup_failed"

    with transaction.atomic():
        _submission, intent = _system_locked_parent_and_intent(intent_id)
        if (
            intent is None
            or _binding_for_intent(intent) != binding
            or intent.state
            not in {MediaUploadIntent.State.VERIFIED, MediaUploadIntent.State.ATTACHED}
            or not intent.upload_cleanup_pending
        ):
            return False
        finished_at = timezone.now()
        intent.upload_cleanup_attempts += 1
        intent.upload_cleanup_last_attempt_at = finished_at
        if deleted:
            if (
                intent.presign_expires_at
                and finished_at < intent.presign_expires_at
            ):
                # The still-valid client authorization can recreate the source.
                # Keep one final upload-key-only cleanup due at authorization expiry.
                intent.upload_cleanup_pending = True
                intent.upload_cleanup_next_attempt_at = intent.presign_expires_at
            else:
                intent.upload_cleanup_pending = False
                intent.upload_cleanup_next_attempt_at = None
            intent.upload_cleanup_error_code = ""
        else:
            intent.upload_cleanup_next_attempt_at = _upload_cleanup_retry_at(
                intent, finished_at
            )
            intent.upload_cleanup_error_code = error_code
        intent.save(
            update_fields=[
                "upload_cleanup_pending",
                "upload_cleanup_attempts",
                "upload_cleanup_last_attempt_at",
                "upload_cleanup_next_attempt_at",
                "upload_cleanup_error_code",
                "updated_at",
            ]
        )
    return deleted


def _attached_image_references_binding(binding):
    return Image.objects.select_for_update().filter(
        is_managed=True,
        state="attached",
        storage_identifier=binding.storage_identifier,
        storage_bucket=binding.bucket,
        storage_key=binding.sealed_key,
    ).exists()


def _intent_has_exact_object_binding(intent, binding):
    current = _binding_for_intent(intent)
    return (
        current.submission_id,
        current.owner_id,
        current.storage_identifier,
        current.bucket,
        current.upload_key,
        current.sealed_key,
    ) == (
        binding.submission_id,
        binding.owner_id,
        binding.storage_identifier,
        binding.bucket,
        binding.upload_key,
        binding.sealed_key,
    )


def _claim_stale_seal_cleanup_locked(intent, binding):
    if (
        not _intent_has_exact_object_binding(intent, binding)
        or intent.state
        in {MediaUploadIntent.State.VERIFIED, MediaUploadIntent.State.ATTACHED}
        or _attached_image_references_binding(binding)
    ):
        return None

    # Attach takes the same parent/intent locks and can only create an Image from
    # VERIFIED. Moving to CLEANUP_PENDING before releasing those locks fences any
    # later attach; replacing an older cleanup token also fences its stale finish.
    now = timezone.now()
    token = uuid.uuid4()
    restore_deleted = intent.state == MediaUploadIntent.State.DELETED
    intent.state = MediaUploadIntent.State.CLEANUP_PENDING
    intent.cleanup_claim_token = token
    intent.cleanup_claimed_at = now
    intent.cleanup_lease_until = now + CLEANUP_LEASE
    intent.cleanup_attempts += 1
    intent.cleanup_last_attempt_at = now
    intent.cleanup_next_attempt_at = None
    intent.save(
        update_fields=[
            "state",
            "cleanup_claim_token",
            "cleanup_claimed_at",
            "cleanup_lease_until",
            "cleanup_attempts",
            "cleanup_last_attempt_at",
            "cleanup_next_attempt_at",
            "updated_at",
        ]
    )
    return StaleSealCleanupClaim(
        intent_id=intent.pk,
        token=token,
        binding=binding,
        restore_deleted=restore_deleted,
    )


def _run_stale_seal_cleanup_claim(claim, storage):
    deleted = False
    error_code = ""
    try:
        storage.delete_object(
            bucket=claim.binding.bucket, key=claim.binding.sealed_key
        )
        deleted = storage.object_is_absent(
            bucket=claim.binding.bucket, key=claim.binding.sealed_key
        )
        if not deleted:
            error_code = "sealed_object_still_present"
    except StorageOperationError:
        error_code = "sealed_cleanup_failed"

    with transaction.atomic():
        _submission, intent = _system_locked_parent_and_intent(claim.intent_id)
        if intent is None:
            return False
        if (
            intent.state != MediaUploadIntent.State.CLEANUP_PENDING
            or intent.cleanup_claim_token != claim.token
            or not _intent_has_exact_object_binding(intent, claim.binding)
        ):
            return False

        attached = _attached_image_references_binding(claim.binding)
        finished_at = timezone.now()
        if deleted and not attached:
            if claim.restore_deleted:
                intent.state = MediaUploadIntent.State.DELETED
                intent.deleted_at = intent.deleted_at or finished_at
            intent.cleanup_error_code = ""
            intent.cleanup_next_attempt_at = None
        else:
            intent.state = MediaUploadIntent.State.CLEANUP_PENDING
            intent.cleanup_error_code = (
                "attachment_present" if attached else error_code
            )
            intent.cleanup_next_attempt_at = _cleanup_retry_at(intent, finished_at)
        intent.cleanup_claim_token = None
        intent.cleanup_claimed_at = None
        intent.cleanup_lease_until = None
        intent.save(
            update_fields=[
                "state",
                "deleted_at",
                "cleanup_error_code",
                "cleanup_next_attempt_at",
                "cleanup_claim_token",
                "cleanup_claimed_at",
                "cleanup_lease_until",
                "updated_at",
            ]
        )
        return deleted and not attached


def verify_upload(actor, intent_id, idempotency_key, *, storage=None):
    key = _media_idempotency_key(idempotency_key)
    operation = SubmissionOperation.MEDIA_VERIFY
    request_hash = _canonical_hash({"intent_id": str(intent_id)})

    with transaction.atomic():
        submission, intent, locked_actor = _locked_media_rows(actor, intent_id)
        _validate_actor_submission(locked_actor, submission, require_draft=False)
        if intent.submission_id != submission.pk or intent.owner_id != locked_actor.pk:
            raise MediaStateConflict("media intent not found", "NOT_FOUND")
        existing = _idempotency_replay(actor, operation, key, request_hash)
        if existing is not None:
            return intent, True
        if submission.state != Request.State.DRAFT:
            raise MediaStateConflict("media can only be changed on a draft submission")
        if intent.state in {MediaUploadIntent.State.VERIFIED, MediaUploadIntent.State.ATTACHED}:
            _record_idempotency(
                actor=actor, operation=operation, key=key, request_hash=request_hash,
                submission=submission, intent=intent, result=_result_for_intent(intent),
            )
            return intent, False
        if intent.state in {
            MediaUploadIntent.State.FAILED,
            MediaUploadIntent.State.EXPIRED,
            MediaUploadIntent.State.CLEANUP_PENDING,
            MediaUploadIntent.State.DELETED,
        }:
            _record_idempotency(
                actor=actor, operation=operation, key=key, request_hash=request_hash,
                submission=submission, intent=intent, result=_result_for_intent(intent),
            )
            return intent, False
        if intent.cleanup_claim_token or intent.state != MediaUploadIntent.State.ISSUED:
            raise MediaStateConflict("media intent cannot be verified in its current state")
        now = timezone.now()
        if now >= intent.absolute_expires_at:
            _expire_locked_intent(intent, now)
            _record_idempotency(
                actor=actor, operation=operation, key=key, request_hash=request_hash,
                submission=submission, intent=intent, result=_result_for_intent(intent),
            )
            return intent, False
        binding = _binding_for_intent(intent)

    storage = storage or configured_media_storage()
    outcome = inspect_uploaded_object(
        storage,
        bucket=binding.bucket,
        key=binding.upload_key,
        sealed_key=binding.sealed_key,
        expected_mime=binding.expected_mime,
        expected_size=binding.expected_size,
        expected_sha256=binding.expected_sha256,
    )

    verified = False
    replayed = False
    stale_error = None
    stale_seal_claim = None
    with transaction.atomic():
        submission, intent, locked_actor = _locked_media_rows(actor, intent_id)
        existing = _idempotency_replay(actor, operation, key, request_hash)
        if existing is not None:
            replayed = True
            if outcome.sealed:
                stale_seal_claim = _claim_stale_seal_cleanup_locked(intent, binding)
        elif intent.cleanup_claim_token or intent.state != MediaUploadIntent.State.ISSUED:
            stale_error = MediaStateConflict(
                "media intent changed while the object was inspected"
            )
            if outcome.sealed:
                stale_seal_claim = _claim_stale_seal_cleanup_locked(intent, binding)
        else:
            current_binding = _binding_for_intent(intent)
            now = timezone.now()
            intent.verification_attempts += 1
            intent.last_verification_at = now
            update_fields = [
                "verification_attempts", "last_verification_at", "updated_at"
            ]
            authorization_changed = (
                not locked_actor.is_active
                or submission.owner_id != locked_actor.pk
                or intent.owner_id != locked_actor.pk
                or submission.state != Request.State.DRAFT
                or current_binding != binding
            )
            if authorization_changed:
                outcome = InspectionOutcome(
                    failure_code="authorization_changed", sealed=outcome.sealed
                )
            elif now >= intent.absolute_expires_at:
                outcome = InspectionOutcome(
                    failure_code="intent_expired", sealed=outcome.sealed
                )
            if outcome.failure_code:
                intent.state = MediaUploadIntent.State.CLEANUP_PENDING
                if not intent.failure_code:
                    intent.failure_code = outcome.failure_code
                    intent.failure_at = now
                update_fields += ["state", "failure_code", "failure_at"]
            else:
                media = outcome.media
                intent.state = MediaUploadIntent.State.VERIFIED
                intent.server_byte_size = media.byte_size
                intent.server_sha256 = media.sha256
                intent.detected_mime = media.detected_mime
                intent.width = media.width
                intent.height = media.height
                intent.verified_at = now
                intent.upload_cleanup_pending = True
                update_fields += [
                    "state", "server_byte_size", "server_sha256", "detected_mime",
                    "width", "height", "verified_at", "upload_cleanup_pending",
                ]
                verified = True
            intent.save(update_fields=update_fields)
            if outcome.sealed and not verified:
                stale_seal_claim = _claim_stale_seal_cleanup_locked(intent, binding)
            _record_idempotency(
                actor=actor, operation=operation, key=key, request_hash=request_hash,
                submission=submission, intent=intent, result=_result_for_intent(intent),
            )

    if stale_seal_claim is not None:
        _run_stale_seal_cleanup_claim(stale_seal_claim, storage)
        intent.refresh_from_db()
    if stale_error is not None:
        raise stale_error
    if replayed:
        return intent, True

    if verified:
        _cleanup_upload_source_after_verification(intent_id, binding, storage)
        intent.refresh_from_db()
    return intent, False


def attach_verified_media(actor, intent_id, idempotency_key):
    key = _media_idempotency_key(idempotency_key)
    operation = SubmissionOperation.MEDIA_ATTACH
    request_hash = _canonical_hash({"intent_id": str(intent_id)})
    expired = False
    replayed = False
    image = None
    with transaction.atomic():
        submission, intent = _locked_parent_and_intent(actor, intent_id, require_draft=False)
        existing = _idempotency_replay(actor, operation, key, request_hash)
        if existing is not None:
            replayed = True
            image_id = existing.original_result.get("image_id")
            if image_id is not None:
                image = Image.objects.get(pk=image_id)
            else:
                expired = True
        elif submission.state != Request.State.DRAFT:
            raise MediaStateConflict("media can only be changed on a draft submission")
        elif intent.state == MediaUploadIntent.State.ATTACHED:
            image = Image.objects.select_for_update().filter(
                intent=intent, is_managed=True
            ).first()
            if image is None:
                raise MediaStateConflict("attached media evidence is incomplete")
            _record_idempotency(
                actor=actor, operation=operation, key=key, request_hash=request_hash,
                submission=submission, intent=intent,
                result={**_result_for_intent(intent), "image_id": image.pk},
            )
        elif intent.state == MediaUploadIntent.State.VERIFIED and (
            timezone.now() >= intent.absolute_expires_at
        ):
            _expire_locked_intent(intent, timezone.now())
            _record_idempotency(
                actor=actor, operation=operation, key=key, request_hash=request_hash,
                submission=submission, intent=intent, result=_result_for_intent(intent),
            )
            expired = True
        else:
            if intent.cleanup_claim_token or intent.state != MediaUploadIntent.State.VERIFIED:
                raise MediaStateConflict("only verified media can be attached")
            if not all([
                intent.server_byte_size, intent.server_sha256, intent.detected_mime,
                intent.width, intent.height, intent.verified_at,
            ]):
                raise MediaStateConflict("verified media evidence is incomplete")
            list(
                MediaUploadIntent.objects.select_for_update()
                .filter(submission=submission)
                .order_by("slot", "id")
            )
            attachments = list(
                Image.objects.select_for_update()
                .filter(request=submission, is_managed=True, state="attached")
                .order_by("position", "pk")
            )
            if len(attachments) >= 3:
                raise MediaStateConflict(
                    "submission already has three attached images", "MEDIA_LIMIT_REACHED"
                )
            if any(attachment.position == intent.slot for attachment in attachments):
                raise MediaStateConflict(
                    "media position is already attached", "MEDIA_SLOT_CONFLICT"
                )
            if any(attachment.sha256 == intent.server_sha256 for attachment in attachments):
                raise MediaStateConflict(
                    "the verified image is already attached", "MEDIA_DIGEST_CONFLICT"
                )
            now = timezone.now()
            try:
                image = Image.objects.create(
                    set_id="", name="", url="", metadata=None,
                    request=submission, place=None, is_managed=True, intent=intent,
                    owner=actor, position=intent.slot, state="attached",
                    storage_identifier=intent.storage_identifier,
                    storage_bucket=intent.storage_bucket,
                    storage_key=intent.sealed_object_key,
                    byte_size=intent.server_byte_size,
                    detected_mime=intent.detected_mime,
                    width=intent.width, height=intent.height,
                    sha256=intent.server_sha256, attached_at=now,
                )
            except IntegrityError as error:
                raise MediaStateConflict(
                    "media attachment conflicts with an existing image"
                ) from error
            intent.state = MediaUploadIntent.State.ATTACHED
            intent.attached_at = now
            intent.save(update_fields=["state", "attached_at", "updated_at"])
            _record_idempotency(
                actor=actor, operation=operation, key=key, request_hash=request_hash,
                submission=submission, intent=intent,
                result={**_result_for_intent(intent), "image_id": image.pk},
            )
    if expired:
        raise MediaStateConflict("media intent has expired", "MEDIA_INTENT_EXPIRED")
    return image, replayed


def remove_attached_media(actor, intent_id, idempotency_key):
    """Draft-only idempotent removal of one attached managed image.

    Locks submission, then intent, then attachment (the same canonical order
    finalize/attach use), deletes the retained attachment, hands the exact
    bound object off to ``cleanup_pending`` in the same transaction, and frees
    the slot. Mirrors ``_expire_locked_intent``'s handoff shape exactly, since
    an owner-removed attachment and an autonomously-expired one both retire
    the intent's storage objects through the identical cleanup path.
    """
    key = _media_idempotency_key(idempotency_key)
    operation = SubmissionOperation.MEDIA_REMOVE
    request_hash = _canonical_hash({"intent_id": str(intent_id)})
    with transaction.atomic():
        submission, intent = _locked_parent_and_intent(actor, intent_id, require_draft=False)
        existing = _idempotency_replay(actor, operation, key, request_hash)
        if existing is not None:
            return intent, True
        if submission.state != Request.State.DRAFT:
            raise MediaStateConflict("media can only be changed on a draft submission")
        if intent.state != MediaUploadIntent.State.ATTACHED:
            raise MediaStateConflict("only attached media can be removed")
        image = Image.objects.select_for_update().filter(
            intent=intent, is_managed=True, state="attached"
        ).first()
        if image is None:
            raise MediaStateConflict("attached media evidence is incomplete")

        now = timezone.now()
        image.delete()
        _expire_locked_intent(intent, now, failure_code="media_removed")
        _record_idempotency(
            actor=actor, operation=operation, key=key, request_hash=request_hash,
            submission=submission, intent=intent, result=_result_for_intent(intent),
        )
        return intent, False


def expire_upload_intent(actor, intent_id, idempotency_key):
    key = _media_idempotency_key(idempotency_key)
    operation = SubmissionOperation.MEDIA_EXPIRE
    request_hash = _canonical_hash({"intent_id": str(intent_id)})
    with transaction.atomic():
        submission, intent = _locked_parent_and_intent(actor, intent_id, require_draft=False)
        existing = _idempotency_replay(actor, operation, key, request_hash)
        if existing is not None:
            return intent, True
        if intent.state in {MediaUploadIntent.State.CLEANUP_PENDING, MediaUploadIntent.State.DELETED}:
            _record_idempotency(
                actor=actor, operation=operation, key=key, request_hash=request_hash,
                submission=submission, intent=intent, result=_result_for_intent(intent),
            )
            return intent, False
        if intent.state not in {
            MediaUploadIntent.State.CREATED,
            MediaUploadIntent.State.ISSUED,
            MediaUploadIntent.State.VERIFIED,
        }:
            raise MediaStateConflict("media intent cannot be expired in its current state")
        now = timezone.now()
        if now < intent.absolute_expires_at:
            raise MediaStateConflict("media intent has not reached its absolute expiry")
        _expire_locked_intent(intent, now)
        _record_idempotency(
            actor=actor, operation=operation, key=key, request_hash=request_hash,
            submission=submission, intent=intent, result=_result_for_intent(intent),
        )
        return intent, False


def _delete_bound_objects(storage, *, bucket, upload_key, sealed_key):
    all_absent = True
    error_code = ""
    for object_key in (upload_key, sealed_key):
        try:
            storage.delete_object(bucket=bucket, key=object_key)
            if not storage.object_is_absent(bucket=bucket, key=object_key):
                all_absent = False
                error_code = error_code or "object_still_present"
        except StorageOperationError:
            all_absent = False
            error_code = error_code or "storage_cleanup_failed"
    return all_absent, error_code


def cleanup_media_object(actor, intent_id, idempotency_key, *, storage=None):
    key = _media_idempotency_key(idempotency_key)
    operation = SubmissionOperation.MEDIA_CLEANUP
    request_hash = _canonical_hash({"intent_id": str(intent_id)})
    now = timezone.now()
    claim_token = uuid.uuid4()
    with transaction.atomic():
        submission, intent = _locked_parent_and_intent(actor, intent_id, require_draft=False)
        existing = _idempotency_replay(actor, operation, key, request_hash)
        if existing is not None:
            return intent, bool(existing.original_result.get("deleted")), True
        if intent.state == MediaUploadIntent.State.DELETED:
            result = {**_result_for_intent(intent), "deleted": True}
            _record_idempotency(
                actor=actor, operation=operation, key=key, request_hash=request_hash,
                submission=submission, intent=intent, result=result,
            )
            return intent, True, False
        if intent.state not in {
            MediaUploadIntent.State.CLEANUP_PENDING,
            MediaUploadIntent.State.FAILED,
            MediaUploadIntent.State.EXPIRED,
        } or Image.objects.select_for_update().filter(intent=intent, is_managed=True, state="attached").exists():
            raise MediaStateConflict("media intent is not eligible for cleanup")
        if intent.cleanup_lease_until and intent.cleanup_lease_until > now:
            raise MediaStateConflict("media cleanup is already claimed", "MEDIA_CLEANUP_CLAIMED")
        if intent.cleanup_next_attempt_at and intent.cleanup_next_attempt_at > now:
            raise MediaStateConflict("media cleanup retry is not due", "MEDIA_CLEANUP_BACKOFF")
        intent.state = MediaUploadIntent.State.CLEANUP_PENDING
        intent.cleanup_claim_token = claim_token
        intent.cleanup_claimed_at = now
        intent.cleanup_lease_until = now + CLEANUP_LEASE
        intent.cleanup_attempts += 1
        intent.cleanup_last_attempt_at = now
        intent.save(update_fields=[
            "state", "cleanup_claim_token", "cleanup_claimed_at", "cleanup_lease_until",
            "cleanup_attempts", "cleanup_last_attempt_at", "updated_at",
        ])
        binding = (
            intent.storage_bucket,
            intent.object_key,
            intent.sealed_object_key,
            intent.submission_id,
            intent.owner_id,
        )

    storage = storage or configured_media_storage()
    deleted, error_code = _delete_bound_objects(
        storage,
        bucket=binding[0],
        upload_key=binding[1],
        sealed_key=binding[2],
    )

    with transaction.atomic():
        submission, intent = _locked_parent_and_intent(actor, intent_id, require_draft=False)
        if (
            intent.cleanup_claim_token != claim_token
            or (
                intent.storage_bucket,
                intent.object_key,
                intent.sealed_object_key,
                intent.submission_id,
                intent.owner_id,
            ) != binding
            or Image.objects.select_for_update().filter(intent=intent, is_managed=True, state="attached").exists()
        ):
            raise MediaStateConflict("media cleanup claim changed before completion")
        finished_at = timezone.now()
        if deleted:
            intent.state = MediaUploadIntent.State.DELETED
            intent.deleted_at = finished_at
            intent.cleanup_error_code = ""
            intent.cleanup_next_attempt_at = None
            intent.upload_cleanup_pending = False
            intent.upload_cleanup_next_attempt_at = None
            intent.upload_cleanup_error_code = ""
        else:
            intent.state = MediaUploadIntent.State.CLEANUP_PENDING
            intent.cleanup_error_code = error_code
            delay = min(3600, 30 * (2 ** min(intent.cleanup_attempts - 1, 7)))
            intent.cleanup_next_attempt_at = finished_at + timedelta(seconds=delay)
        intent.cleanup_claim_token = None
        intent.cleanup_claimed_at = None
        intent.cleanup_lease_until = None
        intent.save(update_fields=[
            "state", "deleted_at", "cleanup_error_code", "cleanup_next_attempt_at",
            "cleanup_claim_token", "cleanup_claimed_at", "cleanup_lease_until", "updated_at",
            "upload_cleanup_pending", "upload_cleanup_next_attempt_at",
            "upload_cleanup_error_code",
        ])
        result = {**_result_for_intent(intent), "deleted": deleted}
        _record_idempotency(
            actor=actor, operation=operation, key=key, request_hash=request_hash,
            submission=submission, intent=intent, result=result,
        )
        return intent, deleted, False


def _system_locked_parent_and_intent(intent_id, *, skip_locked=False):
    """Lock system cleanup rows in the same parent-first order as user operations."""
    binding = (
        MediaUploadIntent.objects.filter(pk=intent_id)
        .values("submission_id")
        .first()
    )
    if binding is None:
        return None, None
    submission = (
        Request.objects.select_for_update(skip_locked=skip_locked)
        .filter(pk=binding["submission_id"])
        .first()
    )
    if submission is None:
        return None, None
    intent = (
        MediaUploadIntent.objects.select_for_update(skip_locked=skip_locked)
        .filter(pk=intent_id, submission=submission)
        .first()
    )
    if intent is None:
        return submission, None
    return submission, intent


def _expire_due_system_intent(intent_id, now_override=None):
    with transaction.atomic():
        _submission, intent = _system_locked_parent_and_intent(intent_id, skip_locked=True)
        if intent is None:
            return False
        now = now_override or timezone.now()
        if (
            intent.state not in {
                MediaUploadIntent.State.CREATED,
                MediaUploadIntent.State.ISSUED,
                MediaUploadIntent.State.VERIFIED,
            }
            or intent.absolute_expires_at > now
        ):
            return False
        _expire_locked_intent(intent, now)
        return True


def _claim_due_system_cleanup(intent_id, now_override=None):
    with transaction.atomic():
        _submission, intent = _system_locked_parent_and_intent(intent_id, skip_locked=True)
        if intent is None or intent.state != MediaUploadIntent.State.CLEANUP_PENDING:
            return None
        now = now_override or timezone.now()
        if intent.cleanup_next_attempt_at and intent.cleanup_next_attempt_at > now:
            return None
        if intent.cleanup_lease_until and intent.cleanup_lease_until > now:
            return None
        if Image.objects.select_for_update().filter(
            intent=intent, is_managed=True, state="attached"
        ).exists():
            return None

        token = uuid.uuid4()
        intent.cleanup_claim_token = token
        intent.cleanup_claimed_at = now
        intent.cleanup_lease_until = now + CLEANUP_LEASE
        intent.cleanup_attempts += 1
        intent.cleanup_last_attempt_at = now
        intent.save(update_fields=[
            "cleanup_claim_token", "cleanup_claimed_at", "cleanup_lease_until",
            "cleanup_attempts", "cleanup_last_attempt_at", "updated_at",
        ])
        return CleanupClaim(
            intent_id=intent.pk,
            token=token,
            bucket=intent.storage_bucket,
            upload_key=intent.object_key,
            sealed_key=intent.sealed_object_key,
            submission_id=intent.submission_id,
            owner_id=intent.owner_id,
        )


def _cleanup_retry_at(intent, finished_at):
    delay = min(3600, 30 * (2 ** min(intent.cleanup_attempts - 1, 7)))
    return finished_at + timedelta(seconds=delay)


def _upload_cleanup_retry_at(intent, finished_at):
    delay = min(3600, 30 * (2 ** min(intent.upload_cleanup_attempts - 1, 7)))
    return finished_at + timedelta(seconds=delay)


def _finish_system_cleanup(claim, *, deleted, error_code):
    with transaction.atomic():
        _submission, intent = _system_locked_parent_and_intent(claim.intent_id)
        if intent is None:
            return "skipped"
        current_binding = (
            intent.storage_bucket,
            intent.object_key,
            intent.sealed_object_key,
            intent.submission_id,
            intent.owner_id,
        )
        claimed_binding = (
            claim.bucket,
            claim.upload_key,
            claim.sealed_key,
            claim.submission_id,
            claim.owner_id,
        )
        if (
            intent.state != MediaUploadIntent.State.CLEANUP_PENDING
            or intent.cleanup_claim_token != claim.token
            or current_binding != claimed_binding
        ):
            return "skipped"

        attached = Image.objects.select_for_update().filter(
            intent=intent, is_managed=True, state="attached"
        ).exists()
        finished_at = timezone.now()
        if deleted and not attached:
            intent.state = MediaUploadIntent.State.DELETED
            intent.deleted_at = finished_at
            intent.cleanup_error_code = ""
            intent.cleanup_next_attempt_at = None
            intent.upload_cleanup_pending = False
            intent.upload_cleanup_next_attempt_at = None
            intent.upload_cleanup_error_code = ""
            outcome = "deleted"
        else:
            intent.cleanup_error_code = "attachment_present" if attached else error_code
            intent.cleanup_next_attempt_at = _cleanup_retry_at(intent, finished_at)
            outcome = "failed"
        intent.cleanup_claim_token = None
        intent.cleanup_claimed_at = None
        intent.cleanup_lease_until = None
        intent.save(update_fields=[
            "state", "deleted_at", "cleanup_error_code", "cleanup_next_attempt_at",
            "cleanup_claim_token", "cleanup_claimed_at", "cleanup_lease_until", "updated_at",
            "upload_cleanup_pending", "upload_cleanup_next_attempt_at",
            "upload_cleanup_error_code",
        ])
        return outcome


def _run_system_cleanup_claim(claim, storage):
    deleted, error_code = _delete_bound_objects(
        storage,
        bucket=claim.bucket,
        upload_key=claim.upload_key,
        sealed_key=claim.sealed_key,
    )
    return _finish_system_cleanup(claim, deleted=deleted, error_code=error_code)


def _claim_due_upload_cleanup(intent_id, now_override=None):
    with transaction.atomic():
        _submission, intent = _system_locked_parent_and_intent(intent_id, skip_locked=True)
        if (
            intent is None
            or intent.state
            not in {MediaUploadIntent.State.VERIFIED, MediaUploadIntent.State.ATTACHED}
            or not intent.upload_cleanup_pending
        ):
            return None
        now = now_override or timezone.now()
        if (
            intent.upload_cleanup_next_attempt_at
            and intent.upload_cleanup_next_attempt_at > now
        ):
            return None
        intent.upload_cleanup_attempts += 1
        intent.upload_cleanup_last_attempt_at = now
        # This timestamp is also a lightweight crash-recoverable lease. Duplicate
        # S3 deletes are harmless, but healthy workers should not duplicate work.
        intent.upload_cleanup_next_attempt_at = now + CLEANUP_LEASE
        intent.save(
            update_fields=[
                "upload_cleanup_attempts",
                "upload_cleanup_last_attempt_at",
                "upload_cleanup_next_attempt_at",
                "updated_at",
            ]
        )
        return UploadCleanupClaim(
            intent_id=intent.pk,
            bucket=intent.storage_bucket,
            upload_key=intent.object_key,
            sealed_key=intent.sealed_object_key,
        )


def _run_upload_cleanup_claim(claim, storage):
    deleted = False
    error_code = ""
    try:
        storage.delete_object(bucket=claim.bucket, key=claim.upload_key)
        deleted = storage.object_is_absent(
            bucket=claim.bucket, key=claim.upload_key
        )
        if not deleted:
            error_code = "upload_object_still_present"
    except StorageOperationError:
        error_code = "upload_cleanup_failed"

    with transaction.atomic():
        _submission, intent = _system_locked_parent_and_intent(claim.intent_id)
        if (
            intent is None
            or intent.storage_bucket != claim.bucket
            or intent.object_key != claim.upload_key
            or intent.sealed_object_key != claim.sealed_key
            or intent.state
            not in {MediaUploadIntent.State.VERIFIED, MediaUploadIntent.State.ATTACHED}
            or not intent.upload_cleanup_pending
        ):
            return "skipped"
        finished_at = timezone.now()
        if deleted:
            if (
                intent.presign_expires_at
                and finished_at < intent.presign_expires_at
            ):
                intent.upload_cleanup_pending = True
                intent.upload_cleanup_next_attempt_at = intent.presign_expires_at
                outcome = "scheduled"
            else:
                intent.upload_cleanup_pending = False
                intent.upload_cleanup_next_attempt_at = None
                outcome = "deleted"
            intent.upload_cleanup_error_code = ""
        else:
            intent.upload_cleanup_next_attempt_at = _upload_cleanup_retry_at(
                intent, finished_at
            )
            intent.upload_cleanup_error_code = error_code
            outcome = "failed"
        intent.save(
            update_fields=[
                "upload_cleanup_pending",
                "upload_cleanup_next_attempt_at",
                "upload_cleanup_error_code",
                "updated_at",
            ]
        )
        return outcome


def _purge_expired_upload_authorizations(*, batch_size, now_override=None):
    purged = 0
    records = SubmissionIdempotency.objects.filter(
        operation__in=[
            SubmissionOperation.MEDIA_ISSUE,
            SubmissionOperation.MEDIA_RENEW,
        ],
        original_result__has_key="upload",
    ).order_by("created_at", "pk")
    for record in records.iterator(chunk_size=min(batch_size, 100)):
        check_time = now_override or timezone.now()
        if _redact_expired_upload_result(record, now=check_time):
            purged += 1
            if purged >= batch_size:
                break
    return purged


def process_media_cleanup(*, batch_size=100, storage=None, now=None):
    """Expire and clean known intents; the intent PK/state is the durable job identity."""
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or not 1 <= batch_size <= 1000
    ):
        raise ValueError("batch_size must be between 1 and 1000")
    batch_now = now or timezone.now()
    counts = MediaCleanupCounts()
    counts.redacted = _purge_expired_upload_authorizations(
        batch_size=batch_size, now_override=now
    )

    expiry_ids = list(
        MediaUploadIntent.objects.filter(
            state__in=[
                MediaUploadIntent.State.CREATED,
                MediaUploadIntent.State.ISSUED,
                MediaUploadIntent.State.VERIFIED,
            ],
            absolute_expires_at__lte=batch_now,
        )
        .order_by("absolute_expires_at", "id")
        .values_list("id", flat=True)[:batch_size]
    )
    for intent_id in expiry_ids:
        if _expire_due_system_intent(intent_id, now_override=now):
            counts.expired += 1
        else:
            counts.skipped += 1

    due_cleanup = (
        Q(cleanup_next_attempt_at__isnull=True)
        | Q(cleanup_next_attempt_at__lte=batch_now)
    ) & (
        Q(cleanup_lease_until__isnull=True) | Q(cleanup_lease_until__lte=batch_now)
    )
    cleanup_ids = list(
        MediaUploadIntent.objects.filter(
            due_cleanup,
            state=MediaUploadIntent.State.CLEANUP_PENDING,
        )
        .order_by("cleanup_next_attempt_at", "absolute_expires_at", "id")
        .values_list("id", flat=True)[:batch_size]
    )
    cleanup_storage = storage
    for intent_id in cleanup_ids:
        claim = _claim_due_system_cleanup(intent_id, now_override=now)
        if claim is None:
            counts.skipped += 1
            continue
        counts.claimed += 1
        if cleanup_storage is None:
            cleanup_storage = configured_media_storage()
        outcome = _run_system_cleanup_claim(claim, cleanup_storage)
        if outcome == "deleted":
            counts.deleted += 1
        elif outcome == "failed":
            counts.failed += 1
        else:
            counts.skipped += 1

    upload_cleanup_ids = list(
        MediaUploadIntent.objects.filter(
            upload_cleanup_pending=True,
            state__in=[
                MediaUploadIntent.State.VERIFIED,
                MediaUploadIntent.State.ATTACHED,
            ],
        )
        .filter(
            Q(upload_cleanup_next_attempt_at__isnull=True)
            | Q(upload_cleanup_next_attempt_at__lte=batch_now)
        )
        .order_by("upload_cleanup_next_attempt_at", "id")
        .values_list("id", flat=True)[:batch_size]
    )
    for intent_id in upload_cleanup_ids:
        claim = _claim_due_upload_cleanup(intent_id, now_override=now)
        if claim is None:
            counts.skipped += 1
            continue
        counts.upload_claimed += 1
        if cleanup_storage is None:
            cleanup_storage = configured_media_storage()
        outcome = _run_upload_cleanup_claim(claim, cleanup_storage)
        if outcome == "deleted":
            counts.upload_deleted += 1
        elif outcome == "failed":
            counts.failed += 1
        else:
            counts.skipped += 1
    return counts
