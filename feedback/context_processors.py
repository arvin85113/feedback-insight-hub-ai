from django.db.models import Q

from .models import ImprovementDispatch


def unread_notification_count(request):
    if request.user.is_authenticated and not request.user.is_manager:
        count = ImprovementDispatch.objects.filter(
            Q(recipient_user=request.user) | Q(submission__user=request.user),
            delivery_status=ImprovementDispatch.DeliveryStatus.SENT,
            is_read=False,
        ).count()
        return {"unread_notification_count": count}
    return {"unread_notification_count": 0}
