import graphene
from graphene.types.generic import GenericScalar
from graphql import GraphQLError
from graphene_django import DjangoObjectType
from .models import (
    Address,
    Category,
    CustomUser,
    Image,
    Location,
    MediaUploadIntent,
    ModerationAudit,
    Place,
    Request,
    RequestTag,
    Tag,
)
from .permissions import (
    FORBIDDEN,
    NOT_FOUND,
    graphql_authorization_error,
    is_moderator,
    require_active_user,
    require_administrator,
    require_moderator,
    role_for_user,
)
from django.contrib.auth import authenticate
import graphql_geojson
from django.db import transaction
from django.db.models import Prefetch, Q
from .submissions import (
    IdempotencyConflict,
    SubmissionInputError,
    create_submission,
)
from .media import (
    MediaInputError,
    MediaStateConflict,
    attach_verified_media,
    cleanup_media_object,
    create_upload_intent,
    expire_upload_intent,
    issue_upload,
    verify_upload,
)
from .media_storage import StorageOperationError
from .tokens import (
    INVALID_TOKEN,
    TokenLifecycleError,
    decode_access_token,
    issue_token_pair,
    log_authentication_event,
    refresh_token_from_request,
    revoke_refresh_family,
    rotate_refresh_token,
)

import logging
logger = logging.getLogger( __name__ )

##################################TYPES###############################
class UserType(DjangoObjectType):

    # if user is in admins group his role will be 'admin' otherwise 'visitor'
    # that is returned in web client for authorzation of users
    role = graphene.String()
    def resolve_role(self, info):
        return role_for_user(self)
    
    class Meta:
        model = CustomUser
        fields = ('name', 'image', 'email', 'role')

class PlaceType(DjangoObjectType):
    class Meta:
        model = Place
        fields = ('id','name', 'category', 'address', 'description', 'tags', 'website', 'image_set')

class CategoryType(DjangoObjectType):
    class Meta:
        model = Category
        fields = ('id', 'slug', 'name', 'description')
    
    # @classmethod
    # def get_queryset(cls, queryset, info):
    #     logger.debug("CategoryType.info.context.user: %s",info.context.user)
    #     if info.context.user.is_anonymous:
    #         return queryset.filter(published=True)
    #     return queryset

class TagType(DjangoObjectType):
    class Meta:
        model = Tag
        fields = ('id', 'name')

class ImageType(DjangoObjectType):
    class Meta:
        model = Image
        fields = ('id', 'name', 'url', 'metadata')

    @classmethod
    def get_queryset(cls, queryset, info):
        return queryset.filter(place__isnull=False, is_managed=False)


class MediaUploadIntentType(DjangoObjectType):
    submission_id = graphene.ID(required=True)
    state = graphene.String(required=True)

    class Meta:
        model = MediaUploadIntent
        fields = (
            "id",
            "submission_id",
            "state",
            "slot",
            "expected_mime",
            "declared_byte_size",
            "absolute_expires_at",
            "presign_expires_at",
            "server_byte_size",
            "detected_mime",
            "width",
            "height",
            "failure_code",
            "verification_attempts",
            "cleanup_attempts",
            "cleanup_next_attempt_at",
            "created_at",
            "verified_at",
            "attached_at",
            "deleted_at",
        )

    def resolve_submission_id(self, info):
        return str(self.submission_id)

    def resolve_state(self, info):
        return self.state.value if hasattr(self.state, "value") else str(self.state)


class ManagedMediaAttachmentType(DjangoObjectType):
    submission_id = graphene.ID(required=True)

    class Meta:
        model = Image
        skip_registry = True
        fields = (
            "id",
            "submission_id",
            "position",
            "state",
            "byte_size",
            "detected_mime",
            "width",
            "height",
            "attached_at",
        )

    def resolve_submission_id(self, info):
        return str(self.request_id)


class MediaUploadAuthorization(graphene.ObjectType):
    url = graphene.String(required=True)
    fields = GenericScalar(required=True)
    expires_at = graphene.DateTime(required=True)

