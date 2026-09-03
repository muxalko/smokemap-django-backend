from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from .models import (
    Image,
    MediaUploadIntent,
    Request,
    SubmissionIdempotency,
    SubmissionLifecycleEvent,
    SubmissionOperation,
)


DRAFT_INACTIVITY_LIMIT = timedelta(days=30)
SUBMISSION_EXPIRED_FAILURE_CODE = "submission_expired"

# These durable records are evidence of an owner submission/media operation.
# Autonomous cleanup writes no idempotency record, so its retries cannot keep a
# draft alive. Finalization cannot coexist with a draft after its transaction.
RELEVANT_OWNER_OPERATIONS = (
    SubmissionOperation.CREATE,
    SubmissionOperation.EDIT,
    SubmissionOperation.MEDIA_CREATE,
    SubmissionOperation.MEDIA_ISSUE,
    SubmissionOperation.MEDIA_RENEW,
    SubmissionOperation.MEDIA_VERIFY,
    SubmissionOperation.MEDIA_ATTACH,
    SubmissionOperation.MEDIA_EXPIRE,
    SubmissionOperation.MEDIA_CLEANUP,
)

# ``failure_at`` is deliberately absent: both owner requests and autonomous
# expiry can stamp it. Owner-caused failures remain activity through the
# operation's SubmissionIdempotency record above.
RELEVANT_INTENT_TIMESTAMPS = (
    "created_at",
    "issued_at",
    "last_verification_at",
    "verified_at",
    "attached_at",
)


@dataclass
class SubmissionExpiryCounts:
    expired: int = 0
    skipped: int = 0


def _validate_batch_size(batch_size):
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or not 1 <= batch_size <= 1000
    ):
        raise ValueError("batch_size must be between 1 and 1000")


def latest_relevant_submission_activity(submission, *, intents=None, images=None):
    """Return authoritative activity while ignoring autonomous cleanup churn.

    Callers performing a transition pass already-locked intent and image rows.
    The parent Request lock serializes all supported owner operations, so the
    idempotency query cannot race a supported write for this submission.
    """
    if intents is None:
        intents = submission.media_upload_intents.all()
    if images is None:
        images = Image.objects.filter(request=submission, is_managed=True)

    timestamps = [submission.date_updated]
    for intent in intents:
        timestamps.extend(
            getattr(intent, field) for field in RELEVANT_INTENT_TIMESTAMPS
        )
    timestamps.extend(
        image.attached_at for image in images if image.attached_at is not None
    )
    operation_at = (
        SubmissionIdempotency.objects.filter(
            submission=submission,
            operation__in=RELEVANT_OWNER_OPERATIONS,
        )
        .order_by("-created_at", "-pk")
        .values_list("created_at", flat=True)
        .first()
    )
    if operation_at is not None:
        timestamps.append(operation_at)
    return max(timestamp for timestamp in timestamps if timestamp is not None)


def _due_candidate_ids(*, cutoff, batch_size):
    """Find genuinely due rows before applying the bound.

    Each recent-activity exclusion is evaluated before LIMIT. Consequently an
    old Request row with newer media evidence cannot repeatedly occupy a batch
    slot and starve a truly inactive draft behind it.
    """
    recent_intent_activity = Q(created_at__gt=cutoff)
    for field in RELEVANT_INTENT_TIMESTAMPS[1:]:
        recent_intent_activity |= Q(**{f"{field}__gt": cutoff})

    recent_intent = MediaUploadIntent.objects.filter(
        submission_id=OuterRef("pk")
    ).filter(recent_intent_activity)
    recent_owner_operation = SubmissionIdempotency.objects.filter(
        submission_id=OuterRef("pk"),
        operation__in=RELEVANT_OWNER_OPERATIONS,
        created_at__gt=cutoff,
    )
    recent_attachment = Image.objects.filter(
        request_id=OuterRef("pk"),
        is_managed=True,
        attached_at__gt=cutoff,
    )

    return list(
        Request.objects.filter(
            state=Request.State.DRAFT,
            date_updated__lte=cutoff,
        )
        .annotate(
            has_recent_intent=Exists(recent_intent),
            has_recent_owner_operation=Exists(recent_owner_operation),
            has_recent_attachment=Exists(recent_attachment),
        )
        .filter(
            has_recent_intent=False,
            has_recent_owner_operation=False,
            has_recent_attachment=False,
        )
        .order_by("date_updated", "pk")
        .values_list("pk", flat=True)
        [:batch_size]
    )


