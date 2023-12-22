from django.contrib import admin
from django.contrib.gis.admin import OSMGeoAdmin

from .models import Category, Tag, Address, Request, Place
# Register your models here.
admin.site.register(Category)
admin.site.register(Tag)
admin.site.register(Address)
admin.site.register(Request)

@admin.register(Place)
class PlaceAdmin(OSMGeoAdmin):
    list_display = ('name', 'address')

