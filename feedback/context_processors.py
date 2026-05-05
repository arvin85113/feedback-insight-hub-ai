from .models import ImprovementDispatch


def unread_notification_count(request):
    if request.user.is_authenticated and not request.user.is_manager:
        count = ImprovementDispatch.objects.filter(
            submission__user=request.user, is_read=False
        ).count()
        return {"unread_notification_count": count}
    return {"unread_notification_count": 0}
