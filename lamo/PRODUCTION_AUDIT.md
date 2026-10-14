# LAMO — Production Audit Report

Scope: Django 4.2 + MySQL (Aiven) deployed on Render (gunicorn + WhiteNoise),
Redis cache, Cloudinary for public images (`worker_proofs/` stays local/private),
Resend e-mail backend. `manage.py` is in `./lamo`.

Baseline commands (before fixes):

| Command | Result |
|---|---|
| `python manage.py check --deploy --settings=lamo.settings_test` | PASS (dev profile; production-only warnings do not apply) |
| `python manage.py makemigrations --check --dry-run` | `No changes detected` (clean) |
| `python manage.py test --settings=lamo.settings_test` | 436 tests, all pass |

## Issue table

| # | File:line | Sev | Cause | Fix |
|---|---|---|---|---|
| 1 | `lamo/lamo/settings.py:766-767` | **High** | `LOG_DIR.mkdir(parents=True, exist_ok=True)` runs at import. Render's disk is read-only/ephemeral; if the mount is not writable the process crashes at startup. The `file` RotatingFileHandler then also fails on a read-only disk. | Make `LOG_DIR` creation best-effort and only attach the file handler when the directory is writable; always keep the `console` handler (stdout). |
| 2 | `lamo/lamoapp/views.py:563-564` | **High** | `float(latitude)` on client-supplied string; `latitude=abc` (emergency booking) raises `ValueError` → HTTP 500. | Wrap in try/except, fall back to `None`. |
| 3 | `lamo/lamoapp/views.py:1528-1529` | **High** | Same `float()` crash on the recurring-booking path. | Guard with a helper. |
| 4 | `lamo/lamoapp/views.py:1605-1606, 1628-1629` | **High** | Same `float()` crash again for series/booking rows. | Guard with a helper. |
| 5 | `lamo/lamoapp/views.py:1934-1935` | **High** | Same `float()` crash on the one-time booking path. | Guard with a helper. |
| 6 | `lamo/lamoapp/views.py:6408-6411` | Medium | Orphaned `@login_required` line between two decorators (dead decorator, confusing/fragile). | Remove the stray blank decorator. |
| 7 | `lamo/lamoapp/views.py:7223-7224` | Low | Duplicate `@login_required` on `cancel_booking`. | Remove the duplicate. |
| 8 | `lamo/lamoapp/views.py:1143-1162` | **High** | Contact-form admin e-mail is gated on `settings.EMAIL_HOST_USER`. Under Resend (`USE_RESEND=True`) that variable is usually empty, so contact enquiries are silently never e-mailed. | Gate on Resend config as well; derive the recipient from a dedicated admin address. |
| 9 | `lamo/lamoapp/views.py:487` (`_handle_emergency_booking`) | Medium | Emergency path calls `request.user.pk` in rate-limit logging without an auth guard, though the route is `@login_required`; fine but the `float()` crash (#2) is the real 500 risk here. | Covered by #2. |
| 10 | `lamo/lamoapp/views.py:4188+ / 4255+ / 4382+` | Low (perf) | Dashboards issue many `.count()` queries; acceptable at current scale, but `admin_dashboard` runs ~14 aggregate queries per load. | Left as-is (correct, indexed, bounded); documented. |
| 11 | `lamo/lamoapp/views.py:6972+` (`admin_booking_details`), `7111+` (`admin_reviews_complaints`), `7282+` (`admin_contact_enquiries`) | Medium (perf) | Unpaginated full-table lists; will degrade as data grows. | Add pagination. |
| 12 | `lamo/lamo/settings.py:171-175, 752-756` | Medium | `ALLOWED_HOSTS` / `CSRF_TRUSTED_ORIGINS` default to localhost-only and are env-driven. If the `onrender.com` host is missing, Render returns 400 `DisallowedHost`. | Document required env values; add a safe `*.onrender.com` note (cannot wildcard hosts in Django, so it must be listed explicitly). |
| 13 | `lamo/lamoapp/views.py:1143` | Low | `contact` reads `settings.EMAIL_HOST_USER` directly instead of `DEFAULT_FROM_EMAIL`/a config constant. | See #8. |
| 14 | `lamo/lamoapp/rate_limit.py:140-151` | **High** | `register_attempt_within_window()` refreshed the counter's TTL (and recorded deadline) on **every** call, making it a *sliding* window. A caller that kept retrying (AI quota, OTP resend, emergency/contact abuse limits) never let the counter expire - once the limit was hit, every retry pushed the reset a full window further out, so the caller stayed blocked **indefinitely**. | Only set the TTL/deadline when the key is first created; use `cache.incr()` for later events so the fixed window expires `window_seconds` after the *first* attempt. |
| 15 | `lamo/lamoapp/views.py:3654` | **High** | `CustomUser.objects.get(email__iexact=email)` in the public forgot-password view caught only `DoesNotExist`. `CustomUser.email` is **not unique** (inherited from `AbstractUser`), so two accounts sharing an address raise `MultipleObjectsReturned` → uncaught → HTTP 500. | Catch `MultipleObjectsReturned` as well and answer it exactly like an unknown address (same generic message, no leak). |

## What is already correct (verified, not changed)

- **Role access control**: every non-public view checks `request.user.role` and
  redirects to `dashboard` (which re-routes per role) or returns 403; all
  private views are `@login_required`. `proof_download` is ADMIN-only.
- **Back button**: dashboards carry `@never_cache`, so a logged-out/other-role
  session cannot repaint a cached dashboard from the browser history.
- **worker_proofs privacy**: `worker_proofs/` is never a Cloudinary prefix
  (fail-closed local), `urls.py` blocks `/media/worker_proofs/...`
  unconditionally, and `proof_download` is the only reader.
- **Email**: every `send_email_safely` call is failure-tolerant (returns bool,
  never raises); no `send_mail` is called bare.
- **`.path` usage**: only `proof_download` opens proofs (local disk); the
  Cloudinary backend raises `NotImplementedError` for remote names, which is
  caught.
- **Unique `Service.name`**: the publish flow reuses/creates safely inside a
  transaction with `iexact` lookups; `IntegrityError` paths are guarded.

## Verification after fixes

| Command | Result |
|---|---|
| `python manage.py check --settings=lamo.settings_test` | System check identified no issues |
| `python manage.py makemigrations --check --dry-run` | `No changes detected` (exit 0) |
| `python manage.py test --settings=lamo.settings_test` | **446 tests, OK** (1 skipped) |
| `DJANGO_ENV=production python manage.py check --deploy` | System check identified no issues (0 silenced) |

## Still needs manual setup in the Render dashboard

These are configuration/secrets that cannot live in the repository:

1. **`DJANGO_SECRET_KEY`** — `sync: false` in `render.yaml`. Generate with
   `python -c "import secrets; print(secrets.token_urlsafe(50))"` (>= 50 chars).
   The process refuses to start without it.
2. **`DATABASE_URL`** — the Aiven MySQL connection string (URL-encode special
   characters in the password). If the Aiven instance requires TLS, also set
   **`DJANGO_DB_SSL_CA`** to the uploaded `ca.pem` path and leave
   `DJANGO_DB_SSL_MODE` at the default (`VERIFY_IDENTITY`).
3. **`DJANGO_CACHE_URL`** — the Render Key Value (Redis) URL, e.g.
   `redis://:PASSWORD@HOST:6379/0` (or `rediss://` for TLS). Required in
   production; the process refuses to start without it.
4. **`DJANGO_ALLOWED_HOSTS`** — must list **both** the custom subdomain and the
   Render host, comma-separated, no scheme:
   `lamo.agenticx.in,<service>.onrender.com`.
   Django cannot wildcard hosts, so the `onrender.com` hostname must be listed
   explicitly or every request returns HTTP 400 `DisallowedHost`.
5. **`DJANGO_CSRF_TRUSTED_ORIGINS`** — full origins **with scheme**:
   `https://lamo.agenticx.in,https://<service>.onrender.com`.
6. **Cloudinary** — `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`,
   `CLOUDINARY_API_SECRET`. Without all three, public images are stored on
   Render's ephemeral disk and vanish on every deploy/restart.
7. **Resend** — `USE_RESEND=True`, `RESEND_API_KEY`, `RESEND_SENDER` (a
   verified domain/sender). Optionally set `CONTACT_NOTIFICATION_EMAIL` to the
   mailbox that should receive contact-form enquiries (falls back to
   `EMAIL_HOST_USER` then `DEFAULT_FROM_EMAIL`).
8. **`GOOGLE_API_KEY`** — optional; AI features fall back to local matching.
9. **Persistent disk (only if you keep local media)** — `worker_proofs/` always
   stays on local disk, so mount a Render persistent disk and set
   `DJANGO_MEDIA_ROOT` to the mount path, otherwise proof documents are lost on
   redeploy. `DJANGO_SERVE_MEDIA` stays `True` only for that local fallback;
   `/media/worker_proofs/...` is blocked unconditionally in `urls.py`.
10. **Build/start** — confirm the build command runs `collectstatic --noinput`
    and the start command is `cd lamo && gunicorn lamo.wsgi --log-file -`
    (already in `render.yaml`). Logging goes to stdout; no log file is needed.