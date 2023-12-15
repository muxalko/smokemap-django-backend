from django.contrib.auth.models import User, Group
from rest_framework_gis import serializers as gis_serializers
from rest_framework import serializers as rest_serializers
from rest_framework_gis.serializers import GeoFeatureModelSerializer, GeometrySerializerMethodField
from backend.models import Place, Category, Tag, Address, Request
from rest_framework_gis.filters import InBBoxFilter


class UserSerializer(rest_serializers.HyperlinkedModelSerializer):
    class Meta:
        model = User
        fields = ['url', 'username', 'email', 'groups']


class GroupSerializer(rest_serializers.HyperlinkedModelSerializer):
    class Meta:
        model = Group
        fields = ['url', 'name']

class CategorySerializer(rest_serializers.HyperlinkedModelSerializer):
    class Meta:
        model = Category
        fields = ['name', 'description']

class TagSerializer(rest_serializers.HyperlinkedModelSerializer):
    class Meta:
        model = Tag
        fields = ['name', 'places']

class TagListingField(rest_serializers.RelatedField):
     def to_representation(self, value):
         return value.name
     
class RequestSerializer(rest_serializers.HyperlinkedModelSerializer):
    class Meta:
        model = Request
        fields = ['name','category','description','address', 'tags']

class PlaceSerializer(gis_serializers.GeoFeatureModelSerializer):
# class PlaceSerializer(rest_serializers.HyperlinkedModelSerializer):
    
    # if need manipulation at serialization:
    # a field which contains a geometry value and can be used as geo_field
    # other_point = GeometrySerializerMethodField()
    location = GeometrySerializerMethodField()

    # def get_other_point(self, obj):
    #     print(obj)
    #     return Point(obj.location[0] / 2, obj.location[1] / 2)
    def get_location(self, obj):
        print(obj)
        return Address.objects.get(pk=obj.address_id).location

    # list of objects
    # tag = TagSerializer(read_only=True, many=True)
    # list of strings
    tags = TagListingField(many=True, read_only=True)
    class Meta:
        model = Place
        geo_field = "location"
        fields = ['name','category','description','address', 'tags']


class AddressSerializer(gis_serializers.GeoFeatureModelSerializer):
    """ A class to serialize locations as GeoJSON compatible data """

    requests = rest_serializers.SerializerMethodField('get_requests')
    def get_requests(self, value): 
        # print("get_request: self=",self)
        # print("get_request: value=",value)
        return Request.objects.filter(address=value).values()
    
    
    places = rest_serializers.SerializerMethodField('get_places')
    def get_places(self, value): 
        # print("get_places: self=",self)
        # print("get_places: value=",value)
        return Place.objects.filter(address=value).values()
    

    # custom_field = JSONField()
    # hello_world = String()

    # @staticmethod
    # def resolve_custom_field(root, info, **kwargs):
    #     return {'msg': 'That was easy!'} # or json.dumps perhaps? you get the idea

    # @staticmethod
    # def resolve_hello_world(root, info, **kwargs):
    #     return 'Hello, World!'
    
    # requests = serializers.SerializerMethodField()
    
    # def get_foo(self, obj):
    #     return "Foo id: %i" % obj.pk
    # def get_properties(self, instance, fields):
    #     properties = {}
    #     properties['test'] = "test property"
    #     print("get_properties:", self, instance, fields)
    #     return properties
    
    # def unformat_geojson(self, feature):
    #     attrs = {
    #         self.Meta.geo_field: feature["geometry"],
    #         "metadata": feature["properties"]
    #     }

    #     if self.Meta.bbox_geo_field and "bbox" in feature:
    #         attrs[self.Meta.bbox_geo_field] = Polygon.from_bbox(feature["bbox"])

    #     return attrs

    # if need manipulation at serialization:
    # a field which contains a geometry value and can be used as geo_field
    # other_point = GeometrySerializerMethodField()

    # def get_other_point(self, obj):
    #     print(obj)
    #     return Point(obj.location[0] / 2, obj.location[1] / 2)
    
    class Meta:
        model = Address
        fields = ("addressString", "places", "requests")
        # fields = '__all__'
        # id_field = False
        # id_field = 'name'
        # geo_field = 'other_point'
        geo_field = "location"

        # you can also explicitly declare which fields you want to include
        # as with a ModelSerializer.
        # fields = ('addressString')

        