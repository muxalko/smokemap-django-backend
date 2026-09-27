from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from backend.place_search_plan import render_report_text, run_plan_inspection, write_report


class Command(BaseCommand):
    help = "Inspect natural PostgreSQL plans for bounded public place search."

    def add_arguments(self, parser):
        parser.add_argument("--output-dir")

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Search-plan inspection requires a DEBUG-enabled environment.")

        report = run_plan_inspection()
        self.stdout.write(render_report_text(report), ending="")
        if options["output_dir"]:
            write_report(report, options["output_dir"])
        if report["failures"]:
            raise CommandError("Representative search plans failed their index contract.")
