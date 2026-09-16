import json
from datetime import timedelta
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from graphene.test import Client as GraphQLClient

from .models import Address, Category, Request
from .schema import schema


QUEUE_QUERY = """
    query Queue($first: Int, $after: String) {
      moderationQueueV4(first: $first, after: $after) {
        items { id name state tags }
        hasNextPage
        nextCursor
      }
    }
"""


class ModerationQueueGraphQLTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.owner = user_model.objects.create_user(
            email="queue-owner@smokemap.test", password="test"
        )
        self.user = user_model.objects.create_user(
            email="queue-user@smokemap.test", password="test"
        )
        self.moderator = user_model.objects.create_user(
            email="queue-moderator@smokemap.test",
            password="test",
            is_staff=True,
        )
        self.administrator = user_model.objects.create_user(
            email="queue-administrator@smokemap.test",
            password="test",
            is_superuser=True,
        )
        self.category = Category.objects.get(slug="outdoors")
        self.address = Address.objects.create(
            addressString="Queue fixture",
            location=Point(-77.0365, 38.8977, srid=4326),
        )
        self.graphql = GraphQLClient(schema)
        self.guest = SimpleNamespace(is_authenticated=False, is_active=False)

    @staticmethod
    def context(user):
        return SimpleNamespace(user=user, META={})

    @staticmethod
    def error_code(result):
        return result["errors"][0]["extensions"]["code"]

    def create_submissions(self, count, *, state=Request.State.PENDING):
        rows = [
            Request.objects.create(
                name=f"Queue submission {index:03d}",
                category=self.category,
                address=self.address,
                owner=self.owner,
                state=state,
            )
            for index in range(count)
        ]
        base = timezone.now() - timedelta(days=1)
        for index, row in enumerate(rows):
            Request.objects.filter(pk=row.pk).update(
                date_created=base + timedelta(seconds=index)
            )
            row.date_created = base + timedelta(seconds=index)
        return rows

    def queue(self, user, *, first=None, after=None):
        variables = {"first": first, "after": after}
        return self.graphql.execute(
            QUEUE_QUERY,
            variable_values=variables,
            context_value=self.context(user),
        )

    def test_queue_requires_a_moderator_or_administrator(self):
        for actor, expected in (
            (self.guest, "UNAUTHENTICATED"),
            (self.user, "FORBIDDEN"),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(self.error_code(self.queue(actor)), expected)

        self.create_submissions(1)
        for actor in (self.moderator, self.administrator):
            with self.subTest(actor=actor.email):
                result = self.queue(actor)
                self.assertNotIn("errors", result)
                self.assertEqual(len(result["data"]["moderationQueueV4"]["items"]), 1)

    def test_default_maximum_and_legacy_queries_are_bounded(self):
        self.create_submissions(55)

        default_page = self.queue(self.moderator)["data"]["moderationQueueV4"]
        self.assertEqual(len(default_page["items"]), 20)
        self.assertTrue(default_page["hasNextPage"])
        self.assertTrue(default_page["nextCursor"])

        with CaptureQueriesContext(connection) as captured:
            maximum_page = self.queue(self.moderator, first=50)
        page = maximum_page["data"]["moderationQueueV4"]
        self.assertEqual(len(page["items"]), 50)
        self.assertTrue(page["hasNextPage"])
        request_queries = [
            query["sql"]
            for query in captured.captured_queries
            if 'FROM "backend_request"' in query["sql"]
        ]
        self.assertEqual(len(request_queries), 1)
        self.assertIn("LIMIT 51", request_queries[0])
        self.assertNotIn("COUNT(", request_queries[0].upper())

        legacy = self.graphql.execute(
            "query { requestsToApprove { id } }",
            context_value=self.context(self.moderator),
        )
        self.assertEqual(len(legacy["data"]["requestsToApprove"]), 50)

    def test_exact_boundary_and_timestamp_ties_have_a_stable_total_order(self):
        rows = self.create_submissions(51)
        tied_at = timezone.now() - timedelta(hours=2)
        Request.objects.filter(pk__in=[row.pk for row in rows]).update(
            date_created=tied_at
        )

        first = self.queue(self.moderator, first=50)["data"]["moderationQueueV4"]
        self.assertEqual(
            [item["id"] for item in first["items"]],
            [str(row.pk) for row in rows[:50]],
        )
        self.assertTrue(first["hasNextPage"])

        second = self.queue(
            self.moderator, first=50, after=first["nextCursor"]
        )["data"]["moderationQueueV4"]
        self.assertEqual(
            [item["id"] for item in second["items"]], [str(rows[50].pk)]
        )
        self.assertFalse(second["hasNextPage"])
        self.assertIsNone(second["nextCursor"])

        Request.objects.filter(pk=rows[50].pk).update(state=Request.State.REJECTED)
        exact = self.queue(self.moderator, first=50)["data"]["moderationQueueV4"]
        self.assertEqual(len(exact["items"]), 50)
        self.assertFalse(exact["hasNextPage"])
        self.assertIsNone(exact["nextCursor"])

    def test_bad_limits_and_malformed_or_tampered_cursors_fail_predictably(self):
        self.create_submissions(2)
        for first in (-1, 0, 51):
            with self.subTest(first=first):
                self.assertEqual(
                    self.error_code(self.queue(self.moderator, first=first)),
                    "INVALID_PAGINATION",
                )

        valid = self.queue(self.moderator, first=1)["data"]["moderationQueueV4"][
            "nextCursor"
        ]
        for cursor in ("not-a-cursor", valid[:-1] + ("a" if valid[-1] != "a" else "b")):
            with self.subTest(cursor=cursor):
                result = self.queue(self.moderator, first=1, after=cursor)
                self.assertEqual(self.error_code(result), "INVALID_PAGINATION")
                self.assertNotIn(cursor, result["errors"][0]["message"])

    def test_each_page_rechecks_pending_state(self):
        rows = self.create_submissions(4)
        draft = self.create_submissions(1, state=Request.State.DRAFT)[0]
        first = self.queue(self.moderator, first=2)["data"]["moderationQueueV4"]
        Request.objects.filter(pk=rows[2].pk).update(state=Request.State.REJECTED)

        second = self.queue(
            self.moderator, first=10, after=first["nextCursor"]
        )["data"]["moderationQueueV4"]
        self.assertEqual([item["id"] for item in second["items"]], [str(rows[3].pk)])
        all_ids = {item["id"] for item in first["items"] + second["items"]}
        self.assertNotIn(str(rows[2].pk), all_ids)
        self.assertNotIn(str(draft.pk), all_ids)

    def test_queue_schema_does_not_expose_private_media_storage(self):
        self.create_submissions(1)
        result = self.queue(self.moderator)
        serialized = json.dumps(result)
        for private_name in (
            "imageSet",
            "mediaUploadIntents",
            "storageBucket",
            "storageKey",
            "objectKey",
            "sealedObjectKey",
            "credentials",
        ):
            self.assertNotIn(private_name, serialized)

        request_type = schema.graphql_schema.get_type("RequestType")
        self.assertNotIn("imageSet", request_type.fields)
        self.assertIn("mediaAttachmentPreviewV3", schema.graphql_schema.query_type.fields)

    def test_model_declares_the_keyset_ordering_index(self):
        indexes = {
            index.name: tuple(index.fields) for index in Request._meta.indexes
        }
        self.assertEqual(
            indexes["request_mod_queue_idx"],
            ("state", "date_created", "id"),
        )
