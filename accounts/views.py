from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.views.generic import CreateView

from feedback.models import FeedbackSubmission, SurveyCategory

from .forms import CustomerPreferenceForm, CustomerProfileForm, CustomerSignUpForm, LoginForm


class PlatformLoginView(LoginView):
    authentication_form = LoginForm
    template_name = "accounts/login.html"

    def get_success_url(self):
        # Respect ?next= first (e.g. survey QR redirect), then fall back to role-based home
        next_url = self.get_redirect_url()
        if next_url:
            return next_url
        if self.request.user.is_manager:
            return str(reverse_lazy("feedback:dashboard"))
        return str(reverse_lazy("feedback:customer-home"))


class PlatformLogoutView(LogoutView):
    pass


class CustomerSignUpView(CreateView):
    form_class = CustomerSignUpForm
    template_name = "accounts/signup.html"

    def get_success_url(self):
        # Preserve ?next= so the login page knows where to go after login
        # GET: initial page load; POST: hidden field submitted with form
        next_url = self.request.POST.get("next") or self.request.GET.get("next", "")
        base = str(reverse_lazy("accounts:login"))
        if next_url:
            from urllib.parse import urlencode
            return f"{base}?{urlencode({'next': next_url})}"
        return base

    def form_valid(self, form):
        messages.success(self.request, "顧客帳號已建立，請登入後查看填答紀錄與通知。")
        return super().form_valid(form)


@login_required
def customer_preferences_view(request):
    if request.user.is_manager:
        return redirect("feedback:dashboard")

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "toggle-global":
            form = CustomerPreferenceForm(request.POST, instance=request.user)
            if form.is_valid():
                form.save()
                messages.success(request, "通知偏好已儲存。")
                return redirect("accounts:preferences")
        elif action == "toggle-survey":
            survey_id = request.POST.get("survey_id")
            enabled = request.POST.get("enabled") == "on"
            updated = FeedbackSubmission.objects.filter(user=request.user, survey_id=survey_id).update(
                consent_follow_up=enabled
            )
            if updated:
                state = "開啟" if enabled else "關閉"
                messages.success(request, f"這份問卷的改善通知已{state}。")
            return redirect("accounts:preferences")

    form = CustomerPreferenceForm(instance=request.user)
    sort = request.GET.get("sort", "newest")
    category_id = request.GET.get("category", "")

    submissions = (
        FeedbackSubmission.objects.filter(user=request.user)
        .select_related("survey", "survey__category")
        .order_by("-submitted_at")
    )
    survey_rows_by_id = {}
    for submission in submissions:
        row = survey_rows_by_id.setdefault(
            submission.survey_id,
            {
                "survey": submission.survey,
                "latest_submission": submission,
                "submission_count": 0,
                "consent_follow_up": False,
            },
        )
        row["submission_count"] += 1
        row["consent_follow_up"] = row["consent_follow_up"] or submission.consent_follow_up
        if submission.submitted_at > row["latest_submission"].submitted_at:
            row["latest_submission"] = submission

    survey_rows = list(survey_rows_by_id.values())
    if category_id:
        survey_rows = [
            row for row in survey_rows
            if row["survey"].category_id and str(row["survey"].category_id) == category_id
        ]
    if sort == "oldest":
        survey_rows.sort(key=lambda row: row["latest_submission"].submitted_at)
    elif sort == "title":
        survey_rows.sort(key=lambda row: row["survey"].title)
    else:
        survey_rows.sort(key=lambda row: row["latest_submission"].submitted_at, reverse=True)

    return render(
        request,
        "accounts/preferences.html",
        {
            "form": form,
            "survey_rows": survey_rows,
            "categories": SurveyCategory.objects.all(),
            "current_category": category_id,
            "current_sort": sort,
        },
    )


@login_required
def customer_profile_view(request):
    if request.user.is_manager:
        return redirect("feedback:dashboard")

    if request.method == "POST":
        form = CustomerProfileForm(request.POST, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "個人資料已儲存。")
            return redirect("accounts:profile")
    else:
        form = CustomerProfileForm(instance=request.user)

    return render(request, "accounts/profile.html", {"form": form})
