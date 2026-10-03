from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.views.generic import FormView, TemplateView

from feedback.views import DashboardBaseMixin
from organizations.access import organization_role
from organizations.models import Organization, OrganizationMembership

from .audit import ACTION_LABELS, ORGANIZATION_RENAMED, record
from .forms import OrganizationSettingsForm
from .models import NodeAuditEvent
from .status import (
    cloud_status,
    database_status,
    disk_status,
    inbox_status,
    lan_status,
    pending_items,
    worker_status,
)

Role = OrganizationMembership.Role


class NodeConsoleMixin(DashboardBaseMixin):
    """Console pages: manager shell plus an organization-level role."""

    allowed_roles = (Role.OWNER, Role.ADMIN)

    def test_func(self):
        return super().test_func() and organization_role(self.request.user) in self.allowed_roles

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.get_dashboard_base_context())
        return context


class OverviewView(NodeConsoleMixin, TemplateView):
    template_name = "node/overview.html"
    active_section = "node:overview"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        paths = settings.NODE_PATHS
        items = [
            database_status(),
            worker_status(paths),
            disk_status(paths),
            lan_status(),
            cloud_status(),
            inbox_status(),
        ]
        events = list(NodeAuditEvent.objects.all()[:10])
        for event in events:
            event.label = ACTION_LABELS.get(event.action, event.action)
        context.update(
            {
                "organization": Organization.current(),
                "status_items": items,
                "pending": pending_items(items),
                "recent_events": events,
                "data_root": paths.root,
            }
        )
        return context


class SettingsView(NodeConsoleMixin, FormView):
    template_name = "node/settings.html"
    active_section = "node:settings"
    form_class = OrganizationSettingsForm

    def get_initial(self):
        organization = Organization.current()
        return {"name": organization.name if organization else ""}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["data_root"] = settings.NODE_PATHS.root
        return context

    def form_valid(self, form):
        organization = Organization.current()
        previous, new = organization.name, form.cleaned_data["name"]
        if previous != new:
            organization.name = new
            organization.save(update_fields=["name"])
            record(ORGANIZATION_RENAMED, request=self.request, target=new, **{"from": previous, "to": new})
            messages.success(self.request, "組織名稱已更新。")
        return redirect("node:settings")
