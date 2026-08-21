import logging
import os
from contextlib import contextmanager

from django.conf import settings
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from backend.models import CustomUser


LOCAL_TEST_PASSWORD_ENV = "SMOKEMAP_LOCAL_TEST_PASSWORD"
LOCAL_TEST_PASSWORD_FALLBACK = "Smokemap-local-test-only-2026!"
LOCAL_TEST_USERS = (
    ("admin@smokemap.local", "Local Administrator", True),
    ("user-one@smokemap.local", "Local User One", False),
    ("user-two@smokemap.local", "Local User Two", False),
)


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
    help = "Create or update the development-only local test user cohort."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "Local test users can only be provisioned when DEBUG is enabled."
            )

        password = os.environ.get(
            LOCAL_TEST_PASSWORD_ENV, LOCAL_TEST_PASSWORD_FALLBACK
        )
        users = []
        for email, name, is_administrator in LOCAL_TEST_USERS:
            user = CustomUser.objects.filter(email=email).first()
            if user is None:
                user = CustomUser(email=email)
            user.name = name
            user.is_active = True
            user.is_staff = is_administrator
            user.is_superuser = is_administrator
            try:
                validate_password(password, user=user)
            except ValidationError as error:
                raise CommandError(" ".join(error.messages)) from error
            user.set_password(password)
            users.append((user, is_administrator))

        with suppress_database_query_logging(), transaction.atomic():
            admins, _ = Group.objects.get_or_create(name="admins")
            for user, is_administrator in users:
                user.save()
                if is_administrator:
                    user.groups.add(admins)
                else:
                    user.groups.remove(admins)

        self.stdout.write(
            self.style.SUCCESS("Provisioned one local administrator and two users.")
        )
