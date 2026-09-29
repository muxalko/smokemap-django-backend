import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from graphene.test import Client as GraphQLClient
from rest_framework.test import APIClient

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
from backend.schema import (
    LEGACY_PUBLIC_PLACE_LIMIT,
    PLACES_DEPRECATION_REASON,
    schema,
)
from backend.tokens import issue_token_pair


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
            ("ab\x00", "null characters"),
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
            ({"q": "\x00\x00"}, "null characters"),
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
        self.assertIn(" % ", data_queries[0])

    def test_pending_submission_is_not_visible_and_public_results_ignore_role(self):
        visible = self.create_place("Shared Public Place")
        User = get_user_model()
        user = User.objects.create_user(
            email="searcher@example.test",
            password="irrelevant-test-password",
        )
        moderator = User.objects.create_user(
            email="search-moderator@example.test",
            password="irrelevant-test-password",
            is_staff=True,
        )
        administrator = User.objects.create_superuser(
            email="search-administrator@example.test",
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
        for account in (user, moderator, administrator):
            with self.subTest(account=account.email):
                response = self.client.get(
                    self.endpoint,
                    {"q": "shared"},
                    HTTP_AUTHORIZATION=f"Bearer {issue_token_pair(account)['token']}",
                )
                self.assertEqual(response.wsgi_request.user, account)
                self.assertEqual(response.json(), anonymous)
        self.assertEqual(
            [result["id"] for result in anonymous["results"]],
            [visible.pk],
        )
        self.assertNotIn("Pending Secret", str(anonymous))

    def test_query_without_trigrams_searches_prefixes_only(self):
        punctuated = self.create_place("!! Punctuated Place")
        self.create_place("Plain Place")

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.endpoint, {"q": "!!"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [(result["id"], result["match"]) for result in response.json()["results"]],
            [(punctuated.pk, "prefix")],
        )
        data_query = next(
            query["sql"] for query in queries if 'FROM "backend_place"' in query["sql"]
        )
        self.assertNotIn(" % ", data_query)

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

    def test_deprecated_places_field_stays_unbounded_and_approved_only(self):
        places = [
            self.create_place(f"Everything {index:02d}")
            for index in range(LEGACY_PUBLIC_PLACE_LIMIT + 5)
        ]
        accounts = create_role_accounts("legacy-graphql")
        create_pending_request(self.category, accounts["user"])

        anonymous = self.client.post(
            "/graphql/",
            data=json.dumps({"query": "{ places { id name } }"}),
            content_type="application/json",
        )

        self.assertEqual(anonymous.status_code, 200)
        payload = anonymous.json()
        self.assertNotIn("errors", payload)
        self.assertEqual(
            sorted(int(place["id"]) for place in payload["data"]["places"]),
            [place.pk for place in places],
        )
        self.assertNotIn("Pending Secret", str(payload))
        for role, account in accounts.items():
            with self.subTest(role=role):
                response = self.client.post(
                    "/graphql/",
                    data=json.dumps({"query": "{ places { id name } }"}),
                    content_type="application/json",
                    HTTP_AUTHORIZATION=f"Bearer {issue_token_pair(account)['token']}",
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), payload)

    def test_graphql_exposes_no_direct_place_write_mutation(self):
        result = self.graphql.execute(
            '{ __type(name: "Mutation") { fields { name } } }'
        )

        self.assertNotIn("errors", result)
        names = [field["name"] for field in result["data"]["__type"]["fields"]]
        self.assertEqual(
            [name for name in names if "place" in name.lower()],
            [],
        )

    def test_legacy_exact_name_field_keeps_shape_and_cap(self):
        places = [
            self.create_place("Duplicate Legacy Name")
            for _ in range(LEGACY_PUBLIC_PLACE_LIMIT + 5)
        ]

        result = self.graphql.execute(
            "query($name: String!) { placesByName(name: $name) { id name } }",
            variable_values={"name": "Duplicate Legacy Name"},
        )

        self.assertNotIn("errors", result)
        self.assertEqual(
            result["data"]["placesByName"],
            [
                {"id": str(place.pk), "name": "Duplicate Legacy Name"}
                for place in places[:LEGACY_PUBLIC_PLACE_LIMIT]
            ],
        )

    def test_only_the_unbounded_places_field_is_deprecated(self):
        result = self.graphql.execute(
            """
            {
              __type(name: "Query") {
                fields(includeDeprecated: true) {
                  name
                  isDeprecated
                  deprecationReason
                }
              }
            }
            """
        )

        self.assertNotIn("errors", result)
        fields = {
            field["name"]: field for field in result["data"]["__type"]["fields"]
        }
        self.assertTrue(fields["places"]["isDeprecated"])
        self.assertEqual(
            fields["places"]["deprecationReason"], PLACES_DEPRECATION_REASON
        )
        self.assertIn("/api/v1/places/search/", PLACES_DEPRECATION_REASON)
        for name in (
            "placeById",
            "placesByName",
            "placesNames",
            "placesStartwithName",
        ):
            with self.subTest(field=name):
                self.assertFalse(fields[name]["isDeprecated"])


