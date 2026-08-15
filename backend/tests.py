from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import CommandError
from django.core.management import call_command
from django.core.management.utils import get_random_string
from django.test import SimpleTestCase, TestCase, override_settings

from .models import Address, Category, Location, Place


class LocalLoggingTests(SimpleTestCase):
    def test_local_logging_uses_console_handlers(self):
        for handler in settings.LOGGING["handlers"].values():
            self.assertNotIn("filename", handler)

        for logger_name in (
            "django",
            "django.request",
            "django.db.backends",
            "backend.schema",
            "backend.stats",
        ):
            self.assertEqual(
                settings.LOGGING["loggers"][logger_name]["handlers"],
                ["console"],
            )


class CategoryProvisioningTests(TestCase):
    def test_provisional_categories_are_available(self):
        self.assertSetEqual(
            set(Category.objects.values_list("name", flat=True)),
            {
                "Indoors",
                "Outdoors",
                "Rooftop",
                "Underground",
                "On the water",
                "Underwater",
                "In the air",
                "Other",
            },
        )


class MockDataCommandTests(TestCase):
    @override_settings(DEBUG=True)
    def test_seed_mock_data_is_idempotent_and_visible_as_geojson(self):
        call_command("seed_mock_data")
        first_place_ids = set(Place.objects.values_list("id", flat=True))

        call_command("seed_mock_data")

        self.assertEqual(Place.objects.count(), 3)
        self.assertEqual(Address.objects.count(), 3)
        self.assertEqual(Location.objects.count(), 3)
        self.assertSetEqual(
            set(Place.objects.values_list("id", flat=True)),
            first_place_ids,
        )

        response = self.client.get(
            "/locations/",
            {"in_bbox": "-78,38,-76,40"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["type"], "FeatureCollection")
        self.assertEqual(len(payload["features"]), 3)
        self.assertSetEqual(
            {feature["properties"]["name"] for feature in payload["features"]},
            {
                "Mock Capitol Patio",
                "Mock Dupont Lounge",
                "Mock Georgetown Rooftop",
            },
        )


class LocalAdminCommandTests(TestCase):
    @override_settings(DEBUG=True)
    @patch("backend.management.commands.create_local_admin.getpass")
    def test_create_local_admin_is_repeatable(self, mock_getpass):
        first_password = f"{get_random_string(40)}A9!"
        second_password = f"{get_random_string(40)}B8!"
        mock_getpass.side_effect = [
            first_password,
            first_password,
            second_password,
            second_password,
        ]
        email = "local-admin@smokemap.test"

        call_command("create_local_admin", email=email, stdout=StringIO())
        call_command("create_local_admin", email=email, stdout=StringIO())

        User = get_user_model()
        self.assertEqual(User.objects.filter(email=email).count(), 1)
        user = User.objects.get(email=email)
        self.assertTrue(user.is_active)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.check_password(second_password))
        self.assertTrue(user.groups.filter(name="admins").exists())
        self.assertEqual(Group.objects.filter(name="admins").count(), 1)

    @override_settings(DEBUG=True)
    @patch("backend.management.commands.create_local_admin.getpass")
    def test_create_local_admin_rejects_mismatched_passwords(self, mock_getpass):
        password = f"{get_random_string(40)}A9!"
        mock_getpass.side_effect = [password, f"{password}different"]

        with self.assertRaisesMessage(CommandError, "Passwords do not match"):
            call_command(
                "create_local_admin",
                email="local-admin@smokemap.test",
                stdout=StringIO(),
            )

        User = get_user_model()
        self.assertFalse(User.objects.filter(email="local-admin@smokemap.test").exists())

    @override_settings(DEBUG=False)
    @patch("backend.management.commands.create_local_admin.getpass")
    def test_create_local_admin_refuses_non_debug_mode(self, mock_getpass):
        with self.assertRaisesMessage(CommandError, "DEBUG is enabled"):
            call_command(
                "create_local_admin",
                email="local-admin@smokemap.test",
                stdout=StringIO(),
            )

        mock_getpass.assert_not_called()


class UsersManagersTests(TestCase):

    def test_create_user(self):
        User = get_user_model()
        user = User.objects.create_user(email="normal@user.com", password="foo")
        self.assertEqual(user.email, "normal@user.com")
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        try:
            # username is None for the AbstractUser option
            # username does not exist for the AbstractBaseUser option
            self.assertIsNone(user.username)
        except AttributeError:
            pass
        with self.assertRaises(TypeError):
            User.objects.create_user()
        with self.assertRaises(TypeError):
            User.objects.create_user(email="")
        with self.assertRaises(ValueError):
            User.objects.create_user(email="", password="foo")

    def test_create_superuser(self):
        User = get_user_model()
        admin_user = User.objects.create_superuser(email="super@user.com", password="foo")
        self.assertEqual(admin_user.email, "super@user.com")
        self.assertTrue(admin_user.is_active)
        self.assertTrue(admin_user.is_staff)
        self.assertTrue(admin_user.is_superuser)
        try:
            # username is None for the AbstractUser option
            # username does not exist for the AbstractBaseUser option
            self.assertIsNone(admin_user.username)
        except AttributeError:
            pass
        with self.assertRaises(ValueError):
            User.objects.create_superuser(
                email="super@user.com", password="foo", is_superuser=False)
