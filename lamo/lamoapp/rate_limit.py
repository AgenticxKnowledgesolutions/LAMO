"""Server-side rate limiting for credential and OTP endpoints.

Why this module exists
----------------------
The login form, the forgot-password form and the OTP resend button used to
keep their counters in ``request.session``. The session lives in a cookie the
client controls, so an attacker could delete it (or open a private window) and
get a brand new, empty counter - turning the "5 attempts then 15 minute
lockout" into "5 attempts, forever".

Counters here are stored in Django's **cache**, i.e. on the server. Clearing
cookies, starting a new session, rotating the user agent or switching IP-masq
behind one host changes nothing: the counter is still there.

Design notes
------------
* Keys are SHA-256 digests, never raw usernames or e-mails, so the cache keys
  (and any cache dump) never leak personal data.
* Failures are counted with ``cache.incr`` on a key that already holds a TTL,
  so the window is created once on the first failure and then slides
  naturally - no extra bookkeeping fields.
* A successful login / OTP verification clears the counter, so legitimate
  users are never punished.

No new dependency and no new model field: this is ``django.core.cache`` only.
Deployments running more than one process should point ``CACHES`` at a shared
backend (Redis/Memcached) via ``DJANGO_CACHE_URL`` - see settings.py.
"""

import hashlib
import logging
import time

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)


# Thresholds/lifetimes, in one place so the views stay readable.
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60

FORGOT_PASSWORD_MAX_ATTEMPTS = 3
FORGOT_PASSWORD_LOCKOUT_SECONDS = 30 * 60

# A resend replaces the code in the user's inbox, so it is rate limited
# per account and per IP to stop it being used as a mail-bombing primitive.
OTP_RESEND_MAX = 3
OTP_RESEND_WINDOW_SECONDS = 15 * 60
OTP_RESEND_COOLDOWN_SECONDS = 60

# Re-sending a fresh code must not hand the attacker a fresh set of guesses.
OTP_VERIFY_MAX_ATTEMPTS = 5
OTP_VERIFY_LOCKOUT_SECONDS = 15 * 60


def _digest(*parts):
    """Hash the identifying parts so raw identifiers never reach the cache."""
    raw = '|'.join(str(part) for part in parts if part is not None)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def client_ip(request):
    """Best-effort client IP, falling back to a stable constant."""
    return (
        request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
        or request.META.get('REMOTE_ADDR')
        or 'unknown'
    )


def is_rate_limited(key, threshold):
    """True when the counter for ``key`` has reached ``threshold``."""
    return int(cache.get(key, 0) or 0) >= threshold


def _expiry_key(key):
    return key + ':expires'


def _record_expiry(key, lockout_seconds):
    """Remember when the window ends.

    ``cache.ttl()`` is not part of the base cache API (it only exists on Redis
    and Memcached backends), so the deadline is tracked explicitly in a second
    cache entry. That keeps ``minutes_remaining()`` working on every backend,
    including the LocMemCache default.
    """
    deadline = int(time.time()) + int(lockout_seconds)
    cache.set(_expiry_key(key), deadline, lockout_seconds)
    return deadline


def lockout_seconds_remaining(key):
    """Seconds left on the counter's window, or 0 when it is not set."""
    deadline = cache.get(_expiry_key(key))
    if deadline is None:
        return 0
    try:
        return max(0, int(deadline) - int(time.time()))
    except (TypeError, ValueError):
        return 0


