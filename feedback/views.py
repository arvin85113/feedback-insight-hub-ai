import uuid
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q
from django.db.models.functions import TruncDate
import segno

from django.http import Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.text import slugify
from django.views.generic import CreateView, DeleteView, DetailView, TemplateView, UpdateView, View

from .ai_report_service import AIReportError, generate_report
from .ai_snapshot_service import (
    SnapshotError,
    build_or_reuse_snapshot,
    get_report_status,
    serialize_ai_report_content,
    serialize_evidence_for_display,
    serialize_report,
)
from .ai_stage_service import (
    StageError,
    generate_stage,
    get_pipeline_status,
    is_stage_current,
    latest_stage_status,
)
from .forms import (
    ImprovementEditForm,
    ImprovementNoticeConfirmationForm,
    ImprovementNoticeForm,
    ImprovementStatusTransitionForm,
    ImprovementUpdateForm,
    QuestionCreateForm,
    RespondentMetaForm,
    SurveyCreateForm,
    SurveyEditForm,
    SurveyFormBuilder,
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
    SurveyCategory,
)
from .notice_service import (
    NoticeConfirmationError,
    begin_notice_retry,
    prepare_notice_dispatches,
    resolve_notice_recipients,
    send_notice_batch,
)
from .service_client import service_client


class ManagerRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_manager


class CustomerRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_authenticated and not self.request.user.is_manager


class DashboardBaseMixin(ManagerRequiredMixin):
    dashboard_nav = [
        ("feedback:dashboard", "營運總覽", "grid"),
        ("feedback:survey-manager", "問卷管理", "clipboard"),
        ("feedback:stats-overview", "統計分析", "chart"),
        ("feedback:text-analysis", "文字洞察", "message"),
        ("feedback:improvement-list", "改善追蹤", "wrench"),
        ("feedback:notice-center", "通知中心", "send"),
    ]

    active_section = ""

    def get_dashboard_base_context(self):
        return {
            "dashboard_nav": self.dashboard_nav,
            "active_section": self.active_section,
            "survey_list": Survey.objects.filter(is_active=True).order_by("title"),
        }


class HomeView(TemplateView):
    template_name = "feedback/home.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(service_client.get_home())
        context.update(
            {
                "featured_capabilities": [
                    {
                        "title": "Survey Operations",
                        "description": "以登入制問卷集中管理題型、資料型態與填答流程，讓回饋收集維持一致脈絡。",
                    },
                    {
                        "title": "Statistical Insight",
                        "description": "依照資料型態整理描述統計與可執行推論，並清楚標示條件不通過的原因。",
                    },
                    {
                        "title": "Closed-loop Follow-up",
                        "description": "把文字洞察與改善追蹤串接起來，讓管理者能把回饋轉成後續通知與行動。",
                    },
                ],
                "homepage_steps": [
                    "建立登入制問卷與題型資料標籤",
                    "收集回覆並保留填答者追蹤偏好",
                    "從統計與文字分析提取決策線索",
                    "發布改善更新並回推給相關顧客",
                ],
            }
        )
        return context


class CustomerHomeView(CustomerRequiredMixin, TemplateView):
    template_name = "feedback/customer_home.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        payload = service_client.get_customer_home(self.request.user)
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
        context.update(service_client.get_customer_notifications(self.request.user))
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
    template_name = "feedback/dashboard.html"
    active_section = "feedback:dashboard"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.get_dashboard_base_context())
        context.update(service_client.get_dashboard())
        context["ai_report_surveys"] = (
            Survey.objects.filter(is_active=True)
            .annotate(
                response_count=Count("submissions", distinct=True),
                valid_response_count=Count(
                    "submissions",
                    filter=Q(submissions__answers__value__gt=""),
                    distinct=True,
                ),
            )
            .order_by("title")
        )
        return context


class AIReportStatusView(ManagerRequiredMixin, View):
    def get(self, request, slug):
        survey = get_object_or_404(Survey, slug=slug, is_active=True)
        return JsonResponse({"ok": True, **get_report_status(survey)})


class AIStagePipelineStatusView(ManagerRequiredMixin, View):
    def get(self, request, slug):
        survey = get_object_or_404(Survey, slug=slug, is_active=True)
        return JsonResponse({"ok": True, **get_pipeline_status(survey)})


