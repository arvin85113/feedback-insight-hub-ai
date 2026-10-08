import uuid
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.db.models import Count, IntegerField, Max, OuterRef, Q, Subquery
from django.db.models.fields.json import KT
from django.db.models.functions import Cast, Coalesce, TruncDate
import segno

from django.http import HttpResponseForbidden, Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.text import slugify
from django.views.generic import CreateView, DeleteView, DetailView, TemplateView, UpdateView, View

from .ai_snapshot_service import (
    serialize_ai_report_content,
    serialize_evidence_for_display,
)
from .forms import (
    ImprovementEditForm,
    ImprovementNoticeConfirmationForm,
    ImprovementNoticeForm,
    ImprovementStatusTransitionForm,
    ImprovementUpdateForm,
    RespondentMetaForm,
    SurveyCreateForm,
    SurveyEditForm,
    SurveyFormBuilder,
    UI_TYPE_LABELS,
)
from .improvement_workflow import (
    ImprovementTransitionError,
    record_initial_status,
    status_targets,
    transition_improvement,
)
from .models import (
    FeedbackSubmission,
    ImprovementDispatch,
    ImprovementNotice,
    ImprovementUpdate,
    KeywordCategory,
    Question,
    Survey,
    SurveyAIAnalysisStage,
    SurveyAIReportSnapshot,
    SurveyAnalysisSource,
    SurveyAnalysisState,
    SurveyCategory,
)
from .notice_service import (
    NoticeConfirmationError,
    begin_notice_retry,
    prepare_notice_dispatches,
    resolve_notice_recipients,
    send_notice_batch,
)
from . import local_service
from .published_analysis import (
    get_published_ai_pipeline_status,
    get_published_analysis_payload,
    is_published_ai_stage_current,
)


def analysis_visible_surveys():
    return (
        Survey.objects.filter(analysis_enabled=True, archived_at__isnull=True)
        .filter(Q(is_active=True) | Q(dataset_import_batches__isnull=False)
                | Q(analysis_source__kind=SurveyAnalysisSource.Kind.EXTERNAL))
        .distinct()
    )


def analysis_report_surveys():
    published_response_count = SurveyAnalysisState.objects.filter(
        survey_id=OuterRef("pk"),
        published_snapshot__isnull=False,
    ).values("published_snapshot__response_count")[:1]
    uploaded_response_count = (
        SurveyAnalysisState.objects.filter(survey_id=OuterRef("pk"), published_upload_uuid__isnull=False)
        .annotate(analyzed=Cast(KT("publication_manifest__coverage__analyzed_unique"), IntegerField()))
        .values("analyzed")[:1]
    )
    return (
        analysis_visible_surveys()
        .annotate(
            response_count=Count("submissions"),
            valid_response_count=Coalesce(
                Subquery(published_response_count, output_field=IntegerField()),
                Subquery(uploaded_response_count, output_field=IntegerField()),
                Count(
                    "submissions",
                    filter=Q(
                        submissions__is_complete=True,
                        submissions__voided_at__isnull=True,
                    ),
                ),
                output_field=IntegerField(),
            ),
        )
        .order_by("title")
    )


def external_source_totals(survey_ids):
    """Row count and latest date of each survey's active external dataset.

    Surveys analysed from a registered external dataset (local Parquet) keep their
    rows outside the database, so counting FeedbackSubmission rows would show 0.
    """

    return {
        source.survey_id: (
            source.active_external_version.row_count,
            source.active_external_version.source_latest_at,
        )
        for source in SurveyAnalysisSource.objects.filter(
            survey_id__in=survey_ids,
            kind=SurveyAnalysisSource.Kind.EXTERNAL,
            active_external_version__isnull=False,
        ).select_related("active_external_version")
    }


def _survey_catalog_rows(queryset):
    """Attach lightweight counts without multiplying questions by submissions."""
    surveys = list(queryset)
    survey_ids = [survey.pk for survey in surveys]
    if not survey_ids:
        return surveys

    question_counts = {
        row["survey_id"]: row
        for row in (
            Question.objects.filter(survey_id__in=survey_ids)
            .values("survey_id")
            .annotate(
                question_count=Count("id"),
                text_question_count=Count(
                    "id",
                    filter=Q(data_type=Question.DataType.TEXT),
                ),
            )
        )
    }
    submission_counts = {
        row["survey_id"]: row
        for row in (
            FeedbackSubmission.objects.filter(survey_id__in=survey_ids)
            .values("survey_id")
            .annotate(
                response_count=Count("id"),
                latest_submission_at=Max("submitted_at"),
            )
        )
    }
    published_counts = {
        row["survey_id"]: row["published_snapshot__response_count"]
        for row in SurveyAnalysisState.objects.filter(
            survey_id__in=survey_ids,
            published_snapshot__isnull=False,
        ).values("survey_id", "published_snapshot__response_count")
    }
    # Node uploads have no local Snapshot on the cloud; their coverage carries the analysed count.
    for row in SurveyAnalysisState.objects.filter(
        survey_id__in=survey_ids,
        published_snapshot__isnull=True,
        published_upload_uuid__isnull=False,
    ).values("survey_id", "publication_manifest"):
        coverage = (row["publication_manifest"] or {}).get("coverage") or {}
        value = coverage.get("analyzed_unique")
        if isinstance(value, int) and not isinstance(value, bool):
            published_counts[row["survey_id"]] = value
    for survey in surveys:
        question_row = question_counts.get(survey.pk, {})
        submission_row = submission_counts.get(survey.pk, {})
        survey.question_count = question_row.get("question_count", 0)
        survey.text_question_count = question_row.get("text_question_count", 0)
        survey.response_count = published_counts.get(
            survey.pk,
            submission_row.get("response_count", 0),
        )
        survey.latest_submission_at = submission_row.get("latest_submission_at")
    return surveys


class ManagerRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_manager


class CustomerRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_authenticated and not self.request.user.is_manager


NODE_CONSOLE_NAV = [("node:overview", "節點總覽", "server"), ("node:datasets", "資料集", "database"),
                    ("node:jobs", "分析工作", "chart")]
NODE_CONSOLE_NAV_TAIL = [("cloudsync:connection", "雲端連線", "cloud"), ("node:settings", "設定", "gear")]


class DashboardBaseMixin(ManagerRequiredMixin):
    dashboard_nav = [
        ("feedback:dashboard", "營運總覽", "grid"),
        ("feedback:analysis-operations", "營運分析", "spark"),
        ("feedback:survey-manager", "問卷管理", "clipboard"),
        ("feedback:stats-overview", "統計分析", "chart"),
        ("feedback:text-analysis", "文字洞察", "message"),
        ("feedback:improvement-list", "改善追蹤", "wrench"),
        ("feedback:notice-center", "通知中心", "send"),
    ]

    active_section = ""

    def get_dashboard_nav(self):
        if settings.IS_NODE:
            return NODE_CONSOLE_NAV + self.dashboard_nav + NODE_CONSOLE_NAV_TAIL
        if settings.CLOUD_INBOX_ENABLED:
            return self.dashboard_nav + [("cloudapi-manage:inbox", "收件匣", "server")]
        return self.dashboard_nav

    def get_dashboard_base_context(self):
        nav = self.get_dashboard_nav()
        return {
            "dashboard_nav": nav,
            "active_section": self.active_section,
            "section_label": next(
                (label for route, label, _icon in nav if route == self.active_section),
                "管理工作區",
            ),
            "survey_list": analysis_visible_surveys().order_by("title"),
        }


