from rest_framework_gis import serializers as gis_serializers
from rest_framework import serializers as rest_serializers
from rest_framework_gis.serializers import GeometrySerializerMethodField
from backend.models import Place, Address
from rest_framework_gis.filters import InBBoxFilter

# from dj_rest_auth.serializers import UserDetailsSerializer

from django.conf import settings

# from allauth.account.adapter import get_adapter
# from allauth.account.utils import setup_user_email

# from dj_rest_auth.registration.serializers import RegisterSerializer
from django.contrib.auth.models import Group
from django.http import JsonResponse
from django.contrib.auth import login, logout, authenticate

# class CustomRegisterSerializer(RegisterSerializer):
#     role = rest_serializers.CharField(max_length=255)
#     # department = rest_serializers.CharField(required=True, max_length=5)
#     # university = rest_serializers.CharField(required=True, max_length=100)

#     def get_cleaned_data(self):
#         data_dict = super().get_cleaned_data()
#         data_dict['role'] = self.validated_data.get('role', '')
#         # data_dict['department'] = self.validated_data.get('department', '')
#         # data_dict['university'] = self.validated_data.get('university', '')
#         return data_dict


# class CustomUserDetailsSerializer(UserDetailsSerializer):
#     # role = rest_serializers.CharField(max_length=255)
#     role = rest_serializers.SerializerMethodField()

#     def get_role(self, user):
#         groups = Group.objects.filter(user = user)
#         # wrap in list(), because QuerySet is not JSON serializable
#         isAdmin = False 
#         for group in groups:
#             if group.name == "admins":
#                 isAdmin = True
#         if isAdmin:
#             return 'admin'
#         else:
#             return 'guest'

#     class Meta(UserDetailsSerializer.Meta):
#         fields = UserDetailsSerializer.Meta.fields + \
#             ('name', 'role', 'image' )
    
    # def to_representation(self, instance):
    #     representation = super(CustomUserDetailsSerializer, self).to_representation(instance)
    #     representation['role'] = instance.get_role()
    #     return representation

        
# class LoginSerializer(rest_serializers.Serializer):
#     """
#     This serializer defines two fields for authentication:
#       * username
#       * password.
#     It will try to authenticate the user with when validated.
#     """
#     username = rest_serializers.CharField(
#         label="Username",
#         write_only=True
#     )
#     password = rest_serializers.CharField(
#         label="Password",
#         # This will be used when the DRF browsable API is enabled
#         style={'input_type': 'password'},
#         trim_whitespace=False,
#         write_only=True
#     )

#     def validate(self, attrs):
#         # Take username and password from request
#         username = attrs.get('username')
#         password = attrs.get('password')

#         if username and password:
#             # Try to authenticate the user using Django auth framework.
#             user = authenticate(request=self.context.get('request'),
#                                 email=username, password=password)
#             if not user:
#                 # If we don't have a regular user, raise a ValidationError
#                 msg = 'Access denied: wrong username or password.'
#                 raise rest_serializers.ValidationError(msg, code='authorization')
#         else:
#             msg = 'Both "username" and "password" are required.'
#             raise rest_serializers.ValidationError(msg, code='authorization')
#         # We have a valid user, put it in the serializer's validated_data.
#         # It will be used in the view.
#         attrs['user'] = user
#         return attrs

# class VisitorSerializer(rest_serializers.Serializer):
#     """
#     This serializer defines Visitor fields for reference:
#       * ip
#       [* geolocation]
#       [* city]
#     """
#     ip = rest_serializers.CharField(
#         label="IP",
#     )
    
# class UserSerializer(rest_serializers.ModelSerializer):

#     class Meta:
#         model = User
#         fields = [
#             'username',
#             'email',
#             'first_name',
#             'last_name',
#         ]

# class UserSerializerHyperlinked(rest_serializers.HyperlinkedModelSerializer):
#     class Meta:
#         model = CustomUser
#         fields = ['url', 'email', 'groups']


# class GroupSerializer(rest_serializers.HyperlinkedModelSerializer):
#     class Meta:
#         model = Group
#         fields = ['url', 'name']

# class CategorySerializer(rest_serializers.HyperlinkedModelSerializer):
#     class Meta:
#         model = Category
#         fields = ['name', 'description']

# class TagSerializer(rest_serializers.HyperlinkedModelSerializer):
#     class Meta:
#         model = Tag
#         fields = ['name', 'places']

