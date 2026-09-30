from django.conf import settings


def deployment(request):
    return {"is_node": settings.IS_NODE}
