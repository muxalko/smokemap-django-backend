import fiona
import pandas as pd
import random

# import points from CSV file
points = pd.read_csv('./../data/random-coordinates-10000.csv')
print("CSV loaded:",points.head())

print("fiona.FIELD_TYPES_MAP",fiona.FIELD_TYPES_MAP)
# define schema
schema={
    'geometry':'Point',
    'properties':[
        ('name','str:128'),
        ('category','int:8'),
        ('info','str:255'),
        ('address','str:255'),
        ('tags','str:255')
        ]
}

print("schema:", schema)

# explore available drivers
print("fiona supported_drivers:",fiona.supported_drivers)
# {'AeronavFAA': 'r',
#  'ARCGEN': 'r',
#  'BNA': 'rw',
#  'DXF': 'rw',
#  'CSV': 'raw',
#  'OpenFileGDB': 'r',
#  'ESRIJSON': 'r',
#  'ESRI Shapefile': 'raw',
#  'FlatGeobuf': 'rw',
#  'GeoJSON': 'raw',
#  'GeoJSONSeq': 'rw',
#  'GPKG': 'raw',
#  'GML': 'rw',
#  'OGR_GMT': 'rw',
#  'GPX': 'rw',
#  'GPSTrackMaker': 'rw',
#  'Idrisi': 'r',
#  'MapInfo File': 'raw',
#  'DGN': 'raw',
#  'PCIDSK': 'rw',
#  'OGR_PDS': 'r',
#  'S57': 'r',
#  'SEGY': 'r',
#  'SQLite': 'raw',
#  'SUA': 'r',
#  'TopoJSON': 'r'}
#structure of fiona object
# fiona.open( fp, mode='r', driver=None, schema=None, crs=None, encoding=None, layer=None, vfs=None, enabled_drivers=None, crs_wkt=None, **kwargs, ):

target_file_path = './../data/places.shp'
mode = 'w'
driver = 'ESRI Shapefile'
crs="EPSG:4326"
with fiona.open( target_file_path, mode=mode, driver=driver, schema=schema, crs=crs) as shpfile:
    for i, row in points.iterrows():
        index = i + 1
        rowDict = { 
                    'geometry': {
                        'type': 'Point',
                        'coordinates': (row.lon, row.lat),
                    },
                    'properties': {
                         'name': 'Test place #'+str(index),
                         'category': int(random.randrange(1,10,1)),
                         'info': 'Test description for the place #'+str(index),
                         'address': 'Test address for the place #'+str(index),
                         'tags': "1,2,3,4,5"
                         }
                    }
        shpfile.write(rowDict)