class HomeView(TemplateView):
    template_name = "feedback/home.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        dashboard_url = reverse("feedback:dashboard")
        customer_url = reverse("feedback:customer-home")

        def login_url(next_url):
            return f"{reverse('accounts:login')}?{urlencode({'next': next_url})}"

        if self.request.user.is_authenticated:
            workspace_url = dashboard_url if self.request.user.is_manager else customer_url
        else:
            workspace_url = reverse("accounts:login")

        context.update(
            {
                "workspace_url": workspace_url,
                "manager_entry_url": (
                    dashboard_url
                    if self.request.user.is_authenticated and self.request.user.is_manager
                    else login_url(dashboard_url)
                ),
                "customer_entry_url": (
                    customer_url
                    if self.request.user.is_authenticated and not self.request.user.is_manager
                    else login_url(customer_url)
                ),
            }
        )
        return context


class CustomerHomeView(CustomerRequiredMixin, TemplateView):
    template_name = "feedback/customer_home.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        payload = local_service.get_customer_home_payload(self.request.user)
        submission_rows = payload.get("submission_rows", [])
        for row in submission_rows:
            submission = row.get("submission", {})
            if row.get("latest_notice"):
                row["status_key"] = "improved"
                row["status_label"] = "已促成改善"
                row["status_class"] = "pill-active"
            elif submission.get("consent_follow_up"):
                row["status_key"] = "tracking"
                row["status_label"] = "願意接收追蹤"
                row["status_class"] = "pill-active"
            else:
                row["status_key"] = "pending"
                row["status_label"] = "待處理"
                row["status_class"] = ""

        status_counts = {
            "all": len(submission_rows),
            "pending": sum(1 for row in submission_rows if row["status_key"] == "pending"),
            "tracking": sum(1 for row in submission_rows if row["status_key"] == "tracking"),
            "improved": sum(1 for row in submission_rows if row["status_key"] == "improved"),
        }
        active_status = self.request.GET.get("status", "all")
        if active_status not in status_counts:
            active_status = "all"
        payload["submission_rows"] = (
            submission_rows
            if active_status == "all"
            else [row for row in submission_rows if row["status_key"] == active_status]
        )
        payload["submission_status_counts"] = status_counts
        payload["active_submission_status"] = active_status
        context.update(payload)
        return context


class CustomerNotificationsView(CustomerRequiredMixin, TemplateView):
    template_name = "feedback/customer_notifications.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(local_service.get_customer_notifications_payload(self.request.user))
        context["notification_opt_in"] = self.request.user.notification_opt_in
        return context


class MarkNoticeReadView(CustomerRequiredMixin, View):
    def post(self, request, pk):
        dispatch = get_object_or_404(
            ImprovementDispatch,
            Q(recipient_user=request.user) | Q(submission__user=request.user),
            pk=pk,
            delivery_status=ImprovementDispatch.DeliveryStatus.SENT,
        )
        dispatch.is_read = True
        dispatch.save(update_fields=["is_read"])
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": True})
        return redirect("feedback:customer-notifications")


class DashboardView(DashboardBaseMixin, TemplateView):
    template_name = "feedback/dashboard_overview.html"
    active_section = "feedback:dashboard"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.get_dashboard_base_context())
        context.update(local_service.get_dashboard_payload())
        context["ai_report_surveys"] = analysis_report_surveys()
        return context


class AnalysisOperationsView(DashboardBaseMixin, TemplateView):
    template_name = "feedback/analysis_operations.html"
    active_section = "feedback:analysis-operations"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.get_dashboard_base_context())
        context["ai_report_surveys"] = analysis_report_surveys()
        return context


class AIReportStatusView(ManagerRequiredMixin, View):
    def get(self, request, slug):
        survey = get_object_or_404(analysis_visible_surveys(), slug=slug)
        return JsonResponse({"ok": True, **get_published_ai_pipeline_status(survey)})


class AIStagePipelineStatusView(ManagerRequiredMixin, View):
    def get(self, request, slug):
        survey = get_object_or_404(analysis_visible_surveys(), slug=slug)
        return JsonResponse({"ok": True, **get_published_ai_pipeline_status(survey)})


class SurveyManagerView(DashboardBaseMixin, TemplateView):
    template_name = "feedback/survey_manager.html"
    active_section = "feedback:survey-manager"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.get_dashboard_base_context())
        sort = self.request.GET.get("sort", "newest")
        category_id = self.request.GET.get("category", "")

        qs = (
            Survey.objects.filter(archived_at__isnull=True)
            .prefetch_related("questions")
            .select_related("category")
            .annotate(
                submission_count=Count("submissions"),
                latest_submission_at=Max("submissions__submitted_at"),
            )
        )
        if category_id:
            qs = qs.filter(category_id=category_id)
        if sort == "oldest":
            qs = qs.order_by("created_at")
        elif sort == "title":
            qs = qs.order_by("title")
        else:
            qs = qs.order_by("-created_at")

        # ── 近 3 日每日回覆數 ──
        today = timezone.localdate()
        trend_days = [today - timedelta(days=i) for i in range(2, -1, -1)]  # [day-2, day-1, today]
        recent_rows = (
            FeedbackSubmission.objects
            .filter(submitted_at__date__gte=trend_days[0])
            .annotate(sub_date=TruncDate("submitted_at"))
            .values("survey_id", "sub_date")
            .annotate(cnt=Count("id"))
        )
        count_map = {}
        for row in recent_rows:
            count_map.setdefault(row["survey_id"], {})[row["sub_date"]] = row["cnt"]

        surveys_list = list(qs)
        external_totals = external_source_totals([survey.id for survey in surveys_list])
        from cloudapi.receipts import received_counts

        received = received_counts([survey.id for survey in surveys_list])
        for survey in surveys_list:
            if survey.id in external_totals:
                survey.submission_count, survey.latest_submission_at = external_totals[survey.id]
            else:
                survey.submission_count = received.get(survey.id, survey.submission_count)
            day_map = count_map.get(survey.id, {})
            survey.trend = [day_map.get(d, 0) for d in trend_days]
            survey.trend_max = max(survey.trend) if any(survey.trend) else 1

        context["surveys"] = surveys_list
        context["trend_days"] = trend_days
        context["categories"] = SurveyCategory.objects.all()
        context["current_sort"] = sort
        context["current_category"] = category_id
        return context


class SurveyCategoryCreateView(ManagerRequiredMixin, View):
    def post(self, request):
        if settings.IS_NODE:
            messages.info(request, "分類由雲端管理；請在雲端網站新增或刪除分類。")
            return redirect("feedback:survey-manager")
        name = request.POST.get("name", "").strip()
        if not name:
            messages.error(request, "分類名稱不能空白。")
            return redirect("feedback:survey-manager")
        if SurveyCategory.objects.filter(name=name).exists():
            messages.error(request, f"分類「{name}」已存在。")
            return redirect("feedback:survey-manager")
        SurveyCategory.objects.create(name=name)
        messages.success(request, f"分類「{name}」已建立。")
        return redirect("feedback:survey-manager")


class SurveyCategoryDeleteView(ManagerRequiredMixin, View):
    def post(self, request, pk):
        if settings.IS_NODE:
            messages.info(request, "分類由雲端管理；請在雲端網站新增或刪除分類。")
            return redirect("feedback:survey-manager")
        category = get_object_or_404(SurveyCategory, pk=pk)
        name = category.name
        from cloudapi.definition import serialize_definition, update_survey
        from feedback.survey_lifecycle import commit

        with transaction.atomic():
            for survey in category.surveys.all():
                definition = serialize_definition(survey)
                update_survey(definition, {"category": None})
                commit(survey, definition, survey.definition_version)
            category.delete()
        messages.success(request, f"分類「{name}」已刪除。")
        return redirect("feedback:survey-manager")