class PublicAddressGraphQLExposureTests(TestCase):
    """Public address features expose a label and point, never submissions."""

    ADDRESS_SELECTION = (
        "address { type id properties { addressString } "
        "geometry { type coordinates } }"
    )

    def setUp(self):
        self.category = Category.objects.get(slug="outdoors")
        self.graphql = GraphQLClient(schema)
        self.accounts = create_role_accounts("address-graphql")
        self.address = Address.objects.create(
            addressString="Shared public address",
            location=Point(-76.5, 38.5, srid=4326),
        )
        self.place = Place.objects.create(
            name="Address Place", category=self.category, address=self.address
        )
        Request.objects.create(
            name="Address Pending Secret",
            category=self.category,
            description="Pending Secret description",
            address=self.address,
            owner=self.accounts["user"],
            state=Request.State.PENDING,
            approved=False,
            approved_comment="Pending Secret comment",
        )

    def post(self, query, account=None, variables=None):
        headers = {}
        if account is not None:
            headers["HTTP_AUTHORIZATION"] = (
                f"Bearer {issue_token_pair(account)['token']}"
            )
        return self.client.post(
            "/graphql/",
            data=json.dumps({"query": query, "variables": variables or {}}),
            content_type="application/json",
            **headers,
        )

    def test_address_types_introspect_only_whitelisted_fields(self):
        result = self.graphql.execute(
            """
            {
              address: __type(name: "AddressType") { fields { name } }
              properties: __type(name: "AddressProperties") { fields { name } }
            }
            """
        )

        self.assertNotIn("errors", result)
        self.assertEqual(
            {field["name"] for field in result["data"]["address"]["fields"]},
            {"type", "id", "geometry", "bbox", "properties"},
        )
        self.assertEqual(
            [field["name"] for field in result["data"]["properties"]["fields"]],
            ["addressString"],
        )

    def test_reverse_request_and_place_relations_are_unqueryable_for_every_role(self):
        queries = {
            "places": "{ places { address { properties { %s { id name } } } } }",
            "placeById": (
                'query($id: ID) { placeById(id: $id) '
                "{ address { properties { %s { id name } } } } }"
            ),
            "addresses": "{ addresses { properties { %s { id name } } } }",
        }
        accounts = {"guest": None, **self.accounts}
        for relation in ("requestSet", "placeSet"):
            for field, template in queries.items():
                for role, account in accounts.items():
                    with self.subTest(relation=relation, field=field, role=role):
                        response = self.post(
                            template % relation,
                            account,
                            {"id": str(self.place.pk)},
                        )
                        payload = response.json()
                        self.assertNotIn("data", payload)
                        self.assertEqual(len(payload["errors"]), 1)
                        self.assertIn(
                            f"Cannot query field '{relation}' on type "
                            "'AddressProperties'.",
                            payload["errors"][0]["message"],
                        )
                        self.assertNotIn("Pending Secret", str(payload))

    def test_frontend_address_label_and_coordinates_are_preserved(self):
        expected = {
            "type": "Feature",
            "id": str(self.address.pk),
            "properties": {"addressString": "Shared public address"},
            "geometry": {"type": "Point", "coordinates": [-76.5, 38.5]},
        }
        by_id = self.post(
            "query($id: ID) { placeById(id: $id) { %s } }"
            % self.ADDRESS_SELECTION,
            variables={"id": str(self.place.pk)},
        ).json()
        listed = self.post("{ places { %s } }" % self.ADDRESS_SELECTION).json()
        addresses = self.post(
            "{ addresses { type id properties { addressString } "
            "geometry { type coordinates } } }"
        ).json()

        self.assertNotIn("errors", by_id)
        self.assertEqual(by_id["data"]["placeById"]["address"], expected)
        self.assertNotIn("errors", listed)
        self.assertEqual(listed["data"]["places"], [{"address": expected}])
        self.assertNotIn("errors", addresses)
        self.assertEqual(addresses["data"]["addresses"], [expected])
        self.assertNotIn("Pending Secret", str([by_id, listed, addresses]))


