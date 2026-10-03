from django.contrib import admin

from .models import NodeDevice


@admin.register(NodeDevice)
class NodeDeviceAdmin(admin.ModelAdmin):
    """Read-only list plus revoke. Tokens are issued and rotated only by management
    commands, which print them once to the operator's terminal — never through
    Django messages, which are stored in the session/cookie."""

    list_display = ("name", "uuid", "status", "last_seen_at", "created_at")
    readonly_fields = ("uuid", "name", "token_hash", "created_at", "last_seen_at")
    actions = ("revoke",)

    def has_add_permission(self, request):
        return False

    @admin.action(description="撤銷權杖")
    def revoke(self, request, queryset):
        queryset.update(status=NodeDevice.Status.REVOKED)
