from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from graphene.test import Client as GraphQLClient

from backend.models import Address, Category, Place, Request
from backend.place_search import (
    SEARCH_DEFAULT_LIMIT,
    SEARCH_QUERY_MAX_LENGTH,
    SEARCH_RESULT_LIMIT,
    PlaceSearchInputError,
    normalize_search_query,
    parse_search_limit,
)
from backend.place_search_plan import (
    PREFIX_INDEX,
    REPORT_SCHEMA,
    TRIGRAM_INDEX,
    benchmark_place_name,
    render_report_text,
    report_failures,
    search_indexes,
)
from backend.schema import LEGACY_PUBLIC_PLACE_LIMIT, schema


class PlaceSearchInputTests(SimpleTestCase):
    def test_query_normalization_is_nfkc_case_and_whitespace_stable(self):
        self.assertEqual(
            normalize_search_query(" \tＡLPhA\n  Lounge  "),
            "alpha lounge",
        )

    def test_missing_short_and_long_queries_are_rejected(self):
        cases = (
            (None, "required"),
            (" \t ", "at least 2"),
            ("a", "at least 2"),
            ("x" * (SEARCH_QUERY_MAX_LENGTH + 1), "at most 100"),
        )
        for query, message in cases:
            with self.subTest(query=query):
                with self.assertRaisesMessage(PlaceSearchInputError, message):
                    normalize_search_query(query)

    def test_limit_has_an_explicit_default_and_hard_range(self):
        self.assertEqual(parse_search_limit(None), SEARCH_DEFAULT_LIMIT)
        self.assertEqual(parse_search_limit("1"), 1)
        self.assertEqual(parse_search_limit(str(SEARCH_RESULT_LIMIT)), 20)
        for value in ("0", "21", "1.5", "many"):
            with self.subTest(value=value):
                with self.assertRaises(PlaceSearchInputError):
                    parse_search_limit(value)


