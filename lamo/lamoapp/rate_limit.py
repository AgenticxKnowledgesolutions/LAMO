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
import ipaddress
import logging
import time

from django.conf import settings
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


# Published Cloudflare IP ranges (IPv4 & IPv6).
# Kept as tuple of ipaddress.ip_network objects for O(1)/fast containment checks.
CLOUDFLARE_IPV4_NETWORKS = (
    ipaddress.ip_network('173.245.48.0/20'),
    ipaddress.ip_network('103.21.244.0/22'),
    ipaddress.ip_network('103.22.200.0/22'),
    ipaddress.ip_network('103.31.4.0/22'),
    ipaddress.ip_network('141.101.64.0/18'),
    ipaddress.ip_network('108.162.192.0/18'),
    ipaddress.ip_network('190.93.240.0/20'),
    ipaddress.ip_network('188.114.96.0/20'),
    ipaddress.ip_network('197.234.240.0/22'),
    ipaddress.ip_network('198.41.128.0/17'),
    ipaddress.ip_network('162.158.0.0/15'),
    ipaddress.ip_network('104.16.0.0/13'),
    ipaddress.ip_network('104.24.0.0/14'),
    ipaddress.ip_network('172.64.0.0/13'),
    ipaddress.ip_network('131.0.72.0/22'),
)

CLOUDFLARE_IPV6_NETWORKS = (
    ipaddress.ip_network('2400:cb00::/32'),
    ipaddress.ip_network('2606:4700::/32'),
    ipaddress.ip_network('2803:f800::/32'),
    ipaddress.ip_network('2405:b500::/32'),
    ipaddress.ip_network('2405:8100::/32'),
    ipaddress.ip_network('2a06:98c0::/29'),
    ipaddress.ip_network('2c0f:f248::/32'),
)


INTERNAL_PROXY_NETWORKS = (
    ipaddress.ip_network('10.0.0.0/8'),
    ipaddress.ip_network('172.16.0.0/12'),
    ipaddress.ip_network('192.168.0.0/16'),
    ipaddress.ip_network('127.0.0.0/8'),
    ipaddress.ip_network('100.64.0.0/10'),
    ipaddress.ip_network('::1/128'),
    ipaddress.ip_network('fc00::/7'),
    ipaddress.ip_network('fe80::/10'),
)


def _safe_parse_ip(val):
    """Safely parse a string into an ipaddress object. Returns None on invalid inputs."""
    if not val or not isinstance(val, str):
        return None
    val = val.strip()
    if not val:
        return None
    # Strip port if present in IPv4 (e.g. '198.51.100.1:443')
    if ':' in val and '.' in val:
        parts = val.split(':')
        if len(parts) == 2 and parts[1].isdigit():
            val = parts[0]
    try:
        return ipaddress.ip_address(val)
    except (ValueError, TypeError):
        return None


def is_cloudflare_ip(ip_obj):
    """Check if an ipaddress object belongs to Cloudflare's published IP ranges."""
    if not ip_obj:
        return False
    if ip_obj.version == 4:
        return any(ip_obj in net for net in CLOUDFLARE_IPV4_NETWORKS)
    elif ip_obj.version == 6:
        return any(ip_obj in net for net in CLOUDFLARE_IPV6_NETWORKS)
    return False


def _is_trusted_internal_proxy(ip_obj):
    """Internal reverse proxies (Render load balancers, loopback, private subnets)."""
    if not ip_obj:
        return False
    return any(ip_obj in net for net in INTERNAL_PROXY_NETWORKS)


