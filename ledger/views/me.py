import segno
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout, update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .. import security
from ..forms import CodeForm, DeviceForm, PasswordConfirmForm, UnitPrefForm
from ..models import Device, RecoveryCode, Transaction, User
from .app import csv_response


# ---- setup: devices + iPhone instructions -----------------------------------------------
def setup(request):
    u = request.user
    new_token = None
    form = DeviceForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        token = security.new_token()
        Device.objects.create(user=u, name=form.cleaned_data["name"], token_hash=security.sha256(token),
                              token_prefix=token[:8])
        new_token = token  # shown once, in this response only; the DB keeps just the hash
        form = DeviceForm()
    devices = u.devices.filter(revoked_at__isnull=True).order_by("-created_at")
    return render(request, "ledger/setup.html", {
        "nav": "more", "form": form, "new_token": new_token, "devices": devices,
        "ingest_url": request.build_absolute_uri("/ingest"),
        "shortcut_url": settings.SHORTCUT_URL,
        "connected": devices.filter(last_used_at__isnull=False).exists(),
        "has_sms": u.messages.exists(),
    })


@require_POST
def device_revoke(request, pk):
    d = get_object_or_404(Device, pk=pk, user=request.user, revoked_at__isnull=True)
    d.revoked_at = timezone.now()
    d.save(update_fields=["revoked_at"])
    messages.success(request, f"دسترسی «{d.name}» قطع شد.")
    return redirect("setup")


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
            pw_form.save()
            update_session_auth_hash(request, pw_form.user)  # other sessions are logged out
            messages.success(request, "رمز عبور عوض شد.")
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
        messages.success(request, "ورود دو مرحله‌ای خاموش شد.")
        return redirect("settings")
    return render(request, "ledger/confirm_password.html", {
        "nav": "more", "form": form, "title": "خاموش کردن ورود دو مرحله‌ای",
        "warning": "بعد از این، فقط با رمز عبور می‌شود وارد حساب شد.",
    })


def export_all(request):
    return csv_response(Transaction.objects.filter(user=request.user).select_related("category", "account",
                                                                                     "message"),
                        f"smsledger-{request.user.username}.csv")


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
