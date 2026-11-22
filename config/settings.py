import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
# Public hostname when deployed (e.g. email-agent.onrender.com), or of a tunnel when
# testing Gmail push locally; unset for plain local dev.
# On Render and Railway it defaults to the hostname the platform assigns.
APP_HOST = (
    os.environ.get("APP_HOST")
    or os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    or os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
)
ALLOWED_HOSTS = ["localhost", "127.0.0.1", *([APP_HOST] if APP_HOST else [])]
CSRF_TRUSTED_ORIGINS = [f"https://{APP_HOST}"] if APP_HOST else []

if APP_HOST:
    # Behind the host's HTTPS proxy: trust its scheme header, so the Google
    # redirect URI is built as https://, and only send cookies over HTTPS.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
]

MIDDLEWARE = [
    # First, so the host's health checks get an answer whatever Host header they send.
    "accounts.middleware.HealthCheckMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
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

# The web server and the scheduler process share this file. When deployed it
# lives on a persistent volume (DATABASE_PATH=/data/app.db).
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("DATABASE_PATH", BASE_DIR / "app.db"),
        "OPTIONS": {
            "init_command": "PRAGMA journal_mode=WAL;",
            "transaction_mode": "IMMEDIATE",
            "timeout": 20,
        },
    }
}

AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "home"
# Set SIGNUP_CLOSED=1 on a public server once your own account exists, so
# strangers can't sign up and run the agent on your Anthropic key.
SIGNUP_CLOSED = os.environ.get("SIGNUP_CLOSED", "0") == "1"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Google "Web application" OAuth client, and the key that encrypts stored refresh tokens.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
TOKEN_KEY = os.environ["TOKEN_KEY"]

# Gmail push notifications (docs/gmail-push.md). Both unset: the scheduler only polls.
# The Pub/Sub topic Gmail posts to, as projects/<project-id>/topics/<topic>.
GMAIL_PUBSUB_TOPIC = os.environ.get("GMAIL_PUBSUB_TOPIC", "")
# Secret in the push subscription's URL, so only Pub/Sub can wake the agent.
GMAIL_PUSH_TOKEN = os.environ.get("GMAIL_PUSH_TOKEN", "")
