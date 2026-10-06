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
    PublicMediaRendition,
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
from django.db.models import Prefetch, Q
from django.db.models.functions import Lower
from .submissions import (
    IdempotencyConflict,
    SubmissionInputError,
    SubmissionOperationError,
    SubmissionSnapshot,
    create_submission,
    edit_submission,
    finalize_submission,
    reorder_attached_media,
    submission_media_state,
)
from .media import (
    MediaInputError,
    MediaStateConflict,
    attach_verified_media,
    cleanup_media_object,
    create_upload_intent,
    expire_upload_intent,
    issue_media_preview,
    issue_upload,
    remove_attached_media,
    verify_upload,
)
from .media_storage import StorageOperationError
from .moderation import (
    approve_submission,
    hard_delete_submission,
    reject_submission,
    withdraw_submission,
)
from .public_media import published_media_prefetch, revoke_public_media
from .moderation_queue import (
    LEGACY_PAGE_SIZE,
    ModerationQueueInputError,
    moderation_queue_page,
    pending_moderation_queryset,
)
from .place_search import (
    PlaceSearchInputError,
    normalize_search_query,
)
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
import warnings
from django.urls import reverse
logger = logging.getLogger( __name__ )

LEGACY_PUBLIC_PLACE_LIMIT = 20
PLACES_DEPRECATION_REASON = (
    "Unbounded; use GET /api/v1/places/search/ for name search or "
    "GET /api/v1/places/ for viewport reads."
)

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

class PublicMediaRenditionType(graphene.ObjectType):
    public_id = graphene.UUID(required=True)
    url = graphene.String(required=True)
    position = graphene.Int(required=True)
    mime_type = graphene.String(required=True)
    byte_size = graphene.Int(required=True)
    width = graphene.Int(required=True)
    height = graphene.Int(required=True)

    def resolve_url(self, info):
        return reverse(
            "public-media-rendition", kwargs={"public_id": self.public_id}
        )


class PlaceType(DjangoObjectType):
    media = graphene.List(
        graphene.NonNull(PublicMediaRenditionType), required=True
    )

    def resolve_media(self, info):
        prefetched = getattr(self, "_published_media", None)
        if prefetched is not None:
            return prefetched
        return self.public_media.filter(
            state=PublicMediaRendition.State.PUBLISHED
        ).order_by("position", "pk")

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
    media_intent_id = graphene.ID(required=True)

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

    def resolve_media_intent_id(self, info):
        return str(self.intent_id)


class ModerationMediaAttachmentV4(graphene.ObjectType):
    """Minimum safe identity needed to preview one queued attachment."""

    id = graphene.ID(required=True)
    position = graphene.Int(required=True)


class MediaUploadAuthorization(graphene.ObjectType):
    url = graphene.String(required=True)
    fields = GenericScalar(required=True)
    expires_at = graphene.DateTime(required=True)


class MediaPreviewAuthorization(graphene.ObjectType):
    """A short-lived, exact-object GET capability. Never a bucket, key, or credential."""

    url = graphene.String(required=True)
    expires_at = graphene.DateTime(required=True)

with warnings.catch_warnings():
    # graphql_geojson nests addressString under properties, which graphene's
    # top-level field validation misreports as an unknown model field.
    warnings.filterwarnings(
        "ignore",
        message='Field name "addressString" matches an attribute',
        category=UserWarning,
    )

    class AddressType(graphql_geojson.GeoJSONType):
        class Meta:
            model = Address
            geojson_field = 'location'
            # Public places share this type, so reverse relations such as
            # requestSet must never become queryable submission metadata.
            fields = ('id', 'addressString', 'location')