def client_ip(request):
    """Safely extract the real client IP in a Cloudflare -> Render -> Django architecture.

    Security guarantees:
    1. NEVER blindly trust arbitrary X-Forwarded-For headers or the first token.
       An attacker can prepend arbitrary spoofed IPs: 'X-Forwarded-For: 1.1.1.1'.
       Render appends the genuine connecting peer IP to the rightmost end.
    2. HTTP_CF_CONNECTING_IP is only trusted when the request actually arrived
       through a verified Cloudflare proxy hop (preventing attackers from bypassing
       Cloudflare and setting CF-Connecting-IP directly on the origin).
    3. All IP strings are strictly validated using Python's ipaddress module.
    4. Missing, invalid, or malformed headers fall back safely to REMOTE_ADDR or
       '127.0.0.1' without raising exceptions or bypassing rate limits.
    """
    if not request:
        return '127.0.0.1'

    # 1. Parse REMOTE_ADDR (the immediate socket peer, e.g. local gunicorn/Render proxy)
    remote_addr_raw = request.META.get('REMOTE_ADDR')
    remote_addr_obj = _safe_parse_ip(remote_addr_raw)

    # 2. Parse X-Forwarded-For chain: [hop_0, hop_1, ..., hop_N]
    # Reverse proxies append to the end. hop_N is the peer connecting to Render.
    xff_raw = request.META.get('HTTP_X_FORWARDED_FOR', '')
    raw_hops = [h.strip() for h in xff_raw.split(',') if h.strip()] if xff_raw else []
    parsed_hops = []
    for h in raw_hops:
        ip_obj = _safe_parse_ip(h)
        if ip_obj is not None:
            parsed_hops.append(ip_obj)

    # 3. Determine if request arrived via Cloudflare
    # In Cloudflare -> Render -> Django:
    # Render's proxy appends Cloudflare's IP to X-Forwarded-For (or connects with it as REMOTE_ADDR).
    # Inspect hops from right to left to identify the external connecting peer.
    is_via_cloudflare = False
    if remote_addr_obj and is_cloudflare_ip(remote_addr_obj):
        is_via_cloudflare = True
    elif parsed_hops:
        # Check the rightmost external (non-private) hop in X-Forwarded-For
        for hop in reversed(parsed_hops):
            if not _is_trusted_internal_proxy(hop):
                if is_cloudflare_ip(hop):
                    is_via_cloudflare = True
                break

    # 4. If confirmed via Cloudflare, check HTTP_CF_CONNECTING_IP
    cf_connecting_ip_raw = request.META.get('HTTP_CF_CONNECTING_IP')
    if is_via_cloudflare and cf_connecting_ip_raw:
        cf_ip_obj = _safe_parse_ip(cf_connecting_ip_raw)
        if cf_ip_obj is not None:
            return str(cf_ip_obj)

    # 5. Non-Cloudflare / Direct / Fallback:
    # Walk X-Forwarded-For from right to left, skipping internal proxies (10.x, 127.x, 172.16-31.x).
    # The rightmost non-internal IP is the authentic connecting client because intermediate
    # proxies append from the right; attackers can only prepend on the left.
    if parsed_hops:
        for hop in reversed(parsed_hops):
            if not _is_trusted_internal_proxy(hop):
                return str(hop)
        # If all hops are private/internal (e.g. test environment / intranet), take the first valid
        return str(parsed_hops[0])

    # 6. Fallback to REMOTE_ADDR
    if remote_addr_obj is not None:
        return str(remote_addr_obj)

    return '127.0.0.1'


# ---------------------------------------------------------------------------
# Cache-failure fallback
# ---------------------------------------------------------------------------
# If the shared cache raises (Redis down, network error), the limiter used to
# "fail open": every counter read returned "not limited", silently disabling the
# login / OTP / password-reset lockouts. Instead a small, bounded, per-process
# store takes over for as long as the cache is unavailable, so the same
# thresholds keep being enforced (per worker process rather than globally - a
# weaker but never absent limit). Entries expire on their own, so nobody is
# blocked permanently; when the store is full of live entries a NEW key is
# denied (a controlled, temporary refusal) rather than allowed unchecked.

class _LocalCounters:
    MAX_KEYS = 5000

    def __init__(self):
        import threading
        self._lock = threading.Lock()
        self._data = {}          # key -> [value, expires_at]

    def _purge(self, now):
        if len(self._data) >= self.MAX_KEYS:
            for key in [k for k, (_v, exp) in self._data.items() if exp <= now]:
                del self._data[key]

    def get(self, key):
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if not item or item[1] <= now:
                self._data.pop(key, None)
                return None
            return item[0]

    def set(self, key, value, ttl):
        now = time.time()
        with self._lock:
            self._purge(now)
            if key not in self._data and len(self._data) >= self.MAX_KEYS:
                return False
            self._data[key] = [value, now + ttl]
            return True

    def incr(self, key):
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if not item or item[1] <= now:
                raise ValueError(key)
            item[0] += 1
            return item[0]

    def delete(self, key):
        with self._lock:
            self._data.pop(key, None)

    def clear_all(self):
        with self._lock:
            self._data.clear()


_local = _LocalCounters()


def _warn_cache(exc, key):
    # Only the first 12 characters of the (already hashed) key are logged.
    logger.warning(
        'Rate-limit cache unavailable (%s); using the per-process fallback for key=%s',
        type(exc).__name__, key[:12],
    )


def _get(key):
    """Read a counter: shared cache first, per-process fallback if it errors."""
    try:
        return cache.get(key)
    except Exception as exc:
        _warn_cache(exc, key)
        return _local.get(key)


def _set(key, value, ttl):
    """Write a counter. Returns False only when no store could take it."""
    try:
        cache.set(key, value, ttl)
        return True
    except Exception as exc:
        _warn_cache(exc, key)
        return _local.set(key, value, ttl)


