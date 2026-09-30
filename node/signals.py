from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.dispatch import receiver

from .audit import LOGIN_FAILED, LOGIN_SUCCEEDED, record


@receiver(user_logged_in)
def audit_login(sender, request, user, **kwargs):
    record(LOGIN_SUCCEEDED, request=request, actor=user)


@receiver(user_login_failed)
def audit_login_failure(sender, credentials, request=None, **kwargs):
    attempted = credentials.get("email") or credentials.get("username") or ""
    record(LOGIN_FAILED, request=request, target=str(attempted).lower())