def minutes_remaining(key):
    seconds = lockout_seconds_remaining(key)
    if seconds <= 0:
        return 0
    return max(1, (seconds + 59) // 60)


def register_failure(key, threshold, lockout_seconds):
    """Count one failure; set the lockout window once the threshold is hit.

    Returns ``(is_locked, minutes_remaining)``.
    """
    if cache.get(key) is None:
        # First failure creates the sliding window.
        cache.set(key, 1, lockout_seconds)
        _record_expiry(key, lockout_seconds)
    else:
        try:
            cache.incr(key)
        except ValueError:
            # Key expired between get() and incr(); start a new window.
            cache.set(key, 1, lockout_seconds)
            _record_expiry(key, lockout_seconds)

    if is_rate_limited(key, threshold):
        # Refresh the window so the lockout is measured from the last failure.
        cache.set(key, cache.get(key), lockout_seconds)
        _record_expiry(key, lockout_seconds)
        logger.warning('Rate limit reached for key=%s', key[:12])
        return True, minutes_remaining(key)

    return False, 0


def register_attempt_within_window(key, maximum, window_seconds):
    """Count an event inside a fixed window (used for OTP resends).

    Returns ``(allowed, minutes_remaining)``.
    """
    count = int(cache.get(key, 0) or 0) + 1
    cache.set(key, count, window_seconds)
    _record_expiry(key, window_seconds)

    if count > maximum:
        return False, minutes_remaining(key)
    return True, 0


def check_cooldown(key, cooldown_seconds):
    """True when ``key`` was touched too recently to allow another event."""
    return cache.get(key) is not None


def start_cooldown(key, cooldown_seconds):
    cache.set(key, timezone.now().isoformat(), cooldown_seconds)


def clear(key):
    cache.delete(key)
    cache.delete(_expiry_key(key))


# Key builders - one place to look for what is rate limited.

def login_key(request, username):
    """Per account *and* per IP: locks out the pair, not every user."""
    return 'ratelimit:login:%s:%s' % (
        _digest(client_ip(request)),
        _digest(username),
    )


def forgot_password_key(request, email):
    return 'ratelimit:forgotpw:%s:%s' % (
        _digest(client_ip(request)),
        _digest((email or '').lower()),
    )


def forgot_password_ip_key(request):
    """Guards address enumeration across many different e-mails."""
    return 'ratelimit:forgotpw-ip:%s' % _digest(client_ip(request))


def otp_resend_user_key(user_id):
    return 'ratelimit:otp-resend-user:%s' % _digest(user_id)


def otp_resend_ip_key(request):
    return 'ratelimit:otp-resend-ip:%s' % _digest(client_ip(request))


# ============================================================
# AI ENDPOINT THROTTLING
# ============================================================
# The AI endpoints spend real money: every ai_service_assistant request from a
# signed-in user can drive up to two billable Gemini calls
# (chat_with_gemini + match_service_and_workers), and Gemini is a 15s blocking
# HTTP call. Registration is open, so "signed in" is a cheap precondition -
# without a limit any account can burn the key's quota.
#
# Guests are throttled too. Their replies are static (no Gemini call), but the
# endpoint still runs a DB query per request, so an unthrottled guest path is a
# cheap way to hammer the app.
#
# Counts are per (IP, identity) pair: the IP half stops a single account being
# rotated behind fresh sessions, and the identity half stops one host being
# locked out by everyone behind the same NAT.

AI_ASSISTANT_PER_MINUTE = 10
AI_ASSISTANT_PER_HOUR = 60
AI_RECOMMEND_PER_MINUTE = 20


def _ai_identity(request):
    """A stable identity for the caller: the account when signed in, else IP."""
    user = getattr(request, 'user', None)
    if user is not None and getattr(user, 'is_authenticated', False):
        return 'user:%s' % user.pk
    return 'ip:%s' % client_ip(request)


def _ai_key(prefix, request, window):
    return 'ratelimit:%s:%s:%s' % (
        prefix,
        _digest(client_ip(request)),
        '%s-%s' % (_digest(_ai_identity(request)), window),
    )


def ai_assistant_minute_key(request):
    return _ai_key('ai-assistant', request, 'min')


def ai_assistant_hour_key(request):
    return _ai_key('ai-assistant', request, 'hour')


def ai_recommend_minute_key(request):
    return _ai_key('ai-recommend', request, 'min')


# ============================================================
# EMERGENCY BOOKING ABUSE LIMIT
# ============================================================
# An emergency request is deliberately high-friction for the customer (it is
# announced to every eligible worker for that trade and to every admin), which
# is exactly what makes it attractive as an abuse primitive: a script could
# create rows and notification floods in a tight loop, burying real workers'
# dashboards and the admin alerts under junk.
#
# Two independent budgets, because one alone is not enough:
#   * per account - stops one account spamming the broadcast
#   * per IP      - stops many throwaway accounts from one host
#
# The limits are deliberately generous so a genuine user who has a real
# problem (a burst, a retry after a flaky connection, two different flats) is
# never blocked, while a scripted loop is stopped within seconds.
EMERGENCY_MAX_PER_HOUR = 5
EMERGENCY_WINDOW_SECONDS = 60 * 60

# Upper bound on how many workers one broadcast pings. Protects the fan-out for
# the legitimate many-workers case as well as the abuse case.
EMERGENCY_MAX_NOTIFIED_WORKERS = 50

# An unclaimed broadcast older than this is considered dead and is cancelled when
# the next emergency arrives, so abandoned requests do not accumulate forever.
EMERGENCY_STALE_HOURS = 12


# ============================================================
# CONTACT FORM ABUSE LIMIT
# ============================================================
# The contact form needs no account and, on every accepted submission, both
# stores a ContactEnquiry row and sends an e-mail to the admin mailbox. Left
# unbounded that is a storage-growth vector and a mail-flooding primitive aimed
# at the admin inbox. Counted per IP and per e-mail address, server-side.
#
# 5 per hour is generous for genuine contact ("my booking reference is wrong",
# "I cannot log in") while stopping a script within a few seconds.
CONTACT_MAX_SUBMISSIONS = 5
CONTACT_WINDOW_SECONDS = 60 * 60


def contact_ip_rate_key(request):
    return 'ratelimit:contact-ip:%s' % _digest(client_ip(request))


def contact_email_rate_key(email):
    return 'ratelimit:contact-email:%s' % _digest((email or '').lower())


def emergency_account_key(user_id):
    return 'ratelimit:emergency-account:%s' % _digest(user_id)


def emergency_ip_key(request):
    return 'ratelimit:emergency-ip:%s' % _digest(client_ip(request))


def consume_ai_quota(key, maximum, window_seconds):
    """Count one AI request against ``key`` and report the outcome.

    Returns ``(allowed, retry_after_seconds)``. ``retry_after_seconds`` is 0
    while the caller is within budget, and otherwise the number of seconds the
    caller should wait - suitable for the ``Retry-After`` header.

    The counters live in the cache, so deleting the session cookie, opening a
    private window or simply waiting for a new session changes nothing.
    """
    allowed, _unused = register_attempt_within_window(key, maximum, window_seconds)
    if allowed:
        return True, 0

    remaining = lockout_seconds_remaining(key)
    return False, max(1, remaining)