class PlaceSearchApiTests(TestCase):
    endpoint = "/api/v1/places/search/"

    def setUp(self):
        self.category = Category.objects.get(slug="outdoors")

    def create_place(self, name, *, longitude=-77.0, latitude=39.0):
        address = Address.objects.create(
            addressString=f"{name} address",
            location=Point(longitude, latitude, srid=4326),
        )
        return Place.objects.create(
            name=name,
            category=self.category,
            description=f"private description for {name}",
            address=address,
            website="https://private-metadata.example.test",
        )

    def test_empty_short_long_and_invalid_limit_inputs_fail_predictably(self):
        cases = (
            ({}, "q is required"),
            ({"q": " "}, "at least 2"),
            ({"q": "a"}, "at least 2"),
            ({"q": "x" * 101}, "at most 100"),
            ({"q": "valid", "limit": "0"}, "limit"),
            ({"q": "valid", "limit": "21"}, "limit"),
            ({"q": "valid", "limit": "all"}, "limit"),
        )
        for params, detail in cases:
            with self.subTest(params=params):
                response = self.client.get(self.endpoint, params)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["code"], "invalid_search")
                self.assertIn(detail, response.json()["detail"])

    def test_normalized_prefixes_rank_before_fuzzy_matches_deterministically(self):
        prefix_b = self.create_place("Alpha Lounge B")
        fuzzy = self.create_place("The Alpha Lounge")
        prefix_a = self.create_place("Alpha Lounge A")

        response = self.client.get(
            self.endpoint,
            {"q": "  ＡLPHA   LOUNGE ", "limit": "10"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["query"], "alpha lounge")
        self.assertEqual(payload["limit"], 10)
        self.assertEqual(
            [result["id"] for result in payload["results"]],
            [prefix_a.pk, prefix_b.pk, fuzzy.pk],
        )
        self.assertEqual(
            [result["match"] for result in payload["results"]],
            ["prefix", "prefix", "fuzzy"],
        )

    def test_result_contains_only_autocomplete_location_and_category_context(self):
        place = self.create_place("Context Place", longitude=-76.5, latitude=38.5)

        result = self.client.get(self.endpoint, {"q": "context"}).json()["results"][0]

        self.assertEqual(result["id"], place.pk)
        self.assertEqual(
            set(result),
            {"id", "name", "address", "location", "category", "match"},
        )
        self.assertEqual(
            result["location"],
            {"type": "Point", "coordinates": [-76.5, 38.5]},
        )
        self.assertEqual(
            result["category"],
            {"id": self.category.pk, "slug": "outdoors", "name": "Outdoors"},
        )
        self.assertNotIn("private", str(result).lower())

    def test_no_match_is_successful_and_sql_metacharacters_are_data(self):
        self.create_place("Ordinary Place")

        no_match = self.client.get(self.endpoint, {"q": "zzzzzzzz"})
        injection = self.client.get(self.endpoint, {"q": "' OR 1=1 --"})

        self.assertEqual(no_match.status_code, 200)
        self.assertEqual(no_match.json()["results"], [])
        self.assertEqual(injection.status_code, 200)
        self.assertEqual(injection.json()["results"], [])

    def test_limit_is_enforced_in_sql_with_only_one_data_select(self):
        for index in range(25):
            self.create_place(f"Bounded Place {index:02d}")

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(
                self.endpoint,
                {"q": "bounded place", "limit": str(SEARCH_RESULT_LIMIT)},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["results"]), SEARCH_RESULT_LIMIT)
        statements = [query["sql"] for query in queries]
        self.assertEqual(
            sum("set_config('pg_trgm.similarity_threshold'" in sql for sql in statements),
            1,
        )
        data_queries = [sql for sql in statements if 'FROM "backend_place"' in sql]
        self.assertEqual(len(data_queries), 1)
        self.assertIn("LIMIT 20", data_queries[0])

    def test_pending_submission_is_not_visible_and_public_results_ignore_role(self):
        visible = self.create_place("Shared Public Place")
        user = get_user_model().objects.create_user(
            email="searcher@example.test",
            password="irrelevant-test-password",
        )
        pending_address = Address.objects.create(
            addressString="Pending Secret address",
            location=Point(-77.1, 39.1, srid=4326),
        )
        Request.objects.create(
            name="Shared Pending Secret",
            category=self.category,
            address=pending_address,
            owner=user,
            state=Request.State.PENDING,
            approved=False,
        )

        anonymous = self.client.get(self.endpoint, {"q": "shared"}).json()
        self.client.force_login(user)
        authenticated = self.client.get(self.endpoint, {"q": "shared"}).json()

        self.assertEqual(anonymous, authenticated)
        self.assertEqual(
            [result["id"] for result in anonymous["results"]],
            [visible.pk],
        )
        self.assertNotIn("Pending Secret", str(anonymous))

    def test_non_get_methods_are_not_search_or_write_surfaces(self):
        response = self.client.post(self.endpoint, {"q": "anything"})

        self.assertEqual(response.status_code, 405)


class LegacyPlaceSearchCompatibilityTests(TestCase):
    def setUp(self):
        self.category = Category.objects.get(slug="outdoors")
        self.graphql = GraphQLClient(schema)

    def create_place(self, name):
        address = Address.objects.create(
            addressString=f"{name} address",
            location=Point(-77.0, 39.0, srid=4326),
        )
        return Place.objects.create(name=name, category=self.category, address=address)

    def test_legacy_name_collection_keeps_shape_but_is_deterministic_and_capped(self):
        for index in reversed(range(LEGACY_PUBLIC_PLACE_LIMIT + 5)):
            self.create_place(f"Legacy {index:02d}")

        result = self.graphql.execute("{ placesNames }")

        self.assertNotIn("errors", result)
        self.assertEqual(len(result["data"]["placesNames"]), LEGACY_PUBLIC_PLACE_LIMIT)
        self.assertEqual(result["data"]["placesNames"][0], "Legacy 00")
        self.assertEqual(result["data"]["placesNames"][-1], "Legacy 19")

    def test_legacy_startswith_field_uses_bounded_normalized_ranked_search(self):
        prefix = self.create_place("Legacy Alpha")
        self.create_place("Unrelated")

        result = self.graphql.execute(
            "query($name: String!) { placesStartwithName(name: $name) { id name } }",
            variable_values={"name": "  LEGACY   ALPHA "},
        )

        self.assertNotIn("errors", result)
        self.assertEqual(
            result["data"]["placesStartwithName"],
            [{"id": str(prefix.pk), "name": "Legacy Alpha"}],
        )