NODE_MISSING_NOTICE = "尚未連接本機節點，發布後不會產生分析"


def active_nodes():
    """Nodes a new website draft may belong to (node-only analysis spec §5); none outside the cloud prototype."""

    from cloudapi.models import NodeDevice

    if settings.IS_NODE or not settings.CLOUD_SYNC_PROTOTYPE_ENABLED:
        return NodeDevice.objects.none()
    return NodeDevice.objects.filter(status=NodeDevice.Status.ACTIVE)


SELF_TEST_ONLY_NOTICE = "這份問卷由本機節點收件；收件匣目前只開放自測問卷，無法發布"


def node_notice(survey=None):
    if settings.IS_NODE:
        return ""
    if survey is not None and survey.owner_node_id:
        from cloudapi.definition import serialize_definition
        from cloudapi.inbox import inbox_scope_allows

        # Same rule as the publish check: external-source surveys never use the inbox.
        unpublishable = survey.published_version is None and not inbox_scope_allows(survey)
        return SELF_TEST_ONLY_NOTICE if unpublishable and not serialize_definition(survey).get("external_source") else ""
    if not settings.CLOUD_SYNC_PROTOTYPE_ENABLED:
        return ""
    return "" if active_nodes().exists() else NODE_MISSING_NOTICE


class SurveyCreateView(DashboardBaseMixin, CreateView):
    template_name = "feedback/survey_create.html"
    form_class = SurveyCreateForm
    active_section = "feedback:survey-manager"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.get_dashboard_base_context())
        context["node_notice"] = node_notice()
        if settings.IS_NODE:
            import uuid as uuid_module

            posted = self.request.POST.get("survey_uuid", "") if self.request.method == "POST" else ""
            context["pending_survey_uuid"] = posted or str(uuid_module.uuid4())
        return context

    def form_valid(self, form):
        import uuid as uuid_module

        from cloudapi.errors import DefinitionCommitError
        from feedback.survey_lifecycle import create_draft

        survey_uuid = uuid_module.uuid4()
        if settings.IS_NODE:
            try:
                survey_uuid = uuid_module.UUID(self.request.POST.get("survey_uuid", ""))
            except ValueError:
                messages.error(self.request, "表單已過期，請重新開啟建立問卷頁。")
                return self.form_invalid(form)
        data = form.cleaned_data
        nodes = list(active_nodes()[:2])
        if len(nodes) > 1:
            # Several nodes: never pick one silently (spec §5).
            messages.error(self.request, "已連接多個本機節點，請先在後台撤銷不用的節點再建立問卷。")
            return self.form_invalid(form)
        try:
            with transaction.atomic():
                self.object = create_draft({
                    "survey_uuid": survey_uuid, "title": data["title"], "description": data.get("description", ""),
                    "is_active": True, "analysis_enabled": data.get("analysis_enabled", True),
                    "thank_you_email_enabled": data.get("thank_you_email_enabled", True),
                    "category": data["category"].name if data.get("category") else None,
                })
                if nodes:
                    from cloudapi.writes import assign_survey_to_node

                    self.object = assign_survey_to_node(self.object, nodes[0]).survey
        except DefinitionCommitError as exc:
            messages.error(self.request, exc.user_message)
            return self.form_invalid(form)
        return HttpResponseRedirect(self.get_success_url())

    def get_success_url(self):
        return reverse("feedback:survey-builder", args=[self.object.slug])


class SurveyBuilderView(DashboardBaseMixin, DetailView):
    template_name = "feedback/survey_builder.html"
    context_object_name = "survey"
    model = Survey
    slug_field = "slug"
    slug_url_kwarg = "slug"
    active_section = "feedback:survey-manager"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.get_dashboard_base_context())
        from .builder_cards import build_cards

        context["cards"], context["new_card"] = build_cards(self.object, kwargs.get("card_form"))
        context["card_conflict"] = kwargs.get("card_conflict", False)
        context["card_error"] = kwargs.get("card_error")
        context["read_only"] = self.object.published_version is not None
        context["ui_types"] = UI_TYPE_LABELS
        context["scale_max_range"] = range(2, 11)
        context["survey_edit_form"] = kwargs.get("survey_edit_form") or SurveyEditForm(instance=self.object)
        external = external_source_totals([self.object.pk]).get(self.object.pk)
        if external:
            context["responses_count"], context["latest_response_at"] = external
        else:
            from cloudapi.receipts import received_counts

            context["responses_count"] = received_counts([self.object.pk])[self.object.pk]
            latest = self.object.submissions.order_by("-submitted_at").only("submitted_at").first()
            context["latest_response_at"] = latest.submitted_at if latest else None
        context["active_tab"] = self.request.GET.get("tab", "questions")
        context["node_notice"] = node_notice(self.object)
        return context

    def post(self, request, *args, **kwargs):
        from cloudapi.builder import builder_post
        from feedback.survey_lifecycle import commit

        self.object = self.get_object()
        return builder_post(self, request, commit)


class SurveyQRCodeView(ManagerRequiredMixin, View):
    def get(self, request, slug):
        survey = get_object_or_404(Survey, slug=slug)
        base_url = request.build_absolute_uri('/')[:-1]
        survey_url = f"{base_url}/survey/{survey.slug}/"
        qr = segno.make(survey_url, error='m')
        response = HttpResponse(content_type='image/svg+xml')
        qr.save(response, kind='svg', scale=4, border=2)
        return response


class SurveyDeleteView(DashboardBaseMixin, DeleteView):
    model = Survey
    success_url = reverse_lazy("feedback:survey-manager")

    def get_queryset(self):
        return Survey.objects.all()

    def form_valid(self, form):
        from cloudapi.builder import delete_post

        delete_post(self.request, self.get_object())
        return HttpResponseRedirect(self.get_success_url())


class StatsOverviewView(DashboardBaseMixin, TemplateView):
    template_name = "feedback/stats_overview.html"
    active_section = "feedback:stats-overview"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        selected_slug = self.request.GET.get("survey")
        sort = self.request.GET.get("sort", "newest")
        category_id = self.request.GET.get("category", "")
        survey = Survey.objects.filter(slug=selected_slug).first() if selected_slug else None
        context.update(self.get_dashboard_base_context())
        context["selected_survey"] = survey
        stats_surveys = analysis_visible_surveys().select_related("category")
        if category_id:
            stats_surveys = stats_surveys.filter(category_id=category_id)
        if sort == "oldest":
            stats_surveys = stats_surveys.order_by("created_at")
        elif sort == "title":
            stats_surveys = stats_surveys.order_by("title")
        else:
            stats_surveys = stats_surveys.order_by("-created_at")
        context["stats_survey_rows"] = _survey_catalog_rows(stats_surveys)
        context["categories"] = SurveyCategory.objects.all()
        context["current_sort"] = sort
        context["current_category"] = category_id
        # Pages only read published results; statistics are computed by the local worker.
        publication = get_published_analysis_payload(survey) if survey else None
        payload = publication["statistics"] if publication else {}
        context["analysis_publication"] = publication
        context["analysis_stage_fresh"] = publication["freshness"]["statistics"] if publication else None
        context["charts"] = payload.get("charts", [])
        context["question_analysis"] = payload.get("question_analysis", [])
        inferential = payload.get("inferential_analysis", [])
        context["inferential_analysis"] = inferential
        context["available_tests_count"] = sum(1 for r in inferential if not r.get("skipped_reason"))
        context["skipped_tests_count"] = sum(1 for r in inferential if r.get("skipped_reason"))
        _group_defs = [
            {
                "key": "mean_comparison",
                "badge": "平均數比較", "badge_class": "method-mean",
                "title": "名目分組 × 連續結果",
                "desc": "2 組跑 Welch t-test，3-5 組跑單因子 ANOVA，並附上效果量。",
                "empty_help": "此問卷沒有可搭配的連續型（小數）題目；1–10 量表屬順序資料，分組結果請查看「排序與關聯」。",
                "families": ("mean_comparison",),
            },
            {
                "key": "categorical_association",
                "badge": "類別關聯", "badge_class": "method-category",
                "title": "名目 × 名目",
                "desc": "單選名目題之間跑卡方檢定；多選題只做多重回應頻率，不當分組。",
                "empty_help": "至少需要兩個具有效回覆的單選名目題；多選題不會作為卡方分組欄位。",
                "families": ("categorical_association",),
            },
            {
                "key": "rank_correlation",
                "badge": "順序 / 相關", "badge_class": "method-rank",
                "title": "排序與關聯",
                "desc": "名目 × 順序跑非母數檢定；連續 × 連續跑 Pearson，涉及順序資料跑 Spearman。",
                "empty_help": "需要可比較的順序或數值題目，且各組有效回覆須符合檢定最低條件。",
                "families": ("nonparametric_rank", "correlation"),
            },
        ]
        context["inference_groups"] = [
            {**g, "results": [r for r in inferential if r.get("analysis_family") in g["families"]]}
            for g in _group_defs
        ]
        return context


