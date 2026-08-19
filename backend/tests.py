from io import StringIO
import json
from datetime import timedelta
from threading import Barrier, Thread
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import CommandError
from django.core.management import call_command
from django.core.management.utils import get_random_string
from django.db import close_old_connections
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from graphene.test import Client as GraphQLClient
from rest_framework.test import APIClient

from .models import (
    Address,
    Category,
    Location,
    ModerationAudit,
    Place,
    RefreshTokenCredential,
    Request,
)
from .permissions import role_for_user
from .schema import schema
from .tokens import (
    REFRESH_COOKIE_NAME,
    TokenLifecycleError,
    encode_access_token,
    issue_token_pair,
    rotate_refresh_token,
)


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


class AuthorizationMatrixTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.guest = SimpleNamespace(is_authenticated=False, is_active=False)
        self.inactive = User.objects.create_user(
            email="inactive@smokemap.test", password="test", is_active=False
        )
        self.user = User.objects.create_user(email="user@smokemap.test", password="test")
        self.other_user = User.objects.create_user(
            email="other@smokemap.test", password="test"
        )
        self.moderator = User.objects.create_user(
            email="moderator@smokemap.test", password="test", is_staff=True
        )
        self.administrator = User.objects.create_superuser(
            email="administrator@smokemap.test", password="test"
        )
        self.category = Category.objects.get(name="Outdoors")
        self.graphql = GraphQLClient(schema)

    def context(self, user):
        return SimpleNamespace(user=user, META={})

    def address(self, suffix):
        address = Address(
            addressString=f"Test address {suffix}",
            location=f"POINT ({suffix} {suffix})",
        )
        address.save(omit_geocode=True)
        return address

    def submission(self, owner, suffix):
        return Request.objects.create(
            name=f"Submission {suffix}",
            category=self.category,
            description="Pending submission",
            address=self.address(suffix),
            owner=owner,
        )

    def error_code(self, result):
        return result["errors"][0]["extensions"]["code"]

    def test_roles_are_normalized_from_current_backend_account_state(self):
        expected = (
            (self.guest, "guest"),
            (self.inactive, "guest"),
            (self.user, "user"),
            (self.moderator, "moderator"),
            (self.administrator, "administrator"),
        )

        for account, role in expected:
            with self.subTest(role=role):
                self.assertEqual(role_for_user(account), role)

    def test_submission_creation_requires_active_user_and_assigns_owner(self):
        mutation = """
            mutation Create($input: RequestInput!) {
              createRequest(input: $input) { request { id requestedBy } }
            }
        """
        variables = {
            "input": {
                "name": "Owner-bound submission",
                "category": str(self.category.pk),
                "description": "Description",
                "addressString": "[1,2]",
                "tags": [],
                "website": "https://example.test",
            }
        }

        for account in (self.guest, self.inactive):
            with self.subTest(account=account):
                result = self.graphql.execute(
                    mutation, variable_values=variables, context_value=self.context(account)
                )
                self.assertEqual(self.error_code(result), "UNAUTHENTICATED")

        result = self.graphql.execute(
            mutation, variable_values=variables, context_value=self.context(self.user)
        )
        request = Request.objects.get(pk=result["data"]["createRequest"]["request"]["id"])
        self.assertEqual(request.owner, self.user)
        self.assertIsNone(request.requested_by)

    def test_pending_queries_hide_other_users_rows(self):
        own = self.submission(self.user, 1)
        other = self.submission(self.other_user, 2)
        query = "query { requests { id } }"

        for account in (self.guest, self.inactive):
            result = self.graphql.execute(query, context_value=self.context(account))
            self.assertEqual(self.error_code(result), "UNAUTHENTICATED")

        result = self.graphql.execute(query, context_value=self.context(self.user))
        self.assertEqual(result["data"]["requests"], [{"id": str(own.pk)}])

        for account in (self.moderator, self.administrator):
            result = self.graphql.execute(query, context_value=self.context(account))
            self.assertSetEqual(
                {item["id"] for item in result["data"]["requests"]},
                {str(own.pk), str(other.pk)},
            )

    def test_only_moderators_approve_and_self_review_is_denied(self):
        submission = self.submission(self.user, 3)
        mutation = """
            mutation Approve($id: ID!) {
              approveRequest(id: $id, input: {approvedComment: "Reviewed"}) {
                request { id approved approvedBy }
              }
            }
        """

        for account, code in (
            (self.guest, "UNAUTHENTICATED"),
            (self.inactive, "UNAUTHENTICATED"),
            (self.user, "FORBIDDEN"),
        ):
            result = self.graphql.execute(
                mutation,
                variable_values={"id": str(submission.pk)},
                context_value=self.context(account),
            )
            self.assertEqual(self.error_code(result), code)

        own_submission = self.submission(self.moderator, 4)
        result = self.graphql.execute(
            mutation,
            variable_values={"id": str(own_submission.pk)},
            context_value=self.context(self.moderator),
        )
        self.assertEqual(self.error_code(result), "FORBIDDEN")
        self.assertTrue(
            ModerationAudit.objects.filter(
                target_id=own_submission.pk, outcome="denied_self_review"
            ).exists()
        )

        result = self.graphql.execute(
            mutation,
            variable_values={"id": str(submission.pk)},
            context_value=self.context(self.moderator),
        )
        self.assertNotIn("errors", result)
        submission.refresh_from_db()
        self.assertTrue(submission.approved)
        self.assertEqual(submission.reviewed_by, self.moderator)
        self.assertTrue(
            ModerationAudit.objects.filter(
                actor=self.moderator,
                action=ModerationAudit.Action.APPROVE,
                target_id=submission.pk,
                outcome="succeeded",
            ).exists()
        )

    def test_only_administrator_can_hard_delete_with_durable_audit(self):
        mutation = "mutation Delete($id: ID!) { deleteRequest(id: $id) { ok } }"

        for index, account, code in (
            (5, self.guest, "UNAUTHENTICATED"),
            (6, self.inactive, "UNAUTHENTICATED"),
            (7, self.user, "FORBIDDEN"),
            (8, self.moderator, "FORBIDDEN"),
        ):
            submission = self.submission(self.user, index)
            result = self.graphql.execute(
                mutation,
                variable_values={"id": str(submission.pk)},
                context_value=self.context(account),
            )
            self.assertEqual(self.error_code(result), code)
            self.assertTrue(Request.objects.filter(pk=submission.pk).exists())

        submission = self.submission(self.user, 9)
        target_id = submission.pk
        result = self.graphql.execute(
            mutation,
            variable_values={"id": str(target_id)},
            context_value=self.context(self.administrator),
        )
        self.assertEqual(result["data"]["deleteRequest"]["ok"], True)
        self.assertFalse(Request.objects.filter(pk=target_id).exists())
        self.assertTrue(
            ModerationAudit.objects.filter(
                actor=self.administrator,
                action=ModerationAudit.Action.HARD_DELETE,
                target_id=target_id,
            ).exists()
        )

    def test_upload_entry_points_fail_closed(self):
        presign = "query { s3PresignedUrl }"
        create_image = """
            mutation { createImage(input: {requestId: "1", name: "x", url: "x"}) {
              image { id }
            } }
        """
        for account in (
            self.guest,
            self.inactive,
            self.user,
            self.moderator,
            self.administrator,
        ):
            for operation in (presign, create_image):
                result = self.graphql.execute(
                    operation, context_value=self.context(account)
                )
                self.assertEqual(self.error_code(result), "FORBIDDEN")

    def test_approved_place_rest_writes_are_administrator_only(self):
        for index, account in enumerate(
            (None, self.inactive, self.user, self.moderator), start=10
        ):
            place = Place.objects.create(
                name=f"Place {index}",
                category=self.category,
                description="Approved",
                address=self.address(index),
            )
            client = APIClient()
            if account is not None:
                client.force_authenticate(account)
            response = client.delete(f"/places/{place.pk}/")
            self.assertIn(response.status_code, (401, 403))
            self.assertTrue(Place.objects.filter(pk=place.pk).exists())

        place = Place.objects.create(
            name="Administrator place",
            category=self.category,
            description="Approved",
            address=self.address(20),
        )
        client = APIClient()
        client.force_authenticate(self.administrator)
        self.assertEqual(client.delete(f"/places/{place.pk}/").status_code, 204)


