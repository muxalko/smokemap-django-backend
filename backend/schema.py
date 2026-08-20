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
    ModerationAudit,
    Place,
    Request,
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
from django.core.exceptions import ValidationError
from django.contrib.auth import authenticate
import graphql_geojson
from django.utils import timezone
from django.db import transaction
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

from django.contrib.gis import geos

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
        fields = ('id', 'name', 'description')
    
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
            'image_set',
            'date_created',
            'date_updated',
            'date_approved',
            'approved',
            'approved_comment',
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
        # Querying a list
        return Tag.objects.all()
      
    def resolve_addresses(root, info):
        return Address.objects.filter(place__isnull=False).distinct()
    
    def resolve_images(root, info):
        return Image.objects.filter(place__isnull=False)
    
    # def resolve_images_by_set_id(root, info, set_id):
    #     # Querying a list
    #     return Image.objects.filter(set_id=set_id)

    def resolve_requests(root, info):
        user = require_active_user(info)
        requests = Request.objects.filter(approved=False)
        if is_moderator(user):
            return requests
        return requests.filter(owner=user)

    # @login_required
    def resolve_requests_to_approve(root, info, **kwargs):
        require_moderator(info)
        return Request.objects.filter(approved=False)

    def resolve_request_by_id(root, info, id):
        user = require_active_user(info)
        requests = Request.objects.filter(approved=False)
        if not is_moderator(user):
            requests = requests.filter(owner=user)
        request = requests.filter(pk=id).first()
        if request is None:
            graphql_authorization_error("Submission not found", NOT_FOUND)
        return request

    def resolve_requests_by_name(root, info, name):
        user = require_active_user(info)
        requests = Request.objects.filter(approved=False, name=name)
        if not is_moderator(user):
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
    @transaction.atomic
    def mutate(cls, root, info, input):
        user = require_active_user(info)

        # apply validation steps
        validated = True
        validation_message = ''

        myaddress = Address()
        
        nonAddressMode = False
        # for NonAddressMode we will check the addressString for an array of numbers
        # it will be sent in the following format: [lng,lat]
        if input.address_string.startswith('[') and input.address_string.endswith(']'):
            logger.debug("Found coordinates in the address string, trying to parse it")
            nonAddressMode = True
            tmp = input.address_string[1:][:-1]
            logger.debug(" - raw string: %s",tmp)
            coordinates = tmp.split(',')
            logger.debug(" - converted to array: %s", coordinates)
            myaddress.addressString = "CustomAddress_{}_{}_{}".format(input.name,coordinates[0],coordinates[1])
            myaddress.location = geos.Point((float(coordinates[0]),float(coordinates[1])))
            logger.debug("Saving address: %s",myaddress.location)
            myaddress.save(omit_geocode=True)


        if not nonAddressMode:
            # check if address already exists, if not save as new
            try:
                myaddress = Address.objects.get(addressString=input.address_string)
                # if an address already in the database,
                # there are chances that there is a request or place share the same address and can indicate a duplicate
                # We only allow different place names per one address
                # Try to find request with the same name
                # we should not find any, hence Exception is a good exit
                try:
                    request = Request.objects.filter(name=input.name, address=myaddress)
                    if (len(request) > 0):
                        logger.debug("FOUND DUPLICATE REQUEST !!!")
                        logger.debug("Found request(s): %s", request)
                        validated = False
                        validation_message = "There is already an {} request with the same name.".format("approved" if request[0].approved else "unapproved")
                except Exception as request_e:
                    logger.debug("Validation of Request is OK: %s", request_e)
                
                # In case requests were deleted lets check if that same place already exists
                # we should not find any, hence Exception is a good exit
                try:
                    place = Place.objects.get(name=input.name, address=myaddress)
                    if (len(place) > 0):
                        logger.debug("FOUND DUPLICATE PLACE !!!")
                        logger.debug("Found place(s): %s", place)
                        validated = False
                        validation_message = 'There is already a place with the same name and address.'
                except Exception as place_e:
                    logger.debug("Validation of Place is OK: %s", place_e)

            except Exception as myaddress_e:
                logger.debug(myaddress_e)
                myaddress.addressString = input.address_string
                myaddress.save()
                logger.debug("New address was created: %s", myaddress.addressString)


            if (not validated):
                    raise ValidationError(
                        (validation_message),
                        params={'value': request},
                    )
        
        try:
            category = Category.objects.get(pk=input.category)
        except Exception as e:
            logger.debug("Exception: %s", e)
            raise ValidationError(
                ('Category was not found'),
                params={'value': input.category},
            )

        if (category is not None):
            # logger.debug("Category: ", category)
            request = Request()
            request.name = input.name
            request.category = category
            request.description = input.description
            request.tags = input.tags
            request.website = input.website
            request.address = myaddress
            request.owner = user
        
            # request.imageurl = input.imageurl

            logger.debug("Request(Category: %s, Name: %s, Desc: %s, Address: %s, Tags: %s, Requested by :%s)",
                request.category, request.name, request.description, request.address, request.tags, request.requested_by)
            request.save()

        # if len(input.images) > 1:
        #     for file in  input.images:
        #         logger.debug("Filename: %s", file)
        #         #https://twigstechtips.blogspot.com/2012/04/django-how-to-save-inmemoryuploadedfile.html
        #         path = default_storage.save(file, ContentFile(file.read()))
        #         logger.debug("Saved to %s", path)

        return CreateRequest(request=request)


