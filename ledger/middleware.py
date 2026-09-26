from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.urls import reverse

from . import audit, ingest, money, parsers, vault
from .models import Message, User

CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "manifest-src 'self'",
    "worker-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])
# Django's admin still uses a few inline style attributes.
CSP_ADMIN = CSP.replace("style-src 'self'", "style-src 'self' 'unsafe-inline'")
# The import page reads an iPhone backup in the browser with SQLite compiled to WebAssembly.
CSP_WASM = CSP.replace("script-src 'self'", "script-src 'self' 'wasm-unsafe-eval'")


class SecurityHeadersMiddleware:
    """CSP and no-store on everything Django renders (static files are served before this by
    WhiteNoise, with their own long-lived cache headers)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        csp = CSP_ADMIN if request.path.startswith("/admin/") else CSP_WASM if request.path == "/import/" else CSP
        response.headers.setdefault("Content-Security-Policy", csp)
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        # no other site can open this one in a window it controls, or embed its responses
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        # financial pages must not be kept by the browser cache or by any proxy
        response.headers.setdefault("Cache-Control", "no-store")
        return response


# Machine endpoints: no browser session involved.
NO_SESSION = ("/healthz", "/metrics", "/ingest")
# Pages a signed-in user can reach while their data is locked or before the key is set up.
VAULT_FREE = ("/logout/", "/unlock/", "/security/recovery-key/", "/sw.js", "/manifest.webmanifest", "/offline/")


class VaultMiddleware:
    """Per request: an empty keyring; for a signed-in user, their data key from the session
    (vault.session_key) so their rows can be decrypted, plus session tracking. Also where SMS
    that arrived while they were away get recorded, and old unparsed ones re-read."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = vault.fresh_keyring()
        try:
            response = self.handle(request)
        finally:
            vault.reset_keyring(token)
        if getattr(request, "_vault_cookie", None):
            response.set_cookie(vault.cookie_name(), request._vault_cookie, max_age=settings.SESSION_COOKIE_AGE,
                                secure=not settings.DEBUG, httponly=True, samesite="Lax")
        elif getattr(request, "_vault_clear", False):
            response.delete_cookie(vault.cookie_name(), samesite="Lax")
        return response

    def signout(self, request, text: str):
        sid = request.session.get(audit.SID)
        if sid:
            audit.end(request.user, pk=sid)
        logout(request)
        request._vault_clear = True
        messages.info(request, text)
        return redirect(f"{reverse('login')}?next={request.get_full_path()}" if request.method == "GET"
                        else reverse("login"))

    def handle(self, request):
        user = request.user
        if not user.is_authenticated or request.path.startswith(NO_SESSION):
            return self.get_response(request)
        state = user.vault_state
        if state == vault.UNPROTECTED:
            # a session from before encryption: signing in again is what moves the key under the password
            return self.signout(request, "داده‌های شما حالا رمزنگاری می‌شود. برای فعال شدنش یک بار دوباره وارد شوید.")
        if audit.current(request) is None:
            return self.signout(request, "این نشست از دستگاه دیگری بسته شد. دوباره وارد شوید.")
        free = request.path.startswith(VAULT_FREE)
        if state == vault.PROTECTED:
            dek = vault.session_key(request, user)
            if dek is None:
                return self.signout(request, "نشست شما تمام شده. دوباره وارد شوید.")
            vault.keyring()[user.pk] = dek
            if not free:
                self.catch_up(request, user)
                if user.recovery_saved_at is None:
                    return redirect("recovery_key")
        elif not free:  # LOCKED (or no keys): only the unlock page helps
            return redirect("unlock")
        return self.get_response(request)

    @staticmethod
    def catch_up(request, user):
        pending = user.messages.filter(status=Message.PENDING)
        if pending.exists():
            ingest.process_pending(user, budget=ingest.PENDING_BUDGET)
            left = pending.count()
            if left and request.method == "GET":  # a whole backup: the next page load carries on
                messages.info(request, f"ثبت پیامک‌های قدیمی ادامه دارد: {money.fa_digits(left)} پیامک مانده؛ "
                                       "صفحه را تازه کنید.")
        if user.parsers_version != parsers.VERSION:
            ingest.reparse(user)
            User.objects.filter(pk=user.pk).update(parsers_version=parsers.VERSION)
            user.parsers_version = parsers.VERSION

    def process_exception(self, request, exception):
        if isinstance(exception, vault.Locked) and getattr(request, "user", None) and request.user.is_authenticated:
            return self.signout(request, "برای دیدن داده‌ها دوباره وارد شوید.")
        return None
