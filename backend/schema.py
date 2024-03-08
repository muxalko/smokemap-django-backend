import graphene
import graphql_jwt
from graphene_django import DjangoObjectType
from .models import Category, Tag, Request, Address, Place, Image, Location
from django.core.exceptions import ValidationError
import graphql_geojson
from django.utils import timezone

from graphene.types import generic
from django.contrib.gis import geos
from django.conf import settings

import boto3
from botocore.exceptions import ClientError
from botocore.config import Config
from django.core.management.utils import get_random_string
from .models import CustomUser
from django.contrib.auth.models import Group

import logging
logger = logging.getLogger( __name__ )

##################################TYPES###############################
class UserType(DjangoObjectType):

    # if user is in admins group his role will be 'admin' otherwise 'visitor'
    # that is returned in web client for authorzation of users
    role = graphene.String()
    def resolve_role(self, info):
        customUser = CustomUser.objects.get(email=self)
        groups = Group.objects.filter(user=customUser)
        logger.debug("groups",groups)
        # wrap in list(), because QuerySet is not JSON serializable
        isAdmin = False 
        for group in groups:
            if group.name == "admins":
                isAdmin = True
        if isAdmin:
            return 'admin'
        else:
            return 'guest'
    
    class Meta:
        model = CustomUser
        fields = ('name', 'image', 'email', 'role')

class PlaceType(DjangoObjectType):
    class Meta:
        model = Place
        fields = ('id','name', 'category', 'address', 'description', 'tags', 'image_set')

