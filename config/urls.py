from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

from .health import database_health, liveness

urlpatterns = [
    path("healthz/", liveness, name="healthz"),
    path("healthz/db/", database_health, name="healthz-db"),
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
]

if settings.IS_NODE:
    from node.setup_views import setup_view

    urlpatterns += [
        path("setup/", setup_view, name="node-setup"),
        path("auth/", include("allauth.urls")),
        # The node has no public landing page; "/" opens the console.
        path("", RedirectView.as_view(pattern_name="node:overview", permanent=False)),
        path("node/cloud/", include("cloudsync.urls")),
        path("node/", include("node.urls")),
    ]

if not settings.IS_NODE:
    urlpatterns += [
        path("api/node/v1/", include("cloudapi.urls")),
        path("dashboard/inbox/", include("cloudapi.manage_urls")),
    ]

urlpatterns += [path("", include("feedback.urls"))]
