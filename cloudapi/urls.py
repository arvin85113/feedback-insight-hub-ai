from django.urls import path

from . import views

app_name = "cloudapi"

urlpatterns = [
    path("surveys/snapshot/", views.survey_snapshot, name="survey-snapshot"),
    path("surveys/changes/", views.survey_changes, name="survey-changes"),
    path("surveys/<uuid:survey_uuid>/revisions/<int:version>/", views.survey_revision, name="survey-revision"),
    path("surveys/<uuid:survey_uuid>/", views.survey_update, name="survey-update"),
    path("surveys/", views.survey_create, name="survey-create"),
    path("heartbeat/", views.heartbeat, name="heartbeat"),
]
