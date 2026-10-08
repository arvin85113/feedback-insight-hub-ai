from django.urls import path

from .views import AnalysisJobsView, DatasetsView, GeminiSettingsView, OverviewView, SettingsView
from .history import HistoryTransferView

app_name = "node"

urlpatterns = [
    path("history/", HistoryTransferView.as_view(), name="history-transfer"),
    path("", OverviewView.as_view(), name="overview"),
    path("settings/", SettingsView.as_view(), name="settings"),
    path("datasets/", DatasetsView.as_view(), name="datasets"),
    path("jobs/", AnalysisJobsView.as_view(), name="jobs"),
    path("settings/gemini/", GeminiSettingsView.as_view(), name="gemini-settings"),
]
