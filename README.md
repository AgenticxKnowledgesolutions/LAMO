# LAMO — Home Service Marketplace

A Django web application that connects customers with verified local service
workers (plumbers, electricians, carpenters, cleaners, …) across Kerala.

Three roles are supported:

| Role | What they can do |
|---|---|
| `USER` (customer) | Browse services, book one-time / recurring / emergency jobs, pay, chat with the worker, review, raise complaints |
| `WORKER` | Register with ID proof, manage availability & leave, accept/reject jobs, start and complete work, request custom services |
| `ADMIN` | Verify & suspend workers, manage services and service requests, handle bookings, reviews, complaints and contact enquiries |

---

## 1. Requirements

- Python **3.13** (3.11+ works)
- MySQL / MariaDB (the project database is `lamo_project`)
- A Gmail account with an **App Password** if you want real OTP / notification emails

## 2. Setup

```bash
# 1. Clone
git clone https://github.com/AgenticxKnowledgesolutions/LAMO.git
cd LAMO

# 2. Virtual environment
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS / Linux

# 3. Dependencies
pip install -r requirements.txt

# 4. Configuration
copy .env.example lamo\.env    # Windows
# cp .env.example lamo/.env    # macOS / Linux
#   -> then edit lamo/.env and fill in the values
```

### Environment variables

Everything is read from `lamo/.env` (loaded by `python-dotenv`). See
[`.env.example`](.env.example) for the full annotated list.