class AddressType(graphql_geojson.GeoJSONType):
    class Meta:
        model = Address
        geojson_field = 'location'
        # yelds UserWarning: Field name "name" matches an attribute on Django model "backend.Place"
        # but it's not a model field so Graphene cannot determine what type it should be.
        # Either define the type of the field on DjangoObjectType "PlaceType"
        # or remove it from the "fields" list
        # fields = (
        #     'id',
        #     # 'addressString',
        #     'location'
        # )

class RequestType(DjangoObjectType):
    state = graphene.String(required=True)
    tags = graphene.List(graphene.NonNull(graphene.String), required=True)
    approved_by = graphene.String(
        description="Deprecated compatibility field containing the reviewer ID."
    )
    requested_by = graphene.String(
        description="Deprecated compatibility field containing the owner ID."
    )

    def resolve_approved_by(self, info):
        return str(self.reviewed_by_id) if self.reviewed_by_id else None

    def resolve_requested_by(self, info):
        return str(self.owner_id) if self.owner_id else None

    def resolve_state(self, info):
        return self.state.value if hasattr(self.state, 'value') else str(self.state)

    def resolve_tags(self, info):
        return [request_tag.display for request_tag in self.request_tags.all()]

    class Meta:
        model = Request
        fields = (
            'id',
            'name',
            'category',
            'description',
            'tags',
            'website',
            'address',
            'date_created',
            'date_updated',
            'date_approved',
            'approved',
            'approved_comment',
            'state',
        )
# TODO: make all methods use **kwargs to use decorator
##############################DECORATORS##############################
def anonymous_return(value):
    def anonymous_return_decorator(func):
        def anonymous_return_wrapper(obj, info, **kwargs):
            if not info.context.user.is_authenticated:
                if callable(value):
                    return value()
                return value
            return func(obj, info, **kwargs)
        return anonymous_return_wrapper
    return anonymous_return_decorator

###############################ERRORS##################################
class AuthenticationRequired(graphene.ObjectType):
    message = graphene.String(
        required=True,
    )

    @staticmethod
    def default_message():
        return AuthenticationRequired(
            message="You must be logged in to perform this action"
        )


def request_queryset_with_tags(queryset):
    return queryset.prefetch_related(
        Prefetch(
            "request_tags",
            queryset=RequestTag.objects.select_related("tag").order_by("position"),
        )
    )


#################################QUERIES###############################