def passing_plan_report():
    return {
        "schema": REPORT_SCHEMA,
        "result": "pass",
        "failures": [],
        "dataset": {"places": 20_000},
        "planner_settings": {"enable_seqscan": "on"},
        "indexes": {
            PREFIX_INDEX: "CREATE INDEX ... USING btree (lower((name)::text) varchar_pattern_ops)",
            TRIGRAM_INDEX: (
                "CREATE INDEX ... USING gin (lower((name)::text) gin_trgm_ops) "
                "WITH (fastupdate=off)"
            ),
        },
        "queries": {
            "prefix": {
                "query": "cedar",
                "used_indexes": [PREFIX_INDEX],
                "planning_time_ms": 0.1,
                "execution_time_ms": 0.2,
            },
            "fuzzy": {
                "query": "cedra",
                "used_indexes": [TRIGRAM_INDEX],
                "planning_time_ms": 0.1,
                "execution_time_ms": 0.2,
            },
        },
    }


class PlaceSearchIndexMigrationTests(TestCase):
    def test_migrated_schema_has_valid_prefix_and_immediate_trigram_indexes(self):
        indexes = search_indexes()

        self.assertIn("varchar_pattern_ops", indexes[PREFIX_INDEX])
        self.assertIn("gin_trgm_ops", indexes[TRIGRAM_INDEX])
        self.assertIn("fastupdate=off", indexes[TRIGRAM_INDEX])


class PlaceSearchPlanEvidenceTests(SimpleTestCase):
    def test_fixture_names_are_deterministic_and_include_rank_paths(self):
        self.assertEqual(benchmark_place_name(0), "Cedar Corner 00000")
        self.assertEqual(benchmark_place_name(100), "Willow Smokehouse")
        self.assertEqual(benchmark_place_name(101), benchmark_place_name(101))
        self.assertNotEqual(benchmark_place_name(101), benchmark_place_name(102))

    def test_plan_contract_requires_natural_prefix_and_fuzzy_indexes(self):
        self.assertEqual(report_failures(passing_plan_report()), [])
        report = passing_plan_report()
        report["planner_settings"]["enable_seqscan"] = "off"
        report["queries"]["prefix"]["used_indexes"] = []
        report["queries"]["fuzzy"]["used_indexes"] = []

        failures = report_failures(report)

        self.assertIn("enable_seqscan must remain on for natural plans", failures)
        self.assertIn("prefix plan did not use a place-search index", failures)
        self.assertIn("fuzzy plan did not use the trigram index", failures)

    def test_plan_contract_requires_trigram_index_without_pending_list(self):
        report = passing_plan_report()
        report["indexes"][TRIGRAM_INDEX] = (
            "CREATE INDEX ... USING gin (lower((name)::text) gin_trgm_ops)"
        )

        self.assertEqual(
            report_failures(report),
            [f"{TRIGRAM_INDEX} must disable the GIN pending list"],
        )

        del report["indexes"][TRIGRAM_INDEX]

        self.assertEqual(
            report_failures(report),
            [f"missing valid search index {TRIGRAM_INDEX}"],
        )

    def test_text_report_records_queries_indexes_and_timings(self):
        report = passing_plan_report()
        rendered = render_report_text(report)

        self.assertIn("Places: 20000", rendered)
        self.assertIn(PREFIX_INDEX, rendered)
        self.assertIn(TRIGRAM_INDEX, rendered)
        self.assertIn("execution_ms=0.2", rendered)

    @override_settings(DEBUG=False)
    @patch("backend.management.commands.inspect_place_search_plans.run_plan_inspection")
    def test_command_refuses_non_debug_environment_before_writes(self, inspection):
        with self.assertRaisesMessage(CommandError, "DEBUG-enabled"):
            call_command("inspect_place_search_plans")
        inspection.assert_not_called()
