import logging
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordResetForm
from django.contrib.auth.views import LoginView, LogoutView
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views.generic import CreateView

from feedback.models import FeedbackSubmission, Survey, SurveyCategory

from .emails import send_password_reset_email_via_resend, send_verification_email
from .forms import CustomerPreferenceForm, CustomerProfileForm, CustomerSignUpForm, LoginForm
from .models import User
from .tokens import verify_verification_token

logger = logging.getLogger(__name__)


# ── Safe password reset form — bypasses SMTP, uses SendGrid HTTP API ──
class SafePasswordResetForm(PasswordResetForm):
    def send_mail(self, subject_template_name, email_template_name,
                  context, from_email, to_email, html_email_template_name=None):
        uid = context.get("uid")
        token = context.get("token")
        protocol = context.get("protocol", "https")
        domain = context.get("domain", "")
        if uid and token and domain:
            reset_url = f"{protocol}://{domain}/accounts/reset/{uid}/{token}/"
            send_password_reset_email_via_resend(to_email, reset_url)
        else:
            logger.error("SafePasswordResetForm: missing context for %s", to_email)


class PlatformLoginView(LoginView):
    authentication_form = LoginForm
    template_name = "accounts/login.html"

    def get(self, request, *args, **kwargs):
        self._unverified_email = request.session.pop("unverified_email", None)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["unverified_email"] = getattr(self, "_unverified_email", None)
        next_url = self.request.GET.get("next", "") or self.request.POST.get("next", "")
        m = re.match(r"^/survey/([^/]+)/", next_url)
        ctx["survey_context"] = Survey.objects.filter(slug=m.group(1)).only("title").first() if m else None
        return ctx

    def get_success_url(self):
        from django.utils.http import url_has_allowed_host_and_scheme
        next_url = self.request.POST.get("next") or self.request.GET.get("next", "")
        if next_url and url_has_allowed_host_and_scheme(
            url=next_url,
            allowed_hosts={self.request.get_host()},
            require_https=False,
        ):
            return next_url
        if self.request.user.is_manager:
            return str(reverse_lazy("feedback:dashboard"))
        return str(reverse_lazy("feedback:customer-home"))

    def form_valid(self, form):
        user = form.get_user()
        if not user.is_manager and not user.is_superuser and not user.is_email_verified:
            messages.warning(self.request, "請先驗證你的信箱。沒收到信？")
            self.request.session["unverified_email"] = user.email
            return redirect("accounts:login")
        return super().form_valid(form)


class PlatformLogoutView(LogoutView):
    pass


class CustomerSignUpView(CreateView):
    form_class = CustomerSignUpForm
    template_name = "accounts/signup.html"

    def get_success_url(self):
        next_url = self.request.POST.get("next") or self.request.GET.get("next", "")
        base = str(reverse_lazy("accounts:login"))
        if next_url:
            from urllib.parse import urlencode
            return f"{base}?{urlencode({'next': next_url})}"
        return base

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        next_url = self.request.GET.get("next", "") or self.request.POST.get("next", "")
        m = re.match(r"^/survey/([^/]+)/", next_url)
        ctx["survey_context"] = Survey.objects.filter(slug=m.group(1)).only("title").first() if m else None
        return ctx

    def form_valid(self, form):
        response = super().form_valid(form)
        send_verification_email(self.object, self.request)
        messages.success(self.request, "帳號已建立！請前往你的信箱點擊驗證連結後再登入。")
        return response


def verify_email_view(request, token):
    pk = verify_verification_token(token)
    if pk is None:
        return render(request, "accounts/verify_email_invalid.html")
    user = get_object_or_404(User, pk=pk)
    if not user.is_email_verified:
        user.is_email_verified = True
        user.save(update_fields=["is_email_verified"])
    messages.success(request, "信箱驗證成功，請登入。")
    return redirect("accounts:login")


def resend_verification_view(request):
    if request.method == "POST":
        email = request.POST.get("email", "").strip()
        try:
            user = User.objects.get(email=email, is_email_verified=False)
            send_verification_email(user, request)
        except User.DoesNotExist:
            pass
        messages.info(request, "若信箱存在且尚未驗證，驗證信已重新寄出。")
    return redirect("accounts:login")


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
