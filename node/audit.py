"""Append-only audit trail for security-relevant node actions."""

from .models import NodeAuditEvent

SETUP_COMPLETED = "setup.completed"
LOGIN_SUCCEEDED = "login.succeeded"
LOGIN_FAILED = "login.failed"
ORGANIZATION_RENAMED = "organization.renamed"
CLOUD_LINKED = "cloud.linked"
CLOUD_UNLINKED = "cloud.unlinked"
DATASET_REGISTERED = "dataset.registered"

ACTION_LABELS = {
    SETUP_COMPLETED: "完成首次設定",
    LOGIN_SUCCEEDED: "登入成功",
    LOGIN_FAILED: "登入失敗",
    ORGANIZATION_RENAMED: "變更組織名稱",
    CLOUD_LINKED: "連結雲端",
    CLOUD_UNLINKED: "中斷雲端連線",
    DATASET_REGISTERED: "登錄外部資料集",
}


def client_ip(request):
    # The node serves browsers directly (no reverse proxy), so REMOTE_ADDR is the client.
    return request.META.get("REMOTE_ADDR") or None


def record(action, *, request=None, actor=None, target="", **details):
    if actor is None and request is not None:
        user = getattr(request, "user", None)
        actor = user if getattr(user, "is_authenticated", False) else None
    return NodeAuditEvent.objects.create(
        actor=actor,
        actor_email=(getattr(actor, "email", "") or "")[:254],
        action=action,
        target=str(target)[:255],
        ip=client_ip(request) if request is not None else None,
        details=details,
    )
