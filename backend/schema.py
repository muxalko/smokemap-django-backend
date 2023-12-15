import graphene
from graphene_django import DjangoObjectType
from .models import Category, Tag, Request, Address, Place
from django.core.exceptions import ValidationError
import graphql_geojson
from django.utils import timezone
from django.contrib.gis import geos

class PlaceType(graphql_geojson.GeoJSONType):
    class Meta:
        model = Place
        geojson_field = 'location'
        # yelds UserWarning: Field name "name" matches an attribute on Django model "backend.Place" 
        # but it's not a model field so Graphene cannot determine what type it should be. 
        # Either define the type of the field on DjangoObjectType "PlaceType" 
        # or remove it from the "fields" list
        # fields = ('id','name', 'category', 'location', 'description', 'tags')

class CategoryType(DjangoObjectType):
    class Meta: 
        model = Category
        fields = ('id','name','description')

class TagType(DjangoObjectType):
    class Meta: 
        model = Tag
        fields = ('id','name')

class AddressType(DjangoObjectType):
    class Meta: 
        model = Address
        fields = (
            'id',
            'address',
            'lat',
            'long'
        )  

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
        location = graphql_geojson.Geometry(required=True)
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
        # print("Input:", input)

        #apply validation steps
        validated = True

        myaddress = Address()
        # check if address already exists, if not save as new
        try:
            myaddress = Address.objects.get(address=input.address_string)
            # if an address already in the database, 
            # the chances that a place share the same address can indicate a duplicate
            # We only allow different place names per one address
            # lets check if that same place or request exists
            try: 
                place = Place.objects.get(location=myaddress.address)
                print("Found place: ", place)
                validated = False
                validation_message = 'There is already a place with the same name.'
            except Exception as place_e:
                print("Validation of Place is OK: ", place_e)
            
            try:
                request = Request.objects.filter(name=input.name)
                if (len(request)>0):
                    validated = False
                    validation_message = 'There is already a request with the same name.'
            except Exception as request_e:
                print("Validation of Request is OK: ", request_e)

            if (not validated):
                raise ValidationError(
                            (validation_message),
                            params={'value': request},
                            )
        
        except Exception as myaddress_e:
            print(myaddress_e)
            myaddress.address = input.address_string
            myaddress.save()
        
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
            request.address = myaddress
            #request.imageurl = input.imageurl
            
            # loop over tags and check if tag already exists,
            #  and create a new one in case it doesn't 
            for arrived_tag in input.tags:
                tag = Tag()
                try:
                    tag = Tag.objects.get(name=arrived_tag)
                except Exception as e:
                    print(e)
                    tag.name = arrived_tag
                    tag.save()
                request.tags.append(tag)
            
            print("Request(Category:{}, Name: {}, Desc: {}, Address: {}, Tags: {})".format(request.category, request.name, request.description, request.address, request.tags))
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
        #request.category = input.category
        #request.tags = input.tags

        request.save()
        return UpdateRequest(request=request)

class RequestApproveInput(graphene.InputObjectType):
    approved_comment = graphene.String() 
    approved_by = graphene.String()
    approved = graphene.Boolean()


class ApproveRequest(graphene.Mutation):

    class Arguments:
        input = RequestApproveInput(required=True)
        id = graphene.ID()

    request = graphene.Field(RequestType)
    
    @classmethod
    def mutate(cls, root, info, input, id):
        request = Request.objects.get(pk=id)

        if request.approved:
            raise ValidationError(
            ('The request has already been approved.'),
            params={'approved_by': request.approved_by},
            )

        request.date_approved = timezone.now()
        request.approved = True

        
        
        newPlace = Place()
        newPlace.name = request.name
        newPlace.category = request.category
        newPlace.description = request.description
        newPlace.tags = request.tags
        
        newPlace.location = geos.Point((request.address.long, request.address.lat))
        newPlace.save()

        request.save()
        return ApproveRequest(request=request)

class Mutation(graphene.ObjectType):
    update_category = UpdateCategory.Field()
    create_category = CreateCategory.Field()
    create_request = CreateRequest.Field()
    update_request = UpdateRequest.Field()
    approve_request = ApproveRequest.Field()

schema = graphene.Schema(query=Query, mutation=Mutation)