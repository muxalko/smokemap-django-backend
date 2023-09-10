from django.db import models
from geopy.geocoders import Nominatim
from django.core.exceptions import ValidationError
from django.contrib.gis.db import models

# Create your models here.
class Place(models.Model):
    name = models.CharField(unique=True, max_length=255)
    location = models.PointField()

class Category(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True, null=True)
    
    class Meta:
        verbose_name_plural = 'Categories'
    def __str__(self):
        return self.name

class Tag(models.Model):
    name = models.CharField(max_length=100) 
    slug = models.SlugField(unique=True, null=True)
    # the tag belongs to a category
    category = models.ForeignKey(Category, blank=True, on_delete=models.PROTECT)

    class Meta:
        verbose_name_plural = 'Tags'
    def __str__(self):
        return self.name

# cache geocoding address resolve
class Address(models.Model):
    address = models.CharField(unique=True, max_length=255)
    lat = models.FloatField(blank=True, null=True)
    long = models.FloatField(blank=True, null=True)

    def save(self, *args, **kwargs):
        geolocator = Nominatim(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/112.0")
        location = geolocator.geocode(self.address)
        if location is not None and hasattr(location,'latitude') and hasattr(location,'longitude'):
            self.lat = location.latitude
            self.long = location.longitude
        else:
            raise ValidationError(
            ('The address cannot be resolved'),
            params={'value': location},
            )
        # g = geocoder.mapbox(self.address, key=mapbox_access_token)
        # g = g.latlng  # returns => [lat, long]
        # self.lat = g[0]
        # self.long = g[1]
        return super(Address, self).save(*args, **kwargs)
    
    def __str__(self):
        return "{} ({},{})".format(self.address,self.lat,self.long)

class Request(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField()
    address = models.ForeignKey(Address, on_delete=models.PROTECT)
    # imageurl = models.URLField(blank=True)
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)
    date_approved = models.DateTimeField(blank=True, null=True)
    approved = models.BooleanField(auto_created=True, default=False)
    approved_comment = models.TextField(blank=True, null=True)
    approved_by = models.CharField(auto_created=True, blank=True, null=True, max_length=100)

    # the item belongs to one category 
    # if having a category is required please remove blank=True
    # category = models.ForeignKey(Category, blank=True) 
    #category = models.ForeignKey(Category, related_name='requests', on_delete=models.DO_NOTHING, blank=True, null=True)
    #tags = models.ManyToManyField('Tag')

    class Meta:
        ordering = ['-date_created']
    
    def __str__(self):
        return "{}: {} - {}, Address: {}".format(self.date_created,self.name,self.description,self.address.address)
