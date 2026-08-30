from importlib import import_module

from django.contrib.gis.geos import Point
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, models, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase
from graphene.test import Client as GraphQLClient

from .models import Address, Category, Place, Request
from .schema import schema


EXPECTED_CATEGORIES = {
    "indoors": "Indoors",
    "outdoors": "Outdoors",
    "rooftop": "Rooftop",
    "underground": "Underground",
    "on-the-water": "On the water",
    "underwater": "Underwater",
    "in-the-air": "In the air",
    "other": "Other",
}


class CategoryTaxonomyTests(TestCase):
    def setUp(self):
        self.category = Category.objects.get(slug="outdoors")
        self.owner = get_user_model().objects.create_user(
            email="category-owner@smokemap.test",
            password="test",
        )

    def test_slugs_are_unique_and_issued_slugs_are_immutable(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Category.objects.create(
                    slug=self.category.slug,
                    name="Duplicate outdoors",
                )

        self.category.slug = "outside"
        with self.assertRaisesMessage(
            ValidationError,
            "An issued category slug is immutable.",
        ):
            self.category.save()
        self.category.refresh_from_db()
        self.assertEqual(self.category.slug, "outdoors")

    def test_display_name_and_description_remain_editable(self):
        self.category.name = "Open air"
        self.category.description = "An administrator-updated display description."

        self.category.full_clean()
        self.category.save()
        self.category.refresh_from_db()

        self.assertEqual(self.category.slug, "outdoors")
        self.assertEqual(self.category.name, "Open air")
        self.assertEqual(
            self.category.description,
            "An administrator-updated display description.",
        )

    def test_graphql_category_reference_data_exposes_exact_slugs(self):
        result = GraphQLClient(schema).execute(
            "query { categories { id slug name description } }"
        )

        self.assertNotIn("errors", result)
        self.assertEqual(
            {item["slug"]: item["name"] for item in result["data"]["categories"]},
            EXPECTED_CATEGORIES,
        )
        self.assertTrue(
            all(
                item["id"] and "description" in item
                for item in result["data"]["categories"]
            )
        )

    def test_place_rest_reference_preserves_id_and_adds_slug(self):
        address = Address(
            addressString="Category reference API address",
            location=Point(-77, 39, srid=4326),
        )
        address.save()
        place = Place.objects.create(
            name="Category reference API place",
            category=self.category,
            description="Approved place",
            address=address,
        )

        response = self.client.get(f"/places/{place.pk}/")

        self.assertEqual(response.status_code, 200)
        properties = response.json()["properties"]
        self.assertEqual(properties["category"], self.category.pk)
        self.assertEqual(properties["category_slug"], "outdoors")

    def test_place_and_request_keep_one_protected_category(self):
        address = Address(
            addressString="Protected category relation address",
            location=Point(-77, 39, srid=4326),
        )
        address.save()
        place = Place.objects.create(
            name="Protected category place",
            category=self.category,
            description="Approved place",
            address=address,
        )
        submission = Request.objects.create(
            name="Protected category request",
            category=self.category,
            description="Proposed place",
            address=address,
            owner=self.owner,
        )

        for model in (Place, Request):
            field = model._meta.get_field("category")
            self.assertFalse(field.null)
            self.assertIs(field.remote_field.on_delete, models.PROTECT)

        with self.assertRaises(ProtectedError):
            self.category.delete()
        self.assertEqual(Place.objects.get(pk=place.pk).category, self.category)
        self.assertEqual(Request.objects.get(pk=submission.pk).category, self.category)


class CategorySlugMigrationTests(TransactionTestCase):
    migrate_from = [("backend", "0003_refresh_token_lifecycle")]
    migrate_to = [("backend", "0004_category_slug")]

    def setUp(self):
        super().setUp()
        self.addCleanup(self._migrate_to_latest)
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps

        category_model = old_apps.get_model("backend", "Category")
        address_model = old_apps.get_model("backend", "Address")
        place_model = old_apps.get_model("backend", "Place")
        request_model = old_apps.get_model("backend", "Request")
        user_model = old_apps.get_model("backend", "CustomUser")
        owner = user_model.objects.create(
            email="pre-slug-owner@smokemap.test",
            password="!",
        )

        category_model.objects.all().delete()
        descriptions = dict(
            import_module("backend.migrations.0001_initial").PROVISIONAL_CATEGORIES
        )
        categories = {
            name: category_model.objects.create(
                name=name,
                description=descriptions[name],
            )
            for name in EXPECTED_CATEGORIES.values()
        }
        address = address_model.objects.create(
            addressString="Pre-slug migration address",
            location=Point(-77, 39, srid=4326),
        )
        self.category_id = categories["Outdoors"].pk
        self.place_id = place_model.objects.create(
            name="Pre-slug place",
            category_id=self.category_id,
            description="Existing place",
            address_id=address.pk,
        ).pk
        self.request_id = request_model.objects.create(
            name="Pre-slug request",
            category_id=self.category_id,
            description="Existing request",
            address_id=address.pk,
            owner_id=owner.pk,
        ).pk

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.migrated_apps = executor.loader.project_state(self.migrate_to).apps

    def _migrate_to_latest(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def _post_teardown(self):
        super()._post_teardown()
        descriptions = dict(
            import_module("backend.migrations.0001_initial").PROVISIONAL_CATEGORIES
        )
        for slug, name in EXPECTED_CATEGORIES.items():
            Category.objects.update_or_create(
                name=name,
                defaults={"slug": slug, "description": descriptions[name]},
            )

    def test_upgrade_backfills_exact_mappings_without_repointing_relations(self):
        category_model = self.migrated_apps.get_model("backend", "Category")
        place_model = self.migrated_apps.get_model("backend", "Place")
        request_model = self.migrated_apps.get_model("backend", "Request")

        self.assertEqual(
            dict(category_model.objects.values_list("slug", "name")),
            EXPECTED_CATEGORIES,
        )
        self.assertEqual(
            place_model.objects.get(pk=self.place_id).category_id,
            self.category_id,
        )
        self.assertEqual(
            request_model.objects.get(pk=self.request_id).category_id,
            self.category_id,
        )
        slug_field = category_model._meta.get_field("slug")
        self.assertFalse(slug_field.null)
        self.assertTrue(slug_field.unique)
