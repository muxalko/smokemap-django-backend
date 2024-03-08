# backend/views.py

from backend.models import Place, Address, Location
from rest_framework import viewsets, permissions, mixins
from backend.serializers import PlaceSerializer, AddressSerializer, LocationSerializer
from rest_framework_gis.filters import InBBoxFilter

class LocationViewSet(mixins.RetrieveModelMixin,
                    mixins.ListModelMixin,
                    viewsets.GenericViewSet):
    """
    API endpoint that allows addresses to be viewed.
    """
    queryset = Location.objects.all()
    serializer_class = LocationSerializer
    permission_classes = [permissions.AllowAny,]
    # bbox_filter_field = 'location'
    
    #TMSTileFilter: /?tile=8/100/200
    # filter_backends = (TMSTileFilter,)
    #InBBoxFilter:  /?in_bbox=-90,29,-89,35
    filter_backends = (InBBoxFilter,)
    bbox_filter_include_overlapping = True # Optional

class AddressViewSet(mixins.RetrieveModelMixin,
                    mixins.ListModelMixin,
                    viewsets.GenericViewSet):
    """
    API endpoint that allows addresses to be viewed.
    """
    queryset = Address.objects.all()
    serializer_class = AddressSerializer
    permission_classes = [permissions.AllowAny,]
    bbox_filter_field = 'location'
    
    #TMSTileFilter: /?tile=8/100/200
    # filter_backends = (TMSTileFilter,)
    #InBBoxFilter:  /?in_bbox=-90,29,-89,35
    filter_backends = (InBBoxFilter,)
    bbox_filter_include_overlapping = True # Optional

class PlaceViewSet(viewsets.ModelViewSet):
    """
    Public API endpoint that allows places to be viewed.
    """
    queryset = Address.objects.all()
    
    bbox_filter_field = 'location'
    
    filter_backends = (InBBoxFilter,)
    bbox_filter_include_overlapping = True # Optional

    def filter_queryset(self, queryset):
        addresses_queryset = super().filter_queryset(queryset)
        addresses=addresses_queryset.values_list('id')
        places_queryset = Place.objects.filter(address_id__in=addresses)
        return places_queryset
    
    serializer_class = PlaceSerializer
    permission_classes = [permissions.AllowAny,]