# backend/views.py
# from datetime import datetime

# from django.http import HttpResponse
# from django.contrib.auth.models import User

# from django.core.serializers import serialize
from backend.models import Place, Category, Tag, Address, Request, Image

from django.contrib.auth.models import User, Group
from rest_framework import viewsets, mixins, permissions
from backend.serializers import UserSerializer, GroupSerializer, \
    CategorySerializer, PlaceSerializer, AddressSerializer, TagSerializer, RequestSerializer, ImageSerializer
from rest_framework_gis.filters import InBBoxFilter, TMSTileFilter

# #password = User.objects.make_random_password() # 7Gjk2kd4T9
# #password = User.objects.make_random_password(length=14) # FTELhrNFdRbSgy
# password = User.objects.make_random_password(length=14, allowed_chars='!#$%&()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[]^_abcdefghijklmnopqrstuvwxyz{|}~') # zvk0hawf8m6394

# #user.set_password(password)

# def index(request):
#     now = datetime.now()
#     geojson = serialize("geojson", Place.objects.all(), geometry_field="location", fields=["name"])

#     html = f'''
#     <html>
#         <body>
#             <h1>Hello from Vercel!</h1>
#             <p>The current time is { now }.</p>
#             <p>The generated password is  { password }</p>
#             { geojson }
#         </body>
#     </html>
#     '''
#     return HttpResponse(html)



class UserViewSet(viewsets.ModelViewSet):
    """
    API endpoint that allows users to be viewed or edited.
    """
    queryset = User.objects.all().order_by('-date_joined')
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]


class GroupViewSet(viewsets.ModelViewSet):
    """
    API endpoint that allows groups to be viewed or edited.
    """
    queryset = Group.objects.all()
    serializer_class = GroupSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    from rest_framework import viewsets, mixins

class AddressViewSet(mixins.RetrieveModelMixin,
                    mixins.ListModelMixin,
                    viewsets.GenericViewSet):
    """
    API endpoint that allows addresses to be viewed.
    """
    queryset = Address.objects.all()
    serializer_class = AddressSerializer
    permission_classes = [permissions.IsAuthenticated]
    bbox_filter_field = 'location'
    
    #TMSTileFilter: /?tile=8/100/200
    # filter_backends = (TMSTileFilter,)
    #InBBoxFilter:  /?in_bbox=-90,29,-89,35
    filter_backends = (InBBoxFilter,)
    bbox_filter_include_overlapping = True # Optional

class RequestViewSet(viewsets.ModelViewSet):
    """
    API endpoint that allows requests to be viewed.
    """
    queryset = Request.objects.all()
    serializer_class = RequestSerializer
    permission_classes = [permissions.IsAuthenticated]


class PlaceViewSet(viewsets.ModelViewSet):
    """
    Public API endpoint that allows places to be viewed.
    """
    queryset = Place.objects.all()
    serializer_class = PlaceSerializer
    # permission_classes = [permissions.IsAuthenticated]
    # bbox_filter_field = 'address.location'
    
    #TMSTileFilter: /?tile=8/100/200
    # filter_backends = (TMSTileFilter,)
    #InBBoxFilter:  /?in_bbox=-90,29,-89,35
    # filter_backends = (InBBoxFilter,)
    # bbox_filter_include_overlapping = True # Optional

# class LocationList(ListAPIView):

#     queryset = Place.objects.all()
#     serializer_class = PlaceSerializer
#     bbox_filter_field = 'point'
#     filter_backends = (InBBoxFilter,)
#     bbox_filter_include_overlapping = True # Optional

class CategoryViewSet(viewsets.ModelViewSet):
    """
    API endpoint that allows categories to be viewed or edited.
    """
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    permission_classes = [permissions.IsAuthenticated]

class TagViewSet(viewsets.ModelViewSet):
    """
    API endpoint that allows categories to be viewed or edited.
    """
    queryset = Tag.objects.all()
    serializer_class = TagSerializer
    permission_classes = [permissions.IsAuthenticated]

class ImageViewSet(viewsets.ModelViewSet):
    """
    API endpoint that allows images to be viewed or edited.
    """
    queryset = Image.objects.all()
    serializer_class = ImageSerializer
    permission_classes = [permissions.IsAuthenticated]
