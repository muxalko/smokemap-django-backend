import hashlib
import threading
import uuid
from datetime import timedelta
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from graphene.test import Client as GraphQLClient

from . import moderation as moderation_services
from . import submission_expiry as expiry_services
from . import submissions as submission_services
from .media import process_media_cleanup
from .models import (
    Category,
    Image,
    Location,
    MediaUploadIntent,
    ModerationAudit,
    Place,
    Request,
    SubmissionIdempotency,
    SubmissionLifecycleEvent,
    SubmissionOperation,
    Tag,
)
from .moderation import (
    approve_submission,
    hard_delete_place,
    hard_delete_submission,
    reject_submission,
    withdraw_submission,
)
from .schema import schema
from .submission_expiry import process_submission_expiry
from .submissions import (
    DuplicateSubmission,
    SubmissionStateError,
    create_submission,
    finalize_submission,
)


PRIVATE_MEDIA_SETTINGS = {
    "AWS_STORAGE_BUCKET_NAME": "legacy-public-images",
    "MEDIA_STORAGE_BUCKET_NAME": "test-private-media",
    "MEDIA_STORAGE_IDENTIFIER": "test-s3-private",
}


class ModerationFixtureMixin:
    def build_fixtures(self):
        user_model = get_user_model()
        token = uuid.uuid4().hex
        self.owner = user_model.objects.create_user(
            email=f"moderation-owner-{token}@smokemap.test", password="test"
        )
        self.other_owner = user_model.objects.create_user(
            email=f"moderation-other-{token}@smokemap.test", password="test"
        )
        self.moderator = user_model.objects.create_user(
            email=f"moderation-reviewer-{token}@smokemap.test",
            password="test",
            is_staff=True,
        )
        self.administrator = user_model.objects.create_user(
            email=f"moderation-admin-{token}@smokemap.test",
            password="test",
            is_staff=True,
            is_superuser=True,
        )
        self.inactive = user_model.objects.create_user(
            email=f"moderation-inactive-{token}@smokemap.test",
            password="test",
            is_active=False,
            is_staff=True,
        )
        self.graphql = GraphQLClient(schema)

    def raw_input(self, name="M4 lifecycle place", **overrides):
        values = {
            "name": name,
            "category_slug": "outdoors",
            "longitude": -77.0365,
            "latitude": 38.8977,
            "address_label": "Moderation address",
            "tags": ["Private proposal"],
            "description": "Moderation description",
            "website": "https://www.smokemap.org/moderation",
        }
        values.update(overrides)
        return values

    def create_draft(self, owner=None, name="M4 lifecycle place", **overrides):
        submission, _ = create_submission(
            owner or self.owner,
            f"create-{uuid.uuid4().hex}",
            self.raw_input(name, **overrides),
        )
        return submission

    def create_pending(self, owner=None, name="M4 lifecycle place", **overrides):
        owner = owner or self.owner
        submission = self.create_draft(owner, name, **overrides)
        finalize_submission(owner, submission.pk, f"finalize-{uuid.uuid4().hex}")
        submission.refresh_from_db()
        return submission

    def attach_media(self, submission, slot=0):
        now = timezone.now()
        digest = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        intent = MediaUploadIntent.objects.create(
            submission=submission,
            owner=submission.owner,
            state=MediaUploadIntent.State.ATTACHED,
            slot=slot,
            storage_identifier="test-s3-private",
            storage_bucket="test-private-media",
            object_key=f"submission-media/{submission.pk}/{uuid.uuid4().hex}",
            sealed_object_key=(
                f"submission-media-sealed/{submission.pk}/{uuid.uuid4().hex}"
            ),
            expected_mime="image/png",
            declared_byte_size=64,
            declared_sha256=digest,
            created_at=now,
            absolute_expires_at=now + timedelta(hours=24),
            issued_at=now,
            presign_expires_at=now + timedelta(minutes=10),
            server_byte_size=64,
            server_sha256=digest,
            detected_mime="image/png",
            width=2,
            height=2,
            verified_at=now,
            attached_at=now,
        )
        image = Image.objects.create(
            set_id="",
            name="",
            url="",
            metadata=None,
            request=submission,
            place=None,
            is_managed=True,
            intent=intent,
            owner=submission.owner,
            position=slot,
            state="attached",
            storage_identifier=intent.storage_identifier,
            storage_bucket=intent.storage_bucket,
            storage_key=intent.sealed_object_key,
            byte_size=64,
            detected_mime="image/png",
            width=2,
            height=2,
            sha256=digest,
            attached_at=now,
        )
        return intent, image

    @staticmethod
    def attach_legacy_image(submission, name="legacy.jpg"):
        return Image.objects.create(
            set_id="legacy-set",
            name=name,
            url=f"https://legacy.invalid/{name}",
            metadata={"legacy": True},
            request=submission,
            place=None,
        )

    @staticmethod
    def context(user):
        return SimpleNamespace(user=user, META={})

    @staticmethod
    def error_code(result):
        return result["errors"][0]["extensions"]["code"]


