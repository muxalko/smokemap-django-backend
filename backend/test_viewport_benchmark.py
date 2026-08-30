import json
from unittest.mock import Mock, patch

from django.contrib.gis.geos import Point
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from backend.models import Address, Category, Place, Tag

from backend.viewport_benchmark import (
    BENCHMARK_CATEGORY_NAME,
    GRID_COLUMNS,
    GRID_ROWS,
    NAMESPACE,
    REPORT_SCHEMA,
    SAMPLE_RUNS,
    TOTAL_PLACES,
    VIEWPORT_MATCHES,
    analyze_benchmark_tables,
    benchmark_coordinates,
    benchmark_failures,
    build_report,
    capture_natural_plan,
    cleanup_benchmark_namespace,
    coordinate_is_in_viewport,
    namespace_counts,
    plan_nodes,
    render_report_json,
    render_report_text,
    seed_benchmark_dataset,
)


def passing_report():
    warmups = [
        {
            "warmup": number,
            "status_code": 200,
            "content_type": "application/geo+json",
            "geojson_type": "FeatureCollection",
            "query_count": 2,
            "feature_count": VIEWPORT_MATCHES,
            "benchmark_feature_count": VIEWPORT_MATCHES,
            "non_benchmark_feature_count": 0,
            "encoded_geojson_bytes": 123_456,
            "server_processing_ms": 45.125,
        }
        for number in range(1, 4)
    ]
    samples = [
        {
            "sample": number,
            "status_code": 200,
            "content_type": "application/geo+json",
            "geojson_type": "FeatureCollection",
            "query_count": 2,
            "feature_count": VIEWPORT_MATCHES,
            "benchmark_feature_count": VIEWPORT_MATCHES,
            "non_benchmark_feature_count": 0,
            "encoded_geojson_bytes": 123_456,
            "server_processing_ms": 42.125,
        }
        for number in range(1, SAMPLE_RUNS + 1)
    ]
    return {
        "schema": REPORT_SCHEMA,
        "result": "pass",
        "failures": [],
        "endpoint": {
            "query": {
                "bbox": "-77.02,38.83,-76.98,38.87",
                "zoom": "13",
                "categories": "123",
            }
        },
        "dataset": {
            "name": "deterministic Washington, DC populated-city grid",
            "namespace": NAMESPACE,
            "actual_total_places": TOTAL_PLACES,
            "database_total_places_during_run": TOTAL_PLACES,
            "database_viewport_places_during_run": VIEWPORT_MATCHES,
            "actual_viewport_matches": VIEWPORT_MATCHES,
            "synthetic_viewport_matches": VIEWPORT_MATCHES,
        },
        "budgets": {
            "max_queries": 2,
            "max_features": 500,
            "max_encoded_geojson_bytes": 512 * 1024,
            "max_server_processing_ms": 250.0,
        },
        "warmup_count": 3,
        "sample_count": SAMPLE_RUNS,
        "warmups": warmups,
        "samples": samples,
        "plan": {
            "explain": "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)",
            "planner_settings": {
                "enable_seqscan": "on",
                "enable_indexscan": "on",
                "enable_bitmapscan": "on",
            },
            "settings_overridden_by_harness": [],
            "address_location_gist_indexes": ["address_location_gist"],
            "used_address_location_gist_indexes": ["address_location_gist"],
            "gist_index_used": True,
            "planning_time_ms": 0.5,
            "execution_time_ms": 1.5,
            "raw": {
                "Plan": {
                    "Node Type": "Bitmap Heap Scan",
                    "Actual Rows": VIEWPORT_MATCHES,
                    "Actual Total Time": 1.25,
                    "Plans": [
                        {
                            "Node Type": "Bitmap Index Scan",
                            "Index Name": "address_location_gist",
                            "Actual Rows": VIEWPORT_MATCHES,
                            "Actual Total Time": 0.25,
                        }
                    ],
                }
            },
        },
    }