class RequestType(DjangoObjectType):
    state = graphene.String(required=True)
    tags = graphene.List(graphene.NonNull(graphene.String), required=True)
    attachments = graphene.List(
        graphene.NonNull(ModerationMediaAttachmentV4),
        required=True,
        description=(
            "Pending managed attachment identities in display order. Use each ID "
            "with mediaAttachmentPreviewV3; storage metadata is never exposed."
        ),
    )
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

    def resolve_attachments(self, info):
        actor = require_active_user(info)
        if self.state != Request.State.PENDING:
            return []
        if self.owner_id != actor.pk and not is_moderator(actor):
            graphql_authorization_error("Submission not found", NOT_FOUND)
        prefetched = getattr(self, "_moderation_attachments", None)
        if prefetched is not None:
            return prefetched
        return self.image_set.filter(
            is_managed=True,
            state="attached",
        ).order_by("position", "pk")

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


class ModerationQueuePageV4(graphene.ObjectType):
    items = graphene.List(graphene.NonNull(RequestType), required=True)
    has_next_page = graphene.Boolean(required=True)
    next_cursor = graphene.String()
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


def request_queryset_with_tags(queryset, *, include_attachments=False):
    prefetches = [
        Prefetch(
            "request_tags",
            queryset=RequestTag.objects.select_related("tag").order_by("position"),
        )
    ]
    if include_attachments:
        prefetches.append(
            Prefetch(
                "image_set",
                queryset=Image.objects.filter(
                    is_managed=True,
                    state="attached",
                ).order_by("position", "pk"),
                to_attr="_moderation_attachments",
            )
        )
    return queryset.prefetch_related(*prefetches)


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
    moderation_queue_v4 = graphene.Field(
        ModerationQueuePageV4,
        first=graphene.Int(),
        after=graphene.String(),
        required=True,
    )
    request_by_id = graphene.Field(
        RequestType,
        id=graphene.ID()
    )
    requests_by_name = graphene.Field(
        RequestType,
        name=graphene.String()
    )

    submission_media_state_v3 = graphene.Field(
        lambda: SubmissionMediaStateType,
        submission_id=graphene.ID(required=True),
    )

    media_attachment_preview_v3 = graphene.Field(
        MediaPreviewAuthorization,
        attachment_id=graphene.ID(required=True),
    )

    places = graphene.List(
        PlaceType,
        deprecation_reason=PLACES_DEPRECATION_REASON,
    )

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
        moderator = is_moderator(user)
        requests = request_queryset_with_tags(
            Request.objects.exclude(state=Request.State.APPROVED),
            include_attachments=moderator,
        )
        if moderator:
            return requests.filter(
                Q(owner=user) | Q(state=Request.State.PENDING)
            )
        return requests.filter(owner=user)

    # @login_required
    def resolve_requests_to_approve(root, info, **kwargs):
        require_moderator(info)
        return request_queryset_with_tags(
            pending_moderation_queryset(),
            include_attachments=True,
        ).order_by("date_created", "pk")[:LEGACY_PAGE_SIZE]

    def resolve_moderation_queue_v4(root, info, first=None, after=None):
        require_moderator(info)
        try:
            return moderation_queue_page(
                first=first,
                after=after,
                queryset=request_queryset_with_tags(
                    pending_moderation_queryset(),
                    include_attachments=True,
                ),
            )
        except ModerationQueueInputError as error:
            graphql_authorization_error(str(error), error.code)

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

    def resolve_submission_media_state_v3(root, info, submission_id):
        actor = require_active_user(info)
        try:
            return submission_media_state(actor, submission_id)
        except Exception as error:
            _raise_submission_graphql_error(error, "SUBMISSION_MEDIA_STATE_FAILED")

    def resolve_media_attachment_preview_v3(root, info, attachment_id):
        actor = require_active_user(info)
        try:
            _image, url, expires_at = issue_media_preview(actor, attachment_id)
        except (MediaStateConflict, StorageOperationError) as error:
            _raise_media_graphql_error(error)
        return MediaPreviewAuthorization(url=url, expires_at=expires_at)

    def resolve_places(root, info):
        # Deprecated and intentionally unbounded until smokemap-webapp#10
        # moves clients to the bounded search API (#105).
        return Place.objects.prefetch_related(published_media_prefetch())
    
    def resolve_places_names(root, info):
        # Deprecated compatibility surface for the old client-side search.
        return Place.objects.order_by(Lower("name"), "pk").values_list(
            "name", flat=True
        )[:LEGACY_PUBLIC_PLACE_LIMIT]
    
    def resolve_place_by_id(root, info, id):
        # Querying a list
        return Place.objects.get(pk=id)
    
    def resolve_places_by_name(root, info, name):
        return (
            Place.objects.filter(name=name)
            .order_by("pk")
            .prefetch_related(published_media_prefetch())[:LEGACY_PUBLIC_PLACE_LIMIT]
        )
    
    def resolve_places_startWith_name(root, info, name):
        try:
            normalized_query = normalize_search_query(name)
        except PlaceSearchInputError as error:
            raise GraphQLError(str(error), extensions={"code": error.code}) from error
        return (
            Place.objects.annotate(normalized_name=Lower("name"))
            .filter(normalized_name__startswith=normalized_query)
            .prefetch_related(published_media_prefetch())
            .order_by("normalized_name", "pk")[:LEGACY_PUBLIC_PLACE_LIMIT]
        )
    
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


