from django.urls import path

from . import views

app_name = "cloudapi"

urlpatterns = [
    path("datasets/register/", views.external_dataset_register, name="dataset-register"),
    path("surveys/snapshot/", views.survey_snapshot, name="survey-snapshot"),
    path("surveys/changes/", views.survey_changes, name="survey-changes"),
    path("surveys/<uuid:survey_uuid>/revisions/<int:version>/", views.survey_revision, name="survey-revision"),
    path("surveys/<uuid:survey_uuid>/", views.survey_update, name="survey-update"),
    path("surveys/", views.survey_create, name="survey-create"),
    path("heartbeat/", views.heartbeat, name="heartbeat"),
    path("inbox/", views.inbox_list, name="inbox"),
    path("inbox/ack/", views.inbox_ack, name="inbox-ack"),
    path("inbox/quarantine/", views.inbox_quarantine, name="inbox-quarantine"),
    path("results/", views.results_upload, name="results"),
]