class KeywordCategoryCreateView(ManagerRequiredMixin, View):
    def post(self, request):
        slug = request.POST.get("survey_slug", "").strip()
        keyword = request.POST.get("keyword", "").strip()
        category = request.POST.get("category", "").strip()
        threshold = request.POST.get("threshold", "2").strip()
        survey = get_object_or_404(Survey, slug=slug)
        if not keyword or not category:
            messages.error(request, "關鍵字與分類名稱不能空白。")
            return redirect(f"{reverse('feedback:text-analysis')}?survey={slug}#text-rules")
        try:
            threshold = int(threshold)
            if threshold < 1:
                raise ValueError
        except ValueError:
            messages.error(request, "門檻值須為正整數。")
            return redirect(f"{reverse('feedback:text-analysis')}?survey={slug}#text-rules")
        if KeywordCategory.objects.filter(survey=survey, keyword=keyword).exists():
            messages.error(request, f"關鍵字「{keyword}」已有分類規則。")
            return redirect(f"{reverse('feedback:text-analysis')}?survey={slug}#text-rules")
        KeywordCategory.objects.create(survey=survey, keyword=keyword, category=category, threshold=threshold)
        messages.success(request, f"關鍵字規則「{keyword}」已建立。")
        return redirect(f"{reverse('feedback:text-analysis')}?survey={slug}#text-rules")


class KeywordCategoryUpdateView(ManagerRequiredMixin, View):
    def post(self, request, pk):
        kc = get_object_or_404(KeywordCategory, pk=pk)
        slug = kc.survey.slug
        keyword = request.POST.get("keyword", "").strip()
        category = request.POST.get("category", "").strip()
        threshold = request.POST.get("threshold", "2").strip()
        redirect_url = f"{reverse('feedback:text-analysis')}?survey={slug}#text-rules"
        if not keyword or not category:
            messages.error(request, "關鍵字與分類名稱不能空白。")
            return redirect(redirect_url)
        try:
            threshold = int(threshold)
            if threshold < 1:
                raise ValueError
        except ValueError:
            messages.error(request, "門檻值須為正整數。")
            return redirect(redirect_url)
        if KeywordCategory.objects.filter(survey=kc.survey, keyword=keyword).exclude(pk=kc.pk).exists():
            messages.error(request, f"關鍵字「{keyword}」已有分類規則。")
            return redirect(redirect_url)
        kc.keyword = keyword
        kc.category = category
        kc.threshold = threshold
        kc.save(update_fields=["keyword", "category", "threshold"])
        messages.success(request, f"關鍵字規則「{keyword}」已更新。")
        return redirect(redirect_url)


class KeywordCategoryDeleteView(ManagerRequiredMixin, View):
    def post(self, request, pk):
        kc = get_object_or_404(KeywordCategory, pk=pk)
        slug = kc.survey.slug
        kc.delete()
        messages.success(request, "關鍵字規則已刪除。")
        return redirect(f"{reverse('feedback:text-analysis')}?survey={slug}#text-rules")


class TextAnalysisView(DashboardBaseMixin, TemplateView):
    template_name = "feedback/text_analysis.html"
    active_section = "feedback:text-analysis"

    def get_context_data(self, **kwargs):
        selected_slug = self.request.GET.get("survey")
        context = super().get_context_data(**kwargs)
        sort = self.request.GET.get("sort", "newest")
        category_id = self.request.GET.get("category", "")
        survey = Survey.objects.filter(slug=selected_slug).first() if selected_slug else None
        context.update(self.get_dashboard_base_context())
        context["selected_survey"] = survey
        text_surveys = analysis_visible_surveys().select_related("category")
        if category_id:
            text_surveys = text_surveys.filter(category_id=category_id)
        if sort == "oldest":
            text_surveys = text_surveys.order_by("created_at")
        elif sort == "title":
            text_surveys = text_surveys.order_by("title")
        else:
            text_surveys = text_surveys.order_by("-created_at")
        context["text_survey_rows"] = _survey_catalog_rows(text_surveys)
        context["categories"] = SurveyCategory.objects.all()
        context["current_sort"] = sort
        context["current_category"] = category_id
        publication = get_published_analysis_payload(survey) if survey else None
        text_analysis_payload = publication["text_analysis"] if publication else {}
        context["analysis_publication"] = publication
        context["analysis_stage_fresh"] = publication["freshness"]["text"] if publication else None
        context["keywords"] = text_analysis_payload.get("keywords", []) if survey else []
        context["analysis_summary"] = text_analysis_payload.get("summary", {}) if survey else {}
        context["category_sentiments"] = text_analysis_payload.get("category_sentiments", []) if survey else []
        context["text_questions"] = survey.questions.filter(data_type=Question.DataType.TEXT) if survey else []
        context["keyword_categories"] = (
            KeywordCategory.objects.filter(survey=survey).order_by("category", "keyword")
            if survey else []
        )
        return context

