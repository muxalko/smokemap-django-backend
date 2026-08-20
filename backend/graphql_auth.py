from graphql import GraphQLError
from graphql_jwt.utils import get_credentials

from .tokens import (
    INVALID_TOKEN,
    TokenLifecycleError,
    decode_access_token,
    log_authentication_event,
    user_from_subject,
)


class BearerAuthenticationMiddleware:
    def resolve(self, next_resolver, root, info, **kwargs):
        context = info.context
        if getattr(context, "_smokemap_bearer_checked", False):
            return next_resolver(root, info, **kwargs)
        context._smokemap_bearer_checked = True

        token = get_credentials(context)
        if token is None:
            return next_resolver(root, info, **kwargs)
        try:
            payload = decode_access_token(token, context)
        except TokenLifecycleError as error:
            log_authentication_event("bearer", "denied", context=context)
            raise GraphQLError(
                "Invalid bearer token", extensions={"code": INVALID_TOKEN}
            ) from error
        user = user_from_subject(payload.get("sub"))
        if user is None or not user.is_active:
            log_authentication_event("bearer", "denied", context=context)
            raise GraphQLError(
                "Invalid bearer token", extensions={"code": INVALID_TOKEN}
            )
        context.user = user
        log_authentication_event(
            "bearer", "succeeded", actor_id=user.pk, context=context
        )
        return next_resolver(root, info, **kwargs)
