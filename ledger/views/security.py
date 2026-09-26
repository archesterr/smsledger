"""The owner's security page (how protected the account is, where it's signed in, what happened),
the recovery key, and getting back into data locked by a password reset."""
from datetime import datetime, timedelta

from django import forms
from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .. import audit, defaults, security, vault
from ..forms import PasswordConfirmForm
from ..models import Account, Budget, Category, Message, Rule, SecurityEvent, Transaction, UserSession

STALE_DEVICE_DAYS = 30
UNLOCK_USER = security.Limiter("unlock-user", 10, 900)


def security_home(request):
    u = request.user
    now = timezone.now()
    sid = request.session.get(audit.SID)
    sessions = list(audit.active(u))
    devices = list(u.devices.filter(revoked_at__isnull=True).order_by("-last_used_at", "-created_at"))
    for d in devices:
        d.stale = not d.last_used_at or d.last_used_at < now - timedelta(days=STALE_DEVICE_DAYS)
    failures = SecurityEvent.objects.filter(user=u, kind__in=audit.FAILURES,
                                            created_at__gte=now - timedelta(days=30)).count()
    codes_left = u.recovery_codes.filter(used_at__isnull=True).count()
    stale = sum(d.stale for d in devices)
    checks = [
        {"ok": u.vault_state == vault.PROTECTED, "title": "رمزنگاری داده‌ها",
         "text": "روشن: مبلغ‌ها، مانده‌ها و متن پیامک‌ها با کلیدی ذخیره می‌شوند که فقط رمز شما بازش می‌کند."},
        {"ok": bool(u.recovery_saved_at), "title": "کلید بازیابی",
         "text": "ذخیره شده" if u.recovery_saved_at
         else "هنوز ذخیره نشده: بدون آن، فراموشی رمز یعنی از دست رفتن داده‌ها.",
         "url": "recovery_key"},
        {"ok": u.has_2fa, "title": "ورود دو مرحله‌ای",
         "text": f"روشن · {codes_left} کد پشتیبان باقی‌مانده" if u.has_2fa
         else "خاموش: با دانستن رمز، هر کسی می‌تواند وارد شود.",
         "url": None if u.has_2fa else "twofa_setup"},
        {"ok": failures == 0, "title": "تلاش ناموفق برای ورود (۳۰ روز)",
         "text": "هیچ" if not failures else f"{failures} بار: اگر شما نبودید، رمز را عوض کنید."},
        {"ok": stale == 0, "title": "کلیدهای آیفون",
         "text": f"{len(devices)} کلید فعال" + (f" · {stale} تا بیش از {STALE_DEVICE_DAYS} روز استفاده نشده؛ "
                                                "اگر لازم نیست باطلش کنید." if stale else "")},
    ]
    return render(request, "ledger/security.html", {
        "nav": "more", "checks": checks, "score": sum(c["ok"] for c in checks), "total": len(checks),
        "sessions": sessions, "sid": sid, "devices": devices,
        "events": SecurityEvent.objects.filter(user=u)[:40],
        "password_age": (now - u.password_changed_at).days if u.password_changed_at else None,
        "stale_days": STALE_DEVICE_DAYS,
    })


@require_POST
def session_revoke(request, pk):
    u = request.user
    if pk == request.session.get(audit.SID):
        messages.error(request, "برای این دستگاه از دکمه «خروج» استفاده کنید.")
    else:
        get_object_or_404(UserSession, pk=pk, user=u, ended_at__isnull=True)
        audit.end(u, pk=pk)
        audit.record(u, "session_revoked", request)
        messages.success(request, "آن دستگاه از حساب خارج شد.")
    return redirect("security")


@require_POST
def sessions_revoke_others(request):
    n = audit.end(request.user, exclude_pk=request.session.get(audit.SID))
    audit.record(request.user, "sessions_revoked", request)
    messages.success(request, f"{n} دستگاه دیگر از حساب خارج شد." if n else "دستگاه دیگری وارد نبود.")
    return redirect("security")


