# backend/urls.py
from django.urls import path

from backend.views import index


# urlpatterns = [
#     path('', index),
# ]

from django.urls import include, path
from rest_framework import routers
from backend import views

router = routers.DefaultRouter()
router.register(r'users', views.UserViewSet)
router.register(r'groups', views.GroupViewSet)
router.register(r'categories', views.CategoryViewSet)
router.register(r'tags', views.TagViewSet)
router.register(r'places', views.PlaceViewSet)
router.register(r'addresses', views.AddressViewSet)
router.register(r'requests', views.RequestViewSet)
# Wire up our API using automatic URL routing.
# Additionally, we include login URLs for the browsable API.
urlpatterns = [
    path('', index),
]