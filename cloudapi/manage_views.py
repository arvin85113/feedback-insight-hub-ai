from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.views.generic import TemplateView

from feedback.views import DashboardBaseMixin

from .inbox import abandon, database_bytes, inbox_summary, requeue
from .models import NodeDevice, SubmissionReceipt


class InboxManageView(DashboardBaseMixin, TemplateView):
    """Inbox status per node and quarantine handling; never shows answer text."""

    template_name = "cloudapi/inbox_manage.html"
    active_section = "cloudapi-manage:inbox"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.get_dashboard_base_context())
        nodes = []
        for node in NodeDevice.objects.order_by("name"):
            receipts = SubmissionReceipt.objects.filter(node=node)
            nodes.append({
                "node": node,
                "summary": inbox_summary(node),
                "synced_count": receipts.filter(status=SubmissionReceipt.Status.SYNCED).count(),
                "abandoned_count": receipts.filter(status=SubmissionReceipt.Status.ABANDONED).count(),
                "quarantined": receipts.filter(status=SubmissionReceipt.Status.QUARANTINED)
                .select_related("survey")
                .order_by("submitted_at"),
            })
        size = database_bytes()
        context.update({
            "nodes": nodes,
            "database_bytes": size,
            "database_warning": size is not None and size >= settings.CLOUD_DB_WARN_BYTES,
        })
        return context

    def post(self, request, *args, **kwargs):
        receipt = SubmissionReceipt.objects.filter(submission_uuid=request.POST.get("submission_uuid")).first()
        action = request.POST.get("action")
        try:
            if receipt is None:
                raise ValueError("找不到這筆回覆")
            if action == "requeue":
                requeue(receipt, request.user)
                messages.success(request, "已放回待收。")
            elif action == "abandon":
                abandon(receipt, request.user)
                messages.success(request, "已放棄這筆回覆。")
            else:
                raise ValueError("不支援的動作")
        except ValueError as exc:
            messages.error(request, str(exc))
        return redirect("cloudapi-manage:inbox")
