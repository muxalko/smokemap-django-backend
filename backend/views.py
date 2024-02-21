# backend/views.py

from backend.models import Place, Address #, Category, Tag, Request, Image
# from django.contrib.auth import login, logout, authenticate
# from django.contrib.auth.models import User, Group
# from django.contrib.auth.mixins import LoginRequiredMixin
from rest_framework import viewsets, permissions #, views,  mixins, status, generics
# from django.contrib.auth.mixins import LoginRequiredMixin
from backend.serializers import PlaceSerializer #, AddressSerializer, LoginSerializer
from rest_framework_gis.filters import InBBoxFilter #, TMSTileFilter
from rest_framework.response import Response

# from .auth import VisitorAuthentication
# from .permissions import VisitorPermission

# from graphene_django.views import GraphQLView

# require login for graphql endpoint
# class PrivateGraphQLView(LoginRequiredMixin, GraphQLView):
#     pass

# from dj_rest_auth.registration.views import SocialLoginView
# from allauth.socialaccount.providers.google.views import GoogleOAuth2Adapter
# from allauth.socialaccount.providers.oauth2.client import OAuth2Client


# class GoogleLogin(SocialLoginView):
#     adapter_class = GoogleOAuth2Adapter
#     callback_url = "http://smokemap.org:3000/"
#     client_class = OAuth2Client


class PlaceViewSet(viewsets.ModelViewSet):
    """
    Public API endpoint that allows places to be viewed.
    """
    # queryset = Place.objects.all()
    queryset = Address.objects.all()
    
    bbox_filter_field = 'location'
    
    #TMSTileFilter: /?tile=8/100/200
    # filter_backends = (TMSTileFilter,)
    #InBBoxFilter:  /?in_bbox=-90,29,-89,35
    filter_backends = (InBBoxFilter,)
    bbox_filter_include_overlapping = True # Optional

    # def get_queryset(self):
    #     bbox = self.request.query_params.get('in_bbox')
    #     print(bbox)
    #     queryset = Address.objects.all()
    #     return queryset

    def filter_queryset(self, queryset):
        addresses_queryset = super().filter_queryset(queryset)
        addresses=addresses_queryset.values_list('id')
        places_queryset = Place.objects.filter(address_id__in=addresses)
        return places_queryset
    
    serializer_class = PlaceSerializer
    # serializer_class = AddressSerializer
    permission_classes = [permissions.AllowAny,]

# class LocationList(ListAPIView):

#     queryset = Place.objects.all()
#     serializer_class = PlaceSerializer
#     bbox_filter_field = 'point'
#     filter_backends = (InBBoxFilter,)
#     bbox_filter_include_overlapping = True # Optional


# class VisitorView(generics.GenericAPIView):
# # class VisitorView(views.APIView):
    
#     authentication_classes = (VisitorAuthentication, )
#     permission_classes = (VisitorPermission, )
    
#     def get(self, request, format=None):
#         visitor = VisitorSerializer(self.request.user)
#         return Response(data = visitor.data, status=status.HTTP_202_ACCEPTED)

# class LoginView(views.APIView):
#     """
#     API endpoint that allows users to login.
#     """
#     # This view should be accessible also for unauthenticated users.
#     permission_classes = (permissions.AllowAny,)

#     def post(self, request, format=None):
#         serializer = LoginSerializer(data=self.request.data,
#             context={ 'request': self.request })
#         serializer.is_valid(raise_exception=True)
#         user = serializer.validated_data['user']
#         login(request, user)
#         reply_user = {  'id': user.id,
#                         'name': user.name,
#                         'email': user.email,
#                         'image': '/admin.svg',
#                         'role': 'admin'
#                         }
#         return Response(reply_user, status=status.HTTP_202_ACCEPTED)

# class LogoutView(views.APIView):
#     """
#     API endpoint that allows users to logout.
#     """
#     def post(self, request, format=None):
#         logout(request)
#         return Response(None, status=status.HTTP_204_NO_CONTENT)

# class ProfileView(generics.RetrieveAPIView):
#     """
#     API endpoint that allows users to get their user profile.
#     """
#     serializer_class = UserSerializer

#     def get_object(self):
#         return self.request.user

# class UserViewSet(viewsets.ModelViewSet):
#     """
#     API endpoint that allows users to be viewed or edited.
#     """
#     queryset = User.objects.all().order_by('-date_joined')
#     serializer_class = UserSerializerHyperlinked

# class GroupViewSet(viewsets.ModelViewSet):
#     """
#     API endpoint that allows groups to be viewed or edited.
#     """
#     queryset = Group.objects.all()
#     serializer_class = GroupSerializer
    
#     from rest_framework import viewsets, mixins

# class AddressViewSet(mixins.RetrieveModelMixin,
#                     mixins.ListModelMixin,
#                     viewsets.GenericViewSet):
#     """
#     API endpoint that allows addresses to be viewed.
#     """
#     queryset = Address.objects.all()
#     serializer_class = AddressSerializer
#     bbox_filter_field = 'location'
    
#     #TMSTileFilter: /?tile=8/100/200
#     # filter_backends = (TMSTileFilter,)
#     #InBBoxFilter:  /?in_bbox=-90,29,-89,35
#     filter_backends = (InBBoxFilter,)
#     bbox_filter_include_overlapping = True # Optional

# class RequestViewSet(viewsets.ModelViewSet):
#     """
#     API endpoint that allows requests to be viewed.
#     """
#     queryset = Request.objects.all()
#     serializer_class = RequestSerializer

# class CategoryViewSet(viewsets.ModelViewSet):
#     """
#     API endpoint that allows categories to be viewed or edited.
#     """
#     queryset = Category.objects.all()
#     serializer_class = CategorySerializer

# class TagViewSet(viewsets.ModelViewSet):
#     """
#     API endpoint that allows categories to be viewed or edited.
#     """
#     queryset = Tag.objects.all()
#     serializer_class = TagSerializer

# class ImageViewSet(viewsets.ModelViewSet):
#     """
#     API endpoint that allows images to be viewed or edited.
#     """
#     queryset = Image.objects.all()
#     serializer_class = ImageSerializer