class Query(graphene.ObjectType):
    categories = graphene.List(CategoryType)
    tags = graphene.List(TagType)
    addresses = graphene.List(AddressType)
    images = graphene.List(ImageType)
    # images_by_set_id = graphene.Field(
    #     graphene.List(ImageType),
    #     set_id=graphene.String()
    # )
    requests = graphene.List(RequestType)
    requests_to_approve = graphene.List(RequestType)
    request_by_id = graphene.Field(
        RequestType,
        id=graphene.ID()
    )
    requests_by_name = graphene.Field(
        RequestType,
        name=graphene.String()
    )

    places = graphene.List(PlaceType)

    places_names = graphene.List(graphene.String)

    place_by_id = graphene.Field(
        PlaceType,
        id=graphene.ID()
    )

    places_by_name = graphene.Field(
        graphene.List(PlaceType),
        name=graphene.String()
    )

    places_startWith_name = graphene.Field(
        graphene.List(PlaceType),
        name=graphene.String()
    )

    s3_presigned_url = graphene.JSONString()
    # s3_presigned_url = graphene.Field(
    #     url=graphene.String(),
    #     fields=graphene.Field(
    #         key=graphene.String(),
    #         x-amz-algorithm=graphene.String(),
    #         x-amz-credential=graphene.String(),
    #         x-amz-date=graphene.String(),
    #         policy=graphene.String(),
    #         x-amz-signature=graphene.String(),
    #     )
    # )

    def resolve_categories(root, info): 
        # Querying a list
        return Category.objects.all()
     
    def resolve_tags(root, info):
        return Tag.objects.filter(is_public=True)
      
    def resolve_addresses(root, info):
        return Address.objects.filter(place__isnull=False).distinct()
    
    def resolve_images(root, info):
        return Image.objects.filter(place__isnull=False, is_managed=False)
    
    # def resolve_images_by_set_id(root, info, set_id):
    #     # Querying a list
    #     return Image.objects.filter(set_id=set_id)

    def resolve_requests(root, info):
        user = require_active_user(info)
        requests = request_queryset_with_tags(
            Request.objects.exclude(state=Request.State.APPROVED)
        )
        if is_moderator(user):
            return requests.filter(
                Q(owner=user) | Q(state=Request.State.PENDING)
            )
        return requests.filter(owner=user)

    # @login_required
    def resolve_requests_to_approve(root, info, **kwargs):
        require_moderator(info)
        return request_queryset_with_tags(
            Request.objects.filter(state=Request.State.PENDING)
        )

    def resolve_request_by_id(root, info, id):
        user = require_active_user(info)
        requests = request_queryset_with_tags(
            Request.objects.exclude(state=Request.State.APPROVED)
        )
        if is_moderator(user):
            requests = requests.filter(
                Q(owner=user) | Q(state=Request.State.PENDING)
            )
        else:
            requests = requests.filter(owner=user)
        request = requests.filter(pk=id).first()
        if request is None:
            graphql_authorization_error("Submission not found", NOT_FOUND)
        return request

    def resolve_requests_by_name(root, info, name):
        user = require_active_user(info)
        requests = request_queryset_with_tags(
            Request.objects.exclude(state=Request.State.APPROVED).filter(name=name)
        )
        if is_moderator(user):
            requests = requests.filter(
                Q(owner=user) | Q(state=Request.State.PENDING)
            )
        else:
            requests = requests.filter(owner=user)
        return requests.first()

    def resolve_places(root, info):
        # Querying a list
        return Place.objects.all()
    
    def resolve_places_names(root, info):
        # Querying a list
        return Place.objects.all().values_list("name", flat=True)
    
    def resolve_place_by_id(root, info, id):
        # Querying a list
        return Place.objects.get(pk=id)
    
    def resolve_places_by_name(root, info, name):
        # Querying a list
        return Place.objects.filter(name=name)
    
    def resolve_places_startWith_name(root, info, name):
        # Querying a list
        return Place.objects.filter(name__startswith=name)
    
    def resolve_s3_presigned_url(root, info):
        graphql_authorization_error(
            "Uploads are disabled until owner-bound upload intents are available",
            FORBIDDEN,
        )
     
class RequestInput(graphene.InputObjectType):
    name = graphene.String()
    category = graphene.String()
    description = graphene.String()
    address_string = graphene.String()
    tags = graphene.List(graphene.String)
    website = graphene.String()

class CreateRequest(graphene.Mutation):
    class Arguments:
        input = RequestInput(required=True)

    request = graphene.Field(RequestType)

    @classmethod
    def mutate(cls, root, info, input):
        require_active_user(info)
        graphql_authorization_error(
            "Legacy submission creation is disabled; use createSubmissionV3",
            FORBIDDEN,
        )


class SubmissionV3Input(graphene.InputObjectType):
    name = graphene.String(required=True)
    category_slug = graphene.String(required=True)
    longitude = graphene.Float(required=True)
    latitude = graphene.Float(required=True)
    address_label = graphene.String()
    tags = graphene.List(graphene.String)
    description = graphene.String()
    website = graphene.String()


class CreateSubmissionV3(graphene.Mutation):
    class Arguments:
        idempotency_key = graphene.String(required=True)
        input = SubmissionV3Input(required=True)

    submission = graphene.Field(RequestType, required=True)
    replayed = graphene.Boolean(required=True)

    @classmethod
    def mutate(cls, root, info, idempotency_key, input):
        actor = require_active_user(info)
        raw_input = {
            "name": input.name,
            "category_slug": input.category_slug,
            "longitude": input.longitude,
            "latitude": input.latitude,
            "address_label": getattr(input, "address_label", None),
            "tags": getattr(input, "tags", None),
            "description": getattr(input, "description", None),
            "website": getattr(input, "website", None),
        }
        try:
            submission, replayed = create_submission(
                actor,
                idempotency_key,
                raw_input,
            )
        except SubmissionInputError as error:
            raise GraphQLError(
                str(error),
                extensions={"code": "INVALID_SUBMISSION", "field": error.field},
            ) from error
        except IdempotencyConflict as error:
            raise GraphQLError(
                str(error),
                extensions={"code": "IDEMPOTENCY_CONFLICT"},
            ) from error
        except Exception as error:
            logger.exception("Submission creation failed after validation")
            raise GraphQLError(
                "Submission could not be created",
                extensions={"code": "SUBMISSION_CREATE_FAILED"},
            ) from error
        return cls(submission=submission, replayed=replayed)