# ---- recovery key -------------------------------------------------------------------------------
def recovery_key(request):
    """Created right after sign-up / the first encrypted login (the middleware sends people here
    until they confirm they saved it). Shown once, in this response only; the server keeps just
    the data key wrapped by it. Replacing a confirmed key asks for the password."""
    u = request.user
    if u.vault_state != vault.PROTECTED:
        return redirect("home")
    need_pw = bool(u.recovery_saved_at)
    form = PasswordConfirmForm(u, request.POST or None) if need_pw else None
    action = request.POST.get("action") if request.method == "POST" else None
    if action == "create" and (form is None or form.is_valid()):
        code = vault.new_recovery_key()
        u.key_rec = vault.wrap_recovery(vault.key_for(u.pk), code)
        u.recovery_saved_at = None
        u.save(update_fields=["key_rec", "recovery_saved_at"])
        audit.record(u, "recovery_key", request)
        return render(request, "ledger/recovery_show.html", {"nav": "more", "code": code})
    if action == "saved" and vault._b(u.key_rec) and request.POST.get("confirm"):
        u.recovery_saved_at = timezone.now()
        u.save(update_fields=["recovery_saved_at"])
        messages.success(request, "کلید بازیابی ثبت شد. آن را جای امنی، جدا از رمز عبور، نگه دارید.")
        return redirect("security" if need_pw else "setup" if not u.devices.exists() else "home")
    return render(request, "ledger/recovery_key.html", {"nav": "more", "form": form, "need_pw": need_pw,
                                                        "missing": not u.recovery_saved_at})


# ---- locked data (password was reset outside the app) ------------------------------------------
class RecoverForm(PasswordConfirmForm):
    code = forms.CharField(label="کلید بازیابی", max_length=80, widget=forms.TextInput(attrs={
        "dir": "ltr", "autocomplete": "off", "autocapitalize": "characters", "spellcheck": "false",
        "placeholder": "XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX"}))
    field_order = ["code", "password"]


class ResetForm(PasswordConfirmForm):
    confirm = forms.BooleanField(label="می‌دانم همه تراکنش‌ها، پیامک‌ها، دسته‌ها و قانون‌هایم پاک می‌شود")


def wipe_and_restart(request, u, password: str) -> None:
    with transaction.atomic():
        Transaction.objects.filter(user=u).delete()
        for model in (Message, Rule, Budget, Category, Account):
            model.objects.filter(user=u).delete()
        dek = vault.new_keys(u)
        vault.protect(u, dek, password)
        u.recovery_saved_at = None
        u.save()
        vault.start_session(request, u, dek)
        defaults.setup_defaults(u)


def unlock(request):
    u = request.user
    if u.vault_state not in (vault.LOCKED, vault.NONE):
        return redirect("home")
    action = request.POST.get("action") if request.method == "POST" else None
    rec = RecoverForm(u, request.POST if action == "recover" else None)
    reset = ResetForm(u, request.POST if action == "reset" else None)
    if action == "recover" and rec.is_valid():
        if UNLOCK_USER.hit(u.pk) > UNLOCK_USER.limit:
            rec.add_error("code", "تلاش‌های زیادی شد. ۱۵ دقیقه دیگر دوباره امتحان کنید.")
        else:
            try:
                dek = vault.unwrap_recovery(u.key_rec, rec.cleaned_data["code"])
            except vault.BadKey:
                rec.add_error("code", "این کلید بازیابی درست نیست.")
            else:
                vault.protect(u, dek, rec.cleaned_data["password"])
                u.recovery_saved_at = None  # it was typed in: make a fresh one
                u.save(update_fields=["key_pw", "key_srv", "recovery_saved_at"])
                vault.start_session(request, u, dek)
                audit.record(u, "vault_recovered", request)
                messages.success(request, "داده‌ها باز شد. حالا یک کلید بازیابی تازه بسازید.")
                return redirect("recovery_key")
    if action == "reset" and reset.is_valid():
        wipe_and_restart(request, u, reset.cleaned_data["password"])
        audit.record(u, "vault_reset", request)
        messages.success(request, "حساب از نو شروع شد. کلید آیفون‌ها همچنان کار می‌کند.")
        return redirect("recovery_key")
    reset_at = SecurityEvent.objects.filter(user=u, kind="password_reset").values_list("created_at", flat=True).first()
    return render(request, "ledger/unlock.html", {"nav": "more", "rec": rec, "reset": reset,
                                                  "reset_at": reset_at if isinstance(reset_at, datetime) else None})
