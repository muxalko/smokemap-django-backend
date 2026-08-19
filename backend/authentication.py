from graphql_jwt.utils import get_credentials
from rest_framework import authentication, exceptions

from .tokens import (
    TokenLifecycleError,
    decode_access_token,
    log_authentication_event,
    user_from_subject,
)


class BearerJSONWebTokenAuthentication(authentication.BaseAuthentication):
    def authenticate(self, request):
        token = get_credentials(request)
        if token is None:
            return None
        try:
            payload = decode_access_token(token, request)
        except TokenLifecycleError as error:
            log_authentication_event("bearer", "denied", context=request)
            raise exceptions.AuthenticationFailed("Invalid bearer token") from error
        user = user_from_subject(payload.get("sub"))
        if user is None or not user.is_active:
            log_authentication_event("bearer", "denied", context=request)
            raise exceptions.AuthenticationFailed("Invalid bearer token")
        log_authentication_event(
            "bearer", "succeeded", actor_id=user.pk, context=request
        )
        return user, token

    def authenticate_header(self, request):
        return 'Bearer realm="api"'
