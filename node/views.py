from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.views.generic import FormView, TemplateView
from django.core import signing
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters
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


@method_decorator(sensitive_post_parameters("api_key"), name="dispatch")
class GeminiSettingsView(NodeConsoleMixin, TemplateView):
    allowed_roles = (Role.OWNER,)
    template_name = "node/gemini_settings.html"
    active_section = "node:settings"

    def get_context_data(self, **kwargs):
        from .gemini import configured
        context = super().get_context_data(**kwargs)
        try:
            context["key_configured"] = configured()
        except Exception:
            context["credential_error"] = True
        return context

    def post(self, request, *args, **kwargs):
        from .gemini import set_key
        action = request.POST.get("action")
        value = request.POST.get("api_key", "").strip()
        if action not in ("save", "delete") or (action == "save" and (not value or len(value) > 4096)):
            messages.error(request, "請輸入有效金鑰。")
        else:
            try:
                set_key(value if action == "save" else "")
            except Exception:
                messages.error(request, "Windows 憑證庫無法使用；金鑰未寫入資料庫或紀錄。")
            else:
                record("ai.credential_changed", request=request, operation=action)
                messages.success(request, "金鑰設定已更新，舊確認將失效；沒有呼叫 API。")
        return redirect("node:gemini-settings")


class AnalysisJobsView(NodeConsoleMixin, TemplateView):
    allowed_roles = (Role.OWNER,)
    template_name = "node/jobs.html"
    active_section = "node:jobs"
    confirmation_salt = "node.ai-confirmation.v1"

    def get_context_data(self, **kwargs):
        from feedback.models import AnalysisJob, Survey
        from .models import NodeAIGrant
        from django.db.models import Count, Max, OuterRef, Subquery, Q
        from .job_display import describe_job, describe_survey
        from feedback.published_analysis import _stage_is_current
        from feedback.analysis_sources import AnalysisSourceConfigurationError, resolve_analysis_source
        from feedback.ai_stage_service import STAGE_MODULES, stage_prompt_version
        synthesis_prompt = stage_prompt_version(STAGE_MODULES["synthesis"])
        context = super().get_context_data(**kwargs)
        from .gemini import configured
        try:
            context["key_configured"] = configured()
        except Exception:
            context["key_configured"] = False
        latest_base = AnalysisJob.objects.filter(survey_id=OuterRef("pk"), executor="deterministic").order_by("-created_at", "-pk")
        surveys = list(Survey.objects.filter(analysis_enabled=True).select_related(
            "analysis_state__published_ai_stage", "analysis_source__active_external_version"
        ).annotate(reply_count=Count("submissions"), latest_reply=Max("submissions__submitted_at"),
                   latest_base_id=Subquery(latest_base.values("pk")[:1])).order_by("title")[:100])
        active = [describe_job(job) for job in AnalysisJob.objects.filter(
            status__in=("pending", "running"), survey_id__in=[s.pk for s in surveys]
        ).select_related("survey").order_by("created_at")[:200]]
        latest = {job.pk: describe_job(job) for job in AnalysisJob.objects.filter(
            pk__in=[s.latest_base_id for s in surveys if s.latest_base_id])}
        for survey in surveys:
            binding = None
            state = getattr(survey, "analysis_state", None)
            try:
                binding = resolve_analysis_source(survey)
                if binding.is_external:
                    survey.reply_count, survey.latest_reply = binding.row_count, binding.source_latest_at
                state = getattr(survey, "analysis_state", None)
                survey.base_current = bool(state and _stage_is_current(state, "statistics", binding=binding)
                    and _stage_is_current(state, "text", binding=binding))
                ai_stage = state.published_ai_stage if state else None
                survey.ai_current = bool(state and _stage_is_current(state, "ai", binding=binding)
                    and ai_stage and ai_stage.model_name == settings.GEMINI_MODEL and ai_stage.prompt_version == synthesis_prompt)
            except AnalysisSourceConfigurationError:
                survey.base_current = survey.ai_current = False
                binding = None
            survey.is_external_source = bool(binding and binding.is_external)
            survey.source_label = "本機完整資料集" if survey.is_external_source else "問卷回覆"
            describe_survey(survey, binding=binding, state=state, active_jobs=[j for j in active if j.survey_id == survey.pk],
                            latest_job=latest.get(survey.latest_base_id), key_configured=context["key_configured"])
        context["surveys"] = surveys
        from cloudsync.models import CloudLink
        from cloudsync.publication_status import decorate_publications
        decorate_publications(surveys, CloudLink.objects.filter(pk=1).first())
        context["jobs"] = [describe_job(job) for job in AnalysisJob.objects.select_related("survey").order_by("-created_at", "-pk")[:50]]
        context["work_summary"] = AnalysisJob.objects.aggregate(
            running=Count("pk", filter=Q(status="running")), waiting=Count("pk", filter=Q(status="pending")))
        context["ready_count"] = sum(s.base_current for s in surveys)
        context["attention_count"] = sum(
            s.ui_tone == "failed" or s.ui_upload_tone == "failed"
            or not s.cloud_bound for s in surveys
        )
        context["auto_refresh"] = not kwargs.get("preview")
        context["grants"] = NodeAIGrant.objects.select_related("job__survey").order_by("-pk")[:20]
        return context

    def post(self, request, *args, **kwargs):
        from django.shortcuts import get_object_or_404
        from feedback.ai_worker import AIWorkerExecutionError
        from feedback.analysis_sources import AnalysisSourceConfigurationError
        from feedback.analysis_jobs import request_job_cancel, schedule_survey_analysis
        from feedback.models import AnalysisJob, Survey, SurveyAnalysisState
        from .gemini import configured, confirm, current_identity
        action = request.POST.get("action")
        try:
            target_id = int(request.POST.get("job" if action == "cancel" else "survey", ""))
        except (TypeError, ValueError):
            from django.http import Http404
            raise Http404
        if action == "cancel":
            job = get_object_or_404(AnalysisJob, pk=target_id)
            request_job_cancel(job.pk)
            from .models import NodeAIGrant
            NodeAIGrant.objects.filter(job=job, in_flight=False).update(status="cancelled", error_code="cancelled_by_owner")
            record("analysis.cancelled", request=request, target=job.survey_id, job_id=job.pk)
            messages.success(request, "已送出取消要求；正在呼叫的 API 可能仍會計費，不會自動重呼。")
            return redirect("node:jobs")
        survey = get_object_or_404(Survey, pk=target_id, analysis_enabled=True)
        try:
            if action == "analyse":
                from django.db import transaction
                from feedback.analysis_sources import resolve_analysis_source
                with transaction.atomic():
                    Survey.objects.select_for_update().get(pk=survey.pk)
                    binding = resolve_analysis_source(survey.pk)
                    state = SurveyAnalysisState.objects.filter(survey=survey).first()
                    already_active = bool(state and AnalysisJob.objects.filter(
                        survey=survey, executor="deterministic", status__in=("pending", "running"),
                        input_version=state.input_version, config_version=state.config_version,
                        pipeline_version=state.pipeline_version, source_kind=binding.kind,
                        source_ref=binding.source_ref, source_version=binding.source_version).exists())
                    if not already_active:
                        schedule_survey_analysis(survey.pk, change="none")
                        record("analysis.requested", request=request, target=survey.pk)
                if already_active:
                    messages.info(request, "目前版本已有統計與文字工作，沒有重複排程。")
                else:
                    messages.success(request, "統計與文字工作已排程；完成後自動更新狀態，頁面不執行分析。")
            elif action == "preview-ai":
                if not configured():
                    raise AIWorkerExecutionError("key_missing")
                identity = current_identity(survey.pk)
                token = signing.dumps({"user": request.user.pk, "identity": identity,
                    "confirmation_uuid": str(uuid.uuid4())}, salt=self.confirmation_salt)
                return self.render_to_response(self.get_context_data(preview=identity, confirmation=token, selected=survey))
            elif action == "confirm-ai":
                signed = signing.loads(request.POST.get("confirmation", ""), salt=self.confirmation_salt, max_age=900)
                if signed.get("user") != request.user.pk or signed["identity"]["survey"] != survey.pk:
                    raise AIWorkerExecutionError("confirmation_expired")
                grant = confirm(survey.pk, request.user, signed["identity"], confirmation_uuid=uuid.UUID(signed["confirmation_uuid"]))
                messages.success(request, f"Gemini 工作 #{grant.job_id} 狀態：{grant.status}；相同確認不重複建立付費工作。")
            else:
                messages.error(request, "不支援的操作。")
        except AIWorkerExecutionError as exc:
            messages.error(request, {
                "previous_call_uncertain": "先前 API 呼叫結果不確定，請先查核供應商紀錄；系統不會自動重呼。",
                "paid_job_already_pending": "這份問卷已有已確認的 Gemini 工作，請查看工作狀態。",
                "windows_vault_required": "Windows 憑證庫無法使用，不能啟動 Gemini。",
            }.get(exc.code, "無法執行：請先設定金鑰、完成最新統計／文字分析，再重新預覽確認。"))
        except (AnalysisSourceConfigurationError, SurveyAnalysisState.DoesNotExist, signing.BadSignature, KeyError, ValueError):
            messages.error(request, "無法執行：請核對來源並完成最新統計／文字分析，再重新預覽確認。")
        return redirect("node:jobs")


class DatasetsView(NodeConsoleMixin, FormView):
    """OWNER-only local file validation and explicit cloud registration; GET never hashes files."""

    allowed_roles = (Role.OWNER,)
    template_name = "node/datasets.html"
    active_section = "node:datasets"
    form_class = DatasetRegistrationForm
    confirmation_salt = "node.dataset-registration.v1"

    def get_initial(self):
        """Resume a local-only source without reading or copying its data on GET."""
        from .models import LocalDatasetLocation
        initial = super().get_initial()
        survey_id = self.request.GET.get("survey", "")
        if survey_id.isdecimal():
            location = LocalDatasetLocation.objects.filter(
                version__source__survey_id=int(survey_id),
                version__active_for_sources__survey_id=int(survey_id),
            ).first()
            if location:
                initial.update(manifest_path=location.manifest_path, mapping_path=location.mapping_path)
        return initial

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
            survey_id=OuterRef("survey_id"), published_at=OuterRef("survey__analysis_state__published_at"),
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
