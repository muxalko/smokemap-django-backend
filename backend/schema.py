import graphene
from graphene_django import DjangoObjectType
from .models import Category, Tag, Request, Address, Place
from django.core.exceptions import ValidationError
import graphql_geojson
from django.utils import timezone
# from django.contrib.gis import geos


class PlaceType(DjangoObjectType):
    class Meta:
        model = Place
        fields = ('id','name', 'category', 'address', 'description', 'tags')


class CategoryType(DjangoObjectType):
    class Meta:
        model = Category
        fields = ('id', 'name', 'description')


class TagType(DjangoObjectType):
    class Meta:
        model = Tag
        fields = ('id', 'name')


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
            # 'imageurl',
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
    requests = graphene.List(RequestType)
    requests_to_approve = graphene.List(RequestType)
    requests_by_id = graphene.Field(
        RequestType,
        id=graphene.ID()
    )
    requests_by_name = graphene.Field(
        RequestType,
        name=graphene.String()
    )

    places = graphene.List(PlaceType)

    def resolve_categories(root, info, **kwargs):
        # Querying a list
        return Category.objects.all()

    def resolve_tags(root, info, **kwargs):
        # Querying a list
        return Tag.objects.all()
    def resolve_addresses(root, info, **kwargs):
        # Querying a list
        return Address.objects.all()

    def resolve_requests(root, info, **kwargs):
        # Querying a list
        return Request.objects.all()

    def resolve_requests_to_approve(root, info, **kwargs):
        # Querying a list
        return Request.objects.filter(approved=False)

    def resolve_requests_by_id(root, info, id):
        # Querying a request
        return Request.objects.get(pk=id)

    def resolve_requests_by_name(root, info, name):
        # Querying a named
        return Request.objects.filter(name=name)

    def resolve_places(root, info, **kwargs):
        # Querying a list
        return Place.objects.all()


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


class RequestInput(graphene.InputObjectType):
    name = graphene.String()
    category = graphene.String()
    description = graphene.String()
    address_string = graphene.String()
    tags = graphene.List(graphene.String)
    # images = graphene.List(graphene.String)

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
        
        
        # set request as approved
        request.approved = True
        request.date_approved = timezone.now()
        request.approved_by = input.approved_by
        request.approved_comment = input.approved_comment

        request.save()
        print("The request (id={}) was updated ".format(request.id), newPlace)
        return ApproveRequest(request=request)


class Mutation(graphene.ObjectType):
    update_category = UpdateCategory.Field()
    create_category = CreateCategory.Field()
    create_request = CreateRequest.Field()
    update_request = UpdateRequest.Field()
    approve_request = ApproveRequest.Field()
    delete_request = DeleteRequest.Field()


schema = graphene.Schema(query=Query, mutation=Mutation)
