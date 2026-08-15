import logging
from contextlib import contextmanager
from getpass import getpass

from django.conf import settings
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import transaction

from backend.models import CustomUser


@contextmanager
def suppress_database_query_logging():
    database_logger = logging.getLogger("django.db.backends")
    previous_level = database_logger.level
    database_logger.setLevel(logging.CRITICAL + 1)
    try:
        yield
    finally:
        database_logger.setLevel(previous_level)


class Command(BaseCommand):
    help = "Create or update a development-only administrator account."

    def add_arguments(self, parser):
        parser.add_argument(
            "--email",
            default="admin@smokemap.local",
            help="Email address for the local administrator.",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "Local administrators can only be provisioned when DEBUG is enabled."
            )

        email = options["email"].strip().lower()
        try:
            validate_email(email)
        except ValidationError as error:
            raise CommandError("Enter a valid local administrator email address.") from error

        password = getpass("Password: ")
        confirmation = getpass("Password (again): ")
        if password != confirmation:
            raise CommandError("Passwords do not match; no account was changed.")

        user = CustomUser.objects.filter(email=email).first()
        created = user is None
        if user is None:
            user = CustomUser(email=email, name="Local Administrator")

        try:
            validate_password(password, user=user)
        except ValidationError as error:
            raise CommandError(" ".join(error.messages)) from error

        user.is_active = True
        user.is_staff = True
        user.is_superuser = True
        user.set_password(password)

        with suppress_database_query_logging(), transaction.atomic():
            user.save()
            admins, _ = Group.objects.get_or_create(name="admins")
            user.groups.add(admins)

        action = "Created" if created else "Updated"
        self.stdout.write(
            self.style.SUCCESS(f"{action} local administrator {email}.")
        )
