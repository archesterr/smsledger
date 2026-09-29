"""The Android app (android/ in this repo). It reads bank SMS itself, so there is nothing to build
on the phone: install the APK, tap Connect on the setup page, allow SMS. Unlike the iPhone, it can
also send the SMS that are already in the inbox, from the app, with no computer."""
from urllib.parse import quote

PACKAGE = "app.smsledger"
APP_NAME = "دخل و خرج"  # android/app/src/main/res/values/strings.xml
AGENT = "smsledger-android/"  # the app's User-Agent


def is_android(request) -> bool:
    return "Android" in request.headers.get("User-Agent", "")


def is_app(request) -> bool:
    return request.headers.get("User-Agent", "").startswith(AGENT)


def connect_url(server: str, token: str, fallback: str) -> str:
    """Chrome opens the app with the key (smsledger://connect?...); without the app installed it
    goes to fallback instead of the Play Store, where the app isn't."""
    query = f"server={quote(server, safe='')}&key={quote(token, safe='')}"
    return (f"intent://connect?{query}#Intent;scheme=smsledger;package={PACKAGE};"
            f"S.browser_fallback_url={quote(fallback, safe='')};end")
