import json
from decimal import Decimal
from pathlib import Path
from time import perf_counter_ns

import django
from django.contrib.gis.geos import Point, Polygon
from django.db import connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext

from backend.models import Address, Category, Image, Place, Request, RequestTag, Tag
from backend.tagging import normalize_tag_text
from backend.views import (
    VIEWPORT_RESPONSE_LIMIT_BYTES,
    VIEWPORT_RESULT_LIMIT,
    viewport_places_queryset,
)


REPORT_SCHEMA = "smokemap.viewport-places-benchmark.v1"
NAMESPACE = "__sm79_vpbench_v1__"
BENCHMARK_CATEGORY_NAME = NAMESPACE
TOTAL_PLACES = 20_000
VIEWPORT_MATCHES = 400
GRID_COLUMNS = 200
GRID_ROWS = 100
GRID_MIN_LONGITUDE = Decimal("-77.200")
GRID_MIN_LATITUDE = Decimal("38.750")
GRID_STEP = Decimal("0.002")
VIEWPORT_BBOX = (-77.020, 38.830, -76.980, 38.870)
VIEWPORT_ZOOM = 13
WARMUP_RUNS = 3
SAMPLE_RUNS = 5
SERVER_PROCESSING_LIMIT_MS = 250.0
ENDPOINT = "/api/v1/places/"
EVIDENCE_TEXT_FILENAME = "viewport-places-benchmark.txt"
EVIDENCE_JSON_FILENAME = "viewport-places-benchmark.json"
TAG_COUNT = 4
BULK_BATCH_SIZE = 2_000


def benchmark_coordinates():
    """Yield the fixed row-major populated-city grid used by the benchmark."""
    for row in range(GRID_ROWS):
        latitude = GRID_MIN_LATITUDE + (Decimal(row) + Decimal("0.5")) * GRID_STEP
        for column in range(GRID_COLUMNS):
            longitude = (
                GRID_MIN_LONGITUDE
                + (Decimal(column) + Decimal("0.5")) * GRID_STEP
            )
            yield row * GRID_COLUMNS + column, float(longitude), float(latitude)


def coordinate_is_in_viewport(longitude, latitude):
    minx, miny, maxx, maxy = VIEWPORT_BBOX
    return minx <= longitude <= maxx and miny <= latitude <= maxy


def namespace_counts():
    return {
        "addresses": Address.objects.filter(
            addressString__startswith=NAMESPACE
        ).count(),
        "places": Place.objects.filter(name__startswith=NAMESPACE).count(),
        "tags": Tag.objects.filter(name__startswith=NAMESPACE).count(),
        "categories": Category.objects.filter(name__startswith=NAMESPACE).count(),
    }


def cleanup_benchmark_namespace():
    """Remove only a verified benchmark namespace left by an older run."""
    namespaced_places = Place.objects.filter(name__startswith=NAMESPACE)
    namespaced_addresses = Address.objects.filter(
        addressString__startswith=NAMESPACE
    )
    namespaced_tags = Tag.objects.filter(name__startswith=NAMESPACE)
    namespaced_categories = Category.objects.filter(name__startswith=NAMESPACE)
    place_tags = Place.tags.through.objects

    unsafe_references = []
    if namespaced_places.exclude(
        address__addressString__startswith=NAMESPACE
    ).exists():
        unsafe_references.append("a namespaced place uses a non-benchmark address")
    if Place.objects.filter(address__in=namespaced_addresses).exclude(
        name__startswith=NAMESPACE
    ).exists():
        unsafe_references.append("a non-benchmark place uses a namespaced address")
    if namespaced_places.exclude(
        category__name__startswith=NAMESPACE
    ).exists():
        unsafe_references.append("a namespaced place uses a non-benchmark category")
    if Place.objects.filter(category__in=namespaced_categories).exclude(
        name__startswith=NAMESPACE
    ).exists():
        unsafe_references.append("a non-benchmark place uses a namespaced category")
    if Request.objects.filter(address__in=namespaced_addresses).exists():
        unsafe_references.append("a request uses a namespaced address")
    if Request.objects.filter(category__in=namespaced_categories).exists():
        unsafe_references.append("a request uses a namespaced category")
    if place_tags.filter(tag__in=namespaced_tags).exclude(
        place__in=namespaced_places
    ).exists():
        unsafe_references.append("a non-benchmark place uses a namespaced tag")
    if place_tags.filter(place__in=namespaced_places).exclude(
        tag__in=namespaced_tags
    ).exists():
        unsafe_references.append("a namespaced place uses a non-benchmark tag")
    if RequestTag.objects.filter(tag__in=namespaced_tags).exists():
        unsafe_references.append("a request uses a namespaced tag")
    if Image.objects.filter(place__in=namespaced_places).exists():
        unsafe_references.append("an image uses a namespaced place")
    if unsafe_references:
        raise RuntimeError("; ".join(unsafe_references))

    before = namespace_counts()
    with transaction.atomic():
        namespaced_places.delete()
        namespaced_addresses.delete()
        namespaced_tags.delete()
        namespaced_categories.delete()
    return before


