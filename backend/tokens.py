import hashlib
import logging
import re
import secrets
import uuid
from datetime import timedelta

import jwt
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .models import RefreshTokenCredential, RefreshTokenFamily


ACCESS_TOKEN_LIFETIME = timedelta(minutes=5)
REFRESH_TOKEN_LIFETIME = timedelta(days=7)
REFRESH_COOKIE_NAME = "JWT-refresh-token"

INVALID_TOKEN = "INVALID_TOKEN"
INVALID_REFRESH_TOKEN = "INVALID_REFRESH_TOKEN"
REFRESH_TOKEN_EXPIRED = "REFRESH_TOKEN_EXPIRED"
REFRESH_TOKEN_REUSED = "REFRESH_TOKEN_REUSED"

logger = logging.getLogger(__name__)


class TokenLifecycleError(Exception):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def _jwt_configuration():
    configuration = settings.GRAPHQL_JWT
    return (
        configuration["JWT_SECRET_KEY"],
        configuration.get("JWT_ALGORITHM", "HS256"),
    )


def access_token_payload(user, context=None):
    now = timezone.now()
    return {
        "sub": str(user.pk),
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + ACCESS_TOKEN_LIFETIME).timestamp()),
    }


def encode_access_token(payload, context=None):
    secret_key, algorithm = _jwt_configuration()
    return jwt.encode(payload, secret_key, algorithm=algorithm)


def decode_access_token(token, context=None):
    secret_key, algorithm = _jwt_configuration()
    try:
        payload = jwt.decode(
            token,
            secret_key,
            algorithms=[algorithm],
            options={"require": ["sub", "type", "iat", "exp"]},
        )
    except jwt.PyJWTError as error:
        raise TokenLifecycleError("Invalid access token", INVALID_TOKEN) from error
    if payload.get("type") != "access":
        raise TokenLifecycleError("Invalid access token", INVALID_TOKEN)
    return payload


def subject_from_payload(payload):
    return payload.get("sub")


def user_from_subject(subject):
    if not subject:
        return None
    try:
        return get_user_model().objects.get(pk=subject)
    except (ValueError, get_user_model().DoesNotExist):
        return None


def _token_digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_refresh_credential(family):
    raw_token = secrets.token_urlsafe(48)
    credential = RefreshTokenCredential.objects.create(
        family=family,
        token_digest=_token_digest(raw_token),
    )
    return credential, raw_token


def issue_token_pair(user):
    now = timezone.now()
    with transaction.atomic():
        family = RefreshTokenFamily.objects.create(
            user=user,
            expires_at=now + REFRESH_TOKEN_LIFETIME,
        )
        _, refresh_token = _new_refresh_credential(family)
    payload = access_token_payload(user)
    return {
        "payload": payload,
        "token": encode_access_token(payload),
        "refresh_token": refresh_token,
        "refresh_expires_in": int(family.expires_at.timestamp()),
    }


def rotate_refresh_token(raw_token, context=None):
    if not raw_token:
        raise TokenLifecycleError(
            "Refresh token required", INVALID_REFRESH_TOKEN
        )

    digest = _token_digest(raw_token)
    failure = None
    result = None
    actor_id = None
    with transaction.atomic():
        credential = (
            RefreshTokenCredential.objects.select_for_update()
            .select_related("family__user")
            .filter(token_digest=digest)
            .first()
        )
        if credential is None or not credential.matches_digest(digest):
            failure = TokenLifecycleError(
                "Invalid refresh token", INVALID_REFRESH_TOKEN
            )
        else:
            family = credential.family
            actor_id = family.user_id
            now = timezone.now()
            if family.revoked_at is not None:
                failure = TokenLifecycleError(
                    "Invalid refresh token", INVALID_REFRESH_TOKEN
                )
            elif family.expires_at <= now:
                family.revoked_at = now
                family.save(update_fields=["revoked_at"])
                family.credentials.filter(revoked_at__isnull=True).update(
                    revoked_at=now
                )
                failure = TokenLifecycleError(
                    "Refresh token expired", REFRESH_TOKEN_EXPIRED
                )
            elif credential.used_at is not None or credential.revoked_at is not None:
                family.revoked_at = now
                family.compromised_at = now
                family.save(update_fields=["revoked_at", "compromised_at"])
                family.credentials.filter(revoked_at__isnull=True).update(
                    revoked_at=now
                )
                failure = TokenLifecycleError(
                    "Refresh token reuse detected", REFRESH_TOKEN_REUSED
                )
            else:
                successor, successor_token = _new_refresh_credential(family)
                credential.used_at = now
                credential.revoked_at = now
                credential.successor = successor
                credential.save(
                    update_fields=["used_at", "revoked_at", "successor"]
                )
                payload = access_token_payload(family.user)
                result = {
                    "payload": payload,
                    "token": encode_access_token(payload),
                    "refresh_token": successor_token,
                    "refresh_expires_in": int(family.expires_at.timestamp()),
                }

    if failure:
        log_authentication_event("refresh", "denied", actor_id, context)
        raise failure
    log_authentication_event("refresh", "succeeded", actor_id, context)
    return result


def revoke_refresh_family(raw_token, context=None):
    if not raw_token:
        raise TokenLifecycleError(
            "Refresh token required", INVALID_REFRESH_TOKEN
        )
    digest = _token_digest(raw_token)
    with transaction.atomic():
        credential = (
            RefreshTokenCredential.objects.select_for_update()
            .select_related("family")
            .filter(token_digest=digest)
            .first()
        )
        if credential is None or not credential.matches_digest(digest):
            raise TokenLifecycleError(
                "Invalid refresh token", INVALID_REFRESH_TOKEN
            )
        family = credential.family
        now = family.revoked_at or timezone.now()
        if family.revoked_at is None:
            family.revoked_at = now
            family.save(update_fields=["revoked_at"])
            family.credentials.filter(revoked_at__isnull=True).update(revoked_at=now)
    log_authentication_event("revoke", "succeeded", family.user_id, context)
    return int(now.timestamp())


def refresh_token_from_request(info, supplied_token=None):
    if supplied_token:
        return supplied_token
    return getattr(info.context, "COOKIES", {}).get(REFRESH_COOKIE_NAME)


def log_authentication_event(action, outcome, actor_id=None, context=None):
    supplied_correlation_id = getattr(context, "META", {}).get(
        "HTTP_X_REQUEST_ID", ""
    )
    correlation_id = (
        supplied_correlation_id
        if re.fullmatch(r"[A-Za-z0-9-]{1,64}", supplied_correlation_id)
        else str(uuid.uuid4())
    )
    logger.info(
        "auth_event action=%s outcome=%s actor_id=%s correlation_id=%s",
        action,
        outcome,
        actor_id or "unknown",
        correlation_id,
    )
