import threading
import uuid
from datetime import timedelta
from queue import Queue
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from . import media as media_services
from . import submission_expiry as expiry_services
from . import submissions as submission_services
from .media import (
    MediaStateConflict,
    attach_verified_media,
    cleanup_media_object,
    create_upload_intent,
    process_media_cleanup,
    remove_attached_media,
)
from .models import (
    Category,
    Image,
    MediaUploadIntent,
    Request,
    SubmissionIdempotency,
    SubmissionOperation,
)
from .submission_expiry import process_submission_expiry
from .submissions import (
    IdempotencyConflict,
    SubmissionInputError,
    SubmissionNotFound,
    SubmissionStateError,
    finalize_submission,
    reorder_attached_media,
    submission_media_state,
)
from .test_media import FakeMediaStorage, encoded_image
from .test_submission_finalization import PRIVATE_MEDIA_SETTINGS, SubmissionFixtureMixin


SUBMISSION_MEDIA_STATE_QUERY = """
    query MediaState($id: ID!) {
      submissionMediaStateV3(submissionId: $id) {
        submission { id state name }
        attachments { id position state byteSize mediaIntentId }
        mediaIntents { id state slot }
      }
    }
"""

REMOVE_ATTACHED_MEDIA = """
    mutation Remove($id: ID!, $key: String!) {
      removeAttachedMedia(intentId: $id, idempotencyKey: $key) {
        intent { id state failureCode }
        replayed
      }
    }
"""

REORDER_SUBMISSION_MEDIA = """
    mutation Reorder($id: ID!, $key: String!, $ids: [ID!]!) {
      reorderSubmissionMediaV3(submissionId: $id, idempotencyKey: $key, attachmentIds: $ids) {
        orderedAttachmentIds
        replayed
      }
    }
"""


