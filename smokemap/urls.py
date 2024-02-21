"""smokemap URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.conf.urls.static import static

from django.urls import path, include

urlpatterns = []


urlpatterns += [
    path('', include('backend.urls')),
    # tiles server
    # path("tiles/", include("django_tiles_gl.urls")),
    #(Change graphiql=True to graphiql=False if you do not want to use the GraphiQL API browser.)
    #path("graphql/", GraphQLView.as_view(graphiql=True)),
    #exempt your Graphql endpoint from CSRF protection
    # TODO: implement CSRF
    # path('graphql/', csrf_exempt(GraphQLView.as_view(graphiql=True))),
    # upload support
]

if settings.ADMIN_ENABLED:
    from django.contrib import admin
    urlpatterns += [path('admin/', admin.site.urls),]
    #urlpatterns += static(settings.MEDIA_URL, document_root = settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)


