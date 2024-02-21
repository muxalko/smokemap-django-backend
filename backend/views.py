# backend/views.py

from backend.models import Place, Address
from rest_framework import viewsets, permissions
from backend.serializers import PlaceSerializer
from rest_framework_gis.filters import InBBoxFilter

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