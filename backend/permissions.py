from graphql import GraphQLError
from rest_framework import permissions


UNAUTHENTICATED = "UNAUTHENTICATED"
FORBIDDEN = "FORBIDDEN"
NOT_FOUND = "NOT_FOUND"


def is_active_user(user):
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
    )


def is_moderator(user):
    return is_active_user(user) and bool(
        getattr(user, "is_staff", False)
        or getattr(user, "is_superuser", False)
    )


def is_administrator(user):
    return is_active_user(user) and bool(getattr(user, "is_superuser", False))


def role_for_user(user):
    if is_administrator(user):
        return "administrator"
    if is_moderator(user):
        return "moderator"
    if is_active_user(user):
        return "user"
    return "guest"


def graphql_authorization_error(message, code):
    raise GraphQLError(message, extensions={"code": code})


def require_active_user(info):
    user = getattr(info.context, "user", None)
    if not is_active_user(user):
        graphql_authorization_error("Authentication required", UNAUTHENTICATED)
    return user


def require_moderator(info):
    user = require_active_user(info)
    if not is_moderator(user):
        graphql_authorization_error("Moderator permission required", FORBIDDEN)
    return user


def require_administrator(info):
    user = require_active_user(info)
    if not is_administrator(user):
        graphql_authorization_error("Administrator permission required", FORBIDDEN)
    return user


class IsAdministratorOrReadOnly(permissions.BasePermission):
    """Allow public reads and active-superuser writes."""

    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        return is_administrator(request.user)