class ViewportBenchmarkDatasetTests(SimpleTestCase):
    def test_grid_is_deterministic_with_exact_viewport_population(self):
        coordinates = list(benchmark_coordinates())

        self.assertEqual(len(coordinates), TOTAL_PLACES)
        self.assertEqual(TOTAL_PLACES, GRID_COLUMNS * GRID_ROWS)
        self.assertEqual(
            len(
                set(
                    (longitude, latitude)
                    for _, longitude, latitude in coordinates
                )
            ),
            TOTAL_PLACES,
        )
        self.assertEqual(coordinates[0], (0, -77.199, 38.751))
        self.assertEqual(coordinates[-1], (19_999, -76.801, 38.949))
        self.assertEqual(
            sum(
                coordinate_is_in_viewport(longitude, latitude)
                for _, longitude, latitude in coordinates
            ),
            VIEWPORT_MATCHES,
        )

    @override_settings(DEBUG=False)
    def test_command_refuses_non_debug_environments_before_database_writes(self):
        with self.assertRaisesMessage(CommandError, "DEBUG-enabled"):
            call_command("benchmark_viewport_places")


class ViewportBenchmarkEvidenceTests(SimpleTestCase):
    def test_plan_traversal_finds_nested_index(self):
        nodes = list(plan_nodes(passing_report()["plan"]["raw"]["Plan"]))

        self.assertEqual(
            [node["Node Type"] for node in nodes],
            ["Bitmap Heap Scan", "Bitmap Index Scan"],
        )
        self.assertEqual(nodes[1]["Index Name"], "address_location_gist")

    @patch(
        "backend.viewport_benchmark.address_location_gist_indexes",
        return_value=["address_location_gist"],
    )
    @patch(
        "backend.viewport_benchmark.planner_settings",
        return_value={"enable_seqscan": "on"},
    )
    @patch("backend.viewport_benchmark.viewport_places_queryset")
    def test_natural_plan_capture_proves_the_real_gist_index_name(
        self,
        queryset_builder,
        _planner_settings,
        _gist_indexes,
    ):
        explained_queryset = Mock()
        explained_queryset.explain.return_value = json.dumps(
            [passing_report()["plan"]["raw"]]
        )
        queryset_builder.return_value.__getitem__.return_value = explained_queryset

        viewport = object()
        plan = capture_natural_plan(viewport, category_id=123)

        queryset_builder.assert_called_once_with(
            viewport,
            category_ids=(123,),
        )
        queryset_builder.return_value.__getitem__.assert_called_once_with(
            slice(None, 501, None)
        )
        explained_queryset.explain.assert_called_once_with(
            format="json",
            analyze=True,
            buffers=True,
        )
        self.assertEqual(
            plan["used_address_location_gist_indexes"],
            ["address_location_gist"],
        )
        self.assertTrue(plan["gist_index_used"])

    def test_passing_samples_and_natural_gist_plan_satisfy_contract(self):
        self.assertEqual(benchmark_failures(passing_report()), [])

    def test_each_sample_and_natural_planner_setting_are_enforced(self):
        report = passing_report()
        report["samples"][2]["query_count"] = 3
        report["samples"][4]["server_processing_ms"] = 250.001
        report["samples"][1]["benchmark_feature_count"] = 399
        report["samples"][1]["non_benchmark_feature_count"] = 1
        report["plan"]["planner_settings"]["enable_seqscan"] = "off"
        report["plan"]["gist_index_used"] = False

        failures = benchmark_failures(report)

        self.assertIn("sample 3 used 3 queries", failures)
        self.assertIn("sample 2 returned 399 benchmark features", failures)
        self.assertIn("sample 2 returned 1 non-benchmark features", failures)
        self.assertIn("sample 5 exceeded the timing budget", failures)
        self.assertIn("enable_seqscan must remain on for the natural plan", failures)
        self.assertIn(
            "the natural analyzed plan did not use Address.location GiST",
            failures,
        )

    def test_text_and_json_evidence_are_canonical_and_substantive(self):
        report = passing_report()

        text = render_report_text(report)
        encoded_json = render_report_json(report)

        self.assertIn("Places: 20000 total; 400 viewport matches", text)
        self.assertIn("Sample 5: status=200; queries=2; features=400", text)
        self.assertIn("Bitmap Index Scan using address_location_gist", text)
        self.assertEqual(json.loads(encoded_json)["schema"], REPORT_SCHEMA)
        self.assertEqual(encoded_json, render_report_json(report))


