import hashlib
import io
import json
import queue
import uuid
from importlib import import_module
from io import StringIO
from datetime import timedelta
from types import SimpleNamespace
from threading import Thread
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone
from PIL import Image as PillowImage

from .media import (
    MediaInputError,
    MediaStateConflict,
    attach_verified_media,
    cleanup_media_object,
    create_upload_intent,
    expire_upload_intent,
    inspect_uploaded_object,
    issue_upload,
    process_media_cleanup,
    verify_upload,
)
from .media_storage import S3MediaStorage, StorageObjectNotFound, StorageOperationError
from .models import (
    Address,
    Category,
    Image,
    MediaUploadIntent,
    Place,
    Request,
    SubmissionIdempotency,
)
from .schema import schema
from .submissions import IdempotencyConflict


def encoded_image(image_format="PNG", size=(2, 2), color=1):
    output = io.BytesIO()
    mode = "1" if color in (0, 1) else "RGB"
    PillowImage.new(mode, size, color=color).save(output, format=image_format)
    return output.getvalue()


class FakeMediaStorage:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.issue_calls = []
        self.read_calls = []
        self.seal_calls = []
        self.delete_calls = []
        self.fail_delete = False
        self.fail_seal = False
        self.confirm_present = False

    def issue_upload(self, **kwargs):
        assert connection.in_atomic_block
        self.issue_calls.append(kwargs)
        return {
            "url": "https://private-storage.invalid/upload",
            "fields": {"key": kwargs["key"], "Content-Type": kwargs["mime_type"]},
        }

    def open_object(self, *, bucket, key):
        assert not connection.in_atomic_block
        self.read_calls.append((bucket, key))
        if key not in self.objects:
            raise StorageObjectNotFound("absent")
        return io.BytesIO(self.objects[key])

    def seal_object(self, *, bucket, key, body, content_type, content_length):
        assert not connection.in_atomic_block
        sealed = body.read()
        self.seal_calls.append(
            (bucket, key, content_type, content_length, sealed)
        )
        self.objects[key] = sealed
        if self.fail_seal:
            raise StorageOperationError("failed after write")

    def object_size(self, *, bucket, key):
        assert not connection.in_atomic_block
        if key not in self.objects:
            raise StorageObjectNotFound("absent")
        return len(self.objects[key])

    def delete_object(self, *, bucket, key):
        assert not connection.in_atomic_block
        self.delete_calls.append((bucket, key))
        if self.fail_delete:
            raise StorageOperationError("failed")
        self.objects.pop(key, None)

    def object_is_absent(self, *, bucket, key):
        assert not connection.in_atomic_block
        return not self.confirm_present and key not in self.objects


class PresignPolicyTests(SimpleTestCase):
    def test_exact_private_post_policy_has_only_intent_key_type_and_bounded_size(self):
        client = Mock()
        client.generate_presigned_post.return_value = {"url": "https://s3", "fields": {}}
        storage = S3MediaStorage(client=client)

        with self.assertNoLogs("backend.media_storage", level="DEBUG"):
            storage.issue_upload(
                bucket="private-media",
                key="submission-media/42/unguessable",
                mime_type="image/webp",
                maximum_size=12345,
                expires_in=600,
            )

        client.generate_presigned_post.assert_called_once_with(
            Bucket="private-media",
            Key="submission-media/42/unguessable",
            Fields={"Content-Type": "image/webp"},
            Conditions=[
                {"key": "submission-media/42/unguessable"},
                {"Content-Type": "image/webp"},
                ["content-length-range", 1, 12345],
            ],
            ExpiresIn=600,
        )

    def test_sealing_uses_exact_private_put_without_acl(self):
        client = Mock()
        storage = S3MediaStorage(client=client)
        body = io.BytesIO(b"verified")

        storage.seal_object(
            bucket="private-media",
            key="submission-media-sealed/42/random",
            body=body,
            content_type="image/png",
            content_length=8,
        )

        client.put_object.assert_called_once_with(
            Bucket="private-media",
            Key="submission-media-sealed/42/random",
            Body=body,
            ContentType="image/png",
            ContentLength=8,
        )

    def test_bad_storage_configuration_is_wrapped_without_endpoint_details(self):
        endpoint = "https://secret-internal-storage.invalid"
        with patch(
            "backend.media_storage.boto3.client",
            side_effect=ValueError(endpoint),
        ):
            with self.assertRaises(StorageOperationError) as caught:
                S3MediaStorage()

        self.assertNotIn(endpoint, str(caught.exception))