class CategoryType(DjangoObjectType):
    class Meta:
        model = Category
        fields = ('id', 'name', 'description')
    
    # @classmethod
    # def get_queryset(cls, queryset, info):
    #     logger.debug("CategoryType.info.context.user:",info.context.user)
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
        fields = ('id', 'name', 'url', 'metadata', 'request')

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
    class Meta:
        model = Request
        fields = (
            'id',
            'name',
            'category',
            'description',
            'tags',
            'address',
            'image_set',
            'date_created',
            'date_updated',
            'date_approved',
            'approved',
            'approved_by',
            'approved_comment',
            'requested_by'
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

    s3_presigned_url = generic.GenericScalar(
        # image_type=graphene.String()
        )
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
        # Querying a list
        return Address.objects.all()
    
    def resolve_images(root, info):
        # Querying a list
        return Image.objects.all()
    
    # def resolve_images_by_set_id(root, info, set_id):
    #     # Querying a list
    #     return Image.objects.filter(set_id=set_id)

    # @anonymous_return(AuthenticationRequired.default_message)
    # def resolve_requests(root, info):
    #     # Querying a list
    #     return Request.objects.all()

    # @login_required
    def resolve_requests_to_approve(root, info, **kwargs):
        # logger.debug("context:",vars(info.context))
        logger.debug("headers:",info.context.headers)

        # logger.debug("csrf token:",info.context.headers['X-Csrftoken'])
        if not info.context.user.is_authenticated:
            raise ValidationError(("You must be logged in to perform this action"),)
        # Querying a list
        return Request.objects.filter(approved=False)

    def resolve_request_by_id(root, info, id):
        if not info.context.user.is_authenticated:
            raise ValidationError(("You must be logged in to perform this action"),)
        # Querying a request
        return Request.objects.get(pk=id)

    def resolve_requests_by_name(root, info, name):
        if not info.context.user.is_authenticated:
            raise ValidationError(("You must be logged in to perform this action"),)
        # Querying a named
        return Request.objects.filter(name=name)

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
        logger.debug("Creating Boto3 client for S3 manipulations")
        s3_client = boto3.client("s3",
                                    aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                                    aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY, 
                                    region_name=settings.AWS_S3_REGION_NAME,
                                    config=Config(signature_version='s3v4'))
        
        object_name = get_random_string(length=16, allowed_chars='0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz')
        logger.debug("Generated name for S3 upload object", object_name)
        
        try:
            response = s3_client.generate_presigned_post(settings.AWS_STORAGE_BUCKET_NAME,
                                                        # object_name,
                                                        # ${filename} will be supplied by the client
                                                        object_name+"/${filename}",
                                                        # "uploads/${filename}",
                                                        # Fields=None,
                                                        Fields = {"Content-Type": "image/jpeg"},
                                                        # Fields = {"acl": "public-read", "Content-Type": "image/jpeg"},
                                                        # Fields={"Content-Type": "image/{}".format(image_type)},
                                                        # Conditions=None,
                                                        Conditions=[
                                                            # {"acl": "public-read" },
                                                            {"bucket": settings.AWS_STORAGE_BUCKET_NAME },
                                                            ["starts-with", "$key", object_name+"/"],
                                                            ["starts-with", "$Content-Type", "image/"]
                                                            ],
                                                        # Conditions=[["starts-with", "$key", "uploads/"]],
                                                        ExpiresIn=60)
        except ClientError as e:
            logger.debug(e)
            return None
        print (response)
        # The response contains the presigned URL and required fields
       
        return response
     
class RequestInput(graphene.InputObjectType):
    name = graphene.String()
    category = graphene.String()
    description = graphene.String()
    address_string = graphene.String()
    tags = graphene.List(graphene.String)
    # images = graphene.List(Upload)
    

class CreateRequest(graphene.Mutation):
    class Arguments:
        input = RequestInput(required=True)

    request = graphene.Field(RequestType)
    
    @classmethod
    def mutate(cls, root, info, input):
        
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
            logger.debug(" - raw string",tmp)
            coordinates = tmp.split(',')
            logger.debug(" - converted to array", coordinates)
            myaddress.addressString = "CustomAddress_{}_{}_{}".format(input.name,coordinates[0],coordinates[1])
            myaddress.location = geos.Point((float(coordinates[0]),float(coordinates[1])))
            logger.debug("Saving {}".format(myaddress.location))
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
                        logger.debug("Found request(s): ", request)
                        validated = False
                        validation_message = "There is already an {} request with the same name.".format("approved" if request[0].approved else "unapproved")
                except Exception as request_e:
                    logger.debug("Validation of Request is OK: ", request_e)
                
                # In case requests were deleted lets check if that same place already exists
                # we should not find any, hence Exception is a good exit
                try:
                    place = Place.objects.get(name=input.name, address=myaddress)
                    if (len(place) > 0):
                        logger.debug("FOUND DUPLICATE PLACE !!!")
                        logger.debug("Found place(s): ", place)
                        validated = False
                        validation_message = 'There is already a place with the same name and address.'
                except Exception as place_e:
                    logger.debug("Validation of Place is OK: ", place_e)

            except Exception as myaddress_e:
                logger.debug(myaddress_e)
                myaddress.addressString = input.address_string
                myaddress.save()
                logger.debug("New address was created: ", myaddress)


            if (not validated):
                    raise ValidationError(
                        (validation_message),
                        params={'value': request},
                    )
        
        try:
            category = Category.objects.get(pk=input.category)
        except Exception as e:
            logger.debug("Exception: ", e)
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
            request.address = myaddress
            request.requested_by = info.context.META.get('HTTP_X_FORWARDED_FOR', info.context.META.get('REMOTE_ADDR', '')).split(',')[0].strip()
        
            # request.imageurl = input.imageurl

            logger.debug("Request(Category:{}, Name: {}, Desc: {}, Address: {}, Tags: {}, Requested by :{})".format(
                request.category, request.name, request.description, request.address, request.tags, request.requested_by))
            request.save()

        # if len(input.images) > 1:
        #     for file in  input.images:
        #         logger.debug("Filename: ", file)
        #         #https://twigstechtips.blogspot.com/2012/04/django-how-to-save-inmemoryuploadedfile.html
        #         path = default_storage.save(file, ContentFile(file.read()))
        #         logger.debug("Saved to ", path)

        return CreateRequest(request=request)


class DeleteRequest(graphene.Mutation):
    ok = graphene.Boolean()

    class Arguments:
        id = graphene.ID()

    @classmethod
    def mutate(cls, root, info, id):
        if not info.context.user.is_authenticated:
            raise ValidationError(("You must be logged in to perform this action"),)
        request = Request.objects.get(pk=id)

        request.delete()
        return cls(ok=True)


class RequestApproveInput(graphene.InputObjectType):
    approved_comment = graphene.String()
    approved_by = graphene.String()


class ApproveRequest(graphene.Mutation):

    class Arguments:
        input = RequestApproveInput(required=True)
        id = graphene.ID()

    request = graphene.Field(RequestType)
    
    @classmethod
    def mutate(cls, root, info, input, id):
        logger.debug("Start approval process for request id", id)
        # if not info.context['user']['is_authenticated']:
        if not info.context.user.is_authenticated:
            raise ValidationError(("You must be logged in to perform this action"),)
        
        # apply validation steps
        validated = True
        validation_message = ''
        request = Request.objects.get(pk=id)
        logger.debug(request)
        if request.approved:
            raise ValidationError(
                ('The request has already been approved.'),
                params={'approved_by': request.approved_by},
            )

        logger.debug("Check if place already exists: ", request.name, request.address)
        # Check if place already exists
        try:
            place = Place.objects.get(name=request.name)#,address=request.address.id)
            logger.debug("Found: ", place)
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

        newPlace.save()
        logger.debug("New place was created: ", newPlace)

        ##### PROCESS TAGS #####
        # find existing 
        tags = Tag.objects.filter(name__in=request.tags)
        logger.debug("Found tags: ", tags, len(tags))
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
                logger.debug("New tag was added:", newTag)
        else:
            # check for not existing
            for tag in request.tags:
                logger.debug("Check if tag ",tag,"is in tags",tags)
                found = next((x for x in tags if x.name == tag), None)
                if found:
                    logger.debug("Ignore existing tag:",tag)
                    # assign to place
                    newPlace.tags.add(found)
                else:
                    # create
                    newTag = Tag()
                    newTag.name = tag.lower()
                    newTag.save()
                    # assign to place
                    newPlace.tags.add(newTag)
                    logger.debug("New tag was added:", newTag)

        # tags = Tag.objects.filter(name__in=request.tags)
        # newPlace.tags.set(tags)
        # dct = {name: classthing(name) for name in request.tags}
        
        ##### PROCESS Images #####
        # find existing images and update place_id if not exist
        images = Image.objects.filter(request_id=request.id)
        logger.debug("Found images: ", images, len(images))
        if (len(images)>0):
            for image in images:
                if not image.place_id:
                    image.place_id = newPlace.id
                    image.save()
                    logger.debug("Image {} was updated with place # {}".format(image.name, image.place_id))
                else:
                    logger.debug("Error: Image {} already has place # {}".format(image.name, image.place_id))
        else:
            logger.debug("No images found associated with request #", request.id)

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
        logger.debug("Saved location", location)

        # set request as approved
        request.approved = True
        request.date_approved = timezone.now()
        request.approved_by = input.approved_by
        request.approved_comment = input.approved_comment

        request.save()

        logger.debug("The request (id={}) was updated ".format(request.id), newPlace)

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
        
        image = Image()
        image.request = Request.objects.get(pk=input.request_id)
        image.name = input.name
        image.url = input.url
        image.metadata = input.metadata
        image.save()

        return cls(image=image)


class ObtainJSONWebToken(graphql_jwt.JSONWebTokenMutation):
    user = graphene.Field(UserType)

    @classmethod
    def resolve(cls, root, info, **kwargs):
        logger.debug("context user:", info.context.user)
        return cls(user=info.context.user)
    
class Mutation(graphene.ObjectType):
    token_auth = ObtainJSONWebToken.Field()
    verify_token = graphql_jwt.Verify.Field()
    refresh_token = graphql_jwt.Refresh.Field()
    revoke_token = graphql_jwt.Revoke.Field()
    create_request = CreateRequest.Field()
    create_image = CreateImage.Field()
    approve_request = ApproveRequest.Field()
    delete_request = DeleteRequest.Field()


schema = graphene.Schema(query=Query, mutation=Mutation)
