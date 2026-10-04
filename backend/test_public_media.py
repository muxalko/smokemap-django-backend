import hashlib
import io
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, SimpleTestCase, TransactionTestCase, override_settings
from PIL import Image as PillowImage

from .media import inspect_uploaded_object, process_media_cleanup
from .media_storage import StorageOperationError
from .models import (
    Category,
    Image,
    MediaUploadIntent,
    ModerationAudit,
    Place,
    PublicMediaRendition,
    Request,
    SubmissionIdempotency,
    SubmissionOperation,
)
from .moderation import ModerationPermissionDenied, approve_submission
from .public_media import (
    PublicMediaUnavailable,
    retrieve_public_media,
    revoke_public_media,
)
from .schema import schema
from .serializers import PlaceSerializer
from .submissions import SubmissionNotFound
from .test_media import FakeMediaStorage, encoded_image
from .test_moderation_lifecycle import ModerationFixtureMixin, PRIVATE_MEDIA_SETTINGS


class RenditionSanitizationTests(SimpleTestCase):
    def test_decoded_rendition_strips_exif_icc_and_text_metadata(self):
        source = io.BytesIO()
        image = PillowImage.new("RGB", (8, 6), color=(12, 34, 56))
        exif = PillowImage.Exif()
        exif[0x010E] = "private submission description"
        image.save(
            source,
            format="JPEG",
            exif=exif,
            icc_profile=b"private-icc-profile",
            comment=b"private-comment",
        )
        body = source.getvalue()
        storage = FakeMediaStorage({"upload": body})

        outcome = inspect_uploaded_object(
            storage,
            bucket="private",
            key="upload",
            sealed_key="submission-media-sealed/1/00000000000000000000000000000000",
            rendition_key="submission-media-renditions/1/00000000000000000000000000000000",
            expected_size=len(body),
            expected_sha256=hashlib.sha256(body).hexdigest(),
            expected_mime="image/jpeg",
        )

        self.assertEqual(outcome.failure_code, "")
        self.assertEqual(storage.objects["submission-media-sealed/1/00000000000000000000000000000000"], body)
        rendition = storage.objects[
            "submission-media-renditions/1/00000000000000000000000000000000"
        ]
        self.assertNotEqual(rendition, body)
        with PillowImage.open(io.BytesIO(rendition)) as decoded:
            decoded.load()
            self.assertEqual(decoded.size, (8, 6))
            self.assertEqual(len(decoded.getexif()), 0)
            self.assertNotIn("icc_profile", decoded.info)
            self.assertNotIn("comment", decoded.info)

    def test_rendition_storage_failure_is_a_stable_failed_inspection(self):
        body = encoded_image("PNG", (4, 4))

        class RenditionFailingStorage(FakeMediaStorage):
            def seal_object(inner_self, *, bucket, key, body, content_type, content_length):
                super().seal_object(
                    bucket=bucket,
                    key=key,
                    body=body,
                    content_type=content_type,
                    content_length=content_length,
                )
                if "renditions" in key:
                    raise StorageOperationError("injected rendition failure")

        storage = RenditionFailingStorage({"upload": body})
        outcome = inspect_uploaded_object(
            storage,
            bucket="private",
            key="upload",
            sealed_key="submission-media-sealed/1/00000000000000000000000000000000",
            rendition_key="submission-media-renditions/1/00000000000000000000000000000000",
            expected_size=len(body),
            expected_sha256=hashlib.sha256(body).hexdigest(),
            expected_mime="image/png",
        )

        self.assertEqual(outcome.failure_code, "object_seal_failed")
        self.assertTrue(outcome.sealed)


