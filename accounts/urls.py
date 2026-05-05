from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from .views import (
    CustomerSignUpView,
    PlatformLoginView,
    PlatformLogoutView,
    customer_preferences_view,
    customer_profile_view,
)

app_name = "accounts"

urlpatterns = [
    path("login/", PlatformLoginView.as_view(), name="login"),
    path("logout/", PlatformLogoutView.as_view(), name="logout"),
    path("signup/", CustomerSignUpView.as_view(), name="signup"),
    path(
        "password-reset/",
        auth_views.PasswordResetView.as_view(
            template_name="accounts/password_reset_form.html",
            email_template_name="accounts/password_reset_email.html",
            subject_template_name="accounts/password_reset_subject.txt",
            success_url=reverse_lazy("accounts:password_reset_done"),
        ),
        name="password_reset",
    ),
    path(
        "password-reset/done/",
        auth_views.PasswordResetDoneView.as_view(template_name="accounts/password_reset_done.html"),
        name="password_reset_done",
    ),
    path(
        "reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="accounts/password_reset_confirm.html",
            success_url=reverse_lazy("accounts:password_reset_complete"),
        ),
        name="password_reset_confirm",
    ),
    path(
        "reset/done/",
        auth_views.PasswordResetCompleteView.as_view(template_name="accounts/password_reset_complete.html"),
        name="password_reset_complete",
    ),
    path(
        "password-change/",
        auth_views.PasswordChangeView.as_view(
            template_name="accounts/password_change_form.html",
            success_url=reverse_lazy("accounts:password_change_done"),
        ),
        name="password_change",
    ),
    path(
        "password-change/done/",
        auth_views.PasswordChangeDoneView.as_view(template_name="accounts/password_change_done.html"),
        name="password_change_done",
    ),
    path("preferences/", customer_preferences_view, name="preferences"),
    path("profile/", customer_profile_view, name="profile"),
]
