import os
from pathlib import Path
from datetime import timedelta
import dj_database_url
from dotenv import load_dotenv

# Charger .env localement (optionnel)
load_dotenv(dotenv_path=os.path.join(Path(__file__).resolve().parent.parent, ".env"))

# ---------------------------
# Base directory
# ---------------------------
BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------------------
# Security (env vars)
# ---------------------------
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'fallback-key-pour-le-dev')

DEBUG = os.environ.get("DEBUG", "True") == "True"

# ALLOWED_HOSTS from env (comma separated) or default local
ALLOWED_HOSTS = [h for h in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]

# ---------------------------
# Installed apps
# ---------------------------
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # third party
    'corsheaders',
    'django_filters',
    'rest_framework',

    # Apps du projet
    'core',
    'academics',
    'fees',
]

# ---------------------------
# Middleware
# ---------------------------
MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',            # doit être haut
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',      # serve static files en prod
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

# ---------------------------
# CORS
# ---------------------------
ENV_CORS = os.environ.get("CORS_ALLOWED_ORIGINS", "")
if ENV_CORS:
    CORS_ALLOWED_ORIGINS = [u.strip() for u in ENV_CORS.split(",") if u.strip()]
    CORS_ALLOW_ALL_ORIGINS = False
else:
    CORS_ALLOWED_ORIGINS = []
    CORS_ALLOW_ALL_ORIGINS = DEBUG  # True only in debug

# Allow cookies for cross-site if you need auth via cookies (we use JWT so probably False)
CORS_ALLOW_CREDENTIALS = False

# ---------------------------
# URL configuration
# ---------------------------
ROOT_URLCONF = 'school_mgmt.urls'

# ---------------------------
# Templates (reste inchangé si tu veux)
# ---------------------------
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

# ---------------------------
# WSGI
# ---------------------------
WSGI_APPLICATION = 'school_mgmt.wsgi.application'

# ---------------------------
# Database (use DATABASE_URL if provided)
# ---------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL:
    # Require SSL in production (DEBUG False) — helps pour Railway/Postgres distant
    ssl_req = not DEBUG
    DATABASES = {
        'default': dj_database_url.parse(DATABASE_URL, conn_max_age=600, ssl_require=ssl_req)
    }
else:
    # fallback local dev DB (Postgres local)
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': 'school_db',
            'USER': 'school_user',
            'PASSWORD': 'Admin123',
            'HOST': 'localhost',
            'PORT': '5432',
        }
    }

# ---------------------------
# Password validation (inchangé)
# ---------------------------
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ---------------------------
# Internationalization
# ---------------------------
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# ---------------------------
# Static files (WhiteNoise)
# ---------------------------
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'   # required for collectstatic
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# If you keep additional static folders, declare STATICFILES_DIRS = [...]
# STATICFILES_DIRS = [BASE_DIR / "some_static_dir"]

# ---------------------------
# Default auto field
# ---------------------------
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ---------------------------
# DRF + JWT Configuration
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
    'ACCESS_TOKEN_LIFETIME': timedelta(hours=100),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'AUTH_HEADER_TYPES': ('Bearer',),
}

# ---------------------------
# Security / Cookies for production
# ---------------------------
# If DEBUG is False, enforce a few security settings
if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_SAMESITE = 'None'   # only if you need cross-site cookies; otherwise 'Lax'
    CSRF_COOKIE_SAMESITE = 'None'
    SECURE_HSTS_SECONDS = 3600
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
else:
    SECURE_SSL_REDIRECT = False

# Security headers for proxied deployments (Railway/Nginx/etc)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
X_FRAME_OPTIONS = 'DENY'