class LegacyRestPlaceListingTests(TestCase):
    """Pin the legacy public `/places/` listing, which is not a search surface."""

    def setUp(self):
        self.category = Category.objects.get(slug="outdoors")
        self.accounts = create_role_accounts("legacy-rest")
        self.places = [
            Place.objects.create(
                name=f"Listed {index:02d}",
                category=self.category,
                address=Address.objects.create(
                    addressString=f"Listed {index:02d} address",
                    location=Point(-77.0, 39.0, srid=4326),
                ),
            )
            for index in range(LEGACY_PUBLIC_PLACE_LIMIT + 5)
        ]
        create_pending_request(self.category, self.accounts["user"])

    def get(self, path, account=None, **params):
        headers = {}
        if account is not None:
            headers["HTTP_AUTHORIZATION"] = (
                f"Bearer {issue_token_pair(account)['token']}"
            )
        return self.client.get(path, params, **headers)

    def test_listing_stays_unbounded_approved_only_and_role_independent(self):
        anonymous = self.get("/places/")

        self.assertEqual(anonymous.status_code, 200)
        payload = anonymous.json()
        self.assertEqual(payload["type"], "FeatureCollection")
        self.assertEqual(
            sorted(
                feature["properties"]["place_id"] for feature in payload["features"]
            ),
            [place.pk for place in self.places],
        )
        self.assertNotIn("Pending Secret", str(payload))
        for role, account in self.accounts.items():
            with self.subTest(role=role):
                response = self.get("/places/", account)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), payload)

    def test_bbox_listing_and_detail_exclude_submission_rows(self):
        in_bbox = self.get("/places/", in_bbox="-77.05,38.95,-76.95,39.05")
        detail = self.get(f"/places/{self.places[0].pk}/")

        self.assertEqual(in_bbox.status_code, 200)
        self.assertEqual(
            len(in_bbox.json()["features"]), LEGACY_PUBLIC_PLACE_LIMIT + 5
        )
        self.assertNotIn("Pending Secret", str(in_bbox.json()))
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(
            detail.json()["properties"]["place_id"], self.places[0].pk
        )

    def test_guest_and_non_administrator_writes_are_denied(self):
        place = self.places[0]
        body = {"name": "Overwritten", "category": self.category.pk}
        writes = (
            ("post", "/places/"),
            ("put", f"/places/{place.pk}/"),
            ("patch", f"/places/{place.pk}/"),
            ("delete", f"/places/{place.pk}/"),
        )
        for role, status in ((None, 401), ("user", 403), ("moderator", 403)):
            client = APIClient()
            if role is not None:
                client.credentials(
                    HTTP_AUTHORIZATION=(
                        f"Bearer {issue_token_pair(self.accounts[role])['token']}"
                    )
                )
            for method, path in writes:
                with self.subTest(role=role or "guest", method=method):
                    response = getattr(client, method)(path, body, format="json")
                    self.assertEqual(response.status_code, status)

        place.refresh_from_db()
        self.assertEqual(place.name, "Listed 00")
        self.assertEqual(Place.objects.count(), LEGACY_PUBLIC_PLACE_LIMIT + 5)


def create_role_accounts(prefix):
    User = get_user_model()
    return {
        "user": User.objects.create_user(
            email=f"{prefix}-user@example.test",
            password="irrelevant-test-password",
        ),
        "moderator": User.objects.create_user(
            email=f"{prefix}-moderator@example.test",
            password="irrelevant-test-password",
            is_staff=True,
        ),
        "administrator": User.objects.create_superuser(
            email=f"{prefix}-administrator@example.test",
            password="irrelevant-test-password",
        ),
    }


def create_pending_request(category, owner):
    return Request.objects.create(
        name="Everything Pending Secret",
        category=category,
        address=Address.objects.create(
            addressString="Pending Secret address",
            location=Point(-77.01, 39.01, srid=4326),
        ),
        owner=owner,
        state=Request.State.PENDING,
        approved=False,
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