def _incr(key):
    try:
        return cache.incr(key)
    except ValueError:
        raise
    except Exception as exc:
        _warn_cache(exc, key)
        return _local.incr(key)


def is_rate_limited(key, threshold):
    """True when the counter for ``key`` has reached ``threshold``."""
    try:
        return int(_get(key) or 0) >= threshold
    except (TypeError, ValueError):
        return False


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
    _set(_expiry_key(key), deadline, lockout_seconds)
    return deadline


def lockout_seconds_remaining(key):
    """Seconds left on the counter's window, or 0 when it is not set."""
    deadline = _get(_expiry_key(key))
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

    Returns ``(is_locked, minutes_remaining)``. If neither the cache nor the
    fallback store can record the failure, the caller is told the key is
    locked (fail closed) so a broken limiter can never disable a lockout.
    """
    current_val = _get(key)

    if current_val is None:
        # First failure creates the sliding window.
        if not _set(key, 1, lockout_seconds):
            return True, 1
        _record_expiry(key, lockout_seconds)
    else:
        try:
            _incr(key)
        except ValueError:
            # Key expired between get() and incr(); start a new window.
            if not _set(key, 1, lockout_seconds):
                return True, 1
            _record_expiry(key, lockout_seconds)
        except Exception as exc:
            logger.warning('Rate-limit counter increment failed: %s', type(exc).__name__)
            return True, 1

    if is_rate_limited(key, threshold):
        # Refresh the window so the lockout is measured from the last failure.
        _set(key, _get(key), lockout_seconds)
        _record_expiry(key, lockout_seconds)
        logger.warning('Rate limit reached for key=%s', key[:12])
        return True, minutes_remaining(key)

    return False, 0


def register_attempt_within_window(key, maximum, window_seconds):
    """Count an event inside a FIXED window (used for OTP resends / AI / etc.).

    Returns ``(allowed, minutes_remaining)``. When no store can count the event
    the answer is "not allowed" for a short, retry-able period.
    """
    existing = _get(key)

    if existing is None:
        # First event opens the window and starts its clock.
        if not _set(key, 1, window_seconds):
            return False, 1
        _record_expiry(key, window_seconds)
        return True, 0

    try:
        count = _incr(key)
    except ValueError:
        # Key expired between get() and incr(): start a fresh window.
        if not _set(key, 1, window_seconds):
            return False, 1
        _record_expiry(key, window_seconds)
        return True, 0
    except Exception as exc:
        logger.warning('Rate-limit counter increment failed: %s', type(exc).__name__)
        return False, 1

    if count > maximum:
        return False, minutes_remaining(key)
    return True, 0


def check_cooldown(key, cooldown_seconds):
    """True when ``key`` was touched too recently to allow another event."""
    return _get(key) is not None


def start_cooldown(key, cooldown_seconds):
    _set(key, timezone.now().isoformat(), cooldown_seconds)


def clear(key):
    for k in (key, _expiry_key(key)):
        try:
            cache.delete(k)
        except Exception as exc:
            _warn_cache(exc, key)
        _local.delete(k)


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


def otp_verify_target_key(purpose, identifier):
    """Wrong-code counter bound to (purpose, destination), shared by every session.

    Lives in the server-side cache, so opening a new session or clearing
    cookies does not reset it. It is keyed on the destination (e-mail/phone) and
    the purpose only - never on the client - so one attacker cannot lock out an
    unrelated destination, and different purposes do not share a counter.
    """
    return 'ratelimit:otp-verify:%s:%s' % (purpose, _digest((identifier or '').strip().lower()))


def login_otp_ip_key(request):
    """Guards login OTP enumeration across many different e-mails / numbers."""
    return 'ratelimit:login-otp-ip:%s' % _digest(client_ip(request))


# OTP sends that are not tied to an existing account (sign-up, guest booking
# verification). Counted in the shared cache - not the session - so a client
# that drops its cookie cannot reset them.
OTP_SEND_PER_IP_MAX = 10
OTP_SEND_PER_TARGET_MAX = 5
OTP_SEND_WINDOW_SECONDS = 60 * 60


def otp_send_ip_key(request):
    return 'ratelimit:otp-send-ip:%s' % _digest(client_ip(request))


def otp_send_target_key(identifier):
    return 'ratelimit:otp-send-target:%s' % _digest((identifier or '').strip().lower())


# Doorstep arrival PIN guesses (per booking): 5 wrong PINs lock the PIN
# for 15 minutes. QR verification by the logged-in customer is unaffected.
ARRIVAL_PIN_MAX_ATTEMPTS = 5
ARRIVAL_PIN_LOCKOUT_SECONDS = 15 * 60


def arrival_pin_key(booking_id):
    return 'ratelimit:arrival-pin:%s' % booking_id


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
