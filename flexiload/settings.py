import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-only-change-me")
DEBUG = os.getenv("DJANGO_DEBUG", "true").lower() == "true"
ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
    if host.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "topups",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "flexiload.urls"

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

WSGI_APPLICATION = "flexiload.wsgi.application"

if os.getenv("DB_ENGINE", "sqlite").lower() == "mysql":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": os.getenv("DB_NAME", "flexiload"),
            "USER": os.getenv("DB_USER", "flexiload"),
            "PASSWORD": os.getenv("DB_PASSWORD", ""),
            "HOST": os.getenv("DB_HOST", "127.0.0.1"),
            "PORT": os.getenv("DB_PORT", "3306"),
            "OPTIONS": {"charset": "utf8mb4"},
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.getenv("DJANGO_TIME_ZONE", "Asia/Dhaka")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "/dashboard/login/"
LOGIN_REDIRECT_URL = "/dashboard/"
AUTHENTICATION_BACKENDS = ["topups.authentication.EmailBackend"]

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "20/minute",
        "user": "120/minute",
        "login": "10/minute",
    },
}

PIN_ENCRYPTION_KEY = os.getenv("PIN_ENCRYPTION_KEY", "")
MODEM_BACKEND = os.getenv("MODEM_BACKEND", "mock")
MODEM_PORT = os.getenv("MODEM_PORT", "COM3")
MODEM_BAUDRATE = int(os.getenv("MODEM_BAUDRATE", "115200"))
MODEM_TIMEOUT = int(os.getenv("MODEM_TIMEOUT", "30"))
MODEM_SMS_POLL_INTERVAL = int(os.getenv("MODEM_SMS_POLL_INTERVAL", "20"))
MODEM_DELETE_AFTER_STORE = os.getenv(
    "MODEM_DELETE_AFTER_STORE", "true"
).lower() == "true"
# Recharges are queued and sent one at a time per SIM by the modem poller.
# If a sent recharge gets no confirmation SMS within this many minutes, it is
# marked failed so the queue keeps moving (a late success SMS still corrects
# it afterwards).
MODEM_PROCESSING_TIMEOUT_MINUTES = int(
    os.getenv("MODEM_PROCESSING_TIMEOUT_MINUTES", "6")
)
# Offer-check aggregator API (used by the Offer Check page/API). The Laravel
# panel used port 9090; set OFFER_API_BASE_URL in the environment to switch.
OFFER_API_BASE_URL = os.getenv("OFFER_API_BASE_URL", "")
OFFER_API_USER_ID = os.getenv("OFFER_API_USER_ID", "")
OFFER_API_PASSWORD = os.getenv("OFFER_API_PASSWORD", "")
OFFER_API_REFER_ID = os.getenv("OFFER_API_REFER_ID", "")
OFFER_API_TIMEOUT = int(os.getenv("OFFER_API_TIMEOUT", "30"))

IGL_WEBHOOK_URL = os.getenv("IGL_WEBHOOK_URL", "")
IGL_WEBHOOK_SECRET = os.getenv("IGL_WEBHOOK_SECRET", "")
IGL_WEBHOOK_TIMEOUT = int(os.getenv("IGL_WEBHOOK_TIMEOUT", "10"))

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} {name} {message}",
            "style": "{",
        }
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
        "transactions": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": LOG_DIR / "transactions.log",
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 5,
            "formatter": "verbose",
        },
    },
    "loggers": {
        "topups": {
            "handlers": ["console", "transactions"],
            "level": os.getenv("LOG_LEVEL", "INFO"),
            "propagate": False,
        }
    },
}
