from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from .models import Address, Category, Location, Place


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
