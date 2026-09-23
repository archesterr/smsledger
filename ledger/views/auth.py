import time
from urllib.parse import urlencode

from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_not_required
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .. import security
from ..forms import CodeForm, LoginForm, SignupForm
from ..models import Invite, RecoveryCode, User

PENDING_TTL = 300  # seconds between password and 2FA code
BACKEND = "django.contrib.auth.backends.ModelBackend"


def safe_next(request) -> str:
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    ok = url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure())
    return nxt if ok else ""


def check_second_factor(user: User, code: str) -> bool:
    step = security.totp_verify(user.totp_secret, code, user.totp_last_step)
    # conditional update: two concurrent requests can't both use the same code
    if step and User.objects.filter(pk=user.pk, totp_last_step__lt=step).update(totp_last_step=step):
        return True
    h = security.recovery_hash(code)
    return bool(RecoveryCode.objects.filter(user=user, code_hash=h, used_at__isnull=True)
                .update(used_at=timezone.now()))


@login_not_required
def login_view(request):
    if request.user.is_authenticated:
        return redirect("home")
    form = LoginForm(request.POST or None)
    error = ""
    if request.method == "POST" and form.is_valid():
        ip, name = security.client_ip(request), form.cleaned_data["username"].strip().casefold()
        if security.LOGIN_IP.blocked(ip) or security.LOGIN_USER.blocked(name):
            error = "تلاش‌های ناموفق زیاد بود. ۱۵ دقیقه دیگر دوباره امتحان کنید."
        else:
            # usernames are unique case-insensitively (see SignupForm), so match that way
            u = User.objects.filter(username__iexact=form.cleaned_data["username"].strip()).first()
            user = authenticate(request, username=u.username if u else form.cleaned_data["username"],
                                password=form.cleaned_data["password"])
            if user is None:
                security.LOGIN_IP.hit(ip)
                security.LOGIN_USER.hit(name)
                error = "نام کاربری یا رمز عبور درست نیست."
            elif user.has_2fa:
                request.session["pending_2fa"] = {"uid": user.pk, "at": time.time(), "next": safe_next(request)}
                return redirect("login_2fa")
            else:
                security.LOGIN_USER.reset(name)
                login(request, user, backend=BACKEND)
                return redirect(safe_next(request) or "home")
    return render(request, "ledger/auth/login.html", {"form": form, "error": error, "next": safe_next(request)})


@login_not_required
def login_2fa(request):
    pending = request.session.get("pending_2fa")
    user = None
    if pending and time.time() - pending["at"] < PENDING_TTL:
        user = User.objects.filter(pk=pending["uid"], is_active=True).first()
    if user is None:
        request.session.pop("pending_2fa", None)
        return redirect("login")
    form = CodeForm(request.POST or None)
    error = ""
    if request.method == "POST" and form.is_valid():
        if security.TOTP_USER.blocked(user.pk):
            error = "تلاش‌های ناموفق زیاد بود. ۱۵ دقیقه دیگر دوباره امتحان کنید."
        elif check_second_factor(user, form.cleaned_data["code"]):
            request.session.pop("pending_2fa", None)
            login(request, user, backend=BACKEND)  # rotates the session key
            return redirect(pending.get("next") or "home")
        else:
            security.TOTP_USER.hit(user.pk)
            error = "کد درست نیست."
    return render(request, "ledger/auth/login_2fa.html", {"form": form, "error": error})


@require_POST
def logout_view(request):
    logout(request)
    return redirect("login")


@login_not_required
def admin_login(request):
    """Django admin's own login would skip 2FA: send it through ours."""
    return redirect(f"{reverse('login')}?{urlencode({'next': request.GET.get('next', '/admin/')})}")


@login_not_required
def join(request, code):
    ip = security.client_ip(request)
    if security.JOIN_IP.blocked(ip):
        return render(request, "ledger/auth/join_invalid.html", {"reason": "rate"}, status=429)
    invite = Invite.objects.filter(code_hash=security.sha256(code)).first()
    if not invite or not invite.is_usable:
        security.JOIN_IP.hit(ip)
        return render(request, "ledger/auth/join_invalid.html", {"reason": "invalid"}, status=404)
    if request.user.is_authenticated:
        return render(request, "ledger/auth/join_invalid.html", {"reason": "logged_in"})
    form = SignupForm(request.POST or None)
    if request.method == "POST":
        security.JOIN_IP.hit(ip)
        if form.is_valid():
            with transaction.atomic():
                inv = Invite.objects.select_for_update().get(pk=invite.pk)
                if not inv.is_usable:
                    return render(request, "ledger/auth/join_invalid.html", {"reason": "invalid"}, status=404)
                user = form.save()
                inv.used_by, inv.used_at = user, timezone.now()
                inv.save(update_fields=["used_by", "used_at"])
            login(request, user, backend=BACKEND)
            return redirect("setup")
    return render(request, "ledger/auth/join.html", {"form": form, "invite": invite})
