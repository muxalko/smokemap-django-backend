from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from graphene.test import Client as GraphQLClient

from .models import (
    Address,
    Category,
    Location,
    ModerationAudit,
    Place,
    Request,
    RequestTag,
    Tag,
)
from .schema import schema
from .tagging import normalize_submission_tags, normalize_tag_text


CREATE_REQUEST = """
    mutation Create($input: RequestInput!) {
      createRequest(input: $input) {
        request { id tags }
      }
    }
"""

APPROVE_REQUEST = """
    mutation Approve($id: ID!) {
      approveRequest(id: $id, input: {approvedComment: "Reviewed"}) {
        request { id approved tags }
      }
    }
"""


def ensure_historical_outdoors_category(apps):
    category_model = apps.get_model("backend", "Category")
    category, _created = category_model.objects.update_or_create(
        name="Outdoors",
        defaults={
            "slug": "outdoors",
            "description": "Outside in an open-air setting.",
        },
    )
    return category


class TagNormalizationTests(SimpleTestCase):
    def test_nfkc_whitespace_and_casefold_normalization(self):
        normalized = normalize_tag_text("  Ｃａｆｅ\u0301\t  Lounge  ")

        self.assertEqual(normalized.display, "Café Lounge")
        self.assertEqual(normalized.canonical, "café lounge")
        self.assertEqual(normalize_tag_text("Straße").canonical, "strasse")

    def test_optional_list_and_all_validation_boundaries(self):
        self.assertEqual(normalize_submission_tags(None), [])
        self.assertEqual(
            [tag.display for tag in normalize_submission_tags(["abc", "x" * 50])],
            ["abc", "x" * 50],
        )

        invalid_values = (
            [None],
            [""],
            ["  \t "],
            ["ab"],
            ["x" * 51],
            [str(index).zfill(3) for index in range(11)],
        )
        for values in invalid_values:
            with self.subTest(values=values):
                with self.assertRaises(ValidationError):
                    normalize_submission_tags(values)

    def test_casefolded_duplicates_are_rejected(self):
        with self.assertRaisesMessage(ValidationError, "duplicate tags"):
            normalize_submission_tags(["Straße", "STRASSE"])


class TagSubmissionTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            email="tag-owner@smokemap.test",
            password="test",
        )
        self.other_user = user_model.objects.create_user(
            email="other-tag-owner@smokemap.test",
            password="test",
        )
        self.moderator = user_model.objects.create_user(
            email="tag-moderator@smokemap.test",
            password="test",
            is_staff=True,
        )
        self.category = Category.objects.get(slug="outdoors")
        self.graphql = GraphQLClient(schema)

    def context(self, user=None):
        return SimpleNamespace(user=user or self.user, META={})

    def create_submission(self, name, tags, user=None):
        return self.graphql.execute(
            CREATE_REQUEST,
            variable_values={
                "input": {
                    "name": name,
                    "category": str(self.category.pk),
                    "description": "Tag proposal test",
                    "addressString": f"[{len(name)},{len(name) + 1}]",
                    "tags": tags,
                    "website": "https://example.test",
                }
            },
            context_value=self.context(user),
        )

    def test_existing_public_tag_is_reused_and_new_proposal_stays_private(self):
        existing = Tag.objects.create(name="Quiet Patio", is_public=True)

        result = self.create_submission(
            "Tag reuse submission",
            ["  quiet\t patio ", "Ｎｅｗ\u00a0Proposal"],
        )

        self.assertNotIn("errors", result)
        request_id = result["data"]["createRequest"]["request"]["id"]
        self.assertEqual(
            result["data"]["createRequest"]["request"]["tags"],
            ["quiet patio", "New Proposal"],
        )
        links = list(
            RequestTag.objects.filter(request_id=request_id)
            .select_related("tag")
            .order_by("position")
        )
        self.assertEqual([link.position for link in links], [0, 1])
        self.assertEqual(links[0].tag_id, existing.pk)
        self.assertTrue(links[0].tag.is_public)
        self.assertFalse(links[1].tag.is_public)
        self.assertEqual(
            [link.display for link in links],
            ["quiet patio", "New Proposal"],
        )

        global_tags = self.graphql.execute("query { tags { id name } }")
        self.assertEqual(
            global_tags["data"]["tags"],
            [{"id": str(existing.pk), "name": "Quiet Patio"}],
        )
        self.assertEqual(
            set(schema.graphql_schema.get_type("TagType").fields),
            {"id", "name"},
        )

    def test_shared_private_tag_keeps_each_owners_display_snapshot_private(self):
        owner_a = self.create_submission(
            "Owner A private spelling",
            ["Quiet Patio"],
            self.user,
        )
        owner_b = self.create_submission(
            "Owner B different private spelling",
            ["QUIET PATIO"],
            self.other_user,
        )

        self.assertNotIn("errors", owner_a)
        self.assertNotIn("errors", owner_b)
        request_a_id = owner_a["data"]["createRequest"]["request"]["id"]
        request_b_id = owner_b["data"]["createRequest"]["request"]["id"]
        self.assertEqual(
            owner_a["data"]["createRequest"]["request"]["tags"],
            ["Quiet Patio"],
        )
        self.assertEqual(
            owner_b["data"]["createRequest"]["request"]["tags"],
            ["QUIET PATIO"],
        )
        self.assertEqual(Tag.objects.filter(canonical="quiet patio").count(), 1)
        self.assertEqual(
            list(
                RequestTag.objects.filter(
                    request_id__in=(request_a_id, request_b_id)
                )
                .order_by("request_id")
                .values_list("display", flat=True)
            ),
            ["Quiet Patio", "QUIET PATIO"],
        )

        for request_id, owner, expected in (
            (request_a_id, self.user, ["Quiet Patio"]),
            (request_b_id, self.other_user, ["QUIET PATIO"]),
        ):
            with self.subTest(request_id=request_id):
                result = self.graphql.execute(
                    "query Detail($id: ID!) { requestById(id: $id) { tags } }",
                    variable_values={"id": request_id},
                    context_value=self.context(owner),
                )
                self.assertEqual(result["data"]["requestById"]["tags"], expected)

        global_tags = self.graphql.execute("query { tags { id name } }")
        self.assertEqual(global_tags["data"]["tags"], [])

    def test_approval_uses_approved_snapshot_without_rewriting_other_history(self):
        owner_a = self.create_submission(
            "First private tag proposal",
            ["Quiet Patio"],
            self.user,
        )
        owner_b = self.create_submission(
            "Second private tag proposal with case",
            ["QUIET PATIO"],
            self.other_user,
        )
        request_a_id = owner_a["data"]["createRequest"]["request"]["id"]
        request_b_id = owner_b["data"]["createRequest"]["request"]["id"]

        approved_b = self.graphql.execute(
            APPROVE_REQUEST,
            variable_values={"id": request_b_id},
            context_value=self.context(self.moderator),
        )

        self.assertNotIn("errors", approved_b)
        self.assertEqual(
            approved_b["data"]["approveRequest"]["request"]["tags"],
            ["QUIET PATIO"],
        )
        shared_tag = Tag.objects.get(canonical="quiet patio")
        self.assertTrue(shared_tag.is_public)
        self.assertEqual(shared_tag.name, "QUIET PATIO")
        self.assertEqual(
            list(
                Place.objects.get(name="Second private tag proposal with case")
                .tags.values_list("name", flat=True)
            ),
            ["QUIET PATIO"],
        )
        global_tags = self.graphql.execute("query { tags { id name } }")
        self.assertEqual(
            global_tags["data"]["tags"],
            [{"id": str(shared_tag.pk), "name": "QUIET PATIO"}],
        )

        owner_a_history = self.graphql.execute(
            "query Detail($id: ID!) { requestById(id: $id) { tags } }",
            variable_values={"id": request_a_id},
            context_value=self.context(self.user),
        )
        self.assertEqual(
            owner_a_history["data"]["requestById"]["tags"],
            ["Quiet Patio"],
        )

        later = self.create_submission(
            "Later public vocabulary reuse",
            ["quiet PATIO"],
            self.user,
        )
        later_id = later["data"]["createRequest"]["request"]["id"]
        self.assertEqual(
            later["data"]["createRequest"]["request"]["tags"],
            ["quiet PATIO"],
        )
        approved_later = self.graphql.execute(
            APPROVE_REQUEST,
            variable_values={"id": later_id},
            context_value=self.context(self.moderator),
        )
        self.assertNotIn("errors", approved_later)
        self.assertEqual(
            approved_later["data"]["approveRequest"]["request"]["tags"],
            ["quiet PATIO"],
        )
        shared_tag.refresh_from_db()
        self.assertEqual(shared_tag.name, "QUIET PATIO")
        self.assertEqual(
            list(
                Place.objects.get(name="Later public vocabulary reuse")
                .tags.values_list("name", flat=True)
            ),
            ["QUIET PATIO"],
        )
        self.assertEqual(
            Location.objects.get(
                place_id=str(Place.objects.get(name="Later public vocabulary reuse").pk)
            ).tags,
            "QUIET PATIO",
        )

    def test_invalid_tags_leave_no_submission_address_or_tag_rows(self):
        baseline = (Request.objects.count(), Address.objects.count(), Tag.objects.count())

        result = self.create_submission("Invalid tag submission", ["okay", "OKAY"])

        self.assertIn("errors", result)
        self.assertEqual(
            (Request.objects.count(), Address.objects.count(), Tag.objects.count()),
            baseline,
        )

    def test_tag_link_failure_rolls_back_the_complete_submission_create(self):
        baseline = (Request.objects.count(), Address.objects.count(), Tag.objects.count())

        with patch(
            "backend.models.RequestTag.objects.bulk_create",
            side_effect=RuntimeError("injected request-tag failure"),
        ):
            result = self.create_submission(
                "Atomic tag submission",
                ["Atomic proposal"],
            )

        self.assertIn("errors", result)
        self.assertEqual(
            (Request.objects.count(), Address.objects.count(), Tag.objects.count()),
            baseline,
        )

    def test_request_list_and_detail_return_ordered_strings_without_n_plus_one(self):
        created = self.create_submission(
            "Ordered tag submission",
            ["Third display", "First display", "Second display"],
        )
        request_id = created["data"]["createRequest"]["request"]["id"]

        with self.assertNumQueries(2):
            listed = self.graphql.execute(
                "query { requests { id tags } }",
                context_value=self.context(),
            )
        with self.assertNumQueries(2):
            detailed = self.graphql.execute(
                "query Detail($id: ID!) { requestById(id: $id) { id tags } }",
                variable_values={"id": request_id},
                context_value=self.context(),
            )

        expected = ["Third display", "First display", "Second display"]
        self.assertEqual(listed["data"]["requests"][0]["tags"], expected)
        self.assertEqual(detailed["data"]["requestById"]["tags"], expected)

    def test_approval_promotes_tags_and_materializes_the_same_rows(self):
        created = self.create_submission(
            "Approved tag submission",
            ["First proposal", "Second proposal"],
        )
        request_id = created["data"]["createRequest"]["request"]["id"]
        tag_ids = list(
            RequestTag.objects.filter(request_id=request_id)
            .order_by("position")
            .values_list("tag_id", flat=True)
        )

        approved = self.graphql.execute(
            APPROVE_REQUEST,
            variable_values={"id": request_id},
            context_value=self.context(self.moderator),
        )

        self.assertNotIn("errors", approved)
        submission = Request.objects.get(pk=request_id)
        place = Place.objects.get(name=submission.name)
        self.assertTrue(submission.approved)
        self.assertEqual(
            set(place.tags.values_list("pk", flat=True)),
            set(tag_ids),
        )
        self.assertFalse(Tag.objects.filter(pk__in=tag_ids, is_public=False).exists())
        self.assertEqual(
            Location.objects.get(place_id=str(place.pk)).tags,
            "First proposal,Second proposal",
        )

    def test_approval_failure_rolls_back_place_promotion_and_audit(self):
        created = self.create_submission(
            "Rollback tag submission",
            ["Private proposal"],
        )
        request_id = created["data"]["createRequest"]["request"]["id"]
        tag = Tag.objects.get(request_tags__request_id=request_id)

        with patch(
            "backend.schema.ModerationAudit.objects.create",
            side_effect=RuntimeError("injected approval failure"),
        ):
            result = self.graphql.execute(
                APPROVE_REQUEST,
                variable_values={"id": request_id},
                context_value=self.context(self.moderator),
            )

        self.assertIn("errors", result)
        self.assertFalse(Request.objects.get(pk=request_id).approved)
        tag.refresh_from_db()
        self.assertFalse(tag.is_public)
        self.assertFalse(Place.objects.filter(name="Rollback tag submission").exists())
        self.assertFalse(Location.objects.filter(name="Rollback tag submission").exists())
        self.assertFalse(
            ModerationAudit.objects.filter(target_id=request_id).exists()
        )


