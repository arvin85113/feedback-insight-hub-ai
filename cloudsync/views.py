from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.views.generic import TemplateView

from node.audit import CLOUD_LINKED, CLOUD_UNLINKED, record
from node.views import NodeConsoleMixin
from organizations.models import OrganizationMembership

from .client import CloudClient, CloudError
from .forms import ConnectForm
from .models import CloudLink, PendingAck
from .runner import run_cycle
from .tokens import delete_token, save_token


class ConnectionView(NodeConsoleMixin, TemplateView):
    template_name = "cloudsync/connection.html"
    active_section = "cloudsync:connection"
    allowed_roles = (OrganizationMembership.Role.OWNER,)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["link"] = CloudLink.load()
        context["inbox"] = context["link"].inbox_status or {}
        context["unconfirmed_acks"] = PendingAck.objects.exclude(last_status="").count()
        context.setdefault("form", ConnectForm())
        return context

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action")
        link = CloudLink.load()
        if action == "disconnect":
            if link.api_url:
                delete_token(link.api_url)
            CloudLink.unlink()  # bumps the generation, so a running sync cannot write the old link back
            record(CLOUD_UNLINKED, request=request, target=link.api_url)
            messages.success(request, "已中斷雲端連線。")
            return redirect("cloudsync:connection")
        if action == "sync-now":
            result = run_cycle(force=True)
            if result == "ok":
                messages.success(request, "同步完成。")
            else:
                messages.error(request, f"同步未完成：{result}")
            return redirect("cloudsync:connection")
        form = ConnectForm(request.POST)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        api_url, token = form.cleaned_data["api_url"], form.cleaned_data["token"]
        try:
            client = CloudClient(api_url, token, allow_loopback_http=settings.CLOUD_SYNC_ALLOW_LOOPBACK_HTTP)
            reply = client.post("heartbeat/")
        except CloudError as exc:
            form.add_error(None, f"測試連線失敗：{exc.kind}")
            return self.render_to_response(self.get_context_data(form=form))
        if link.api_url and link.api_url != api_url:
            delete_token(link.api_url)
        save_token(api_url, token)
        # Every (re)link bumps the generation and clears the cursor: the next cycle starts
        # from a snapshot, and any sync still running for the old link stops (spec §5).
        CloudLink.relink(api_url, reply["node_uuid"])
        record(CLOUD_LINKED, request=request, target=api_url)
        messages.success(request, "已連結雲端。")
        return redirect("cloudsync:connection")
