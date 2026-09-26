"""Security events and signed-in sessions, for their owner to see (the security page).
Only a network prefix and a short device name are kept: enough to recognise "that's not me",
not a location history."""
from __future__ import annotations

import ipaddress
import re
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from . import security
from .models import SecurityEvent, UserSession

EVENT_DAYS = 180
TOUCH_EVERY = 60  # seconds between last_seen updates
FAILURES = ("login_failed", "login_blocked", "2fa_failed")


def ip_prefix(ip: str) -> str:
    """5.120.33.x for IPv4, the /48 for IPv6."""
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return ""
    if addr.version == 4:
        return ".".join(str(addr).split(".")[:3]) + ".x"
    return str(ipaddress.ip_network(f"{addr}/48", strict=False).network_address) + "/48"


_DEVICES = [("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Macintosh", "Mac"),
            ("Windows", "Windows"), ("Linux", "Linux")]
_BROWSERS = [(r"Edg/", "Edge"), (r"(?:CriOS|Chrome)/", "Chrome"), (r"(?:FxiOS|Firefox)/", "Firefox"),
             (r"Safari/", "Safari")]


def agent(request) -> str:
    ua = request.headers.get("User-Agent", "")
    device = next((name for key, name in _DEVICES if key in ua), "")
    browser = next((name for pat, name in _BROWSERS if re.search(pat, ua)), "")
    if device in ("iPhone", "iPad") and not browser:
        browser = "برنامه نصب‌شده"  # Home Screen web app: no "Safari/" in its UA
    return " · ".join(p for p in (device, browser) if p) or "نامشخص"


def record(user, kind: str, request=None) -> SecurityEvent:
    ev = SecurityEvent.objects.create(
        user=user, kind=kind,
        ip=ip_prefix(security.client_ip(request)) if request else "",
        agent=agent(request) if request else "",
    )
    if ev.pk % 50 == 0:  # occasional cleanup instead of a cron job
        SecurityEvent.objects.filter(created_at__lt=timezone.now() - timedelta(days=EVENT_DAYS)).delete()
    return ev


# ---- sessions --------------------------------------------------------------------------
SID = "sid"


def start_session(request, user) -> UserSession:
    s = UserSession.objects.create(user=user, ip=ip_prefix(security.client_ip(request)), agent=agent(request))
    request.session[SID] = s.pk
    return s


def current(request) -> UserSession | None:
    """This browser's session row; None if it was signed out from elsewhere."""
    sid = request.session.get(SID)
    if sid is None:  # signed in before sessions were tracked
        return start_session(request, request.user)
    s = UserSession.objects.filter(pk=sid, user=request.user).first()
    if s is None or s.ended_at is not None:
        return None
    now = timezone.now()
    if (now - s.last_seen).total_seconds() > TOUCH_EVERY:
        ip = ip_prefix(security.client_ip(request))
        UserSession.objects.filter(pk=s.pk).update(last_seen=now, ip=ip)
        s.last_seen, s.ip = now, ip
    return s


def active(user):
    since = timezone.now() - timedelta(seconds=settings.SESSION_COOKIE_AGE)
    return UserSession.objects.filter(user=user, ended_at__isnull=True, last_seen__gte=since)


def end(user, exclude_pk=None, pk=None) -> int:
    qs = UserSession.objects.filter(user=user, ended_at__isnull=True)
    if pk is not None:
        qs = qs.filter(pk=pk)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.update(ended_at=timezone.now())


def failures_since(user, since) -> int:
    qs = SecurityEvent.objects.filter(user=user, kind__in=FAILURES)
    return qs.filter(created_at__gt=since).count() if since else 0
