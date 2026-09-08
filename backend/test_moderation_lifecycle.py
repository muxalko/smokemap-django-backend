import hashlib
import threading
import uuid
from datetime import timedelta
from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from graphene.test import Client as GraphQLClient

from .models import (
    Image,
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
    hard_delete_submission,
    reject_submission,
    withdraw_submission,
)
from .schema import schema
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
        event = SubmissionLifecycleEvent.objects.get(
            submission=submission, operation=SubmissionOperation.APPROVE
        )
        self.assertEqual(event.actor_id, self.moderator.pk)
        self.assertEqual(event.from_state, Request.State.PENDING)
        self.assertEqual(event.to_state, Request.State.APPROVED)

        replay = approve_submission(
            self.moderator, submission.pk, "approve-key", "Looks good"
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.place.pk, result.place.pk)
        self.assertEqual(Place.objects.filter(name=submission.name).count(), 1)

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


@override_settings(**PRIVATE_MEDIA_SETTINGS)
class ModerationRaceTests(ModerationFixtureMixin, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.build_fixtures()

    def test_simultaneous_approval_of_one_submission_materializes_once(self):
        submission = self.create_pending()
        barrier = threading.Barrier(2)
        outcomes = Queue()

        def approve(key):
            close_old_connections()
            try:
                reviewer_id = (
                    self.moderator.pk if key.endswith("0") else self.administrator.pk
                )
                actor = get_user_model().objects.get(pk=reviewer_id)
                barrier.wait(timeout=10)
                result = approve_submission(actor, submission.pk, key)
                outcomes.put(("approved", result.place.pk))
            except Exception as error:  # asserted below with the database outcome
                outcomes.put((type(error).__name__, str(error)))
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=approve, args=(f"race-{index}",), daemon=True)
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive())

        observed = [outcomes.get(timeout=1) for _ in range(2)]
        self.assertEqual(sum(item[0] == "approved" for item in observed), 1)
        self.assertEqual(
            sum(item[0] == SubmissionStateError.__name__ for item in observed), 1
        )
        self.assertEqual(Place.objects.count(), 1)
        self.assertEqual(
            SubmissionLifecycleEvent.objects.filter(
                submission=submission, operation=SubmissionOperation.APPROVE
            ).count(),
            1,
        )

    def test_same_canonical_name_approvals_cannot_both_pass_duplicate_check(self):
        first = self.create_pending(name="Contested Name")
        second = self.create_pending(
            owner=self.other_owner,
            name="  contested\tname ",
            longitude=-77.03649,
        )
        barrier = threading.Barrier(2)
        outcomes = Queue()

        def approve(submission_id, key):
            close_old_connections()
            try:
                reviewer_id = (
                    self.moderator.pk if key.endswith("0") else self.administrator.pk
                )
                actor = get_user_model().objects.get(pk=reviewer_id)
                barrier.wait(timeout=10)
                result = approve_submission(actor, submission_id, key)
                outcomes.put(("approved", result.place.pk))
            except Exception as error:  # asserted below with the database outcome
                outcomes.put((type(error).__name__, str(error)))
            finally:
                close_old_connections()

        threads = [
            threading.Thread(
                target=approve,
                args=(submission_id, f"canonical-race-{index}"),
                daemon=True,
            )
            for index, submission_id in enumerate((first.pk, second.pk))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive())

        observed = [outcomes.get(timeout=1) for _ in range(2)]
        self.assertEqual(sum(item[0] == "approved" for item in observed), 1)
        self.assertEqual(
            sum(item[0] == DuplicateSubmission.__name__ for item in observed), 1
        )
        self.assertEqual(Place.objects.count(), 1)
        self.assertEqual(
            SubmissionLifecycleEvent.objects.filter(
                operation=SubmissionOperation.APPROVE
            ).count(),
            1,
        )