| Variable | Default | Purpose |
|---|---|---|
| `DJANGO_SECRET_KEY` | insecure dev key (dev only) | **Required in production.** Must be ≥50 chars and not the committed dev key, or startup fails |
| `DJANGO_DEBUG` | `False` | `True` for local development |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma separated |
| `DJANGO_ENV` | `development` | `production` enables HTTPS/HSTS/secure cookies **and** the hard `SECRET_KEY` / `DJANGO_CACHE_URL` gates |
| `DJANGO_TIME_ZONE` | `UTC` | Use `Asia/Kolkata` for Indian local time |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | *(empty)* | Required behind a proxy |
| `DJANGO_CACHE_URL` | *(empty)* | **Required in production.** Shared cache for the rate limits; startup fails without it |
| `DJANGO_EMAIL_BACKEND` | SMTP | Use `django.core.mail.backends.console.EmailBackend` to print emails to the console while developing |
| `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` | *(empty)* | Gmail address + App Password |
| `GOOGLE_API_KEY` | *(empty)* | Gemini key for the AI assistant; optional, falls back to DB matching |
| `DATABASE_URL` / `DJANGO_DB_*` | MySQL `lamo_project` on `127.0.0.1` | See [Database](#3-database) |
| `DJANGO_SERVE_MEDIA` | `False` | Serve `/media/` through Django when `DEBUG=False` |

### Production configuration gates

When `DJANGO_ENV=production`, two settings are **mandatory** and the app refuses
to start (`ImproperlyConfigured`) rather than silently degrading:

1. **`DJANGO_SECRET_KEY`** — must be set, must be at least 50 characters, and
   must not be the development key committed in `settings.py`. That key signs
   session cookies, CSRF tokens and password-reset tokens, so a known key means
   those values can be forged.
2. **`DJANGO_CACHE_URL`** — must point at a **shared** cache. The login,
   password-reset, OTP and AI rate limits are stored in the cache, and Django's
   default per-process cache gives every gunicorn worker its own counters, which
   quietly defeats every lockout.

Both checks run while Django imports `settings.py`, so a misconfigured
deployment fails immediately and visibly instead of running insecurely.
Development and the test suite are unaffected: they keep using the development
key and an in-process cache.

## 3. Database

LAMO uses **MySQL** (`lamo_project`) by default, preserving existing user
accounts. The connection is read from the environment. To use a different
database, set one connection string or the explicit `DJANGO_DB_*` values.

```bash
# optional: override the default database
DATABASE_URL=mysql://user:password@127.0.0.1:3306/lamo_project
```

or the individual values:

| Variable | Default |
|---|---|
| `DJANGO_DB_ENGINE` | `django.db.backends.mysql` |
| `DJANGO_DB_NAME` | `lamo_project` |
| `DJANGO_DB_USER` | `root` |
| `DJANGO_DB_PASSWORD` | *(empty)* |
| `DJANGO_DB_HOST` | `127.0.0.1` |
| `DJANGO_DB_PORT` | `3306` |
| `DJANGO_DB_CONN_MAX_AGE` | `0` (persistent connections off) |

`DATABASE_URL` accepts `mysql://`, `mariadb://`, `postgresql://` and `sqlite:///`
and, when present, takes precedence over the `DJANGO_DB_*` variables. Passwords
containing `@`, `:` or `/` must be URL-encoded (`@` ? `%40`).
The parsing lives in `_database_from_url()` in `lamo/lamo/settings.py`.

For MySQL, create the database (once per machine) and apply the migrations:

```sql
CREATE DATABASE lamo_project CHARACTER SET utf8mb4;
```

```bash
cd lamo
python manage.py migrate
python manage.py createsuperuser
```

> The test suite (`--settings=lamo.settings_test`) runs against a throwaway
> **in-memory** database so tests work without a MySQL server. It never reads
> from or writes to your MySQL data.

## 4. Run

```bash
cd lamo
python manage.py runserver
```

Now open <http://127.0.0.1:8000/>.

> AI features and the Nominatim geocoder are **optional**: if the API key is
> missing or the network is unavailable, the app silently falls back to local
> database matching (see `lamoapp/ai_utils.py`).

### A note on the "Generated by Django X" headers in migrations

The `# Generated by Django <version>` first line of a migration file is
**provenance metadata only** — it records which Django version happened to
write the file. Django never reads it, and it has no effect on how the
migration is applied.

Several migrations in `lamoapp/migrations/` were generated by Django 6.0.4 while
the project pins and runs Django 4.2.27 (`requirements.txt`). They were kept
as-is: every operation they contain (`CreateModel`, `AddField`, `AlterField`,
`AlterUniqueTogether`, `RunPython`) and every field definition is valid in
Django 4.2, which is confirmed by the checks below passing cleanly:

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
```

Do not "fix" the headers by hand: editing them changes nothing functionally and
would only obscure the real history. If the project later upgrades to Django 6,
these files are still valid and can simply be left alone.

## 5. Tests

The suite runs against a disposable **in-memory** database, so **no MySQL server
is required** to run the tests:

```bash
cd lamo
python manage.py test --settings=lamo.settings_test
```

Other useful commands:

```bash
python manage.py check                                   # system checks
python manage.py makemigrations --check --dry-run         # models vs migrations
python manage.py collectstatic --noinput                  # production static files
```

GitHub Actions runs all three on every push/PR — see
[`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## 6. Production deployment checklist

1. `DJANGO_DEBUG=False`, `DJANGO_ENV=production`, real `DJANGO_SECRET_KEY`
   (≥50 chars, e.g. `python -c "import secrets; print(secrets.token_urlsafe(50))"`).
2. `DJANGO_CACHE_URL` set to a **shared** cache, e.g.
   `redis://:password@host:6379/0`. `redis` is already in `requirements.txt`.
   Without this the process refuses to start, because per-process rate-limit
   counters would be defeated across multiple workers.
3. `DJANGO_ALLOWED_HOSTS` + `DJANGO_CSRF_TRUSTED_ORIGINS` set to your domain.
4. `pip install whitenoise` (uncomment it in `requirements.txt`) and add
   `whitenoise.middleware.WhiteNoiseMiddleware` right after
   `SecurityMiddleware`, then run `collectstatic` — `STATIC_ROOT` is already
   configured at `lamo/staticfiles/`.
5. Serve `/media/` from the web server (or set `DJANGO_SERVE_MEDIA=True`).
6. Run the app with a real WSGI/ASGI server (e.g. `gunicorn lamo.wsgi`).
6. Watch `lamo/logs/lamo.log` — errors that used to be swallowed now land there.

## 7. Project layout

```
LAMO/
├── .env.example              # template for lamo/.env
├── requirements.txt
├── .github/workflows/ci.yml
└── lamo/
    ├── manage.py
    ├── lamo/                 # project settings
    │   ├── settings.py
    │   ├── settings_test.py  # in-memory DB for tests (never used by the app)
    │   └── urls.py
    ├── lamoapp/              # the single Django app
    │   ├── models.py         # 14 models (users, bookings, payments, …)
    │   ├── views.py          # all views (function based)
    │   ├── ai_utils.py       # Gemini + geocoding helpers
    │   ├── decorators.py     # role-based access decorators
    │   ├── admin.py
    │   └── migrations/
    ├── templates/            # 404.html / 500.html at the root
    └── media/                # user uploads (git-ignored)
```

## 8. Known follow-ups

These are intentionally **not** part of the current codebase — see the audit
notes for details:

- Real payment gateway (everything currently uses `Payment.gateway = 'manual'`).
  Customer-submitted payment reports remain pending until an admin verifies
  them against external payment evidence; the platform does not process cards
  or UPI payments itself.
- Refund handling (`Payment.status = 'REFUNDED'` exists but is never set).
- Recurring-series management UI — `BookingSeries.is_active` is never toggled.
- Real-time chat (`BookingMessage` is POST + page reload; no WebSockets).
- CSV/PDF report exports and worker payouts/commission.
- `lamoapp/apps.py` still shims two Django MySQL feature checks; upgrading
  MySQL/MariaDB lets that shim be deleted.
