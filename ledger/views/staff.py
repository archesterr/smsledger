"""Operator pages. Deliberately show only counts and health signals, never anyone's amounts,
balances or SMS (except samples a user explicitly shared)."""
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.db.models import Count, Max, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .. import security
from ..models import Invite, Message, SupportSample, User


def staff_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_staff:
            raise Http404
        if not request.user.has_2fa:
            messages.warning(request, "برای دسترسی مدیر، اول ورود دو مرحله‌ای را روشن کنید.")
            return redirect("twofa_setup")
        return view(request, *args, **kwargs)
    return wrapper


@staff_required
def staff_home(request):
    users = (User.objects.annotate(
        n_devices=Count("devices", filter=Q(devices__revoked_at__isnull=True), distinct=True),
        last_seen=Max("devices__last_used_at"),
    ).order_by("-date_joined"))
    unparsed = dict(Message.objects.filter(status=Message.UNPARSED).values_list("user").annotate(n=Count("id")))
    silent_before = timezone.now() - timedelta(days=settings.SILENT_DAYS)
    rows = [{"u": u, "unparsed": unparsed.get(u.pk, 0),
             "silent": bool(u.n_devices and (u.last_seen is None or u.last_seen < silent_before))} for u in users]
    return render(request, "ledger/staff.html", {
        "nav": "more", "rows": rows,
        "invites": Invite.objects.select_related("used_by").order_by("-created_at")[:30],
        "samples": SupportSample.objects.filter(resolved=False).select_related("user").order_by("-created_at"),
        "new_link": request.session.pop("new_invite_link", None),
    })


@staff_required
@require_POST
def invite_create(request):
    code = security.new_invite_code()
    Invite.objects.create(code_hash=security.sha256(code), note=request.POST.get("note", "")[:100],
                          created_by=request.user,
                          expires_at=timezone.now() + timedelta(days=settings.INVITE_DAYS))
    # shown once on the next page load; only the hash stays in the invite table
    request.session["new_invite_link"] = request.build_absolute_uri(f"/join/{code}/")
    return redirect("staff")


@staff_required
@require_POST
def invite_revoke(request, pk):
    Invite.objects.filter(pk=pk, used_at__isnull=True).update(expires_at=timezone.now())
    return redirect("staff")


@staff_required
@require_POST
def user_toggle(request, pk):
    u = get_object_or_404(User, pk=pk)
    if u.pk == request.user.pk:
        messages.error(request, "نمی‌توانید حساب خودتان را غیرفعال کنید.")
    else:
        u.is_active = not u.is_active
        u.save(update_fields=["is_active"])
        messages.success(request, f"«{u.username}» {'فعال' if u.is_active else 'غیرفعال'} شد.")
    return redirect("staff")


@staff_required
@require_POST
def sample_resolve(request, pk):
    SupportSample.objects.filter(pk=pk).update(resolved=True)
    return redirect("staff")