class DeleteRequest(graphene.Mutation):
    ok = graphene.Boolean()

    class Arguments:
        id = graphene.ID()

    @classmethod
    def mutate(cls, root, info, id):
        actor = require_administrator(info)
        request = Request.objects.exclude(state=Request.State.APPROVED).filter(pk=id).first()
        if request is None:
            graphql_authorization_error("Submission not found", NOT_FOUND)

        with transaction.atomic():
            ModerationAudit.objects.create(
                actor=actor,
                action=ModerationAudit.Action.HARD_DELETE,
                target_type="request",
                target_id=request.pk,
            )
            request.delete()
        return cls(ok=True)


class RequestApproveInput(graphene.InputObjectType):
    approved_comment = graphene.String()


class ApproveRequest(graphene.Mutation):

    class Arguments:
        input = RequestApproveInput(required=True)
        id = graphene.ID()

    request = graphene.Field(RequestType)
    
    @classmethod
    def mutate(cls, root, info, input, id):
        actor = require_moderator(info)
        request = Request.objects.exclude(state=Request.State.APPROVED).filter(pk=id).first()
        if request is None:
            graphql_authorization_error("Submission not found", NOT_FOUND)
        if request.owner_id == actor.pk:
            ModerationAudit.objects.create(
                actor=actor,
                action=ModerationAudit.Action.APPROVE,
                target_type="request",
                target_id=request.pk,
                outcome="denied_self_review",
            )
        graphql_authorization_error(
            "Legacy approval is disabled for the M3 lifecycle",
            FORBIDDEN,
        )

class ImageInput(graphene.InputObjectType):
    request_id = graphene.String()
    name = graphene.String()
    url = graphene.String()
    metadata = graphene.String(required=False)

class CreateImage(graphene.Mutation):
    image = graphene.Field(ImageType)

    class Arguments:
        input = ImageInput(required=True)

    @classmethod
    def mutate(cls, root, info, input):
        graphql_authorization_error(
            "Uploads are disabled until owner-bound upload intents are available",
            FORBIDDEN,
        )


def _raise_media_graphql_error(error):
    if isinstance(error, MediaInputError):
        raise GraphQLError(
            str(error),
            extensions={"code": error.code, "field": error.field},
        ) from error
    if isinstance(error, IdempotencyConflict):
        raise GraphQLError(
            str(error),
            extensions={"code": "IDEMPOTENCY_CONFLICT"},
        ) from error
    if isinstance(error, MediaStateConflict):
        raise GraphQLError(str(error), extensions={"code": error.code}) from error
    if isinstance(error, StorageOperationError):
        raise GraphQLError(
            "Private media storage is temporarily unavailable",
            extensions={"code": "MEDIA_STORAGE_UNAVAILABLE"},
        ) from error
    raise error


class CreateMediaUploadIntentInput(graphene.InputObjectType):
    submission_id = graphene.ID(required=True)
    mime_type = graphene.String(required=True)
    declared_byte_size = graphene.Int(required=True)
    declared_sha256 = graphene.String(required=True)
    original_filename = graphene.String()
    slot = graphene.Int()


class CreateMediaUploadIntent(graphene.Mutation):
    class Arguments:
        idempotency_key = graphene.String(required=True)
        input = CreateMediaUploadIntentInput(required=True)

    intent = graphene.Field(MediaUploadIntentType, required=True)
    replayed = graphene.Boolean(required=True)

    @classmethod
    def mutate(cls, root, info, idempotency_key, input):
        actor = require_active_user(info)
        try:
            intent, replayed = create_upload_intent(
                actor,
                input.submission_id,
                idempotency_key,
                mime_type=input.mime_type,
                declared_byte_size=input.declared_byte_size,
                declared_sha256=input.declared_sha256,
                original_filename=getattr(input, "original_filename", "") or "",
                slot=getattr(input, "slot", None),
            )
        except (
            MediaInputError,
            MediaStateConflict,
            IdempotencyConflict,
            StorageOperationError,
        ) as error:
            _raise_media_graphql_error(error)
        return cls(intent=intent, replayed=replayed)