class ImprovementListView(DashboardBaseMixin, TemplateView):
    template_name = "feedback/improvement_list.html"
    active_section = "feedback:improvement-list"

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action")
        if action == "toggle-tracking":
            from cloudapi.definition import serialize_definition, update_survey
            from cloudapi.errors import DefinitionCommitError
            from feedback.survey_lifecycle import commit

            survey = get_object_or_404(Survey, id=request.POST.get("survey_id"))
            definition = serialize_definition(survey)
            update_survey(definition, {"improvement_tracking_enabled": request.POST.get("enabled") == "on"})
            try:
                commit(survey, definition, survey.definition_version)
            except DefinitionCommitError as exc:
                messages.error(request, exc.user_message)
                return redirect(f"{reverse('feedback:improvement-list')}?survey={survey.slug}")
            survey.refresh_from_db()
            state = "啟用" if survey.improvement_tracking_enabled else "停用"
            messages.success(request, f"「{survey.title}」改善追蹤已{state}。")
            return redirect(f"{reverse('feedback:improvement-list')}?survey={survey.slug}")
        return redirect("feedback:improvement-list")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.get_dashboard_base_context())
        selected_slug = self.request.GET.get("survey")
        sort = self.request.GET.get("sort", "newest")
        category_id = self.request.GET.get("category", "")
        selected_survey = Survey.objects.filter(slug=selected_slug).first() if selected_slug else None

        improvement_surveys = (
            Survey.objects.filter(is_active=True)
            .select_related("category")
            .annotate(
                improvement_count=Count("improvements", distinct=True),
                response_count=Count("submissions", distinct=True),
                latest_submission_at=Max("submissions__submitted_at"),
            )
        )
        if category_id:
            improvement_surveys = improvement_surveys.filter(category_id=category_id)
        if sort == "oldest":
            improvement_surveys = improvement_surveys.order_by("created_at")
        elif sort == "title":
            improvement_surveys = improvement_surveys.order_by("title")
        else:
            improvement_surveys = improvement_surveys.order_by("-created_at")

        context["selected_survey"] = selected_survey
        context["improvement_survey_rows"] = improvement_surveys
        context["selected_improvements"] = (
            selected_survey.improvements.order_by("-created_at") if selected_survey else []
        )
        context["create_url"] = (
            reverse("feedback:improvement-create", args=[selected_survey.slug]) if selected_survey else ""
        )
        context["categories"] = SurveyCategory.objects.all()
        context["current_sort"] = sort
        context["current_category"] = category_id
        return context


class NoticeCenterView(DashboardBaseMixin, TemplateView):
    template_name = "feedback/notice_center.html"
    active_section = "feedback:notice-center"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.get_dashboard_base_context())
        selected_slug = self.request.GET.get("survey")
        sort = self.request.GET.get("sort", "newest")
        category_id = self.request.GET.get("category", "")
        selected_survey = Survey.objects.filter(slug=selected_slug).first() if selected_slug else None

        notice_surveys = (
            Survey.objects.filter(is_active=True)
            .select_related("category")
            .annotate(
                notice_count=(
                    Count("improvements__notices", distinct=True)
                    + Count(
                        "improvements__dispatches",
                        filter=Q(improvements__dispatches__notice__isnull=True),
                        distinct=True,
                    )
                ),
                response_count=Count("submissions", distinct=True),
                latest_submission_at=Max("submissions__submitted_at"),
            )
        )
        if category_id:
            notice_surveys = notice_surveys.filter(category_id=category_id)
        if sort == "oldest":
            notice_surveys = notice_surveys.order_by("created_at")
        elif sort == "title":
            notice_surveys = notice_surveys.order_by("title")
        else:
            notice_surveys = notice_surveys.order_by("-created_at")

        notices = (
            ImprovementNotice.objects.filter(improvement__survey=selected_survey)
            .select_related("improvement")
            .order_by("-created_at")
            if selected_survey else ImprovementNotice.objects.none()
        )
        legacy_notices = (
            selected_survey.improvements.filter(
                dispatches__isnull=False,
                dispatches__notice__isnull=True,
            ).distinct().order_by("-created_at")
            if selected_survey else ImprovementUpdate.objects.none()
        )
        context["selected_survey"] = selected_survey
        context["notice_survey_rows"] = notice_surveys
        context["notices"] = notices
        context["legacy_notices"] = legacy_notices
        context["categories"] = SurveyCategory.objects.all()
        context["current_sort"] = sort
        context["current_category"] = category_id
        return context


class NoticeDetailView(DashboardBaseMixin, DetailView):
    template_name = "feedback/notice_detail.html"
    model = ImprovementUpdate
    context_object_name = "improvement"
    active_section = "feedback:notice-center"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.get_dashboard_base_context())
        context["dispatches"] = (
            self.object.dispatches
            .select_related("submission__user", "submission__survey")
            .order_by("-sent_at")
        )
        return context