@override_settings(**PRIVATE_MEDIA_SETTINGS)
class SubmissionMediaStateTests(SubmissionFixtureMixin, TestCase):
    category_slug = "outdoors"

    def setUp(self):
        self.build_users()
        self.submission = self.create_draft()

    def test_owner_draft_resume_returns_submission_plus_non_sensitive_media(self):
        intent, image = self.attach_managed_image(self.submission, 0)
        pending_intent = self.create_intent(
            self.submission, 1, MediaUploadIntent.State.VERIFIED
        )

        state = submission_media_state(self.owner, self.submission.pk)

        self.assertEqual(state.submission.submission_id, self.submission.pk)
        self.assertEqual(state.submission.state, Request.State.DRAFT)
        self.assertEqual([item.pk for item in state.attachments], [image.pk])
        self.assertEqual(
            sorted(item.pk for item in state.media_intents),
            sorted([intent.pk, pending_intent.pk]),
        )

    def test_attachment_exposes_its_actual_related_intent_id(self):
        intent, image = self.attach_managed_image(self.submission, 0)

        state = submission_media_state(self.owner, self.submission.pk)

        [attachment] = state.attachments
        self.assertEqual(attachment.pk, image.pk)
        # The relation is read straight off the row's own FK, not inferred
        # from slot/position agreement with a sibling intent.
        self.assertEqual(attachment.intent_id, intent.pk)

    def test_pending_submission_is_resumable(self):
        finalize_submission(self.owner, self.submission.pk, "resume-pending-finalize")
        state = submission_media_state(self.owner, self.submission.pk)
        self.assertEqual(state.submission.state, Request.State.PENDING)

    def test_terminal_states_fail_closed_as_not_found(self):
        for terminal in (
            Request.State.WITHDRAWN,
            Request.State.EXPIRED,
            Request.State.APPROVED,
            Request.State.REJECTED,
        ):
            with self.subTest(state=terminal):
                submission = self.create_draft(name=f"Terminal {terminal}")
                submission.state = terminal
                update_fields = ["state"]
                if terminal == Request.State.APPROVED:
                    submission.approved = True
                    update_fields.append("approved")
                submission.save(update_fields=update_fields)
                with self.assertRaises(SubmissionNotFound):
                    submission_media_state(self.owner, submission.pk)

    def test_owner_isolation_is_not_found_not_forbidden(self):
        with self.assertRaises(SubmissionNotFound):
            submission_media_state(self.other_owner, self.submission.pk)

    def test_missing_and_unparsable_submission_ids_are_not_found(self):
        for identifier in (0, "not-an-id", None):
            with self.subTest(identifier=identifier):
                with self.assertRaises(SubmissionNotFound):
                    submission_media_state(self.owner, identifier)

    def test_inactive_actor_is_denied(self):
        from .submissions import SubmissionAuthenticationRequired

        with self.assertRaises(SubmissionAuthenticationRequired):
            submission_media_state(self.inactive, self.submission.pk)

    def test_graphql_resume_exposes_no_storage_secrets(self):
        self.attach_managed_image(self.submission, 0)
        result = self.graphql.execute(
            SUBMISSION_MEDIA_STATE_QUERY,
            variable_values={"id": str(self.submission.pk)},
            context_value=self.context(self.owner),
        )

        self.assertNotIn("errors", result)
        payload = result["data"]["submissionMediaStateV3"]
        self.assertEqual(payload["submission"]["state"], "draft")
        self.assertEqual(len(payload["attachments"]), 1)
        schema_text = str(self.graphql.schema)
        self.assertNotIn("storageKey", schema_text)
        self.assertNotIn("storageBucket", schema_text)
        self.assertNotIn("sealedObjectKey", schema_text)
        self.assertNotIn("objectKey", schema_text)

    def test_graphql_attachments_expose_their_own_real_intent_id(self):
        first_intent, first_image = self.attach_managed_image(self.submission, 2)
        second_intent, second_image = self.attach_managed_image(self.submission, 0)

        result = self.graphql.execute(
            SUBMISSION_MEDIA_STATE_QUERY,
            variable_values={"id": str(self.submission.pk)},
            context_value=self.context(self.owner),
        )

        self.assertNotIn("errors", result)
        attachments = result["data"]["submissionMediaStateV3"]["attachments"]
        by_id = {item["id"]: item["mediaIntentId"] for item in attachments}
        self.assertEqual(by_id[str(first_image.pk)], str(first_intent.pk))
        self.assertEqual(by_id[str(second_image.pk)], str(second_intent.pk))

    def test_graphql_owner_isolation_reports_not_found(self):
        result = self.graphql.execute(
            SUBMISSION_MEDIA_STATE_QUERY,
            variable_values={"id": str(self.submission.pk)},
            context_value=self.context(self.other_owner),
        )
        self.assertEqual(self.error_code(result), "NOT_FOUND")


