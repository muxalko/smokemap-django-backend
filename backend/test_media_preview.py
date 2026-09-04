import uuid
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from botocore.exceptions import ClientError
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from .media import (
    MEDIA_PREVIEW_LIFETIME_SECONDS,
    MediaStateConflict,
    issue_media_preview,
    remove_attached_media,
)
from .media_storage import S3MediaStorage, StorageOperationError
from .models import Image, Request
from .schema import schema
from .test_submission_finalization import PRIVATE_MEDIA_SETTINGS, SubmissionFixtureMixin


MEDIA_ATTACHMENT_PREVIEW = """
    query Preview($id: ID!) {
      mediaAttachmentPreviewV3(attachmentId: $id) {
        url
        expiresAt
      }
    }
"""


class FakePreviewStorage:
    def __init__(self):
        self.calls = []

    def issue_preview(self, *, bucket, key, expires_in):
        self.calls.append({"bucket": bucket, "key": key, "expires_in": expires_in})
        return f"https://private-storage.invalid/{bucket}/{key}?sig=preview"


class FailingPreviewStorage:
    def issue_preview(self, *, bucket, key, expires_in):
        raise StorageOperationError("https://secret-storage-endpoint.invalid")


@override_settings(**PRIVATE_MEDIA_SETTINGS)
class MediaPreviewServiceTests(SubmissionFixtureMixin, TestCase):
    category_slug = "outdoors"

    def setUp(self):
        self.build_users()
        user_model = get_user_model()
        self.moderator = user_model.objects.create_user(
            email=f"preview-moderator-{uuid.uuid4().hex}@smokemap.test",
            password="test",
            is_staff=True,
        )
        self.administrator = user_model.objects.create_user(
            email=f"preview-admin-{uuid.uuid4().hex}@smokemap.test",
            password="test",
            is_superuser=True,
        )
        self.submission = self.create_draft()
        self.intent, self.image = self.attach_managed_image(self.submission, 0)

    def set_state(self, state):
        self.submission.state = state
        self.submission.approved = state == Request.State.APPROVED
        self.submission.save(update_fields=["state", "approved"])

    # ---- authentication ---------------------------------------------------

    def test_guest_and_inactive_are_unauthenticated(self):
        storage = FakePreviewStorage()
        for actor in (
            SimpleNamespace(is_authenticated=False, is_active=False),
            self.inactive,
        ):
            with self.assertRaises(MediaStateConflict) as caught:
                issue_media_preview(actor, self.image.pk, storage=storage)
            self.assertEqual(caught.exception.code, "UNAUTHENTICATED")
        self.assertEqual(storage.calls, [])

    # ---- owner state matrix ------------------------------------------------

    def test_owner_can_preview_draft_and_pending(self):
        storage = FakePreviewStorage()
        for state in (Request.State.DRAFT, Request.State.PENDING):
            self.set_state(state)
            image, url, expires_at = issue_media_preview(
                self.owner, self.image.pk, storage=storage
            )
            self.assertEqual(image.pk, self.image.pk)
            self.assertTrue(url)
            self.assertLessEqual(
                (expires_at - timezone.now()).total_seconds(),
                MEDIA_PREVIEW_LIFETIME_SECONDS,
            )

    def test_owner_cannot_preview_terminal_or_review_states(self):
        storage = FakePreviewStorage()
        for state in (
            Request.State.WITHDRAWN,
            Request.State.EXPIRED,
            Request.State.APPROVED,
            Request.State.REJECTED,
        ):
            self.set_state(state)
            with self.assertRaises(MediaStateConflict) as caught:
                issue_media_preview(self.owner, self.image.pk, storage=storage)
            self.assertEqual(caught.exception.code, "NOT_FOUND")
        self.assertEqual(storage.calls, [])

    # ---- cross-owner and reviewer matrix ------------------------------------

    def test_other_owner_gets_not_found_regardless_of_state(self):
        storage = FakePreviewStorage()
        for state in (Request.State.DRAFT, Request.State.PENDING):
            self.set_state(state)
            with self.assertRaises(MediaStateConflict) as caught:
                issue_media_preview(self.other_owner, self.image.pk, storage=storage)
            self.assertEqual(caught.exception.code, "NOT_FOUND")
        self.assertEqual(storage.calls, [])

    def test_moderator_and_administrator_can_preview_pending_but_not_another_owners_draft(self):
        storage = FakePreviewStorage()
        for reviewer in (self.moderator, self.administrator):
            self.set_state(Request.State.DRAFT)
            with self.assertRaises(MediaStateConflict) as caught:
                issue_media_preview(reviewer, self.image.pk, storage=storage)
            self.assertEqual(caught.exception.code, "NOT_FOUND")

            self.set_state(Request.State.PENDING)
            _image, url, _expires_at = issue_media_preview(
                reviewer, self.image.pk, storage=storage
            )
            self.assertTrue(url)

    def test_moderator_can_preview_their_own_draft_through_the_owner_path(self):
        submission = self.create_draft(owner=self.moderator)
        _intent, image = self.attach_managed_image(submission, 0, owner=self.moderator)
        storage = FakePreviewStorage()
        _image, url, _expires_at = issue_media_preview(
            self.moderator, image.pk, storage=storage
        )
        self.assertTrue(url)

    # ---- exact-object binding, shape, and eligibility ------------------------

    def test_preview_binds_exactly_the_sealed_object_and_is_bounded(self):
        storage = FakePreviewStorage()
        issue_media_preview(self.owner, self.image.pk, storage=storage)

        self.assertEqual(len(storage.calls), 1)
        call_kwargs = storage.calls[0]
        self.assertEqual(call_kwargs["bucket"], self.image.storage_bucket)
        self.assertEqual(call_kwargs["key"], self.image.storage_key)
        self.assertLessEqual(call_kwargs["expires_in"], 600)
        self.assertLessEqual(MEDIA_PREVIEW_LIFETIME_SECONDS, 600)

    def test_missing_malformed_legacy_and_removed_attachments_are_not_found(self):
        storage = FakePreviewStorage()

        with self.assertRaises(MediaStateConflict) as missing:
            issue_media_preview(self.owner, self.image.pk + 1_000_000, storage=storage)
        self.assertEqual(missing.exception.code, "NOT_FOUND")

        with self.assertRaises(MediaStateConflict) as malformed:
            issue_media_preview(self.owner, uuid.uuid4(), storage=storage)
        self.assertEqual(malformed.exception.code, "NOT_FOUND")

        legacy = Image.objects.create(set_id="", name="legacy.png", url="https://public.invalid/legacy.png")
        with self.assertRaises(MediaStateConflict) as legacy_caught:
            issue_media_preview(self.owner, legacy.pk, storage=storage)
        self.assertEqual(legacy_caught.exception.code, "NOT_FOUND")

        remove_attached_media(self.owner, self.intent.pk, "remove-before-preview")
        self.assertFalse(Image.objects.filter(pk=self.image.pk).exists())
        with self.assertRaises(MediaStateConflict) as removed:
            issue_media_preview(self.owner, self.image.pk, storage=storage)
        self.assertEqual(removed.exception.code, "NOT_FOUND")

        self.assertEqual(storage.calls, [])

    # ---- storage failure ----------------------------------------------------

    def test_storage_failure_propagates_without_denying_or_granting_access(self):
        with self.assertRaises(StorageOperationError):
            issue_media_preview(self.owner, self.image.pk, storage=FailingPreviewStorage())

    # ---- GraphQL surface ------------------------------------------------------

    def test_preview_type_exposes_only_url_and_expiry(self):
        preview_type = schema.graphql_schema.get_type("MediaPreviewAuthorization")
        self.assertEqual(set(preview_type.fields.keys()), {"url", "expiresAt"})

    def test_graphql_guest_and_inactive_are_unauthenticated(self):
        for user in (
            SimpleNamespace(is_authenticated=False, is_active=False),
            self.inactive,
        ):
            result = self.graphql.execute(
                MEDIA_ATTACHMENT_PREVIEW,
                variable_values={"id": str(self.image.pk)},
                context_value=self.context(user),
            )
            self.assertEqual(self.error_code(result), "UNAUTHENTICATED")

    def test_graphql_other_owner_is_not_found(self):
        result = self.graphql.execute(
            MEDIA_ATTACHMENT_PREVIEW,
            variable_values={"id": str(self.image.pk)},
            context_value=self.context(self.other_owner),
        )
        self.assertEqual(self.error_code(result), "NOT_FOUND")

    @override_settings(
        AWS_ACCESS_KEY_ID="minio-access",
        AWS_SECRET_ACCESS_KEY="minio-secret",
        AWS_S3_REGION_NAME="us-east-1",
        AWS_S3_ADDRESSING_STYLE="path",
        MEDIA_STORAGE_INTERNAL_ENDPOINT_URL="http://storage:9000",
        MEDIA_UPLOAD_ENDPOINT_URL="http://localhost:19000",
    )
    def test_graphql_round_trip_issues_a_browser_reachable_get_only_capability(self):
        result = self.graphql.execute(
            MEDIA_ATTACHMENT_PREVIEW,
            variable_values={"id": str(self.image.pk)},
            context_value=self.context(self.owner),
        )

        self.assertNotIn("errors", result)
        preview = result["data"]["mediaAttachmentPreviewV3"]
        self.assertEqual(set(preview.keys()), {"url", "expiresAt"})
        self.assertTrue(preview["url"].startswith("http://localhost:19000/"))
        self.assertIn(self.image.storage_key, preview["url"])
        self.assertIn("X-Amz-Signature", preview["url"])
        # A read authorization, never a write/list form.
        self.assertNotIn("Policy", preview["url"])

    def test_graphql_maps_storage_failure_without_leaking_endpoint(self):
        endpoint = "https://secret-storage-endpoint.invalid"
        with patch(
            "backend.media.configured_media_storage",
            side_effect=StorageOperationError(endpoint),
        ):
            result = self.graphql.execute(
                MEDIA_ATTACHMENT_PREVIEW,
                variable_values={"id": str(self.image.pk)},
                context_value=self.context(self.owner),
            )
        self.assertEqual(self.error_code(result), "MEDIA_STORAGE_UNAVAILABLE")
        self.assertNotIn(endpoint, result["errors"][0]["message"])


