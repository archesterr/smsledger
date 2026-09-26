import time
from urllib.parse import urlencode

import segno
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_not_required
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .. import audit, security, vault
from ..forms import CodeForm, LoginForm, SignupForm
from ..models import Invite, RecoveryCode, User
from .me import RE_IOS

PENDING_TTL = 300  # seconds between password and 2FA code
BACKEND = "django.contrib.auth.backends.ModelBackend"


def safe_next(request) -> str:
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    ok = url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure())
    return nxt if ok else ""


def check_second_factor(user: User, code: str) -> str | None:
    """"totp" / "recovery" for a good code (each works once), None otherwise."""
    step = security.totp_verify(user.totp_secret, code, user.totp_last_step)
    # conditional update: two concurrent requests can't both use the same code
    if step and User.objects.filter(pk=user.pk, totp_last_step__lt=step).update(totp_last_step=step):
        return "totp"
    h = security.recovery_hash(code)
    if RecoveryCode.objects.filter(user=user, code_hash=h, used_at__isnull=True).update(used_at=timezone.now()):
        return "recovery"
    return None


def open_vault(request, user: User, password: str) -> None:
    """Right after a correct password (the only time it's in hand): unlock the data key into this
    session. The first time after the upgrade, this is also what puts the key under the password."""
    first = user.vault_state == vault.UNPROTECTED
    dek = vault.unlock_password(user, password)
    if first:
        vault.protect(user, dek, password)
        user.save(update_fields=["key_pw", "key_srv"])
        audit.record(user, "vault_on", request)
    if dek is not None:  # None: password was reset outside the app; the unlock page asks for the recovery key
        vault.start_session(request, user, dek)


def finish_login(request, user: User) -> None:
    prev = user.last_login
    login(request, user, backend=BACKEND)  # rotates the session key; keeps the vault entry
    request.session["prev_login"] = prev.isoformat() if prev else ""
    audit.start_session(request, user)
    audit.record(user, "login", request)


@login_not_required
def login_view(request):
    if request.user.is_authenticated:
        return redirect("home")
    form = LoginForm(request.POST or None)
    error = ""
    if request.method == "POST" and form.is_valid():
        ip, name = security.client_ip(request), form.cleaned_data["username"].strip().casefold()
        # usernames are unique case-insensitively (see SignupForm), so match that way
        u = User.objects.filter(username__iexact=form.cleaned_data["username"].strip()).first()
        if security.LOGIN_IP.blocked(ip) or security.LOGIN_USER.blocked(name):
            error = "تلاش‌های ناموفق زیاد بود. ۱۵ دقیقه دیگر دوباره امتحان کنید."
            if u and security.LOGIN_USER.count(name) == security.LOGIN_USER.limit:
                security.LOGIN_USER.hit(name)  # record the lockout once, not every blocked try
                audit.record(u, "login_blocked", request)
        else:
            password = form.cleaned_data["password"]
            user = authenticate(request, username=u.username if u else form.cleaned_data["username"],
                                password=password)
            if user is None:
                security.LOGIN_IP.hit(ip)
                security.LOGIN_USER.hit(name)
                if u:
                    audit.record(u, "login_failed", request)
                error = "نام کاربری یا رمز عبور درست نیست."
            else:
                open_vault(request, user, password)
                if user.has_2fa:
                    request.session["pending_2fa"] = {"uid": user.pk, "at": time.time(), "next": safe_next(request)}
                    return redirect("login_2fa")
                security.LOGIN_USER.reset(name)
                finish_login(request, user)
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
        request.session.pop(vault.SESSION_KEY, None)
        return redirect("login")
    form = CodeForm(request.POST or None)
    error = ""
    if request.method == "POST" and form.is_valid():
        kind = None if security.TOTP_USER.blocked(user.pk) else check_second_factor(user, form.cleaned_data["code"])
        if security.TOTP_USER.blocked(user.pk):
            error = "تلاش‌های ناموفق زیاد بود. ۱۵ دقیقه دیگر دوباره امتحان کنید."
        elif kind:
            request.session.pop("pending_2fa", None)
            finish_login(request, user)
            if kind == "recovery":
                audit.record(user, "recovery_code_used", request)
            return redirect(pending.get("next") or "home")
        else:
            security.TOTP_USER.hit(user.pk)
            audit.record(user, "2fa_failed", request)
            error = "کد درست نیست."
    return render(request, "ledger/auth/login_2fa.html", {"form": form, "error": error})


@require_POST
def logout_view(request):
    if request.user.is_authenticated:
        sid = request.session.get(audit.SID)
        if sid:
            audit.end(request.user, pk=sid)
        audit.record(request.user, "logout", request)
    logout(request)
    request._vault_clear = True
    return redirect("login")


@login_not_required
def admin_login(request):
    """Django admin's own login would skip 2FA: send it through ours."""
    if request.user.is_authenticated and request.user.is_staff and not request.user.has_2fa:
        messages.warning(request, "برای دسترسی مدیر، اول ورود دو مرحله‌ای را روشن کنید.")
        return redirect("twofa_setup")
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
                user = form.save()  # set_password made the user's keys (vault.password_set)
                inv.used_by, inv.used_at = user, timezone.now()
                inv.save(update_fields=["used_by", "used_at"])
            vault.start_session(request, user, vault.key_for(user.pk))
            finish_login(request, user)
            return redirect("setup")  # after the recovery key page (middleware)
    on_iphone = bool(RE_IOS.search(request.headers.get("User-Agent", "")))
    return render(request, "ledger/auth/join.html", {
        "form": form, "invite": invite, "on_iphone": on_iphone,
        "inviter": invite.created_by.username if invite.created_by else "",
        # opened on a computer: the same link as a QR, to carry on on the iPhone
        "qr": None if on_iphone else segno.make(request.build_absolute_uri(), error="m").svg_inline(
            scale=5, omitsize=True, dark="#111", light="#fff", border=2),
    })