class DeleteRequest(graphene.Mutation):
    ok = graphene.Boolean()

    class Arguments:
        id = graphene.ID()

    @classmethod
    def mutate(cls, root, info, id):
        actor = require_administrator(info)
        request = Request.objects.filter(pk=id, approved=False).first()
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
        logger.debug("Start approval process for request id: %s", id)

        # apply validation steps
        validated = True
        validation_message = ''
        request = Request.objects.filter(pk=id, approved=False).first()
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
                "Reviewers cannot approve their own submission", FORBIDDEN
            )
        logger.debug(request)

        logger.debug("Check if place already exists: %s %s", request.name, request.address)
        # Check if place already exists
        try:
            place = Place.objects.get(name=request.name)#,address=request.address.id)
            logger.debug("Found: %s", place)
            validated = False
            validation_message = 'There is already a place with the same name ' + place.name
        except Exception as place_e:
            logger.debug(place_e)
        
        if (not validated):
                logger.debug(validation_message)
                raise ValidationError(
                    (validation_message),
                    params={'value': request},
                )
        
        newPlace = Place()
        newPlace.name = request.name
        newPlace.category = request.category
        newPlace.description = request.description
        newPlace.address = request.address
        newPlace.website = request.website

        newPlace.save()
        logger.debug("New place was created: %s", newPlace)

        ##### PROCESS TAGS #####
        # find existing 
        tags = Tag.objects.filter(name__in=request.tags)
        logger.debug("Found tags: %s %s", tags, len(tags))
        # if None found, create as new tags
        if (len(tags)<1):
            # create new tags
            # TODO: check how to make a bulk creation
            for tag in request.tags:
                newTag = Tag()
                newTag.name = tag.lower()
                newTag.save()
                # assign to place
                newPlace.tags.add(newTag)
                logger.debug("New tag was added: %s", newTag)
        else:
            # check for not existing
            for tag in request.tags:
                logger.debug("Check if tag ",tag,"is in tags",tags)
                found = next((x for x in tags if x.name == tag), None)
                if found:
                    logger.debug("Ignore existing tag: %s",tag)
                    # assign to place
                    newPlace.tags.add(found)
                else:
                    # create
                    newTag = Tag()
                    newTag.name = tag.lower()
                    newTag.save()
                    # assign to place
                    newPlace.tags.add(newTag)
                    logger.debug("New tag was added: %s", newTag)

        # tags = Tag.objects.filter(name__in=request.tags)
        # newPlace.tags.set(tags)
        # dct = {name: classthing(name) for name in request.tags}
        
        ##### PROCESS Images #####
        # find existing images and update place_id if not exist
        images = Image.objects.filter(request_id=request.id)
        logger.debug("Found images: %s, length=%s", images, len(images))
        if (len(images)>0):
            for image in images:
                if not image.place_id:
                    image.place_id = newPlace.id
                    image.save()
                    logger.debug("Image %s was updated with place #%s",image.name, image.place_id)
                else:
                    logger.debug("Error: Image %s already has place #%s",image.name, image.place_id)
        else:
            logger.debug("No images found associated with request #%s", request.id)

        # create location for showing on the map
        # location consist of lightweight model for fast showing on the map
        location = Location()
        location.place_id = str(newPlace.id)
        location.name = newPlace.name
        location.category = newPlace.category.id
        location.info = newPlace.description
        location.address = newPlace.address.addressString
        location.geom = newPlace.address.location
        tags = newPlace.tags.values_list('name',flat=True)
        if (tags):
            location.tags = ','.join(tags)
       
        location.save()
        logger.debug("Saved location: %s", location)

        # set request as approved
        request.approved = True
        request.date_approved = timezone.now()
        request.reviewed_by = actor
        request.approved_comment = input.approved_comment

        request.save()

        ModerationAudit.objects.create(
            actor=actor,
            action=ModerationAudit.Action.APPROVE,
            target_type="request",
            target_id=request.pk,
        )

        logger.debug("The request (id=%s) was updated with: %s",request.id, newPlace)

        return ApproveRequest(request=request)

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
    create_image = CreateImage.Field()
    approve_request = ApproveRequest.Field()
    delete_request = DeleteRequest.Field()


schema = graphene.Schema(query=Query, mutation=Mutation)
