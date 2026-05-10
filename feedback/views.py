import uuid
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.mail import send_mail
from django.db.models import Count, Max, Q
from django.db.models.functions import TruncDate
import segno

from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.text import slugify
from django.views.generic import CreateView, DeleteView, DetailView, TemplateView, View

from .forms import (
    ImprovementUpdateForm,
    QuestionCreateForm,
    RespondentMetaForm,
    SurveyCreateForm,
    SurveyEditForm,
    SurveyFormBuilder,
)
from .models import FeedbackSubmission, ImprovementDispatch, ImprovementUpdate, KeywordCategory, Question, Survey, SurveyCategory
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
            pk=pk,
            submission__user=request.user,
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
        return context


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
        slug = slugify(form.cleaned_data["title"])
        if Survey.objects.filter(slug=slug).exists():
            counter = 2
            while Survey.objects.filter(slug=f"{slug}-{counter}").exists():
                counter += 1
            slug = f"{slug}-{counter}"
        form.instance.slug = slug
        form.instance.improvement_tracking_enabled = True
        return super().form_valid(form)

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
        context = super().get_context_data(**kwargs)
        selected_slug = self.request.GET.get("survey")
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
                notice_count=Count("improvements", distinct=True),
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
            selected_survey.improvements.order_by("-created_at")
            if selected_survey else ImprovementUpdate.objects.none()
        )
        context["selected_survey"] = selected_survey
        context["notice_survey_rows"] = notice_surveys
        context["notices"] = notices
        context["create_url"] = (
            reverse("feedback:improvement-create", args=[selected_survey.slug]) if selected_survey else ""
        )
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
        response = super().form_valid(form)

        all_recipients = (
            self.survey.submissions.select_related("user")
            .filter(Q(consent_follow_up=True) | Q(user__notification_opt_in=True))
            .exclude(respondent_email="")
        )

        related_recipients_ids = set()
        if form.instance.related_category:
            related_qs = all_recipients.filter(
                answers__question__survey=self.survey,
                answers__value__icontains=form.instance.related_category,
            ).distinct()
            related_recipients_ids = set(related_qs.values_list("id", flat=True))

        sent_count = 0
        for submission in all_recipients.distinct():
            if submission.user and not submission.user.notification_opt_in:
                continue
            if not submission.consent_follow_up and not (submission.user and submission.user.notification_opt_in):
                continue

            is_related = submission.id in related_recipients_ids

            dispatch, created = ImprovementDispatch.objects.get_or_create(
                improvement=form.instance,
                submission=submission,
                defaults={
                    "personalized_note": (
                        f"你先前在 {form.instance.related_category or self.survey.title} 提供的回饋，"
                        "已被納入這次改善項目中，因此我們主動把最新進度同步給你。"
                    ) if is_related else ""
                },
            )
            if not created:
                continue

            send_mail(
                subject=f"{self.survey.title} 改善進度通知",
                message=f"{form.instance.title}\n\n{form.instance.summary}",
                from_email=None,
                recipient_list=[submission.respondent_email],
                fail_silently=True,
            )

            if is_related:
                send_mail(
                    subject=f"感謝你的回饋！{self.survey.title}",
                    message=(
                        f"親愛的 {submission.display_name}，\n\n"
                        f"感謝你先前填寫「{self.survey.title}」並提供寶貴意見。\n"
                        f"你的回饋直接促成了我們這次的改善：\n\n"
                        f"【{form.instance.title}】\n"
                        f"{form.instance.summary}\n\n"
                        f"感謝你讓我們變得更好！"
                    ),
                    from_email=None,
                    recipient_list=[submission.respondent_email],
                    fail_silently=True,
                )
            sent_count += 1

        if sent_count:
            form.instance.emailed_at = timezone.now()
            form.instance.save(update_fields=["emailed_at"])

        messages.success(self.request, f"改善通知已建立，並成功寄送給 {sent_count} 位填答者。")
        return response

    def get_success_url(self):
        return f"{reverse('feedback:improvement-list')}?survey={self.survey.slug}"
