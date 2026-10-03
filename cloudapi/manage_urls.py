from django.urls import path

from .manage_views import InboxManageView

app_name = "cloudapi-manage"

urlpatterns = [path("", InboxManageView.as_view(), name="inbox")]