def seed_benchmark_dataset():
    category = Category.objects.create(
        slug=f"{NAMESPACE}category",
        name=BENCHMARK_CATEGORY_NAME,
        description="Reserved category for the issue #79 viewport benchmark.",
    )
    tags = []
    for index in range(TAG_COUNT):
        normalized = normalize_tag_text(f"{NAMESPACE}tag_{index}")
        tags.append(
            Tag(
                name=normalized.display,
                canonical=normalized.canonical,
                is_public=True,
            )
        )
    Tag.objects.bulk_create(tags)

    addresses = []
    for index, longitude, latitude in benchmark_coordinates():
        addresses.append(
            Address(
                addressString=f"{NAMESPACE}address_{index:05d}",
                location=Point(longitude, latitude, srid=4326),
            )
        )
    Address.objects.bulk_create(addresses, batch_size=BULK_BATCH_SIZE)

    places = [
        Place(
            name=f"{NAMESPACE}place_{index:05d}",
            category=category,
            description=(
                "Deterministic synthetic populated-city benchmark place "
                f"{index:05d}."
            ),
            address=addresses[index],
            website=f"https://benchmark.invalid/places/{index:05d}",
        )
        for index in range(TOTAL_PLACES)
    ]
    Place.objects.bulk_create(places, batch_size=BULK_BATCH_SIZE)

    through_model = Place.tags.through
    through_model.objects.bulk_create(
        [
            through_model(
                place_id=place.pk,
                tag_id=tags[index % TAG_COUNT].pk,
            )
            for index, place in enumerate(places)
        ],
        batch_size=BULK_BATCH_SIZE,
    )
    return category


def analyze_benchmark_tables():
    table_names = (
        Address._meta.db_table,
        Place._meta.db_table,
        Place.tags.through._meta.db_table,
    )
    with connection.cursor() as cursor:
        for table_name in table_names:
            cursor.execute(f"ANALYZE {connection.ops.quote_name(table_name)}")


def planner_settings():
    settings = {}
    with connection.cursor() as cursor:
        for setting_name in (
            "enable_seqscan",
            "enable_indexscan",
            "enable_bitmapscan",
        ):
            cursor.execute(f"SHOW {setting_name}")
            settings[setting_name] = cursor.fetchone()[0]
    return settings


