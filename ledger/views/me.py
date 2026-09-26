import re

import segno
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout, update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .. import audit, security, shortcut
from ..forms import CodeForm, DeviceForm, PasswordConfirmForm, UnitPrefForm
from ..models import Device, RecoveryCode, Transaction, User
from .app import csv_response

# ---- setup: devices + iPhone instructions -----------------------------------------------
RE_IOS = re.compile(r"(?:iPhone|iPad|CPU) OS (\d+)_")


def ios_version(request) -> int | None:
    """Major iOS version from Safari's User-Agent, to open the right automation guide."""
    m = RE_IOS.search(request.headers.get("User-Agent", ""))
    return int(m.group(1)) if m else None


def setup(request):
    u = request.user
    ios = ios_version(request)
    new_token = new_device = None
    form = DeviceForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        token = security.new_token()
        name = form.cleaned_data["name"] or (f"iPhone · iOS {ios}" if ios else "iPhone")
        with transaction.atomic():
            # keys from earlier taps that never reached the server are just loose ends
            stale = u.devices.filter(revoked_at__isnull=True, last_used_at__isnull=True).update(
                revoked_at=timezone.now())
            new_device = Device.objects.create(user=u, name=name, token_hash=security.sha256(token),
                                               token_prefix=token[:8])
        if stale:
            audit.record(u, "device_revoked", request)
        audit.record(u, "device_added", request)
        new_token = token  # shown once, in this response only; the DB keeps just the hash
        form = DeviceForm()
    devices = u.devices.filter(revoked_at__isnull=True).order_by("-created_at")
    return render(request, "ledger/setup.html", {
        "nav": "more", "form": form, "new_token": new_token, "new_device": new_device, "devices": devices,
        "connect_url": shortcut.connect_url(new_token) if new_token else None,
        "ingest_url": request.build_absolute_uri("/ingest"),
        "shortcut_url": settings.SHORTCUT_URL, "shortcut_name": shortcut.NAME,
        "otp_pattern": shortcut.OTP_PATTERN,
        "connected": devices.filter(last_used_at__isnull=False).exists(),
        "has_sms": u.messages.exists(),
        "ios": ios,
        # opened on a computer: the Connect button only works on the iPhone itself
        "phone_qr": None if ios else segno.make(request.build_absolute_uri(request.path), error="m").svg_inline(
            scale=5, omitsize=True, dark="#111", light="#fff", border=2),
    })


@require_GET
def device_status(request, pk):
    """Polled by the setup page while the Shortcut runs the Connect step."""
    d = get_object_or_404(Device, pk=pk, user=request.user)
    return JsonResponse({"connected": d.revoked_at is None and d.last_used_at is not None})


@require_GET
def shortcut_file(request):
    """The unsigned Shortcut with this server's address in it, for `shortcuts sign` on a Mac.
    It holds no key: the Connect button puts each phone's own key next to it."""
    resp = HttpResponse(shortcut.build(request.build_absolute_uri("/ingest")),
                        content_type="application/octet-stream")
    resp["Content-Disposition"] = f'attachment; filename="{shortcut.NAME}.shortcut"'
    return resp


@require_POST
def device_revoke(request, pk):
    d = get_object_or_404(Device, pk=pk, user=request.user, revoked_at__isnull=True)
    d.revoked_at = timezone.now()
    d.save(update_fields=["revoked_at"])
    audit.record(request.user, "device_revoked", request)
    messages.success(request, f"دسترسی «{d.name}» قطع شد.")
    return redirect("security" if request.POST.get("back") == "security" else "setup")


# ---- settings -------------------------------------------------------------------------
def settings_view(request):
    u = request.user
    unit_form = UnitPrefForm(instance=u)
    pw_form = PasswordChangeForm(u)
    if request.method == "POST" and request.POST.get("form") == "unit":
        unit_form = UnitPrefForm(request.POST, instance=u)
        if unit_form.is_valid():
            unit_form.save()
            messages.success(request, "ذخیره شد.")
            return redirect("settings")
    elif request.method == "POST" and request.POST.get("form") == "password":
        pw_form = PasswordChangeForm(u, request.POST)
        if pw_form.is_valid():
            u.password_changed_at = timezone.now()
            pw_form.save()  # set_password re-wraps the data key with the new password (vault.py)
            update_session_auth_hash(request, pw_form.user)  # other sessions are logged out
            audit.end(u, exclude_pk=request.session.get(audit.SID))
            audit.record(u, "password_changed", request)
            messages.success(request, "رمز عبور عوض شد. بقیه دستگاه‌ها از حساب خارج شدند.")
            return redirect("settings")
    return render(request, "ledger/settings.html", {
        "nav": "more", "unit_form": unit_form, "pw_form": pw_form,
        "recovery_left": u.recovery_codes.filter(used_at__isnull=True).count(),
    })


