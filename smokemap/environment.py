"""Strict environment parsing for non-development deployments.

Errors intentionally name only the invalid variable. They never include values,
because several callers contain credentials.
"""

from dataclasses import dataclass
from ipaddress import ip_address
import re
from urllib.parse import urlsplit


class ConfigurationError(RuntimeError):
    """Raised when deployment configuration is absent or unsafe."""


@dataclass(frozen=True, repr=False)
class StagingEnvironment:
    secret_key: str
    allowed_hosts: tuple[str, ...]
    cors_allowed_origins: tuple[str, ...]
    csrf_trusted_origins: tuple[str, ...]
    postgres_host: str
    postgres_port: int
    postgres_db: str
    postgres_user: str
    postgres_password: str
    postgres_options: str
    postgres_sslmode: str
    aws_access_key_id: str
    aws_secret_access_key: str
    aws_region: str
    aws_storage_bucket_name: str
    aws_s3_endpoint_url: str
    aws_s3_addressing_style: str
    media_storage_bucket_name: str
    media_storage_identifier: str
    media_storage_internal_endpoint_url: str
    media_upload_endpoint_url: str


_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE)
_BUCKET_NAME = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_DEVELOPMENT_HOSTS = {
    "localhost",
    "127.0.0.1",
    "::1",
    "backend",
    "db",
    "minio",
    "postgres",
    "storage",
}


def _required(environ, name):
    value = environ.get(name, "")
    if not value or not value.strip():
        raise ConfigurationError(f"{name} is required")
    if value != value.strip():
        raise ConfigurationError(f"{name} must not have surrounding whitespace")
    return value


def _list(environ, name):
    values = tuple(value.strip() for value in _required(environ, name).split(","))
    if not values or any(not value for value in values):
        raise ConfigurationError(f"{name} must be a non-empty comma-separated list")
    if len(set(values)) != len(values):
        raise ConfigurationError(f"{name} must not contain duplicates")
    return values


def _validated_hostname(hostname, name):
    normalized = hostname.rstrip(".").lower()
    if not normalized or normalized in _DEVELOPMENT_HOSTS or "*" in normalized:
        raise ConfigurationError(f"{name} contains a development or wildcard host")

    try:
        address = ip_address(normalized)
    except ValueError:
        if len(normalized) > 253 or any(
            _HOST_LABEL.fullmatch(label) is None for label in normalized.split(".")
        ):
            raise ConfigurationError(f"{name} contains an invalid host") from None
    else:
        if address.is_loopback or address.is_unspecified or address.is_link_local:
            raise ConfigurationError(f"{name} contains a development host")

    return normalized


def _exact_hosts(environ, name):
    hosts = _list(environ, name)
    normalized_hosts = []
    for host in hosts:
        if any(character in host for character in ":/@?#"):
            raise ConfigurationError(f"{name} must contain exact deployment hosts")
        normalized_hosts.append(_validated_hostname(host, name))
    if len(set(normalized_hosts)) != len(normalized_hosts):
        raise ConfigurationError(f"{name} must not contain duplicates")
    return tuple(normalized_hosts)


def _database_host(environ, name):
    value = _required(environ, name)
    try:
        ip_address(value)
    except ValueError:
        if any(character in value for character in ":/@?#"):
            raise ConfigurationError(
                f"{name} must be a host without a URL or port"
            ) from None
    _validated_hostname(value, name)
    return value


