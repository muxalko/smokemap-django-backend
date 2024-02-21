from graphene_django.views import GraphQLView
from rest_framework.permissions import IsAuthenticated
from rest_framework.authentication import TokenAuthentication
from rest_framework.decorators import authentication_classes, permission_classes, api_view
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework.settings import api_settings
from rest_framework.request import Request
class DRFAuthenticatedGraphQLView(GraphQLView):
    # need this to fix the following error with missing _body when CSRF is read 
    # error msg: You cannot access body after reading from request's data stream 
    def parse_body(self, request):
        print("DRFAuthenticatedGraphQLView.parse_body()",self,request)
        if isinstance(request, Request):
            return request.data
        return super(DRFAuthenticatedGraphQLView, self).parse_body(request)
    # custom view for using DRF TokenAuthentication with graphene GraphQL.as_view()
    # all requests to Graphql endpoint will require token for auth, obtained from DRF endpoint
    # https://github.com/graphql-python/graphene/issues/249
    @classmethod
    def as_view(cls, *args, **kwargs):
        print("DRFAuthenticatedGraphQLView.as_view()", cls, args, kwargs)
        view = super(DRFAuthenticatedGraphQLView, cls).as_view(*args, **kwargs)
        view = permission_classes((IsAuthenticated,))(view)
        view = authentication_classes((api_settings.DEFAULT_AUTHENTICATION_CLASSES))(view)
        view = api_view(['POST'])(view)
        return view