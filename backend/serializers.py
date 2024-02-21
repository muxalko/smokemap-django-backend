from rest_framework_gis import serializers as gis_serializers
from rest_framework import serializers as rest_serializers
from rest_framework_gis.serializers import GeometrySerializerMethodField
from backend.models import Place, Address


class TagListingField(rest_serializers.RelatedField):
     def to_representation(self, value):
         return value.name

class PlaceSerializer(gis_serializers.GeoFeatureModelSerializer):

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
