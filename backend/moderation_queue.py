from dataclasses import dataclass

from django.core import signing
from django.db.models import Q
from django.utils.dateparse import parse_datetime

from .models import Request


DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50
LEGACY_PAGE_SIZE = 50
CURSOR_SALT = "backend.moderation-queue.v4"
MAX_CURSOR_LENGTH = 1024


class ModerationQueueInputError(ValueError):
    code = "INVALID_PAGINATION"


@dataclass(frozen=True)
class ModerationQueuePage:
    items: list
    has_next_page: bool
    next_cursor: str | None


def pending_moderation_queryset():
    """The single pending-only source for current and compatibility queues."""
    return Request.objects.filter(state=Request.State.PENDING).select_related(
        "category", "address"
    )


def encode_moderation_cursor(submission):
    return signing.dumps(
        {
            "v": 1,
            "created": submission.date_created.isoformat(),
            "id": submission.pk,
        },
        salt=CURSOR_SALT,
        compress=True,
    )


def decode_moderation_cursor(cursor):
    if not cursor or len(cursor) > MAX_CURSOR_LENGTH:
        raise ModerationQueueInputError("Invalid moderation queue cursor")
    try:
        payload = signing.loads(cursor, salt=CURSOR_SALT)
        if not isinstance(payload, dict) or set(payload) != {"v", "created", "id"}:
            raise ValueError
        if payload["v"] != 1:
            raise ValueError
        submission_id = payload["id"]
        if isinstance(submission_id, bool) or not isinstance(submission_id, int):
            raise ValueError
        if submission_id <= 0:
            raise ValueError
        created_at = parse_datetime(payload["created"])
        if created_at is None or created_at.tzinfo is None:
            raise ValueError
    except (signing.BadSignature, TypeError, ValueError):
        raise ModerationQueueInputError("Invalid moderation queue cursor") from None
    return created_at, submission_id


def moderation_queue_page(*, first=DEFAULT_PAGE_SIZE, after=None, queryset=None):
    if first is None:
        first = DEFAULT_PAGE_SIZE
    if (
        isinstance(first, bool)
        or not isinstance(first, int)
        or not 1 <= first <= MAX_PAGE_SIZE
    ):
        raise ModerationQueueInputError(
            f"Moderation queue first must be between 1 and {MAX_PAGE_SIZE}"
        )

    queue = queryset if queryset is not None else pending_moderation_queryset()
    if after is not None:
        created_at, submission_id = decode_moderation_cursor(after)
        queue = queue.filter(
            Q(date_created__gt=created_at)
            | Q(date_created=created_at, pk__gt=submission_id)
        )

    rows = list(queue.order_by("date_created", "pk")[: first + 1])
    has_next_page = len(rows) > first
    items = rows[:first]
    next_cursor = (
        encode_moderation_cursor(items[-1]) if has_next_page and items else None
    )
    return ModerationQueuePage(
        items=items,
        has_next_page=has_next_page,
        next_cursor=next_cursor,
    )