def _endpoint(environ, name, *, require_https):
    value = _required(environ, name)
    try:
        parsed = urlsplit(value)
        parsed_port = parsed.port
        parsed_hostname = parsed.hostname
    except ValueError:
        raise ConfigurationError(f"{name} contains an invalid endpoint") from None
    allowed_schemes = {"https"} if require_https else {"http", "https"}
    if (
        parsed.scheme not in allowed_schemes
        or not parsed_hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        scheme = "HTTPS" if require_https else "HTTP(S)"
        raise ConfigurationError(f"{name} must be an absolute {scheme} endpoint")
    if parsed_port is not None and not 1 <= parsed_port <= 65535:
        raise ConfigurationError(f"{name} contains an invalid port")
    _validated_hostname(parsed_hostname, name)
    return value.rstrip("/")


def _https_origins(environ, name):
    origins = _list(environ, name)
    normalized_origins = []
    origin_keys = []
    for origin in origins:
        normalized = _endpoint({name: origin}, name, require_https=True)
        if normalized != origin.rstrip("/"):
            raise ConfigurationError(f"{name} contains an invalid origin")
        normalized_origins.append(origin.rstrip("/"))
        parsed = urlsplit(origin)
        origin_keys.append((parsed.hostname.lower().rstrip("."), parsed.port or 443))
    if len(set(origin_keys)) != len(origin_keys):
        raise ConfigurationError(f"{name} must not contain duplicates")
    return tuple(normalized_origins)


def _choice(environ, name, choices):
    value = _required(environ, name).lower()
    if value not in choices:
        raise ConfigurationError(f"{name} is invalid")
    return value


def _port(environ, name):
    value = _required(environ, name)
    try:
        port = int(value)
    except ValueError:
        raise ConfigurationError(f"{name} must be an integer port") from None
    if not 1 <= port <= 65535:
        raise ConfigurationError(f"{name} must be an integer port")
    return port


def _bucket(environ, name):
    value = _required(environ, name)
    if _BUCKET_NAME.fullmatch(value) is None or ".." in value:
        raise ConfigurationError(f"{name} must be an S3-compatible bucket name")
    return value


def load_staging_environment(environ):
    """Return a validated staging configuration or raise without leaking values."""

    if _required(environ, "DJANGO_TRUST_X_FORWARDED_PROTO") != "true":
        raise ConfigurationError("DJANGO_TRUST_X_FORWARDED_PROTO must be true")

    secret_key = _required(environ, "DJANGO_SECRET_KEY")
    if len(secret_key) < 50:
        raise ConfigurationError("DJANGO_SECRET_KEY must contain at least 50 characters")

    public_bucket = _bucket(environ, "AWS_STORAGE_BUCKET_NAME")
    private_bucket = _bucket(environ, "MEDIA_STORAGE_BUCKET_NAME")
    if public_bucket == private_bucket:
        raise ConfigurationError(
            "AWS_STORAGE_BUCKET_NAME and MEDIA_STORAGE_BUCKET_NAME must differ"
        )

    storage_identifier = _required(environ, "MEDIA_STORAGE_IDENTIFIER")
    if _IDENTIFIER.fullmatch(storage_identifier) is None:
        raise ConfigurationError("MEDIA_STORAGE_IDENTIFIER is invalid")

    return StagingEnvironment(
        secret_key=secret_key,
        allowed_hosts=_exact_hosts(environ, "DJANGO_ALLOWED_HOSTS"),
        cors_allowed_origins=_https_origins(environ, "CORS_ALLOWED_ORIGINS"),
        csrf_trusted_origins=_https_origins(environ, "CSRF_TRUSTED_ORIGINS"),
        postgres_host=_database_host(environ, "POSTGRES_HOST"),
        postgres_port=_port(environ, "POSTGRES_PORT"),
        postgres_db=_required(environ, "POSTGRES_DB"),
        postgres_user=_required(environ, "POSTGRES_USER"),
        postgres_password=_required(environ, "POSTGRES_PASSWORD"),
        postgres_options=_required(environ, "POSTGRES_OPTIONS"),
        postgres_sslmode=_choice(
            environ, "POSTGRES_SSLMODE", {"require", "verify-ca", "verify-full"}
        ),
        aws_access_key_id=_required(environ, "AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=_required(environ, "AWS_SECRET_ACCESS_KEY"),
        aws_region=_required(environ, "AWS_REGION"),
        aws_storage_bucket_name=public_bucket,
        aws_s3_endpoint_url=_endpoint(
            environ, "AWS_S3_ENDPOINT_URL", require_https=True
        ),
        aws_s3_addressing_style=_choice(
            environ, "AWS_S3_ADDRESSING_STYLE", {"path", "virtual"}
        ),
        media_storage_bucket_name=private_bucket,
        media_storage_identifier=storage_identifier,
        media_storage_internal_endpoint_url=_endpoint(
            environ, "MEDIA_STORAGE_INTERNAL_ENDPOINT_URL", require_https=False
        ),
        media_upload_endpoint_url=_endpoint(
            environ, "MEDIA_UPLOAD_ENDPOINT_URL", require_https=True
        ),
    )