class TokenLifecycleTests(TestCase):
    LOGIN_MUTATION = """
        mutation Login($email: String!, $password: String!) {
          tokenAuth(email: $email, password: $password) {
            payload
            token
            refreshToken
            refreshExpiresIn
            user { role }
          }
        }
    """
    REFRESH_MUTATION = """
        mutation Refresh($refreshToken: String) {
          refreshToken(refreshToken: $refreshToken) {
            payload
            token
            refreshToken
            refreshExpiresIn
          }
        }
    """
    REVOKE_MUTATION = """
        mutation Revoke($refreshToken: String) {
          revokeToken(refreshToken: $refreshToken) { revoked }
        }
    """

    def setUp(self):
        User = get_user_model()
        self.password = "correct horse battery staple"
        self.user = User.objects.create_user(
            email="token-user@smokemap.test", password=self.password
        )
        self.administrator = User.objects.create_superuser(
            email="token-admin@smokemap.test", password=self.password
        )
        self.graphql = GraphQLClient(schema)

    def context(self, cookies=None):
        return SimpleNamespace(
            user=SimpleNamespace(is_authenticated=False, is_active=False),
            META={},
            COOKIES=cookies or {},
        )

    def execute(self, operation, variables=None, cookies=None):
        return self.graphql.execute(
            operation,
            variable_values=variables,
            context_value=self.context(cookies),
        )

    def login(self, user=None):
        user = user or self.user
        result = self.execute(
            self.LOGIN_MUTATION,
            {"email": user.email, "password": self.password},
        )
        self.assertNotIn("errors", result)
        return result["data"]["tokenAuth"]

    def error_code(self, result):
        return result["errors"][0]["extensions"]["code"]

    def test_login_uses_minimal_five_minute_access_claims_and_hashed_refresh(self):
        token_pair = self.login()
        payload = token_pair["payload"]

        self.assertSetEqual(set(payload), {"sub", "type", "iat", "exp"})
        self.assertEqual(payload["sub"], str(self.user.pk))
        self.assertEqual(payload["type"], "access")
        self.assertEqual(payload["exp"] - payload["iat"], 300)
        self.assertNotIn(self.user.email, token_pair["token"])

        credential = RefreshTokenCredential.objects.get()
        self.assertNotEqual(credential.token_digest, token_pair["refreshToken"])
        self.assertNotIn(
            token_pair["refreshToken"],
            RefreshTokenCredential.objects.values_list("token_digest", flat=True),
        )
        self.assertAlmostEqual(
            token_pair["refreshExpiresIn"] - payload["iat"],
            7 * 24 * 60 * 60,
            delta=2,
        )

    def test_login_failure_is_generic_for_unknown_inactive_and_wrong_password(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        attempts = (
            {"email": "unknown@smokemap.test", "password": self.password},
            {"email": self.user.email, "password": self.password},
            {"email": self.administrator.email, "password": "wrong"},
        )
        for variables in attempts:
            with self.subTest(email=variables["email"]):
                result = self.execute(self.LOGIN_MUTATION, variables)
                self.assertEqual(self.error_code(result), "AUTHENTICATION_FAILED")
                self.assertEqual(result["errors"][0]["message"], "Invalid credentials")

    def test_refresh_rotates_once_without_extending_the_family_lifetime(self):
        original = self.login()
        refreshed = self.execute(
            self.REFRESH_MUTATION,
            {"refreshToken": original["refreshToken"]},
        )["data"]["refreshToken"]

        self.assertNotEqual(refreshed["refreshToken"], original["refreshToken"])
        self.assertEqual(
            refreshed["refreshExpiresIn"], original["refreshExpiresIn"]
        )
        predecessor = RefreshTokenCredential.objects.get(predecessor__isnull=True)
        self.assertIsNotNone(predecessor.used_at)
        self.assertIsNotNone(predecessor.revoked_at)
        self.assertIsNotNone(predecessor.successor)

    def test_rotated_token_reuse_revokes_the_entire_family(self):
        original = self.login()
        refreshed = self.execute(
            self.REFRESH_MUTATION,
            {"refreshToken": original["refreshToken"]},
        )["data"]["refreshToken"]

        reuse = self.execute(
            self.REFRESH_MUTATION,
            {"refreshToken": original["refreshToken"]},
        )
        self.assertEqual(self.error_code(reuse), "REFRESH_TOKEN_REUSED")

        family = RefreshTokenCredential.objects.first().family
        family.refresh_from_db()
        self.assertIsNotNone(family.revoked_at)
        self.assertIsNotNone(family.compromised_at)
        self.assertFalse(
            family.credentials.filter(revoked_at__isnull=True).exists()
        )

        successor = self.execute(
            self.REFRESH_MUTATION,
            {"refreshToken": refreshed["refreshToken"]},
        )
        self.assertEqual(self.error_code(successor), "INVALID_REFRESH_TOKEN")

    def test_refresh_cookie_is_supported_and_missing_token_is_terminal(self):
        original = self.login()
        refreshed = self.execute(
            "mutation { refreshToken { refreshToken } }",
            cookies={REFRESH_COOKIE_NAME: original["refreshToken"]},
        )
        self.assertNotIn("errors", refreshed)

        missing = self.execute("mutation { refreshToken { refreshToken } }")
        self.assertEqual(self.error_code(missing), "INVALID_REFRESH_TOKEN")

    def test_expired_refresh_token_revokes_its_family(self):
        original = self.login()
        family = RefreshTokenCredential.objects.get().family
        family.expires_at = timezone.now() - timedelta(seconds=1)
        family.save(update_fields=["expires_at"])

        result = self.execute(
            self.REFRESH_MUTATION,
            {"refreshToken": original["refreshToken"]},
        )
        self.assertEqual(self.error_code(result), "REFRESH_TOKEN_EXPIRED")
        family.refresh_from_db()
        self.assertIsNotNone(family.revoked_at)

    def test_revoke_terminates_the_family_and_is_accepted_from_cookie(self):
        original = self.login()
        revoked = self.execute(
            "mutation { revokeToken { revoked } }",
            cookies={REFRESH_COOKIE_NAME: original["refreshToken"]},
        )
        self.assertNotIn("errors", revoked)

        result = self.execute(
            self.REFRESH_MUTATION,
            {"refreshToken": original["refreshToken"]},
        )
        self.assertEqual(self.error_code(result), "INVALID_REFRESH_TOKEN")

    def test_access_token_authenticates_graphql_and_rest(self):
        user_pair = self.login()
        graphql_response = self.client.post(
            "/graphql/",
            data=json.dumps({"query": "query { requests { id } }"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {user_pair['token']}",
        )
        self.assertEqual(graphql_response.status_code, 200)
        self.assertEqual(graphql_response.json()["data"]["requests"], [])

        admin_pair = self.login(self.administrator)
        place = Place.objects.create(
            name="Bearer protected place",
            category=Category.objects.get(name="Outdoors"),
            description="Approved",
            address=self._address(),
        )
        response = self.client.delete(
            f"/places/{place.pk}/",
            HTTP_AUTHORIZATION=f"Bearer {admin_pair['token']}",
        )
        self.assertEqual(response.status_code, 204)

        invalid = self.client.delete(
            "/places/999999/",
            HTTP_AUTHORIZATION="Bearer malformed",
        )
        self.assertEqual(invalid.status_code, 401)

    def _address(self):
        address = Address(
            addressString="Bearer protected address",
            location="POINT (1 1)",
        )
        address.save(omit_geocode=True)
        return address

    def test_verify_rejects_expired_or_malformed_access_tokens(self):
        malformed = self.execute(
            "mutation { verifyToken(token: \"malformed\") { payload } }"
        )
        self.assertEqual(self.error_code(malformed), "INVALID_TOKEN")

        now = int(timezone.now().timestamp())
        expired_token = encode_access_token(
            {
                "sub": str(self.user.pk),
                "type": "access",
                "iat": now - 600,
                "exp": now - 300,
            }
        )
        expired = self.execute(
            "mutation Verify($token: String!) { verifyToken(token: $token) { payload } }",
            {"token": expired_token},
        )
        self.assertEqual(self.error_code(expired), "INVALID_TOKEN")

        pair = issue_token_pair(self.user)
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        protected = self.client.post(
            "/graphql/",
            data=json.dumps({"query": "query { requests { id } }"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {pair['token']}",
        )
        self.assertIsNotNone(protected.json().get("errors"))
        self.assertEqual(
            protected.json()["errors"][0]["extensions"]["code"],
            "INVALID_TOKEN",
        )


class ConcurrentRefreshTests(TransactionTestCase):
    reset_sequences = True

    def test_concurrent_rotation_allows_one_success_and_revokes_on_reuse(self):
        user = get_user_model().objects.create_user(
            email="concurrent-refresh@smokemap.test", password="test"
        )
        original = issue_token_pair(user)
        barrier = Barrier(2)
        outcomes = []

        def rotate():
            close_old_connections()
            barrier.wait()
            try:
                rotate_refresh_token(original["refresh_token"])
                outcomes.append("succeeded")
            except TokenLifecycleError as error:
                outcomes.append(error.code)
            finally:
                close_old_connections()

        threads = [Thread(target=rotate), Thread(target=rotate)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertCountEqual(outcomes, ["succeeded", "REFRESH_TOKEN_REUSED"])
        family = RefreshTokenCredential.objects.first().family
        family.refresh_from_db()
        self.assertIsNotNone(family.revoked_at)
        self.assertIsNotNone(family.compromised_at)
