import time
from django.utils.deprecation import MiddlewareMixin
import logging
import json


logger = logging.getLogger( __name__ )

def safe_serialize(obj):
  default = lambda o: f"<<non-serializable: {type(o).__qualname__}>>"
  return json.dumps(obj, default=default)


class StatsMiddleware(MiddlewareMixin):

    def process_request(self, request):
        #"Store the start time when the request comes in."
        request.start_time = time.time()

    def process_response(self, request, response):
        #"Calculate and output the page generation duration"
        # Get the start time from the request and calculate how long
        # the response took.
        duration = time.time() - request.start_time

        # Add the header.
        response["X-Page-Generation-Duration-ms"] = int(duration * 1000)
        # print(safe_serialize(request.__dict__))
        #logger.debug('Took: %sms %s %s %s%s %s' % (int(duration * 1000), request.META.get('REMOTE_ADDR'), request.method, request.path, "?"+request.META.get('QUERY_STRING') if request.META.get('QUERY_STRING') else '', request.body))
        logger.debug("Took: {}ms {} {} {}{} {}".format(int(duration * 1000), request.META.get('REMOTE_ADDR'), request.method, request.path, "?"+request.META.get('QUERY_STRING') if request.META.get('QUERY_STRING') else '', request.body.decode('utf-8')))

        return response