@override_settings(**PRIVATE_MEDIA_SETTINGS)
class RemoveAttachedMediaTests(SubmissionFixtureMixin, TransactionTestCase):
    # A TransactionTestCase, not TestCase: several tests below call
    # process_media_cleanup, whose FakeMediaStorage asserts storage calls
    # happen outside any DB transaction -- an invariant TestCase's own
    # per-test wrapping transaction would otherwise always violate.
    #
    # A TransactionTestCase must not depend on migration-seeded rows: once
    # any TransactionTestCase in the run flushes the database, that seed
    # data is gone for the rest of the process, so the category is created
    # locally instead of reusing the "outdoors" class-attribute convention.
    category_slug = "remove-media-test"

    def setUp(self):
        self.build_users()
        Category.objects.get_or_create(
            slug=self.category_slug, defaults={"name": "Remove media test"}
        )
        self.submission = self.create_draft()

    def test_removal_deletes_attachment_hands_off_cleanup_and_frees_slot(self):
        intent, image = self.attach_managed_image(self.submission, 1)

        removed, replayed = remove_attached_media(self.owner, intent.pk, "remove-1")

        self.assertFalse(replayed)
        self.assertEqual(removed.state, MediaUploadIntent.State.CLEANUP_PENDING)
        self.assertEqual(removed.failure_code, "media_removed")
        self.assertIsNone(removed.cleanup_claim_token)
        self.assertFalse(Image.objects.filter(pk=image.pk).exists())
        # The exact bound object survives on the intent for the handoff.
        self.assertEqual(removed.storage_bucket, intent.storage_bucket)
        self.assertEqual(removed.sealed_object_key, intent.sealed_object_key)

        # Slot 1 is free again: a brand-new intent can claim it.
        replacement = self.create_intent(
            self.submission, 1, MediaUploadIntent.State.CREATED
        )
        self.assertEqual(replacement.slot, 1)

    def test_same_key_replay_is_idempotent_and_stable_after_state_change(self):
        intent, _image = self.attach_managed_image(self.submission, 0)
        first, replayed_first = remove_attached_media(self.owner, intent.pk, "remove-replay")
        self.assertFalse(replayed_first)

        # Advance the submission out of draft; the replay must still succeed.
        self.submission.state = Request.State.PENDING
        self.submission.save(update_fields=["state"])

        second, replayed_second = remove_attached_media(
            self.owner, intent.pk, "remove-replay"
        )
        self.assertTrue(replayed_second)
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(second.state, MediaUploadIntent.State.CLEANUP_PENDING)

    def test_same_key_different_intent_is_idempotency_conflict(self):
        first_intent, _first_image = self.attach_managed_image(self.submission, 0)
        second_intent, _second_image = self.attach_managed_image(self.submission, 1)
        remove_attached_media(self.owner, first_intent.pk, "shared-key")

        with self.assertRaises(IdempotencyConflict):
            remove_attached_media(self.owner, second_intent.pk, "shared-key")

        self.assertTrue(Image.objects.filter(intent=second_intent).exists())

    def test_non_owner_is_not_found(self):
        intent, _image = self.attach_managed_image(self.submission, 0)
        with self.assertRaises(MediaStateConflict) as caught:
            remove_attached_media(self.other_owner, intent.pk, "other-owner-remove")
        self.assertEqual(caught.exception.code, "NOT_FOUND")
        self.assertTrue(Image.objects.filter(intent=intent).exists())

    def test_non_draft_submission_fails_closed(self):
        intent, _image = self.attach_managed_image(self.submission, 0)
        self.submission.state = Request.State.PENDING
        self.submission.save(update_fields=["state"])

        with self.assertRaises(MediaStateConflict) as caught:
            remove_attached_media(self.owner, intent.pk, "not-draft-remove")
        self.assertNotEqual(caught.exception.code, "NOT_FOUND")
        self.assertTrue(Image.objects.filter(intent=intent).exists())

    def test_non_attached_intent_is_rejected(self):
        intent = self.create_intent(
            self.submission, 0, MediaUploadIntent.State.VERIFIED
        )
        with self.assertRaises(MediaStateConflict):
            remove_attached_media(self.owner, intent.pk, "not-attached-remove")

    def test_missing_attachment_evidence_is_a_stable_conflict(self):
        intent, image = self.attach_managed_image(self.submission, 0)
        # Simulate corrupted evidence: the intent claims ATTACHED with no row.
        Image.objects.filter(pk=image.pk).delete()

        with self.assertRaises(MediaStateConflict) as caught:
            remove_attached_media(self.owner, intent.pk, "missing-evidence-remove")
        self.assertIn("incomplete", str(caught.exception))

    def test_missing_intent_is_not_found(self):
        with self.assertRaises(MediaStateConflict) as caught:
            remove_attached_media(self.owner, uuid.uuid4(), "missing-intent-remove")
        self.assertEqual(caught.exception.code, "NOT_FOUND")

    def test_cleanup_deletes_exact_bound_objects_only(self):
        intent, image = self.attach_managed_image(self.submission, 0)
        other_intent, other_image = self.attach_managed_image(self.submission, 1)
        storage = FakeMediaStorage(
            {
                intent.object_key: b"upload",
                intent.sealed_object_key: b"sealed",
                other_intent.object_key: b"other-upload",
                other_intent.sealed_object_key: b"other-sealed",
            }
        )

        remove_attached_media(self.owner, intent.pk, "remove-cleanup")
        counts = process_media_cleanup(storage=storage)

        self.assertEqual(counts.deleted, 1)
        self.assertNotIn(intent.object_key, storage.objects)
        self.assertNotIn(intent.sealed_object_key, storage.objects)
        # The sibling attachment's bound objects are untouched.
        self.assertIn(other_intent.object_key, storage.objects)
        self.assertIn(other_intent.sealed_object_key, storage.objects)
        self.assertTrue(Image.objects.filter(pk=other_image.pk).exists())
        intent.refresh_from_db()
        self.assertEqual(intent.state, MediaUploadIntent.State.DELETED)

    def test_finalize_requires_removed_intent_to_finish_cleanup_first(self):
        intent, _image = self.attach_managed_image(self.submission, 0)
        remove_attached_media(self.owner, intent.pk, "remove-before-finalize")

        from .submissions import MediaNotReady

        with self.assertRaises(MediaNotReady):
            finalize_submission(self.owner, self.submission.pk, "finalize-too-soon")

        storage = FakeMediaStorage(
            {intent.object_key: b"x", intent.sealed_object_key: b"y"}
        )
        process_media_cleanup(storage=storage)

        result, replayed = finalize_submission(
            self.owner, self.submission.pk, "finalize-after-cleanup"
        )
        self.assertFalse(replayed)
        self.assertEqual(result.state, Request.State.PENDING)

    def test_graphql_mutation_maps_stable_error_codes(self):
        intent, _image = self.attach_managed_image(self.submission, 0)
        result = self.graphql.execute(
            REMOVE_ATTACHED_MEDIA,
            variable_values={"id": str(intent.pk), "key": "graphql-remove-denied"},
            context_value=self.context(self.other_owner),
        )
        self.assertEqual(self.error_code(result), "NOT_FOUND")

        result = self.graphql.execute(
            REMOVE_ATTACHED_MEDIA,
            variable_values={"id": str(intent.pk), "key": "graphql-remove-ok"},
            context_value=self.context(self.owner),
        )
        self.assertNotIn("errors", result)
        payload = result["data"]["removeAttachedMedia"]
        self.assertFalse(payload["replayed"])
        self.assertEqual(payload["intent"]["state"], "cleanup_pending")
        self.assertEqual(payload["intent"]["failureCode"], "media_removed")