class SurveyDetailView(DetailView):
    template_name = "feedback/survey_detail.html"
    context_object_name = "survey"
    model = Survey
    slug_field = "slug"
    slug_url_kwarg = "slug"

    def _is_preview(self, request):
        return request.GET.get("preview") == "1" and getattr(request.user, "is_manager", False)

    def _notice(self, message, kind):
        return self.render_to_response(self.get_context_data(survey_notice=message, survey_notice_type=kind))

    def dispatch(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not request.user.is_authenticated:
            messages.warning(request, "這份問卷需要先登入後才能填答。")
            return redirect(f"{reverse('accounts:login')}?next={request.path}")
        if request.GET.get("preview") == "1" and request.method != "GET":
            return HttpResponseForbidden("預覽模式不能送出")
        if self._is_preview(request):
            response = super().dispatch(request, *args, **kwargs)
            # Only the manager preview may be framed, by the builder on this site (builder spec §3).
            response["X-Frame-Options"] = "SAMEORIGIN"
            return response
        if request.method == "GET":
            # Display limits only; a POST is judged by the service under the survey lock (spec §7.1).
            if not self.object.is_published:
                return self._notice("問卷尚未開放", "error")
            if not self.object.accepts_responses:
                return self._notice("這份問卷目前未開放填答。", "error")
            if not self.object.questions.filter(is_active=True).exists():
                return self._notice("這份問卷目前沒有任何題目。", "warning")
            if not request.user.is_manager:
                from cloudapi.receipts import has_submitted

                if has_submitted(self.object, request.user):
                    return self._notice("你已填答過這份問卷。", "info")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        initial = {}
        if self.request.user.is_authenticated:
            initial = {
                "consent_follow_up": getattr(self.request.user, "notification_opt_in", False),
            }
        context["respondent_form"] = kwargs.get("respondent_form") or RespondentMetaForm(prefix="meta", initial=initial)
        context["form"] = kwargs.get("form") or SurveyFormBuilder(survey=self.object)
        context["active_question_count"] = self.object.questions.filter(is_active=True).count()
        from cloudapi.inbox import uses_inbox

        context["uses_inbox"] = uses_inbox(self.object)
        context["form_version"] = self.object.published_version
        context["preview"] = self._is_preview(self.request)
        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        respondent_form = RespondentMetaForm(request.POST, prefix="meta")
        form = SurveyFormBuilder(request.POST, survey=self.object)
        if not (form.is_valid() and respondent_form.is_valid()):
            context = self.get_context_data(object=self.object, form=form, respondent_form=respondent_form)
            return self.render_to_response(context)
        consent_follow_up = respondent_form.cleaned_data["consent_follow_up"]

        from cloudapi.inbox import SurveyClosed, uses_inbox

        if settings.CLOUD_INBOX_ENABLED and self.object.owner_node_id and not uses_inbox(self.object):
            # A node survey outside the self-test scope: never fall back to a cloud write the node cannot see.
            return self._notice(SurveyClosed.user_message, "error")
        if uses_inbox(self.object):
            if not request.user.is_manager:
                request.user.notification_opt_in = consent_follow_up
                request.user.save(update_fields=["notification_opt_in"])
            return self._submit_to_inbox(request, form, respondent_form, consent_follow_up)

        try:
            form_version = int(request.POST.get("definition_version", ""))
        except ValueError:
            form_version = -1  # a form without a version can never match the published one
        try:
            submission_result = local_service.submit_survey_payload(
                self.object,
                user=request.user,
                respondent_name=request.user.get_full_name(),
                respondent_email=request.user.email,
                consent_follow_up=consent_follow_up,
                answers={key: value for key, value in form.cleaned_data.items()},
                idempotency_key=respondent_form.cleaned_data["idempotency_key"],
                form_version=form_version,
                reject_repeat=not request.user.is_manager,
            )
        except local_service.SurveyFormOutdated as exc:
            messages.error(request, exc.user_message)
            context = self.get_context_data(object=self.object, form=form, respondent_form=respondent_form)
            return self.render_to_response(context)
        except (local_service.SurveyNotAccepting, local_service.SurveyAlreadySubmitted) as exc:
            return self._notice(exc.user_message, "error")
        except ValueError:
            messages.error(request, "這份回覆無法重複送出，請重新填寫")
            context = self.get_context_data(object=self.object, form=form, respondent_form=respondent_form)
            return self.render_to_response(context)

        if not request.user.is_manager and not submission_result["reused"]:
            request.user.notification_opt_in = consent_follow_up
            request.user.save(update_fields=["notification_opt_in"])
        if (
            not submission_result["reused"]
            and submission_result["thank_you_email_enabled"]
            and submission_result["respondent_email"]
        ):
            send_mail(
                subject=f"感謝填寫 {submission_result['survey_title']}",
                message="我們已收到你的回覆。若後續有對應的改善通知，將依你的偏好主動提供最新進度。",
                from_email=None,
                recipient_list=[submission_result["respondent_email"]],
                fail_silently=True,
            )
        return HttpResponseRedirect(reverse("feedback:survey-success", args=[self.object.slug]))

    def _submit_to_inbox(self, request, form, respondent_form, consent_follow_up):
        from cloudapi.envelope import encode_answers
        from cloudapi.inbox import DefinitionOutdated, InboxRejected, accept_submission

        try:
            try:
                form_version = int(request.POST.get("definition_version", ""))
            except ValueError as exc:
                raise DefinitionOutdated() from exc
            result = accept_submission(
                self.object,
                user=request.user,
                submission_uuid=respondent_form.cleaned_data["idempotency_key"] or uuid.uuid4(),
                form_version=form_version,
                consent_follow_up=consent_follow_up,
                answers=encode_answers(self.object, form.cleaned_data),
            )
        except InboxRejected as exc:
            messages.error(request, exc.user_message)
            self.object.refresh_from_db()
            context = self.get_context_data(object=self.object, form=form, respondent_form=respondent_form)
            return self.render_to_response(context)
        if not result.reused and self.object.thank_you_email_enabled and request.user.email:
            send_mail(
                subject=f"感謝填寫 {self.object.title}",
                message="我們已收到你的回覆。若後續有對應的改善通知，將依你的偏好主動提供最新進度。",
                from_email=None,
                recipient_list=[request.user.email],
                fail_silently=True,
            )
        return HttpResponseRedirect(reverse("feedback:survey-success", args=[self.object.slug]))


class SurveySubmitSuccessView(TemplateView):
    template_name = "feedback/survey_success.html"


class ImprovementCreateView(DashboardBaseMixin, CreateView):
    template_name = "feedback/improvement_form.html"
    form_class = ImprovementUpdateForm
    active_section = "feedback:improvement-list"

    def dispatch(self, request, *args, **kwargs):
        self.survey = get_object_or_404(Survey, slug=kwargs["slug"])
        if request.method == "POST" and not self.survey.improvement_tracking_enabled:
            messages.warning(request, "這份問卷的改善追蹤目前已停用。")
            return redirect(f"{reverse('feedback:improvement-list')}?survey={self.survey.slug}")
        return super().dispatch(request, *args, **kwargs)

    def get_initial(self):
        initial = super().get_initial()
        category = self.request.GET.get("category", "")
        keyword = self.request.GET.get("keyword", "")
        if category:
            initial["related_category"] = category
        if keyword:
            initial["title"] = f"改善「{keyword}」相關問題"
            initial["summary"] = f"根據顧客回饋中「{keyword}」關鍵字的高頻出現，針對此方向進行改善。"
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.get_dashboard_base_context())
        context["survey"] = self.survey
        context["source_keyword"] = self.request.GET.get("keyword", "")
        context["source_category"] = self.request.GET.get("category", "")
        return context

    def form_valid(self, form):
        form.instance.survey = self.survey
        form.instance.created_by = self.request.user
        form.instance.updated_by = self.request.user
        response = super().form_valid(form)
        record_initial_status(form.instance, self.request.user)
        messages.success(self.request, "改善項目已建立；通知需從項目詳細頁另外建立與確認。")
        return response

    def get_success_url(self):
        return f"{reverse('feedback:improvement-list')}?survey={self.survey.slug}"


class ImprovementDetailView(DashboardBaseMixin, DetailView):
    template_name = "feedback/improvement_detail.html"
    model = ImprovementUpdate
    context_object_name = "improvement"
    active_section = "feedback:improvement-list"

    def get_queryset(self):
        return super().get_queryset().select_related(
            "survey",
            "created_by",
            "updated_by",
            "source_ai_analysis_stage__snapshot",
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.get_dashboard_base_context())
        stage = self.object.source_ai_analysis_stage
        refs = self.object.source_evidence_refs or []
        registry = (stage.output_json or {}).get("_evidence_registry", {}) if stage else {}
        context["source_ai_evidence"] = [
            serialize_evidence_for_display(registry[ref], stage.snapshot.source_snapshot)
            for ref in refs
            if ref in registry
        ]
        context["status_targets"] = status_targets(self.object)
        context["status_history"] = self.object.status_history.select_related("changed_by")
        return context


class ImprovementUpdateView(DashboardBaseMixin, UpdateView):
    template_name = "feedback/improvement_edit.html"
    model = ImprovementUpdate
    form_class = ImprovementEditForm
    context_object_name = "improvement"
    active_section = "feedback:improvement-list"

    def dispatch(self, request, *args, **kwargs):
        self.object = self.get_object()
        if self.object.status == ImprovementUpdate.Status.ARCHIVED:
            messages.warning(request, "封存項目需先恢復才能編輯。")
            return redirect("feedback:improvement-detail", pk=self.object.pk)
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        with transaction.atomic():
            locked = ImprovementUpdate.objects.select_for_update().get(pk=self.object.pk)
            if locked.status == ImprovementUpdate.Status.ARCHIVED:
                messages.warning(self.request, "項目已被封存，本次內容未更新。")
                return redirect("feedback:improvement-detail", pk=locked.pk)
            for field_name in ImprovementEditForm.Meta.fields:
                setattr(locked, field_name, form.cleaned_data[field_name])
            locked.updated_by = self.request.user
            locked.save(update_fields=[*ImprovementEditForm.Meta.fields, "updated_by", "updated_at"])
            self.object = locked
        messages.success(self.request, "改善項目已更新；未建立或寄送任何通知。")
        return HttpResponseRedirect(self.get_success_url())

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.get_dashboard_base_context())
        return context

    def get_success_url(self):
        return reverse("feedback:improvement-detail", args=[self.object.pk])


class ImprovementStatusTransitionView(ManagerRequiredMixin, View):
    def post(self, request, pk):
        improvement = get_object_or_404(ImprovementUpdate, pk=pk)
        form = ImprovementStatusTransitionForm(
            request.POST,
            improvement=improvement,
            choices=status_targets(improvement),
        )
        if not form.is_valid():
            messages.error(request, "請選擇可用的下一狀態。")
            return redirect("feedback:improvement-detail", pk=pk)
        try:
            updated = transition_improvement(pk, form.cleaned_data["status"], request.user)
        except ImprovementTransitionError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"狀態已更新為「{updated.get_status_display()}」。")
        return redirect("feedback:improvement-detail", pk=pk)


