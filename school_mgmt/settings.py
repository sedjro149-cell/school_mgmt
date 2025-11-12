# school_mgmt/settings.py — robuste et prêt pour prod/dev
import os
from pathlib import Path
from datetime import timedelta

# Optional imports — tolerant if missing during early dev
try:
    import dj_database_url
except Exception:
    dj_database_url = None

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

# Load local .env file if python-dotenv available
BASE_DIR = Path(__file__).resolve().parent.parent
if load_dotenv:
    load_dotenv(dotenv_path=os.path.join(BASE_DIR, ".env"))

# ---------------------------
# Security / Debug
# ---------------------------
# Support both DJANGO_SECRET_KEY and SECRET_KEY env names
SECRET_KEY = (
    os.environ.get("DJANGO_SECRET_KEY")
    or os.environ.get("SECRET_KEY")
    or "fallback-dev-secret-key-please-change"
)

# Robust boolean parsing for DEBUG
def bool_from_env(key, default=False):
    val = os.environ.get(key)
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")

DEBUG = bool_from_env("DEBUG", default=True)

# ALLOWED_HOSTS: supports ALLOWED_HOSTS or DJANGO_ALLOWED_HOSTS env var
_env_hosts = os.environ.get("ALLOWED_HOSTS") or os.environ.get("DJANGO_ALLOWED_HOSTS") or ""
if _env_hosts:
    ALLOWED_HOSTS = [h.strip() for h in _env_hosts.split(",") if h.strip()]
else:
    # safe defaults for dev
    ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
# You can append runtime host names if needed:
_extra_host = os.environ.get("EXTRA_ALLOWED_HOST")
if _extra_host:
    ALLOWED_HOSTS.append(_extra_host.strip())

# ---------------------------
# Installed apps
# ---------------------------
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
]

# ---------------------------
# Middleware
# ---------------------------
MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",               # should be high
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",         # static files in prod
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# ---------------------------
# CORS
# ---------------------------
# Accept several env formats: comma-separated OR space-separated
_env_cors = os.environ.get("CORS_ALLOWED_ORIGINS") or os.environ.get("CORS_ALLOWED_ORIGIN") or ""
CORS_ALLOWED_ORIGINS = []
if _env_cors:
    # split on comma or whitespace
    if "," in _env_cors:
        CORS_ALLOWED_ORIGINS = [u.strip() for u in _env_cors.split(",") if u.strip()]
    else:
        CORS_ALLOWED_ORIGINS = [u.strip() for u in _env_cors.split() if u.strip()]

# If no explicit origins and DEBUG True, allow all for dev convenience
CORS_ALLOW_ALL_ORIGINS = bool_from_env("CORS_ALLOW_ALL_ORIGINS", default=DEBUG and not CORS_ALLOWED_ORIGINS)
# Allow credentials if explicitly requested (rare for JWT)
CORS_ALLOW_CREDENTIALS = bool_from_env("CORS_ALLOW_CREDENTIALS", default=False)

# ---------------------------
# URL / Templates / WSGI
# ---------------------------
ROOT_URLCONF = "school_mgmt.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],  # add project-level templates if any
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

# ---------------------------
# Database — robust handling
# ---------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and dj_database_url:
    # ssl_require toggles automatically based on DEBUG or explicit env DB_SSL
    db_ssl_env = os.environ.get("DB_SSL", "")
    ssl_require = bool_from_env("DB_SSL", default=not DEBUG)
    DATABASES = {
        "default": dj_database_url.parse(DATABASE_URL, conn_max_age=int(os.environ.get("DB_CONN_MAX_AGE", 600)), ssl_require=ssl_require)
    }
else:
    # Try individual Postgres env vars
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
        # Final fallback to sqlite for maximum robustness (local dev convenience)
        DATABASES = {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": os.path.join(BASE_DIR, "db.sqlite3"),
            }
        }

# ---------------------------
# Password validation
# ---------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------
# Internationalization
# ---------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

# ---------------------------
# Static files (WhiteNoise)
# ---------------------------
STATIC_URL = os.environ.get("STATIC_URL", "/static/")
STATIC_ROOT = Path(os.environ.get("STATIC_ROOT", BASE_DIR / "staticfiles"))
# optional additional static dirs
_extra_static_dirs = os.environ.get("STATICFILES_DIRS", "")
if _extra_static_dirs:
    # comma separated list of relative paths from BASE_DIR
    STATICFILES_DIRS = [BASE_DIR / p.strip() for p in _extra_static_dirs.split(",") if p.strip()]
else:
    STATICFILES_DIRS = []

# Use WhiteNoise storage by default in production-like envs
STATICFILES_STORAGE = os.environ.get("STATICFILES_STORAGE", "whitenoise.storage.CompressedManifestStaticFilesStorage")

# ---------------------------
# Default auto field
# ---------------------------
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------
# Django REST framework + JWT
# ---------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=int(os.environ.get("JWT_ACCESS_HOURS", 100))),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=int(os.environ.get("JWT_REFRESH_DAYS", 7))),
    "AUTH_HEADER_TYPES": tuple(os.environ.get("JWT_AUTH_HEADER_TYPES", "Bearer").split(",")),
}

# ---------------------------
# Production security hardening
# ---------------------------
# Only enable strong security when DEBUG is False (or when explicitly requested)
if not DEBUG or bool_from_env("FORCE_SECURE", default=False):
    SECURE_SSL_REDIRECT = bool_from_env("SECURE_SSL_REDIRECT", default=True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

    # If cross-site cookies are required (rare), set to 'None' otherwise Lax
    if CORS_ALLOW_CREDENTIALS:
        SESSION_COOKIE_SAMESITE = os.environ.get("SESSION_COOKIE_SAMESITE", "None")
        CSRF_COOKIE_SAMESITE = os.environ.get("CSRF_COOKIE_SAMESITE", "None")
    else:
        SESSION_COOKIE_SAMESITE = os.environ.get("SESSION_COOKIE_SAMESITE", "Lax")
        CSRF_COOKIE_SAMESITE = os.environ.get("CSRF_COOKIE_SAMESITE", "Lax")

    SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", 3600))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = bool_from_env("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=True)
    SECURE_HSTS_PRELOAD = bool_from_env("SECURE_HSTS_PRELOAD", default=True)
else:
    SECURE_SSL_REDIRECT = False

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
X_FRAME_OPTIONS = os.environ.get("X_FRAME_OPTIONS", "DENY")

# ---------------------------
# Logging — console friendly for Railway logs
# ---------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {"format": "%(levelname)s %(asctime)s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "default"},
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
    },
}

# ---------------------------
# Helpful quick-health endpoint name (optional)
# ---------------------------
# If you expose a simple /health/ endpoint in your urls, it helps uptime checks
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "/health/")

# End of settings.py
