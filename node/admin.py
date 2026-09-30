from django.contrib import admin

from .models import NodeAuditEvent, NodeInstallation


@admin.register(NodeAuditEvent)
class NodeAuditEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "actor_email", "target", "ip")
    list_filter = ("action",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(NodeInstallation)
