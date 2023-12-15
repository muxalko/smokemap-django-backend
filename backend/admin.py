from django.contrib import admin
from django.contrib.gis.admin import OSMGeoAdmin

from .models import Category, Tag, Address, Request, Place
# Register your models here.
admin.site.register(Category)
admin.site.register(Tag)
admin.site.register(Place)
admin.site.register(Request)

@admin.register(Address)
class PlaceAdmin(OSMGeoAdmin):
    list_display = ('addressString', 'location')