class ViewportBenchmarkNamespaceCleanupTests(TransactionTestCase):
    def create_place(self, name, category, address_string, longitude):
        address = Address(
            addressString=address_string,
            location=Point(longitude, 38.85, srid=4326),
        )
        Address.objects.bulk_create([address])
        return Place.objects.create(
            name=name,
            category=category,
            description="Cleanup isolation test place.",
            address=address,
        )

    def test_cleanup_removes_only_complete_benchmark_namespace(self):
        benchmark_category = Category.objects.create(
            slug=f"{NAMESPACE}category",
            name=BENCHMARK_CATEGORY_NAME,
        )
        benchmark_tag = Tag.objects.create(name=f"{NAMESPACE}tag_0")
        benchmark_place = self.create_place(
            f"{NAMESPACE}place_00000",
            benchmark_category,
            f"{NAMESPACE}address_00000",
            -77.001,
        )
        benchmark_place.tags.add(benchmark_tag)

        unrelated_category = Category.objects.create(
            slug="unrelated-category",
            name="Unrelated category",
        )
        unrelated_place = self.create_place(
            "Unrelated viewport place",
            unrelated_category,
            "Unrelated viewport address",
            -77.002,
        )

        removed = cleanup_benchmark_namespace()

        self.assertEqual(
            removed,
            {"addresses": 1, "places": 1, "tags": 1, "categories": 1},
        )
        self.assertTrue(Place.objects.filter(pk=unrelated_place.pk).exists())
        self.assertTrue(Address.objects.filter(pk=unrelated_place.address_id).exists())
        self.assertTrue(Category.objects.filter(pk=unrelated_category.pk).exists())

    def test_cleanup_refuses_cross_namespace_category_without_deleting(self):
        benchmark_category = Category.objects.create(
            slug=f"{NAMESPACE}category",
            name=BENCHMARK_CATEGORY_NAME,
        )
        unrelated_place = self.create_place(
            "Unrelated place using reserved category",
            benchmark_category,
            "Unrelated address using reserved category",
            -77.003,
        )

        with self.assertRaisesMessage(
            RuntimeError,
            "a non-benchmark place uses a namespaced category",
        ):
            cleanup_benchmark_namespace()

        self.assertTrue(Place.objects.filter(pk=unrelated_place.pk).exists())
        self.assertTrue(Category.objects.filter(pk=benchmark_category.pk).exists())


class ViewportBenchmarkDatabaseIsolationTests(TransactionTestCase):
    def test_real_endpoint_returns_400_benchmark_features_with_unrelated_bbox_row(self):
        unrelated_category = Category.objects.create(
            slug="unrelated-category",
            name="Unrelated category",
        )
        unrelated_address = Address(
            addressString="Unrelated address inside benchmark viewport",
            location=Point(-77.0, 38.85, srid=4326),
        )
        Address.objects.bulk_create([unrelated_address])
        unrelated_place = Place.objects.create(
            name="Unrelated place inside benchmark viewport",
            category=unrelated_category,
            description="Must survive and stay outside benchmark measurements.",
            address=unrelated_address,
        )

        with transaction.atomic():
            benchmark_category = seed_benchmark_dataset()
            analyze_benchmark_tables()
            report = build_report(
                {
                    "addresses": 0,
                    "places": 0,
                    "tags": 0,
                    "categories": 0,
                },
                benchmark_category.pk,
            )
            transaction.set_rollback(True)
        analyze_benchmark_tables()

        self.assertEqual(report["dataset"]["database_viewport_places_during_run"], 401)
        self.assertEqual(report["dataset"]["actual_viewport_matches"], 400)
        for measurement in report["warmups"] + report["samples"]:
            self.assertEqual(measurement["feature_count"], 400)
            self.assertEqual(measurement["benchmark_feature_count"], 400)
            self.assertEqual(measurement["non_benchmark_feature_count"], 0)
        self.assertTrue(Place.objects.filter(pk=unrelated_place.pk).exists())
        self.assertTrue(Address.objects.filter(pk=unrelated_address.pk).exists())
        self.assertEqual(
            namespace_counts(),
            {"addresses": 0, "places": 0, "tags": 0, "categories": 0},
        )
