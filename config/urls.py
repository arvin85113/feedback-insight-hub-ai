from django.contrib import admin
from django.urls import include, path

from .health import database_health, liveness

urlpatterns = [
    path("healthz/", liveness, name="healthz"),
    path("healthz/db/", database_health, name="healthz-db"),
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("", include("feedback.urls")),
]
