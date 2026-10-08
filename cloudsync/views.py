from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.views.generic import TemplateView

from node.audit import CLOUD_LINKED, CLOUD_UNLINKED, record
from node.views import NodeConsoleMixin
from organizations.models import OrganizationMembership

from .client import CloudClient, CloudError
from .forms import ConnectForm
from .models import CloudLink, PendingAck, ResultUpload
from .results import retry_failed
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
        context["pending_results"] = ResultUpload.objects.filter(status=ResultUpload.Status.PENDING).count()
        from .publication_status import publication_issues
        issues = publication_issues()
        context["unbound_results_count"] = issues.filter(cloud_bound=False).count()
        context["unbound_results"] = list(issues.filter(cloud_bound=False).values("survey__title")[:20])
        context["unqueued_results_count"] = issues.filter(cloud_bound=True).count()
        context["failed_results"] = list(
            ResultUpload.objects.filter(status=ResultUpload.Status.FAILED)
            .select_related("survey")
            .order_by("publish_sequence")
            .values("survey__title", "publish_sequence", "last_error")[:20]
        )
        context.setdefault("form", ConnectForm())
        from node.models import NodeAuditEvent
        context["backfill_errors"] = NodeAuditEvent.objects.filter(action="result.backfill_failed")[:20]
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
        if action == "retry-results":
            count = retry_failed()
            messages.success(request, f"已重新排入 {count} 份結果，下一次同步會上傳。")
            return redirect("cloudsync:connection")
        if action == "sync-now":
            result = run_cycle(force=True)
            if result == "ok":
                messages.success(request, "已完成本次雲端同步；結果上傳狀態以下方各版本紀錄為準。")
            elif result == "results_incomplete":
                messages.warning(request, "雲端連線正常，但部分本機結果尚未上傳；請查看未綁定來源、補建或失敗紀錄。")
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
