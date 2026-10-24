import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

from smokemap.environment import ConfigurationError, load_staging_environment


PROJECT_ROOT = Path(__file__).resolve().parents[1]

VALID_ENVIRONMENT = {
    "DJANGO_TRUST_X_FORWARDED_PROTO": "true",
    "DJANGO_SECRET_KEY": "x" * 64,
    "DJANGO_ALLOWED_HOSTS": "api.staging.example.com",
    "CORS_ALLOWED_ORIGINS": "https://staging.example.com",
    "CSRF_TRUSTED_ORIGINS": "https://staging.example.com",
    "POSTGRES_HOST": "postgres.internal.example.com",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "smokemap",
    "POSTGRES_USER": "smokemap",
    "POSTGRES_PASSWORD": "database-secret",
    "POSTGRES_OPTIONS": "-c search_path=smokemap",
    "POSTGRES_SSLMODE": "verify-full",
    "AWS_ACCESS_KEY_ID": "storage-access-key",
    "AWS_SECRET_ACCESS_KEY": "storage-secret-key",
    "AWS_REGION": "us-east-1",
    "AWS_STORAGE_BUCKET_NAME": "smokemap-public",
    "AWS_S3_ENDPOINT_URL": "https://objects.example.com",
    "AWS_S3_ADDRESSING_STYLE": "path",
    "MEDIA_STORAGE_BUCKET_NAME": "smokemap-private",
    "MEDIA_STORAGE_IDENTIFIER": "staging-private-v1",
    "MEDIA_STORAGE_INTERNAL_ENDPOINT_URL": "http://storage.internal:9000",
    "MEDIA_UPLOAD_ENDPOINT_URL": "https://uploads.example.com",
}


class StagingEnvironmentTests(unittest.TestCase):
    def load(self, **overrides):
        environment = {**VALID_ENVIRONMENT, **overrides}
        return load_staging_environment(environment)

    def test_complete_environment_is_normalized_without_secret_repr(self):
        configuration = self.load(
            DJANGO_ALLOWED_HOSTS="api.staging.example.com,admin.staging.example.com",
            CORS_ALLOWED_ORIGINS="https://staging.example.com/",
        )

        self.assertEqual(
            configuration.allowed_hosts,
            ("api.staging.example.com", "admin.staging.example.com"),
        )
        self.assertEqual(
            configuration.cors_allowed_origins, ("https://staging.example.com",)
        )
        self.assertEqual(configuration.postgres_port, 5432)
        self.assertEqual(configuration.postgres_sslmode, "verify-full")
        self.assertEqual(
            configuration.media_storage_internal_endpoint_url,
            "http://storage.internal:9000",
        )
        self.assertNotIn(VALID_ENVIRONMENT["DJANGO_SECRET_KEY"], repr(configuration))
        self.assertNotIn(VALID_ENVIRONMENT["POSTGRES_PASSWORD"], repr(configuration))

    def test_every_staging_variable_is_required(self):
        for name in VALID_ENVIRONMENT:
            with self.subTest(name=name):
                environment = dict(VALID_ENVIRONMENT)
                environment.pop(name)
                with self.assertRaisesRegex(ConfigurationError, name):
                    load_staging_environment(environment)

    def test_errors_do_not_echo_secret_values(self):
        exposed_value = "short-but-sensitive"
        with self.assertRaises(ConfigurationError) as raised:
            self.load(DJANGO_SECRET_KEY=exposed_value)

        self.assertNotIn(exposed_value, str(raised.exception))

    def test_hosts_reject_wildcards_urls_and_development_names(self):
        for hosts in ("*", ".example.com", "https://api.example.com", "localhost"):
            with self.subTest(hosts=hosts):
                with self.assertRaisesRegex(ConfigurationError, "DJANGO_ALLOWED_HOSTS"):
                    self.load(DJANGO_ALLOWED_HOSTS=hosts)

    def test_browser_origins_require_https_without_credentials_or_paths(self):
        for origin in (
            "http://staging.example.com",
            "https://user:secret@staging.example.com",
            "https://staging.example.com/path",
            "https://localhost",
        ):
            with self.subTest(origin=origin):
                with self.assertRaisesRegex(ConfigurationError, "CORS_ALLOWED_ORIGINS"):
                    self.load(CORS_ALLOWED_ORIGINS=origin)

    def test_public_storage_endpoints_require_https(self):
        for name in ("AWS_S3_ENDPOINT_URL", "MEDIA_UPLOAD_ENDPOINT_URL"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ConfigurationError, name):
                    self.load(**{name: "http://objects.example.com"})

    def test_database_and_storage_boundaries_are_fail_closed(self):
        invalid = (
            ("POSTGRES_SSLMODE", "disable"),
            ("POSTGRES_PORT", "0"),
            ("DJANGO_TRUST_X_FORWARDED_PROTO", "false"),
            ("AWS_S3_ADDRESSING_STYLE", "auto"),
            ("MEDIA_STORAGE_BUCKET_NAME", "smokemap-public"),
        )
        for name, value in invalid:
            with self.subTest(name=name):
                with self.assertRaises(ConfigurationError):
                    self.load(**{name: value})