@override_settings(
    AWS_STORAGE_BUCKET_NAME="legacy-public-images",
    MEDIA_STORAGE_BUCKET_NAME="test-private-media",
    MEDIA_STORAGE_IDENTIFIER="test-s3-private",
)
class MediaServiceTests(TransactionTestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(email="media-owner@smokemap.test", password="test")
        self.other = User.objects.create_user(email="media-other@smokemap.test", password="test")
        self.inactive = User.objects.create_user(
            email="media-inactive@smokemap.test", password="test", is_active=False
        )
        self.category = Category.objects.create(slug="media-test", name="Media test")
        self.address = Address.objects.create(
            addressString="Media test", location=Point(34.8, 32.1, srid=4326)
        )
        self.submission = Request.objects.create(
            name="Media draft",
            category=self.category,
            address=self.address,
            owner=self.owner,
            state=Request.State.DRAFT,
        )

    def create_intent(self, data=None, key="create-1", slot=None):
        body = data if data is not None else encoded_image()
        return create_upload_intent(
            self.owner,
            self.submission.pk,
            key,
            mime_type="image/png",
            declared_byte_size=len(body),
            declared_sha256=hashlib.sha256(body).hexdigest(),
            original_filename="ignored-client-name.png",
            slot=slot,
        )[0]

    def test_creation_is_owner_bound_idempotent_and_key_is_code_owned(self):
        intent = self.create_intent()
        replay, replayed = create_upload_intent(
            self.owner,
            self.submission.pk,
            "create-1",
            mime_type="image/png",
            declared_byte_size=intent.declared_byte_size,
            declared_sha256=intent.declared_sha256,
            original_filename="ignored-client-name.png",
        )

        self.assertTrue(replayed)
        self.assertEqual(replay.pk, intent.pk)
        self.assertTrue(intent.object_key.startswith(f"submission-media/{self.submission.pk}/"))
        self.assertTrue(
            intent.sealed_object_key.startswith(
                f"submission-media-sealed/{self.submission.pk}/"
            )
        )
        self.assertNotEqual(intent.object_key, intent.sealed_object_key)
        self.assertNotIn("ignored-client-name", intent.object_key)
        self.assertEqual(intent.storage_bucket, "test-private-media")
        self.assertEqual(intent.storage_identifier, "test-s3-private")
        self.assertEqual(intent.absolute_expires_at - intent.created_at, timedelta(hours=24))
        with self.assertRaises(IdempotencyConflict):
            create_upload_intent(
                self.owner,
                self.submission.pk,
                "create-1",
                mime_type="image/png",
                declared_byte_size=intent.declared_byte_size + 1,
                declared_sha256=intent.declared_sha256,
            )
        self.assertEqual(MediaUploadIntent.objects.count(), 1)

    @override_settings(
        AWS_STORAGE_BUCKET_NAME="legacy-public-images",
        MEDIA_STORAGE_BUCKET_NAME="",
    )
    def test_missing_private_bucket_fails_closed_before_any_write(self):
        with self.assertRaises(MediaStateConflict) as caught:
            self.create_intent(key="missing-private-bucket")

        self.assertEqual(caught.exception.code, "MEDIA_STORAGE_UNAVAILABLE")
        self.assertFalse(MediaUploadIntent.objects.exists())
        self.assertFalse(SubmissionIdempotency.objects.exists())

    @override_settings(
        AWS_STORAGE_BUCKET_NAME="shared-media",
        MEDIA_STORAGE_BUCKET_NAME="shared-media",
    )
    def test_private_bucket_must_not_equal_nonempty_legacy_bucket(self):
        with self.assertRaises(MediaStateConflict) as caught:
            self.create_intent(key="shared-bucket")

        self.assertEqual(caught.exception.code, "MEDIA_STORAGE_UNAVAILABLE")
        self.assertFalse(MediaUploadIntent.objects.exists())

    def test_owner_active_draft_and_input_rules_fail_closed(self):
        digest = "0" * 64
        for actor in (self.other, self.inactive):
            with self.assertRaises(MediaStateConflict):
                create_upload_intent(
                    actor, self.submission.pk, f"denied-{actor.pk}",
                    mime_type="image/png", declared_byte_size=1, declared_sha256=digest,
                )
        self.submission.state = Request.State.PENDING
        self.submission.save(update_fields=["state"])
        with self.assertRaises(MediaStateConflict):
            create_upload_intent(
                self.owner, self.submission.pk, "not-draft",
                mime_type="image/png", declared_byte_size=1, declared_sha256=digest,
            )
        self.submission.state = Request.State.DRAFT
        self.submission.save(update_fields=["state"])
        invalid_values = [
            ("text/plain", 1, digest),
            ("image/png", 0, digest),
            ("image/png", 5_000_001, digest),
            ("image/png", 1, "A" * 64),
            ("image/png", 1, "0" * 63),
        ]
        for index, (mime, size, sha256) in enumerate(invalid_values):
            with self.assertRaises(MediaInputError):
                create_upload_intent(
                    self.owner, self.submission.pk, f"invalid-{index}",
                    mime_type=mime, declared_byte_size=size, declared_sha256=sha256,
                )

    def test_missing_media_row_maps_to_stable_not_found(self):
        with self.assertRaises(MediaStateConflict) as caught:
            verify_upload(self.owner, uuid.uuid4(), "verify-missing", storage=FakeMediaStorage())

        self.assertEqual(caught.exception.code, "NOT_FOUND")

    def test_three_slots_and_zero_media_are_valid_but_a_fourth_is_not(self):
        self.assertFalse(Image.objects.filter(request=self.submission, is_managed=True).exists())
        intents = [self.create_intent(key=f"slot-{slot}", slot=slot) for slot in range(3)]
        self.assertEqual([intent.slot for intent in intents], [0, 1, 2])
        with self.assertRaises(MediaStateConflict) as caught:
            self.create_intent(key="slot-four")
        self.assertEqual(caught.exception.code, "MEDIA_LIMIT_REACHED")

    def test_issue_and_renew_use_same_key_and_never_extend_absolute_deadline(self):
        intent = self.create_intent()
        storage = FakeMediaStorage()
        issued, upload, replayed = issue_upload(
            self.owner, intent.pk, "issue-1", storage=storage
        )
        absolute_deadline = issued.absolute_expires_at
        self.assertFalse(replayed)
        self.assertEqual(upload["fields"]["key"], intent.object_key)
        self.assertLessEqual(storage.issue_calls[0]["expires_in"], 600)
        self.assertEqual(storage.issue_calls[0]["maximum_size"], intent.declared_byte_size)
        replay_intent, replay_upload, replayed = issue_upload(
            self.owner, intent.pk, "issue-1", storage=storage
        )
        self.assertTrue(replayed)
        self.assertEqual(replay_intent.pk, intent.pk)
        self.assertEqual(replay_upload, upload)
        self.assertEqual(len(storage.issue_calls), 1)

        renewed, renewed_upload, _ = issue_upload(
            self.owner, intent.pk, "renew-1", renew=True, storage=storage
        )
        self.assertEqual(renewed_upload["fields"]["key"], intent.object_key)
        self.assertEqual(renewed.absolute_expires_at, absolute_deadline)
        _renewed, renewal_replay, replayed = issue_upload(
            self.owner, intent.pk, "renew-1", renew=True, storage=storage
        )
        self.assertTrue(replayed)
        self.assertEqual(renewal_replay, renewed_upload)
        self.assertEqual(len(storage.issue_calls), 2)
        with self.assertRaises(MediaStateConflict):
            issue_upload(self.owner, intent.pk, "second-issue", storage=storage)

    def test_presign_signing_holds_parent_lock_until_state_and_replay_are_persisted(self):
        intent = self.create_intent()
        lock_result = queue.Queue()

        class LockAssertingStorage(FakeMediaStorage):
            def issue_upload(inner_self, **kwargs):
                self.assertTrue(connection.in_atomic_block)

                def competing_cleanup():
                    close_old_connections()
                    try:
                        with transaction.atomic():
                            Request.objects.select_for_update(nowait=True).get(
                                pk=self.submission.pk
                            )
                            MediaUploadIntent.objects.select_for_update(nowait=True).get(
                                pk=intent.pk
                            )
                        lock_result.put("interleaved")
                    except DatabaseError:
                        lock_result.put("locked")
                    finally:
                        close_old_connections()

                thread = Thread(target=competing_cleanup)
                thread.start()
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())
                self.assertEqual(lock_result.get_nowait(), "locked")
                return super().issue_upload(**kwargs)

        storage = LockAssertingStorage()
        issued, upload, replayed = issue_upload(
            self.owner, intent.pk, "issue-locked", storage=storage
        )
        self.assertFalse(replayed)
        self.assertEqual(issued.state, MediaUploadIntent.State.ISSUED)
        replay = SubmissionIdempotency.objects.get(
            actor=self.owner, operation="media.intent.issue.v3", key="issue-locked"
        )
        self.assertEqual(replay.original_result["upload"], {
            "url": upload["url"], "fields": upload["fields"]
        })

    def test_presign_failure_rolls_back_created_or_issued_state_and_idempotency(self):
        intent = self.create_intent()

        class FailingSigner(FakeMediaStorage):
            def issue_upload(inner_self, **kwargs):
                self.assertTrue(connection.in_atomic_block)
                raise StorageOperationError("signing failed")

        with self.assertRaises(StorageOperationError):
            issue_upload(self.owner, intent.pk, "issue-fails", storage=FailingSigner())
        intent.refresh_from_db()
        self.assertEqual(intent.state, MediaUploadIntent.State.CREATED)
        self.assertIsNone(intent.issued_at)
        self.assertFalse(SubmissionIdempotency.objects.filter(key="issue-fails").exists())

        issued, _upload, _replayed = issue_upload(
            self.owner, intent.pk, "issue-succeeds", storage=FakeMediaStorage()
        )
        issued_evidence = (issued.state, issued.issued_at, issued.presign_expires_at)
        with self.assertRaises(StorageOperationError):
            issue_upload(
                self.owner, intent.pk, "renew-fails", renew=True, storage=FailingSigner()
            )
        intent.refresh_from_db()
        self.assertEqual(
            (intent.state, intent.issued_at, intent.presign_expires_at),
            issued_evidence,
        )
        self.assertFalse(SubmissionIdempotency.objects.filter(key="renew-fails").exists())

    def test_expired_presign_replay_is_redacted_and_never_returns_stale_secrets(self):
        intent = self.create_intent()
        storage = FakeMediaStorage()
        issue_upload(self.owner, intent.pk, "issue-redact", storage=storage)
        record = SubmissionIdempotency.objects.get(key="issue-redact")
        original = dict(record.original_result)
        original["upload_expires_at"] = (
            timezone.now() - timedelta(seconds=1)
        ).isoformat()
        record.original_result = original
        record.save(update_fields=["original_result"])

        with patch(
            "backend.media.configured_media_storage",
            side_effect=AssertionError("replay must not construct storage"),
        ):
            with self.assertRaises(MediaStateConflict) as caught:
                issue_upload(self.owner, intent.pk, "issue-redact")

        self.assertEqual(caught.exception.code, "UPLOAD_AUTHORIZATION_EXPIRED")
        record.refresh_from_db()
        self.assertNotIn("upload", record.original_result)
        self.assertTrue(record.original_result["upload_authorization_expired"])
        self.assertEqual(len(storage.issue_calls), 1)
        other_intent = self.create_intent(key="create-redact-conflict", slot=1)
        with self.assertRaises(IdempotencyConflict):
            issue_upload(
                self.owner,
                other_intent.pk,
                "issue-redact",
                storage=storage,
            )

    def test_cleanup_runner_redacts_expired_presign_evidence(self):
        intent = self.create_intent(key="create-runner-redact")
        storage = FakeMediaStorage()
        issue_upload(self.owner, intent.pk, "issue-runner-redact", storage=storage)
        record = SubmissionIdempotency.objects.get(key="issue-runner-redact")
        result = dict(record.original_result)
        result["upload_expires_at"] = (
            timezone.now() - timedelta(seconds=1)
        ).isoformat()
        record.original_result = result
        record.save(update_fields=["original_result"])

        counts = process_media_cleanup(storage=storage)

        record.refresh_from_db()
        self.assertEqual(counts.redacted, 1)
        self.assertNotIn("upload", record.original_result)

    def test_verification_streams_exact_bytes_and_attach_copies_only_server_evidence(self):
        body = encoded_image("PNG", (7, 5))
        intent = self.create_intent(body)
        storage = FakeMediaStorage({intent.object_key: body})
        _issued, upload, _ = issue_upload(
            self.owner, intent.pk, "issue-verify-1", storage=storage
        )
        self.assertNotIn(intent.sealed_object_key, repr(upload))

        verified, replayed = verify_upload(
            self.owner, intent.pk, "verify-1", storage=storage
        )
        self.assertFalse(replayed)
        self.assertEqual(verified.state, MediaUploadIntent.State.VERIFIED)
        self.assertEqual(verified.server_byte_size, len(body))
        self.assertEqual(verified.server_sha256, hashlib.sha256(body).hexdigest())
        self.assertEqual((verified.detected_mime, verified.width, verified.height), ("image/png", 7, 5))
        self.assertTrue(verified.upload_cleanup_pending)
        self.assertEqual(
            verified.upload_cleanup_next_attempt_at, verified.presign_expires_at
        )
        self.assertEqual(storage.objects[verified.sealed_object_key], body)
        self.assertNotIn(verified.object_key, storage.objects)
        self.assertEqual(
            storage.seal_calls,
            [(
                intent.storage_bucket,
                intent.sealed_object_key,
                "image/png",
                len(body),
                body,
            )],
        )

        replay, replayed = verify_upload(
            self.owner, intent.pk, "verify-1", storage=storage
        )
        self.assertTrue(replayed)
        self.assertEqual(replay.pk, intent.pk)
        self.assertEqual(len(storage.read_calls), 1)

        attachment, replayed = attach_verified_media(self.owner, intent.pk, "attach-1")
        self.assertFalse(replayed)
        self.assertTrue(attachment.is_managed)
        self.assertEqual(attachment.storage_key, intent.sealed_object_key)
        overwritten = encoded_image("PNG", (3, 3), color=0)
        storage.objects[intent.object_key] = overwritten
        self.assertEqual(storage.objects[attachment.storage_key], body)
        MediaUploadIntent.objects.filter(pk=intent.pk).update(
            presign_expires_at=timezone.now() - timedelta(seconds=1),
            upload_cleanup_next_attempt_at=timezone.now() - timedelta(seconds=1),
        )
        cleanup_counts = process_media_cleanup(storage=storage)
        self.assertEqual(cleanup_counts.upload_deleted, 1)
        self.assertNotIn(intent.object_key, storage.objects)
        self.assertEqual(storage.objects[attachment.storage_key], body)
        self.assertNotIn(
            (intent.storage_bucket, intent.sealed_object_key), storage.delete_calls
        )
        self.assertEqual(attachment.sha256, verified.server_sha256)
        self.assertEqual(attachment.owner, self.owner)
        self.assertEqual(attachment.request, self.submission)
        self.assertEqual(attachment.state, "attached")
        attachment_replay, replayed = attach_verified_media(
            self.owner, intent.pk, "attach-1"
        )
        self.assertTrue(replayed)
        self.assertEqual(attachment_replay.pk, attachment.pk)

    def test_absolute_expiry_is_durable_idempotent_cleanup_work(self):
        intent = self.create_intent()
        MediaUploadIntent.objects.filter(pk=intent.pk).update(
            absolute_expires_at=timezone.now() - timedelta(seconds=1)
        )
        expired, replayed = expire_upload_intent(
            self.owner, intent.pk, "expire-1"
        )
        self.assertFalse(replayed)
        self.assertEqual(expired.state, MediaUploadIntent.State.CLEANUP_PENDING)
        self.assertEqual(expired.failure_code, "intent_expired")
        replay, replayed = expire_upload_intent(
            self.owner, intent.pk, "expire-1"
        )
        self.assertTrue(replayed)
        self.assertEqual(replay.failure_code, "intent_expired")

    def test_sealing_failure_becomes_durable_two_key_cleanup_without_resurrection(self):
        body = encoded_image()
        intent = self.create_intent(body)
        storage = FakeMediaStorage({intent.object_key: body})
        storage.fail_seal = True
        issue_upload(self.owner, intent.pk, "issue-seal-fail", storage=storage)

        failed, _ = verify_upload(
            self.owner, intent.pk, "verify-seal-fail", storage=storage
        )
        self.assertEqual(failed.state, MediaUploadIntent.State.CLEANUP_PENDING)
        self.assertEqual(failed.failure_code, "object_seal_failed")
        self.assertIn(intent.sealed_object_key, storage.objects)
        with self.assertRaises(MediaStateConflict):
            attach_verified_media(self.owner, intent.pk, "attach-seal-fail")

        storage.fail_delete = True
        first, deleted, _ = cleanup_media_object(
            self.owner, intent.pk, "cleanup-seal-fail-1", storage=storage
        )
        self.assertFalse(deleted)
        self.assertEqual(first.state, MediaUploadIntent.State.CLEANUP_PENDING)
        MediaUploadIntent.objects.filter(pk=intent.pk).update(
            cleanup_next_attempt_at=timezone.now() - timedelta(seconds=1)
        )
        storage.fail_delete = False
        cleaned, deleted, _ = cleanup_media_object(
            self.owner, intent.pk, "cleanup-seal-fail-2", storage=storage
        )
        self.assertTrue(deleted)
        self.assertEqual(cleaned.state, MediaUploadIntent.State.DELETED)
        self.assertNotIn(intent.object_key, storage.objects)
        self.assertNotIn(intent.sealed_object_key, storage.objects)
        self.assertFalse(Image.objects.filter(intent=intent).exists())

    def test_source_cleanup_failure_retries_without_blocking_sealed_attachment(self):
        body = encoded_image()
        intent = self.create_intent(body)
        storage = FakeMediaStorage({intent.object_key: body})
        issue_upload(self.owner, intent.pk, "issue-source-retry", storage=storage)
        storage.fail_delete = True

        verified, _ = verify_upload(
            self.owner, intent.pk, "verify-source-retry", storage=storage
        )
        self.assertEqual(verified.state, MediaUploadIntent.State.VERIFIED)
        self.assertTrue(verified.upload_cleanup_pending)
        self.assertEqual(verified.upload_cleanup_error_code, "upload_cleanup_failed")
        attachment, _ = attach_verified_media(
            self.owner, intent.pk, "attach-source-retry"
        )
        self.assertEqual(attachment.storage_key, intent.sealed_object_key)
        MediaUploadIntent.objects.filter(pk=intent.pk).update(
            presign_expires_at=timezone.now() - timedelta(seconds=1),
            upload_cleanup_next_attempt_at=timezone.now() - timedelta(seconds=1),
        )
        storage.fail_delete = False

        counts = process_media_cleanup(storage=storage)

        intent.refresh_from_db()
        self.assertEqual((counts.upload_claimed, counts.upload_deleted), (1, 1))
        self.assertFalse(intent.upload_cleanup_pending)
        self.assertNotIn(intent.object_key, storage.objects)
        self.assertEqual(storage.objects[intent.sealed_object_key], body)
        self.assertTrue(Image.objects.filter(pk=attachment.pk).exists())

    def test_digest_mismatch_is_stable_cleanup_work_and_confirmed_absence_deletes(self):
        declared = encoded_image(color=0)
        stored = encoded_image(color=1)
        intent = self.create_intent(declared)
        storage = FakeMediaStorage({intent.object_key: stored})
        issue_upload(self.owner, intent.pk, "issue-verify-bad", storage=storage)

        failed, _ = verify_upload(self.owner, intent.pk, "verify-bad", storage=storage)
        self.assertEqual(failed.state, MediaUploadIntent.State.CLEANUP_PENDING)
        self.assertEqual(failed.failure_code, "sha256_mismatch")
        self.assertIsNotNone(failed.failure_at)
        with self.assertRaises(MediaStateConflict):
            attach_verified_media(self.owner, intent.pk, "attach-bad")

        cleaned, deleted, replayed = cleanup_media_object(
            self.owner, intent.pk, "cleanup-1", storage=storage
        )
        self.assertTrue(deleted)
        self.assertFalse(replayed)
        self.assertEqual(cleaned.state, MediaUploadIntent.State.DELETED)
        self.assertEqual(cleaned.failure_code, "sha256_mismatch")
        self.assertEqual(storage.delete_calls, [
            (intent.storage_bucket, intent.object_key),
            (intent.storage_bucket, intent.sealed_object_key),
        ])

    def test_cleanup_failure_keeps_exact_retry_metadata_and_same_key_replays(self):
        body = encoded_image()
        intent = self.create_intent(body)
        MediaUploadIntent.objects.filter(pk=intent.pk).update(
            state=MediaUploadIntent.State.CLEANUP_PENDING,
            failure_code="object_not_found",
            failure_at=timezone.now(),
        )
        storage = FakeMediaStorage({intent.object_key: body})
        storage.fail_delete = True
        failed, deleted, _ = cleanup_media_object(
            self.owner, intent.pk, "cleanup-fail", storage=storage
        )
        self.assertFalse(deleted)
        self.assertEqual(failed.state, MediaUploadIntent.State.CLEANUP_PENDING)
        self.assertEqual(failed.cleanup_error_code, "storage_cleanup_failed")
        self.assertIsNotNone(failed.cleanup_next_attempt_at)
        replay, deleted, replayed = cleanup_media_object(
            self.owner, intent.pk, "cleanup-fail", storage=storage
        )
        self.assertTrue(replayed)
        self.assertFalse(deleted)
        self.assertEqual(replay.cleanup_attempts, 1)

    def test_cleanup_claim_winning_during_object_read_prevents_verification(self):
        body = encoded_image()
        intent = self.create_intent(body)

        class CleanupRacingStorage(FakeMediaStorage):
            def open_object(inner_self, *, bucket, key):
                body_stream = super(CleanupRacingStorage, inner_self).open_object(
                    bucket=bucket, key=key
                )
                now = timezone.now()
                MediaUploadIntent.objects.filter(pk=intent.pk).update(
                    state=MediaUploadIntent.State.CLEANUP_PENDING,
                    failure_code="intent_expired",
                    failure_at=now,
                    cleanup_claim_token=uuid.uuid4(),
                    cleanup_claimed_at=now,
                    cleanup_lease_until=now + timedelta(minutes=5),
                )
                return body_stream

        storage = CleanupRacingStorage({intent.object_key: body})
        issue_upload(self.owner, intent.pk, "issue-verify-race", storage=storage)
        with self.assertRaises(MediaStateConflict):
            verify_upload(self.owner, intent.pk, "verify-race", storage=storage)
        intent.refresh_from_db()
        self.assertEqual(intent.state, MediaUploadIntent.State.CLEANUP_PENDING)
        self.assertEqual(intent.failure_code, "intent_expired")
        self.assertFalse(Image.objects.filter(intent=intent).exists())

    def test_cleanup_reaching_deleted_before_seal_is_compensated_without_orphan(self):
        body = encoded_image()
        intent = self.create_intent(body)
        observed = {}

        class CleanupBeforeSealStorage(FakeMediaStorage):
            def seal_object(
                inner_self, *, bucket, key, body, content_type, content_length
            ):
                now = timezone.now()
                MediaUploadIntent.objects.filter(pk=intent.pk).update(
                    state=MediaUploadIntent.State.CLEANUP_PENDING,
                    failure_code="intent_expired",
                    failure_at=now,
                )
                counts = process_media_cleanup(storage=inner_self, now=now)
                observed["cleanup_deleted"] = counts.deleted
                observed["state_before_seal"] = MediaUploadIntent.objects.get(
                    pk=intent.pk
                ).state
                super(CleanupBeforeSealStorage, inner_self).seal_object(
                    bucket=bucket,
                    key=key,
                    body=body,
                    content_type=content_type,
                    content_length=content_length,
                )
                observed["sealed_recreated"] = key in inner_self.objects

            def delete_object(inner_self, *, bucket, key):
                if observed.get("sealed_recreated") and key == intent.sealed_object_key:
                    try:
                        attach_verified_media(
                            self.owner, intent.pk, "attach-during-compensation"
                        )
                    except MediaStateConflict:
                        observed["attach_blocked"] = True
                return super().delete_object(bucket=bucket, key=key)

        storage = CleanupBeforeSealStorage({intent.object_key: body})
        issue_upload(self.owner, intent.pk, "issue-cleanup-before-seal", storage=storage)

        with self.assertRaises(MediaStateConflict):
            verify_upload(
                self.owner,
                intent.pk,
                "verify-cleanup-before-seal",
                storage=storage,
            )

        intent.refresh_from_db()
        self.assertEqual(observed["cleanup_deleted"], 1)
        self.assertEqual(observed["state_before_seal"], MediaUploadIntent.State.DELETED)
        self.assertTrue(observed["sealed_recreated"])
        self.assertTrue(observed["attach_blocked"])
        self.assertEqual(intent.state, MediaUploadIntent.State.DELETED)
        self.assertIsNone(intent.cleanup_claim_token)
        self.assertNotIn(intent.sealed_object_key, storage.objects)
        self.assertFalse(Image.objects.filter(intent=intent).exists())

    def test_stale_seal_delete_failure_revives_deleted_intent_for_retry(self):
        body = encoded_image()
        intent = self.create_intent(body)

        class FailingCompensationStorage(FakeMediaStorage):
            def seal_object(
                inner_self, *, bucket, key, body, content_type, content_length
            ):
                now = timezone.now()
                MediaUploadIntent.objects.filter(pk=intent.pk).update(
                    state=MediaUploadIntent.State.CLEANUP_PENDING,
                    failure_code="intent_expired",
                    failure_at=now,
                )
                process_media_cleanup(storage=inner_self, now=now)
                super(FailingCompensationStorage, inner_self).seal_object(
                    bucket=bucket,
                    key=key,
                    body=body,
                    content_type=content_type,
                    content_length=content_length,
                )
                inner_self.fail_delete = True

        storage = FailingCompensationStorage({intent.object_key: body})
        issue_upload(self.owner, intent.pk, "issue-stale-seal-retry", storage=storage)

        with self.assertRaises(MediaStateConflict):
            verify_upload(
                self.owner, intent.pk, "verify-stale-seal-retry", storage=storage
            )

        intent.refresh_from_db()
        self.assertEqual(intent.state, MediaUploadIntent.State.CLEANUP_PENDING)
        self.assertEqual(intent.cleanup_error_code, "sealed_cleanup_failed")
        self.assertIsNotNone(intent.cleanup_next_attempt_at)
        self.assertIsNone(intent.cleanup_claim_token)
        self.assertIn(intent.sealed_object_key, storage.objects)
        self.assertFalse(Image.objects.filter(intent=intent).exists())

        storage.fail_delete = False
        retry = process_media_cleanup(
            storage=storage, now=intent.cleanup_next_attempt_at
        )
        intent.refresh_from_db()
        self.assertEqual((retry.claimed, retry.deleted), (1, 1))
        self.assertEqual(intent.state, MediaUploadIntent.State.DELETED)
        self.assertNotIn(intent.sealed_object_key, storage.objects)

    def test_system_cleanup_claim_blocks_verify_and_attach_without_resurrection(self):
        body = encoded_image()
        intent = self.create_intent(body)
        storage = FakeMediaStorage({intent.object_key: body})
        issue_upload(self.owner, intent.pk, "issue-cleanup-race", storage=storage)
        verify_upload(self.owner, intent.pk, "verify-cleanup-race", storage=storage)
        now = timezone.now()
        MediaUploadIntent.objects.filter(pk=intent.pk).update(
            state=MediaUploadIntent.State.CLEANUP_PENDING,
            failure_code="intent_expired",
            failure_at=now,
        )
        observed = {}

        class ClaimObservingStorage(FakeMediaStorage):
            def delete_object(inner_self, *, bucket, key):
                claimed = MediaUploadIntent.objects.get(pk=intent.pk)
                observed["claim"] = claimed.cleanup_claim_token
                observed["verify"], _ = verify_upload(
                    self.owner, intent.pk, "verify-after-cleanup-claim", storage=inner_self
                )
                try:
                    attach_verified_media(self.owner, intent.pk, "attach-after-cleanup-claim")
                except MediaStateConflict:
                    observed["attach_denied"] = True
                return super().delete_object(bucket=bucket, key=key)

        racing_storage = ClaimObservingStorage()
        racing_storage.objects = storage.objects
        counts = process_media_cleanup(storage=racing_storage)
        intent.refresh_from_db()
        self.assertIsNotNone(observed["claim"])
        self.assertEqual(observed["verify"].state, MediaUploadIntent.State.CLEANUP_PENDING)
        self.assertTrue(observed["attach_denied"])
        self.assertEqual(intent.state, MediaUploadIntent.State.DELETED)
        self.assertEqual(counts.deleted, 1)
        self.assertNotIn(intent.sealed_object_key, storage.objects)
        self.assertFalse(Image.objects.filter(intent=intent).exists())

    def test_attached_media_cannot_be_claimed_or_deleted_by_system_cleanup(self):
        body = encoded_image()
        intent = self.create_intent(body)
        storage = FakeMediaStorage({intent.object_key: body})
        issue_upload(self.owner, intent.pk, "issue-attach-wins", storage=storage)
        verify_upload(self.owner, intent.pk, "verify-attach-wins", storage=storage)
        attachment, _ = attach_verified_media(self.owner, intent.pk, "attach-wins")

        counts = process_media_cleanup(storage=storage)
        intent.refresh_from_db()
        self.assertEqual(intent.state, MediaUploadIntent.State.ATTACHED)
        self.assertIsNone(intent.cleanup_claim_token)
        self.assertEqual(counts.claimed, 0)
        self.assertEqual(
            storage.delete_calls,
            [(intent.storage_bucket, intent.object_key)],
        )
        self.assertTrue(Image.objects.filter(pk=attachment.pk, state="attached").exists())

    def test_autonomous_command_expires_and_cleans_overdue_issued_intent_once(self):
        body = encoded_image()
        intent = self.create_intent(body)
        storage = FakeMediaStorage({intent.object_key: body})
        issue_upload(self.owner, intent.pk, "issue-autonomous", storage=storage)
        MediaUploadIntent.objects.filter(pk=intent.pk).update(
            absolute_expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.owner.is_active = False
        self.owner.save(update_fields=["is_active"])
        output = StringIO()
        with patch("backend.media.configured_media_storage", return_value=storage):
            call_command("process_media_cleanup", batch_size=10, stdout=output)
            call_command("process_media_cleanup", batch_size=10, stdout=output)

        intent.refresh_from_db()
        rendered = output.getvalue()
        self.assertEqual(intent.state, MediaUploadIntent.State.DELETED)
        self.assertEqual(intent.failure_code, "intent_expired")
        self.assertEqual(intent.cleanup_attempts, 1)
        self.assertEqual(storage.delete_calls, [
            (intent.storage_bucket, intent.object_key),
            (intent.storage_bucket, intent.sealed_object_key),
        ])
        self.assertEqual(SubmissionIdempotency.objects.filter(media_intent=intent).count(), 2)
        self.assertNotIn(intent.object_key, rendered)
        self.assertNotIn(intent.storage_bucket, rendered)
        self.assertNotIn(intent.declared_sha256, rendered)

    def test_three_abandoned_verified_slots_expire_clean_and_release(self):
        body = encoded_image()
        storage = FakeMediaStorage()
        intents = []
        for slot in range(3):
            intent = self.create_intent(body, key=f"verified-create-{slot}", slot=slot)
            storage.objects[intent.object_key] = body
            issue_upload(
                self.owner, intent.pk, f"verified-issue-{slot}", storage=storage
            )
            verify_upload(
                self.owner, intent.pk, f"verified-verify-{slot}", storage=storage
            )
            intents.append(intent)
        MediaUploadIntent.objects.filter(pk__in=[item.pk for item in intents]).update(
            absolute_expires_at=timezone.now() - timedelta(seconds=1)
        )

        counts = process_media_cleanup(storage=storage)

        self.assertEqual((counts.expired, counts.deleted), (3, 3))
        self.assertEqual(
            MediaUploadIntent.objects.filter(
                pk__in=[item.pk for item in intents],
                state=MediaUploadIntent.State.DELETED,
            ).count(),
            3,
        )
        for intent in intents:
            self.assertNotIn(intent.object_key, storage.objects)
            self.assertNotIn(intent.sealed_object_key, storage.objects)
        replacements = [
            self.create_intent(key=f"replacement-{slot}", slot=slot)
            for slot in range(3)
        ]
        self.assertEqual([item.slot for item in replacements], [0, 1, 2])

    def test_attach_after_absolute_deadline_fails_closed_and_preserves_no_image(self):
        body = encoded_image()
        intent = self.create_intent(body)
        storage = FakeMediaStorage({intent.object_key: body})
        issue_upload(self.owner, intent.pk, "issue-late-attach", storage=storage)
        verify_upload(self.owner, intent.pk, "verify-late-attach", storage=storage)
        MediaUploadIntent.objects.filter(pk=intent.pk).update(
            absolute_expires_at=timezone.now() - timedelta(seconds=1)
        )

        with self.assertRaises(MediaStateConflict) as caught:
            attach_verified_media(self.owner, intent.pk, "late-attach")

        self.assertEqual(caught.exception.code, "MEDIA_INTENT_EXPIRED")
        intent.refresh_from_db()
        self.assertEqual(intent.state, MediaUploadIntent.State.CLEANUP_PENDING)
        self.assertFalse(Image.objects.filter(intent=intent).exists())

    def test_autonomous_cleanup_reclaims_expired_lease_and_honors_retry_backoff(self):
        body = encoded_image()
        intent = self.create_intent(body)
        storage = FakeMediaStorage({intent.object_key: body})
        storage.fail_delete = True
        now = timezone.now()
        MediaUploadIntent.objects.filter(pk=intent.pk).update(
            state=MediaUploadIntent.State.CLEANUP_PENDING,
            failure_code="intent_expired",
            failure_at=now,
            cleanup_claim_token=uuid.uuid4(),
            cleanup_claimed_at=now - timedelta(minutes=10),
            cleanup_lease_until=now - timedelta(minutes=5),
            cleanup_attempts=1,
        )

        first = process_media_cleanup(storage=storage, now=now)
        intent.refresh_from_db()
        self.assertEqual((first.claimed, first.failed), (1, 1))
        self.assertEqual(intent.cleanup_attempts, 2)
        retry_at = intent.cleanup_next_attempt_at
        self.assertGreater(retry_at, now)
        second = process_media_cleanup(storage=storage, now=now)
        self.assertEqual(second.claimed, 0)
        self.assertEqual(len(storage.delete_calls), 2)

        storage.fail_delete = False
        third = process_media_cleanup(storage=storage, now=retry_at)
        fourth = process_media_cleanup(storage=storage, now=retry_at + timedelta(hours=1))
        intent.refresh_from_db()
        self.assertEqual((third.claimed, third.deleted), (1, 1))
        self.assertEqual(fourth.claimed, 0)
        self.assertEqual(intent.state, MediaUploadIntent.State.DELETED)
        self.assertEqual(len(storage.delete_calls), 4)

    def test_managed_image_database_rejects_bad_sha256_and_excess_pixel_area(self):
        intent = self.create_intent()
        base = {
            "set_id": "",
            "name": "",
            "url": "",
            "metadata": None,
            "request": self.submission,
            "place": None,
            "is_managed": True,
            "intent": intent,
            "owner": self.owner,
            "position": intent.slot,
            "state": "attached",
            "storage_identifier": intent.storage_identifier,
            "storage_bucket": intent.storage_bucket,
            "storage_key": intent.sealed_object_key,
            "byte_size": intent.declared_byte_size,
            "detected_mime": intent.expected_mime,
            "attached_at": timezone.now(),
        }
        invalid_rows = [
            {**base, "width": 2, "height": 2, "sha256": "A" * 64},
            {**base, "width": 5_001, "height": 5_000, "sha256": "0" * 64},
        ]
        for values in invalid_rows:
            with self.assertRaises(IntegrityError), transaction.atomic():
                Image.objects.create(**values)

    def test_database_enforces_upload_and_sealed_key_namespaces(self):
        intent = self.create_intent(key="key-constraints")
        invalid_updates = [
            {"object_key": f"client-chosen/{uuid.uuid4().hex}"},
            {"sealed_object_key": f"submission-media/{self.submission.pk}/{uuid.uuid4().hex}"},
            {"sealed_object_key": intent.object_key},
        ]
        for values in invalid_updates:
            with self.assertRaises(IntegrityError), transaction.atomic():
                MediaUploadIntent.objects.filter(pk=intent.pk).update(**values)
        intent.refresh_from_db()
        self.assertTrue(intent.object_key.startswith("submission-media/"))
        self.assertTrue(intent.sealed_object_key.startswith("submission-media-sealed/"))

    def test_corrupt_truncated_dimension_area_and_bomb_inputs_fail_decode_safely(self):
        samples = [
            b"\x89PNG\r\n\x1a\nnot-an-image",
            encoded_image()[:20],
            encoded_image(size=(10_001, 1)),
            encoded_image(size=(5_001, 5_000)),
            encoded_image(size=(10_000, 10_000)),
        ]
        for body in samples:
            storage = FakeMediaStorage({"key": body})
            outcome = inspect_uploaded_object(
                storage,
                bucket="bucket",
                key="key",
                sealed_key="submission-media-sealed/1/00000000000000000000000000000000",
                expected_size=len(body),
                expected_sha256=hashlib.sha256(body).hexdigest(),
                expected_mime="image/png",
            )
            self.assertIsNone(outcome.media)
            self.assertIn(
                outcome.failure_code,
                {"image_decode_failed", "image_dimensions_exceeded", "image_area_exceeded"},
            )

    def test_excess_area_is_rejected_before_verify_or_load(self):
        body = b"\x89PNG\r\n\x1a\nsmall-header-only"
        storage = FakeMediaStorage({"key": body})
        decoded = Mock(format="PNG", size=(5_001, 5_000))
        context = Mock()
        context.__enter__ = Mock(return_value=decoded)
        context.__exit__ = Mock(return_value=False)

        with patch("backend.media.PillowImage.open", return_value=context) as opened:
            outcome = inspect_uploaded_object(
                storage,
                bucket="bucket",
                key="key",
                sealed_key=(
                    "submission-media-sealed/1/00000000000000000000000000000000"
                ),
                expected_size=len(body),
                expected_sha256=hashlib.sha256(body).hexdigest(),
                expected_mime="image/png",
            )

        self.assertEqual(outcome.failure_code, "image_area_exceeded")
        opened.assert_called_once()
        decoded.verify.assert_not_called()
        decoded.load.assert_not_called()

    def test_private_managed_media_is_absent_from_public_images_and_schema_fields(self):
        body = encoded_image()
        intent = self.create_intent(body)
        storage = FakeMediaStorage({intent.object_key: body})
        issue_upload(self.owner, intent.pk, "issue-private", storage=storage)
        verify_upload(self.owner, intent.pk, "verify-private", storage=storage)
        attach_verified_media(self.owner, intent.pk, "attach-private")

        result = schema.execute(
            "{ images { id name url metadata } }",
            context_value=SimpleNamespace(user=SimpleNamespace(is_authenticated=False), META={}),
        )
        self.assertIsNone(result.errors)
        self.assertEqual(result.data["images"], [])
        schema_text = str(schema)
        self.assertNotIn("storageKey", schema_text)
        self.assertNotIn("storageBucket", schema_text)
        self.assertNotIn("serverSha256", schema_text)
        self.assertNotIn("sealedObjectKey", schema_text)
        self.assertNotIn("objectKey", schema_text)

    def test_legacy_place_image_set_keeps_public_shape_and_excludes_draft_media(self):
        place = Place.objects.create(
            name="Public legacy place",
            category=self.category,
            address=self.address,
        )
        legacy = Image.objects.create(
            set_id="legacy-public",
            name="legacy.png",
            url="https://public.invalid/legacy.png",
            metadata={"credit": "legacy"},
            place=place,
            request=None,
        )
        body = encoded_image()
        intent = self.create_intent(body, key="private-for-place-regression")
        storage = FakeMediaStorage({intent.object_key: body})
        issue_upload(self.owner, intent.pk, "issue-place-regression", storage=storage)
        verify_upload(self.owner, intent.pk, "verify-place-regression", storage=storage)
        attach_verified_media(self.owner, intent.pk, "attach-place-regression")

        result = schema.execute(
            """
            query PublicPlace($id: ID!) {
              placeById(id: $id) {
                imageSet { id name url metadata }
              }
            }
            """,
            variable_values={"id": str(place.pk)},
            context_value=SimpleNamespace(
                user=SimpleNamespace(is_authenticated=False), META={}
            ),
        )

        self.assertIsNone(result.errors)
        returned_images = result.data["placeById"]["imageSet"]
        self.assertEqual(len(returned_images), 1)
        returned = returned_images[0]
        self.assertEqual(returned["id"], str(legacy.pk))
        self.assertEqual(returned["name"], "legacy.png")
        self.assertEqual(returned["url"], "https://public.invalid/legacy.png")
        self.assertEqual(json.loads(returned["metadata"]), {"credit": "legacy"})
        request_type = schema.graphql_schema.get_type("RequestType")
        self.assertNotIn("imageSet", request_type.fields)

    def test_guest_and_inactive_users_cannot_create_intents(self):
        mutation = """
          mutation Create($input: CreateMediaUploadIntentInput!, $key: String!) {
            createMediaUploadIntent(input: $input, idempotencyKey: $key) {
              intent { id }
            }
          }
        """
        variables = {
            "key": "denied-graphql",
            "input": {
                "submissionId": str(self.submission.pk),
                "mimeType": "image/png",
                "declaredByteSize": 1,
                "declaredSha256": "0" * 64,
            },
        }
        for user in (SimpleNamespace(is_authenticated=False, is_active=False), self.inactive):
            result = schema.execute(
                mutation,
                variable_values=variables,
                context_value=SimpleNamespace(user=user, META={}),
            )
            self.assertEqual(result.errors[0].extensions["code"], "UNAUTHENTICATED")

    def test_graphql_maps_storage_configuration_failure_without_detail_leak(self):
        intent = self.create_intent(key="bad-storage-config")
        endpoint = "https://secret-storage-endpoint.invalid"
        mutation = """
          mutation Issue($id: ID!, $key: String!) {
            issueMediaUploadIntent(intentId: $id, idempotencyKey: $key) {
              upload { url }
            }
          }
        """
        with patch(
            "backend.media.configured_media_storage",
            side_effect=StorageOperationError(endpoint),
        ):
            result = schema.execute(
                mutation,
                variable_values={"id": str(intent.pk), "key": "bad-storage-issue"},
                context_value=SimpleNamespace(user=self.owner, META={}),
            )

        self.assertEqual(result.errors[0].extensions["code"], "MEDIA_STORAGE_UNAVAILABLE")
        self.assertNotIn(endpoint, result.errors[0].message)

    def test_cross_owner_attach_and_duplicate_digest_are_denied(self):
        body = encoded_image()
        first = self.create_intent(body, key="first-digest", slot=0)
        second = self.create_intent(body, key="second-digest", slot=1)
        storage = FakeMediaStorage({first.object_key: body, second.object_key: body})
        issue_upload(self.owner, first.pk, "issue-first", storage=storage)
        issue_upload(self.owner, second.pk, "issue-second", storage=storage)
        verify_upload(self.owner, first.pk, "verify-first", storage=storage)
        verify_upload(self.owner, second.pk, "verify-second", storage=storage)
        attach_verified_media(self.owner, first.pk, "attach-first")

        with self.assertRaises(MediaStateConflict) as other_owner:
            attach_verified_media(self.other, second.pk, "attach-other")
        self.assertEqual(other_owner.exception.code, "NOT_FOUND")
        with self.assertRaises(MediaStateConflict) as duplicate:
            attach_verified_media(self.owner, second.pk, "attach-second")
        self.assertEqual(duplicate.exception.code, "MEDIA_DIGEST_CONFLICT")
        self.assertEqual(Image.objects.filter(request=self.submission, is_managed=True).count(), 1)

    def test_legacy_upload_apis_remain_denied(self):
        context = SimpleNamespace(user=self.owner, META={})
        query = schema.execute("{ s3PresignedUrl }", context_value=context)
        mutation = schema.execute(
            'mutation { createImage(input: {requestId: "1", name: "x", url: "https://evil"}) { image { id } } }',
            context_value=context,
        )
        self.assertEqual(query.errors[0].extensions["code"], "FORBIDDEN")
        self.assertEqual(mutation.errors[0].extensions["code"], "FORBIDDEN")


@override_settings(
    AWS_STORAGE_BUCKET_NAME="legacy-public-images",
    MEDIA_STORAGE_BUCKET_NAME="test-private-media",
    MEDIA_STORAGE_IDENTIFIER="test-s3-private",
)
class MediaSlotConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(email="slot-race@smokemap.test", password="test")
        category = Category.objects.create(slug="slot-race", name="Slot race")
        address = Address.objects.create(addressString="Slot race", location=Point(1, 1, srid=4326))
        self.submission = Request.objects.create(
            name="Slot race", category=category, address=address, owner=self.owner
        )
        body = encoded_image()
        digest = hashlib.sha256(body).hexdigest()
        for slot in (0, 1):
            create_upload_intent(
                self.owner, self.submission.pk, f"existing-{slot}",
                mime_type="image/png", declared_byte_size=len(body),
                declared_sha256=digest, slot=slot,
            )
        self.body = body
        self.digest = digest

    def test_two_concurrent_creates_at_two_slots_cannot_allocate_a_fourth(self):
        results = queue.Queue()

        def worker(index):
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=self.owner.pk)
                intent, _ = create_upload_intent(
                    actor, self.submission.pk, f"race-{index}",
                    mime_type="image/png", declared_byte_size=len(self.body),
                    declared_sha256=self.digest,
                )
                results.put(("created", str(intent.pk)))
            except MediaStateConflict as error:
                results.put(("denied", error.code))
            finally:
                close_old_connections()

        threads = [Thread(target=worker, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        outcomes = [results.get_nowait(), results.get_nowait()]
        self.assertEqual(sorted(outcome[0] for outcome in outcomes), ["created", "denied"])
        self.assertEqual(
            MediaUploadIntent.objects.filter(
                submission=self.submission,
                state__in=MediaUploadIntent.RESERVING_STATES,
            ).count(),
            3,
        )
        self.assertEqual(SubmissionIdempotency.objects.filter(operation="media.intent.create.v3").count(), 3)


class OwnerBoundMediaMigrationTests(TransactionTestCase):
    migrate_from = [("backend", "0006_m3_submission_creation")]
    migrate_to = [("backend", "0007_owner_bound_media")]

    def setUp(self):
        super().setUp()
        self.addCleanup(self.restore_latest)
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        apps = executor.loader.project_state(self.migrate_from).apps
        category = apps.get_model("backend", "Category").objects.create(
            slug="legacy-media", name="Legacy media"
        )
        address = apps.get_model("backend", "Address").objects.create(
            addressString="Legacy media", location=Point(2, 2, srid=4326)
        )
        self.category_id = category.pk
        self.address_id = address.pk
        place = apps.get_model("backend", "Place").objects.create(
            name="Legacy place", category_id=category.pk, address_id=address.pk
        )
        self.image_id = apps.get_model("backend", "Image").objects.create(
            set_id="legacy-set",
            name="legacy.jpg",
            url="https://legacy-public.invalid/image.jpg",
            place_id=place.pk,
        ).pk
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def restore_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def _post_teardown(self):
        super()._post_teardown()
        initial_categories = import_module(
            "backend.migrations.0004_category_slug"
        ).INITIAL_CATEGORIES
        descriptions = dict(
            import_module("backend.migrations.0001_initial").PROVISIONAL_CATEGORIES
        )
        for slug, name in initial_categories:
            Category.objects.update_or_create(
                name=name,
                defaults={"slug": slug, "description": descriptions[name]},
            )

    def test_forward_preserves_public_place_image_without_marking_it_managed(self):
        image = self.apps.get_model("backend", "Image").objects.get(pk=self.image_id)
        self.assertFalse(image.is_managed)
        self.assertEqual(image.url, "https://legacy-public.invalid/image.jpg")
        self.assertIsNone(image.intent_id)

    def test_reverse_is_lossless_when_no_managed_media_exists(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        apps = executor.loader.project_state(self.migrate_from).apps
        image = apps.get_model("backend", "Image").objects.get(pk=self.image_id)
        self.assertEqual(image.url, "https://legacy-public.invalid/image.jpg")

    def test_reverse_refuses_when_managed_media_intent_exists(self):
        user_model = self.apps.get_model("backend", "CustomUser")
        request_model = self.apps.get_model("backend", "Request")
        intent_model = self.apps.get_model("backend", "MediaUploadIntent")
        owner = user_model.objects.create(email="migration-media@smokemap.test")
        submission = request_model.objects.create(
            name="Managed migration draft",
            category_id=self.category_id,
            address_id=self.address_id,
            owner_id=owner.pk,
            state="draft",
        )
        now = timezone.now()
        intent_model.objects.create(
            submission_id=submission.pk,
            owner_id=owner.pk,
            state="created",
            slot=0,
            storage_identifier="private",
            storage_bucket="private-media",
            object_key=f"submission-media/{submission.pk}/{uuid.uuid4().hex}",
            expected_mime="image/png",
            declared_byte_size=1,
            declared_sha256="0" * 64,
            absolute_expires_at=now + timedelta(hours=24),
        )

        executor = MigrationExecutor(connection)
        with self.assertRaisesRegex(RuntimeError, "managed media evidence"):
            executor.migrate(self.migrate_from)