def _handoff_media_for_expired_submission(submission, *, intents, images, now):
    # Image is the availability link. The intent retains the exact immutable
    # bindings and verified metadata as the non-secret tombstone.
    if images:
        Image.objects.filter(pk__in=[image.pk for image in images]).delete()

    for intent in intents:
        if intent.state == MediaUploadIntent.State.DELETED:
            continue

        update_fields = []
        if not intent.failure_code:
            intent.failure_code = SUBMISSION_EXPIRED_FAILURE_CODE
            intent.failure_at = now
            update_fields.extend(["failure_code", "failure_at"])

        if intent.state != MediaUploadIntent.State.CLEANUP_PENDING:
            intent.state = MediaUploadIntent.State.CLEANUP_PENDING
            intent.cleanup_claim_token = None
            intent.cleanup_claimed_at = None
            intent.cleanup_lease_until = None
            intent.cleanup_next_attempt_at = None
            intent.cleanup_error_code = ""
            intent.upload_cleanup_pending = False
            intent.upload_cleanup_next_attempt_at = None
            intent.upload_cleanup_error_code = ""
            update_fields.extend(
                [
                    "state",
                    "cleanup_claim_token",
                    "cleanup_claimed_at",
                    "cleanup_lease_until",
                    "cleanup_next_attempt_at",
                    "cleanup_error_code",
                    "upload_cleanup_pending",
                    "upload_cleanup_next_attempt_at",
                    "upload_cleanup_error_code",
                ]
            )

        # Existing cleanup work keeps its claim and backoff because its SLA began
        # earlier. A missing failure reason may still be completed without
        # disturbing that lease.
        if update_fields:
            intent.save(update_fields=[*dict.fromkeys(update_fields), "updated_at"])


def _expire_submission_if_due(submission_id, *, cutoff, now):
    with transaction.atomic():
        submission = (
            Request.objects.select_for_update(skip_locked=True)
            .filter(pk=submission_id)
            .first()
        )
        if submission is None or submission.state != Request.State.DRAFT:
            return False

        # Preserve the aggregate lock order used by edit/finalize/media:
        # parent Request, then intents, then managed Image attachments.
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
        if latest_relevant_submission_activity(
            submission, intents=intents, images=images
        ) > cutoff:
            return False

        _handoff_media_for_expired_submission(
            submission, intents=intents, images=images, now=now
        )
        submission.state = Request.State.EXPIRED
        submission.save(update_fields=["state", "date_updated"])
        SubmissionLifecycleEvent.objects.create(
            submission=submission,
            actor=None,
            system_actor=SubmissionLifecycleEvent.DRAFT_EXPIRY_SYSTEM_ACTOR,
            operation=SubmissionOperation.EXPIRE,
            from_state=Request.State.DRAFT,
            to_state=Request.State.EXPIRED,
            outcome=SubmissionLifecycleEvent.Outcome.SUCCEEDED,
            idempotency=None,
        )
        return True


def process_submission_expiry(*, batch_size=100, now=None):
    """Expire a bounded batch of drafts inactive for at least exactly 30 days."""
    _validate_batch_size(batch_size)
    batch_now = now or timezone.now()
    cutoff = batch_now - DRAFT_INACTIVITY_LIMIT
    counts = SubmissionExpiryCounts()
    for submission_id in _due_candidate_ids(cutoff=cutoff, batch_size=batch_size):
        if _expire_submission_if_due(submission_id, cutoff=cutoff, now=batch_now):
            counts.expired += 1
        else:
            counts.skipped += 1
    return counts
