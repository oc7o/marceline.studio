from django.contrib import admin
from django.urls import path

from marceline.api import api
from marceline.internal_api import internal_api

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", api.urls),
    # Machine to machine only (ADR 0028): never on the customer surface, each half only on its role.
    path("internal/v1/", internal_api.urls),
]
