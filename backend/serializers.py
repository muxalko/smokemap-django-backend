from django.contrib.auth.models import User, Group
from rest_framework import serializers
from django.contrib.gis.geos import Point
from rest_framework_gis.serializers import GeoFeatureModelSerializer, GeometrySerializerMethodField
from backend.models import Place, Category
from rest_framework_gis.filters import InBBoxFilter

class UserSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = User
        fields = ['url', 'username', 'email', 'groups']


class GroupSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = Group
        fields = ['url', 'name']

class CategorySerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = Category
        fields = ['name', 'description']


class PlaceSerializer(GeoFeatureModelSerializer):
# class PlaceSerializer(serializers.HyperlinkedModelSerializer):
    """ A class to serialize locations as GeoJSON compatible data """
    
    # a field which contains a geometry value and can be used as geo_field
    # other_point = GeometrySerializerMethodField()

    # def get_other_point(self, obj):
    #     print(obj)
    #     return Point(obj.location[0] / 2, obj.location[1] / 2)
    
    class Meta:
        model = Place
        fields = '__all__'
        # id_field = False
        # id_field = 'name'
        # geo_field = 'other_point'
        geo_field = "location"

        # you can also explicitly declare which fields you want to include
        # as with a ModelSerializer.
        # fields = ('id', 'address', 'city', 'state')
        