class SubmissionV3SnapshotType(graphene.ObjectType):
    id = graphene.ID(required=True)
    state = graphene.String(required=True)
    name = graphene.String(required=True)
    category_slug = graphene.String(required=True)
    longitude = graphene.Float(required=True)
    latitude = graphene.Float(required=True)
    address_label = graphene.String()
    tags = graphene.List(graphene.NonNull(graphene.String), required=True)
    description = graphene.String()
    website = graphene.String()

    @staticmethod
    def resolve_id(root: SubmissionSnapshot, info):
        return str(root.submission_id)

    @staticmethod
    def resolve_tags(root: SubmissionSnapshot, info):
        return list(root.tags)


class SubmissionMediaStateType(graphene.ObjectType):
    """Owner-safe resume read model: no bucket, key, credential, or owner id."""

    submission = graphene.Field(SubmissionV3SnapshotType, required=True)
    attachments = graphene.List(
        graphene.NonNull(ManagedMediaAttachmentType), required=True
    )
    media_intents = graphene.List(
        graphene.NonNull(MediaUploadIntentType), required=True
    )

    @staticmethod
    def resolve_attachments(root, info):
        return list(root.attachments)

    @staticmethod
    def resolve_media_intents(root, info):
        return list(root.media_intents)


def _submission_v3_raw_input(input):
    return {
        "name": input.name,
        "category_slug": input.category_slug,
        "longitude": input.longitude,
        "latitude": input.latitude,
        "address_label": getattr(input, "address_label", None),
        "tags": getattr(input, "tags", None),
        "description": getattr(input, "description", None),
        "website": getattr(input, "website", None),
    }


