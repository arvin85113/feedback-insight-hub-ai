"""OWNER-only, signed preview → explicit import; no network call or worker work."""

from django import forms
from django.contrib import messages
from django.core import signing
from django.shortcuts import redirect
from django.views.generic import FormView

from feedback.history_transfer import HistoryBundle, HistoryError, import_history
from feedback.models import DatasetImportBatch
from organizations.models import OrganizationMembership

from .audit import record
from .views import NodeConsoleMixin

SALT = "history-copy-preview-v1"


class HistoryForm(forms.Form):
    package_path = forms.CharField(label="本機歷史資料包 ZIP 路徑", max_length=1024)
    confirmation = forms.CharField(required=False, widget=forms.HiddenInput)


class HistoryTransferView(NodeConsoleMixin, FormView):
    allowed_roles = (OrganizationMembership.Role.OWNER,)
    active_section = "cloudsync:connection"
    template_name = "node/history_transfer.html"
    form_class = HistoryForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["batches"] = DatasetImportBatch.objects.filter(source_name="cloud-history").select_related("survey")[:20]
        return context

    def form_valid(self, form):
        action = self.request.POST.get("action")
        path = form.cleaned_data["package_path"]
        try:
            with HistoryBundle(path) as bundle:
                if action == "preview":
                    token = signing.dumps({"sha256": bundle.sha256, "path": path, "actor": self.request.user.pk}, salt=SALT)
                    form.data = form.data.copy()
                    form.data["confirmation"] = token
                    return self.render_to_response(self.get_context_data(form=form, preview=bundle.preview()))
                if action != "import":
                    raise HistoryError("請先驗證並預覽資料包")
                confirmation = signing.loads(form.cleaned_data["confirmation"], salt=SALT, max_age=900)
                if confirmation != {"sha256": bundle.sha256, "path": path, "actor": self.request.user.pk}:
                    raise HistoryError("資料包或操作者已變更，請重新預覽")
                report = import_history(bundle)
        except (HistoryError, OSError, signing.BadSignature):
            form.add_error(None, "資料包驗證失敗、預覽已失效或來源內容衝突；未匯入。請重新核對檔案及既有副本。")
            return self.form_invalid(form)
        copied = sum(row["copied"] for row in report["surveys"])
        duplicates = sum(row["duplicates"] for row in report["surveys"])
        record("history.imported", request=self.request, target=report["package_sha256"],
               surveys=len(report["surveys"]), copied=copied, duplicates=duplicates)
        messages.success(self.request, f"歷史副本對帳完成：新增 {copied} 筆、相同重送 {duplicates} 筆；雲端未變更，未安排分析。")
        return redirect("node:history-transfer")
