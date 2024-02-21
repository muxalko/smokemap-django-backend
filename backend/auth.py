from django.contrib.auth.models import AnonymousUser
from django.contrib.auth.backends import ModelBackend
# from django.contrib.gis.geoip2 import GeoIP2
# from django.contrib.gis import geos

class Visitor(AnonymousUser):
    """
    Extension of `AnonymousUser` class:
    - Adds the `ip` [,`city` and `geolocation`] parameter to the `AnonymousUser` instance.
    - Ensures that the `is_authenticated` property returns `True`.
    """
    def __init__(self, ip):
        super().__init__()
        self.ip = ip
        # self.geolocation = geolocation
        # self.city = city
   
    @property
    def is_authenticated(self):
        return True

class VisitorAuthentication(ModelBackend):
    def authenticate(self, request):
        """
        Authenticates the incoming request using the request's API key 
        or Token, and creates an `AnonymousUser` that is associated with
        the `Visitor` instance that corresponds to the request.
        """

        # Authentication logic goes here. 
        # It pairs the API key or Token with the `Visitor` 
        # instance that it belongs to.
        # ...
        # ...
        # Try to register client IP

        client_ip = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', '')).split(',')[0].strip()
        # g = GeoIP2()
        # lat,long = g.lat_lon(client_ip)
        # location = geos.Point(lat,long)
        user = Visitor(client_ip)

        return user, None