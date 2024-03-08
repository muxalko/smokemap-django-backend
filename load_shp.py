import os
from django.contrib.gis.utils import LayerMapping
from backend.models import Location

# Auto-generated `LayerMapping` dictionary for Location model
location_mapping = {
    'name': 'name',
    'category': 'category',
    'info': 'info',
    'address': 'address',
    'tags': 'tags',
    'geom': 'POINT',
}

location_shapefile = './../data/places.shp'

def run(verbose=True):
    layermap = LayerMapping(Location,location_shapefile,location_mapping,transform=False)
    layermap.save(strict=True,verbose=verbose)