@override_settings(**PRIVATE_MEDIA_SETTINGS)
class ModerationLifecycleTests(ModerationFixtureMixin, TestCase):
    def setUp(self):
        self.build_fixtures()

    def test_owner_withdrawal_serializes_state_audit_and_media_cleanup_handoff(self):
        submission = self.create_draft()
        intent, image = self.attach_media(submission)

        result = withdraw_submission(self.owner, submission.pk, "withdraw-key")

        self.assertFalse(result.replayed)
        submission.refresh_from_db()
        intent.refresh_from_db()
        self.assertEqual(submission.state, Request.State.WITHDRAWN)
        self.assertFalse(Image.objects.filter(pk=image.pk).exists())
        self.assertEqual(intent.state, MediaUploadIntent.State.CLEANUP_PENDING)
        self.assertEqual(intent.failure_code, "submission_withdrawn")
        event = SubmissionLifecycleEvent.objects.get(
            submission=submission, operation=SubmissionOperation.WITHDRAW
        )
        self.assertEqual(event.actor_id, self.owner.pk)
        self.assertEqual(event.from_state, Request.State.DRAFT)
        self.assertEqual(event.to_state, Request.State.WITHDRAWN)
        self.assertEqual(event.idempotency.actor_id, self.owner.pk)

        replay = withdraw_submission(self.owner, submission.pk, "withdraw-key")
        self.assertTrue(replay.replayed)
        self.assertEqual(
            SubmissionLifecycleEvent.objects.filter(
                submission=submission, operation=SubmissionOperation.WITHDRAW
            ).count(),
            1,
        )

    def test_approval_materializes_one_place_promotes_tags_and_records_reviewer(self):
        submission = self.create_pending()
        private_tag = Tag.objects.get(canonical="private proposal")
        managed_intent, managed_image = self.attach_media(submission)
        legacy_image = self.attach_legacy_image(submission)

        result = approve_submission(
            self.moderator, submission.pk, "approve-key", "  Looks\n good  "
        )

        self.assertFalse(result.replayed)
        submission.refresh_from_db()
        private_tag.refresh_from_db()
        self.assertEqual(submission.state, Request.State.APPROVED)
        self.assertTrue(submission.approved)
        self.assertEqual(submission.reviewed_by_id, self.moderator.pk)
        self.assertEqual(submission.approved_comment, "Looks good")
        self.assertIsNotNone(submission.date_approved)
        self.assertEqual(result.place.name, submission.name)
        self.assertEqual(result.place.address_id, submission.address_id)
        self.assertEqual(
            list(result.place.tags.values_list("pk", flat=True)), [private_tag.pk]
        )
        self.assertTrue(private_tag.is_public)
        legacy_image.refresh_from_db()
        managed_image.refresh_from_db()
        managed_intent.refresh_from_db()
        self.assertEqual(legacy_image.place_id, result.place.pk)
        self.assertIsNone(managed_image.place_id)
        self.assertEqual(managed_image.request_id, submission.pk)
        self.assertEqual(managed_intent.state, MediaUploadIntent.State.ATTACHED)
        location = Location.objects.get(place_id=str(result.place.pk))
        self.assertEqual(location.name, submission.name)
        self.assertEqual(location.category, submission.category_id)
        self.assertEqual(location.geom, submission.address.location)
        self.assertEqual(location.tags, private_tag.name)
        event = SubmissionLifecycleEvent.objects.get(
            submission=submission, operation=SubmissionOperation.APPROVE
        )
        self.assertEqual(event.actor_id, self.moderator.pk)
        self.assertEqual(event.comment, "Looks good")
        self.assertEqual(event.from_state, Request.State.PENDING)
        self.assertEqual(event.to_state, Request.State.APPROVED)

        replay = approve_submission(
            self.moderator, submission.pk, "approve-key", "Looks good"
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.place.pk, result.place.pk)
        with self.assertRaises(SubmissionStateError):
            approve_submission(
                self.moderator, submission.pk, "different-approve-key", "Looks good"
            )
        self.assertEqual(Place.objects.filter(name=submission.name).count(), 1)
        self.assertEqual(
            Location.objects.filter(place_id=str(result.place.pk)).count(), 1
        )

    def test_rejection_is_distinct_from_withdrawal_and_hands_media_to_cleanup(self):
        submission = self.create_pending()
        intent, image = self.attach_media(submission)

        result = reject_submission(
            self.moderator, submission.pk, "reject-key", "Insufficient detail"
        )

        self.assertFalse(result.replayed)
        submission.refresh_from_db()
        intent.refresh_from_db()
        self.assertEqual(submission.state, Request.State.REJECTED)
        self.assertEqual(submission.reviewed_by_id, self.moderator.pk)
        self.assertEqual(submission.approved_comment, "Insufficient detail")
        self.assertFalse(submission.approved)
        self.assertFalse(Image.objects.filter(pk=image.pk).exists())
        self.assertEqual(intent.state, MediaUploadIntent.State.CLEANUP_PENDING)
        event = SubmissionLifecycleEvent.objects.get(
            submission=submission, operation=SubmissionOperation.REJECT
        )
        self.assertEqual(event.actor_id, self.moderator.pk)
        self.assertEqual(event.comment, "Insufficient detail")
        self.assertEqual(event.to_state, Request.State.REJECTED)
        self.assertFalse(
            SubmissionLifecycleEvent.objects.filter(
                submission=submission, operation=SubmissionOperation.WITHDRAW
            ).exists()
        )

    def test_authorization_matrix_and_self_review_fail_without_writes(self):
        pending = self.create_pending()
        own_staff_pending = self.create_pending(
            owner=self.moderator, name="Reviewer's own submission", longitude=12
        )
        guest = SimpleNamespace(is_authenticated=False, is_active=False)
        mutation = """
          mutation Review($id: ID!, $key: String!) {
            approveSubmissionV4(submissionId: $id, idempotencyKey: $key) {
              submission { id state }
              place { id }
              replayed
            }
          }
        """
        for index, account, code in (
            (0, guest, "UNAUTHENTICATED"),
            (1, self.inactive, "UNAUTHENTICATED"),
            (2, self.owner, "FORBIDDEN"),
        ):
            result = self.graphql.execute(
                mutation,
                variable_values={"id": str(pending.pk), "key": f"denied-{index}"},
                context_value=self.context(account),
            )
            self.assertEqual(self.error_code(result), code)

        self_review = self.graphql.execute(
            mutation,
            variable_values={"id": str(own_staff_pending.pk), "key": "self-review"},
            context_value=self.context(self.moderator),
        )
        self.assertEqual(self.error_code(self_review), "FORBIDDEN")
        self.assertFalse(
            SubmissionLifecycleEvent.objects.filter(
                operation=SubmissionOperation.APPROVE
            ).exists()
        )
        self.assertFalse(Place.objects.exists())

        foreign_withdrawal = """
          mutation Withdraw($id: ID!, $key: String!) {
            withdrawSubmissionV4(submissionId: $id, idempotencyKey: $key) {
              submission { id state }
              replayed
            }
          }
        """
        denied = self.graphql.execute(
            foreign_withdrawal,
            variable_values={"id": str(pending.pk), "key": "foreign-withdraw"},
            context_value=self.context(self.other_owner),
        )
        self.assertEqual(self.error_code(denied), "NOT_FOUND")

    def test_injected_audit_failure_rolls_back_place_tags_and_state(self):
        submission = self.create_pending()
        tag = Tag.objects.get(canonical="private proposal")
        legacy_image = self.attach_legacy_image(submission, "rollback.jpg")

        with patch.object(
            SubmissionLifecycleEvent.objects,
            "create",
            side_effect=RuntimeError("injected audit failure"),
        ), self.assertRaises(RuntimeError):
            approve_submission(self.moderator, submission.pk, "rollback-key")

        submission.refresh_from_db()
        tag.refresh_from_db()
        self.assertEqual(submission.state, Request.State.PENDING)
        self.assertFalse(submission.approved)
        self.assertIsNone(submission.reviewed_by_id)
        self.assertFalse(tag.is_public)
        self.assertFalse(Place.objects.exists())
        self.assertFalse(Location.objects.exists())
        legacy_image.refresh_from_db()
        self.assertIsNone(legacy_image.place_id)
        self.assertFalse(
            SubmissionIdempotency.objects.filter(
                operation=SubmissionOperation.APPROVE
            ).exists()
        )

    def test_injected_rejection_audit_failure_restores_attachment_and_intent(self):
        submission = self.create_pending()
        intent, image = self.attach_media(submission)

        with patch.object(
            SubmissionLifecycleEvent.objects,
            "create",
            side_effect=RuntimeError("injected rejection audit failure"),
        ), self.assertRaises(RuntimeError):
            reject_submission(self.moderator, submission.pk, "reject-rollback")

        submission.refresh_from_db()
        intent.refresh_from_db()
        self.assertEqual(submission.state, Request.State.PENDING)
        self.assertEqual(intent.state, MediaUploadIntent.State.ATTACHED)
        self.assertTrue(Image.objects.filter(pk=image.pk, state="attached").exists())
        self.assertFalse(
            SubmissionIdempotency.objects.filter(
                operation=SubmissionOperation.REJECT
            ).exists()
        )

    def test_approval_revalidates_duplicate_under_the_canonical_name_lock(self):
        first = self.create_pending(name="  Ｒooftop\tCAFÉ  ")
        second = self.create_pending(
            owner=self.other_owner, name="rooftop café", longitude=-77.03649
        )
        approve_submission(self.moderator, first.pk, "first-approval")

        with self.assertRaises(DuplicateSubmission):
            approve_submission(self.moderator, second.pk, "second-approval")

        second.refresh_from_db()
        self.assertEqual(second.state, Request.State.PENDING)
        self.assertEqual(Place.objects.count(), 1)
        self.assertFalse(
            SubmissionLifecycleEvent.objects.filter(
                submission=second, operation=SubmissionOperation.APPROVE
            ).exists()
        )

    def test_audit_models_reject_update_and_direct_delete(self):
        submission = self.create_pending()
        approve_submission(self.moderator, submission.pk, "immutable-audit")
        event = SubmissionLifecycleEvent.objects.get(
            submission=submission, operation=SubmissionOperation.APPROVE
        )
        event.actor = self.administrator
        with self.assertRaisesMessage(ValidationError, "immutable"):
            event.save()
        with self.assertRaisesMessage(ValidationError, "immutable"):
            event.delete()
        with self.assertRaisesMessage(ValidationError, "immutable"):
            SubmissionLifecycleEvent.objects.filter(pk=event.pk).update(
                actor=self.administrator
            )

    def test_hard_delete_remains_admin_only_and_keeps_standalone_audit(self):
        submission = self.create_pending()
        target_id = submission.pk

        with self.assertRaisesMessage(ValueError, "administrator"):
            hard_delete_submission(self.moderator, target_id)
        self.assertTrue(Request.objects.filter(pk=target_id).exists())

        self.assertEqual(hard_delete_submission(self.administrator, target_id), target_id)
        self.assertFalse(Request.objects.filter(pk=target_id).exists())
        self.assertTrue(
            ModerationAudit.objects.filter(
                actor=self.administrator,
                action=ModerationAudit.Action.HARD_DELETE,
                target_id=target_id,
                outcome="succeeded",
            ).exists()
        )

    def test_hard_delete_refuses_to_orphan_managed_media(self):
        submission = self.create_pending()
        intent, image = self.attach_media(submission)

        with self.assertRaisesMessage(ValueError, "must be cleaned"):
            hard_delete_submission(self.administrator, submission.pk)

        self.assertTrue(Request.objects.filter(pk=submission.pk).exists())
        self.assertTrue(Image.objects.filter(pk=image.pk).exists())
        intent.refresh_from_db()
        self.assertEqual(intent.state, MediaUploadIntent.State.ATTACHED)
        self.assertFalse(
            ModerationAudit.objects.filter(target_id=submission.pk).exists()
        )

    def test_hard_delete_refuses_legacy_image_metadata_without_storage_authority(self):
        submission = self.create_pending()
        image = self.attach_legacy_image(submission)

        with self.assertRaisesMessage(ValueError, "image metadata"):
            hard_delete_submission(self.administrator, submission.pk)

        self.assertTrue(Request.objects.filter(pk=submission.pk).exists())
        self.assertTrue(Image.objects.filter(pk=image.pk).exists())
        self.assertFalse(
            ModerationAudit.objects.filter(
                target_type="request", target_id=submission.pk
            ).exists()
        )

    def test_place_hard_delete_is_admin_only_atomic_and_audited(self):
        place = Place.objects.create(
            name="Unreferenced legacy place",
            category=Category.objects.get(slug="outdoors"),
            description="Legacy",
            address=self.create_draft(name="Address source").address,
        )
        location = Location.objects.create(
            place_id=str(place.pk),
            name=place.name,
            category=place.category_id,
            info="Legacy",
            address="Moderation address",
            tags="",
            geom=place.address.location,
        )

        with self.assertRaisesMessage(ValueError, "administrator"):
            hard_delete_place(self.moderator, place.pk)

        self.assertEqual(hard_delete_place(self.administrator, place.pk), place.pk)
        self.assertFalse(Place.objects.filter(pk=place.pk).exists())
        self.assertFalse(Location.objects.filter(pk=location.pk).exists())
        self.assertTrue(
            ModerationAudit.objects.filter(
                actor=self.administrator,
                action=ModerationAudit.Action.HARD_DELETE,
                target_type="place",
                target_id=place.pk,
            ).exists()
        )

    def test_place_hard_delete_refuses_images_and_approved_submission_results(self):
        image_place = Place.objects.create(
            name="Place with legacy image",
            category=Category.objects.get(slug="outdoors"),
            description="Legacy",
            address=self.create_draft(name="Image address source").address,
        )
        image = Image.objects.create(
            set_id="legacy-set",
            name="place.jpg",
            url="https://legacy.invalid/place.jpg",
            metadata=None,
            request=None,
            place=image_place,
        )
        with self.assertRaisesMessage(ValueError, "image metadata"):
            hard_delete_place(self.administrator, image_place.pk)
        self.assertTrue(Place.objects.filter(pk=image_place.pk).exists())
        self.assertTrue(Image.objects.filter(pk=image.pk).exists())

        submission = self.create_pending(name="Retained approved result", longitude=10)
        approved = approve_submission(
            self.moderator, submission.pk, "retained-approved-result"
        ).place
        with self.assertRaisesMessage(ValueError, "approved submission"):
            hard_delete_place(self.administrator, approved.pk)
        self.assertTrue(Place.objects.filter(pk=approved.pk).exists())
        self.assertTrue(Location.objects.filter(place_id=str(approved.pk)).exists())


