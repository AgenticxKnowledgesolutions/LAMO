"""Test-only settings.

The application database is MySQL (configured in settings.py) and is NOT
touched by this module. Tests run against a disposable **in-memory** database
so the suite works on any machine and in CI without a MySQL server.

Nothing here is ever imported by the running application - it only applies
when you pass ``--settings=lamo.settings_test``.

Usage:
    python manage.py test --settings=lamo.settings_test
"""

import os

# settings.py warns when DJANGO_SECRET_KEY is missing. Provide a throwaway
# value before importing it so the test run stays quiet.
os.environ.setdefault('DJANGO_SECRET_KEY', 'test-only-secret-key-not-for-production')

# The production configuration gates in settings.py (P0-B / P0-C) refuse to
# start a production process without a real DJANGO_SECRET_KEY and a shared
# DJANGO_CACHE_URL. The test run is never production, so make that explicit
# rather than relying on the ambient environment - otherwise running the suite
# on a production box (or with DJANGO_ENV exported) would fail at import.
os.environ['DJANGO_ENV'] = 'development'
# Never point the suite at a developer's real Redis instance.
os.environ.pop('DJANGO_CACHE_URL', None)
# Never point the suite at a real Cloudinary account: a developer's exported
# credentials would otherwise activate remote uploads for every test.
os.environ.pop('CLOUDINARY_CLOUD_NAME', None)
os.environ.pop('CLOUDINARY_API_KEY', None)
os.environ.pop('CLOUDINARY_API_SECRET', None)
os.environ.pop('CLOUDINARY_URL', None)

from .settings import *  # noqa: E402,F401,F403


DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

# Tests never need a shared cache: there is a single process, so the
# per-process cache is exactly right and keeps the run self-contained.
# Setting it explicitly also means a developer's exported DJANGO_CACHE_URL
# cannot make the suite depend on a running Redis server.
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'lamo-test-cache',
    }
}

# Fast, non-cryptographic hashing - tests only.
PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.MD5PasswordHasher',
]

# Never touch a real SMTP server while testing.
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
USE_RESEND = False

# Never touch the real Cloudinary account while testing: the prefix-routing
# storage backend activates only when all three credentials are set, so
# blanking them pins every upload to local disk for the whole suite.
CLOUDINARY_CLOUD_NAME = ''
CLOUDINARY_API_KEY = ''
CLOUDINARY_API_SECRET = ''

DEBUG = False
