import hashlib
import json
from pathlib import Path

from django.contrib.gis.geos import Point
from django.db import connection, transaction

from backend.models import Address, Category, Place
from backend.place_search import explain_place_search, plan_nodes


REPORT_SCHEMA = "smokemap.place-search-plan.v1"
NAMESPACE = "__sm54_search_plan_v1__"
TOTAL_PLACES = 20_000
PREFIX_QUERY = "cedar corner 000"
FUZZY_QUERY = "wilow smokehous"
PREFIX_INDEX = "place_name_lower_prefix_idx"
TRIGRAM_INDEX = "place_name_lower_trgm_idx"


def benchmark_place_name(index):
    if index < 100:
        return f"Cedar Corner {index:05d}"
    if index == 100:
        return "Willow Smokehouse"
    token = hashlib.sha256(str(index).encode("ascii")).hexdigest()[:16]
    return f"Venue {token} {index:05d}"


def search_indexes():
    sql = """
        SELECT index_class.relname, pg_get_indexdef(index_class.oid)
        FROM pg_index AS index_definition
        JOIN pg_class AS table_class
          ON table_class.oid = index_definition.indrelid
        JOIN pg_class AS index_class
          ON index_class.oid = index_definition.indexrelid
        WHERE table_class.oid = %s::regclass
          AND index_class.relname IN (%s, %s)
          AND index_definition.indisvalid
        ORDER BY index_class.relname
    """
    with connection.cursor() as cursor:
        cursor.execute(
            sql,
            [Place._meta.db_table, PREFIX_INDEX, TRIGRAM_INDEX],
        )
        return {name: definition for name, definition in cursor.fetchall()}


def planner_settings():
    with connection.cursor() as cursor:
        cursor.execute("SHOW enable_seqscan")
        return {"enable_seqscan": cursor.fetchone()[0]}


def seed_plan_dataset():
    if (
        Category.objects.filter(slug="sm54-search-plan-v1").exists()
        or Address.objects.filter(addressString__startswith=NAMESPACE).exists()
    ):
        raise RuntimeError("The reserved search-plan namespace is already in use.")

    category = Category.objects.create(
        slug="sm54-search-plan-v1",
        name=NAMESPACE,
        description="Transactional issue #54 search-plan fixture.",
    )
    addresses = [
        Address(
            addressString=f"{NAMESPACE}{index:05d}",
            location=Point(-77.0, 39.0, srid=4326),
        )
        for index in range(TOTAL_PLACES)
    ]
    Address.objects.bulk_create(addresses, batch_size=2_000)
    Place.objects.bulk_create(
        [
            Place(
                name=benchmark_place_name(index),
                category=category,
                description="Representative place-search plan fixture.",
                address=addresses[index],
            )
            for index in range(TOTAL_PLACES)
        ],
        batch_size=2_000,
    )


def analyze_search_table():
    with connection.cursor() as cursor:
        cursor.execute(
            f"ANALYZE {connection.ops.quote_name(Place._meta.db_table)}"
        )


def used_indexes(plan):
    return sorted(
        {
            node["Index Name"]
            for node in plan_nodes(plan["Plan"])
            if node.get("Index Name")
        }
    )


def inspect_representative_plans():
    indexes = search_indexes()
    settings = planner_settings()
    plans = {
        "prefix": explain_place_search(PREFIX_QUERY),
        "fuzzy": explain_place_search(FUZZY_QUERY),
    }
    return {
        "schema": REPORT_SCHEMA,
        "dataset": {
            "description": "deterministic 20,000-place autocomplete corpus",
            "places": TOTAL_PLACES,
        },
        "planner_settings": settings,
        "settings_overridden_by_harness": [],
        "indexes": indexes,
        "queries": {
            name: {
                "query": PREFIX_QUERY if name == "prefix" else FUZZY_QUERY,
                "used_indexes": used_indexes(plan),
                "planning_time_ms": plan.get("Planning Time"),
                "execution_time_ms": plan.get("Execution Time"),
                "plan": plan,
            }
            for name, plan in plans.items()
        },
    }


def report_failures(report):
    failures = []
    if report["planner_settings"].get("enable_seqscan") != "on":
        failures.append("enable_seqscan must remain on for natural plans")
    for index_name in (PREFIX_INDEX, TRIGRAM_INDEX):
        if index_name not in report["indexes"]:
            failures.append(f"missing valid search index {index_name}")
    if not set(report["queries"]["prefix"]["used_indexes"]).intersection(
        {PREFIX_INDEX, TRIGRAM_INDEX}
    ):
        failures.append("prefix plan did not use a place-search index")
    if TRIGRAM_INDEX not in report["queries"]["fuzzy"]["used_indexes"]:
        failures.append("fuzzy plan did not use the trigram index")
    return failures


def run_plan_inspection():
    report = None
    try:
        with transaction.atomic():
            seed_plan_dataset()
            analyze_search_table()
            report = inspect_representative_plans()
            transaction.set_rollback(True)
    finally:
        # Restore estimates after the transactional fixture has disappeared.
        analyze_search_table()
    report["failures"] = report_failures(report)
    report["result"] = "pass" if not report["failures"] else "fail"
    return report


def render_report_text(report):
    lines = [
        f"Schema: {report['schema']}",
        f"Result: {report['result']}",
        f"Places: {report['dataset']['places']}",
        f"enable_seqscan: {report['planner_settings']['enable_seqscan']}",
    ]
    for name in ("prefix", "fuzzy"):
        query = report["queries"][name]
        lines.append(
            f"{name.title()}: query={query['query']!r}; "
            f"indexes={','.join(query['used_indexes']) or 'none'}; "
            f"planning_ms={query['planning_time_ms']}; "
            f"execution_ms={query['execution_time_ms']}"
        )
    lines.extend(f"Failure: {failure}" for failure in report["failures"])
    return "\n".join(lines) + "\n"


def write_report(report, output_dir):
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    (output_path / "place-search-plan.txt").write_text(
        render_report_text(report), encoding="utf-8"
    )
    (output_path / "place-search-plan.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
