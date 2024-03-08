
from django.urls import include, path
from rest_framework import routers
from backend import views
from django.views.decorators.csrf import csrf_exempt
from graphene_django.views import GraphQLView
from django.conf import settings

router = routers.DefaultRouter()
router.register(r'places', views.PlaceViewSet)
router.register(r'addresses', views.AddressViewSet)
router.register(r'locations', views.LocationViewSet)

# Wire up our API using automatic URL routing.
# Additionally, we include login URLs for the browsable API.
urlpatterns = [
    path('', include(router.urls)),
    path('graphql/', csrf_exempt(GraphQLView.as_view(graphiql=settings.DEBUG))),
]