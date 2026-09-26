import hmac
import json
from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.core.exceptions import RequestDataTooBig
from django.db import connection
from django.db.models import Max
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .. import audit, ingest, security
from ..models import Device, Message, Transaction, User


def device_from_request(request) -> Device | None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:].strip()
    if not token.startswith(security.TOKEN_PREFIX) or len(token) > 100:
        return None
    return (Device.objects.select_related("user")
            .filter(token_hash=security.sha256(token), revoked_at__isnull=True, user__is_active=True).first())


# shown on the phone by the Shortcut's Connect step ("message" from the reply)
MESSAGES = {
    401: "❌ این کلید معتبر نیست یا باطل شده.\nدر صفحه «راه‌اندازی آیفون» دوباره «اتصال این آیفون» را بزنید.",
    429: "⏳ درخواست‌ها زیاد بود؛ چند دقیقه بعد دوباره امتحان کنید.",
}


def _error(msg: str, status: int) -> JsonResponse:
    # Error bodies never contain a "status" key: the Shortcut empties its queue only when it sees one.
    body = {"error": msg}
    if status in MESSAGES:
        body["message"] = MESSAGES[status]
    return JsonResponse(body, status=status, json_dumps_params={"ensure_ascii": False})


def _touch(device: Device, ip: str) -> None:
    Device.objects.filter(pk=device.pk).update(last_used_at=timezone.now(), last_ip=audit.ip_prefix(ip))


@csrf_exempt
@login_not_required
@require_POST
def ingest_view(request):
    """POST /ingest   Authorization: Bearer sml_...
    Body: JSON {"sms": "..." | [...], "source": "..."}, or text/plain (?split=1 splits on '---' lines).
    ?source=connect is the Shortcut's hello after the setup page's Connect button: no body.
    The device token can only add SMS. It can't read anything back."""
    ip = security.client_ip(request)
    if security.INGEST_BAD.blocked(ip):
        return _error("too many attempts", 429)
    device = device_from_request(request)
    if device is None:
        security.INGEST_BAD.hit(ip)
        return _error("unauthorized", 401)
    if security.INGEST_DEVICE.hit(device.pk) > security.INGEST_DEVICE.limit:
        return _error("rate limited", 429)
    source = request.GET.get("source", "shortcut")
    if source == "connect":
        # the setup page's Connect button ran the Shortcut, which just saved this key
        _touch(device, ip)
        return JsonResponse({"status": "connected", "message": (
            f"✅ این آیفون به حساب «{device.user.username}» وصل شد.\n"
            "به صفحه راه‌اندازی برگردید و اتوماسیون پیامک را بسازید.")},
            json_dumps_params={"ensure_ascii": False})
    try:
        body = request.body.decode("utf-8", "replace")
    except RequestDataTooBig:
        return _error("body too large", 413)

    if "json" in request.content_type:
        try:
            data = json.loads(body or "{}")
        except json.JSONDecodeError:
            return _error("bad json", 400)
        if not isinstance(data, dict):
            return _error("bad json", 400)
        sms = data.get("sms")
        source = str(data.get("source") or source)
        items = sms if isinstance(sms, list) else [sms]
    elif request.GET.get("split") == "1":
        items = ingest.split_batch(body)
    else:
        items = [body]
    texts = [x if isinstance(x, str) else "" for x in items]

    results = ingest.ingest(device.user, texts, source, device)
    _touch(device, ip)
    if len(results) == 1:
        return JsonResponse(results[0], json_dumps_params={"ensure_ascii": False})
    counts = Counter(r["status"] for r in results)
    return JsonResponse({"status": "ok", "count": dict(counts), "results": results},
                        json_dumps_params={"ensure_ascii": False})