class IssueMediaUploadIntent(graphene.Mutation):
    class Arguments:
        intent_id = graphene.ID(required=True)
        idempotency_key = graphene.String(required=True)

    intent = graphene.Field(MediaUploadIntentType, required=True)
    upload = graphene.Field(MediaUploadAuthorization, required=True)
    replayed = graphene.Boolean(required=True)

    @classmethod
    def mutate(cls, root, info, intent_id, idempotency_key):
        actor = require_active_user(info)
        try:
            intent, upload, replayed = issue_upload(
                actor, intent_id, idempotency_key, renew=False
            )
        except (MediaInputError, MediaStateConflict, IdempotencyConflict, StorageOperationError) as error:
            _raise_media_graphql_error(error)
        return cls(
            intent=intent,
            upload=MediaUploadAuthorization(
                url=upload["url"],
                fields=upload["fields"],
                expires_at=upload["expires_at"],
            ),
            replayed=replayed,
        )


class RenewMediaUploadIntent(graphene.Mutation):
    class Arguments:
        intent_id = graphene.ID(required=True)
        idempotency_key = graphene.String(required=True)

    intent = graphene.Field(MediaUploadIntentType, required=True)
    upload = graphene.Field(MediaUploadAuthorization, required=True)
    replayed = graphene.Boolean(required=True)

    @classmethod
    def mutate(cls, root, info, intent_id, idempotency_key):
        actor = require_active_user(info)
        try:
            intent, upload, replayed = issue_upload(
                actor, intent_id, idempotency_key, renew=True
            )
        except (MediaInputError, MediaStateConflict, IdempotencyConflict, StorageOperationError) as error:
            _raise_media_graphql_error(error)
        return cls(
            intent=intent,
            upload=MediaUploadAuthorization(
                url=upload["url"],
                fields=upload["fields"],
                expires_at=upload["expires_at"],
            ),
            replayed=replayed,
        )


class VerifyMediaUploadIntent(graphene.Mutation):
    class Arguments:
        intent_id = graphene.ID(required=True)
        idempotency_key = graphene.String(required=True)

    intent = graphene.Field(MediaUploadIntentType, required=True)
    replayed = graphene.Boolean(required=True)

    @classmethod
    def mutate(cls, root, info, intent_id, idempotency_key):
        actor = require_active_user(info)
        try:
            intent, replayed = verify_upload(actor, intent_id, idempotency_key)
        except (
            MediaInputError,
            MediaStateConflict,
            IdempotencyConflict,
            StorageOperationError,
        ) as error:
            _raise_media_graphql_error(error)
        return cls(intent=intent, replayed=replayed)


class AttachVerifiedMedia(graphene.Mutation):
    class Arguments:
        intent_id = graphene.ID(required=True)
        idempotency_key = graphene.String(required=True)

    attachment = graphene.Field(ManagedMediaAttachmentType, required=True)
    replayed = graphene.Boolean(required=True)

    @classmethod
    def mutate(cls, root, info, intent_id, idempotency_key):
        actor = require_active_user(info)
        try:
            attachment, replayed = attach_verified_media(actor, intent_id, idempotency_key)
        except (MediaInputError, MediaStateConflict, IdempotencyConflict) as error:
            _raise_media_graphql_error(error)
        return cls(attachment=attachment, replayed=replayed)


class ExpireMediaUploadIntent(graphene.Mutation):
    class Arguments:
        intent_id = graphene.ID(required=True)
        idempotency_key = graphene.String(required=True)

    intent = graphene.Field(MediaUploadIntentType, required=True)
    replayed = graphene.Boolean(required=True)

    @classmethod
    def mutate(cls, root, info, intent_id, idempotency_key):
        actor = require_active_user(info)
        try:
            intent, replayed = expire_upload_intent(actor, intent_id, idempotency_key)
        except (MediaInputError, MediaStateConflict, IdempotencyConflict) as error:
            _raise_media_graphql_error(error)
        return cls(intent=intent, replayed=replayed)


