from queue import Queue
from threading import Barrier, Thread
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from graphene.test import Client as GraphQLClient

from .models import (
    Address,
    Category,
    Place,
    Request,
    RequestTag,
    SubmissionIdempotency,
    SubmissionLifecycleEvent,
    SubmissionOperation,
    Tag,
)
from .schema import schema
from .submissions import (
    SubmissionInputError,
    normalize_website,
    validate_submission_input,
)


CREATE_SUBMISSION = """
    mutation Create($input: SubmissionV3Input!, $key: String!) {
      createSubmissionV3(input: $input, idempotencyKey: $key) {
        submission {
          id
          name
          state
          description
          website
          requestedBy
          tags
        }
        replayed
      }
    }
"""


class WebsiteValidationTests(SimpleTestCase):
    def test_accepts_https_dns_and_canonicalizes_idna_and_default_port(self):
        self.assertEqual(
            normalize_website("  HTTPS://BÜCHER.de:443/path?q=yes  "),
            "https://xn--bcher-kva.de/path?q=yes",
        )
        self.assertEqual(normalize_website(""), None)

    def test_rejects_unsafe_or_ambiguous_urls_without_network_access(self):
        invalid = (
            "http://smokemap.org",
            "/relative",
            "https://localhost/path",
            "https://api.example.com/path",
            "https://127.0.0.1/path",
            "https://[::1]/path",
            "https://2130706433/path",
            "https://127.1/path",
            "https://0x7f.0x0.0x0.0x1/path",
            "https://user:pass@smokemap.org/path",
            "https://smokemap.org:444/path",
            "https://smokemap.org/path#fragment",
            "https://smokemap.org/%zz",
            "https://smokemap.org/%0a",
            "https://singlelabel",
            "https://-invalid.example.co",
            "https://smokemap.org/line\nbreak",
        )
        with patch("socket.getaddrinfo", side_effect=AssertionError("DNS called")), patch(
            "socket.socket", side_effect=AssertionError("network called")
        ):
            self.assertEqual(
                normalize_website("https://www.smokemap.org/a?b=c"),
                "https://www.smokemap.org/a?b=c",
            )
            for value in invalid:
                with self.subTest(value=value), self.assertRaises(SubmissionInputError):
                    normalize_website(value)


class SubmissionCreationTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            email="submission-owner@smokemap.test",
            password="test",
        )
        self.other_user = user_model.objects.create_user(
            email="other-submission-owner@smokemap.test",
            password="test",
        )
        self.inactive = user_model.objects.create_user(
            email="inactive-submission-owner@smokemap.test",
            password="test",
            is_active=False,
        )
        self.category = Category.objects.get(slug="outdoors")
        self.graphql = GraphQLClient(schema)

    def context(self, user):
        return SimpleNamespace(user=user, META={})

    def input(self, **overrides):
        values = {
            "name": "  Owner\tDraft  ",
            "categorySlug": "outdoors",
            "longitude": -77.0365,
            "latitude": 38.8977,
            "addressLabel": "  Human\tlabel  ",
            "tags": ["  Quiet\tPatio ", "Ｎｅｗ Proposal"],
            "description": "  Optional\n description ",
            "website": "HTTPS://BÜCHER.de:443/path?q=yes",
        }
        values.update(overrides)
        return values

    def create(self, key="submission-key", user=None, **overrides):
        return self.graphql.execute(
            CREATE_SUBMISSION,
            variable_values={"input": self.input(**overrides), "key": key},
            context_value=self.context(user or self.user),
        )

    def error_code(self, result):
        return result["errors"][0]["extensions"]["code"]

    def test_success_creates_one_consistent_owner_bound_draft_and_evidence(self):
        public_tag = Tag.objects.create(name="Quiet Patio", is_public=True)

        result = self.create()

        self.assertNotIn("errors", result)
        payload = result["data"]["createSubmissionV3"]
        submission = Request.objects.select_related("address", "owner").get(
            pk=payload["submission"]["id"]
        )
        self.assertFalse(payload["replayed"])
        self.assertEqual(payload["submission"]["state"], "draft")
        self.assertEqual(submission.owner, self.user)
        self.assertEqual(submission.state, Request.State.DRAFT)
        self.assertFalse(submission.approved)
        self.assertEqual(submission.name, "Owner Draft")
        self.assertEqual(submission.description, "Optional description")
        self.assertEqual(submission.address.addressString, "Human label")
        self.assertEqual(submission.address.location.srid, 4326)
        self.assertEqual(
            (submission.address.location.x, submission.address.location.y),
            (-77.0365, 38.8977),
        )
        self.assertEqual(
            submission.website,
            "https://xn--bcher-kva.de/path?q=yes",
        )
        links = list(
            RequestTag.objects.filter(request=submission)
            .select_related("tag")
            .order_by("position")
        )
        self.assertEqual([link.position for link in links], [0, 1])
        self.assertEqual([link.display for link in links], ["Quiet Patio", "New Proposal"])
        self.assertEqual(links[0].tag_id, public_tag.pk)
        self.assertTrue(links[0].tag.is_public)
        self.assertFalse(links[1].tag.is_public)

        idempotency = SubmissionIdempotency.objects.get(submission=submission)
        self.assertEqual(idempotency.actor, self.user)
        self.assertEqual(idempotency.operation, SubmissionOperation.CREATE)
        self.assertEqual(
            idempotency.original_result,
            {"state": "draft", "submission_id": submission.pk},
        )
        event = SubmissionLifecycleEvent.objects.get(submission=submission)
        self.assertEqual(event.actor, self.user)
        self.assertIsNone(event.from_state)
        self.assertEqual(event.to_state, Request.State.DRAFT)
        self.assertEqual(event.outcome, SubmissionLifecycleEvent.Outcome.SUCCEEDED)
        self.assertEqual(event.idempotency, idempotency)

    def test_guest_inactive_and_client_owned_identity_fields_fail_closed(self):
        guest = SimpleNamespace(is_authenticated=False, is_active=False)
        for account in (guest, self.inactive):
            with self.subTest(account=account):
                result = self.create(user=account)
                self.assertEqual(self.error_code(result), "UNAUTHENTICATED")
        spoofed = self.input()
        spoofed.update(
            {
                "owner": str(self.other_user.pk),
                "state": "pending",
                "requestedBy": "spoofed",
            }
        )
        result = self.graphql.execute(
            CREATE_SUBMISSION,
            variable_values={"input": spoofed, "key": "spoof"},
            context_value=self.context(self.user),
        )
        self.assertIn("errors", result)
        self.assertEqual(Request.objects.count(), 0)

    def test_name_and_optional_text_boundaries_and_empty_null_normalization(self):
        for index, name in enumerate(("ab", "x" * 100)):
            result = self.create(
                key=f"name-boundary-{index}",
                name=name,
                addressLabel="   ",
                description="\t",
                website=" ",
                tags=[],
            )
            self.assertNotIn("errors", result)
            submission = Request.objects.get(
                pk=result["data"]["createSubmissionV3"]["submission"]["id"]
            )
            self.assertIsNone(submission.address.addressString)
            self.assertIsNone(submission.description)
            self.assertIsNone(submission.website)

        for index, field, value in (
            (0, "name", "a"),
            (1, "name", "x" * 101),
            (2, "addressLabel", "x" * 256),
            (3, "description", "x" * 256),
            (4, "website", "https://smokemap.org/" + "x" * 240),
        ):
            with self.subTest(field=field):
                result = self.create(key=f"invalid-text-{index}", **{field: value})
                self.assertEqual(self.error_code(result), "INVALID_SUBMISSION")

    def test_category_slug_is_exact_and_numeric_ids_are_not_an_input(self):
        for index, slug in enumerate(("Outdoors", " outdoors", "outdoors ")):
            result = self.create(key=f"bad-category-{index}", categorySlug=slug)
            self.assertEqual(self.error_code(result), "INVALID_SUBMISSION")
            self.assertEqual(
                result["errors"][0]["extensions"]["field"],
                "category_slug",
            )
        result = self.graphql.execute(
            CREATE_SUBMISSION,
            variable_values={
                "key": "numeric-category",
                "input": self.input(categorySlug=self.category.pk),
            },
            context_value=self.context(self.user),
        )
        self.assertIn("errors", result)
        self.assertEqual(Request.objects.count(), 0)

    def test_coordinate_boundaries_and_invalid_values(self):
        for index, (longitude, latitude) in enumerate(
            ((-180, -90), (180, 90), (0, 0))
        ):
            result = self.create(
                key=f"coordinate-boundary-{index}",
                name=f"Boundary {index}",
                longitude=longitude,
                latitude=latitude,
                tags=[],
            )
            self.assertNotIn("errors", result)

        for index, overrides in enumerate(
            (
                {"longitude": -180.0001},
                {"longitude": 180.0001},
                {"latitude": -90.0001},
                {"latitude": 90.0001},
                {"longitude": "not-a-number"},
            )
        ):
            result = self.create(key=f"bad-coordinate-{index}", **overrides)
            self.assertIn("errors", result)

        raw = {
            "name": "Finite coordinate check",
            "category_slug": "outdoors",
            "longitude": -77,
            "latitude": 39,
            "tags": [],
        }
        for field, value in (
            ("longitude", float("nan")),
            ("longitude", float("inf")),
            ("latitude", float("-inf")),
        ):
            with self.subTest(field=field, value=value):
                invalid = dict(raw)
                invalid[field] = value
                with self.assertRaises(SubmissionInputError):
                    validate_submission_input(invalid)

    def test_idempotency_key_boundaries(self):
        maximum = self.create(key="k" * 255, name="Maximum key")
        self.assertNotIn("errors", maximum)
        for key in ("", "k" * 256, "nul\x00key"):
            with self.subTest(key_length=len(key)):
                result = self.create(key=key, name="Rejected key")
                self.assertEqual(self.error_code(result), "INVALID_SUBMISSION")

    def test_same_retry_replays_and_changed_payload_conflicts_without_writes(self):
        first = self.create(key="retry-key")
        retried = self.create(key="retry-key")
        changed = self.create(key="retry-key", name="Changed payload")

        self.assertNotIn("errors", first)
        self.assertNotIn("errors", retried)
        self.assertTrue(retried["data"]["createSubmissionV3"]["replayed"])
        self.assertEqual(
            first["data"]["createSubmissionV3"]["submission"]["id"],
            retried["data"]["createSubmissionV3"]["submission"]["id"],
        )
        self.assertEqual(self.error_code(changed), "IDEMPOTENCY_CONFLICT")
        self.assertEqual(Request.objects.count(), 1)
        self.assertEqual(Address.objects.count(), 1)
        self.assertEqual(SubmissionIdempotency.objects.count(), 1)
        self.assertEqual(SubmissionLifecycleEvent.objects.count(), 1)

    def test_same_key_is_independent_for_another_authenticated_actor(self):
        first = self.create(key="actor-scoped")
        second = self.create(key="actor-scoped", user=self.other_user)
        self.assertNotIn("errors", first)
        self.assertNotIn("errors", second)
        self.assertEqual(Request.objects.count(), 2)

    def test_injected_lifecycle_failure_rolls_back_every_related_row(self):
        baseline = {
            "addresses": Address.objects.count(),
            "requests": Request.objects.count(),
            "tags": Tag.objects.count(),
            "links": RequestTag.objects.count(),
        }
        with patch(
            "backend.submissions.SubmissionLifecycleEvent.objects.create",
            side_effect=RuntimeError("injected lifecycle failure"),
        ):
            result = self.create(key="rollback")
        self.assertEqual(self.error_code(result), "SUBMISSION_CREATE_FAILED")
        self.assertEqual(Address.objects.count(), baseline["addresses"])
        self.assertEqual(Request.objects.count(), baseline["requests"])
        self.assertEqual(Tag.objects.count(), baseline["tags"])
        self.assertEqual(RequestTag.objects.count(), baseline["links"])
        self.assertEqual(SubmissionIdempotency.objects.count(), 0)
        self.assertEqual(SubmissionLifecycleEvent.objects.count(), 0)

    def test_address_persistence_is_side_effect_free_and_labels_are_not_unique(self):
        with patch("socket.getaddrinfo", side_effect=AssertionError("DNS called")), patch(
            "socket.socket", side_effect=AssertionError("network called")
        ):
            Address.objects.create(addressString="Repeated", location=Point(1, 2, srid=4326))
            Address.objects.create(addressString="Repeated", location=Point(3, 4, srid=4326))
        self.assertEqual(Address.objects.filter(addressString="Repeated").count(), 2)

    def test_place_names_are_not_globally_unique(self):
        first_address = Address.objects.create(location=Point(1, 2, srid=4326))
        second_address = Address.objects.create(location=Point(3, 4, srid=4326))
        Place.objects.create(
            name="Repeated place",
            category=self.category,
            address=first_address,
        )
        Place.objects.create(
            name="Repeated place",
            category=self.category,
            address=second_address,
        )
        self.assertEqual(Place.objects.filter(name="Repeated place").count(), 2)

    def test_database_rejects_invalid_state_and_invalid_create_transition(self):
        created = self.create(key="constraints", tags=[])
        submission = Request.objects.get(
            pk=created["data"]["createSubmissionV3"]["submission"]["id"]
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Request.objects.filter(pk=submission.pk).update(state="unknown")
        with self.assertRaises(IntegrityError), transaction.atomic():
            SubmissionLifecycleEvent.objects.filter(submission=submission).update(
                to_state=Request.State.PENDING
            )

    def test_database_rejects_pending_to_expired_transition(self):
        created = self.create(key="pending-expiry-constraint", tags=[])
        submission = Request.objects.get(
            pk=created["data"]["createSubmissionV3"]["submission"]["id"]
        )
        idempotency = SubmissionIdempotency.objects.create(
            actor=self.user,
            operation=SubmissionOperation.EXPIRE,
            key="pending-expiry-event",
            request_hash="0" * 64,
            submission=submission,
            original_result={"state": Request.State.EXPIRED},
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            SubmissionLifecycleEvent.objects.create(
                submission=submission,
                actor=self.user,
                operation=SubmissionOperation.EXPIRE,
                from_state=Request.State.PENDING,
                to_state=Request.State.EXPIRED,
                outcome=SubmissionLifecycleEvent.Outcome.SUCCEEDED,
                idempotency=idempotency,
            )

    def test_repeated_submission_operation_uses_actor_scoped_idempotency(self):
        created = self.create(key="repeated-operation-setup", tags=[])
        submission = Request.objects.get(
            pk=created["data"]["createSubmissionV3"]["submission"]["id"]
        )

        for key in ("expiry-attempt-one", "expiry-attempt-two"):
            idempotency = SubmissionIdempotency.objects.create(
                actor=self.user,
                operation=SubmissionOperation.EXPIRE,
                key=key,
                request_hash="1" * 64,
                submission=submission,
                original_result={"state": Request.State.EXPIRED},
            )
            SubmissionLifecycleEvent.objects.create(
                submission=submission,
                actor=self.user,
                operation=SubmissionOperation.EXPIRE,
                from_state=Request.State.DRAFT,
                to_state=Request.State.EXPIRED,
                outcome=SubmissionLifecycleEvent.Outcome.SUCCEEDED,
                idempotency=idempotency,
            )

        self.assertEqual(
            SubmissionIdempotency.objects.filter(
                submission=submission,
                operation=SubmissionOperation.EXPIRE,
            ).count(),
            2,
        )
        self.assertEqual(
            SubmissionLifecycleEvent.objects.filter(
                submission=submission,
                operation=SubmissionOperation.EXPIRE,
            ).count(),
            2,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            SubmissionIdempotency.objects.create(
                actor=self.user,
                operation=SubmissionOperation.EXPIRE,
                key="expiry-attempt-one",
                request_hash="2" * 64,
                submission=submission,
                original_result={"state": Request.State.EXPIRED},
            )

    def test_legacy_submission_and_moderation_writes_are_fail_closed(self):
        legacy_create = """
            mutation {
              createRequest(input: {
                name: "Legacy", category: "1", description: "legacy",
                addressString: "[1,2]", tags: [], website: "https://smokemap.org"
              }) { request { id } }
            }
        """
        legacy_approve = """
            mutation { approveRequest(id: "1", input: {approvedComment: "x"}) {
              request { id }
            } }
        """
        for operation in (legacy_create, legacy_approve):
            result = self.graphql.execute(operation, context_value=self.context(self.user))
            self.assertEqual(self.error_code(result), "FORBIDDEN")
        self.assertEqual(Request.objects.count(), 0)


class ConcurrentSubmissionCreationTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="concurrent-submission-owner@smokemap.test",
            password="test",
        )

    def test_concurrent_same_key_creates_one_draft_event_and_result(self):
        barrier = Barrier(2)
        results = Queue()
        user_id = self.user.pk

        def worker():
            close_old_connections()
            user = get_user_model().objects.get(pk=user_id)
            barrier.wait()
            result = GraphQLClient(schema).execute(
                CREATE_SUBMISSION,
                variable_values={
                    "key": "concurrent-key",
                    "input": {
                        "name": "Concurrent draft",
                        "categorySlug": "outdoors",
                        "longitude": -77,
                        "latitude": 39,
                        "tags": [],
                        "website": "https://smokemap.org",
                    },
                },
                context_value=SimpleNamespace(user=user, META={}),
            )
            results.put(result)
            close_old_connections()

        threads = [Thread(target=worker) for _index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        responses = [results.get_nowait(), results.get_nowait()]
        self.assertTrue(all("errors" not in response for response in responses))
        ids = {
            response["data"]["createSubmissionV3"]["submission"]["id"]
            for response in responses
        }
        self.assertEqual(len(ids), 1)
        self.assertEqual(Request.objects.count(), 1)
        self.assertEqual(Address.objects.count(), 1)
        self.assertEqual(SubmissionIdempotency.objects.count(), 1)
        self.assertEqual(SubmissionLifecycleEvent.objects.count(), 1)
        self.assertEqual(
            sorted(
                response["data"]["createSubmissionV3"]["replayed"]
                for response in responses
            ),
            [False, True],
        )


class M3SubmissionMigrationTests(TransactionTestCase):
    migrate_from = [("backend", "0005_canonical_request_tags")]
    migrate_to = [("backend", "0006_m3_submission_creation")]

    def setUp(self):
        super().setUp()
        self.addCleanup(self._migrate_to_latest)
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps

        user_model = old_apps.get_model("backend", "CustomUser")
        category_model = old_apps.get_model("backend", "Category")
        address_model = old_apps.get_model("backend", "Address")
        place_model = old_apps.get_model("backend", "Place")
        request_model = old_apps.get_model("backend", "Request")

        owner = user_model.objects.create(
            email="m3-migration-owner@smokemap.test",
            password="!",
        )
        category, _created = category_model.objects.get_or_create(
            slug="outdoors",
            defaults={
                "name": "Outdoors",
                "description": "Outside in an open-air setting.",
            },
        )
        address = address_model.objects.create(
            addressString="Legacy shared label",
            location=Point(-77, 39, srid=4326),
        )
        self.category_id = category.pk
        self.address_id = address.pk
        self.request_id = request_model.objects.create(
            name="Legacy pending request",
            category_id=category.pk,
            description="Legacy pending description",
            address_id=address.pk,
            owner_id=owner.pk,
            approved=False,
        ).pk
        self.place_id = place_model.objects.create(
            name="Legacy duplicate-capable place",
            category_id=category.pk,
            address_id=address.pk,
        ).pk

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.migrated_apps = executor.loader.project_state(self.migrate_to).apps

    def _migrate_to_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_forward_backfills_pending_and_removes_global_text_uniqueness(self):
        request_model = self.migrated_apps.get_model("backend", "Request")
        address_model = self.migrated_apps.get_model("backend", "Address")
        place_model = self.migrated_apps.get_model("backend", "Place")

        self.assertEqual(request_model.objects.get(pk=self.request_id).state, "pending")
        self.assertFalse(address_model._meta.get_field("addressString").unique)
        self.assertFalse(place_model._meta.get_field("name").unique)

        duplicate_address = address_model.objects.create(
            addressString="Legacy shared label",
            location=Point(-76, 38, srid=4326),
        )
        place_model.objects.create(
            name="Legacy duplicate-capable place",
            category_id=self.category_id,
            address_id=duplicate_address.pk,
        )
        self.assertEqual(
            address_model.objects.filter(addressString="Legacy shared label").count(),
            2,
        )
        self.assertEqual(
            place_model.objects.filter(name="Legacy duplicate-capable place").count(),
            2,
        )

    def test_reverse_refuses_lossy_duplicate_schema_contraction(self):
        address_model = self.migrated_apps.get_model("backend", "Address")
        address_model.objects.create(
            addressString="Legacy shared label",
            location=Point(-76, 38, srid=4326),
        )
        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(RuntimeError, "duplicate address labels"):
            executor.migrate(self.migrate_from)

    def test_reverse_restores_the_legacy_shape_when_it_is_lossless(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        reversed_apps = executor.loader.project_state(self.migrate_from).apps
        request_model = reversed_apps.get_model("backend", "Request")
        address_model = reversed_apps.get_model("backend", "Address")
        place_model = reversed_apps.get_model("backend", "Place")

        legacy_request = request_model.objects.get(pk=self.request_id)
        self.assertFalse(legacy_request.approved)
        self.assertNotIn("state", {field.name for field in request_model._meta.fields})
        self.assertTrue(address_model._meta.get_field("addressString").unique)
        self.assertTrue(place_model._meta.get_field("name").unique)


class M3OwnerlessMigrationFailureTests(TransactionTestCase):
    migrate_from = [("backend", "0005_canonical_request_tags")]
    migrate_to = [("backend", "0006_m3_submission_creation")]

    def test_forward_refuses_to_invent_owner_identity(self):
        self.addCleanup(self._migrate_to_latest)
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        category_model = old_apps.get_model("backend", "Category")
        address_model = old_apps.get_model("backend", "Address")
        request_model = old_apps.get_model("backend", "Request")
        category, category_created = category_model.objects.get_or_create(
            slug="outdoors",
            defaults={
                "name": "Outdoors",
                "description": "Outside in an open-air setting.",
            },
        )
        address_id = None
        request_id = None

        try:
            address = address_model.objects.create(
                addressString="Ownerless migration address",
                location=Point(-77, 39, srid=4326),
            )
            address_id = address.pk
            request_id = request_model.objects.create(
                name="Ownerless legacy request",
                category_id=category.pk,
                description="Ambiguous owner",
                address_id=address.pk,
                owner_id=None,
            ).pk
            executor = MigrationExecutor(connection)
            with self.assertRaisesMessage(RuntimeError, "ownerless legacy submissions"):
                executor.migrate(self.migrate_to)
        finally:
            # The migration is atomic on PostgreSQL, so the old historical model is
            # still authoritative. Remove only this fixture before restoring latest.
            executor = MigrationExecutor(connection)
            executor.migrate(self.migrate_from)
            cleanup_apps = executor.loader.project_state(self.migrate_from).apps
            cleanup_apps.get_model("backend", "Request").objects.filter(
                pk=request_id
            ).delete()
            cleanup_apps.get_model("backend", "Address").objects.filter(
                pk=address_id
            ).delete()
            if category_created:
                cleanup_apps.get_model("backend", "Category").objects.filter(
                    pk=category.pk
                ).delete()

    def _migrate_to_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
