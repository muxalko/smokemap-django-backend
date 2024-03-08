from django.db import models
from geopy.geocoders import Nominatim
from django.core.exceptions import ValidationError
from django.contrib.gis.db import models
from django.contrib.postgres.fields import ArrayField
from django.contrib.gis import geos
from django.utils import timezone
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from .managers import CustomUserManager
# Create your models here.

# https://docs.djangoproject.com/en/5.0/topics/auth/customizing/#custom-users-and-proxy-models
# AbstractUser vs AbstractBaseUser
# The default user model in Django uses a username to uniquely identify a user during authentication. If you'd rather use an email address, you'll need to create a custom user model by either subclassing AbstractUser or AbstractBaseUser.

# Options:

# AbstractUser: Use this option if you are happy with the existing fields on the user model and just want to remove the username field.
# AbstractBaseUser: Use this option if you want to start from scratch by creating your own, completely new user model.

# This model behaves identically to the default user model, but you’ll be able to customize it in the future if the need arises:
# https://docs.djangoproject.com/en/5.0/topics/auth/customizing/#using-a-custom-user-model-when-starting-a-project
# class User(AbstractUser):
#     pass

# Created a new class called CustomUser that subclasses AbstractBaseUser
# Removed the username field
# Made the email field required and unique
# Set the USERNAME_FIELD -- which defines the unique identifier for the User model -- to email
# Specified that all objects for the class come from the CustomUserManager
class CustomUser(AbstractBaseUser, PermissionsMixin):
    name = models.CharField(max_length=25, default='visitor')
    image = models.CharField(max_length=255, default='/guest.svg')
    email = models.EmailField(unique=True)
    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    date_joined = models.DateTimeField(default=timezone.now)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = CustomUserManager()

    def __str__(self):
        return self.email 


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
    
    def save(self, *args, omit_geocode = False, **kwargs):
        validated = True
        validation_message = ''
        try: 
            check = Address.objects.get(addressString=self.addressString)
            validated = False
            validation_message = "Address already exists!"
        except Exception as e:
            print("Trying to find address in database: ", e)
            
        if (not validated):
            raise ValidationError(
                (validation_message),
                params={'value': check},
            )
        
        if not omit_geocode:
            print("Trying to resolve {} by geocode".format(self.addressString))
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
        print("Saving address with the following params:", self)
        
        return super(Address, self).save(*args, **kwargs)

    def __str__(self):
        return "{} ({},{})".format(self.addressString, self.location[0], self.location[1])

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
    requested_by = models.CharField(max_length=255, null=True)
    # images_set_id = models.CharField(blank=True, null=True, max_length=255)
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
    # images_set_id = models.CharField(blank=True, null=True, max_length=255)
    class Meta:
        verbose_name_plural = 'Places'

    def __str__(self):
        return "{}".format(self.name)
        # return "{} ({},{})".format(self.name, self.address.location[0], self.address.location[1])

class Image(models.Model):
    set_id = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    url = models.CharField(max_length=255)
    metadata = models.JSONField(blank=True, null=True)
    request = models.ForeignKey(Request, on_delete=models.DO_NOTHING, null=True)
    place = models.ForeignKey(Place, on_delete=models.DO_NOTHING, null=True)
    class Meta:
        verbose_name_plural = 'Images'

    def __str__(self):
        return self.url
    

class Location(models.Model):
    place_id = models.CharField(max_length=10)
    name = models.CharField(max_length=128)
    category = models.IntegerField()
    info = models.CharField(max_length=254,default="")
    address = models.CharField(max_length=254,default="")
    tags = models.CharField(max_length=254,default="")
    geom = models.PointField(srid=4326)
    
    class Meta:
        verbose_name_plural = 'Locations'

    def __str__(self):
        # return "{}".format(self.name)
        return "{},{},{},({}),[{}]".format(self.name, self.category, self.address, self.geom, self.tags)
