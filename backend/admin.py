from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .forms import CustomUserCreationForm, CustomUserChangeForm
from .models import CustomUser
from django.contrib.gis.admin import OSMGeoAdmin

from .models import Category, Tag, Address, Request, Place, Image

class CustomUserAdmin(UserAdmin):
    add_form = CustomUserCreationForm
    form = CustomUserChangeForm
    model = CustomUser
    list_display = ("name", "email", "is_staff", "is_active",)
    list_filter = ("name", "email", "is_staff", "is_active",)
    fieldsets = (
        (None, {"fields": ("name", "image", "email", "password")}),
        ("Permissions", {"fields": ("is_staff", "is_active", "groups", "user_permissions")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": (
                "name", "image", "email", "password1", "password2", "is_staff",
                "is_active", "groups", "user_permissions"
            )}
        ),
    )
    search_fields = ("name", "email",)
    ordering = ("name", "email",)

# Register your models here.
admin.site.register(CustomUser, CustomUserAdmin)
admin.site.register(Category)
admin.site.register(Tag)
admin.site.register(Address)
admin.site.register(Request)
admin.site.register(Image)
@admin.register(Place)
class PlaceAdmin(OSMGeoAdmin):
    list_display = ('name', 'address')