class TagDatabaseConstraintTests(TestCase):
    def setUp(self):
        self.category = Category.objects.get(slug="outdoors")
        self.address = Address(
            addressString="Tag constraint address",
            location=Point(-77, 39, srid=4326),
        )
        self.address.save(omit_geocode=True)
        self.request = Request.objects.create(
            name="Tag constraint request",
            category=self.category,
            description="Constraint test",
            address=self.address,
        )
        self.first = Tag.objects.create(name="First tag")
        self.second = Tag.objects.create(name="Second tag")
        RequestTag.objects.create(
            request=self.request,
            tag=self.first,
            display="First tag",
            position=0,
        )

    def assert_integrity_error(self, **values):
        values.setdefault("display", "Second tag")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RequestTag.objects.create(request=self.request, **values)

    def test_canonical_and_ordering_constraints_are_database_enforced(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Tag.objects.create(name="FIRST TAG")

        self.assert_integrity_error(tag=self.first, position=1)
        self.assert_integrity_error(tag=self.second, position=0)
        self.assert_integrity_error(tag=self.second, position=10)

    def test_request_tag_display_is_required_and_limited_to_50_characters(self):
        display_field = RequestTag._meta.get_field("display")
        self.assertFalse(display_field.null)
        self.assertEqual(display_field.max_length, 50)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RequestTag.objects.create(
                    request=self.request,
                    tag=self.second,
                    position=1,
                )

        invalid = RequestTag(
            request=self.request,
            tag=self.second,
            display="x" * 51,
            position=1,
        )
        with self.assertRaises(ValidationError):
            invalid.full_clean()


class CanonicalTagMigrationTests(TransactionTestCase):
    migrate_from = [("backend", "0004_category_slug")]
    migrate_to = [("backend", "0005_canonical_request_tags")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        self.addCleanup(self._migrate_to_latest)
        old_apps = executor.loader.project_state(self.migrate_from).apps

        address_model = old_apps.get_model("backend", "Address")
        place_model = old_apps.get_model("backend", "Place")
        request_model = old_apps.get_model("backend", "Request")
        tag_model = old_apps.get_model("backend", "Tag")

        category = ensure_historical_outdoors_category(old_apps)
        address = address_model.objects.create(
            addressString="Legacy tag migration address",
            location=Point(-77, 39, srid=4326),
        )
        existing_tag = tag_model.objects.create(name="  Patio\tSmoke ")
        place = place_model.objects.create(
            name="Legacy tagged place",
            category_id=category.pk,
            description="Existing approved place",
            address_id=address.pk,
        )
        place.tags.add(existing_tag)
        request = request_model.objects.create(
            name="Legacy tagged request",
            category_id=category.pk,
            description="Existing proposal",
            address_id=address.pk,
            tags=["ＰＡＴＩＯ SMOKE", "  New\tTag "],
        )
        other_request = request_model.objects.create(
            name="Legacy request with shared private canonical",
            category_id=category.pk,
            description="Same canonical with its own display spelling",
            address_id=address.pk,
            tags=["NEW TAG"],
        )
        self.existing_tag_id = existing_tag.pk
        self.place_id = place.pk
        self.request_id = request.pk
        self.other_request_id = other_request.pk

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.migrated_apps = executor.loader.project_state(self.migrate_to).apps

    def _migrate_to_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_upgrade_preserves_place_vocabulary_and_ordered_request_tags(self):
        tag_model = self.migrated_apps.get_model("backend", "Tag")
        place_model = self.migrated_apps.get_model("backend", "Place")
        request_tag_model = self.migrated_apps.get_model("backend", "RequestTag")

        existing = tag_model.objects.get(pk=self.existing_tag_id)
        self.assertEqual((existing.name, existing.canonical), ("Patio Smoke", "patio smoke"))
        self.assertTrue(existing.is_public)
        self.assertTrue(
            place_model.objects.get(pk=self.place_id).tags.filter(pk=existing.pk).exists()
        )

        links = list(
            request_tag_model.objects.filter(request_id=self.request_id)
            .select_related("tag")
            .order_by("position")
        )
        self.assertEqual([link.position for link in links], [0, 1])
        self.assertEqual([link.tag.name for link in links], ["Patio Smoke", "New Tag"])
        self.assertEqual([link.display for link in links], ["PATIO SMOKE", "New Tag"])
        self.assertEqual(links[0].tag_id, existing.pk)
        self.assertFalse(links[1].tag.is_public)

        other_link = request_tag_model.objects.select_related("tag").get(
            request_id=self.other_request_id
        )
        self.assertEqual(other_link.tag_id, links[1].tag_id)
        self.assertEqual(other_link.display, "NEW TAG")
        self.assertEqual(
            tag_model.objects.filter(canonical="new tag").count(),
            1,
        )

    def test_reverse_migration_restores_each_requests_own_display_snapshot(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        rolled_back_apps = executor.loader.project_state(self.migrate_from).apps
        request_model = rolled_back_apps.get_model("backend", "Request")

        self.assertEqual(
            request_model.objects.get(pk=self.request_id).tags,
            ["PATIO SMOKE", "New Tag"],
        )
        self.assertEqual(
            request_model.objects.get(pk=self.other_request_id).tags,
            ["NEW TAG"],
        )


class CanonicalTagCollisionMigrationTests(TransactionTestCase):
    migrate_from = [("backend", "0004_category_slug")]
    migrate_to = [("backend", "0005_canonical_request_tags")]

    def setUp(self):
        super().setUp()
        self.conflicting_tag_id = None
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        self.addCleanup(self._repair_and_migrate_to_latest)
        self.old_apps = executor.loader.project_state(self.migrate_from).apps
        tag_model = self.old_apps.get_model("backend", "Tag")
        tag_model.objects.create(name="Straße")
        self.conflicting_tag_id = tag_model.objects.create(name="STRASSE").pk

    def _repair_and_migrate_to_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        if self.conflicting_tag_id is not None:
            old_apps.get_model("backend", "Tag").objects.filter(
                pk=self.conflicting_tag_id
            ).delete()
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_upgrade_fails_closed_on_existing_canonical_collisions(self):
        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(RuntimeError, "canonical tag collision"):
            executor.migrate(self.migrate_to)

        self.assertEqual(
            self.old_apps.get_model("backend", "Tag").objects.count(),
            2,
        )


class InvalidLegacyRequestTagMigrationTests(TransactionTestCase):
    migrate_from = [("backend", "0004_category_slug")]
    migrate_to = [("backend", "0005_canonical_request_tags")]

    def setUp(self):
        super().setUp()
        self.request_id = None
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        self.addCleanup(self._repair_and_migrate_to_latest)
        self.old_apps = executor.loader.project_state(self.migrate_from).apps
        category = ensure_historical_outdoors_category(self.old_apps)
        address = self.old_apps.get_model("backend", "Address").objects.create(
            addressString="Invalid legacy request tag address",
            location=Point(-77, 39, srid=4326),
        )
        self.request_id = self.old_apps.get_model("backend", "Request").objects.create(
            name="Invalid legacy request tag",
            category_id=category.pk,
            description="Migration must reject null elements.",
            address_id=address.pk,
            tags=[None],
        ).pk

    def _repair_and_migrate_to_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        if self.request_id is not None:
            old_apps.get_model("backend", "Request").objects.filter(
                pk=self.request_id
            ).delete()
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_upgrade_fails_closed_on_invalid_request_array_values(self):
        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(RuntimeError, "tag values must be strings"):
            executor.migrate(self.migrate_to)

        self.assertTrue(
            self.old_apps.get_model("backend", "Request").objects.filter(
                pk=self.request_id
            ).exists()
        )


class DuplicateLegacyRequestTagMigrationTests(TransactionTestCase):
    migrate_from = [("backend", "0004_category_slug")]
    migrate_to = [("backend", "0005_canonical_request_tags")]

    def setUp(self):
        super().setUp()
        self.request_id = None
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        self.addCleanup(self._repair_and_migrate_to_latest)
        self.old_apps = executor.loader.project_state(self.migrate_from).apps
        category = ensure_historical_outdoors_category(self.old_apps)
        address = self.old_apps.get_model("backend", "Address").objects.create(
            addressString="Duplicate legacy request tag address",
            location=Point(-77, 39, srid=4326),
        )
        self.request_id = self.old_apps.get_model("backend", "Request").objects.create(
            name="Duplicate legacy request tag",
            category_id=category.pk,
            description="Migration must reject canonical duplicates.",
            address_id=address.pk,
            tags=["New Tag", "NEW TAG"],
        ).pk

    def _repair_and_migrate_to_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        if self.request_id is not None:
            old_apps.get_model("backend", "Request").objects.filter(
                pk=self.request_id
            ).update(tags=["New Tag", "Other Tag"])
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_upgrade_fails_closed_on_duplicate_canonical_request_tags(self):
        executor = MigrationExecutor(connection)
        message = (
            f"Cannot migrate Request {self.request_id}: duplicate canonical tag "
            "'new tag' in the legacy tag array."
        )
        with self.assertRaises(RuntimeError) as raised:
            executor.migrate(self.migrate_to)

        self.assertEqual(str(raised.exception), message)
        self.assertNotIn("backend_requesttag", connection.introspection.table_names())
        request = self.old_apps.get_model("backend", "Request").objects.get(
            pk=self.request_id
        )
        self.assertEqual(request.tags, ["New Tag", "NEW TAG"])


class LegacyTagValidationMigrationTests(SimpleTestCase):
    def test_invalid_legacy_values_fail_closed(self):
        migration = import_module("backend.migrations.0005_canonical_request_tags")
        for value in (None, "", "ab", "x" * 51):
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError):
                    migration.normalize_legacy_tag(value, "test fixture")
