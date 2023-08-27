# backend/urls.py
from django.urls import path

from backend.views import index


urlpatterns = [
    path('', index),
]