class ImprovementNoticeCreateView(DashboardBaseMixin, CreateView):
    template_name = "feedback/improvement_notice_form.html"
    model = ImprovementNotice
    form_class = ImprovementNoticeForm
    active_section = "feedback:notice-center"

    def dispatch(self, request, *args, **kwargs):
        self.improvement = get_object_or_404(
            ImprovementUpdate.objects.select_related("survey"),
            pk=kwargs["pk"],
        )
        if self.improvement.status == ImprovementUpdate.Status.ARCHIVED:
            messages.warning(request, "封存項目無法建立新通知；請先恢復項目。")
            return redirect("feedback:improvement-detail", pk=self.improvement.pk)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["improvement"] = self.improvement
        return kwargs

    def get_initial(self):
        initial = super().get_initial()
        initial.update(
            {
                "subject": f"{self.improvement.title}｜改善進度通知",
                "body": self.improvement.summary,
                "audience_type": ImprovementNotice.AudienceType.SURVEY_RESPONDENTS,
            }
        )
        if self.improvement.survey_id is None:
            initial["audience_type"] = ImprovementNotice.AudienceType.GLOBAL
        return initial

    def form_valid(self, form):
        with transaction.atomic():
            improvement = ImprovementUpdate.objects.select_for_update().get(pk=self.improvement.pk)
            if improvement.status == ImprovementUpdate.Status.ARCHIVED:
                messages.warning(self.request, "項目已被封存，未建立通知草稿。")
                return redirect("feedback:improvement-detail", pk=improvement.pk)
            form.instance.improvement = improvement
            form.instance.created_by = self.request.user
            self.object = form.save()
        messages.success(self.request, "通知草稿已保存，尚未寄送。請先預覽收件範圍。")
        return HttpResponseRedirect(self.get_success_url())

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.get_dashboard_base_context())
        context["improvement"] = self.improvement
        context["form_mode"] = "create"
        return context

    def get_success_url(self):
        return reverse("feedback:notice-batch-preview", args=[self.object.pk])


class ImprovementNoticeUpdateView(DashboardBaseMixin, UpdateView):
    template_name = "feedback/improvement_notice_form.html"
    model = ImprovementNotice
    form_class = ImprovementNoticeForm
    context_object_name = "notice"
    active_section = "feedback:notice-center"

    def get_queryset(self):
        return super().get_queryset().select_related("improvement", "improvement__survey")

    def dispatch(self, request, *args, **kwargs):
        self.object = self.get_object()
        if self.object.status != ImprovementNotice.Status.DRAFT:
            messages.warning(request, "已確認寄送的通知內容不可修改。")
            return redirect("feedback:notice-batch-detail", pk=self.object.pk)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["improvement"] = self.object.improvement
        return kwargs

    def form_valid(self, form):
        with transaction.atomic():
            locked = ImprovementNotice.objects.select_for_update().get(pk=self.object.pk)
            if locked.status != ImprovementNotice.Status.DRAFT:
                messages.warning(self.request, "通知已進入寄送流程，內容未被修改。")
                return redirect("feedback:notice-batch-detail", pk=locked.pk)
            locked.subject = form.cleaned_data["subject"]
            locked.body = form.cleaned_data["body"]
            locked.audience_type = form.cleaned_data["audience_type"]
            locked.content_version += 1
            locked.confirmation_token = uuid.uuid4()
            locked.save(
                update_fields=[
                    "subject",
                    "body",
                    "audience_type",
                    "content_version",
                    "confirmation_token",
                    "updated_at",
                ]
            )
            self.object = locked
        messages.success(self.request, "通知草稿已更新；舊預覽確認資料已失效。")
        return HttpResponseRedirect(self.get_success_url())

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.get_dashboard_base_context())
        context["improvement"] = self.object.improvement
        context["form_mode"] = "update"
        return context

    def get_success_url(self):
        return reverse("feedback:notice-batch-preview", args=[self.object.pk])


class ImprovementNoticePreviewView(DashboardBaseMixin, DetailView):
    template_name = "feedback/improvement_notice_preview.html"
    model = ImprovementNotice
    context_object_name = "notice"
    active_section = "feedback:notice-center"

    def get_queryset(self):
        return super().get_queryset().select_related("improvement", "improvement__survey")

    def dispatch(self, request, *args, **kwargs):
        self.object = self.get_object()
        if self.object.status != ImprovementNotice.Status.DRAFT:
            return redirect("feedback:notice-batch-detail", pk=self.object.pk)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.get_dashboard_base_context())
        recipients = resolve_notice_recipients(self.object)
        context["recipient_count"] = len(recipients)
        context["confirmation_form"] = ImprovementNoticeConfirmationForm(
            initial={
                "confirmation_token": self.object.confirmation_token,
                "content_version": self.object.content_version,
            }
        )
        return context


class ImprovementNoticeDetailView(DashboardBaseMixin, DetailView):
    template_name = "feedback/improvement_notice_detail.html"
    model = ImprovementNotice
    context_object_name = "notice"
    active_section = "feedback:notice-center"

    def get_queryset(self):
        return super().get_queryset().select_related(
            "improvement",
            "improvement__survey",
            "created_by",
            "confirmed_by",
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.get_dashboard_base_context())
        context["dispatches"] = self.object.dispatches.select_related(
            "recipient_user",
            "submission",
        ).order_by("id")
        return context


class ImprovementNoticeSendView(ManagerRequiredMixin, View):
    def post(self, request, pk):
        notice = get_object_or_404(ImprovementNotice, pk=pk)
        form = ImprovementNoticeConfirmationForm(request.POST)
        if not form.is_valid():
            messages.error(request, "確認資料無效，請重新預覽通知。")
            return redirect("feedback:notice-batch-preview", pk=pk)
        try:
            notice, prepared = prepare_notice_dispatches(
                pk,
                confirmation_token=form.cleaned_data["confirmation_token"],
                content_version=form.cleaned_data["content_version"],
                actor=request.user,
            )
        except NoticeConfirmationError as exc:
            messages.error(request, str(exc))
            return redirect("feedback:notice-batch-preview", pk=pk)
        if not prepared:
            messages.info(request, "這份通知已確認，不會重複寄送。")
            return redirect("feedback:notice-batch-detail", pk=pk)

        notice = send_notice_batch(notice.pk)
        if notice.status == ImprovementNotice.Status.SENT:
            messages.success(request, f"通知已寄送給 {notice.sent_count} 位收件者。")
        elif notice.status == ImprovementNotice.Status.PARTIALLY_SENT:
            messages.warning(
                request,
                f"通知部分完成：成功 {notice.sent_count} 人，失敗 {notice.failed_count} 人。",
            )
        else:
            messages.error(request, "通知未能寄出，可在批次詳細頁手動重試失敗項目。")
        return redirect("feedback:notice-batch-detail", pk=pk)


class ImprovementNoticeRetryView(ManagerRequiredMixin, View):
    def post(self, request, pk):
        get_object_or_404(ImprovementNotice, pk=pk)
        notice, started = begin_notice_retry(pk)
        if not started:
            messages.info(request, "目前沒有可重試的失敗寄送。")
            return redirect("feedback:notice-batch-detail", pk=pk)
        notice = send_notice_batch(notice.pk, retry_failed=True)
        if notice.status == ImprovementNotice.Status.SENT:
            messages.success(request, "失敗項目重試完成，通知已全部寄送。")
        elif notice.status == ImprovementNotice.Status.PARTIALLY_SENT:
            messages.warning(request, "部分項目重試後仍失敗，可再次手動重試。")
        else:
            messages.error(request, "重試仍未成功，請檢查郵件服務設定後再試。")
        return redirect("feedback:notice-batch-detail", pk=pk)