@override_settings(**PRIVATE_MEDIA_SETTINGS)
class ModerationRaceTests(ModerationFixtureMixin, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        # TransactionTestCase flushes migration-seeded reference rows between
        # methods. Keep every race independently runnable and order-agnostic.
        Category.objects.get_or_create(
            slug="outdoors",
            defaults={"name": "Outdoors", "description": "Outside."},
        )
        self.build_fixtures()

    def run_paused_first(self, first, second, *, service, lock_name):
        """Run two workers after proving the first owns the serialization lock."""
        first_locked = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        outcomes = Queue()
        real_lock = getattr(service, lock_name)

        def paused_lock(*args, **kwargs):
            result = real_lock(*args, **kwargs)
            if threading.current_thread().name == "moderation-race-first":
                first_locked.set()
                if not release_first.wait(timeout=20):
                    raise AssertionError("timed out waiting to release first race worker")
            return result

        def worker(label, operation, started=None):
            close_old_connections()
            try:
                if started is not None:
                    started.set()
                outcomes.put((label, "ok", operation()))
            except Exception as error:
                outcomes.put((label, type(error).__name__, str(error)))
            finally:
                close_old_connections()

        with patch.object(service, lock_name, side_effect=paused_lock):
            first_thread = threading.Thread(
                target=worker,
                args=("first", first),
                name="moderation-race-first",
                daemon=True,
            )
            second_thread = threading.Thread(
                target=worker,
                args=("second", second, second_started),
                name="moderation-race-second",
                daemon=True,
            )
            first_thread.start()
            try:
                self.assertTrue(first_locked.wait(timeout=10))
                second_thread.start()
                self.assertTrue(second_started.wait(timeout=10))
            finally:
                release_first.set()
            first_thread.join(timeout=30)
            second_thread.join(timeout=30)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        observed = [outcomes.get(timeout=1) for _index in range(2)]
        return {item[0]: item[1:] for item in observed}

    def actor(self, user):
        return get_user_model().objects.get(pk=user.pk)

    def make_due(self, submission):
        old = timezone.now() - timedelta(days=31)
        Request.objects.filter(pk=submission.pk).update(
            date_created=old, date_updated=old
        )
        SubmissionIdempotency.objects.filter(submission=submission).update(
            created_at=old
        )
        submission.refresh_from_db()
        return submission

    def make_cleanup_pending_media(self, submission):
        intent, image = self.attach_media(submission)
        image.delete()
        now = timezone.now()
        intent.state = MediaUploadIntent.State.CLEANUP_PENDING
        intent.failure_code = "media_removed"
        intent.failure_at = now
        intent.save(
            update_fields=["state", "failure_code", "failure_at", "updated_at"]
        )
        return intent

    def assert_one_event(self, submission, operation):
        self.assertEqual(
            SubmissionLifecycleEvent.objects.filter(
                submission=submission, operation=operation
            ).count(),
            1,
        )

    def test_simultaneous_withdrawals_serialize_to_one_transition(self):
        submission = self.create_pending()
        outcomes = self.run_paused_first(
            lambda: withdraw_submission(
                self.actor(self.owner), submission.pk, "withdraw-race-first"
            ).replayed,
            lambda: withdraw_submission(
                self.actor(self.owner), submission.pk, "withdraw-race-second"
            ).replayed,
            service=moderation_services,
            lock_name="_locked_submission",
        )

        self.assertEqual(outcomes["first"], ("ok", False))
        self.assertEqual(outcomes["second"][0], SubmissionStateError.__name__)
        submission.refresh_from_db()
        self.assertEqual(submission.state, Request.State.WITHDRAWN)
        self.assert_one_event(submission, SubmissionOperation.WITHDRAW)

    def test_simultaneous_rejections_serialize_to_one_transition(self):
        submission = self.create_pending()
        outcomes = self.run_paused_first(
            lambda: reject_submission(
                self.actor(self.moderator), submission.pk, "reject-race-first"
            ).replayed,
            lambda: reject_submission(
                self.actor(self.administrator), submission.pk, "reject-race-second"
            ).replayed,
            service=moderation_services,
            lock_name="_locked_submission",
        )

        self.assertEqual(outcomes["first"], ("ok", False))
        self.assertEqual(outcomes["second"][0], SubmissionStateError.__name__)
        submission.refresh_from_db()
        self.assertEqual(submission.state, Request.State.REJECTED)
        self.assert_one_event(submission, SubmissionOperation.REJECT)

    def test_finalize_and_withdrawal_are_linearizable_in_both_orders(self):
        finalize_first = self.create_draft(name="Finalize wins")
        outcomes = self.run_paused_first(
            lambda: finalize_submission(
                self.actor(self.owner), finalize_first.pk, "finalize-race-first"
            )[1],
            lambda: withdraw_submission(
                self.actor(self.owner), finalize_first.pk, "withdraw-after-finalize"
            ).replayed,
            service=submission_services,
            lock_name="_locked_owned_submission",
        )
        self.assertEqual(outcomes["first"], ("ok", False))
        self.assertEqual(outcomes["second"], ("ok", False))
        finalize_first.refresh_from_db()
        self.assertEqual(finalize_first.state, Request.State.WITHDRAWN)
        self.assert_one_event(finalize_first, SubmissionOperation.FINALIZE)
        self.assert_one_event(finalize_first, SubmissionOperation.WITHDRAW)

        withdrawal_first = self.create_draft(name="Withdrawal wins", longitude=10)
        outcomes = self.run_paused_first(
            lambda: withdraw_submission(
                self.actor(self.owner), withdrawal_first.pk, "withdraw-race-first"
            ).replayed,
            lambda: finalize_submission(
                self.actor(self.owner), withdrawal_first.pk, "finalize-after-withdraw"
            )[1],
            service=moderation_services,
            lock_name="_locked_submission",
        )
        self.assertEqual(outcomes["first"], ("ok", False))
        self.assertEqual(outcomes["second"][0], SubmissionStateError.__name__)
        withdrawal_first.refresh_from_db()
        self.assertEqual(withdrawal_first.state, Request.State.WITHDRAWN)
        self.assertFalse(
            SubmissionLifecycleEvent.objects.filter(
                submission=withdrawal_first,
                operation=SubmissionOperation.FINALIZE,
            ).exists()
        )
        self.assert_one_event(withdrawal_first, SubmissionOperation.WITHDRAW)

    def test_withdrawal_and_expiry_are_linearizable_in_both_orders(self):
        withdrawal_first = self.make_due(
            self.create_draft(name="Withdrawal before expiry")
        )
        outcomes = self.run_paused_first(
            lambda: withdraw_submission(
                self.actor(self.owner), withdrawal_first.pk, "withdraw-before-expiry"
            ).replayed,
            lambda: process_submission_expiry(now=timezone.now()).expired,
            service=moderation_services,
            lock_name="_locked_submission",
        )
        self.assertEqual(outcomes["first"], ("ok", False))
        self.assertEqual(outcomes["second"], ("ok", 0))
        withdrawal_first.refresh_from_db()
        self.assertEqual(withdrawal_first.state, Request.State.WITHDRAWN)
        self.assert_one_event(withdrawal_first, SubmissionOperation.WITHDRAW)
        self.assertFalse(
            SubmissionLifecycleEvent.objects.filter(
                submission=withdrawal_first,
                operation=SubmissionOperation.EXPIRE,
            ).exists()
        )

        expiry_first = self.make_due(
            self.create_draft(name="Expiry before withdrawal", longitude=10)
        )
        outcomes = self.run_paused_first(
            lambda: process_submission_expiry(now=timezone.now()).expired,
            lambda: withdraw_submission(
                self.actor(self.owner), expiry_first.pk, "withdraw-after-expiry"
            ).replayed,
            service=expiry_services,
            lock_name="_handoff_media_for_expired_submission",
        )
        self.assertEqual(outcomes["first"], ("ok", 1))
        self.assertEqual(outcomes["second"][0], SubmissionStateError.__name__)
        expiry_first.refresh_from_db()
        self.assertEqual(expiry_first.state, Request.State.EXPIRED)
        self.assert_one_event(expiry_first, SubmissionOperation.EXPIRE)
        self.assertFalse(
            SubmissionLifecycleEvent.objects.filter(
                submission=expiry_first,
                operation=SubmissionOperation.WITHDRAW,
            ).exists()
        )

    def assert_review_race(self, first_operation, second_operation, expected_state):
        submission = self.create_pending(
            name=f"{first_operation} before {second_operation}"
        )
        operations = {
            "approve": lambda key: approve_submission(
                self.actor(self.moderator), submission.pk, key
            ).replayed,
            "reject": lambda key: reject_submission(
                self.actor(self.moderator), submission.pk, key
            ).replayed,
            "withdraw": lambda key: withdraw_submission(
                self.actor(self.owner), submission.pk, key
            ).replayed,
        }
        outcomes = self.run_paused_first(
            lambda: operations[first_operation](f"{first_operation}-race-first"),
            lambda: operations[second_operation](f"{second_operation}-race-second"),
            service=moderation_services,
            lock_name="_locked_submission",
        )
        self.assertEqual(outcomes["first"], ("ok", False))
        self.assertEqual(outcomes["second"][0], SubmissionStateError.__name__)
        submission.refresh_from_db()
        self.assertEqual(submission.state, expected_state)
        self.assert_one_event(
            submission, getattr(SubmissionOperation, first_operation.upper())
        )
        self.assertFalse(
            SubmissionLifecycleEvent.objects.filter(
                submission=submission,
                operation=getattr(SubmissionOperation, second_operation.upper()),
            ).exists()
        )

    def test_approval_and_rejection_are_linearizable_in_both_orders(self):
        self.assert_review_race("approve", "reject", Request.State.APPROVED)
        self.assert_review_race("reject", "approve", Request.State.REJECTED)

    def test_approval_and_withdrawal_are_linearizable_in_both_orders(self):
        self.assert_review_race("approve", "withdraw", Request.State.APPROVED)
        self.assert_review_race("withdraw", "approve", Request.State.WITHDRAWN)

    def test_rejection_and_withdrawal_are_linearizable_in_both_orders(self):
        self.assert_review_race("reject", "withdraw", Request.State.REJECTED)
        self.assert_review_race("withdraw", "reject", Request.State.WITHDRAWN)

    def test_simultaneous_approval_of_one_submission_materializes_once(self):
        submission = self.create_pending()
        outcomes = self.run_paused_first(
            lambda: approve_submission(
                self.actor(self.moderator), submission.pk, "approval-race-first"
            ).place.pk,
            lambda: approve_submission(
                self.actor(self.administrator), submission.pk, "approval-race-second"
            ).place.pk,
            service=moderation_services,
            lock_name="_locked_submission",
        )

        self.assertEqual(outcomes["first"][0], "ok")
        self.assertEqual(outcomes["second"][0], SubmissionStateError.__name__)
        self.assertEqual(Place.objects.count(), 1)
        self.assert_one_event(submission, SubmissionOperation.APPROVE)

    def test_same_canonical_name_approvals_cannot_both_pass_duplicate_check(self):
        first = self.create_pending(name="Contested Name")
        second = self.create_pending(
            owner=self.other_owner,
            name="  contested\tname ",
            longitude=-77.03649,
        )
        outcomes = self.run_paused_first(
            lambda: approve_submission(
                self.actor(self.moderator), first.pk, "canonical-race-first"
            ).place.pk,
            lambda: approve_submission(
                self.actor(self.administrator), second.pk, "canonical-race-second"
            ).place.pk,
            service=moderation_services,
            lock_name="acquire_canonical_name_lock",
        )

        self.assertEqual(outcomes["first"][0], "ok")
        self.assertEqual(outcomes["second"][0], DuplicateSubmission.__name__)
        self.assertEqual(Place.objects.count(), 1)
        self.assertEqual(
            SubmissionLifecycleEvent.objects.filter(
                operation=SubmissionOperation.APPROVE
            ).count(),
            1,
        )

    def test_withdrawal_preserves_an_in_flight_media_cleanup_handoff(self):
        submission = self.create_draft(name="Cleanup claim race")
        intent = self.make_cleanup_pending_media(submission)
        cleanup_started = threading.Event()
        release_cleanup = threading.Event()
        outcomes = Queue()
        storage = Mock()
        storage.object_is_absent.return_value = True

        def paused_delete(**kwargs):
            if not cleanup_started.is_set():
                cleanup_started.set()
                if not release_cleanup.wait(timeout=20):
                    raise AssertionError("timed out waiting to release cleanup")

        storage.delete_object.side_effect = paused_delete

        def cleanup_worker():
            close_old_connections()
            try:
                counts = process_media_cleanup(storage=storage, now=timezone.now())
                outcomes.put(
                    (
                        "cleanup",
                        "ok",
                        counts.claimed,
                        counts.deleted,
                        counts.skipped,
                    )
                )
            except Exception as error:
                outcomes.put(("cleanup", type(error).__name__, str(error)))
            finally:
                close_old_connections()

        cleanup_thread = threading.Thread(
            target=cleanup_worker, name="moderation-media-cleanup", daemon=True
        )
        cleanup_thread.start()
        try:
            self.assertTrue(cleanup_started.wait(timeout=10))
            result = withdraw_submission(
                self.owner, submission.pk, "withdraw-during-cleanup"
            )
            self.assertFalse(result.replayed)
        finally:
            release_cleanup.set()
        cleanup_thread.join(timeout=30)
        self.assertFalse(cleanup_thread.is_alive())

        observed = outcomes.get(timeout=1)
        self.assertEqual(observed, ("cleanup", "ok", 1, 1, 0))
        submission.refresh_from_db()
        intent.refresh_from_db()
        self.assertEqual(submission.state, Request.State.WITHDRAWN)
        self.assertEqual(intent.state, MediaUploadIntent.State.DELETED)
        self.assertIsNone(intent.cleanup_claim_token)
        self.assert_one_event(submission, SubmissionOperation.WITHDRAW)
        self.assertEqual(storage.delete_object.call_count, 2)
