
from django.urls import include, path
from rest_framework import routers
from backend import views
from django.views.decorators.csrf import csrf_exempt
from graphene_django.views import GraphQLView

router = routers.DefaultRouter()

router.register(r'places', views.PlaceViewSet)

# Wire up our API using automatic URL routing.
# Additionally, we include login URLs for the browsable API.
urlpatterns = [
    path('', include(router.urls)),
    path('graphql/', csrf_exempt(GraphQLView.as_view(graphiql=True))),
]