class AIImprovementDraftCreateView(ImprovementCreateView):
    def _load_ai_draft(self):
        self.snapshot = get_object_or_404(
            SurveyAIReportSnapshot,
            pk=self.kwargs["snapshot_id"],
            survey=self.survey,
            status=SurveyAIReportSnapshot.Status.SUCCEEDED,
        )
        drafts = (self.snapshot.ai_report or {}).get("improvement_drafts", [])
        self.ai_draft = next(
            (draft for draft in drafts if draft.get("draft_id") == self.kwargs["draft_id"]),
            None,
        )
        if self.ai_draft is None:
            raise Http404("找不到指定的 AI 改善草稿。")

    def get(self, request, *args, **kwargs):
        self._load_ai_draft()
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self._load_ai_draft()
        return super().post(request, *args, **kwargs)

    def get_initial(self):
        return {
            "title": self.ai_draft["title"],
            "summary": self.ai_draft["summary"],
            "related_category": self.ai_draft["related_category"],
        }

    def form_valid(self, form):
        priority = self.ai_draft.get("priority")
        if priority in ImprovementUpdate.Priority.values:
            form.instance.priority = priority
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        serialized_draft = serialize_ai_report_content(
            {"improvement_drafts": [self.ai_draft]},
            self.snapshot.source_snapshot,
        )["improvement_drafts"][0]
        context.update(
            {
                "survey": self.survey,
                "source_ai_draft": serialized_draft,
                "source_ai_priority_label": {
                    "high": "高優先",
                    "medium": "中優先",
                    "low": "低優先",
                }.get(self.ai_draft.get("priority"), self.ai_draft.get("priority")),
                "source_keyword": "",
                "source_category": self.ai_draft["related_category"],
            }
        )
        return context


class AIStageImprovementDraftCreateView(ImprovementCreateView):
    unavailable_message = "這份 AI 改善草稿已過期或無法使用，請重新產生綜合分析。"

    def _load_stage_draft(self, *, lock=False):
        queryset = SurveyAIAnalysisStage.objects.select_related("snapshot__survey")
        if lock:
            queryset = queryset.select_for_update()
        self.source_stage = get_object_or_404(
            queryset,
            pk=self.kwargs["stage_id"],
            snapshot__survey=self.survey,
            stage_type=SurveyAIAnalysisStage.StageType.SYNTHESIS,
            status=SurveyAIAnalysisStage.Status.SUCCEEDED,
        )
        if not is_published_ai_stage_current(self.source_stage):
            return False
        drafts = (self.source_stage.output_json or {}).get("improvement_drafts", [])
        draft_id = str(self.kwargs["draft_id"])
        self.ai_draft = next((draft for draft in drafts if draft.get("draft_id") == draft_id), None)
        if self.ai_draft is None:
            raise Http404("找不到指定的 AI 改善草稿。")
        registry = (self.source_stage.output_json or {}).get("_evidence_registry", {})
        refs = self.ai_draft.get("evidence_refs")
        if (
            not isinstance(refs, list)
            or not refs
            or len(refs) != len(set(refs))
            or any(ref not in registry for ref in refs)
        ):
            return False
        self.ai_evidence = [
            serialize_evidence_for_display(registry[ref], self.source_stage.snapshot.source_snapshot)
            for ref in refs
        ]
        self.existing_improvement = ImprovementUpdate.objects.filter(
            source_ai_analysis_stage=self.source_stage,
            source_ai_draft_id=draft_id,
        ).first()
        return True

    def _existing_url(self, improvement):
        return f"{reverse('feedback:improvement-list')}?survey={self.survey.slug}#improvement-{improvement.pk}"

    def get(self, request, *args, **kwargs):
        if not self._load_stage_draft():
            return HttpResponse(self.unavailable_message, status=409)
        if self.existing_improvement:
            messages.info(request, "這份 AI 草稿已加入改善追蹤。")
            return redirect(self._existing_url(self.existing_improvement))
        return CreateView.get(self, request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        if not self._load_stage_draft():
            return HttpResponse(self.unavailable_message, status=409)
        if self.existing_improvement:
            messages.info(request, "這份 AI 草稿已加入改善追蹤。")
            return redirect(self._existing_url(self.existing_improvement))
        return CreateView.post(self, request, *args, **kwargs)

    def get_initial(self):
        acceptance = self.ai_draft.get("acceptance_criteria") or []
        limitations = self.ai_draft.get("data_limitations") or []
        summary_parts = [
            f"問題與目標：\n{self.ai_draft['title']}",
            f"建議行動：\n{self.ai_draft['summary']}",
            f"分析依據：\n{self.ai_draft['rationale']}",
        ]
        if acceptance:
            summary_parts.append("驗收方向：\n" + "\n".join(f"- {item}" for item in acceptance))
        if limitations:
            summary_parts.append("資料限制：\n" + "\n".join(f"- {item}" for item in limitations))
        return {
            "title": self.ai_draft["title"],
            "summary": "\n\n".join(summary_parts),
            "related_category": self.ai_draft["related_category"],
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        upstream_ids = self.source_stage.input_manifest.get("upstream_stage_ids", {})
        upstream = {
            row.stage_type: row
            for row in SurveyAIAnalysisStage.objects.filter(pk__in=upstream_ids.values())
        }
        context.update(
            {
                "survey": self.survey,
                "source_ai_draft": self.ai_draft,
                "source_ai_evidence": self.ai_evidence,
                "source_ai_stage": self.source_stage,
                "source_ai_priority_label": {
                    "high": "高優先",
                    "medium": "中優先",
                    "low": "低優先",
                }.get(self.ai_draft.get("priority"), self.ai_draft.get("priority")),
                "source_statistics_stage": upstream.get(SurveyAIAnalysisStage.StageType.STATISTICS),
                "source_text_stage": upstream.get(SurveyAIAnalysisStage.StageType.TEXT),
                "source_keyword": "",
                "source_category": self.ai_draft["related_category"],
            }
        )
        return context

    def form_valid(self, form):
        draft_id = str(self.kwargs["draft_id"])
        existing = None
        try:
            with transaction.atomic():
                if not self._load_stage_draft(lock=True):
                    return HttpResponse(self.unavailable_message, status=409)
                existing = ImprovementUpdate.objects.filter(
                    source_ai_analysis_stage=self.source_stage,
                    source_ai_draft_id=draft_id,
                ).first()
                if existing is None:
                    form.instance.survey = self.survey
                    form.instance.created_by = self.request.user
                    form.instance.updated_by = self.request.user
                    form.instance.priority = self.ai_draft["priority"]
                    form.instance.source_ai_analysis_stage = self.source_stage
                    form.instance.source_ai_draft_id = draft_id
                    form.instance.source_evidence_refs = list(self.ai_draft["evidence_refs"])
                    form.instance.source_ai_metadata = {
                        "priority": self.ai_draft["priority"],
                        "rationale": self.ai_draft["rationale"],
                        "acceptance_criteria": list(self.ai_draft.get("acceptance_criteria") or []),
                        "schema_version": self.source_stage.schema_version,
                        "prompt_version": self.source_stage.prompt_version,
                    }
                    self.object = form.save()
                    record_initial_status(self.object, self.request.user)
        except IntegrityError:
            existing = ImprovementUpdate.objects.filter(
                source_ai_analysis_stage_id=self.kwargs["stage_id"],
                source_ai_draft_id=draft_id,
            ).first()
            if existing is None:
                raise
        if existing:
            messages.info(self.request, "這份 AI 草稿已加入改善追蹤。")
            return redirect(self._existing_url(existing))
        messages.success(self.request, "AI 改善草稿已加入追蹤；尚未建立或寄送通知。")
        return redirect(self._existing_url(self.object))
