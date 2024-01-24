import graphene
from graphene_django import DjangoObjectType
from .models import Category, Tag, Request, Address, Place, Image
from django.core.exceptions import ValidationError
import graphql_geojson
from django.utils import timezone
# from django.contrib.gis import geos
from graphene_file_upload.scalars import Upload
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from graphene.types import generic
from django.contrib.gis import geos
from django.conf import settings
# import logging
import boto3
from botocore.exceptions import ClientError
from botocore.config import Config
from django.core.management.utils import get_random_string

class PlaceType(DjangoObjectType):
    class Meta:
        model = Place
        fields = ('id','name', 'category', 'address', 'description', 'tags', 'image_set')

class CategoryType(DjangoObjectType):
    class Meta:
        model = Category
        fields = ('id', 'name', 'description')

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
        )

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

    s3_presigned_url = generic.GenericScalar()
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

    def resolve_requests(root, info):
        # Querying a list
        return Request.objects.all()

    def resolve_requests_to_approve(root, info, **kwargs):
        # Querying a list
        return Request.objects.filter(approved=False)

    def resolve_request_by_id(root, info, id):
        # Querying a request
        return Request.objects.get(pk=id)

    def resolve_requests_by_name(root, info, name):
        # Querying a named
        return Request.objects.filter(name=name)

    def resolve_places(root, info):
        # Querying a list
        return Place.objects.all()
    
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
        print("Creating Boto3 client for S3 manipulations")
        s3_client = boto3.client("s3",
                                    aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                                    aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY, 
                                    region_name=settings.AWS_S3_REGION_NAME,
                                    config=Config(signature_version='s3v4'))
        
        object_name = get_random_string(length=16, allowed_chars='0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz')
        print ("Generated name for S3 upload object", object_name)
        try:
            response = s3_client.generate_presigned_post(settings.AWS_STORAGE_BUCKET_NAME,
                                                        # object_name,
                                                        # ${filename} will be supplied by the client
                                                        object_name+"/${filename}",
                                                        # "uploads/${filename}",
                                                        Fields=None,
                                                        # Conditions=None,
                                                        Conditions=[["starts-with", "$key", object_name+"/"]],
                                                        # Conditions=[["starts-with", "$key", "uploads/"]],
                                                        ExpiresIn=60)
        except ClientError as e:
            print(e)
            return None
        print (response)
        # The response contains the presigned URL and required fields
       
        return response
       
      
class CreatePlace(graphene.Mutation):
    place = graphene.Field(PlaceType)

    class Arguments:
        name = graphene.String(required=True)
        category = graphene.Int(required=False)
        description = graphene.String(required=False)
        # location = graphql_geojson.Geometry(required=True)
        address = graphene.Field(AddressType)
        tags = graphene.List(TagType, required=False)

    @classmethod
    def mutate(cls, root, info, **args):
        place = Place.objects.create(**args)
        return cls(place=place)

class UpdateCategory(graphene.Mutation):
    class Arguments:
        # Mutation to update a category
        description = graphene.String(required=False)
        name = graphene.String(required=False)
        id = graphene.ID()

    category = graphene.Field(CategoryType)

    @classmethod
    def mutate(cls, root, info, id, name, description):
        category = Category.objects.get(pk=id)
        category.name = name
        category.description = description
        category.save()

        return UpdateCategory(category=category)


class CreateCategory(graphene.Mutation):
    class Arguments:
        # Mutation to create a category
        name = graphene.String(required=True)
        description = graphene.String(required=False)


    # Class attributes define the response of the mutation
    category = graphene.Field(CategoryType)

    @classmethod
    def mutate(cls, root, info, name, description):
        category = Category()
        category.name = name
        category.description = description
        category.save()

        return CreateCategory(category=category)

class UploadFile(graphene.Mutation):
    class Arguments:
        file = Upload(required=True)

    success = graphene.Boolean()

    def mutate(self, info, file, **kwargs):
        print("Filename: ", file)
        print(f'File Size in Bytes is {file.size}')
        #https://twigstechtips.blogspot.com/2012/04/django-how-to-save-inmemoryuploadedfile.html
        path = default_storage.save(file, ContentFile(file.read()))
        print("Saved to ", path)

        return UploadFile(success=True)