def twofa_setup(request):
    """Enroll TOTP: the secret lives in the session until the user proves they saved it."""
    u = request.user
    if u.has_2fa:
        return redirect("settings")
    secret = request.session.get("totp_setup") or security.totp_new_secret()
    request.session["totp_setup"] = secret
    form = CodeForm(request.POST or None)
    codes = None
    if request.method == "POST" and form.is_valid():
        step = security.totp_verify(secret, form.cleaned_data["code"])
        if step:
            codes = security.new_recovery_codes()
            with transaction.atomic():
                u.totp_secret, u.totp_last_step = secret, step
                u.save(update_fields=["totp_secret", "totp_last_step"])
                u.recovery_codes.all().delete()
                RecoveryCode.objects.bulk_create(
                    RecoveryCode(user=u, code_hash=security.recovery_hash(c)) for c in codes)
            request.session.pop("totp_setup", None)
            audit.record(u, "2fa_on", request)
            return render(request, "ledger/twofa_done.html", {"nav": "more", "codes": codes})
        form.add_error("code", "کد درست نیست. ساعت گوشی را هم بررسی کنید.")
    uri = security.totp_uri(secret, u.username, settings.SITE_NAME)
    qr = segno.make(uri, error="m").svg_inline(scale=5, dark="#111", light="#fff", border=2)
    return render(request, "ledger/twofa_setup.html", {
        "nav": "more", "form": form, "secret": secret, "uri": uri, "qr": qr,
        "grouped": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
    })


def twofa_disable(request):
    u = request.user
    form = PasswordConfirmForm(u, request.POST or None)
    if request.method == "POST" and form.is_valid():
        if u.is_staff:
            messages.error(request, "برای مدیر، ورود دو مرحله‌ای اجباری است.")
            return redirect("settings")
        u.totp_secret, u.totp_last_step = "", 0
        u.save(update_fields=["totp_secret", "totp_last_step"])
        u.recovery_codes.all().delete()
        audit.record(u, "2fa_off", request)
        messages.success(request, "ورود دو مرحله‌ای خاموش شد.")
        return redirect("settings")
    return render(request, "ledger/confirm_password.html", {
        "nav": "more", "form": form, "title": "خاموش کردن ورود دو مرحله‌ای",
        "warning": "بعد از این، فقط با رمز عبور می‌شود وارد حساب شد.",
    })


def export_all(request):
    """Everything, decrypted, in one file: asks for the password first, like other risky actions."""
    u = request.user
    form = PasswordConfirmForm(u, request.POST or None)
    if request.method == "POST" and form.is_valid():
        audit.record(u, "export", request)
        return csv_response(Transaction.objects.filter(user=u).select_related("category", "account", "message"),
                            f"smsledger-{u.username}.csv")
    return render(request, "ledger/confirm_password.html", {
        "nav": "more", "form": form, "title": "دریافت خروجی همه داده‌ها",
        "warning": "فایل خروجی رمزنگاری‌شده نیست: همه تراکنش‌ها و متن پیامک‌ها را خوانا دارد. "
                   "آن را جای امن نگه دارید و بعد از استفاده پاکش کنید.",
        "button": "دریافت فایل",
    })


def delete_account(request):
    u = request.user
    form = PasswordConfirmForm(u, request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            User.objects.filter(pk=u.pk).delete()  # cascades to every row this user owns
        logout(request)
        return render(request, "ledger/deleted.html")
    return render(request, "ledger/confirm_password.html", {
        "nav": "more", "form": form, "title": "حذف همیشگی حساب",
        "warning": "همه تراکنش‌ها، پیامک‌ها، دسته‌ها، قانون‌ها و دستگاه‌های شما برای همیشه پاک می‌شود. "
                   "اگر نسخه‌ای می‌خواهید، اول از تنظیمات خروجی CSV بگیرید.",
        "danger": True,
    })