class AIReportSnapshotView(ManagerRequiredMixin, View):
    def post(self, request, slug):
        survey = get_object_or_404(Survey, slug=slug, is_active=True)
        try:
            result = build_or_reuse_snapshot(survey)
        except SnapshotError as exc:
            return JsonResponse(
                {"ok": False, "error_code": exc.error_code, "message": exc.user_message},
                status=exc.status_code,
            )
        snapshot = result.snapshot
        payload = {
            "ok": True,
            "snapshot": {
                "id": snapshot.pk,
                "survey_slug": survey.slug,
                "status": snapshot.status,
                "source_latest_at": snapshot.source_latest_at.isoformat() if snapshot.source_latest_at else None,
                "response_count": snapshot.response_count,
                "analysis_coverage": float(snapshot.analysis_coverage),
                "fingerprint_ms": snapshot.fingerprint_ms,
                "snapshot_ms": snapshot.snapshot_ms,
                "cache_hit": result.cache_hit,
                "generate_url": reverse(
                    "feedback:ai-report-generate",
                    args=[survey.slug, snapshot.pk],
                ),
                "stage_urls": {
                    stage_type: reverse(
                        "feedback:ai-stage-generate",
                        args=[survey.slug, snapshot.pk, stage_type],
                    )
                    for stage_type in (
                        SurveyAIAnalysisStage.StageType.STATISTICS,
                        SurveyAIAnalysisStage.StageType.TEXT,
                        SurveyAIAnalysisStage.StageType.SYNTHESIS,
                    )
                },
            },
        }
        if snapshot.status == SurveyAIReportSnapshot.Status.SUCCEEDED:
            payload["report"] = serialize_report(snapshot, is_current=True, cache_hit=True)
        return JsonResponse(payload)


class AIReportGenerateView(ManagerRequiredMixin, View):
    def post(self, request, slug, pk):
        survey = get_object_or_404(Survey, slug=slug, is_active=True)
        snapshot = get_object_or_404(
            SurveyAIReportSnapshot.objects.select_related("survey"),
            pk=pk,
            survey=survey,
        )
        try:
            generate_report(snapshot)
        except AIReportError as exc:
            return JsonResponse(
                {"ok": False, "error_code": exc.error_code, "message": exc.user_message},
                status=exc.status_code,
            )
        return JsonResponse({"ok": True, **get_report_status(survey)})


class AIStageGenerateView(ManagerRequiredMixin, View):
    def post(self, request, slug, pk, stage_type):
        survey = get_object_or_404(Survey, slug=slug, is_active=True)
        snapshot = get_object_or_404(
            SurveyAIReportSnapshot.objects.select_related("survey"),
            pk=pk,
            survey=survey,
        )
        allowed = {item.value for item in SurveyAIAnalysisStage.StageType}
        if stage_type not in allowed:
            raise Http404("不支援的 AI 分析階段。")
        if stage_type == SurveyAIAnalysisStage.StageType.TEXT:
            statistics = latest_stage_status(snapshot)[SurveyAIAnalysisStage.StageType.STATISTICS]
            if statistics["status"] != SurveyAIAnalysisStage.Status.SUCCEEDED or not statistics["is_current"]:
                return JsonResponse(
                    {"ok": False, "error_code": "upstream_incomplete", "message": "請先完成統計分析階段。"},
                    status=409,
                )
        try:
            stage = generate_stage(snapshot, stage_type)
        except StageError as exc:
            return JsonResponse(
                {"ok": False, "error_code": exc.error_code, "message": exc.user_message},
                status=exc.status_code,
            )
        return JsonResponse(
            {
                "ok": True,
                "completed_stage": {
                    "id": stage.pk,
                    "stage_type": stage.stage_type,
                    "status": stage.status,
                    "cache_hit": bool(stage.reused_from_id),
                },
                **get_pipeline_status(survey),
            }
        )


