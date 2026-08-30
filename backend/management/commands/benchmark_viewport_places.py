from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from backend.viewport_benchmark import (
    analyze_benchmark_tables,
    build_report,
    cleanup_benchmark_namespace,
    namespace_counts,
    render_report_text,
    seed_benchmark_dataset,
    write_evidence,
)


class Command(BaseCommand):
    help = (
        "Benchmark the real viewport place endpoint against the deterministic "
        "20,000-place issue #79 dataset."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--output-dir",
            help=(
                "Write canonical viewport-places-benchmark.txt and .json "
                "evidence files to this directory."
            ),
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "Viewport benchmark can only run in a DEBUG-enabled local or "
                "CI environment."
            )

        try:
            stale_cleanup_counts = cleanup_benchmark_namespace()
        except RuntimeError as error:
            raise CommandError(
                f"Refusing unsafe benchmark namespace cleanup: {error}"
            ) from error

        report = None
        try:
            with transaction.atomic():
                category = seed_benchmark_dataset()
                analyze_benchmark_tables()
                report = build_report(stale_cleanup_counts, category.pk)
                transaction.set_rollback(True)
        finally:
            # ANALYZE statistics are not transactional. Refresh them after the
            # synthetic transaction rolls back so an interrupted benchmark does
            # not leave production-like tables with synthetic row estimates.
            analyze_benchmark_tables()

        remaining = namespace_counts()
        if any(remaining.values()):
            raise CommandError(
                "Benchmark cleanup failed; remaining namespace rows: "
                f"{remaining}"
            )
        report["dataset"]["namespace_rows_after_run"] = remaining

        text_evidence = render_report_text(report)
        self.stdout.write(text_evidence, ending="")

        if options["output_dir"]:
            text_path, json_path = write_evidence(report, options["output_dir"])
            self.stdout.write(f"Text evidence: {text_path}")
            self.stdout.write(f"JSON evidence: {json_path}")

        if report["failures"]:
            raise CommandError(
                "Viewport benchmark failed: " + "; ".join(report["failures"])
            )

        self.stdout.write(self.style.SUCCESS("Viewport benchmark passed."))
