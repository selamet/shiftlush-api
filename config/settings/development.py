"""Local development. Never used in production."""

from .base import *

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

CORS_ALLOWED_ORIGINS = ["http://localhost:5173"]

# Invitation and password-reset mails go to the console rather than a real
# outbox, so a developer never sends one by accident.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
