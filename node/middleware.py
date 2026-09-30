from django.conf import settings
from django.shortcuts import redirect

from .models import NodeInstallation


class SetupRequiredMiddleware:
    """Until first-run setup finishes, every page except setup leads to /setup/."""

    EXEMPT_PREFIXES = ("/setup/", "/healthz/")

    def __init__(self, get_response):
        self.get_response = get_response
        self._complete = False  # setup never becomes incomplete again

    def __call__(self, request):
        if settings.NODE_SETUP_GATE and not self._complete and not self._exempt(request.path):
            if NodeInstallation.setup_complete():
                self._complete = True
            else:
                return redirect("node-setup")
        return self.get_response(request)

    def _exempt(self, path):
        static_prefix = "/" + settings.STATIC_URL.lstrip("/")
        return path.startswith(self.EXEMPT_PREFIXES) or path.startswith(static_prefix)