class CreateSubmissionV3(graphene.Mutation):
    class Arguments:
        idempotency_key = graphene.String(required=True)
        input = SubmissionV3Input(required=True)

    submission = graphene.Field(RequestType, required=True)
    replayed = graphene.Boolean(required=True)

    @classmethod
    def mutate(cls, root, info, idempotency_key, input):
        actor = require_active_user(info)
        try:
            submission, replayed = create_submission(
                actor,
                idempotency_key,
                _submission_v3_raw_input(input),
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


def _raise_submission_graphql_error(error, failure_code):
    """Map service outcomes onto stable, non-sensitive GraphQL error codes."""
    if isinstance(error, SubmissionInputError):
        raise GraphQLError(
            str(error),
            extensions={"code": "INVALID_SUBMISSION", "field": error.field},
        ) from error
    if isinstance(error, IdempotencyConflict):
        raise GraphQLError(
            str(error),
            extensions={"code": "IDEMPOTENCY_CONFLICT"},
        ) from error
    if isinstance(error, SubmissionOperationError):
        raise GraphQLError(str(error), extensions={"code": error.code}) from error
    logger.exception("Submission operation failed after validation")
    raise GraphQLError(
        "Submission could not be updated",
        extensions={"code": failure_code},
    ) from error


class EditSubmissionV3(graphene.Mutation):
    class Arguments:
        submission_id = graphene.ID(required=True)
        idempotency_key = graphene.String(required=True)
        input = SubmissionV3Input(required=True)

    submission = graphene.Field(SubmissionV3SnapshotType, required=True)
    replayed = graphene.Boolean(required=True)

    @classmethod
    def mutate(cls, root, info, submission_id, idempotency_key, input):
        actor = require_active_user(info)
        try:
            submission, replayed = edit_submission(
                actor,
                submission_id,
                idempotency_key,
                _submission_v3_raw_input(input),
            )
        except Exception as error:
            _raise_submission_graphql_error(error, "SUBMISSION_EDIT_FAILED")
        return cls(submission=submission, replayed=replayed)


class FinalizeSubmissionV3(graphene.Mutation):
    class Arguments:
        submission_id = graphene.ID(required=True)
        idempotency_key = graphene.String(required=True)

    submission = graphene.Field(SubmissionV3SnapshotType, required=True)
    replayed = graphene.Boolean(required=True)

    @classmethod
    def mutate(cls, root, info, submission_id, idempotency_key):
        actor = require_active_user(info)
        try:
            submission, replayed = finalize_submission(
                actor, submission_id, idempotency_key
            )
        except Exception as error:
            _raise_submission_graphql_error(error, "SUBMISSION_FINALIZE_FAILED")
        return cls(submission=submission, replayed=replayed)


class ReorderSubmissionMediaV3(graphene.Mutation):
    class Arguments:
        submission_id = graphene.ID(required=True)
        idempotency_key = graphene.String(required=True)
        attachment_ids = graphene.List(graphene.NonNull(graphene.ID), required=True)

    ordered_attachment_ids = graphene.List(graphene.NonNull(graphene.ID), required=True)
    replayed = graphene.Boolean(required=True)

    @classmethod
    def mutate(cls, root, info, submission_id, idempotency_key, attachment_ids):
        actor = require_active_user(info)
        try:
            ordered_ids, replayed = reorder_attached_media(
                actor, submission_id, idempotency_key, attachment_ids
            )
        except Exception as error:
            _raise_submission_graphql_error(error, "SUBMISSION_REORDER_MEDIA_FAILED")
        return cls(ordered_attachment_ids=list(ordered_ids), replayed=replayed)


class WithdrawSubmissionV4(graphene.Mutation):
    class Arguments:
        submission_id = graphene.ID(required=True)
        idempotency_key = graphene.String(required=True)

    submission = graphene.Field(RequestType, required=True)
    replayed = graphene.Boolean(required=True)

    @classmethod
    def mutate(cls, root, info, submission_id, idempotency_key):
        actor = require_active_user(info)
        try:
            result = withdraw_submission(actor, submission_id, idempotency_key)
        except Exception as error:
            _raise_submission_graphql_error(error, "SUBMISSION_WITHDRAW_FAILED")
        return cls(submission=result.submission, replayed=result.replayed)


class ReviewSubmissionV4Input(graphene.InputObjectType):
    comment = graphene.String()


class ApproveSubmissionV4(graphene.Mutation):
    class Arguments:
        submission_id = graphene.ID(required=True)
        idempotency_key = graphene.String(required=True)
        input = ReviewSubmissionV4Input()

    submission = graphene.Field(RequestType, required=True)
    place = graphene.Field(PlaceType, required=True)
    replayed = graphene.Boolean(required=True)

    @classmethod
    def mutate(cls, root, info, submission_id, idempotency_key, input=None):
        actor = require_moderator(info)
        try:
            result = approve_submission(
                actor,
                submission_id,
                idempotency_key,
                getattr(input, "comment", None),
            )
        except Exception as error:
            _raise_submission_graphql_error(error, "SUBMISSION_APPROVE_FAILED")
        return cls(
            submission=result.submission,
            place=result.place,
            replayed=result.replayed,
        )


class RejectSubmissionV4(graphene.Mutation):
    class Arguments:
        submission_id = graphene.ID(required=True)
        idempotency_key = graphene.String(required=True)
        input = ReviewSubmissionV4Input()

    submission = graphene.Field(RequestType, required=True)
    replayed = graphene.Boolean(required=True)

    @classmethod
    def mutate(cls, root, info, submission_id, idempotency_key, input=None):
        actor = require_moderator(info)
        try:
            result = reject_submission(
                actor,
                submission_id,
                idempotency_key,
                getattr(input, "comment", None),
            )
        except Exception as error:
            _raise_submission_graphql_error(error, "SUBMISSION_REJECT_FAILED")
        return cls(submission=result.submission, replayed=result.replayed)


class RevokePublicMediaV4(graphene.Mutation):
    class Arguments:
        public_id = graphene.UUID(required=True)
        idempotency_key = graphene.String(required=True)

    public_id = graphene.UUID(required=True)
    state = graphene.String(required=True)
    replayed = graphene.Boolean(required=True)

    @classmethod
    def mutate(cls, root, info, public_id, idempotency_key):
        actor = require_administrator(info)
        try:
            result = revoke_public_media(actor, public_id, idempotency_key)
        except Exception as error:
            _raise_submission_graphql_error(error, "PUBLIC_MEDIA_REVOKE_FAILED")
        return cls(
            public_id=result.rendition.public_id,
            state=result.rendition.state,
            replayed=result.replayed,
        )


class DeleteRequest(graphene.Mutation):
    ok = graphene.Boolean()

    class Arguments:
        id = graphene.ID()

    @classmethod
    def mutate(cls, root, info, id):
        actor = require_administrator(info)
        try:
            hard_delete_submission(actor, id)
        except Exception as error:
            _raise_submission_graphql_error(error, "SUBMISSION_DELETE_FAILED")
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


class RemoveAttachedMedia(graphene.Mutation):
    class Arguments:
        intent_id = graphene.ID(required=True)
        idempotency_key = graphene.String(required=True)

    intent = graphene.Field(MediaUploadIntentType, required=True)
    replayed = graphene.Boolean(required=True)

    @classmethod
    def mutate(cls, root, info, intent_id, idempotency_key):
        actor = require_active_user(info)
        try:
            intent, replayed = remove_attached_media(actor, intent_id, idempotency_key)
        except (MediaInputError, MediaStateConflict, IdempotencyConflict) as error:
            _raise_media_graphql_error(error)
        return cls(intent=intent, replayed=replayed)


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
    edit_submission_v3 = EditSubmissionV3.Field()
    finalize_submission_v3 = FinalizeSubmissionV3.Field()
    reorder_submission_media_v3 = ReorderSubmissionMediaV3.Field()
    withdraw_submission_v4 = WithdrawSubmissionV4.Field()
    approve_submission_v4 = ApproveSubmissionV4.Field()
    reject_submission_v4 = RejectSubmissionV4.Field()
    revoke_public_media_v4 = RevokePublicMediaV4.Field()
    create_media_upload_intent = CreateMediaUploadIntent.Field()
    issue_media_upload_intent = IssueMediaUploadIntent.Field()
    renew_media_upload_intent = RenewMediaUploadIntent.Field()
    verify_media_upload_intent = VerifyMediaUploadIntent.Field()
    attach_verified_media = AttachVerifiedMedia.Field()
    remove_attached_media = RemoveAttachedMedia.Field()
    expire_media_upload_intent = ExpireMediaUploadIntent.Field()
    cleanup_media_upload_intent = CleanupMediaUploadIntent.Field()
    create_image = CreateImage.Field()
    approve_request = ApproveRequest.Field()
    delete_request = DeleteRequest.Field()


schema = graphene.Schema(query=Query, mutation=Mutation)