class ProductionImageContractTests(unittest.TestCase):
    def test_production_target_uses_staging_settings_and_gunicorn_as_non_root(self):
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("FROM dependencies AS production", dockerfile)
        self.assertIn("ENV SETTINGS_MODE=staging", dockerfile)
        self.assertIn("USER smokemap", dockerfile)
        self.assertIn('CMD ["gunicorn",', dockerfile)
        self.assertIn("smokemap.wsgi:app", dockerfile)
        self.assertNotIn("pip install --upgrade pip", dockerfile)

    def test_gunicorn_is_exactly_pinned_in_runtime_manifest(self):
        requirement_lines = (PROJECT_ROOT / "requirements.txt").read_text(
            encoding="utf-8"
        ).splitlines()

        self.assertEqual(
            [line for line in requirement_lines if line.lower().startswith("gunicorn")],
            ["gunicorn==23.0.0; python_version >= '3.7'"],
        )

    def test_settings_activate_strict_staging_loader_and_https_controls(self):
        settings_source = (PROJECT_ROOT / "smokemap" / "settings.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('elif SETTINGS_MODE == "staging":', settings_source)
        self.assertIn("load_staging_environment(os.environ)", settings_source)
        self.assertIn(
            'SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")',
            settings_source,
        )
        self.assertIn("SECURE_SSL_REDIRECT = True", settings_source)
        self.assertIn("SESSION_COOKIE_SECURE = True", settings_source)
        self.assertIn("CSRF_COOKIE_SECURE = True", settings_source)

    def test_staging_values_are_wired_into_django_settings(self):
        environment = {
            **VALID_ENVIRONMENT,
            "SETTINGS_MODE": "staging",
            "GDAL_LIBRARY_PATH": "/runtime/libgdal.so",
            "GEOS_LIBRARY_PATH": "/runtime/libgeos_c.so",
            "PYTHONPATH": str(PROJECT_ROOT),
        }
        program = textwrap.dedent(
            """
            import sys
            import types

            sys.modules["dotenv"] = types.SimpleNamespace(load_dotenv=lambda: None)
            import smokemap.settings as settings

            assert settings.DEBUG is False
            assert settings.ADMIN_ENABLED is False
            assert settings.ALLOWED_HOSTS == ["api.staging.example.com"]
            assert settings.CORS_ALLOWED_ORIGINS == ["https://staging.example.com"]
            assert settings.CSRF_TRUSTED_ORIGINS == ["https://staging.example.com"]
            assert settings.SECURE_PROXY_SSL_HEADER == (
                "HTTP_X_FORWARDED_PROTO", "https"
            )
            assert settings.DATABASES["default"]["ENGINE"].endswith("postgis")
            assert settings.DATABASES["default"]["OPTIONS"] == {
                "options": "-c search_path=smokemap",
                "sslmode": "verify-full",
            }
            assert settings.AWS_S3_ENDPOINT_URL == "https://objects.example.com"
            assert settings.MEDIA_STORAGE_INTERNAL_ENDPOINT_URL == (
                "http://storage.internal:9000"
            )
            assert settings.MEDIA_UPLOAD_ENDPOINT_URL == "https://uploads.example.com"
            """
        )

        result = subprocess.run(
            [sys.executable, "-c", program],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)


if __name__ == "__main__":
    unittest.main()
