import hashlib
import threading
import time
import uuid
from datetime import timedelta
from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from django.db import (
    IntegrityError,
    close_old_connections,
    connection,
    transaction,
)
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from graphene.test import Client as GraphQLClient

from . import submissions as submission_services
from .media import MediaStateConflict, create_upload_intent
from .models import (
    Address,
    Category,
    Image,
    MediaUploadIntent,
    Place,
    Request,
    RequestTag,
    SubmissionIdempotency,
    SubmissionLifecycleEvent,
    SubmissionOperation,
)
from .schema import schema
from .submissions import (
    DuplicateSubmission,
    IdempotencyConflict,
    MediaNotReady,
    SubmissionInputError,
    SubmissionNotFound,
    SubmissionStateError,
    canonical_name_lock_key,
    canonical_place_name,
    create_submission,
    edit_submission,
    finalize_submission,
)


EDIT_SUBMISSION = """
    mutation Edit($id: ID!, $key: String!, $input: SubmissionV3Input!) {
      editSubmissionV3(submissionId: $id, idempotencyKey: $key, input: $input) {
        submission { id name state description website tags }
        replayed
      }
    }
"""

FINALIZE_SUBMISSION = """
    mutation Finalize($id: ID!, $key: String!) {
      finalizeSubmissionV3(submissionId: $id, idempotencyKey: $key) {
        submission { id name state }
        replayed
      }
    }
"""

PRIVATE_MEDIA_SETTINGS = {
    "AWS_STORAGE_BUCKET_NAME": "legacy-public-images",
    "MEDIA_STORAGE_BUCKET_NAME": "test-private-media",
    "MEDIA_STORAGE_IDENTIFIER": "test-s3-private",
}


