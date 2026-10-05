from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.views.generic import FormView, TemplateView
from django.core import signing
import uuid

from feedback.views import DashboardBaseMixin
from organizations.access import organization_role
from organizations.models import Organization, OrganizationMembership

from .audit import ACTION_LABELS, ORGANIZATION_RENAMED, record
from .forms import DatasetRegistrationForm, OrganizationSettingsForm
from .models import NodeAuditEvent
from .status import (
    cloud_status,
    database_status,
    disk_status,
    inbox_status,
    results_status,
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
            results_status(),
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


class DatasetsView(NodeConsoleMixin, FormView):
    """OWNER-only local file validation and explicit cloud registration; GET never hashes files."""

    allowed_roles = (Role.OWNER,)
    template_name = "node/datasets.html"
    active_section = "node:datasets"
    form_class = DatasetRegistrationForm
    confirmation_salt = "node.dataset-registration.v1"

    def get_context_data(self, **kwargs):
        from cloudsync.models import CloudLink, ResultUpload
        from django.db.models import OuterRef, Subquery
        from feedback.models import SurveyAnalysisSource
        context = super().get_context_data(**kwargs)
        context["link"] = CloudLink.objects.filter(pk=1).first()
        # Metadata only: bounded list, no stat/hash/Parquet scan on page load.
        context["sources"] = SurveyAnalysisSource.objects.filter(kind="external").select_related(
            "survey", "survey__analysis_state", "active_external_version", "active_external_version__local_location"
        ).annotate(last_upload_status=Subquery(ResultUpload.objects.filter(
            survey_id=OuterRef("survey_id")
        ).order_by("-publish_sequence", "-pk").values("status")[:1])).order_by("survey__title")[:100]
        return context

    def form_valid(self, form):
        from cloudapi.errors import DefinitionCommitError
        from cloudsync.datasets import register_dataset
        from cloudsync.models import CloudLink, StaleLink
        from feedback.external_dataset import ExternalDatasetInvalid, file_hash, validate_external_dataset
        from feedback.importing.mapping import MappingConfigError, load_mapping
        from feedback.importing.service import mapping_definition
        from feedback.models import Survey

        try:
            verified = validate_external_dataset(form.cleaned_data["manifest_path"], form.cleaned_data["mapping_path"])
            mapping = load_mapping(verified.mapping_path)
            if file_hash(verified.mapping_path) != verified.mapping_sha256:
                raise ExternalDatasetInvalid("mapping 已改變")
            registration = verified.registration
            # Same mapping/schema => same survey across file revisions and lost replies.
            survey_uuid = uuid.uuid5(uuid.NAMESPACE_URL, "feedback-external:" + ":".join(
                registration[key] for key in ("source_ref", "mapping_key", "schema_sha256")
            ))
            definition = mapping_definition(mapping, survey_uuid)
            link = CloudLink.objects.filter(pk=1).first() or CloudLink()
            existing = Survey.objects.filter(uuid=survey_uuid).first()
            expected = existing.definition_version if existing else 0
            base = {"user": self.request.user.pk, "manifest": str(verified.manifest_path),
                    "mapping": str(verified.mapping_path), "manifest_hash": verified.manifest_sha256,
                    "mapping_hash": verified.mapping_sha256, "survey_uuid": str(survey_uuid),
                    "generation": link.generation}
            if self.request.POST.get("action") == "register":
                confirmation = signing.loads(form.cleaned_data["confirmation"], salt=self.confirmation_salt, max_age=900)
                if any(confirmation.get(key) != value for key, value in base.items()):
                    raise ExternalDatasetInvalid("預覽後檔案或雲端連結已變更，請重新驗證")
                survey, version = register_dataset(verified, definition,
                    expected_version=confirmation["expected_version"], generation=link.generation,
                    actor=self.request.user)
                messages.success(self.request, "來源已登錄，檔案留在原位置；統計與文字分析由本機 Worker 處理。")
                return redirect("node:datasets")
            confirmation = signing.dumps({**base, "expected_version": expected}, salt=self.confirmation_salt)
            form = self.form_class(initial={**form.cleaned_data, "confirmation": confirmation})
            return self.render_to_response(self.get_context_data(form=form, preview=registration,
                survey_title=mapping.survey.title))
        except (ExternalDatasetInvalid, MappingConfigError, OSError, signing.BadSignature):
            form.add_error(None, "驗證失敗或確認已逾期，請核對本機檔案並重新驗證；沒有建立本機登錄。")
        except StaleLink:
            form.add_error(None, "雲端連結已變更，請重新載入並驗證。")
        except DefinitionCommitError as exc:
            form.add_error(None, exc.user_message + "；尚未完成本機登錄。可重新驗證後重送同一來源。")
        return self.form_invalid(form)