class SurveyManagerView(DashboardBaseMixin, TemplateView):
    template_name = "feedback/survey_manager.html"
    active_section = "feedback:survey-manager"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.get_dashboard_base_context())
        sort = self.request.GET.get("sort", "newest")
        category_id = self.request.GET.get("category", "")

        qs = (
            Survey.objects
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
        for survey in surveys_list:
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
        category = get_object_or_404(SurveyCategory, pk=pk)
        name = category.name
        category.delete()
        messages.success(request, f"分類「{name}」已刪除。")
        return redirect("feedback:survey-manager")


class SurveyCreateView(DashboardBaseMixin, CreateView):
    template_name = "feedback/survey_create.html"
    form_class = SurveyCreateForm
    active_section = "feedback:survey-manager"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.get_dashboard_base_context())
        return context

    def form_valid(self, form):
        form.instance.improvement_tracking_enabled = True
        self.object = form.save()
        base_slug = slugify(form.cleaned_data["title"]) or f"survey-{self.object.pk}"
        slug = base_slug
        counter = 2
        while Survey.objects.filter(slug=slug).exclude(pk=self.object.pk).exists():
            slug = f"{base_slug}-{counter}"
            counter += 1
        if self.object.slug != slug:
            self.object.slug = slug
            self.object.save(update_fields=["slug"])
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
        context["question_form"] = kwargs.get("question_form") or QuestionCreateForm(
            initial={"order": self.object.questions.count() + 1}
        )
        context["responses_count"] = self.object.submissions.count()
        context["survey_edit_form"] = kwargs.get("survey_edit_form") or SurveyEditForm(instance=self.object)
        context["latest_response"] = self.object.submissions.order_by("-submitted_at").first()
        context["active_tab"] = self.request.GET.get("tab", "questions")
        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        action = request.POST.get("action")

        if action == "move-question":
            question = get_object_or_404(Question, id=request.POST.get("question_id"), survey=self.object)
            direction = request.POST.get("direction")
            questions = list(self.object.questions.order_by("order", "id"))
            idx = next((i for i, q in enumerate(questions) if q.id == question.id), None)
            if idx is not None:
                if direction == "up" and idx > 0:
                    swap = questions[idx - 1]
                    question.order, swap.order = swap.order, question.order
                    question.save(update_fields=["order"])
                    swap.save(update_fields=["order"])
                elif direction == "down" and idx < len(questions) - 1:
                    swap = questions[idx + 1]
                    question.order, swap.order = swap.order, question.order
                    question.save(update_fields=["order"])
                    swap.save(update_fields=["order"])
            return redirect(reverse("feedback:survey-builder", args=[self.object.slug]) + "?tab=questions")

        if action == "delete-question":
            question = get_object_or_404(Question, id=request.POST.get("question_id"), survey=self.object)
            question.delete()
            messages.success(request, "題目已從問卷中移除。")
            return redirect("feedback:survey-builder", slug=self.object.slug)

        if action == "edit-question":
            question = get_object_or_404(Question, id=request.POST.get("question_id"), survey=self.object)
            question_form = QuestionCreateForm(request.POST, instance=question)
            if question_form.is_valid():
                question_form.save()
                messages.success(request, "題目已更新。")
                return redirect(reverse("feedback:survey-builder", args=[self.object.slug]) + "?tab=questions")
            context = self.get_context_data(question_form=question_form, object=self.object)
            return self.render_to_response(context)

        if action == "update-survey":
            survey_edit_form = SurveyEditForm(request.POST, instance=self.object)
            if survey_edit_form.is_valid():
                survey_edit_form.save()
                messages.success(request, "問卷設定已儲存。")
                return redirect(reverse("feedback:survey-builder", args=[self.object.slug]) + "?tab=settings")
            context = self.get_context_data(survey_edit_form=survey_edit_form, object=self.object)
            return self.render_to_response(context)

        question_form = QuestionCreateForm(request.POST)
        if question_form.is_valid():
            question = question_form.save(commit=False)
            question.survey = self.object
            question.save()
            messages.success(request, "新題目已加入問卷。")
            return redirect("feedback:survey-builder", slug=self.object.slug)

        context = self.get_context_data(question_form=question_form, object=self.object)
        return self.render_to_response(context)


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
        survey_title = self.get_object().title
        response = super().form_valid(form)
        messages.success(self.request, f"問卷「{survey_title}」已刪除。")
        return response


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
        stats_surveys = (
            Survey.objects.filter(is_active=True)
            .select_related("category")
            .annotate(
                question_count=Count("questions", distinct=True),
                response_count=Count("submissions", distinct=True),
                latest_submission_at=Max("submissions__submitted_at"),
            )
        )
        if category_id:
            stats_surveys = stats_surveys.filter(category_id=category_id)
        if sort == "oldest":
            stats_surveys = stats_surveys.order_by("created_at")
        elif sort == "title":
            stats_surveys = stats_surveys.order_by("title")
        else:
            stats_surveys = stats_surveys.order_by("-created_at")
        context["stats_survey_rows"] = stats_surveys
        context["categories"] = SurveyCategory.objects.all()
        context["current_sort"] = sort
        context["current_category"] = category_id
        payload = service_client.get_stats(selected_slug) if selected_slug else {"charts": [], "question_analysis": [], "inferential_analysis": []}
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
                "families": ("mean_comparison",),
            },
            {
                "key": "categorical_association",
                "badge": "類別關聯", "badge_class": "method-category",
                "title": "名目 × 名目",
                "desc": "單選名目題之間跑卡方檢定；多選題只做多重回應頻率，不當分組。",
                "families": ("categorical_association",),
            },
            {
                "key": "rank_correlation",
                "badge": "順序 / 相關", "badge_class": "method-rank",
                "title": "排序與關聯",
                "desc": "名目 × 順序跑非母數檢定；連續 × 連續跑 Pearson，涉及順序資料跑 Spearman。",
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
        text_surveys = (
            Survey.objects.filter(is_active=True)
            .select_related("category")
            .annotate(
                question_count=Count("questions", distinct=True),
                response_count=Count("submissions", distinct=True),
                text_question_count=Count(
                    "questions",
                    filter=Q(questions__data_type=Question.DataType.TEXT),
                    distinct=True,
                ),
                latest_submission_at=Max("submissions__submitted_at"),
            )
        )
        if category_id:
            text_surveys = text_surveys.filter(category_id=category_id)
        if sort == "oldest":
            text_surveys = text_surveys.order_by("created_at")
        elif sort == "title":
            text_surveys = text_surveys.order_by("title")
        else:
            text_surveys = text_surveys.order_by("-created_at")
        context["text_survey_rows"] = text_surveys
        context["categories"] = SurveyCategory.objects.all()
        context["current_sort"] = sort
        context["current_category"] = category_id
        text_analysis_payload = service_client.get_text_analysis(selected_slug) if survey else {}
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
            survey = get_object_or_404(Survey, id=request.POST.get("survey_id"))
            survey.improvement_tracking_enabled = request.POST.get("enabled") == "on"
            survey.save(update_fields=["improvement_tracking_enabled"])
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

    def dispatch(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not request.user.is_authenticated:
            messages.warning(request, "這份問卷需要先登入後才能填答。")
            return redirect(f"{reverse('accounts:login')}?next={request.path}")
        if not self.object.is_active:
            return self.render_to_response(
                self.get_context_data(survey_notice="這份問卷目前未開放填答。", survey_notice_type="error")
            )
        if not self.object.questions.exists():
            return self.render_to_response(
                self.get_context_data(survey_notice="這份問卷目前沒有任何題目。", survey_notice_type="warning")
            )
        if not request.user.is_manager:
            already = FeedbackSubmission.objects.filter(
                survey=self.object, user=request.user
            ).exists()
            if already:
                return self.render_to_response(
                    self.get_context_data(survey_notice="你已填答過這份問卷。", survey_notice_type="info")
                )
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
        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        respondent_form = RespondentMetaForm(request.POST, prefix="meta")
        form = SurveyFormBuilder(request.POST, survey=self.object)
        if form.is_valid() and respondent_form.is_valid():
            consent_follow_up = respondent_form.cleaned_data["consent_follow_up"]
            if request.user.is_authenticated and not request.user.is_manager:
                request.user.notification_opt_in = consent_follow_up
                request.user.save(update_fields=["notification_opt_in"])

            respondent_name = request.user.get_full_name() if request.user.is_authenticated else ""
            respondent_email = request.user.email if request.user.is_authenticated else ""

            submission_result = service_client.submit_survey(
                self.object,
                user=request.user if request.user.is_authenticated else None,
                respondent_name=respondent_name,
                respondent_email=respondent_email,
                consent_follow_up=consent_follow_up,
                answers={key: value for key, value in form.cleaned_data.items()},
            )

            if submission_result["thank_you_email_enabled"] and submission_result["respondent_email"]:
                send_mail(
                    subject=f"感謝填寫 {submission_result['survey_title']}",
                    message="我們已收到你的回覆。若後續有對應的改善通知，將依你的偏好主動提供最新進度。",
                    from_email=None,
                    recipient_list=[submission_result["respondent_email"]],
                    fail_silently=True,
                )
            return HttpResponseRedirect(reverse("feedback:survey-success", args=[self.object.slug]))

        context = self.get_context_data(object=self.object, form=form, respondent_form=respondent_form)
        return self.render_to_response(context)


class SurveySubmitSuccessView(TemplateView):
    template_name = "feedback/survey_success.html"


class ImprovementCreateView(ManagerRequiredMixin, CreateView):
    template_name = "feedback/improvement_form.html"
    form_class = ImprovementUpdateForm

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
        context = CreateView.get_context_data(self, **kwargs)
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
        if not is_stage_current(self.source_stage):
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
        context = CreateView.get_context_data(self, **kwargs)
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
