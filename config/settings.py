"""Django settings. Everything that differs between deployments comes from the environment
(see deploy/.env.example)."""
import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


DEBUG = env_bool("DEBUG")
SECRET_KEY = env("SECRET_KEY")
if len(SECRET_KEY) < 50:  # Django's own deploy check (security.W009) wants >= 50
    if not (DEBUG or TESTING):
        raise ImproperlyConfigured(
            'SECRET_KEY must be set (>= 50 chars): python3 -c "import secrets; print(secrets.token_urlsafe(48))"')
    SECRET_KEY = "insecure-dev-only-key-" + "0123456789abcdefghijklmnopqrstuvwxyz"

DOMAIN = env("DOMAIN", "localhost")
SITE_NAME = env("SITE_NAME", "دخل و خرج")
# "app" is the compose service name (internal healthchecks / metrics scraping).
ALLOWED_HOSTS = [DOMAIN, "localhost", "127.0.0.1", "app"] + [h for h in env("EXTRA_HOSTS").split(",") if h]
CSRF_TRUSTED_ORIGINS = [f"https://{DOMAIN}"]

# Optional iCloud link to a ready-made shortcut (with import questions for URL + token).
SHORTCUT_URL = env("SHORTCUT_URL")
# /metrics is disabled unless a token is set; scrape it with "Authorization: Bearer <token>".
METRICS_TOKEN = env("METRICS_TOKEN")
# Behind Caddy: it overwrites X-Forwarded-For, so the last entry is the real client.
TRUST_X_FORWARDED_FOR = env_bool("TRUST_X_FORWARDED_FOR", True)
INVITE_DAYS = int(env("INVITE_DAYS", "7"))
SILENT_DAYS = int(env("SILENT_DAYS", "3"))

INSTALLED_APPS = [
    "config.admin.LedgerAdminConfig",  # django.contrib.admin with a 2FA-only site
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "ledger",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # every view needs a login unless it is decorated with @login_not_required
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "ledger.middleware.SecurityHeadersMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "ledger.context.globals",
            ],
        },
    },
]

if env("POSTGRES_HOST"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "HOST": env("POSTGRES_HOST"),
            "PORT": env("POSTGRES_PORT", "5432"),
            "NAME": env("POSTGRES_DB", "smsledger"),
            "USER": env("POSTGRES_USER", "smsledger"),
            "PASSWORD": env("POSTGRES_PASSWORD"),
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
        }
    }
else:  # local development and single-container self-hosting
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": env("DB_PATH") or BASE_DIR / "dev.sqlite3",
        }
    }
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Shared between gunicorn workers (rate limits live here), no Redis needed.
CACHES = {"default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "django_cache"}}
if TESTING:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

AUTH_USER_MODEL = "ledger.User"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
if TESTING:  # hashing speed only; production always uses Argon2
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "fa"
TIME_ZONE = "Asia/Tehran"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        if (DEBUG or TESTING)
        else "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

# ---- request limits --------------------------------------------------------
DATA_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024  # a year of queued SMS is ~200 KB
DATA_UPLOAD_MAX_NUMBER_FIELDS = 400

# ---- cookies / transport security (Caddy terminates TLS) -------------------
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", not (DEBUG or TESTING))
SECURE_REDIRECT_EXEMPT = [r"^healthz$", r"^metrics$"]
SECURE_HSTS_SECONDS = 0 if DEBUG else 31536000
# The app lives on its own host (tx.example.ir), so this only covers *.tx.example.ir.
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
# Preload lists are for registrable domains served at the apex; not applicable to a subdomain app.
SILENCED_SYSTEM_CHECKS = ["security.W021"]
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_AGE = 14 * 24 * 3600
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = True
if not DEBUG:
    SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = True
    # __Host- prefix: browser refuses the cookie unless Secure, Path=/ and no Domain
    SESSION_COOKIE_NAME = "__Host-session"
    CSRF_COOKIE_NAME = "__Host-csrftoken"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
    # never log SMS bodies, amounts or tokens: the ledger code only logs counts and ids
}