def address_location_gist_indexes():
    sql = """
        SELECT DISTINCT index_class.relname
        FROM pg_index AS index_definition
        JOIN pg_class AS table_class
          ON table_class.oid = index_definition.indrelid
        JOIN pg_class AS index_class
          ON index_class.oid = index_definition.indexrelid
        JOIN pg_am AS access_method
          ON access_method.oid = index_class.relam
        JOIN pg_attribute AS attribute
          ON attribute.attrelid = table_class.oid
         AND attribute.attnum = ANY(index_definition.indkey)
        WHERE table_class.oid = %s::regclass
          AND access_method.amname = 'gist'
          AND attribute.attname = 'location'
          AND index_definition.indisvalid
        ORDER BY index_class.relname
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, [Address._meta.db_table])
        return [row[0] for row in cursor.fetchall()]


def plan_nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from plan_nodes(child)


def capture_natural_plan(viewport, category_id):
    settings = planner_settings()
    gist_indexes = address_location_gist_indexes()
    queryset = viewport_places_queryset(
        viewport,
        category_ids=(category_id,),
    )[: VIEWPORT_RESULT_LIMIT + 1]
    raw_plan = json.loads(
        queryset.explain(format="json", analyze=True, buffers=True)
    )[0]
    nodes = list(plan_nodes(raw_plan["Plan"]))
    used_indexes = sorted(
        {node["Index Name"] for node in nodes if node.get("Index Name")}
    )
    used_gist_indexes = sorted(set(used_indexes).intersection(gist_indexes))
    return {
        "explain": "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)",
        "planner_settings": settings,
        "settings_overridden_by_harness": [],
        "address_location_gist_indexes": gist_indexes,
        "used_indexes": used_indexes,
        "used_address_location_gist_indexes": used_gist_indexes,
        "gist_index_used": bool(used_gist_indexes),
        "planning_time_ms": raw_plan.get("Planning Time"),
        "execution_time_ms": raw_plan.get("Execution Time"),
        "raw": raw_plan,
    }


def measure_endpoint(client, category_id):
    params = {
        "bbox": ",".join(str(value) for value in VIEWPORT_BBOX),
        "zoom": str(VIEWPORT_ZOOM),
        "categories": str(category_id),
    }
    started = perf_counter_ns()
    with CaptureQueriesContext(connection) as queries:
        response = client.get(ENDPOINT, params)
    elapsed_ms = (perf_counter_ns() - started) / 1_000_000
    encoded_body = bytes(response.content)
    try:
        payload = response.json()
    except ValueError:
        payload = None
    features = payload.get("features") if isinstance(payload, dict) else None
    benchmark_features = (
        [
            feature
            for feature in features
            if isinstance(feature, dict)
            and isinstance(feature.get("properties"), dict)
            and str(feature["properties"].get("name", "")).startswith(NAMESPACE)
        ]
        if isinstance(features, list)
        else None
    )
    return {
        "status_code": response.status_code,
        "content_type": response.get("Content-Type", "").split(";", 1)[0],
        "geojson_type": payload.get("type") if isinstance(payload, dict) else None,
        "query_count": len(queries),
        "feature_count": len(features) if isinstance(features, list) else None,
        "benchmark_feature_count": (
            len(benchmark_features) if benchmark_features is not None else None
        ),
        "non_benchmark_feature_count": (
            len(features) - len(benchmark_features)
            if isinstance(features, list) and benchmark_features is not None
            else None
        ),
        "encoded_geojson_bytes": len(encoded_body),
        "server_processing_ms": round(elapsed_ms, 3),
    }


def benchmark_failures(report):
    failures = []
    dataset = report["dataset"]
    if dataset["actual_total_places"] != TOTAL_PLACES:
        failures.append(
            f"dataset contains {dataset['actual_total_places']} places, "
            f"expected {TOTAL_PLACES}"
        )
    if dataset["actual_viewport_matches"] != VIEWPORT_MATCHES:
        failures.append(
            "viewport contains "
            f"{dataset['actual_viewport_matches']} places, expected {VIEWPORT_MATCHES}"
        )
    if dataset["synthetic_viewport_matches"] != VIEWPORT_MATCHES:
        failures.append(
            "synthetic grid contains "
            f"{dataset['synthetic_viewport_matches']} viewport places, "
            f"expected {VIEWPORT_MATCHES}"
        )

    plan = report["plan"]
    if plan["planner_settings"].get("enable_seqscan") != "on":
        failures.append("enable_seqscan must remain on for the natural plan")
    if plan["settings_overridden_by_harness"]:
        failures.append("the harness must not override PostgreSQL planner settings")
    if not plan["gist_index_used"]:
        failures.append("the natural analyzed plan did not use Address.location GiST")

    if (
        report["warmup_count"] != WARMUP_RUNS
        or len(report["warmups"]) != WARMUP_RUNS
    ):
        failures.append(f"the harness must execute exactly {WARMUP_RUNS} warmups")
    if (
        report["sample_count"] != SAMPLE_RUNS
        or len(report["samples"]) != SAMPLE_RUNS
    ):
        failures.append(f"the harness must execute exactly {SAMPLE_RUNS} samples")

    for warmup in report["warmups"]:
        if (
            warmup["status_code"] != 200
            or warmup["geojson_type"] != "FeatureCollection"
            or warmup["feature_count"] != VIEWPORT_MATCHES
            or warmup["benchmark_feature_count"] != VIEWPORT_MATCHES
            or warmup["non_benchmark_feature_count"] != 0
        ):
            failures.append(
                f"warmup {warmup['warmup']} did not return the 400-feature viewport"
            )

    budgets = report["budgets"]
    for sample in report["samples"]:
        sample_number = sample["sample"]
        if sample["status_code"] != 200:
            failures.append(
                f"sample {sample_number} returned HTTP {sample['status_code']}"
            )
        if sample["geojson_type"] != "FeatureCollection":
            failures.append(f"sample {sample_number} did not return GeoJSON")
        if sample["query_count"] > budgets["max_queries"]:
            failures.append(
                f"sample {sample_number} used {sample['query_count']} queries"
            )
        if sample["feature_count"] != VIEWPORT_MATCHES:
            failures.append(
                f"sample {sample_number} returned {sample['feature_count']} features"
            )
        if sample["benchmark_feature_count"] != VIEWPORT_MATCHES:
            failures.append(
                f"sample {sample_number} returned "
                f"{sample['benchmark_feature_count']} benchmark features"
            )
        if sample["non_benchmark_feature_count"] != 0:
            failures.append(
                f"sample {sample_number} returned "
                f"{sample['non_benchmark_feature_count']} non-benchmark features"
            )
        if (
            sample["feature_count"] is not None
            and sample["feature_count"] > budgets["max_features"]
        ):
            failures.append(
                f"sample {sample_number} exceeded the feature budget"
            )
        if sample["encoded_geojson_bytes"] > budgets["max_encoded_geojson_bytes"]:
            failures.append(f"sample {sample_number} exceeded the byte budget")
        if sample["server_processing_ms"] > budgets["max_server_processing_ms"]:
            failures.append(f"sample {sample_number} exceeded the timing budget")
    return failures


def build_report(stale_cleanup_counts, category_id):
    viewport = Polygon.from_bbox(VIEWPORT_BBOX)
    actual_synthetic_total = Place.objects.filter(
        name__startswith=NAMESPACE
    ).count()
    synthetic_matches = Place.objects.filter(
        name__startswith=NAMESPACE,
        address__location__coveredby=viewport,
    ).count()
    actual_matches = viewport_places_queryset(
        viewport,
        category_ids=(category_id,),
    ).count()
    database_viewport_places = Place.objects.filter(
        address__location__coveredby=viewport
    ).count()
    plan = capture_natural_plan(viewport, category_id)
    client = Client(HTTP_HOST="localhost")
    warmups = [
        {"warmup": number, **measure_endpoint(client, category_id)}
        for number in range(1, WARMUP_RUNS + 1)
    ]
    samples = [
        {"sample": number, **measure_endpoint(client, category_id)}
        for number in range(1, SAMPLE_RUNS + 1)
    ]
    report = {
        "schema": REPORT_SCHEMA,
        "result": "pending",
        "endpoint": {
            "method": "GET",
            "path": ENDPOINT,
            "query": {
                "bbox": ",".join(str(value) for value in VIEWPORT_BBOX),
                "zoom": str(VIEWPORT_ZOOM),
                "categories": str(category_id),
            },
        },
        "dataset": {
            "name": "deterministic Washington, DC populated-city grid",
            "namespace": NAMESPACE,
            "generator": "row-major 200x100 grid, v1",
            "city_bounds": [-77.200, 38.750, -76.800, 38.950],
            "grid_columns": GRID_COLUMNS,
            "grid_rows": GRID_ROWS,
            "expected_total_places": TOTAL_PLACES,
            "actual_total_places": actual_synthetic_total,
            "database_total_places_during_run": Place.objects.count(),
            "database_viewport_places_during_run": database_viewport_places,
            "viewport_bbox": list(VIEWPORT_BBOX),
            "expected_viewport_matches": VIEWPORT_MATCHES,
            "actual_viewport_matches": actual_matches,
            "synthetic_viewport_matches": synthetic_matches,
            "category": BENCHMARK_CATEGORY_NAME,
            "category_id": category_id,
            "synthetic_tag_count": TAG_COUNT,
            "stale_namespace_rows_removed_before_run": stale_cleanup_counts,
        },
        "budgets": {
            "max_queries": 2,
            "max_features": VIEWPORT_RESULT_LIMIT,
            "max_encoded_geojson_bytes": VIEWPORT_RESPONSE_LIMIT_BYTES,
            "max_server_processing_ms": SERVER_PROCESSING_LIMIT_MS,
        },
        "warmup_count": WARMUP_RUNS,
        "sample_count": SAMPLE_RUNS,
        "warmups": warmups,
        "samples": samples,
        "plan": plan,
        "environment": {
            "django": django.get_version(),
            "postgresql": connection.pg_version,
        },
    }
    failures = benchmark_failures(report)
    report["result"] = "pass" if not failures else "fail"
    report["failures"] = failures
    return report


def render_report_json(report):
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def render_plan_tree(node, depth=0):
    label = node.get("Node Type", "Unknown")
    if node.get("Index Name"):
        label += f" using {node['Index Name']}"
    actual_rows = node.get("Actual Rows")
    actual_time = node.get("Actual Total Time")
    details = []
    if actual_rows is not None:
        details.append(f"actual_rows={actual_rows}")
    if actual_time is not None:
        details.append(f"actual_total_ms={actual_time}")
    suffix = f" ({', '.join(details)})" if details else ""
    lines = [f"{'  ' * depth}{label}{suffix}"]
    for child in node.get("Plans", []):
        lines.extend(render_plan_tree(child, depth + 1))
    return lines


def render_report_text(report):
    dataset = report["dataset"]
    plan = report["plan"]
    budgets = report["budgets"]
    lines = [
        f"Schema: {report['schema']}",
        f"Result: {report['result']}",
        (
            f"Endpoint: GET {ENDPOINT}?"
            f"bbox={report['endpoint']['query']['bbox']}&zoom={VIEWPORT_ZOOM}"
            f"&categories={report['endpoint']['query']['categories']}"
        ),
        f"Dataset: {dataset['name']}",
        f"Namespace: {dataset['namespace']}",
        (
            "Places: "
            f"{dataset['actual_total_places']} total; "
            f"{dataset['actual_viewport_matches']} viewport matches"
        ),
        (
            "Database during run: "
            f"{dataset['database_total_places_during_run']} total places; "
            f"{dataset['database_viewport_places_during_run']} unfiltered "
            "viewport places"
        ),
        (
            "Budgets: "
            f"queries<={budgets['max_queries']}; "
            f"features<={budgets['max_features']}; "
            f"encoded_geojson_bytes<={budgets['max_encoded_geojson_bytes']}; "
            f"server_processing_ms<={budgets['max_server_processing_ms']}"
        ),
        f"Warmups: {report['warmup_count']}",
        f"Samples: {report['sample_count']}",
    ]
    for warmup in report["warmups"]:
        lines.append(
            "Warmup {warmup}: status={status_code}; queries={query_count}; "
            "features={feature_count}; benchmark_features={benchmark_feature_count}; "
            "non_benchmark_features={non_benchmark_feature_count}; "
            "encoded_geojson_bytes={encoded_geojson_bytes}; "
            "server_processing_ms={server_processing_ms:.3f}".format(**warmup)
        )
    for sample in report["samples"]:
        lines.append(
            "Sample {sample}: status={status_code}; queries={query_count}; "
            "features={feature_count}; benchmark_features={benchmark_feature_count}; "
            "non_benchmark_features={non_benchmark_feature_count}; "
            "encoded_geojson_bytes={encoded_geojson_bytes}; "
            "server_processing_ms={server_processing_ms:.3f}".format(**sample)
        )
    lines.extend(
        [
            f"Planner settings: {json.dumps(plan['planner_settings'], sort_keys=True)}",
            "Planner settings overridden by harness: none",
            "Address.location GiST indexes: "
            + ", ".join(plan["address_location_gist_indexes"]),
            "Used Address.location GiST indexes: "
            + ", ".join(plan["used_address_location_gist_indexes"]),
            f"Planning time ms: {plan['planning_time_ms']}",
            f"Execution time ms: {plan['execution_time_ms']}",
            f"Plan: {plan['explain']}",
        ]
    )
    lines.extend(render_plan_tree(plan["raw"]["Plan"]))
    if report["failures"]:
        lines.append("Failures:")
        lines.extend(f"- {failure}" for failure in report["failures"])
    return "\n".join(lines) + "\n"


def write_evidence(report, output_directory):
    output_path = Path(output_directory)
    output_path.mkdir(parents=True, exist_ok=True)
    text_path = output_path / EVIDENCE_TEXT_FILENAME
    json_path = output_path / EVIDENCE_JSON_FILENAME
    text_path.write_text(render_report_text(report), encoding="utf-8")
    json_path.write_text(render_report_json(report), encoding="utf-8")
    return text_path, json_path