class CleanupMediaUploadIntent(graphene.Mutation):
    class Arguments:
        intent_id = graphene.ID(required=True)
        idempotency_key = graphene.String(required=True)

    intent = graphene.Field(MediaUploadIntentType, required=True)
    deleted = graphene.Boolean(required=True)
    replayed = graphene.Boolean(required=True)

    @classmethod
    def mutate(cls, root, info, intent_id, idempotency_key):
        actor = require_active_user(info)
        try:
            intent, deleted, replayed = cleanup_media_object(actor, intent_id, idempotency_key)
        except (MediaInputError, MediaStateConflict, IdempotencyConflict, StorageOperationError) as error:
            _raise_media_graphql_error(error)
        return cls(intent=intent, deleted=deleted, replayed=replayed)


class ObtainJSONWebToken(graphene.Mutation):
    payload = GenericScalar(required=True)
    token = graphene.String(required=True)
    refresh_token = graphene.String(required=True)
    refresh_expires_in = graphene.Int(required=True)
    user = graphene.Field(UserType)

    class Arguments:
        email = graphene.String(required=True)
        password = graphene.String(required=True)

    @classmethod
    def mutate(cls, root, info, email, password):
        user = authenticate(request=info.context, username=email, password=password)
        if user is None or not user.is_active:
            log_authentication_event("login", "denied", context=info.context)
            raise GraphQLError(
                "Invalid credentials",
                extensions={"code": "AUTHENTICATION_FAILED"},
            )
        token_pair = issue_token_pair(user)
        log_authentication_event(
            "login", "succeeded", actor_id=user.pk, context=info.context
        )
        return cls(user=user, **token_pair)


class Refresh(graphene.Mutation):
    payload = GenericScalar(required=True)
    token = graphene.String(required=True)
    refresh_token = graphene.String(required=True)
    refresh_expires_in = graphene.Int(required=True)

    class Arguments:
        refresh_token = graphene.String()

    @classmethod
    def mutate(cls, root, info, refresh_token=None):
        raw_token = refresh_token_from_request(info, refresh_token)
        try:
            return cls(**rotate_refresh_token(raw_token, info.context))
        except TokenLifecycleError as error:
            raise GraphQLError(str(error), extensions={"code": error.code}) from error


class Revoke(graphene.Mutation):
    revoked = graphene.Int(required=True)

    class Arguments:
        refresh_token = graphene.String()

    @classmethod
    def mutate(cls, root, info, refresh_token=None):
        raw_token = refresh_token_from_request(info, refresh_token)
        try:
            return cls(revoked=revoke_refresh_family(raw_token, info.context))
        except TokenLifecycleError as error:
            log_authentication_event("revoke", "denied", context=info.context)
            raise GraphQLError(str(error), extensions={"code": error.code}) from error


class Verify(graphene.Mutation):
    payload = GenericScalar(required=True)

    class Arguments:
        token = graphene.String(required=True)

    @classmethod
    def mutate(cls, root, info, token):
        try:
            payload = decode_access_token(token, info.context)
            log_authentication_event(
                "verify", "succeeded", actor_id=payload["sub"], context=info.context
            )
            return cls(payload=payload)
        except TokenLifecycleError as error:
            log_authentication_event("verify", "denied", context=info.context)
            raise GraphQLError(
                "Invalid access token", extensions={"code": INVALID_TOKEN}
            ) from error
    
class Mutation(graphene.ObjectType):
    token_auth = ObtainJSONWebToken.Field()
    verify_token = Verify.Field()
    refresh_token = Refresh.Field()
    revoke_token = Revoke.Field()
    create_request = CreateRequest.Field()
    create_submission_v3 = CreateSubmissionV3.Field()
    create_media_upload_intent = CreateMediaUploadIntent.Field()
    issue_media_upload_intent = IssueMediaUploadIntent.Field()
    renew_media_upload_intent = RenewMediaUploadIntent.Field()
    verify_media_upload_intent = VerifyMediaUploadIntent.Field()
    attach_verified_media = AttachVerifiedMedia.Field()
    expire_media_upload_intent = ExpireMediaUploadIntent.Field()
    cleanup_media_upload_intent = CleanupMediaUploadIntent.Field()
    create_image = CreateImage.Field()
    approve_request = ApproveRequest.Field()
    delete_request = DeleteRequest.Field()


schema = graphene.Schema(query=Query, mutation=Mutation)
