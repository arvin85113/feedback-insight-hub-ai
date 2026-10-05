from django.urls import path

from .views import DatasetsView, OverviewView, SettingsView

app_name = "node"

urlpatterns = [
    path("", OverviewView.as_view(), name="overview"),
    path("settings/", SettingsView.as_view(), name="settings"),
    path("datasets/", DatasetsView.as_view(), name="datasets"),
]
