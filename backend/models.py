from django.db import models
from geopy.geocoders import Nominatim
from django.core.exceptions import ValidationError
from django.contrib.gis.db import models
from django.contrib.postgres.fields import ArrayField
from django.contrib.gis import geos

# Create your models here.


class Category(models.Model):
    name = models.CharField(max_length=25)
    description = models.CharField(null=True, max_length=255)

    class Meta:
        verbose_name_plural = 'Categories'

    def __str__(self):
        return self.name


class Tag(models.Model):
    name = models.CharField(unique=True, max_length=50)
    # the tag belongs to a category
    # category = models.ForeignKey(Category, blank=True, on_delete=models.PROTECT)

    class Meta:
        verbose_name_plural = 'Tags'

    def __str__(self):
        return self.name

# cache geocoding address resolve
class Address(models.Model):
    addressString = models.CharField(unique=True, max_length=255)
    location = models.PointField()
    
    
          
    def save(self, *args, **kwargs):
        validated = True
        validation_message = ''
        try: 
            check = Address.objects.get(addressString=self.addressString)
            validated = False
            validation_message = "Address already exists!"
        except Exception as e:
            print("Check address exists: ", e)
            
        if (not validated):
            raise ValidationError(
                (validation_message),
                params={'value': check},
            )
        
        geolocator = Nominatim(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/112.0")
        location = geolocator.geocode(self.addressString)
        if location is not None and hasattr(location, 'latitude') and hasattr(location, 'longitude'):
            self.location = geos.Point((location.longitude, location.latitude)) 
            print("Address is resolved to ", self.location, location)
        else:
            raise ValidationError(
                ('The address cannot be resolved'),
                params={'value': self.addressString},
            )
        return super(Address, self).save(*args, **kwargs)

    def __str__(self):
        return "{} ({},{})".format(self.addressString, self.location[0], self.location[1])


def get_tags_default():
    # return list(dict([]).keys())
    return []


def get_tags_default():
    # return list(dict([]).keys())
    return []

class Request(models.Model):
    name = models.CharField(max_length=100)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, default=-1)
    description = models.CharField(max_length=255)
    address = models.ForeignKey(Address, on_delete=models.PROTECT)
    # just aray of strings on request
    tags = ArrayField(
        models.CharField(null=True, max_length=50),
        default=get_tags_default
    )
    # images = ArrayField(
    #     models.CharField(blank=True, max_length=255)
    # )
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)
    date_approved = models.DateTimeField(blank=True, null=True)
    approved = models.BooleanField(auto_created=True, default=False)
    approved_comment = models.TextField(blank=True, null=True)
    approved_by = models.CharField(
        auto_created=True, blank=True, null=True, max_length=100)

    # the item belongs to one category
    # if having a category is required please remove blank=True
    # category = models.ForeignKey(Category, blank=True)
    # category = models.ForeignKey(Category, related_name='requests', on_delete=models.DO_NOTHING, blank=True, null=True)

    class Meta:
        ordering = ['-date_created']

    def __str__(self):
        return "{}: {} - {}, {}".format(self.date_created, self.name, self.description, self.address.addressString)

class Place(models.Model):
    name = models.CharField(unique=True, max_length=255)
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, default=-1)
    description = models.TextField(max_length=255, null=True)
    address = models.ForeignKey(Address, on_delete=models.PROTECT)
    tags = models.ManyToManyField(Tag, related_name="places")

    class Meta:
        verbose_name_plural = 'Places'

    def __str__(self):
        return "{}".format(self.name)
        # return "{} ({},{})".format(self.name, self.address.location[0], self.address.location[1])
