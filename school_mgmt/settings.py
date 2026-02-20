import os
import warnings
from pathlib import Path
from datetime import timedelta
from corsheaders.defaults import default_headers

# Optional imports
try:
    import dj_database_url
except Exception:
    dj_database_url = None

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

from django.core.exceptions import ImproperlyConfigured

# -------------------------------------------------
# Base
# -------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent

if load_dotenv:
    load_dotenv(dotenv_path=os.path.join(BASE_DIR, ".env"))

# -------------------------------------------------
# Security / Debug
# -------------------------------------------------
SECRET_KEY = (
    os.environ.get("DJANGO_SECRET_KEY")
    or os.environ.get("SECRET_KEY")
    or "fallback-dev-secret-key-please-change"
)

def bool_from_env(key, default=False):
    val = os.environ.get(key)
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")

# Default to False in prod-ready settings
DEBUG = bool_from_env("DEBUG", default=True)

# In production ensure a proper SECRET_KEY is provided
if not DEBUG:
    if not os.environ.get("DJANGO_SECRET_KEY") and not os.environ.get("SECRET_KEY"):
        raise ImproperlyConfigured("DJANGO_SECRET_KEY (or SECRET_KEY) environment variable is required in production.")

# -------------------------------------------------
# Allowed hosts
# -------------------------------------------------
_env_hosts = os.environ.get("ALLOWED_HOSTS") or os.environ.get("DJANGO_ALLOWED_HOSTS") or ""
if _env_hosts:
    if "," in _env_hosts:
        ALLOWED_HOSTS = [h.strip() for h in _env_hosts.split(",") if h.strip()]
    else:
        ALLOWED_HOSTS = [h.strip() for h in _env_hosts.split() if h.strip()]
else:
    # Minimal safe defaults for local dev + common hosting patterns (you should override with env in prod)
    ALLOWED_HOSTS = [
        "localhost",
        "127.0.0.1",
        "schoolmgmt-production.up.railway.app",
        ".railway.app",
        ".onrender.com",
    ]

_extra_host = os.environ.get("EXTRA_ALLOWED_HOST")
if _extra_host:
    ALLOWED_HOSTS.append(_extra_host.strip())

# -------------------------------------------------
# Installed apps
# -------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    # Third party
    "corsheaders",
    "django_filters",
    "rest_framework",

    # Project apps
    "core",
    "academics",
    "fees",
    "notifications",
]

# -------------------------------------------------
# Middleware
# -------------------------------------------------
MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",  # Must be at the top
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# -------------------------------------------------
# CORS / CSRF
# -------------------------------------------------
# Keep strict defaults; explicitly set allowed origins in env in prod
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOW_CREDENTIALS = True

CORS_ALLOWED_ORIGINS = [
    "http://localhost:5173", "https://scol360.netlify.app",
]

# Allow common headers; removed ngrok-specific headers
CORS_ALLOW_HEADERS = list(default_headers)

# Build CSRF_TRUSTED_ORIGINS from ALLOWED_HOSTS (https)
_csrf_from_env = os.environ.get("CSRF_TRUSTED_ORIGINS")
if _csrf_from_env:
    CSRF_TRUSTED_ORIGINS = [s.strip() for s in _csrf_from_env.split(",") if s.strip()]
else:
    CSRF_TRUSTED_ORIGINS = []
    for h in ALLOWED_HOSTS:
        # skip empty or wildcard entries
        if not h or h.startswith("."):
            continue
        # if host already contains scheme, keep as-is
        if h.startswith("http://") or h.startswith("https://"):
            CSRF_TRUSTED_ORIGINS.append(h)
        else:
            CSRF_TRUSTED_ORIGINS.append(f"https://{h}")

# -------------------------------------------------
# URLs / Templates / WSGI
# -------------------------------------------------
ROOT_URLCONF = "school_mgmt.urls"

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
            ],
        },
    },
]

WSGI_APPLICATION = "school_mgmt.wsgi.application"

# -------------------------------------------------
# Database
# -------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL and dj_database_url:
    ssl_require = bool_from_env("DB_SSL", default=(not DEBUG))
    conn_max_age = int(os.environ.get("DB_CONN_MAX_AGE", 600))
    DATABASES = {
        "default": dj_database_url.parse(
            DATABASE_URL,
            conn_max_age=conn_max_age,
            ssl_require=ssl_require,
        )
    }
else:
    DB_NAME = os.environ.get("DB_NAME")
    DB_USER = os.environ.get("DB_USER")
    DB_PASS = os.environ.get("DB_PASSWORD") or os.environ.get("DB_PASS")
    DB_HOST = os.environ.get("DB_HOST", "localhost")
    DB_PORT = os.environ.get("DB_PORT", "5432")

    if DB_NAME and DB_USER:
        DATABASES = {
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": DB_NAME,
                "USER": DB_USER,
                "PASSWORD": DB_PASS or "",
                "HOST": DB_HOST,
                "PORT": DB_PORT,
            }
        }
    else:
        DATABASES = {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": BASE_DIR / "db.sqlite3",
            }
        }

# -------------------------------------------------
# Internationalization
# -------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

# -------------------------------------------------
# Static / Media
# -------------------------------------------------
STATIC_URL = os.environ.get("STATIC_URL", "/static/")
STATIC_ROOT = Path(os.environ.get("STATIC_ROOT", BASE_DIR / "staticfiles"))
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

MEDIA_URL = os.environ.get("MEDIA_URL", "/media/")
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", BASE_DIR / "media"))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# -------------------------------------------------
# DRF + JWT
# -------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),

    # Pagination par défaut (25 par page, modifiable)
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,

    # Backends de filtrage (recherche côté serveur)
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
}
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=int(os.environ.get("JWT_ACCESS_HOURS", 100))),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=int(os.environ.get("JWT_REFRESH_DAYS", 7))),
    "AUTH_HEADER_TYPES": ("Bearer",),
}

# -------------------------------------------------
# Production security
# -------------------------------------------------
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

FORCE_SECURE = bool_from_env("FORCE_SECURE", default=False)

if not DEBUG or FORCE_SECURE:
    SECURE_SSL_REDIRECT = bool_from_env("SECURE_SSL_REDIRECT", default=True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", 31536000))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = bool_from_env("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=True)
    SECURE_HSTS_PRELOAD = bool_from_env("SECURE_HSTS_PRELOAD", default=True)
    SECURE_BROWSER_XSS_FILTER = True
    X_FRAME_OPTIONS = "DENY"
else:
    # dev-friendly defaults
    SECURE_SSL_REDIRECT = False