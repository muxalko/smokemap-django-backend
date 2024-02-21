# backend/urls.py
# from django.urls import path

# from backend.views import index


# urlpatterns = [
#     path('', index),
# ]

from django.urls import include, path
from rest_framework import routers
from backend import views
from django.views.decorators.csrf import csrf_exempt
from graphene_django.views import GraphQLView
from graphql_jwt.decorators import jwt_cookie
from backend.graphql.custom_auth import DRFAuthenticatedGraphQLView
#from graphene_file_upload.django import FileUploadGraphQLView
# from backend.views import PrivateGraphQLView
# from . import schema

router = routers.DefaultRouter()
# router.register(r'users', views.UserViewSet)
# router.register(r'groups', views.GroupViewSet)
# router.register(r'categories', views.CategoryViewSet)
# router.register(r'tags', views.TagViewSet)
# router.register(r'addresses', views.AddressViewSet)
# router.register(r'requests', views.RequestViewSet)
# router.register(r'images', views.ImageViewSet)
router.register(r'places', views.PlaceViewSet)
# Wire up our API using automatic URL routing.
# Additionally, we include login URLs for the browsable API.
urlpatterns = [
    path('', include(router.urls)),
    # path('api-auth/', include('rest_framework.urls', namespace='rest_framework')),
    # path('login/', views.LoginView.as_view()),
    # path('profile/', views.ProfileView.as_view()),
    # path('visitor/', views.VisitorView.as_view()),
    # path('logout/', views.LogoutView.as_view()),
    # Enforcing CSRF validation for the whole graphql endpoint makes no sense,
    # because we will validate user authentication at later stages and for specific mutations.
    # CSRF exemption 
    # path('graphql/', jwt_cookie(GraphQLView.as_view(graphiql=True))),
    # path('graphql/', jwt_cookie(csrf_exempt(GraphQLView.as_view(graphiql=True)))),
    # path('graphql/', csrf_exempt(FileUploadGraphQLView.as_view(graphiql=True))), 
    # path('graphql/', DRFAuthenticatedGraphQLView.as_view(graphiql=True)),
    # path('graphql/', csrf_exempt(DRFAuthenticatedGraphQLView.as_view(graphiql=True))),
    path('graphql/', csrf_exempt(GraphQLView.as_view(graphiql=True))),
    # path('graphql/', GraphQLView.as_view(graphiql=True)),
    # Force redirect to login if not authenticated
    # path('graphql/', PrivateGraphQLView.as_view(graphiql=True)),
  
    # path('accounts/', include('allauth.urls')),
    # path('dj-rest-auth/', include('dj_rest_auth.urls')),
    # path('dj-rest-auth/registration/', include('dj_rest_auth.registration.urls')),
    # path("google/", views.GoogleLogin.as_view(), name="google_login"),
]