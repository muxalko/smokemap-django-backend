from rest_framework_gis import serializers as gis_serializers
from rest_framework import serializers as rest_serializers
from rest_framework_gis.serializers import GeometrySerializerMethodField
from django.urls import reverse

from backend.models import Place, Address, Location, PublicMediaRendition


class TagListingField(rest_serializers.RelatedField):
     def to_representation(self, value):
         return value.name


class PublicMediaRenditionSerializer(rest_serializers.ModelSerializer):
    public_id = rest_serializers.UUIDField(read_only=True)
    url = rest_serializers.SerializerMethodField()

    def get_url(self, value):
        relative = reverse(
            "public-media-rendition", kwargs={"public_id": value.public_id}
        )
        request = self.context.get("request")
        return request.build_absolute_uri(relative) if request is not None else relative

    class Meta:
        model = PublicMediaRendition
        fields = (
            "public_id",
            "url",
            "position",
            "mime_type",
            "byte_size",
            "width",
            "height",
        )

class LocationSerializer(gis_serializers.GeoFeatureModelSerializer):
    """ A class to serialize locations as GeoJSON compatible data """

    class Meta:
        model = Location
        fields = ['name','category','info','address', 'tags', 'place_id']
        geo_field = "geom"

class AddressSerializer(gis_serializers.GeoFeatureModelSerializer):
    """ A class to serialize locations as GeoJSON compatible data """

    places = rest_serializers.SerializerMethodField('get_places')
    def get_places(self, value): 
        return Place.objects.filter(address=value).values()
    
    class Meta:
        model = Address
        fields = ["addressString", "places"]
        geo_field = "location"

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
    category_slug = rest_serializers.CharField(source="category.slug", read_only=True)
    media = rest_serializers.SerializerMethodField()
    def get_place_id(self, value):
         return value.id

    def get_media(self, value):
        prefetched = getattr(value, "_published_media", None)
        renditions = prefetched if prefetched is not None else value.public_media.filter(
            state=PublicMediaRendition.State.PUBLISHED
        ).order_by("position", "pk")
        return PublicMediaRenditionSerializer(
            renditions, many=True, context=self.context
        ).data
    
    class Meta:
        model = Place
        geo_field = "location"
        fields = [
            'place_id',
            'name',
            'category',
            'category_slug',
            'description',
            'address',
            'tags',
            'website',
            'media',
        ]


class ViewportPlaceSerializer(gis_serializers.GeoFeatureModelSerializer):
    location = GeometrySerializerMethodField()
    place_id = rest_serializers.IntegerField(source="id")
    category = rest_serializers.SerializerMethodField()
    address = rest_serializers.CharField(source="address.addressString")
    tags = TagListingField(many=True, read_only=True)

    def get_location(self, obj):
        return obj.address.location

    def get_category(self, obj):
        return {
            "id": obj.category_id,
            "slug": obj.category.slug,
            "name": obj.category.name,
        }

    class Meta:
        model = Place
        geo_field = "location"
        fields = (
            "place_id",
            "name",
            "category",
            "description",
            "address",
            "tags",
            "website",
        )


class PlaceSearchResultSerializer(rest_serializers.ModelSerializer):
    id = rest_serializers.IntegerField(read_only=True)
    address = rest_serializers.CharField(
        source="address.addressString",
        allow_null=True,
        read_only=True,
    )
    location = rest_serializers.SerializerMethodField()
    category = rest_serializers.SerializerMethodField()
    match = rest_serializers.SerializerMethodField()

    def get_location(self, obj):
        return {
            "type": "Point",
            "coordinates": [obj.address.location.x, obj.address.location.y],
        }

    def get_category(self, obj):
        return {
            "id": obj.category_id,
            "slug": obj.category.slug,
            "name": obj.category.name,
        }

    def get_match(self, obj):
        return "prefix" if obj.match_rank == 0 else "fuzzy"

    class Meta:
        model = Place
        fields = ("id", "name", "address", "location", "category", "match")
