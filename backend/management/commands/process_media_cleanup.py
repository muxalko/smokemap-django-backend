from django.core.management.base import BaseCommand, CommandError

from backend.media import process_media_cleanup
from backend.submission_expiry import process_submission_expiry


class Command(BaseCommand):
    help = "Expire inactive drafts and due media intents, then process exact-key cleanup."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=100)

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        try:
            expiry_counts = process_submission_expiry(batch_size=batch_size)
            counts = process_media_cleanup(batch_size=batch_size)
        except ValueError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(
            "media cleanup processed: "
            f"expired={counts.expired} claimed={counts.claimed} "
            f"deleted={counts.deleted} upload_claimed={counts.upload_claimed} "
            f"upload_deleted={counts.upload_deleted} redacted={counts.redacted} "
            f"failed={counts.failed} skipped={counts.skipped} "
            f"draft_expired={expiry_counts.expired}"
        )