class UploadFiles(graphene.Mutation):
    class Arguments:
        files = graphene.List(Upload)

    success = graphene.Boolean()

    def mutate(self, info, files, **kwargs):
        for file in files:
            print("Filename: ", file)
            print(f'File Size in Bytes is {file.size}')
            #https://twigstechtips.blogspot.com/2012/04/django-how-to-save-inmemoryuploadedfile.html
            path = default_storage.save(file, ContentFile(file.read()))
            print("Saved to ", path)

        return UploadFiles(success=True)

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
            print("Found coordinates in the address string, trying to parse it")
            nonAddressMode = True
            tmp = input.address_string[1:][:-1]
            print(" - raw string",tmp)
            coordinates = tmp.split(',')
            print(" - converted to array", coordinates)
            myaddress.addressString = "CustomAddress_{}_{}_{}".format(input.name,coordinates[0],coordinates[1])
            myaddress.location = geos.Point((float(coordinates[0]),float(coordinates[1])))
            print("Saving {}".format(myaddress.location))
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
                        print("FOUND DUPLICATE REQUEST !!!")
                        print("Found request(s): ", request)
                        validated = False
                        validation_message = "There is already an {} request with the same name.".format("approved" if request[0].approved else "unapproved")
                except Exception as request_e:
                    print("Validation of Request is OK: ", request_e)
                
                # In case requests were deleted lets check if that same place already exists
                # we should not find any, hence Exception is a good exit
                try:
                    place = Place.objects.get(name=input.name, address=myaddress)
                    if (len(place) > 0):
                        print("FOUND DUPLICATE PLACE !!!")
                        print("Found place(s): ", place)
                        validated = False
                        validation_message = 'There is already a place with the same name and address.'
                except Exception as place_e:
                    print("Validation of Place is OK: ", place_e)

            except Exception as myaddress_e:
                print(myaddress_e)
                myaddress.addressString = input.address_string
                myaddress.save()
                print("New address was created: ", myaddress)


            if (not validated):
                    raise ValidationError(
                        (validation_message),
                        params={'value': request},
                    )
        
        try:
            category = Category.objects.get(pk=input.category)
        except Exception as e:
            print("Exception: ", e)
            raise ValidationError(
                ('Category was not found'),
                params={'value': input.category},
            )

        if (category is not None):
            # print("Category: ", category)
            request = Request()
            request.name = input.name
            request.category = category
            request.description = input.description
            request.tags = input.tags
            request.address = myaddress
            # request.imageurl = input.imageurl

            print("Request(Category:{}, Name: {}, Desc: {}, Address: {}, Tags: {})".format(
                request.category, request.name, request.description, request.address, request.tags))
            request.save()

        # if len(input.images) > 1:
        #     for file in  input.images:
        #         print("Filename: ", file)
        #         #https://twigstechtips.blogspot.com/2012/04/django-how-to-save-inmemoryuploadedfile.html
        #         path = default_storage.save(file, ContentFile(file.read()))
        #         print("Saved to ", path)

        return CreateRequest(request=request)

class UpdateRequest(graphene.Mutation):
    class Arguments:
        input = RequestInput(required=True)
        id = graphene.ID()

    request = graphene.Field(RequestType)

    @classmethod
    def mutate(cls, root, info, input, id):
        request = Request.objects.get(pk=id)
        request.name = input.name
        request.description = input.description
        request.address = input.address
        # request.imageurl = input.imageurl
        request.date_created = input.date_created
        request.date_approved = input.date_approved
        request.approved = input.approved
        # request.category = input.category
        # request.tags = input.tags

        request.save()
        return UpdateRequest(request=request)

class DeleteRequest(graphene.Mutation):
    ok = graphene.Boolean()

    class Arguments:
        id = graphene.ID()

    @classmethod
    def mutate(cls, root, info, id):
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
        print("Start approval process for request id=", id)
         # apply validation steps
        validated = True
        validation_message = ''
        request = Request.objects.get(pk=id)
        print(request)
        if request.approved:
            raise ValidationError(
                ('The request has already been approved.'),
                params={'approved_by': request.approved_by},
            )

        print("Check if place already exists: ", request.name, request.address)
        # Check if place already exists
        try:
            place = Place.objects.get(name=request.name)#,address=request.address.id)
            print("Found: ", place)
            validated = False
            validation_message = 'There is already a place with the same name ' + place.name
        except Exception as place_e:
            print(place_e)
        
        if (not validated):
                print(validation_message)
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
        print("New place was created: ", newPlace)

        ##### PROCESS TAGS #####
        # find existing 
        tags = Tag.objects.filter(name__in=request.tags)
        print("Found tags: ", tags, len(tags))
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
                print("New tag was added:", newTag)
        else:
            # check for not existing
            for tag in request.tags:
                print("Check if tag ",tag,"is in tags",tags)
                found = next((x for x in tags if x.name == tag), None)
                if found:
                    print("Ignore existing tag:",tag)
                    # assign to place
                    newPlace.tags.add(found)
                else:
                    # create
                    newTag = Tag()
                    newTag.name = tag.lower()
                    newTag.save()
                    # assign to place
                    newPlace.tags.add(newTag)
                    print("New tag was added:", newTag)

        # tags = Tag.objects.filter(name__in=request.tags)
        # newPlace.tags.set(tags)
        # dct = {name: classthing(name) for name in request.tags}
        
        ##### PROCESS Images #####
        # find existing images and update place_id if not exist
        images = Image.objects.filter(request_id=request.id)
        print("Found images: ", images, len(images))
        if (len(images)>0):
            for image in images:
                if not image.place_id:
                    image.place_id = newPlace.id
                    image.save()
                    print("Image {} was updated with place # {}".format(image.name, image.place_id))
                else:
                    print("Error: Image {} already has place # {}".format(image.name, image.place_id))
        else:
            print("No images found associated with request #", request.id)

        # set request as approved
        request.approved = True
        request.date_approved = timezone.now()
        request.approved_by = input.approved_by
        request.approved_comment = input.approved_comment

        request.save()
        print("The request (id={}) was updated ".format(request.id), newPlace)
        return ApproveRequest(request=request)

class ImageInput(graphene.InputObjectType):
    # set_id = graphene.String()
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
        # image.set_id = input.set_id
        image.request = Request.objects.get(pk=input.request_id)
        image.name = input.name
        image.url = input.url
        image.metadata = input.metadata
        image.save()

        return cls(image=image)

class Mutation(graphene.ObjectType):
    update_category = UpdateCategory.Field()
    create_category = CreateCategory.Field()
    create_request = CreateRequest.Field()
    create_image = CreateImage.Field()
    update_request = UpdateRequest.Field()
    # update_request_images_set_id = UpdateRequesImagesSetId.Field()
    approve_request = ApproveRequest.Field()
    delete_request = DeleteRequest.Field()
    upload_file = UploadFile.Field()
    upload_files = UploadFiles.Field()

schema = graphene.Schema(query=Query, mutation=Mutation)