class TagListingField(rest_serializers.RelatedField):
     def to_representation(self, value):
         return value.name
     
# class RequestSerializer(rest_serializers.HyperlinkedModelSerializer):
#     class Meta:
#         model = Request
#         fields = ['name','category','description','address', 'tags']

# class ImageSerializer(rest_serializers.HyperlinkedModelSerializer):
#     class Meta:
#         model = Image
#         fields = ['name','url','metadata']

class PlaceSerializer(gis_serializers.GeoFeatureModelSerializer):
# class PlaceSerializer(rest_serializers.HyperlinkedModelSerializer):
    
    # if need manipulation at serialization:
    # a field which contains a geometry value and can be used as geo_field
    # other_point = GeometrySerializerMethodField()
    location = GeometrySerializerMethodField()

    # resolve all places address geolocation
    # def get_other_point(self, obj):
    #     print(obj)
    #     return Point(obj.location[0] / 2, obj.location[1] / 2)
    def get_location(self, obj):
        # print(obj)
        return Address.objects.get(pk=obj.address_id).location

    # list of objects
    # tag = TagSerializer(read_only=True, many=True)
    # list of strings
    tags = TagListingField(many=True, read_only=True)

    # Moved to fetch images when feature is clicked
    # images = rest_serializers.SerializerMethodField('get_images')
    # def get_images(self, value):
    #     return Image.objects.filter(place_id=value).values_list("url", flat=True)

    # add place ID to properties
    place_id = rest_serializers.SerializerMethodField('get_place_id')
    def get_place_id(self, value):
         return value.id
    

    class Meta:
        model = Place
        geo_field = "location"
        fields = ['place_id', 'name','category','description','address', 'tags']


# class AddressSerializer(gis_serializers.GeoFeatureModelSerializer):
#     """ A class to serialize locations as GeoJSON compatible data """

#     requests = rest_serializers.SerializerMethodField('get_requests')
#     def get_requests(self, value): 
#         # print("get_request: self=",self)
#         # print("get_request: value=",value)
#         return Request.objects.filter(address=value).values()
    
    
#     places = rest_serializers.SerializerMethodField('get_places')
#     def get_places(self, value): 
#         # print("get_places: self=",self)
#         # print("get_places: value=",value)
#         return Place.objects.filter(address=value).values()
    

#     # custom_field = JSONField()
#     # hello_world = String()

#     # @staticmethod
#     # def resolve_custom_field(root, info, **kwargs):
#     #     return {'msg': 'That was easy!'} # or json.dumps perhaps? you get the idea

#     # @staticmethod
#     # def resolve_hello_world(root, info, **kwargs):
#     #     return 'Hello, World!'
    
#     # requests = serializers.SerializerMethodField()
    
#     # def get_foo(self, obj):
#     #     return "Foo id: %i" % obj.pk
#     # def get_properties(self, instance, fields):
#     #     properties = {}
#     #     properties['test'] = "test property"
#     #     print("get_properties:", self, instance, fields)
#     #     return properties
    
#     # def unformat_geojson(self, feature):
#     #     attrs = {
#     #         self.Meta.geo_field: feature["geometry"],
#     #         "metadata": feature["properties"]
#     #     }

#     #     if self.Meta.bbox_geo_field and "bbox" in feature:
#     #         attrs[self.Meta.bbox_geo_field] = Polygon.from_bbox(feature["bbox"])

#     #     return attrs

#     # if need manipulation at serialization:
#     # a field which contains a geometry value and can be used as geo_field
#     # other_point = GeometrySerializerMethodField()

#     # def get_other_point(self, obj):
#     #     print(obj)
#     #     return Point(obj.location[0] / 2, obj.location[1] / 2)
    
#     class Meta:
#         model = Address
#         fields = ("addressString", "places", "requests")
#         # fields = '__all__'
#         # id_field = False
#         # id_field = 'name'
#         # geo_field = 'other_point'
#         geo_field = "location"

#         # you can also explicitly declare which fields you want to include
#         # as with a ModelSerializer.
#         # fields = ('addressString')

class AddressSerializer(gis_serializers.GeoFeatureModelSerializer):
    """ A class to serialize locations as GeoJSON compatible data """

    places = rest_serializers.SerializerMethodField('get_places')
    def get_places(self, value): 
        # print("get_places: self=",self)
        # print("get_places: value=",value)
        return Place.objects.filter(address=value).values()
    
    class Meta:
        model = Address
        fields = ("addressString", "places")
        geo_field = "location"