@override_settings(**PRIVATE_MEDIA_SETTINGS)
class PublicMediaContractTests(ModerationFixtureMixin, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        Category.objects.get_or_create(
            slug="outdoors",
            defaults={"name": "Outdoors", "description": "Outside."},
        )
        self.build_fixtures()
        self.submission = self.create_pending()
        self.intent, self.image = self.attach_media(self.submission)
        self.body = encoded_image("PNG", (2, 2))
        digest = hashlib.sha256(self.body).hexdigest()
        MediaUploadIntent.objects.filter(pk=self.intent.pk).update(
            rendition_byte_size=len(self.body),
            rendition_sha256=digest,
            rendition_mime="image/png",
        )
        self.intent.refresh_from_db()

    def approve(self, key="approve-public-media"):
        return approve_submission(
            self.moderator, self.submission.pk, key, "Safe publication"
        )

    def storage(self):
        return FakeMediaStorage(
            {
                self.intent.object_key: b"upload-original",
                self.intent.sealed_object_key: b"private-original",
                self.intent.rendition_object_key: self.body,
            }
        )

    def test_approval_publishes_one_safe_record_and_replay_does_not_duplicate(self):
        first = self.approve()
        replay = self.approve()

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.place.pk, replay.place.pk)
        rendition = PublicMediaRendition.objects.get()
        self.assertEqual(rendition.place, first.place)
        self.assertEqual(rendition.source_image, self.image)
        self.assertEqual(rendition.intent, self.intent)
        self.assertEqual(rendition.state, PublicMediaRendition.State.PUBLISHED)
        self.assertEqual(PublicMediaRendition.objects.count(), 1)

    def test_public_rest_and_graphql_metadata_expose_only_application_fields(self):
        result = self.approve()
        rendition = PublicMediaRendition.objects.get()
        request = RequestFactory().get("/")
        request.user = AnonymousUser()

        serialized = PlaceSerializer(result.place, context={"request": request}).data
        public_media = serialized["properties"]["media"]
        self.assertEqual(len(public_media), 1)
        self.assertEqual(
            set(public_media[0]),
            {"public_id", "url", "position", "mime_type", "byte_size", "width", "height"},
        )
        rendered = repr(public_media)
        for secret in (
            self.intent.object_key,
            self.intent.sealed_object_key,
            self.intent.rendition_object_key,
            str(self.intent.pk),
            str(self.submission.pk),
            str(self.owner.pk),
            self.intent.server_sha256,
        ):
            self.assertNotIn(secret, rendered)

        graphql = schema.execute(
            """
            query PublicPlace($id: ID!) {
              placeById(id: $id) {
                media { publicId url position mimeType byteSize width height }
              }
            }
            """,
            variable_values={"id": str(result.place.pk)},
            context_value=request,
        )
        self.assertIsNone(graphql.errors)
        self.assertEqual(
            graphql.data["placeById"]["media"][0]["publicId"],
            str(rendition.public_id),
        )
        self.assertIn(f"/api/v1/media/{rendition.public_id}/", repr(graphql.data))

    def test_anonymous_retrieval_rechecks_current_approval_and_exact_binding(self):
        self.approve()
        rendition = PublicMediaRendition.objects.get()
        storage = self.storage()

        payload = retrieve_public_media(rendition.public_id, storage=storage)
        self.assertEqual(payload.body, self.body)
        self.assertEqual(payload.mime_type, "image/png")
        self.assertEqual(
            storage.read_calls,
            [(self.intent.storage_bucket, self.intent.rendition_object_key)],
        )
        self.assertNotIn((self.intent.storage_bucket, self.intent.sealed_object_key), storage.read_calls)

        Request.objects.filter(pk=self.submission.pk).update(
            state=Request.State.REJECTED, approved=False
        )
        with self.assertRaises(SubmissionNotFound):
            retrieve_public_media(rendition.public_id, storage=storage)

    def test_retrieval_storage_failure_is_non_secret_and_does_not_mutate_publication(self):
        self.approve()
        rendition = PublicMediaRendition.objects.get()
        storage = FakeMediaStorage()

        with self.assertRaises(PublicMediaUnavailable) as caught:
            retrieve_public_media(rendition.public_id, storage=storage)

        self.assertNotIn(self.intent.rendition_object_key, str(caught.exception))
        rendition.refresh_from_db()
        self.assertEqual(rendition.state, PublicMediaRendition.State.PUBLISHED)

    def test_approval_failure_rolls_back_place_publication_and_state(self):
        with patch(
            "backend.moderation.PublicMediaRendition.objects.create",
            side_effect=RuntimeError("injected publication failure"),
        ):
            with self.assertRaises(RuntimeError):
                self.approve("approval-rollback")

        self.submission.refresh_from_db()
        self.assertEqual(self.submission.state, Request.State.PENDING)
        self.assertFalse(self.submission.approved)
        self.assertEqual(Place.objects.count(), 0)
        self.assertEqual(PublicMediaRendition.objects.count(), 0)
        self.assertEqual(
            SubmissionIdempotency.objects.filter(
                operation=SubmissionOperation.APPROVE,
                key="approval-rollback",
            ).count(),
            0,
        )

    def test_administrator_revocation_is_audited_idempotent_and_cleanup_safe(self):
        self.approve()
        rendition = PublicMediaRendition.objects.get()
        storage = self.storage()

        first = revoke_public_media(
            self.administrator, rendition.public_id, "revoke-public-media"
        )
        replay = revoke_public_media(
            self.administrator, rendition.public_id, "revoke-public-media"
        )

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        rendition.refresh_from_db()
        self.intent.refresh_from_db()
        self.assertEqual(rendition.state, PublicMediaRendition.State.REVOKED)
        self.assertIsNotNone(rendition.revoked_at)
        self.assertIsNone(rendition.source_image_id)
        self.assertFalse(Image.objects.filter(pk=self.image.pk).exists())
        self.assertEqual(self.intent.state, MediaUploadIntent.State.CLEANUP_PENDING)
        self.assertEqual(self.intent.failure_code, "public_media_revoked")
        self.assertEqual(
            ModerationAudit.objects.filter(
                action=ModerationAudit.Action.REVOKE_MEDIA,
                target_id=rendition.pk,
            ).count(),
            1,
        )
        self.assertEqual(
            SubmissionIdempotency.objects.filter(
                operation=SubmissionOperation.MEDIA_REVOKE,
                media_intent=self.intent,
            ).count(),
            1,
        )
        with self.assertRaises(SubmissionNotFound):
            retrieve_public_media(rendition.public_id, storage=storage)

        counts = process_media_cleanup(storage=storage)
        self.intent.refresh_from_db()
        self.assertEqual(counts.deleted, 1)
        self.assertEqual(self.intent.state, MediaUploadIntent.State.DELETED)
        self.assertNotIn(self.intent.object_key, storage.objects)
        self.assertNotIn(self.intent.sealed_object_key, storage.objects)
        self.assertNotIn(self.intent.rendition_object_key, storage.objects)

    def test_revocation_denies_non_admin_and_cleanup_failure_keeps_revoked_truth(self):
        self.approve()
        rendition = PublicMediaRendition.objects.get()
        with self.assertRaises(ModerationPermissionDenied):
            revoke_public_media(self.owner, rendition.public_id, "owner-revoke")

        revoke_public_media(self.administrator, rendition.public_id, "admin-revoke")
        storage = self.storage()
        storage.fail_delete = True
        counts = process_media_cleanup(storage=storage)

        rendition.refresh_from_db()
        self.intent.refresh_from_db()
        self.assertEqual(rendition.state, PublicMediaRendition.State.REVOKED)
        self.assertEqual(self.intent.state, MediaUploadIntent.State.CLEANUP_PENDING)
        self.assertEqual(counts.failed, 1)
