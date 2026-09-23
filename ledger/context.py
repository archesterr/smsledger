from django.conf import settings

from .models import Transaction


def globals(request):
    ctx = {"site_name": settings.SITE_NAME}
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        ctx["unit"] = user.unit
        ctx["inbox_count"] = Transaction.objects.filter(user=user, category__isnull=True).count()
    return ctx
