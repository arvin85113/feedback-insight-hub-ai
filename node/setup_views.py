import secrets

from django.conf import settings
from django.contrib.auth import login
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache

from config.node_paths import read_setup_token

from .forms import NodeSetupForm
from .models import NodeInstallation
from .setup import SetupAlreadyCompleted, complete_setup, token_digest

LOCAL_ADDRESSES = {"127.0.0.1", "::1"}
SESSION_KEY = "node_setup_token_digest"


def _blocked(request, reason):
    return render(request, "node/setup_blocked.html", {"reason": reason}, status=403)


def _current_digest():
    token = read_setup_token(settings.NODE_PATHS)
    return token_digest(token) if token else None


@never_cache
def setup_view(request):
    if NodeInstallation.setup_complete():
        raise Http404
    if request.META.get("REMOTE_ADDR") not in LOCAL_ADDRESSES:
        return _blocked(request, "首次設定只能在安裝本程式的電腦上進行。")

    current = _current_digest()
    supplied = request.GET.get("token", "")
    if supplied:
        if current and secrets.compare_digest(token_digest(supplied), current):
            request.session[SESSION_KEY] = current
            return redirect("node-setup")  # drop the token from the address bar
        return _blocked(request, "設定連結無效或已過期。")

    approved = request.session.get(SESSION_KEY, "")
    if not (current and approved and secrets.compare_digest(approved, current)):
        return _blocked(request, "設定連結無效或已過期。")

    form = NodeSetupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            owner = complete_setup(
                organization_name=form.cleaned_data["organization_name"],
                email=form.cleaned_data["email"],
                password=form.cleaned_data["password1"],
                request=request,
            )
        except SetupAlreadyCompleted:
            raise Http404
        request.session.pop(SESSION_KEY, None)
        login(request, owner, backend="django.contrib.auth.backends.ModelBackend")
        return redirect("node:overview")
    return render(request, "node/setup.html", {"form": form})