class S3MediaStoragePreviewTests(SimpleTestCase):
    @override_settings(
        AWS_ACCESS_KEY_ID="private-access-key",
        AWS_SECRET_ACCESS_KEY="private-secret-key",
        AWS_S3_REGION_NAME="us-east-1",
        AWS_S3_ADDRESSING_STYLE="path",
        MEDIA_STORAGE_INTERNAL_ENDPOINT_URL="http://storage:9000",
        MEDIA_UPLOAD_ENDPOINT_URL="http://localhost:19000",
    )
    def test_preview_is_signed_by_the_browser_reachable_upload_client_only(self):
        internal_client = Mock()
        upload_client = Mock()
        upload_client.generate_presigned_url.return_value = (
            "http://localhost:19000/private-media/submission-media-sealed/1/x?sig=1"
        )

        with patch(
            "backend.media_storage.boto3.client",
            side_effect=[internal_client, upload_client],
        ):
            storage = S3MediaStorage()

        url = storage.issue_preview(
            bucket="private-media",
            key="submission-media-sealed/1/x",
            expires_in=600,
        )

        self.assertEqual(
            url, "http://localhost:19000/private-media/submission-media-sealed/1/x?sig=1"
        )
        upload_client.generate_presigned_url.assert_called_once_with(
            "get_object",
            Params={"Bucket": "private-media", "Key": "submission-media-sealed/1/x"},
            ExpiresIn=600,
        )
        self.assertEqual(
            upload_client.method_calls,
            [
                call.generate_presigned_url(
                    "get_object",
                    Params={
                        "Bucket": "private-media",
                        "Key": "submission-media-sealed/1/x",
                    },
                    ExpiresIn=600,
                )
            ],
        )
        internal_client.generate_presigned_url.assert_not_called()
        internal_client.get_object.assert_not_called()
        internal_client.put_object.assert_not_called()

    def test_preview_storage_failure_is_wrapped_without_leaking_detail(self):
        endpoint = "https://secret-storage-endpoint.invalid"
        upload_client = Mock()
        upload_client.generate_presigned_url.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": endpoint}}, "GetObject"
        )
        storage = S3MediaStorage(client=Mock(), upload_client=upload_client)

        with self.assertRaises(StorageOperationError) as caught:
            storage.issue_preview(bucket="b", key="k", expires_in=60)
        self.assertNotIn(endpoint, str(caught.exception))
