from django.urls import path

from .views import ConnectionView

app_name = "cloudsync"

urlpatterns = [path("", ConnectionView.as_view(), name="connection")]