def projected_point(longitude, latitude, metres, azimuth_degrees=90.0):
    """Return the WGS84 point exactly ``metres`` away along a geodesic azimuth."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT ST_X(projected::geometry), ST_Y(projected::geometry) FROM ("
            "  SELECT ST_Project("
            "    ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,"
            "    %s, radians(%s)"
            "  ) AS projected"
            ") AS geodesic",
            [longitude, latitude, metres, azimuth_degrees],
        )
        return cursor.fetchone()


class SubmissionFixtureMixin:
    """Shared owner-bound draft fixtures for the edit and finalize services."""

    longitude = -77.0365
    latitude = 38.8977

    def build_users(self):
        user_model = get_user_model()
        self.owner = user_model.objects.create_user(
            email=f"finalize-owner-{uuid.uuid4().hex}@smokemap.test", password="test"
        )
        self.other_owner = user_model.objects.create_user(
            email=f"finalize-other-{uuid.uuid4().hex}@smokemap.test", password="test"
        )
        self.inactive = user_model.objects.create_user(
            email=f"finalize-inactive-{uuid.uuid4().hex}@smokemap.test",
            password="test",
            is_active=False,
        )
        self.graphql = GraphQLClient(schema)

    def context(self, user):
        return SimpleNamespace(user=user, META={})

    def raw_input(self, **overrides):
        values = {
            "name": "Owner Draft",
            "category_slug": self.category_slug,
            "longitude": self.longitude,
            "latitude": self.latitude,
            "address_label": "Human label",
            "tags": ["Quiet Patio"],
            "description": "Original description",
            "website": "https://www.smokemap.org/original",
        }
        values.update(overrides)
        return values

    def graphql_input(self, **overrides):
        raw = self.raw_input(**overrides)
        return {
            "name": raw["name"],
            "categorySlug": raw["category_slug"],
            "longitude": raw["longitude"],
            "latitude": raw["latitude"],
            "addressLabel": raw["address_label"],
            "tags": raw["tags"],
            "description": raw["description"],
            "website": raw["website"],
        }

    def create_draft(self, owner=None, key=None, **overrides):
        submission, _replayed = create_submission(
            owner or self.owner,
            key or f"create-{uuid.uuid4().hex}",
            self.raw_input(**overrides),
        )
        return submission

    def error_code(self, result):
        return result["errors"][0]["extensions"]["code"]

    def attach_managed_image(self, submission, slot, *, owner=None, seed=None):
        owner = owner or self.owner
        now = timezone.now()
        digest = hashlib.sha256(
            (seed or f"{submission.pk}-{slot}-{uuid.uuid4().hex}").encode("utf-8")
        ).hexdigest()
        intent = MediaUploadIntent.objects.create(
            submission=submission,
            owner=owner,
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
            owner=owner,
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

    def create_intent(self, submission, slot, state, *, owner=None):
        owner = owner or self.owner
        now = timezone.now()
        digest = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        issued_states = {
            MediaUploadIntent.State.ISSUED,
            MediaUploadIntent.State.VERIFIED,
        }
        values = {
            "submission": submission,
            "owner": owner,
            "state": state,
            "slot": slot,
            "storage_identifier": "test-s3-private",
            "storage_bucket": "test-private-media",
            "object_key": f"submission-media/{submission.pk}/{uuid.uuid4().hex}",
            "sealed_object_key": (
                f"submission-media-sealed/{submission.pk}/{uuid.uuid4().hex}"
            ),
            "expected_mime": "image/png",
            "declared_byte_size": 64,
            "declared_sha256": digest,
            "created_at": now,
            "absolute_expires_at": now + timedelta(hours=24),
        }
        if state in issued_states:
            values["issued_at"] = now
            values["presign_expires_at"] = now + timedelta(minutes=10)
        if state == MediaUploadIntent.State.VERIFIED:
            values.update(
                server_byte_size=64,
                server_sha256=digest,
                detected_mime="image/png",
                width=2,
                height=2,
                verified_at=now,
            )
        return MediaUploadIntent.objects.create(**values)


@override_settings(**PRIVATE_MEDIA_SETTINGS)
class SubmissionEditTests(SubmissionFixtureMixin, TestCase):
    category_slug = "outdoors"

    def setUp(self):
        self.build_users()
        self.submission = self.create_draft()

    def edit(self, submission=None, key="edit-key", user=None, **overrides):
        return self.graphql.execute(
            EDIT_SUBMISSION,
            variable_values={
                "id": str((submission or self.submission).pk),
                "key": key,
                "input": self.graphql_input(**overrides),
            },
            context_value=self.context(user or self.owner),
        )

    def test_owner_edit_replaces_every_field_with_create_normalization(self):
        original_address_id = self.submission.address_id

        result = self.edit(
            name="  Ｅdited\tDraft  ",
            category_slug="rooftop",
            longitude=13.405,
            latitude=52.52,
            address_label="  Edited\tlabel  ",
            tags=["  Second\tTag ", "Ｆｉｒｓｔ Tag"],
            description="  Edited\n description ",
            website="HTTPS://BÜCHER.de:443/edited?q=yes",
        )

        self.assertNotIn("errors", result)
        payload = result["data"]["editSubmissionV3"]
        self.assertFalse(payload["replayed"])
        self.assertEqual(payload["submission"]["state"], "draft")

        submission = Request.objects.select_related("address", "category").get(
            pk=self.submission.pk
        )
        self.assertEqual(submission.name, "Edited Draft")
        self.assertEqual(submission.category.slug, "rooftop")
        self.assertEqual(submission.description, "Edited description")
        self.assertEqual(submission.website, "https://xn--bcher-kva.de/edited?q=yes")
        self.assertEqual(submission.address.addressString, "Edited label")
        self.assertEqual(
            (submission.address.location.x, submission.address.location.y),
            (13.405, 52.52),
        )
        self.assertEqual(submission.address.location.srid, 4326)
        self.assertEqual(submission.owner_id, self.owner.pk)
        self.assertEqual(submission.state, Request.State.DRAFT)

        links = list(
            RequestTag.objects.filter(request=submission)
            .select_related("tag")
            .order_by("position")
        )
        self.assertEqual([link.position for link in links], [0, 1])
        self.assertEqual([link.display for link in links], ["Second Tag", "First Tag"])
        self.assertEqual([link.tag.canonical for link in links], ["second tag", "first tag"])

        # A single authoritative Request and no second submission model.
        self.assertEqual(Request.objects.count(), 1)
        # The replaced address row was provably orphaned, so it is gone.
        self.assertFalse(Address.objects.filter(pk=original_address_id).exists())
        self.assertNotEqual(submission.address_id, original_address_id)

        idempotency = SubmissionIdempotency.objects.get(
            operation=SubmissionOperation.EDIT
        )
        self.assertEqual(idempotency.actor_id, self.owner.pk)
        self.assertEqual(idempotency.submission_id, submission.pk)
        self.assertIsNone(idempotency.media_intent_id)
        self.assertEqual(
            idempotency.original_result,
            {
                "snapshot_version": 1,
                "submission_id": submission.pk,
                "state": "draft",
                "name": "Edited Draft",
                "category_slug": "rooftop",
                "longitude": 13.405,
                "latitude": 52.52,
                "address_label": "Edited label",
                "tags": ["Second Tag", "First Tag"],
                "description": "Edited description",
                "website": "https://xn--bcher-kva.de/edited?q=yes",
            },
        )
        event = SubmissionLifecycleEvent.objects.get(
            operation=SubmissionOperation.EDIT
        )
        self.assertEqual(event.from_state, Request.State.DRAFT)
        self.assertEqual(event.to_state, Request.State.DRAFT)
        self.assertEqual(event.actor_id, self.owner.pk)
        self.assertEqual(event.idempotency_id, idempotency.pk)

    def test_unchanged_address_content_reuses_the_existing_address_row(self):
        original_address_id = self.submission.address_id
        result = self.edit(description="Only the description moved")

        self.assertNotIn("errors", result)
        submission = Request.objects.get(pk=self.submission.pk)
        self.assertEqual(submission.address_id, original_address_id)
        self.assertEqual(Address.objects.count(), 1)

    def test_shared_address_rows_are_replaced_rather_than_mutated(self):
        shared_address = self.submission.address
        place = Place.objects.create(
            name="Public place on the shared address",
            category=Category.objects.get(slug="indoors"),
            address=shared_address,
        )

        result = self.edit(longitude=1.5, latitude=2.5, address_label="Moved label")

        self.assertNotIn("errors", result)
        shared_address.refresh_from_db()
        place.refresh_from_db()
        self.assertEqual(place.address_id, shared_address.pk)
        self.assertEqual(shared_address.addressString, "Human label")
        self.assertEqual(
            (shared_address.location.x, shared_address.location.y),
            (self.longitude, self.latitude),
        )
        submission = Request.objects.select_related("address").get(pk=self.submission.pk)
        self.assertNotEqual(submission.address_id, shared_address.pk)
        self.assertEqual(
            (submission.address.location.x, submission.address.location.y), (1.5, 2.5)
        )

    def test_other_owner_and_missing_submission_share_one_not_found_surface(self):
        other_draft = self.create_draft(owner=self.other_owner)

        foreign = self.edit(submission=other_draft, key="foreign")
        missing = self.graphql.execute(
            EDIT_SUBMISSION,
            variable_values={
                "id": "987654321",
                "key": "missing",
                "input": self.graphql_input(),
            },
            context_value=self.context(self.owner),
        )

        self.assertEqual(self.error_code(foreign), "NOT_FOUND")
        self.assertEqual(self.error_code(missing), "NOT_FOUND")
        self.assertEqual(
            foreign["errors"][0]["message"], missing["errors"][0]["message"]
        )
        other_draft.refresh_from_db()
        self.assertEqual(other_draft.name, "Owner Draft")
        self.assertFalse(
            SubmissionIdempotency.objects.filter(
                operation=SubmissionOperation.EDIT
            ).exists()
        )

    def test_guest_and_inactive_accounts_cannot_edit(self):
        guest = SimpleNamespace(is_authenticated=False, is_active=False)
        for account in (guest, self.inactive):
            with self.subTest(account=account):
                result = self.edit(user=account, key=f"denied-{id(account)}")
                self.assertEqual(self.error_code(result), "UNAUTHENTICATED")
        self.assertFalse(
            SubmissionLifecycleEvent.objects.filter(
                operation=SubmissionOperation.EDIT
            ).exists()
        )

    def test_replay_returns_the_prior_result_and_conflicts_change_nothing(self):
        first = self.edit(key="edit-retry", name="Retried edit")
        replay = self.edit(key="edit-retry", name="Retried edit")
        changed = self.edit(key="edit-retry", name="Different payload")

        second_draft = self.create_draft(key="second-draft")
        other_target = self.edit(
            submission=second_draft, key="edit-retry", name="Retried edit"
        )

        self.assertNotIn("errors", first)
        self.assertFalse(first["data"]["editSubmissionV3"]["replayed"])
        self.assertTrue(replay["data"]["editSubmissionV3"]["replayed"])
        self.assertEqual(
            first["data"]["editSubmissionV3"]["submission"]["id"],
            replay["data"]["editSubmissionV3"]["submission"]["id"],
        )
        self.assertEqual(self.error_code(changed), "IDEMPOTENCY_CONFLICT")
        self.assertEqual(self.error_code(other_target), "IDEMPOTENCY_CONFLICT")

        self.assertEqual(
            SubmissionIdempotency.objects.filter(
                operation=SubmissionOperation.EDIT
            ).count(),
            1,
        )
        self.assertEqual(
            SubmissionLifecycleEvent.objects.filter(
                operation=SubmissionOperation.EDIT
            ).count(),
            1,
        )
        second_draft.refresh_from_db()
        self.assertEqual(second_draft.name, "Owner Draft")

    def test_delayed_edit_replay_returns_immutable_original_snapshot(self):
        first = self.edit(
            key="immutable-edit-a",
            name="First historical edit",
            category_slug="rooftop",
            longitude=13.405,
            latitude=52.52,
            address_label="First historical label",
            tags=["First Historical Tag"],
            description="First historical description",
            website="https://www.smokemap.org/first",
        )
        self.assertNotIn("errors", first)
        expected = first["data"]["editSubmissionV3"]["submission"]

        second = self.edit(
            key="immutable-edit-b",
            name="Second current edit",
            address_label="Second current label",
            tags=["Second Current Tag"],
            description="Second current description",
            website="https://www.smokemap.org/second",
        )
        self.assertNotIn("errors", second)
        finalized = self.graphql.execute(
            FINALIZE_SUBMISSION,
            variable_values={
                "id": str(self.submission.pk),
                "key": "immutable-finalize",
            },
            context_value=self.context(self.owner),
        )
        self.assertNotIn("errors", finalized)

        replay = self.edit(
            key="immutable-edit-a",
            name="First historical edit",
            category_slug="rooftop",
            longitude=13.405,
            latitude=52.52,
            address_label="First historical label",
            tags=["First Historical Tag"],
            description="First historical description",
            website="https://www.smokemap.org/first",
        )
        self.assertNotIn("errors", replay)
        self.assertTrue(replay["data"]["editSubmissionV3"]["replayed"])
        self.assertEqual(replay["data"]["editSubmissionV3"]["submission"], expected)
        self.assertEqual(expected["state"], "draft")
        self.assertEqual(expected["name"], "First historical edit")
        self.assertEqual(expected["tags"], ["First Historical Tag"])

        current = Request.objects.prefetch_related("request_tags").get(pk=self.submission.pk)
        self.assertEqual(current.state, Request.State.PENDING)
        self.assertEqual(current.name, "Second current edit")
        self.assertEqual(
            [link.display for link in current.request_tags.order_by("position")],
            ["Second Current Tag"],
        )
        self.assertEqual(
            SubmissionLifecycleEvent.objects.filter(
                submission=current, operation=SubmissionOperation.EDIT
            ).count(),
            2,
        )

    def test_the_same_key_stays_independent_per_actor(self):
        other_draft = self.create_draft(owner=self.other_owner)
        mine = self.edit(key="shared-key", name="My edit")
        theirs = self.graphql.execute(
            EDIT_SUBMISSION,
            variable_values={
                "id": str(other_draft.pk),
                "key": "shared-key",
                "input": self.graphql_input(name="Their edit"),
            },
            context_value=self.context(self.other_owner),
        )
        self.assertNotIn("errors", mine)
        self.assertNotIn("errors", theirs)
        self.assertEqual(
            SubmissionLifecycleEvent.objects.filter(
                operation=SubmissionOperation.EDIT
            ).count(),
            2,
        )

    def test_edit_is_allowed_only_in_draft(self):
        finalize_submission(self.owner, self.submission.pk, "finalize-before-edit")

        result = self.edit(key="edit-after-finalize", name="Late edit")

        self.assertEqual(self.error_code(result), "INVALID_SUBMISSION_STATE")
        submission = Request.objects.get(pk=self.submission.pk)
        self.assertEqual(submission.state, Request.State.PENDING)
        self.assertEqual(submission.name, "Owner Draft")
        self.assertFalse(
            SubmissionLifecycleEvent.objects.filter(
                operation=SubmissionOperation.EDIT
            ).exists()
        )

    def test_edit_reuses_the_exact_create_validation_rules(self):
        invalid = (
            ("name", {"name": "a"}),
            ("name", {"name": "x" * 101}),
            ("category_slug", {"category_slug": " outdoors"}),
            ("category_slug", {"category_slug": "Outdoors"}),
            ("longitude", {"longitude": 180.0001}),
            ("latitude", {"latitude": -90.0001}),
            ("address_label", {"address_label": "x" * 256}),
            ("description", {"description": "x" * 256}),
            ("website", {"website": "http://smokemap.org"}),
            ("website", {"website": "https://singlelabel"}),
            ("website", {"website": "https://127.0.0.1/path"}),
            ("tags", {"tags": ["Same Tag", "same tag"]}),
            ("tags", {"tags": ["ab"]}),
            ("tags", {"tags": [f"tag-{index}" for index in range(11)]}),
        )
        for index, (field, overrides) in enumerate(invalid):
            with self.subTest(field=field, overrides=overrides):
                result = self.edit(key=f"invalid-edit-{index}", **overrides)
                self.assertEqual(self.error_code(result), "INVALID_SUBMISSION")
                self.assertEqual(result["errors"][0]["extensions"]["field"], field)

        submission = Request.objects.get(pk=self.submission.pk)
        self.assertEqual(submission.name, "Owner Draft")
        self.assertEqual(Address.objects.count(), 1)
        self.assertFalse(
            SubmissionIdempotency.objects.filter(
                operation=SubmissionOperation.EDIT
            ).exists()
        )

    def test_optional_fields_become_null_and_tags_may_be_cleared(self):
        result = self.edit(
            key="clear-optional",
            address_label="   ",
            description="\t",
            website=" ",
            tags=[],
        )
        self.assertNotIn("errors", result)
        submission = Request.objects.select_related("address").get(pk=self.submission.pk)
        self.assertIsNone(submission.address.addressString)
        self.assertIsNone(submission.description)
        self.assertIsNone(submission.website)
        self.assertFalse(RequestTag.objects.filter(request=submission).exists())

    def test_injected_failure_leaves_no_partial_content_address_tag_or_evidence(self):
        original = Request.objects.select_related("address").get(pk=self.submission.pk)
        baseline = {
            "name": original.name,
            "address_id": original.address_id,
            "addresses": Address.objects.count(),
            "tags": sorted(
                RequestTag.objects.filter(request=original).values_list(
                    "display", "position"
                )
            ),
        }

        with patch(
            "backend.submissions.SubmissionLifecycleEvent.objects.create",
            side_effect=RuntimeError("injected edit failure"),
        ):
            result = self.edit(
                key="edit-rollback",
                name="Rolled back edit",
                longitude=1.25,
                latitude=2.5,
                tags=["Replacement Tag"],
            )

        self.assertEqual(self.error_code(result), "SUBMISSION_EDIT_FAILED")
        submission = Request.objects.select_related("address").get(pk=self.submission.pk)
        self.assertEqual(submission.name, baseline["name"])
        self.assertEqual(submission.address_id, baseline["address_id"])
        self.assertEqual(Address.objects.count(), baseline["addresses"])
        self.assertEqual(
            sorted(
                RequestTag.objects.filter(request=submission).values_list(
                    "display", "position"
                )
            ),
            baseline["tags"],
        )
        self.assertFalse(
            SubmissionIdempotency.objects.filter(
                operation=SubmissionOperation.EDIT
            ).exists()
        )
        self.assertFalse(
            SubmissionLifecycleEvent.objects.filter(
                operation=SubmissionOperation.EDIT
            ).exists()
        )

    def test_client_supplied_owner_and_state_fields_are_rejected(self):
        spoofed = self.graphql_input()
        spoofed.update({"owner": str(self.other_owner.pk), "state": "pending"})
        result = self.graphql.execute(
            EDIT_SUBMISSION,
            variable_values={
                "id": str(self.submission.pk),
                "key": "spoofed-edit",
                "input": spoofed,
            },
            context_value=self.context(self.owner),
        )
        self.assertIn("errors", result)
        submission = Request.objects.get(pk=self.submission.pk)
        self.assertEqual(submission.owner_id, self.owner.pk)
        self.assertEqual(submission.state, Request.State.DRAFT)

    def test_edit_never_performs_dns_network_or_object_store_calls(self):
        with patch("socket.getaddrinfo", side_effect=AssertionError("DNS called")), patch(
            "socket.socket", side_effect=AssertionError("network called")
        ), patch(
            "backend.media_storage.boto3.client",
            side_effect=AssertionError("object store called"),
        ):
            result = self.edit(key="no-network", website="https://www.smokemap.org/new")
        self.assertNotIn("errors", result)


@override_settings(**PRIVATE_MEDIA_SETTINGS)
class SubmissionFinalizationTests(SubmissionFixtureMixin, TestCase):
    category_slug = "outdoors"

    def setUp(self):
        self.build_users()
        self.submission = self.create_draft()

    def finalize(self, submission=None, key="finalize-key", user=None):
        return self.graphql.execute(
            FINALIZE_SUBMISSION,
            variable_values={
                "id": str((submission or self.submission).pk),
                "key": key,
            },
            context_value=self.context(user or self.owner),
        )

    def test_zero_media_finalization_moves_draft_to_pending_exactly_once(self):
        with patch("socket.getaddrinfo", side_effect=AssertionError("DNS called")), patch(
            "socket.socket", side_effect=AssertionError("network called")
        ), patch(
            "backend.media_storage.boto3.client",
            side_effect=AssertionError("object store called"),
        ):
            result = self.finalize()

        self.assertNotIn("errors", result)
        payload = result["data"]["finalizeSubmissionV3"]
        self.assertFalse(payload["replayed"])
        self.assertEqual(payload["submission"]["state"], "pending")

        submission = Request.objects.get(pk=self.submission.pk)
        self.assertEqual(submission.state, Request.State.PENDING)
        self.assertFalse(submission.approved)
        self.assertIsNone(submission.date_approved)
        self.assertIsNone(submission.reviewed_by_id)
        self.assertFalse(MediaUploadIntent.objects.exists())
        self.assertFalse(Image.objects.exists())

        idempotency = SubmissionIdempotency.objects.get(
            operation=SubmissionOperation.FINALIZE
        )
        self.assertEqual(idempotency.actor_id, self.owner.pk)
        self.assertEqual(idempotency.submission_id, submission.pk)
        self.assertEqual(
            idempotency.original_result,
            {
                "snapshot_version": 1,
                "submission_id": submission.pk,
                "state": "pending",
                "name": "Owner Draft",
                "category_slug": "outdoors",
                "longitude": self.longitude,
                "latitude": self.latitude,
                "address_label": "Human label",
                "tags": ["Quiet Patio"],
                "description": "Original description",
                "website": "https://www.smokemap.org/original",
            },
        )
        events = list(
            SubmissionLifecycleEvent.objects.filter(
                submission=submission, operation=SubmissionOperation.FINALIZE
            )
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].from_state, Request.State.DRAFT)
        self.assertEqual(events[0].to_state, Request.State.PENDING)
        self.assertEqual(events[0].idempotency_id, idempotency.pk)

    def test_finalization_succeeds_with_one_and_with_three_attachments(self):
        for count in (1, 3):
            with self.subTest(attachments=count):
                submission = self.create_draft(name=f"Draft with {count} images")
                for slot in range(count):
                    self.attach_managed_image(submission, slot)

                result = self.finalize(
                    submission=submission, key=f"finalize-media-{count}"
                )

                self.assertNotIn("errors", result)
                submission.refresh_from_db()
                self.assertEqual(submission.state, Request.State.PENDING)
                self.assertEqual(
                    Image.objects.filter(
                        request=submission, is_managed=True, state="attached"
                    ).count(),
                    count,
                )

    def test_unsettled_intents_block_finalization_without_writes(self):
        for state in (
            MediaUploadIntent.State.CREATED,
            MediaUploadIntent.State.ISSUED,
            MediaUploadIntent.State.VERIFIED,
            MediaUploadIntent.State.FAILED,
            MediaUploadIntent.State.EXPIRED,
            MediaUploadIntent.State.CLEANUP_PENDING,
        ):
            with self.subTest(state=state):
                submission = self.create_draft(name=f"Draft blocked by {state}")
                self.create_intent(submission, 0, state)

                with self.assertRaises(MediaNotReady):
                    finalize_submission(self.owner, submission.pk, f"blocked-{state}")

                submission.refresh_from_db()
                self.assertEqual(submission.state, Request.State.DRAFT)
                self.assertFalse(
                    SubmissionIdempotency.objects.filter(
                        submission=submission,
                        operation=SubmissionOperation.FINALIZE,
                    ).exists()
                )
                self.assertFalse(
                    SubmissionLifecycleEvent.objects.filter(
                        submission=submission,
                        operation=SubmissionOperation.FINALIZE,
                    ).exists()
                )

    def test_a_retained_attachment_whose_intent_left_attached_blocks(self):
        submission = self.create_draft(name="Draft with a cleaned-up attachment")
        intent, _image = self.attach_managed_image(submission, 0)
        MediaUploadIntent.objects.filter(pk=intent.pk).update(
            state=MediaUploadIntent.State.CLEANUP_PENDING
        )

        with self.assertRaises(MediaNotReady):
            finalize_submission(self.owner, submission.pk, "cleanup-pending-attachment")

        submission.refresh_from_db()
        self.assertEqual(submission.state, Request.State.DRAFT)

    def test_a_retained_attachment_owned_by_another_account_blocks(self):
        submission = self.create_draft(name="Draft with a foreign attachment")
        _intent, image = self.attach_managed_image(submission, 0)
        Image.objects.filter(pk=image.pk).update(owner=self.other_owner)

        with self.assertRaises(MediaNotReady):
            finalize_submission(self.owner, submission.pk, "foreign-attachment")

        submission.refresh_from_db()
        self.assertEqual(submission.state, Request.State.DRAFT)

    def test_deleted_intents_without_attachments_do_not_block(self):
        submission = self.create_draft(name="Draft after cleaned uploads")
        intent = self.create_intent(submission, 0, MediaUploadIntent.State.EXPIRED)
        MediaUploadIntent.objects.filter(pk=intent.pk).update(
            state=MediaUploadIntent.State.DELETED,
            deleted_at=timezone.now(),
        )

        submission, replayed = finalize_submission(
            self.owner, submission.pk, "deleted-intents"
        )

        self.assertFalse(replayed)
        self.assertEqual(submission.state, Request.State.PENDING)

    def test_attached_intent_without_attachment_row_blocks(self):
        submission = self.create_draft(name="Draft missing attachment row")
        intent, image = self.attach_managed_image(submission, 0)
        image.delete()

        with self.assertRaises(MediaNotReady):
            finalize_submission(self.owner, submission.pk, "missing-attachment-row")

        submission.refresh_from_db()
        self.assertEqual(submission.state, Request.State.DRAFT)
        intent.refresh_from_db()
        self.assertEqual(intent.state, MediaUploadIntent.State.ATTACHED)

    def test_replay_returns_the_prior_result_and_a_new_key_is_invalid(self):
        first = self.finalize(key="finalize-once")
        replay = self.finalize(key="finalize-once")
        second_key = self.finalize(key="finalize-twice")

        self.assertNotIn("errors", first)
        self.assertFalse(first["data"]["finalizeSubmissionV3"]["replayed"])
        self.assertTrue(replay["data"]["finalizeSubmissionV3"]["replayed"])
        self.assertEqual(
            replay["data"]["finalizeSubmissionV3"]["submission"]["state"], "pending"
        )
        self.assertEqual(self.error_code(second_key), "INVALID_SUBMISSION_STATE")
        self.assertEqual(
            SubmissionLifecycleEvent.objects.filter(
                operation=SubmissionOperation.FINALIZE
            ).count(),
            1,
        )
        self.assertEqual(
            SubmissionIdempotency.objects.filter(
                operation=SubmissionOperation.FINALIZE
            ).count(),
            1,
        )

    def test_the_same_key_against_another_target_is_an_idempotency_conflict(self):
        other_draft = self.create_draft(name="Second finalizable draft")
        first = self.finalize(key="shared-finalize-key")
        conflict = self.finalize(submission=other_draft, key="shared-finalize-key")

        self.assertNotIn("errors", first)
        self.assertEqual(self.error_code(conflict), "IDEMPOTENCY_CONFLICT")
        other_draft.refresh_from_db()
        self.assertEqual(other_draft.state, Request.State.DRAFT)
        self.assertEqual(
            SubmissionLifecycleEvent.objects.filter(
                operation=SubmissionOperation.FINALIZE
            ).count(),
            1,
        )

    def test_finalization_is_owner_only_and_privacy_safe(self):
        other_draft = self.create_draft(owner=self.other_owner)

        foreign = self.finalize(submission=other_draft, key="foreign-finalize")
        missing = self.graphql.execute(
            FINALIZE_SUBMISSION,
            variable_values={"id": "987654321", "key": "missing-finalize"},
            context_value=self.context(self.owner),
        )
        guest = self.finalize(
            user=SimpleNamespace(is_authenticated=False, is_active=False),
            key="guest-finalize",
        )
        inactive = self.finalize(user=self.inactive, key="inactive-finalize")

        self.assertEqual(self.error_code(foreign), "NOT_FOUND")
        self.assertEqual(self.error_code(missing), "NOT_FOUND")
        self.assertEqual(
            foreign["errors"][0]["message"], missing["errors"][0]["message"]
        )
        self.assertEqual(self.error_code(guest), "UNAUTHENTICATED")
        self.assertEqual(self.error_code(inactive), "UNAUTHENTICATED")
        other_draft.refresh_from_db()
        self.assertEqual(other_draft.state, Request.State.DRAFT)
        self.assertFalse(SubmissionLifecycleEvent.objects.filter(
            operation=SubmissionOperation.FINALIZE
        ).exists())

    def test_finalization_revalidates_the_current_stored_snapshot(self):
        Request.objects.filter(pk=self.submission.pk).update(name="x")

        result = self.finalize(key="revalidated")

        self.assertEqual(self.error_code(result), "INVALID_SUBMISSION")
        self.assertEqual(result["errors"][0]["extensions"]["field"], "name")
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.state, Request.State.DRAFT)

    def test_finalization_revalidates_an_edited_snapshot(self):
        edit_submission(
            self.owner,
            self.submission.pk,
            "edit-before-finalize",
            self.raw_input(name="Edited before finalize", tags=["Edited Tag"]),
        )
        submission, replayed = finalize_submission(
            self.owner, self.submission.pk, "finalize-after-edit"
        )
        self.assertFalse(replayed)
        self.assertEqual(submission.state, Request.State.PENDING)
        self.assertEqual(submission.name, "Edited before finalize")
        self.assertEqual(
            list(
                SubmissionLifecycleEvent.objects.filter(
                    submission=submission
                ).values_list("operation", flat=True)
            ),
            [
                SubmissionOperation.CREATE,
                SubmissionOperation.EDIT,
                SubmissionOperation.FINALIZE,
            ],
        )

    def test_media_apis_remain_fail_closed_once_pending(self):
        finalize_submission(self.owner, self.submission.pk, "before-media-lockdown")

        with self.assertRaises(MediaStateConflict) as caught:
            create_upload_intent(
                self.owner,
                self.submission.pk,
                "intent-after-finalize",
                mime_type="image/png",
                declared_byte_size=64,
                declared_sha256="0" * 64,
            )
        self.assertEqual(caught.exception.code, "MEDIA_STATE_CONFLICT")
        self.assertFalse(MediaUploadIntent.objects.exists())

    def test_media_not_ready_and_duplicate_surface_stable_graphql_codes(self):
        blocked = self.create_draft(name="Blocked by an open intent")
        self.create_intent(blocked, 0, MediaUploadIntent.State.ISSUED)
        not_ready = self.finalize(submission=blocked, key="graphql-media-not-ready")
        self.assertEqual(self.error_code(not_ready), "MEDIA_NOT_READY")

        Place.objects.create(
            name="owner  DRAFT",
            category=Category.objects.get(slug="outdoors"),
            address=Address.objects.create(
                location=Point(self.longitude, self.latitude, srid=4326)
            ),
        )
        duplicate = self.finalize(key="graphql-duplicate")
        self.assertEqual(self.error_code(duplicate), "DUPLICATE_SUBMISSION")

        self.submission.refresh_from_db()
        blocked.refresh_from_db()
        self.assertEqual(self.submission.state, Request.State.DRAFT)
        self.assertEqual(blocked.state, Request.State.DRAFT)
        self.assertFalse(
            SubmissionLifecycleEvent.objects.filter(
                operation=SubmissionOperation.FINALIZE
            ).exists()
        )

    def test_legacy_and_m4_write_paths_remain_unreachable(self):
        legacy_operations = (
            """
            mutation {
              createRequest(input: {
                name: "Legacy", category: "1", description: "legacy",
                addressString: "[1,2]", tags: [], website: "https://smokemap.org"
              }) { request { id } }
            }
            """,
            """
            mutation { approveRequest(id: "1", input: {approvedComment: "x"}) {
              request { id }
            } }
            """,
            """
            mutation { createImage(input: {
              requestId: "1", name: "n", url: "https://smokemap.org/i.png"
            }) { image { id } } }
            """,
            "query { s3PresignedUrl }",
        )
        for operation in legacy_operations:
            with self.subTest(operation=operation.strip()[:32]):
                result = self.graphql.execute(
                    operation, context_value=self.context(self.owner)
                )
                self.assertEqual(self.error_code(result), "FORBIDDEN")

        mutation_fields = set(schema.graphql_schema.mutation_type.fields)
        self.assertIn("editSubmissionV3", mutation_fields)
        self.assertIn("finalizeSubmissionV3", mutation_fields)
        for reserved in (
            "withdrawSubmissionV3",
            "withdrawSubmissionV4",
            "approveSubmissionV3",
            "approveSubmissionV4",
            "rejectSubmissionV3",
            "rejectSubmissionV4",
            "expireSubmissionV3",
        ):
            self.assertNotIn(reserved, mutation_fields)

        self.assertFalse(
            Request.objects.filter(
                state__in=[
                    Request.State.WITHDRAWN,
                    Request.State.APPROVED,
                    Request.State.REJECTED,
                ]
            ).exists()
        )

    def test_lifecycle_and_idempotency_uniqueness_is_enforced_for_edits(self):
        edit_submission(
            self.owner,
            self.submission.pk,
            "unique-edit",
            self.raw_input(name="Uniqueness probe"),
        )
        existing = SubmissionIdempotency.objects.get(operation=SubmissionOperation.EDIT)

        with self.assertRaises(IntegrityError), transaction.atomic():
            SubmissionIdempotency.objects.create(
                actor=self.owner,
                operation=SubmissionOperation.EDIT,
                key="unique-edit",
                request_hash="3" * 64,
                submission=self.submission,
                original_result={"state": "draft"},
            )

        with self.assertRaises(IntegrityError), transaction.atomic():
            SubmissionLifecycleEvent.objects.create(
                submission=self.submission,
                actor=self.owner,
                operation=SubmissionOperation.EDIT,
                from_state=Request.State.DRAFT,
                to_state=Request.State.DRAFT,
                outcome=SubmissionLifecycleEvent.Outcome.SUCCEEDED,
                idempotency=existing,
            )

    def test_database_rejects_an_edit_event_that_changes_state(self):
        idempotency = SubmissionIdempotency.objects.create(
            actor=self.owner,
            operation=SubmissionOperation.EDIT,
            key="invalid-edit-transition",
            request_hash="4" * 64,
            submission=self.submission,
            original_result={"state": "draft"},
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            SubmissionLifecycleEvent.objects.create(
                submission=self.submission,
                actor=self.owner,
                operation=SubmissionOperation.EDIT,
                from_state=Request.State.DRAFT,
                to_state=Request.State.PENDING,
                outcome=SubmissionLifecycleEvent.Outcome.SUCCEEDED,
                idempotency=idempotency,
            )

    def test_edit_idempotency_records_may_not_target_a_media_intent(self):
        submission = self.create_draft(name="Media target constraint")
        intent = self.create_intent(
            submission, 0, MediaUploadIntent.State.CREATED
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            SubmissionIdempotency.objects.create(
                actor=self.owner,
                operation=SubmissionOperation.EDIT,
                key="edit-with-media-target",
                request_hash="5" * 64,
                submission=submission,
                media_intent=intent,
                original_result={"state": "draft"},
            )

    def test_create_submission_semantics_and_hash_are_unchanged(self):
        submission, replayed = create_submission(
            self.owner, "unchanged-create", self.raw_input(name="Unchanged create")
        )
        replay, replayed_again = create_submission(
            self.owner, "unchanged-create", self.raw_input(name="Unchanged create")
        )
        self.assertFalse(replayed)
        self.assertTrue(replayed_again)
        self.assertEqual(submission.pk, replay.pk)
        record = SubmissionIdempotency.objects.get(
            operation=SubmissionOperation.CREATE, key="unchanged-create"
        )
        self.assertEqual(
            record.request_hash,
            submission_services.canonical_request_hash(
                submission_services.validate_submission_input(
                    self.raw_input(name="Unchanged create")
                )
            ),
        )


@override_settings(**PRIVATE_MEDIA_SETTINGS)
class SubmissionDuplicatePolicyTests(SubmissionFixtureMixin, TestCase):
    category_slug = "outdoors"

    def setUp(self):
        self.build_users()
        self.category = Category.objects.get(slug="outdoors")

    def create_place(self, name, longitude, latitude):
        return Place.objects.create(
            name=name,
            category=self.category,
            address=Address.objects.create(
                addressString=None, location=Point(longitude, latitude, srid=4326)
            ),
        )

    def test_canonical_name_is_nfkc_collapsed_and_case_folded(self):
        self.assertEqual(canonical_place_name("  Ｒooftop\tCAFÉ  "), "rooftop café")
        self.assertEqual(canonical_place_name("rooftop café"), "rooftop café")
        self.assertEqual(
            canonical_name_lock_key(canonical_place_name("  Ｒooftop\tCAFÉ  ")),
            canonical_name_lock_key(canonical_place_name("rooftop café")),
        )
        self.assertNotEqual(
            canonical_name_lock_key("rooftop café"),
            canonical_name_lock_key("rooftop cafe"),
        )
        # A truncated prefix must not collide with the full canonical name.
        self.assertNotEqual(
            canonical_name_lock_key("rooftop café"),
            canonical_name_lock_key("rooftop caf"),
        )

    def test_public_place_inside_the_inclusive_boundary_blocks_finalization(self):
        longitude, latitude = projected_point(
            self.longitude, self.latitude, 24.999999
        )
        self.create_place("Ｒooftop  CAFÉ", longitude, latitude)
        submission = self.create_draft(name="rooftop café")

        with self.assertRaises(DuplicateSubmission):
            finalize_submission(self.owner, submission.pk, "duplicate-inside")

        submission.refresh_from_db()
        self.assertEqual(submission.state, Request.State.DRAFT)
        self.assertFalse(
            SubmissionIdempotency.objects.filter(
                operation=SubmissionOperation.FINALIZE
            ).exists()
        )
        self.assertFalse(
            SubmissionLifecycleEvent.objects.filter(
                operation=SubmissionOperation.FINALIZE
            ).exists()
        )

    def test_the_same_canonical_name_just_outside_the_boundary_is_allowed(self):
        longitude, latitude = projected_point(
            self.longitude, self.latitude, 25.000001
        )
        self.create_place("Rooftop Café", longitude, latitude)
        submission = self.create_draft(name="rooftop café")

        submission, _replayed = finalize_submission(
            self.owner, submission.pk, "outside-boundary"
        )
        self.assertEqual(submission.state, Request.State.PENDING)

    def test_the_same_canonical_name_far_away_is_allowed(self):
        longitude, latitude = projected_point(self.longitude, self.latitude, 250.0)
        self.create_place("Rooftop Café", longitude, latitude)
        submission = self.create_draft(name="Rooftop Café")

        submission, _replayed = finalize_submission(
            self.owner, submission.pk, "far-away-same-name"
        )
        self.assertEqual(submission.state, Request.State.PENDING)

    def test_a_different_canonical_name_at_the_same_point_is_allowed(self):
        self.create_place("Another Rooftop", self.longitude, self.latitude)
        submission = self.create_draft(name="Rooftop Café")

        submission, _replayed = finalize_submission(
            self.owner, submission.pk, "same-point-different-name"
        )
        self.assertEqual(submission.state, Request.State.PENDING)

    def test_the_owner_other_draft_and_pending_proposals_block(self):
        for index, state in enumerate(
            (Request.State.DRAFT, Request.State.PENDING)
        ):
            with self.subTest(state=state):
                existing = self.create_draft(name="Shared Owner Name")
                Request.objects.filter(pk=existing.pk).update(state=state)
                candidate = self.create_draft(name="shared owner name")

                with self.assertRaises(DuplicateSubmission):
                    finalize_submission(
                        self.owner, candidate.pk, f"owner-duplicate-{index}"
                    )

                candidate.refresh_from_db()
                self.assertEqual(candidate.state, Request.State.DRAFT)
                Request.objects.filter(pk__in=(existing.pk, candidate.pk)).update(
                    state=Request.State.EXPIRED
                )

    def test_the_owner_terminal_proposals_do_not_block(self):
        for index, state in enumerate(
            (
                Request.State.WITHDRAWN,
                Request.State.EXPIRED,
                Request.State.REJECTED,
            )
        ):
            with self.subTest(state=state):
                existing = self.create_draft(name="Terminal Owner Name")
                Request.objects.filter(pk=existing.pk).update(state=state)
                candidate = self.create_draft(name="terminal owner name")

                candidate, _replayed = finalize_submission(
                    self.owner, candidate.pk, f"terminal-{index}"
                )
                self.assertEqual(candidate.state, Request.State.PENDING)
                Request.objects.filter(pk__in=(existing.pk, candidate.pk)).update(
                    state=Request.State.EXPIRED
                )

    def test_a_submission_never_blocks_itself(self):
        submission = self.create_draft(name="Self comparison")
        submission, _replayed = finalize_submission(
            self.owner, submission.pk, "self-comparison"
        )
        self.assertEqual(submission.state, Request.State.PENDING)

    def test_another_owner_private_proposal_neither_blocks_nor_leaks(self):
        foreign = self.create_draft(owner=self.other_owner, name="Private Neighbour")
        candidate = self.create_draft(name="private neighbour")

        queried = []
        real_owner_query = submission_services._nearby_owner_proposal_names

        def recording_owner_query(owner_id, exclude_id, longitude, latitude):
            queried.append(owner_id)
            return real_owner_query(owner_id, exclude_id, longitude, latitude)

        with patch.object(
            submission_services,
            "_nearby_owner_proposal_names",
            side_effect=recording_owner_query,
        ):
            candidate, _replayed = finalize_submission(
                self.owner, candidate.pk, "cross-owner"
            )

        self.assertEqual(candidate.state, Request.State.PENDING)
        self.assertEqual(queried, [self.owner.pk])
        foreign.refresh_from_db()
        self.assertEqual(foreign.state, Request.State.DRAFT)

        # The other owner may still finalize the identical proposal.
        foreign, _replayed = finalize_submission(
            self.other_owner, foreign.pk, "cross-owner-second"
        )
        self.assertEqual(foreign.state, Request.State.PENDING)

    def test_the_advisory_lock_precedes_the_spatial_check_with_no_matching_row(self):
        submission = self.create_draft(name="Uncontested Canonical Name")
        order = []
        real_lock = submission_services.acquire_canonical_name_lock
        real_check = submission_services._assert_no_duplicate

        def recording_lock(canonical):
            order.append(("lock", canonical))
            return real_lock(canonical)

        def recording_check(actor, target, validated, canonical):
            order.append(("spatial-check", canonical))
            return real_check(actor, target, validated, canonical)

        with patch.object(
            submission_services,
            "acquire_canonical_name_lock",
            side_effect=recording_lock,
        ), patch.object(
            submission_services, "_assert_no_duplicate", side_effect=recording_check
        ):
            submission, _replayed = finalize_submission(
                self.owner, submission.pk, "uncontested"
            )

        self.assertEqual(submission.state, Request.State.PENDING)
        self.assertEqual(
            order,
            [
                ("lock", "uncontested canonical name"),
                ("spatial-check", "uncontested canonical name"),
            ],
        )

    def test_duplicate_detection_uses_geography_dwithin_at_25_metres(self):
        submission = self.create_draft(name="Predicate probe")
        captured = {}
        real_places = submission_services._nearby_public_place_names

        def recording_places(longitude, latitude):
            captured["arguments"] = (longitude, latitude)
            return real_places(longitude, latitude)

        with patch.object(
            submission_services,
            "_nearby_public_place_names",
            side_effect=recording_places,
        ):
            finalize_submission(self.owner, submission.pk, "predicate-probe")

        self.assertEqual(captured["arguments"], (self.longitude, self.latitude))
        self.assertEqual(submission_services.DUPLICATE_RADIUS_METRES, 25.0)


@override_settings(**PRIVATE_MEDIA_SETTINGS)
class SubmissionRaceTests(SubmissionFixtureMixin, TransactionTestCase):
    reset_sequences = True
    category_slug = "race-test"

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

    def test_same_canonical_name_finalizations_never_overlap(self):
        """Two owners share one canonical name, so the advisory lock alone orders them."""
        first = self.create_draft(name="Contested Canonical Name")
        second = self.create_draft(
            owner=self.other_owner, name="contested  CANONICAL name"
        )
        maximum = self.measure_overlap([(self.owner, first), (self.other_owner, second)])

        self.assertEqual(maximum, 1)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.state, Request.State.PENDING)
        self.assertEqual(second.state, Request.State.PENDING)
        self.assertEqual(
            SubmissionLifecycleEvent.objects.filter(
                operation=SubmissionOperation.FINALIZE
            ).count(),
            2,
        )

    def test_distinct_canonical_names_are_not_serialized_by_the_lock(self):
        first = self.create_draft(name="Distinct Name One")
        second = self.create_draft(owner=self.other_owner, name="Distinct Name Two")
        maximum = self.measure_overlap([(self.owner, first), (self.other_owner, second)])

        self.assertEqual(maximum, 2)

    def measure_overlap(self, pairs):
        barrier = threading.Barrier(len(pairs), timeout=60)
        guard = threading.Lock()
        counters = {"inside": 0, "maximum": 0}
        errors = Queue()
        real_check = submission_services._assert_no_duplicate

        def instrumented(actor, target, validated, canonical):
            with guard:
                counters["inside"] += 1
                counters["maximum"] = max(counters["maximum"], counters["inside"])
            try:
                time.sleep(0.5)
                return real_check(actor, target, validated, canonical)
            finally:
                with guard:
                    counters["inside"] -= 1

        def worker(actor_id, submission_id, key):
            try:
                close_old_connections()
                actor = get_user_model().objects.get(pk=actor_id)
                barrier.wait()
                finalize_submission(actor, submission_id, key)
            except Exception as error:  # surfaced by the assertions below
                errors.put(error)
            finally:
                close_old_connections()

        with patch.object(
            submission_services, "_assert_no_duplicate", side_effect=instrumented
        ):
            self.run_threads(
                [
                    (lambda actor=actor, target=target, index=index: worker(
                        actor.pk, target.pk, f"overlap-{index}"
                    ))
                    for index, (actor, target) in enumerate(pairs)
                ]
            )

        self.assertTrue(errors.empty(), msg=list(errors.queue))
        return counters["maximum"]

    def test_foreign_row_lock_does_not_stall_private_not_found_lookup(self):
        foreign = self.create_draft(owner=self.other_owner, name="Foreign locked draft")
        locked = threading.Event()
        release = threading.Event()
        denied_done = threading.Event()
        outcomes = Queue()
        foreign_id = foreign.pk
        owner_id = self.owner.pk

        def holder():
            close_old_connections()
            try:
                with transaction.atomic():
                    Request.objects.select_for_update().get(pk=foreign_id)
                    locked.set()
                    release.wait(timeout=30)
            finally:
                close_old_connections()

        def denied_lookup():
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=owner_id)
                edit_submission(
                    actor, foreign_id, "foreign-lock-probe", self.raw_input()
                )
            except Exception as error:
                outcomes.put(type(error).__name__)
            finally:
                denied_done.set()
                close_old_connections()

        holder_thread = threading.Thread(target=holder)
        holder_thread.start()
        self.assertTrue(locked.wait(timeout=10))
        denied_thread = threading.Thread(target=denied_lookup)
        denied_thread.start()
        try:
            self.assertTrue(
                denied_done.wait(timeout=3),
                "foreign ownership lookup waited on a row it must not lock",
            )
        finally:
            release.set()
            holder_thread.join(timeout=30)
            denied_thread.join(timeout=30)

        self.assertFalse(holder_thread.is_alive())
        self.assertFalse(denied_thread.is_alive())
        self.assertEqual(outcomes.get_nowait(), "SubmissionNotFound")

    def test_edit_and_finalize_serialize_on_the_submission_row(self):
        submission = self.create_draft(name="Raced draft")
        barrier = threading.Barrier(2, timeout=60)
        outcomes = Queue()
        owner_id = self.owner.pk
        submission_id = submission.pk
        edited_input = self.raw_input(name="Raced edit", description="Raced edit")

        def edit_worker():
            close_old_connections()
            actor = get_user_model().objects.get(pk=owner_id)
            barrier.wait()
            try:
                edit_submission(actor, submission_id, "raced-edit", edited_input)
                outcomes.put(("edit", None))
            except Exception as error:
                outcomes.put(("edit", type(error).__name__))
            finally:
                close_old_connections()

        def finalize_worker():
            close_old_connections()
            actor = get_user_model().objects.get(pk=owner_id)
            barrier.wait()
            try:
                finalize_submission(actor, submission_id, "raced-finalize")
                outcomes.put(("finalize", None))
            except Exception as error:
                outcomes.put(("finalize", type(error).__name__))
            finally:
                close_old_connections()

        self.run_threads([edit_worker, finalize_worker])
        results = dict(outcomes.get_nowait() for _index in range(2))

        submission.refresh_from_db()
        self.assertIsNone(results["finalize"])
        self.assertEqual(submission.state, Request.State.PENDING)
        self.assertEqual(
            SubmissionLifecycleEvent.objects.filter(
                submission=submission, operation=SubmissionOperation.FINALIZE
            ).count(),
            1,
        )
        edit_events = SubmissionLifecycleEvent.objects.filter(
            submission=submission, operation=SubmissionOperation.EDIT
        ).count()
        if results["edit"] is None:
            # The edit committed first, so the pending snapshot is the edited one.
            self.assertEqual(edit_events, 1)
            self.assertEqual(submission.name, "Raced edit")
        else:
            self.assertEqual(results["edit"], "SubmissionStateError")
            self.assertEqual(edit_events, 0)
            self.assertEqual(submission.name, "Raced draft")

    def test_the_owner_second_draft_with_one_canonical_name_conflicts(self):
        first = self.create_draft(name="Owner Serialized Name")
        finalize_submission(self.owner, first.pk, "owner-first")
        # Created afterwards so that the first finalization had nothing to match.
        second = self.create_draft(name="owner  serialized  NAME")

        with self.assertRaises(DuplicateSubmission):
            finalize_submission(self.owner, second.pk, "owner-second")

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.state, Request.State.PENDING)
        self.assertEqual(second.state, Request.State.DRAFT)


class DraftEditMigrationTests(TransactionTestCase):
    migrate_from = [("backend", "0009_sealed_media_objects")]
    migrate_to = [("backend", "0010_submission_draft_edit")]

    def setUp(self):
        super().setUp()
        self.addCleanup(self._migrate_to_latest)
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps

        user_model = old_apps.get_model("backend", "CustomUser")
        category_model = old_apps.get_model("backend", "Category")
        address_model = old_apps.get_model("backend", "Address")
        request_model = old_apps.get_model("backend", "Request")

        self.owner_id = user_model.objects.create(
            email="edit-migration-owner@smokemap.test", password="!"
        ).pk
        category, _created = category_model.objects.get_or_create(
            slug="outdoors",
            defaults={"name": "Outdoors", "description": "Outside."},
        )
        address = address_model.objects.create(
            addressString="Edit migration address",
            location=Point(-77, 39, srid=4326),
        )
        self.submission_id = request_model.objects.create(
            name="Edit migration draft",
            category_id=category.pk,
            address_id=address.pk,
            owner_id=self.owner_id,
            state="draft",
            approved=False,
        ).pk

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.migrated_apps = executor.loader.project_state(self.migrate_to).apps

    def _migrate_to_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def _write_edit_evidence(self, apps, key="migrated-edit"):
        idempotency_model = apps.get_model("backend", "SubmissionIdempotency")
        lifecycle_model = apps.get_model("backend", "SubmissionLifecycleEvent")
        idempotency = idempotency_model.objects.create(
            actor_id=self.owner_id,
            operation="submission.edit.v3",
            key=key,
            request_hash="6" * 64,
            submission_id=self.submission_id,
            original_result={"state": "draft"},
        )
        return lifecycle_model.objects.create(
            submission_id=self.submission_id,
            actor_id=self.owner_id,
            operation="submission.edit.v3",
            from_state="draft",
            to_state="draft",
            outcome="succeeded",
            idempotency=idempotency,
        )

    def test_forward_accepts_edit_evidence_and_still_rejects_invalid_transitions(self):
        event = self._write_edit_evidence(self.migrated_apps)
        self.assertEqual(event.to_state, "draft")

        idempotency_model = self.migrated_apps.get_model(
            "backend", "SubmissionIdempotency"
        )
        lifecycle_model = self.migrated_apps.get_model(
            "backend", "SubmissionLifecycleEvent"
        )
        invalid = idempotency_model.objects.create(
            actor_id=self.owner_id,
            operation="submission.edit.v3",
            key="invalid-transition",
            request_hash="7" * 64,
            submission_id=self.submission_id,
            original_result={"state": "draft"},
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            lifecycle_model.objects.create(
                submission_id=self.submission_id,
                actor_id=self.owner_id,
                operation="submission.edit.v3",
                from_state="draft",
                to_state="pending",
                outcome="succeeded",
                idempotency=invalid,
            )

    def test_forward_preserves_the_pre_edit_operation_rules(self):
        lifecycle_model = self.migrated_apps.get_model(
            "backend", "SubmissionLifecycleEvent"
        )
        idempotency_model = self.migrated_apps.get_model(
            "backend", "SubmissionIdempotency"
        )
        record = idempotency_model.objects.create(
            actor_id=self.owner_id,
            operation="submission.finalize.v3",
            key="still-valid-finalize",
            request_hash="8" * 64,
            submission_id=self.submission_id,
            original_result={"state": "pending"},
        )
        event = lifecycle_model.objects.create(
            submission_id=self.submission_id,
            actor_id=self.owner_id,
            operation="submission.finalize.v3",
            from_state="draft",
            to_state="pending",
            outcome="succeeded",
            idempotency=record,
        )
        self.assertEqual(event.operation, "submission.finalize.v3")

        # A media operation still requires a media target after the widening.
        with self.assertRaises(IntegrityError), transaction.atomic():
            idempotency_model.objects.create(
                actor_id=self.owner_id,
                operation="media.intent.create.v3",
                key="media-without-target",
                request_hash="9" * 64,
                submission_id=self.submission_id,
                media_intent_id=None,
                original_result={},
            )

        # Reserved M4 transitions remain unreachable at the database level.
        reserved_idempotency_value = "reserved-m4-transition"
        reserved = idempotency_model.objects.create(
            actor_id=self.owner_id,
            operation="submission.approve.v4",
            key=reserved_idempotency_value,
            request_hash="b" * 64,
            submission_id=self.submission_id,
            original_result={"state": "approved"},
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            lifecycle_model.objects.create(
                submission_id=self.submission_id,
                actor_id=self.owner_id,
                operation="submission.approve.v4",
                from_state="draft",
                to_state="approved",
                outcome="succeeded",
                idempotency=reserved,
            )

    def test_reverse_refuses_while_edit_evidence_exists(self):
        self._write_edit_evidence(self.migrated_apps)
        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(RuntimeError, "draft edit evidence"):
            executor.migrate(self.migrate_from)

    def test_reverse_is_lossless_without_edit_evidence(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        reversed_apps = executor.loader.project_state(self.migrate_from).apps
        request_model = reversed_apps.get_model("backend", "Request")
        idempotency_model = reversed_apps.get_model("backend", "SubmissionIdempotency")

        self.assertEqual(
            request_model.objects.get(pk=self.submission_id).state, "draft"
        )
        self.assertNotIn(
            "submission.edit.v3",
            dict(idempotency_model._meta.get_field("operation").choices),
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            idempotency_model.objects.create(
                actor_id=self.owner_id,
                operation="submission.edit.v3",
                key="post-reverse-edit",
                request_hash="a" * 64,
                submission_id=self.submission_id,
                original_result={"state": "draft"},
            )


class SubmissionServiceErrorSurfaceTests(SubmissionFixtureMixin, TestCase):
    category_slug = "outdoors"

    def setUp(self):
        self.build_users()

    def test_service_errors_expose_stable_non_sensitive_codes(self):
        self.assertEqual(SubmissionNotFound("x").code, "NOT_FOUND")
        self.assertEqual(SubmissionStateError("x").code, "INVALID_SUBMISSION_STATE")
        self.assertEqual(DuplicateSubmission("x").code, "DUPLICATE_SUBMISSION")
        self.assertEqual(MediaNotReady("x").code, "MEDIA_NOT_READY")

    def test_invalid_idempotency_keys_are_rejected_before_any_lookup(self):
        submission = self.create_draft()
        for key in ("", "k" * 256, "nul\x00key"):
            with self.subTest(key=len(key)):
                with self.assertRaises(SubmissionInputError):
                    finalize_submission(self.owner, submission.pk, key)
                with self.assertRaises(SubmissionInputError):
                    edit_submission(self.owner, submission.pk, key, self.raw_input())
        submission.refresh_from_db()
        self.assertEqual(submission.state, Request.State.DRAFT)

    def test_edit_and_finalize_raise_not_found_for_unparsable_identifiers(self):
        for identifier in ("not-an-id", None, "0"):
            with self.subTest(identifier=identifier):
                with self.assertRaises(SubmissionNotFound):
                    finalize_submission(self.owner, identifier, "bad-id-finalize")
                with self.assertRaises(SubmissionNotFound):
                    edit_submission(
                        self.owner, identifier, "bad-id-edit", self.raw_input()
                    )

    def test_idempotency_conflict_is_raised_before_any_state_change(self):
        submission = self.create_draft()
        finalize_submission(self.owner, submission.pk, "conflict-key")
        other = self.create_draft(name="Conflict partner")
        with self.assertRaises(IdempotencyConflict):
            finalize_submission(self.owner, other.pk, "conflict-key")
        other.refresh_from_db()
        self.assertEqual(other.state, Request.State.DRAFT)
