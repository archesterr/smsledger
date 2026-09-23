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


class SecurityHeadersMiddleware:
    """CSP and no-store on everything Django renders (static files are served before this by
    WhiteNoise, with their own long-lived cache headers)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault(
            "Content-Security-Policy", CSP_ADMIN if request.path.startswith("/admin/") else CSP
        )
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        # financial pages must not be kept by the browser cache or by any proxy
        response.headers.setdefault("Cache-Control", "no-store")
        return response