@override_settings(**PRIVATE_MEDIA_SETTINGS)
class ReorderAttachedMediaTests(SubmissionFixtureMixin, TestCase):
    category_slug = "outdoors"

    def setUp(self):
        self.build_users()
        self.submission = self.create_draft()
        self.images = [
            self.attach_managed_image(self.submission, slot)[1] for slot in range(3)
        ]

    def positions(self):
        return {
            image.pk: Image.objects.get(pk=image.pk).position
            for image in self.images
        }

    def test_full_permutation_is_applied_atomically(self):
        rotated = [self.images[2].pk, self.images[0].pk, self.images[1].pk]

        ordered, replayed = reorder_attached_media(
            self.owner, self.submission.pk, "reorder-rotate", rotated
        )

        self.assertFalse(replayed)
        self.assertEqual(list(ordered), [str(value) for value in rotated])
        positions = self.positions()
        self.assertEqual(positions[self.images[2].pk], 0)
        self.assertEqual(positions[self.images[0].pk], 1)
        self.assertEqual(positions[self.images[1].pk], 2)

    def test_two_way_swap_is_applied(self):
        swapped = [self.images[1].pk, self.images[0].pk, self.images[2].pk]
        reorder_attached_media(self.owner, self.submission.pk, "reorder-swap", swapped)
        positions = self.positions()
        self.assertEqual(positions[self.images[1].pk], 0)
        self.assertEqual(positions[self.images[0].pk], 1)
        self.assertEqual(positions[self.images[2].pk], 2)

    def test_identity_permutation_is_a_no_op(self):
        identity = [image.pk for image in self.images]
        reorder_attached_media(self.owner, self.submission.pk, "reorder-identity", identity)
        self.assertEqual(self.positions(), {image.pk: index for index, image in enumerate(self.images)})

    def test_same_key_replay_is_idempotent(self):
        rotated = [self.images[2].pk, self.images[0].pk, self.images[1].pk]
        first, replayed_first = reorder_attached_media(
            self.owner, self.submission.pk, "reorder-replay", rotated
        )
        self.assertFalse(replayed_first)
        second, replayed_second = reorder_attached_media(
            self.owner, self.submission.pk, "reorder-replay", rotated
        )
        self.assertTrue(replayed_second)
        self.assertEqual(second, first)

    def test_same_key_different_payload_is_idempotency_conflict(self):
        first = [self.images[0].pk, self.images[1].pk, self.images[2].pk]
        second = [self.images[2].pk, self.images[1].pk, self.images[0].pk]
        reorder_attached_media(self.owner, self.submission.pk, "reorder-conflict", first)

        with self.assertRaises(IdempotencyConflict):
            reorder_attached_media(
                self.owner, self.submission.pk, "reorder-conflict", second
            )

    def test_subset_extra_foreign_and_duplicate_ids_are_rejected(self):
        foreign_submission = self.create_draft(
            owner=self.other_owner, name="Foreign media source"
        )
        _foreign_intent, foreign_image = self.attach_managed_image(
            foreign_submission, 0, owner=self.other_owner
        )
        invalid_payloads = [
            [self.images[0].pk, self.images[1].pk],  # subset
            [image.pk for image in self.images] + [foreign_image.pk],  # extra
            [self.images[0].pk, foreign_image.pk, self.images[2].pk],  # foreign swap-in
            [self.images[0].pk, self.images[0].pk, self.images[2].pk],  # duplicate
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(SubmissionInputError):
                    reorder_attached_media(
                        self.owner, self.submission.pk, f"reorder-bad-{payload}", payload
                    )
        self.assertEqual(
            self.positions(), {image.pk: index for index, image in enumerate(self.images)}
        )

    def test_non_list_and_oversized_payloads_are_rejected(self):
        with self.assertRaises(SubmissionInputError):
            reorder_attached_media(
                self.owner, self.submission.pk, "reorder-not-list", "not-a-list"
            )
        with self.assertRaises(SubmissionInputError):
            reorder_attached_media(
                self.owner, self.submission.pk, "reorder-oversized",
                [image.pk for image in self.images] + [999999, 999998],
            )

    def test_non_draft_submission_is_rejected(self):
        finalize_submission(self.owner, self.submission.pk, "reorder-finalize-first")
        ids = [image.pk for image in self.images]
        with self.assertRaises(SubmissionStateError):
            reorder_attached_media(self.owner, self.submission.pk, "reorder-not-draft", ids)

    def test_non_owner_is_not_found(self):
        ids = [image.pk for image in self.images]
        with self.assertRaises(SubmissionNotFound):
            reorder_attached_media(
                self.other_owner, self.submission.pk, "reorder-other-owner", ids
            )

    def test_empty_retained_set_reorders_trivially(self):
        empty_submission = self.create_draft(name="No media draft")
        ordered, replayed = reorder_attached_media(
            self.owner, empty_submission.pk, "reorder-empty", []
        )
        self.assertEqual(ordered, ())
        self.assertFalse(replayed)

    def test_media_intent_id_survives_reorder_without_slot_position_inference(self):
        # Reorder permutes position only; each intent's slot stays put at its
        # original assignment, so after this rotation no attachment's new
        # position agrees with its own intent's slot any more -- pairing by
        # slot/position agreement would misidentify every attachment here.
        original_intents = {image.pk: image.intent_id for image in self.images}
        rotated = [self.images[2].pk, self.images[0].pk, self.images[1].pk]
        reorder_attached_media(self.owner, self.submission.pk, "reorder-intent-id", rotated)

        state = submission_media_state(self.owner, self.submission.pk)
        for attachment in state.attachments:
            self.assertNotEqual(
                attachment.position,
                MediaUploadIntent.objects.get(pk=attachment.intent_id).slot,
                "fixture no longer demonstrates a slot/position mismatch",
            )
            self.assertEqual(attachment.intent_id, original_intents[attachment.pk])

    def test_graphql_reorder_keeps_media_intent_id_bound_to_the_right_attachment(self):
        original_intents = {image.pk: image.intent_id for image in self.images}
        rotated = [self.images[2].pk, self.images[0].pk, self.images[1].pk]
        reorder_attached_media(self.owner, self.submission.pk, "reorder-intent-id-gql", rotated)

        result = self.graphql.execute(
            SUBMISSION_MEDIA_STATE_QUERY,
            variable_values={"id": str(self.submission.pk)},
            context_value=self.context(self.owner),
        )

        self.assertNotIn("errors", result)
        attachments = result["data"]["submissionMediaStateV3"]["attachments"]
        self.assertEqual(len(attachments), 3)
        for item in attachments:
            image_pk = int(item["id"])
            self.assertEqual(item["mediaIntentId"], str(original_intents[image_pk]))

    def test_removal_targets_the_correct_attachment_after_reorder(self):
        # The intent id read off the state (not the attachment's post-reorder
        # position) is what identifies which image removal must delete.
        rotated = [self.images[2].pk, self.images[0].pk, self.images[1].pk]
        reorder_attached_media(self.owner, self.submission.pk, "reorder-before-remove", rotated)

        target_intent_id = self.images[1].intent_id
        sibling_ids = {self.images[0].pk, self.images[2].pk}

        removed, replayed = remove_attached_media(
            self.owner, target_intent_id, "remove-after-reorder"
        )

        self.assertFalse(replayed)
        self.assertEqual(removed.pk, target_intent_id)
        self.assertFalse(Image.objects.filter(pk=self.images[1].pk).exists())
        self.assertEqual(
            set(
                Image.objects.filter(
                    request=self.submission, is_managed=True, state="attached"
                ).values_list("pk", flat=True)
            ),
            sibling_ids,
        )

    def test_finalize_after_reorder_preserves_new_positions(self):
        rotated = [self.images[2].pk, self.images[0].pk, self.images[1].pk]
        reorder_attached_media(self.owner, self.submission.pk, "reorder-then-finalize", rotated)

        result, _replayed = finalize_submission(
            self.owner, self.submission.pk, "finalize-after-reorder"
        )
        self.assertEqual(result.state, Request.State.PENDING)
        positions = self.positions()
        self.assertEqual(positions[self.images[2].pk], 0)
        self.assertEqual(positions[self.images[0].pk], 1)
        self.assertEqual(positions[self.images[1].pk], 2)

    def test_graphql_mutation_round_trips_and_maps_errors(self):
        result = self.graphql.execute(
            REORDER_SUBMISSION_MEDIA,
            variable_values={
                "id": str(self.submission.pk),
                "key": "graphql-reorder-denied",
                "ids": [str(self.images[0].pk)],
            },
            context_value=self.context(self.other_owner),
        )
        self.assertEqual(self.error_code(result), "NOT_FOUND")

        rotated = [str(self.images[1].pk), str(self.images[2].pk), str(self.images[0].pk)]
        result = self.graphql.execute(
            REORDER_SUBMISSION_MEDIA,
            variable_values={
                "id": str(self.submission.pk),
                "key": "graphql-reorder-ok",
                "ids": rotated,
            },
            context_value=self.context(self.owner),
        )
        self.assertNotIn("errors", result)
        payload = result["data"]["reorderSubmissionMediaV3"]
        self.assertFalse(payload["replayed"])
        self.assertEqual(payload["orderedAttachmentIds"], rotated)


@override_settings(**PRIVATE_MEDIA_SETTINGS)
class DraftMediaResumeRaceTests(SubmissionFixtureMixin, TransactionTestCase):
    category_slug = "draft-media-race-test"

    def setUp(self):
        self.build_users()
        Category.objects.get_or_create(
            slug=self.category_slug, defaults={"name": "Race test"}
        )

    def run_threads(self, targets):
        threads = [threading.Thread(target=target) for target in targets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertTrue(all(not thread.is_alive() for thread in threads))

    def test_remove_serializes_with_finalize_on_the_submission_row(self):
        submission = self.create_draft(name="Race remove vs finalize")
        intent, _image = self.attach_managed_image(submission, 0)
        barrier = threading.Barrier(2, timeout=60)
        outcomes = Queue()
        owner_id = self.owner.pk
        submission_id = submission.pk
        intent_id = intent.pk

        def remove_worker():
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=owner_id)
                barrier.wait()
                remove_attached_media(actor, intent_id, "race-remove")
                outcomes.put(("remove", None))
            except Exception as error:
                outcomes.put(("remove", type(error).__name__))
            finally:
                close_old_connections()

        def finalize_worker():
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=owner_id)
                barrier.wait()
                finalize_submission(actor, submission_id, "race-finalize")
                outcomes.put(("finalize", None))
            except Exception as error:
                outcomes.put(("finalize", type(error).__name__))
            finally:
                close_old_connections()

        self.run_threads([remove_worker, finalize_worker])
        results = dict(outcomes.get_nowait() for _index in range(2))

        # Exactly one lock winner: either the removal wins (finalize then sees
        # not-ready media, since cleanup has not run) or finalize wins first
        # (locking the submission before the removal can start, so the
        # removal proceeds afterward against an already-pending submission).
        self.assertIn(results["remove"], (None, "MediaStateConflict"))
        if results["remove"] is None:
            self.assertEqual(results["finalize"], "MediaNotReady")
        else:
            self.assertIsNone(results["finalize"])

    def test_remove_serializes_with_draft_expiry(self):
        submission = self.create_draft(name="Race remove vs expiry")
        intent, _image = self.attach_managed_image(submission, 0)
        old = timezone.now() - timedelta(days=31)
        Request.objects.filter(pk=submission.pk).update(
            date_created=old, date_updated=old
        )

        locked = threading.Event()
        release = threading.Event()
        outcomes = Queue()
        owner_id = self.owner.pk
        submission_id = submission.pk
        intent_id = intent.pk
        real_validate = media_services._validate_actor_submission

        def paused_validate(*args, **kwargs):
            result = real_validate(*args, **kwargs)
            if args[1].pk == submission_id:
                locked.set()
                release.wait(timeout=30)
            return result

        def remove_worker():
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=owner_id)
                remove_attached_media(actor, intent_id, "race-remove-expiry")
                outcomes.put(("remove", None))
            except Exception as error:
                outcomes.put(("remove", type(error).__name__))
            finally:
                close_old_connections()

        def expiry_worker():
            close_old_connections()
            try:
                counts = process_submission_expiry(now=timezone.now())
                outcomes.put(("expiry", counts.expired))
            except Exception as error:
                outcomes.put(("expiry", type(error).__name__))
            finally:
                close_old_connections()

        with patch.object(
            media_services, "_validate_actor_submission", side_effect=paused_validate
        ):
            remove_thread = threading.Thread(target=remove_worker)
            remove_thread.start()
            self.assertTrue(locked.wait(timeout=10))
            expiry_thread = threading.Thread(target=expiry_worker)
            expiry_thread.start()
            expiry_thread.join(timeout=10)
            release.set()
            remove_thread.join(timeout=30)
            expiry_thread.join(timeout=30)

        self.assertFalse(remove_thread.is_alive())
        self.assertFalse(expiry_thread.is_alive())
        results = dict(outcomes.get_nowait() for _index in range(2))
        # The remover's lock is already held, so expiry's own row lock on the
        # same submission must skip it this pass, and the removal proceeds.
        self.assertEqual(results["expiry"], 0)
        self.assertIsNone(results["remove"])
        submission.refresh_from_db()
        self.assertEqual(submission.state, Request.State.DRAFT)

    def test_reorder_serializes_with_remove_on_the_submission_row(self):
        submission = self.create_draft(name="Race reorder vs remove")
        first_intent, first_image = self.attach_managed_image(submission, 0)
        _second_intent, second_image = self.attach_managed_image(submission, 1)
        barrier = threading.Barrier(2, timeout=60)
        outcomes = Queue()
        owner_id = self.owner.pk
        submission_id = submission.pk
        intent_id = first_intent.pk
        rotated = [second_image.pk, first_image.pk]

        def reorder_worker():
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=owner_id)
                barrier.wait()
                reorder_attached_media(actor, submission_id, "race-reorder", rotated)
                outcomes.put(("reorder", None))
            except Exception as error:
                outcomes.put(("reorder", type(error).__name__))
            finally:
                close_old_connections()

        def remove_worker():
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=owner_id)
                barrier.wait()
                remove_attached_media(actor, intent_id, "race-remove-reorder")
                outcomes.put(("remove", None))
            except Exception as error:
                outcomes.put(("remove", type(error).__name__))
            finally:
                close_old_connections()

        self.run_threads([reorder_worker, remove_worker])
        results = dict(outcomes.get_nowait() for _index in range(2))

        # Both requests target the same submission row, so they are fully
        # serialized: each completes cleanly against the state it observes.
        self.assertIsNone(results["remove"])
        if results["reorder"] is None:
            # Reorder committed before removal deleted first_image: fine either way.
            pass
        else:
            self.assertEqual(results["reorder"], "SubmissionInputError")

    def test_attach_and_cleanup_do_not_disturb_a_concurrently_removed_sibling(self):
        submission = self.create_draft(name="Race attach vs remove sibling")
        removed_intent, _removed_image = self.attach_managed_image(submission, 0)
        pending_intent = self.create_intent(
            submission, 1, MediaUploadIntent.State.VERIFIED
        )
        barrier = threading.Barrier(2, timeout=60)
        outcomes = Queue()
        owner_id = self.owner.pk
        removed_intent_id = removed_intent.pk
        pending_intent_id = pending_intent.pk

        def remove_worker():
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=owner_id)
                barrier.wait()
                remove_attached_media(actor, removed_intent_id, "race-remove-sibling")
                outcomes.put(("remove", None))
            except Exception as error:
                outcomes.put(("remove", type(error).__name__))
            finally:
                close_old_connections()

        def attach_worker():
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=owner_id)
                barrier.wait()
                attach_verified_media(actor, pending_intent_id, "race-attach-sibling")
                outcomes.put(("attach", None))
            except Exception as error:
                outcomes.put(("attach", type(error).__name__))
            finally:
                close_old_connections()

        self.run_threads([remove_worker, attach_worker])
        results = dict(outcomes.get_nowait() for _index in range(2))

        self.assertIsNone(results["remove"])
        self.assertIsNone(results["attach"])
        self.assertEqual(
            Image.objects.filter(request=submission, is_managed=True, state="attached").count(),
            1,
        )
        self.assertTrue(
            Image.objects.filter(intent_id=pending_intent_id, state="attached").exists()
        )
        self.assertFalse(Image.objects.filter(intent_id=removed_intent_id).exists())

    def test_freed_slot_admits_exactly_one_concurrent_winner(self):
        submission = self.create_draft(name="Race slot reuse")
        intent, _image = self.attach_managed_image(submission, 2)
        remove_attached_media(self.owner, intent.pk, "free-slot-before-race")
        storage = FakeMediaStorage(
            {intent.object_key: b"x", intent.sealed_object_key: b"y"}
        )
        process_media_cleanup(storage=storage)

        barrier = threading.Barrier(2, timeout=60)
        outcomes = Queue()
        owner_id = self.owner.pk
        submission_id = submission.pk

        def worker(index):
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=owner_id)
                barrier.wait()
                created, _replayed = create_upload_intent(
                    actor, submission_id, f"race-slot-{index}",
                    mime_type="image/png", declared_byte_size=10,
                    declared_sha256=f"{index}" * 64, slot=2,
                )
                outcomes.put(("created", created.pk))
            except MediaStateConflict as error:
                outcomes.put(("denied", error.code))
            finally:
                close_old_connections()

        self.run_threads([lambda index=index: worker(index) for index in range(2)])
        results = [outcomes.get_nowait(), outcomes.get_nowait()]

        self.assertEqual(sorted(item[0] for item in results), ["created", "denied"])
        self.assertEqual(
            MediaUploadIntent.objects.filter(
                submission=submission,
                slot=2,
                state__in=MediaUploadIntent.RESERVING_STATES,
            ).count(),
            1,
        )