@login_not_required
@require_GET
def healthz(request):
    try:
        connection.ensure_connection()
    except Exception:
        return HttpResponse("db down", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")


@login_not_required
@require_GET
def metrics(request):
    """Prometheus text format. Disabled unless METRICS_TOKEN is set. No per-user labels:
    only counts, so the monitoring stack never holds anyone's financial data."""
    token = settings.METRICS_TOKEN
    if not token:
        raise Http404
    if not hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {token}"):
        return HttpResponse(status=401)
    now = timezone.now()
    by_status = dict(Counter(Message.objects.values_list("status", flat=True)))
    last = Message.objects.aggregate(m=Max("received_at"))["m"]
    silent_before = now - timedelta(days=settings.SILENT_DAYS)
    silent = (User.objects.filter(is_active=True, devices__revoked_at__isnull=True)
              .annotate(last=Max("messages__received_at"))
              .filter(last__lt=silent_before).distinct().count())
    lines = [
        "# HELP smsledger_users Active users.",
        "# TYPE smsledger_users gauge",
        f"smsledger_users {User.objects.filter(is_active=True).count()}",
        "# HELP smsledger_devices Devices with a live token.",
        "# TYPE smsledger_devices gauge",
        f"smsledger_devices {Device.objects.filter(revoked_at__isnull=True).count()}",
        "# HELP smsledger_messages Stored SMS by parse status.",
        "# TYPE smsledger_messages gauge",
        *(f'smsledger_messages{{status="{s}"}} {by_status.get(s, 0)}'
          for s in (Message.PARSED, Message.UNPARSED, Message.IGNORED)),
        "# HELP smsledger_balance_gaps Transactions flagged as having a missed SMS before them.",
        "# TYPE smsledger_balance_gaps gauge",
        f"smsledger_balance_gaps {Transaction.objects.filter(has_gap=True).count()}",
        f"# HELP smsledger_users_silent Users with a device but no SMS for {settings.SILENT_DAYS}+ days.",
        "# TYPE smsledger_users_silent gauge",
        f"smsledger_users_silent {silent}",
    ]
    if last:
        lines += ["# HELP smsledger_last_ingest_timestamp_seconds Newest stored SMS.",
                  "# TYPE smsledger_last_ingest_timestamp_seconds gauge",
                  f"smsledger_last_ingest_timestamp_seconds {last.timestamp():.0f}"]
    return HttpResponse("\n".join(lines) + "\n", content_type="text/plain; version=0.0.4")


@login_not_required
@require_GET
def manifest(request):
    icons = [
        {"src": static("ledger/icons/icon-192.png"), "sizes": "192x192", "type": "image/png"},
        {"src": static("ledger/icons/icon-512.png"), "sizes": "512x512", "type": "image/png"},
        {"src": static("ledger/icons/icon-maskable-512.png"), "sizes": "512x512", "type": "image/png",
         "purpose": "maskable"},
    ]
    data = {
        "name": settings.SITE_NAME, "short_name": settings.SITE_NAME, "lang": "fa", "dir": "rtl",
        "start_url": "/", "scope": "/", "display": "standalone",
        "background_color": "#f6f7f9", "theme_color": "#0f766e", "icons": icons,
    }
    resp = JsonResponse(data, json_dumps_params={"ensure_ascii": False})
    resp["Content-Type"] = "application/manifest+json"
    resp["Cache-Control"] = "no-cache"
    return resp


@login_not_required
@require_GET
def service_worker(request):
    assets = [static("ledger/app.css"), static("ledger/banks.css"), static("ledger/app.js"),
              static("ledger/fonts/Vazirmatn-wght.woff2"), static("ledger/icons/icon.svg")]
    # hashed static names change with their content, so this changes on every asset update
    version = security.sha256("|".join(assets))[:12]
    resp = render(request, "ledger/sw.js", {"assets": assets, "version": version},
                  content_type="application/javascript")
    resp["Cache-Control"] = "no-cache"
    return resp


@login_not_required
@require_GET
def offline(request):
    return render(request, "ledger/offline.html")
