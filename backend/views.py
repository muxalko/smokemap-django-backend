# backend/views.py

from backend.models import Place, Address, Location
from rest_framework import viewsets, permissions, mixins
import json
import math

from django.contrib.gis.geos import Polygon
from rest_framework.response import Response
from rest_framework.views import APIView

from backend.serializers import (
    AddressSerializer,
    LocationSerializer,
    PlaceSerializer,
    ViewportPlaceSerializer,
)
from backend.permissions import IsAdministratorOrReadOnly
from rest_framework_gis.filters import InBBoxFilter


VIEWPORT_MAX_SPAN_DEGREES = 10
VIEWPORT_MAX_CATEGORIES = 20
VIEWPORT_RESULT_LIMIT = 500
VIEWPORT_RESPONSE_LIMIT_BYTES = 512 * 1024


class ViewportQueryError(ValueError):
    pass


def parse_viewport_query(query_params):
    raw_bbox = query_params.get("bbox")
    if raw_bbox is None:
        raise ViewportQueryError("bbox is required")
    try:
        bbox = tuple(float(value) for value in raw_bbox.split(","))
    except (TypeError, ValueError) as error:
        raise ViewportQueryError("bbox must contain four numbers") from error
    if len(bbox) != 4 or not all(math.isfinite(value) for value in bbox):
        raise ViewportQueryError("bbox must contain four finite numbers")

    minx, miny, maxx, maxy = bbox
    if not (-180 <= minx <= 180 and -180 <= maxx <= 180):
        raise ViewportQueryError("bbox longitude must be between -180 and 180")
    if not (-90 <= miny <= 90 and -90 <= maxy <= 90):
        raise ViewportQueryError("bbox latitude must be between -90 and 90")
    if minx >= maxx or miny >= maxy:
        raise ViewportQueryError("bbox minimums must be less than maximums")
    if (
        maxx - minx > VIEWPORT_MAX_SPAN_DEGREES
        or maxy - miny > VIEWPORT_MAX_SPAN_DEGREES
    ):
        raise ViewportQueryError(
            f"bbox span must not exceed {VIEWPORT_MAX_SPAN_DEGREES} degrees"
        )

    raw_zoom = query_params.get("zoom")
    try:
        zoom = int(raw_zoom)
    except (TypeError, ValueError) as error:
        raise ViewportQueryError("zoom must be an integer between 0 and 22") from error
    if not 0 <= zoom <= 22:
        raise ViewportQueryError("zoom must be an integer between 0 and 22")

    raw_categories = query_params.get("categories", "").strip()
    category_ids = ()
    if raw_categories:
        try:
            category_ids = tuple(
                dict.fromkeys(int(value) for value in raw_categories.split(","))
            )
        except ValueError as error:
            raise ViewportQueryError(
                "categories must contain positive integer IDs"
            ) from error
        if not category_ids or any(value <= 0 for value in category_ids):
            raise ViewportQueryError("categories must contain positive integer IDs")
        if len(category_ids) > VIEWPORT_MAX_CATEGORIES:
            raise ViewportQueryError(
                f"categories must contain at most {VIEWPORT_MAX_CATEGORIES} IDs"
            )

    return bbox, zoom, category_ids


def viewport_places_queryset(viewport, category_ids=()):
    queryset = (
        Place.objects.filter(address__location__coveredby=viewport)
        .select_related("address", "category")
        .prefetch_related("tags")
        .order_by("id")
    )
    if category_ids:
        queryset = queryset.filter(category_id__in=category_ids)
    return queryset


class ViewportPlaceView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        try:
            bbox, _zoom, category_ids = parse_viewport_query(request.query_params)
        except ViewportQueryError as error:
            return Response(
                {"code": "invalid_viewport", "detail": str(error)}, status=400
            )

        viewport = Polygon.from_bbox(bbox)
        queryset = viewport_places_queryset(viewport, category_ids)

        places = list(queryset[: VIEWPORT_RESULT_LIMIT + 1])
        if len(places) > VIEWPORT_RESULT_LIMIT:
            return Response(
                {
                    "code": "viewport_result_limit_exceeded",
                    "detail": (
                        f"viewport contains more than {VIEWPORT_RESULT_LIMIT} places; "
                        "zoom in or select categories"
                    ),
                },
                status=400,
            )

        payload = ViewportPlaceSerializer(places, many=True).data
        encoded_size = len(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        if encoded_size > VIEWPORT_RESPONSE_LIMIT_BYTES:
            return Response(
                {
                    "code": "viewport_result_limit_exceeded",
                    "detail": (
                        "viewport response exceeds 512 KiB; "
                        "zoom in or select categories"
                    ),
                },
                status=400,
            )

        return Response(payload)

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
    queryset = Address.objects.filter(place__isnull=False).distinct()
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
        places_queryset = Place.objects.filter(
            address_id__in=addresses
        ).select_related("category")
        return places_queryset
    
    serializer_class = PlaceSerializer
    permission_classes = [IsAdministratorOrReadOnly]
