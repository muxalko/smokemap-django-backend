import uuid
import threading
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from .models import (
    Address,
    Category,
    Image,
    MediaUploadIntent,
    Request,
    SubmissionIdempotency,
    SubmissionLifecycleEvent,
    SubmissionOperation,
)
from .submission_expiry import (
    DRAFT_INACTIVITY_LIMIT,
    process_submission_expiry,
)
from . import media as media_services
from . import submission_expiry as expiry_services
from . import submissions as submission_services
from .media import (
    cleanup_media_object,
    create_upload_intent,
    expire_upload_intent,
)
from .submissions import edit_submission, finalize_submission


User = get_user_model()


class SubmissionExpiryTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            email="draft-expiry@smokemap.test", password="test"
        )
        self.category, _created = Category.objects.get_or_create(
            slug="outdoors",
            defaults={"name": "Outdoors", "description": "Outside."},
        )

    def create_submission(self, *, state=Request.State.DRAFT, activity_at=None, name=None):
        address = Address.objects.create(
            addressString="Expiry test", location=Point(34.8, 32.1, srid=4326)
        )
        submission = Request.objects.create(
            name=name or f"Expiry test {uuid.uuid4().hex}",
            category=self.category,
            address=address,
            owner=self.owner,
            state=state,
            approved=(state == Request.State.APPROVED),
        )
        if activity_at is not None:
            Request.objects.filter(pk=submission.pk).update(
                date_created=activity_at,
                date_updated=activity_at,
            )
            submission.refresh_from_db()
        return submission

    def create_intent(self, submission, state, *, slot=0, activity_at=None):
        activity_at = activity_at or timezone.now()
        values = {
            "submission": submission,
            "owner": self.owner,
            "state": state,
            "slot": slot,
            "storage_identifier": "private",
            "storage_bucket": "private-media",
            "object_key": f"submission-media/{submission.pk}/{uuid.uuid4().hex}",
            "sealed_object_key": (
                f"submission-media-sealed/{submission.pk}/{uuid.uuid4().hex}"
            ),
            "expected_mime": "image/png",
            "declared_byte_size": 10,
            "declared_sha256": "a" * 64,
            "created_at": activity_at,
            "absolute_expires_at": activity_at + timedelta(hours=24),
        }
        if state in {
            MediaUploadIntent.State.ISSUED,
            MediaUploadIntent.State.VERIFIED,
            MediaUploadIntent.State.ATTACHED,
        }:
            values.update(
                issued_at=activity_at,
                presign_expires_at=activity_at + timedelta(minutes=10),
            )
        if state in {
            MediaUploadIntent.State.VERIFIED,
            MediaUploadIntent.State.ATTACHED,
        }:
            values.update(
                server_byte_size=10,
                server_sha256="b" * 64,
                detected_mime="image/png",
                width=2,
                height=2,
                verified_at=activity_at,
                last_verification_at=activity_at,
            )
        if state == MediaUploadIntent.State.ATTACHED:
            values["attached_at"] = activity_at
        if state in {MediaUploadIntent.State.FAILED, MediaUploadIntent.State.EXPIRED}:
            values.update(failure_code="prior_failure", failure_at=activity_at)
        if state == MediaUploadIntent.State.DELETED:
            values["deleted_at"] = activity_at
        return MediaUploadIntent.objects.create(**values)

    def attach_image(self, submission, intent, *, attached_at):
        return Image.objects.create(
            set_id="",
            name="",
            url="",
            metadata=None,
            request=submission,
            place=None,
            is_managed=True,
            intent=intent,
            owner=self.owner,
            position=intent.slot,
            state="attached",
            storage_identifier=intent.storage_identifier,
            storage_bucket=intent.storage_bucket,
            storage_key=intent.sealed_object_key,
            byte_size=intent.server_byte_size,
            detected_mime=intent.detected_mime,
            width=intent.width,
            height=intent.height,
            sha256=intent.server_sha256,
            attached_at=attached_at,
        )

    def test_exact_30_day_inclusive_threshold(self):
        now = timezone.now()
        before = self.create_submission(
            activity_at=now - DRAFT_INACTIVITY_LIMIT + timedelta(microseconds=1),
            name="Before threshold",
        )
        exact = self.create_submission(
            activity_at=now - DRAFT_INACTIVITY_LIMIT,
            name="Exact threshold",
        )
        after = self.create_submission(
            activity_at=now - DRAFT_INACTIVITY_LIMIT - timedelta(microseconds=1),
            name="After threshold",
        )

        counts = process_submission_expiry(batch_size=10, now=now)

        before.refresh_from_db()
        exact.refresh_from_db()
        after.refresh_from_db()
        self.assertEqual(counts.expired, 2)
        self.assertEqual(before.state, Request.State.DRAFT)
        self.assertEqual(exact.state, Request.State.EXPIRED)
        self.assertEqual(after.state, Request.State.EXPIRED)

    def test_recent_edit_and_owner_only_media_timestamps_keep_draft_alive(self):
        now = timezone.now()
        old = now - timedelta(days=31)
        recent = now - timedelta(days=1)
        edited = self.create_submission(activity_at=recent, name="Recent edit")

        timestamp_states = (
            (MediaUploadIntent.State.CREATED, "created_at"),
            (MediaUploadIntent.State.ISSUED, "issued_at"),
            (MediaUploadIntent.State.VERIFIED, "last_verification_at"),
            (MediaUploadIntent.State.VERIFIED, "verified_at"),
            (MediaUploadIntent.State.ATTACHED, "attached_at"),
        )
        media_submissions = []
        for index, (state, field) in enumerate(timestamp_states):
            submission = self.create_submission(
                activity_at=old, name=f"Recent media {index}"
            )
            intent = self.create_intent(submission, state, activity_at=old)
            MediaUploadIntent.objects.filter(pk=intent.pk).update(**{field: recent})
            if state == MediaUploadIntent.State.ATTACHED:
                intent.refresh_from_db()
                self.attach_image(submission, intent, attached_at=recent)
            media_submissions.append(submission)

        counts = process_submission_expiry(batch_size=20, now=now)

        self.assertEqual(counts.expired, 0)
        edited.refresh_from_db()
        self.assertEqual(edited.state, Request.State.DRAFT)
        self.assertFalse(
            Request.objects.filter(
                pk__in=[submission.pk for submission in media_submissions]
            ).exclude(state=Request.State.DRAFT).exists()
        )

    def test_delayed_autonomous_media_failure_does_not_refresh_draft(self):
        cleanup_now = timezone.now()
        old = cleanup_now - timedelta(days=31)
        autonomous_only = self.create_submission(
            activity_at=old, name="Autonomous failure only"
        )
        owner_refreshed = self.create_submission(
            activity_at=old, name="Owner media activity"
        )
        autonomous_intent = self.create_intent(
            autonomous_only, MediaUploadIntent.State.CREATED, activity_at=old
        )
        owner_intent = self.create_intent(
            owner_refreshed, MediaUploadIntent.State.CREATED, activity_at=old
        )
        storage = Mock()
        storage.object_is_absent.return_value = True

        media_counts = media_services.process_media_cleanup(
            storage=storage, now=cleanup_now
        )

        autonomous_intent.refresh_from_db()
        owner_intent.refresh_from_db()
        self.assertEqual((media_counts.expired, media_counts.deleted), (2, 2))
        self.assertEqual(autonomous_intent.failure_at, cleanup_now)
        self.assertEqual(owner_intent.failure_at, cleanup_now)
        self.assertFalse(
            SubmissionIdempotency.objects.filter(submission=autonomous_only).exists()
        )

        # Expiring an already-cleaned intent is still an owner operation. Its
        # idempotency record, rather than the ambiguous failure timestamp, is
        # authoritative evidence that this draft was recently active.
        expire_upload_intent(self.owner, owner_intent.pk, "owner-media-expire")
        expiry_now = timezone.now()
        self.assertTrue(
            SubmissionIdempotency.objects.filter(
                submission=owner_refreshed,
                media_intent=owner_intent,
                operation=SubmissionOperation.MEDIA_EXPIRE,
                created_at__gt=expiry_now - DRAFT_INACTIVITY_LIMIT,
            ).exists()
        )

        expiry_counts = process_submission_expiry(batch_size=10, now=expiry_now)

        autonomous_only.refresh_from_db()
        owner_refreshed.refresh_from_db()
        self.assertEqual(expiry_counts.expired, 1)
        self.assertEqual(autonomous_only.state, Request.State.EXPIRED)
        self.assertEqual(owner_refreshed.state, Request.State.DRAFT)

    def test_owner_media_cleanup_idempotency_refreshes_draft(self):
        now = timezone.now()
        old = now - timedelta(days=31)
        submission = self.create_submission(activity_at=old)
        intent = self.create_intent(
            submission, MediaUploadIntent.State.CLEANUP_PENDING, activity_at=old
        )
        storage = Mock()
        storage.object_is_absent.return_value = True

        cleaned, deleted, replayed = cleanup_media_object(
            self.owner, intent.pk, "owner-media-cleanup", storage=storage
        )
        expiry_now = timezone.now()

        self.assertTrue(deleted)
        self.assertFalse(replayed)
        self.assertEqual(cleaned.state, MediaUploadIntent.State.DELETED)
        self.assertTrue(
            SubmissionIdempotency.objects.filter(
                submission=submission,
                media_intent=intent,
                actor=self.owner,
                operation=SubmissionOperation.MEDIA_CLEANUP,
                created_at__gt=expiry_now - DRAFT_INACTIVITY_LIMIT,
            ).exists()
        )

        counts = process_submission_expiry(now=expiry_now)

        submission.refresh_from_db()
        self.assertEqual(counts.expired, 0)
        self.assertEqual(submission.state, Request.State.DRAFT)

    def test_autonomous_cleanup_retry_writes_no_owner_evidence_or_refresh(self):
        cleanup_now = timezone.now()
        old = cleanup_now - timedelta(days=31)
        submission = self.create_submission(activity_at=old)
        intent = self.create_intent(
            submission, MediaUploadIntent.State.CLEANUP_PENDING, activity_at=old
        )
        storage = Mock()
        storage.object_is_absent.return_value = False

        media_counts = media_services.process_media_cleanup(
            storage=storage, now=cleanup_now
        )

        intent.refresh_from_db()
        self.assertEqual((media_counts.claimed, media_counts.failed), (1, 1))
        self.assertEqual(intent.cleanup_attempts, 1)
        self.assertEqual(intent.cleanup_last_attempt_at, cleanup_now)
        self.assertIsNotNone(intent.cleanup_next_attempt_at)
        self.assertFalse(
            SubmissionIdempotency.objects.filter(submission=submission).exists()
        )

        expiry_counts = process_submission_expiry(now=timezone.now())

        submission.refresh_from_db()
        self.assertEqual(expiry_counts.expired, 1)
        self.assertEqual(submission.state, Request.State.EXPIRED)

    def test_cleanup_retry_timestamps_do_not_refresh_activity_or_steal_claim(self):
        now = timezone.now()
        old = now - timedelta(days=31)
        submission = self.create_submission(activity_at=old)
        intent = self.create_intent(
            submission, MediaUploadIntent.State.CLEANUP_PENDING, activity_at=old
        )
        token = uuid.uuid4()
        retry_at = now + timedelta(minutes=10)
        MediaUploadIntent.objects.filter(pk=intent.pk).update(
            failure_code="prior_failure",
            failure_at=old,
            cleanup_claim_token=token,
            cleanup_claimed_at=now,
            cleanup_lease_until=now + timedelta(minutes=5),
            cleanup_attempts=4,
            cleanup_last_attempt_at=now,
            cleanup_next_attempt_at=retry_at,
            updated_at=now,
        )

        counts = process_submission_expiry(now=now)

        submission.refresh_from_db()
        intent.refresh_from_db()
        self.assertEqual(counts.expired, 1)
        self.assertEqual(submission.state, Request.State.EXPIRED)
        self.assertEqual(intent.cleanup_claim_token, token)
        self.assertEqual(intent.cleanup_next_attempt_at, retry_at)
        self.assertEqual(intent.cleanup_attempts, 4)

    def test_recent_false_positive_cannot_starve_due_candidate(self):
        now = timezone.now()
        oldest = now - timedelta(days=40)
        recent_submission = self.create_submission(
            activity_at=oldest, name="Old row recent media"
        )
        self.create_intent(
            recent_submission,
            MediaUploadIntent.State.CREATED,
            activity_at=now - timedelta(days=1),
        )
        due = self.create_submission(
            activity_at=now - timedelta(days=31), name="Actually due"
        )

        counts = process_submission_expiry(batch_size=1, now=now)

        recent_submission.refresh_from_db()
        due.refresh_from_db()
        self.assertEqual(counts.expired, 1)
        self.assertEqual(recent_submission.state, Request.State.DRAFT)
        self.assertEqual(due.state, Request.State.EXPIRED)

    def test_repeated_runs_and_non_draft_states_are_immune(self):
        now = timezone.now()
        old = now - timedelta(days=31)
        due = self.create_submission(activity_at=old, name="Run once")
        immune = [
            self.create_submission(state=state, activity_at=old, name=f"Immune {state}")
            for state in (
                Request.State.PENDING,
                Request.State.WITHDRAWN,
                Request.State.EXPIRED,
                Request.State.APPROVED,
                Request.State.REJECTED,
            )
        ]

        first = process_submission_expiry(now=now)
        second = process_submission_expiry(now=now)

        self.assertEqual((first.expired, second.expired), (1, 0))
        self.assertEqual(
            SubmissionLifecycleEvent.objects.filter(
                submission=due, operation=SubmissionOperation.EXPIRE
            ).count(),
            1,
        )
        self.assertFalse(
            Request.objects.filter(pk__in=[item.pk for item in immune], state=Request.State.DRAFT).exists()
        )

    def test_expiry_hands_off_every_non_deleted_state_and_removes_attachment(self):
        now = timezone.now()
        old = now - timedelta(days=31)
        submission = self.create_submission(activity_at=old)
        states = (
            MediaUploadIntent.State.CREATED,
            MediaUploadIntent.State.ISSUED,
            MediaUploadIntent.State.VERIFIED,
            MediaUploadIntent.State.ATTACHED,
            MediaUploadIntent.State.FAILED,
            MediaUploadIntent.State.EXPIRED,
            MediaUploadIntent.State.DELETED,
        )
        intents = {}
        reserving_slots = iter((0, 1, 2))
        for state in states:
            slot = next(reserving_slots) if state in MediaUploadIntent.RESERVING_STATES else 0
            intent = self.create_intent(submission, state, slot=slot, activity_at=old)
            intents[state] = intent
            if state == MediaUploadIntent.State.ATTACHED:
                self.attach_image(submission, intent, attached_at=old)

        with patch(
            "backend.media.configured_media_storage",
            side_effect=AssertionError("storage accessed under expiry lock"),
        ), patch(
            "socket.socket",
            side_effect=AssertionError("network accessed under expiry lock"),
        ):
            counts = process_submission_expiry(now=now)

        self.assertEqual(counts.expired, 1)
        self.assertFalse(Image.objects.filter(request=submission, is_managed=True).exists())
        for state, intent in intents.items():
            intent.refresh_from_db()
            expected = (
                MediaUploadIntent.State.DELETED
                if state == MediaUploadIntent.State.DELETED
                else MediaUploadIntent.State.CLEANUP_PENDING
            )
            self.assertEqual(intent.state, expected)
            if state not in {
                MediaUploadIntent.State.FAILED,
                MediaUploadIntent.State.EXPIRED,
                MediaUploadIntent.State.DELETED,
            }:
                self.assertEqual(intent.failure_code, "submission_expired")
                self.assertIsNone(intent.cleanup_next_attempt_at)

        event = SubmissionLifecycleEvent.objects.get(
            submission=submission, operation=SubmissionOperation.EXPIRE
        )
        self.assertIsNone(event.actor_id)
        self.assertIsNone(event.idempotency_id)
        self.assertEqual(event.system_actor, "draft-expiry.v3")

    def test_database_rejects_fake_owner_and_system_evidence(self):
        submission = self.create_submission()
        with self.assertRaises(IntegrityError), transaction.atomic():
            SubmissionIdempotency.objects.create(
                actor=self.owner,
                operation=SubmissionOperation.EXPIRE,
                key="fake-expiry",
                request_hash="0" * 64,
                submission=submission,
                original_result={"state": "expired"},
            )

        with self.assertRaises(IntegrityError), transaction.atomic():
            SubmissionLifecycleEvent.objects.create(
                submission=submission,
                actor=self.owner,
                system_actor=None,
                operation=SubmissionOperation.EXPIRE,
                from_state=Request.State.DRAFT,
                to_state=Request.State.EXPIRED,
                outcome=SubmissionLifecycleEvent.Outcome.SUCCEEDED,
                idempotency=None,
            )

        edit_idempotency = SubmissionIdempotency.objects.create(
            actor=self.owner,
            operation=SubmissionOperation.EDIT,
            key="fake-system-edit",
            request_hash="1" * 64,
            submission=submission,
            original_result={"state": "draft"},
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            SubmissionLifecycleEvent.objects.create(
                submission=submission,
                actor=None,
                system_actor=SubmissionLifecycleEvent.DRAFT_EXPIRY_SYSTEM_ACTOR,
                operation=SubmissionOperation.EDIT,
                from_state=Request.State.DRAFT,
                to_state=Request.State.DRAFT,
                outcome=SubmissionLifecycleEvent.Outcome.SUCCEEDED,
                idempotency=edit_idempotency,
            )

    def test_batch_size_validation(self):
        for value in (True, 0, 1001, 1.5, "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                process_submission_expiry(batch_size=value)

    def test_command_runs_draft_expiry_before_media_cleanup(self):
        calls = []
        media_counts = SimpleNamespace(
            expired=0,
            claimed=0,
            deleted=0,
            upload_claimed=0,
            upload_deleted=0,
            redacted=0,
            failed=0,
            skipped=0,
        )
        output = StringIO()
        with patch(
            "backend.management.commands.process_media_cleanup.process_submission_expiry",
            side_effect=lambda **kwargs: (
                calls.append("expiry") or SimpleNamespace(expired=2)
            ),
        ), patch(
            "backend.management.commands.process_media_cleanup.process_media_cleanup",
            side_effect=lambda **kwargs: calls.append("media") or media_counts,
        ):
            call_command("process_media_cleanup", batch_size=7, stdout=output)

        self.assertEqual(calls, ["expiry", "media"])
        self.assertIn("draft_expired=2", output.getvalue())
        self.assertIn("expired=0 claimed=0", output.getvalue())


@override_settings(
    AWS_STORAGE_BUCKET_NAME="legacy-public-images",
    MEDIA_STORAGE_BUCKET_NAME="test-private-media",
    MEDIA_STORAGE_IDENTIFIER="test-s3-private",
)
class SubmissionExpiryRaceTests(TransactionTestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            email="draft-expiry-race@smokemap.test", password="test"
        )
        self.category, _created = Category.objects.get_or_create(
            slug="outdoors", defaults={"name": "Outdoors"}
        )

    def create_due_submission(self, name):
        address = Address.objects.create(
            addressString="Race", location=Point(34.8, 32.1, srid=4326)
        )
        submission = Request.objects.create(
            name=name,
            category=self.category,
            address=address,
            owner=self.owner,
            state=Request.State.DRAFT,
            approved=False,
        )
        old = timezone.now() - timedelta(days=31)
        Request.objects.filter(pk=submission.pk).update(
            date_created=old, date_updated=old
        )
        submission.refresh_from_db()
        return submission

    def raw_input(self, name):
        return {
            "name": name,
            "category_slug": self.category.slug,
            "longitude": 34.8,
            "latitude": 32.1,
            "address_label": "Race",
            "tags": [],
            "description": None,
            "website": None,
        }

    def run_thread(self, target):
        thread = threading.Thread(target=target)
        thread.start()
        return thread

    def owner_operation(self, kind, actor, submission):
        if kind == "edit":
            return edit_submission(
                actor,
                submission.pk,
                f"race-edit-{submission.pk}",
                self.raw_input(f"Edited {submission.pk}"),
            )
        if kind == "finalize":
            return finalize_submission(
                actor, submission.pk, f"race-finalize-{submission.pk}"
            )
        return create_upload_intent(
            actor,
            submission.pk,
            f"race-media-{submission.pk}",
            mime_type="image/png",
            declared_byte_size=10,
            declared_sha256="a" * 64,
        )

    def test_expiry_lock_wins_against_edit_finalize_and_media(self):
        for kind in ("edit", "finalize", "media"):
            with self.subTest(kind=kind):
                submission = self.create_due_submission(f"Expiry wins {kind}")
                expiry_locked = threading.Event()
                release_expiry = threading.Event()
                outcomes = []
                real_handoff = expiry_services._handoff_media_for_expired_submission

                def paused_handoff(*args, **kwargs):
                    expiry_locked.set()
                    release_expiry.wait(timeout=30)
                    return real_handoff(*args, **kwargs)

                def expiry_worker():
                    close_old_connections()
                    try:
                        process_submission_expiry(now=timezone.now())
                    except Exception as error:
                        outcomes.append(("expiry", type(error).__name__))
                    finally:
                        close_old_connections()

                def owner_worker():
                    close_old_connections()
                    try:
                        actor = User.objects.get(pk=self.owner.pk)
                        self.owner_operation(kind, actor, submission)
                        outcomes.append(("owner", "succeeded"))
                    except Exception as error:
                        outcomes.append(("owner", type(error).__name__))
                    finally:
                        close_old_connections()

                with patch.object(
                    expiry_services,
                    "_handoff_media_for_expired_submission",
                    side_effect=paused_handoff,
                ):
                    expiry_thread = self.run_thread(expiry_worker)
                    self.assertTrue(expiry_locked.wait(timeout=10))
                    owner_thread = self.run_thread(owner_worker)
                    release_expiry.set()
                    expiry_thread.join(timeout=30)
                    owner_thread.join(timeout=30)

                self.assertFalse(expiry_thread.is_alive())
                self.assertFalse(owner_thread.is_alive())
                submission.refresh_from_db()
                self.assertEqual(submission.state, Request.State.EXPIRED)
                expected_error = (
                    "MediaStateConflict" if kind == "media" else "SubmissionStateError"
                )
                self.assertIn(("owner", expected_error), outcomes)
                self.assertEqual(
                    SubmissionLifecycleEvent.objects.filter(
                        submission=submission,
                        operation=SubmissionOperation.EXPIRE,
                    ).count(),
                    1,
                )

    def test_owner_lock_wins_and_expiry_recheck_skips(self):
        for kind in ("edit", "finalize", "media"):
            with self.subTest(kind=kind):
                submission = self.create_due_submission(f"Owner wins {kind}")
                owner_locked = threading.Event()
                release_owner = threading.Event()
                outcomes = []

                if kind in {"edit", "finalize"}:
                    real_lock = submission_services._locked_owned_submission

                    def paused_lock(*args, **kwargs):
                        locked = real_lock(*args, **kwargs)
                        if locked.pk == submission.pk:
                            owner_locked.set()
                            release_owner.wait(timeout=30)
                        return locked

                    patcher = patch.object(
                        submission_services,
                        "_locked_owned_submission",
                        side_effect=paused_lock,
                    )
                else:
                    real_validate = media_services._validate_actor_submission

                    def paused_validation(*args, **kwargs):
                        result = real_validate(*args, **kwargs)
                        if args[1].pk == submission.pk:
                            owner_locked.set()
                            release_owner.wait(timeout=30)
                        return result

                    patcher = patch.object(
                        media_services,
                        "_validate_actor_submission",
                        side_effect=paused_validation,
                    )

                def owner_worker():
                    close_old_connections()
                    try:
                        actor = User.objects.get(pk=self.owner.pk)
                        self.owner_operation(kind, actor, submission)
                        outcomes.append(("owner", "succeeded"))
                    except Exception as error:
                        outcomes.append(("owner", type(error).__name__))
                    finally:
                        close_old_connections()

                def expiry_worker():
                    close_old_connections()
                    try:
                        counts = process_submission_expiry(now=timezone.now())
                        outcomes.append(("expiry", counts.expired))
                    except Exception as error:
                        outcomes.append(("expiry", type(error).__name__))
                    finally:
                        close_old_connections()

                with patcher:
                    owner_thread = self.run_thread(owner_worker)
                    self.assertTrue(owner_locked.wait(timeout=10))
                    expiry_thread = self.run_thread(expiry_worker)
                    expiry_thread.join(timeout=10)
                    release_owner.set()
                    owner_thread.join(timeout=30)
                    expiry_thread.join(timeout=30)

                self.assertFalse(owner_thread.is_alive())
                self.assertFalse(expiry_thread.is_alive())
                self.assertIn(("owner", "succeeded"), outcomes)
                self.assertIn(("expiry", 0), outcomes)
                submission.refresh_from_db()
                expected_state = (
                    Request.State.PENDING
                    if kind == "finalize"
                    else Request.State.DRAFT
                )
                self.assertEqual(submission.state, expected_state)
                self.assertFalse(
                    SubmissionLifecycleEvent.objects.filter(
                        submission=submission,
                        operation=SubmissionOperation.EXPIRE,
                    ).exists()
                )


class SystemDraftExpiryMigrationTests(TransactionTestCase):
    migrate_from = [("backend", "0010_submission_draft_edit")]
    migrate_to = [("backend", "0011_system_draft_expiry")]

    def setUp(self):
        super().setUp()
        self.addCleanup(self.restore_latest)
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        apps = executor.loader.project_state(self.migrate_from).apps
        user = apps.get_model("backend", "CustomUser").objects.create(
            email="expiry-migration@smokemap.test", password="!"
        )
        category, _created = apps.get_model("backend", "Category").objects.get_or_create(
            slug="outdoors", defaults={"name": "Outdoors"}
        )
        address = apps.get_model("backend", "Address").objects.create(
            addressString="Migration", location=Point(1, 1, srid=4326)
        )
        self.submission_id = apps.get_model("backend", "Request").objects.create(
            name="Migration expiry",
            category_id=category.pk,
            address_id=address.pk,
            owner_id=user.pk,
            state="draft",
            approved=False,
        ).pk
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def restore_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_reverse_refuses_to_drop_system_expiry_evidence(self):
        self.apps.get_model("backend", "SubmissionLifecycleEvent").objects.create(
            submission_id=self.submission_id,
            actor_id=None,
            system_actor="draft-expiry.v3",
            operation="submission.expire.v3",
            from_state="draft",
            to_state="expired",
            outcome="succeeded",
            idempotency_id=None,
        )
        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(RuntimeError, "system expiry evidence"):
            executor.migrate(self.migrate_from)

    def test_reverse_is_lossless_without_system_expiry_evidence(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        apps = executor.loader.project_state(self.migrate_from).apps
        lifecycle_model = apps.get_model("backend", "SubmissionLifecycleEvent")
        self.assertNotIn(
            "system_actor", {field.name for field in lifecycle_model._meta.fields}
        )

    def test_forward_refuses_legacy_actor_attributed_expiry_evidence(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        apps = executor.loader.project_state(self.migrate_from).apps
        request_model = apps.get_model("backend", "Request")
        idempotency_model = apps.get_model("backend", "SubmissionIdempotency")
        lifecycle_model = apps.get_model("backend", "SubmissionLifecycleEvent")
        submission = request_model.objects.get(pk=self.submission_id)
        record = idempotency_model.objects.create(
            actor_id=submission.owner_id,
            operation="submission.expire.v3",
            key="legacy-owner-expiry",
            request_hash="f" * 64,
            submission_id=submission.pk,
            original_result={"state": "expired"},
        )
        event = lifecycle_model.objects.create(
            submission_id=submission.pk,
            actor_id=submission.owner_id,
            operation="submission.expire.v3",
            from_state="draft",
            to_state="expired",
            outcome="succeeded",
            idempotency_id=record.pk,
        )

        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(RuntimeError, "legacy actor-attributed"):
            executor.migrate(self.migrate_to)

        # Leave the database at 0010 without the deliberately invalid evidence
        # so the shared latest-schema cleanup can run independently.
        lifecycle_model.objects.filter(pk=event.pk).delete()
        idempotency_model.objects.filter(pk=record.pk).delete()
