import re
import csv
import json
import io
import logging
import urllib.request
import urllib.parse
import os
import uuid
import hashlib
import secrets
import string
from decimal import Decimal, InvalidOperation
from functools import lru_cache

from django.db import IntegrityError, transaction, models
from django.db.models import Q, Sum, Avg, Count, Min, Case, When, Value, IntegerField, Prefetch, ProtectedError, F
from django.db.models.functions import TruncMonth
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth import update_session_auth_hash
from django.contrib import messages
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse, HttpResponseForbidden, Http404, FileResponse, HttpResponseNotAllowed, HttpResponseNotFound
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone
from datetime import timedelta
from .models import CustomUser, UserProfile, Service, WorkerProfile, Booking, BookingSeries, BookingMessage, Notification, Complaint, Review, WorkerLeave, ContactEnquiry, ServiceRequest, Payment, StrikeRecord, WorkerWallet, WalletTransaction, PushSubscription, CallSession
from .push_utils import send_web_push_async, broadcast_web_push_async
from .email_utils import send_email_safely
from django.core.signing import TimestampSigner, BadSignature, SignatureExpired
from .rate_limit import (
    AI_ASSISTANT_PER_HOUR,
    AI_ASSISTANT_PER_MINUTE,
    AI_RECOMMEND_PER_MINUTE,
    EMERGENCY_MAX_NOTIFIED_WORKERS,
    EMERGENCY_MAX_PER_HOUR,
    EMERGENCY_STALE_HOURS,
    EMERGENCY_WINDOW_SECONDS,
    CONTACT_MAX_SUBMISSIONS,
    CONTACT_WINDOW_SECONDS,
    FORGOT_PASSWORD_LOCKOUT_SECONDS,
    FORGOT_PASSWORD_MAX_ATTEMPTS,
    LOGIN_LOCKOUT_SECONDS,
    LOGIN_MAX_ATTEMPTS,
    OTP_RESEND_COOLDOWN_SECONDS,
    OTP_RESEND_MAX,
    OTP_RESEND_WINDOW_SECONDS,
    OTP_VERIFY_LOCKOUT_SECONDS,
    OTP_VERIFY_MAX_ATTEMPTS,
    otp_verify_target_key,
    ai_assistant_hour_key,
    ai_assistant_minute_key,
    ai_recommend_minute_key,
    check_cooldown,
    clear,
    client_ip,
    contact_email_rate_key,
    contact_ip_rate_key,
    consume_ai_quota,
    emergency_account_key,
    emergency_ip_key,
    forgot_password_ip_key,
    forgot_password_key,
    is_rate_limited,
    login_key,
    login_otp_ip_key,
    minutes_remaining,
    otp_resend_ip_key,
    otp_resend_user_key,
    register_attempt_within_window,
    register_failure,
    start_cooldown,
    OTP_SEND_PER_IP_MAX,
    OTP_SEND_PER_TARGET_MAX,
    OTP_SEND_WINDOW_SECONDS,
    otp_send_ip_key,
    otp_send_target_key,
    ARRIVAL_PIN_MAX_ATTEMPTS,
    ARRIVAL_PIN_LOCKOUT_SECONDS,
    arrival_pin_key,
)
from .forms import ServiceForm, UserProfileForm, WorkerProfileForm
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.conf import settings
from django.utils import timezone
from datetime import datetime, date, time as datetime_time, timedelta
# geocode_place and haversine_km are used by _compute_worker_distance() to turn a
# worker's free-text address into coordinates and then measure the distance to the
# customer. They live in ai_utils; importing them here is what makes the AI
# service-match reply work for workers who have an address but no explicit
# latitude/longitude. Without this import the call raised NameError, which the
# broad `except Exception` in ai_service_assistant() turned into a generic
# "something went wrong" reply instead of the matched service and worker list.
from .ai_utils import (
    build_worker_recommendations,
    geocode_place,
    haversine_km,
)

logger = logging.getLogger(__name__)


def _create_idempotent_notification(recipient, message, booking=None, notification_type='SYSTEM', dedupe_window_seconds=10):
    """Creates a notification ensuring rapid duplicate requests within dedupe_window_seconds are ignored.
    
    Safe across web requests without schema migrations: uses cache atomic check with fallback to DB window check.
    """
    if not recipient:
        return None

    import hashlib
    from django.core.cache import cache
    from django.utils import timezone
    from datetime import timedelta

    booking_id = booking.id if booking else 0
    raw_key = f"{recipient.id}:{booking_id}:{notification_type}:{message.strip()}"
    key_hash = hashlib.sha256(raw_key.encode('utf-8')).hexdigest()[:24]
    cache_key = f"notif_dedupe:{key_hash}"

    # 1. Atomic cache lock (fast path)
    try:
        acquired = cache.add(cache_key, True, timeout=dedupe_window_seconds)
        if not acquired:
            logger.info("Deduplicated duplicate notification for user %s (cache match)", recipient.username)
            return None
    except Exception:
        # Cache backend failed/unavailable, proceed to DB check
        pass

    # 2. Database recency check (defense in depth)
    recent_cutoff = timezone.now() - timedelta(seconds=dedupe_window_seconds)
    duplicate_exists = Notification.objects.filter(
        recipient=recipient,
        booking=booking,
        notification_type=notification_type,
        message=message,
        created_at__gte=recent_cutoff
    ).exists()

    if duplicate_exists:
        logger.info("Deduplicated duplicate notification for user %s (DB match)", recipient.username)
        return None

    return Notification.objects.create(
        recipient=recipient,
        booking=booking,
        notification_type=notification_type,
        message=message
    )


def _safe_coord(value):
    """Parse a client-supplied latitude/longitude, or return ``None``.

    Coordinates arrive as free-form strings (form fields, JSON body, or the
    literal ``'undefined'`` the browser's Geolocation API produces on failure).
    Calling ``float()`` on any of those directly raised ``ValueError`` and
    turned a bad POST into an HTTP 500. Anything that is not a real number is
    treated as "no coordinates" instead.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ('undefined', 'null', 'none', 'nan'):
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    # Reject NaN/inf: they are not valid coordinates and break distance math.
    if number != number or number in (float('inf'), float('-inf')):
        return None
    return number


def custom_403_view(request, exception=None):
    """Friendly 403 Forbidden page (wired up via handler403 in lamo/urls.py)."""
    return render(request, '403.html', status=403)


def custom_404_view(request, exception=None):
    """Friendly 404 page (wired up via handler404 in lamo/urls.py)."""
    return render(request, '404.html', status=404)


def custom_500_view(request):
    """Friendly 500 page (wired up via handler500 in lamo/urls.py)."""
    return render(request, '500.html', status=500)


def health_check(request):
    """Ultra-lightweight health check endpoint for container orchestrators (e.g. Render).

    Guarantees:
    - HTTP 200 plain text response
    - Zero database queries
    - Zero external API calls
    - Zero template rendering
    - No authentication dependency
    """
    return HttpResponse('OK', content_type='text/plain; charset=utf-8', status=200)


def robots_txt(request):
    """Serve robots.txt with search crawler directives protecting private routes."""
    lines = [
        "User-agent: *",
        "Disallow: /admin/",
        "Disallow: /admin-dashboard/",
        "Disallow: /user-dashboard/",
        "Disallow: /worker-dashboard/",
        "Disallow: /my-bookings/",
        "Disallow: /my-series/",
        "Disallow: /my-complaints/",
        "Disallow: /profile/",
        "Disallow: /booking/",
        "Disallow: /worker/",
        "Disallow: /worker-jobs/",
        "Disallow: /worker-earnings/",
        "Disallow: /worker-profile/",
        "Disallow: /worker-notifications/",
        "Disallow: /notifications/",
        "Disallow: /admin-notifications/",
        "Disallow: /admin-export/",
        "Disallow: /service-requests/",
        "Disallow: /ai/",
        "Disallow: /media/worker_proofs/",
        "Allow: /",
        "Allow: /about/",
        "Allow: /contact/",
        "Allow: /service_page/",
        "Allow: /services/",
        "Allow: /terms/",
        "Allow: /privacy/",
        "Allow: /static/",
        "",
        f"Sitemap: {request.build_absolute_uri('/sitemap.xml')}",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain; charset=utf-8")


def sitemap_xml(request):
    """Serve dynamic XML sitemap with public canonical URLs."""
    base_url = request.build_absolute_uri('/').rstrip('/')

    static_urls = [
        {'loc': f"{base_url}/", 'changefreq': 'daily', 'priority': '1.0'},
        {'loc': f"{base_url}/about/", 'changefreq': 'monthly', 'priority': '0.8'},
        {'loc': f"{base_url}/contact/", 'changefreq': 'monthly', 'priority': '0.8'},
        {'loc': f"{base_url}/service_page/", 'changefreq': 'daily', 'priority': '0.9'},
        {'loc': f"{base_url}/terms/", 'changefreq': 'yearly', 'priority': '0.5'},
        {'loc': f"{base_url}/privacy/", 'changefreq': 'yearly', 'priority': '0.5'},
    ]

    xml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]

    for item in static_urls:
        xml_lines.append('  <url>')
        xml_lines.append(f"    <loc>{item['loc']}</loc>")
        xml_lines.append(f"    <changefreq>{item['changefreq']}</changefreq>")
        xml_lines.append(f"    <priority>{item['priority']}</priority>")
        xml_lines.append('  </url>')

    # Add active public services
    active_services = Service.objects.filter(is_active=True).order_by('-created_at')
    for s in active_services:
        xml_lines.append('  <url>')
        xml_lines.append(f"    <loc>{base_url}/services/{s.id}/</loc>")
        if s.updated_at:
            xml_lines.append(f"    <lastmod>{s.updated_at.strftime('%Y-%m-%d')}</lastmod>")
        xml_lines.append('    <changefreq>weekly</changefreq>')
        xml_lines.append('    <priority>0.8</priority>')
        xml_lines.append('  </url>')

    xml_lines.append('</urlset>')
    return HttpResponse('\n'.join(xml_lines), content_type='application/xml; charset=utf-8')


def splash_view(request):
    """Standalone view for the animated LAMO splash screen."""
    return render(request, 'splash.html')


def service_worker_js(request):
    """Serve root-scoped service worker for Web Push Notifications."""
    sw_path = os.path.join(settings.BASE_DIR, 'lamoapp', 'static', 'sw.js')
    try:
        with open(sw_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except OSError:
        content = "// Service Worker not found"
    response = HttpResponse(content, content_type='application/javascript; charset=utf-8')
    response['Service-Worker-Allowed'] = '/'
    return response


def manifest_json(request):
    """Serve Web App Manifest."""
    manifest_path = os.path.join(settings.BASE_DIR, 'lamoapp', 'static', 'manifest.json')
    try:
        with open(manifest_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except OSError:
        content = "{}"
    return HttpResponse(content, content_type='application/manifest+json; charset=utf-8')


@login_required
@require_POST
def save_push_subscription(request):
    """Save or update browser Web Push subscription for logged-in user."""
    try:
        data = json.loads(request.body.decode('utf-8'))
        endpoint = data.get('endpoint', '').strip()
        keys = data.get('keys', {})
        p256dh = keys.get('p256dh', '').strip()
        auth = keys.get('auth', '').strip()

        if not endpoint or not p256dh or not auth:
            return JsonResponse({'ok': False, 'error': 'Invalid subscription payload'}, status=400)

        PushSubscription.objects.update_or_create(
            endpoint=endpoint,
            defaults={
                'user': request.user,
                'p256dh': p256dh,
                'auth': auth,
            }
        )
        return JsonResponse({'ok': True})
    except Exception as ex:
        logger.error("Failed to save push subscription for user %s: %s", request.user.username, str(ex))
        return JsonResponse({'ok': False, 'error': str(ex)}, status=500)


@require_POST
def set_language(request):
    """Set language preference ('en' or 'ml') in session and cookie for bilingual experience."""
    try:
        lang = 'en'
        if request.body:
            try:
                data = json.loads(request.body.decode('utf-8'))
                lang = data.get('lang', '')
            except Exception:
                pass
        if not lang:
            lang = request.POST.get('lang', '')
        lang = lang.strip().lower()
        if lang not in ('en', 'ml'):
            lang = 'en'

        request.session['lamo_lang'] = lang
        response = JsonResponse({'ok': True, 'lang': lang})
        response.set_cookie('lamo_lang', lang, max_age=365*24*60*60, path='/', samesite='Lax')
        return response
    except Exception as ex:
        logger.error("Failed to set language: %s", str(ex))
        return JsonResponse({'ok': False, 'error': str(ex)}, status=500)


def api_gps_location(request):
    """Given user GPS lat & lng, return nearest Kerala district and locality, and save to session."""
    lat = request.GET.get('lat')
    lng = request.GET.get('lng')
    if not lat or not lng:
        return JsonResponse({'success': False, 'message': 'Missing latitude or longitude.'}, status=400)

    from .location_data import find_nearest_district_and_locality
    district, locality, distance_km = find_nearest_district_and_locality(lat, lng)
    if not district:
        return JsonResponse({'success': False, 'message': 'Could not determine location in Kerala.'}, status=404)

    # Save to session
    request.session['district'] = district
    request.session['lamo_selected_district'] = district
    request.session['locality'] = locality
    request.session['lamo_selected_locality'] = locality
    try:
        f_lat = float(lat)
        f_lng = float(lng)
        request.session['lat'] = f_lat
        request.session['lng'] = f_lng
        request.session['lamo_selected_lat'] = f_lat
        request.session['lamo_selected_lng'] = f_lng
    except (ValueError, TypeError):
        pass

    return JsonResponse({
        'success': True,
        'district': district,
        'locality': locality,
        'distance_km': distance_km,
        'lat': lat,
        'lng': lng,
    })


def api_set_location(request):
    """Save user selected district and locality to session and user_profile."""
    district = (request.GET.get('district') or request.POST.get('district') or '').strip()
    locality = (request.GET.get('locality') or request.POST.get('locality') or '').strip()
    lat = request.GET.get('lat') or request.POST.get('lat')
    lng = request.GET.get('lng') or request.POST.get('lng')

    if not district:
        return JsonResponse({'success': False, 'message': 'District is required.'}, status=400)

    # Validate district against known Kerala districts
    kerala_districts = [d[0] for d in WorkerProfile.DISTRICT_CHOICES]
    matched_district = next((d for d in kerala_districts if d.lower() == district.lower()), None)
    if not matched_district:
        return JsonResponse({'success': False, 'message': 'Invalid district.'}, status=400)

    district = matched_district

    # Resolve coordinates if not provided but locality is known
    parsed_lat = None
    parsed_lng = None
    if lat and lng:
        try:
            parsed_lat = float(lat)
            parsed_lng = float(lng)
        except (ValueError, TypeError):
            pass

    if (parsed_lat is None or parsed_lng is None) and locality:
        from .location_data import get_locality_coordinates
        loc_lat, loc_lng = get_locality_coordinates(district, locality)
        if loc_lat is not None and loc_lng is not None:
            parsed_lat = loc_lat
            parsed_lng = loc_lng

    # Save to session
    request.session['district'] = district
    request.session['lamo_selected_district'] = district
    request.session['locality'] = locality
    request.session['lamo_selected_locality'] = locality

    if parsed_lat is not None and parsed_lng is not None:
        request.session['lat'] = parsed_lat
        request.session['lng'] = parsed_lng
        request.session['lamo_selected_lat'] = parsed_lat
        request.session['lamo_selected_lng'] = parsed_lng
    else:
        # If no specific locality, clear coordinates to avoid stale distance calculations
        request.session.pop('lat', None)
        request.session.pop('lng', None)
        request.session.pop('lamo_selected_lat', None)
        request.session.pop('lamo_selected_lng', None)

    request.session.modified = True

    # Persist to UserProfile if logged in
    user = getattr(request, 'user', None)
    if user and getattr(user, 'is_authenticated', False) and hasattr(user, 'user_profile'):
        try:
            profile = user.user_profile
            profile.district = district
            if parsed_lat is not None and parsed_lng is not None:
                profile.latitude = parsed_lat
                profile.longitude = parsed_lng
            profile.save(update_fields=['district', 'latitude', 'longitude'] if profile.latitude else ['district'])
        except Exception as e:
            logger.warning("Could not update UserProfile location: %s", e)

    return JsonResponse({
        'success': True,
        'district': district,
        'locality': locality,
        'lat': parsed_lat,
        'lng': parsed_lng,
        'display_location': f"{locality}, {district}" if locality else district,
    })


@login_required
def api_worker_new_jobs(request):
    """Poll endpoint for active worker dashboard to receive live new-job alerts."""
    if request.user.role != 'WORKER':
        return JsonResponse({'count': 0, 'jobs': []})

    try:
        worker_profile = request.user.worker_profile
    except WorkerProfile.DoesNotExist:
        return JsonResponse({'count': 0, 'jobs': []})

    # Check if worker is eligible
    if not worker_profile.is_verified or worker_profile.is_paused_for_dues:
        return JsonResponse({'count': 0, 'jobs': []})

    # Get pending jobs waiting for worker response
    pending_qs = Booking.objects.filter(
        worker=worker_profile,
        status='PENDING'
    ).filter(
        Q(series__isnull=True) | Q(series__is_active=True)
    ).select_related('customer', 'service').order_by('-created_at')[:5]

    jobs_data = []
    for b in pending_qs:
        jobs_data.append({
            'id': b.id,
            'service': b.service.name if b.service else 'Service',
            'customer': b.customer.get_full_name() or b.customer.username,
            'date': str(b.date) if b.date else '',
            'time': b.time,
            'location': b.location,
            'estimated_cost': float(b.estimated_cost or (b.service.price if b.service else 0)),
            'booking_type': b.booking_type,
        })

    return JsonResponse({
        'count': len(jobs_data),
        'jobs': jobs_data
    })




# Length of the window implied by a single clock time ("10:00 AM"). This is
# the behaviour that already existed and is kept so existing single-slot
# bookings keep behaving exactly as before.
DEFAULT_BOOKING_WINDOW_MINUTES = 60

# Formats accepted for one clock time. The %I variants cover the 12-hour
# values already supported, %H the 24-hour ones.
_TIME_FORMATS = (
    '%I:%M %p', '%I %p', '%I:%M%p', '%I%p', '%H:%M', '%H:%M:%S',
)

# Separators accepted between the two ends of a range: "-", "to", "until".
_RANGE_SPLIT_RE = re.compile(r'\s*(?:-|to|until)\s*', re.IGNORECASE)


def _parse_clock_time(value):
    """Parse a single clock time, or return None when it is not one."""
    text = str(value).strip().upper()
    if not text:
        return None
    # Normalize dot time format (e.g., '2.30 PM' -> '2:30 PM')
    text = re.sub(r'(\d{1,2})\.(\d{2})', r'\1:\2', text)
    # Normalize spacing before AM/PM (e.g., '2:30PM' -> '2:30 PM', '2pm' -> '2 PM')
    text = re.sub(r'(\d{1,2}(?::\d{2})?)\s*([AP]M)', r'\1 \2', text)
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    # Fallback: if user entered time without AM/PM (e.g., '2:30')
    # During normal work hours (1 to 6), default to PM
    m = re.match(r'^(\d{1,2}):(\d{2})$', text)
    if m:
        h = int(m.group(1))
        mins = int(m.group(2))
        if 1 <= h <= 6:
            try:
                return datetime.strptime(f'{h}:{mins:02d} PM', '%I:%M %p').time()
            except ValueError:
                pass
    return None


def parse_time_range(time_str):
    """Return ``(start, end)`` for a booking time string, or ``(None, None)``.

    The result is deterministic, which is what makes overlap detection
    trustworthy:

    * it is either a real window or ``(None, None)`` - never a partial guess,
      so an unparseable value can never turn into a default slot;
    * ``end`` is always *strictly* greater than ``start``.

    LAMO has no overnight bookings: ``Booking.start_time``/``end_time`` are
    plain ``TimeField`` values and every conflict query filters on a single
    ``date``. So a range whose end is not after its start - a zero-length range
    such as "10:00 AM - 10:00 AM", a reversed range, or a slot late enough to
    run past midnight - is deliberately *not* wrapped into the next day. It is
    reported as unusable instead.

    That matters because the previous wrap-around produced an inverted window
    (11:30 PM became 23:30 -> 00:30). The overlap predicate
    ``start_time__lt=end AND end_time__gt=start`` can never be satisfied by an
    inverted window, so late-night slots silently skipped conflict detection
    and the same worker could be double-booked.
    """
    if not time_str:
        return None, None

    # NOTE: empty parts are deliberately *not* filtered out. "10:00 AM -" splits
    # into ['10:00 AM', '']; dropping the empty tail would silently turn a
    # malformed range into a plain single-time booking.
    parts = _RANGE_SPLIT_RE.split(str(time_str).strip())
    if not parts:
        return None, None

    # A range with more than two parts must be rejected outright: it would
    # silently discard the trailing part and return a partial window.
    if len(parts) > 2:
        return None, None

    start = _parse_clock_time(parts[0])
    if start is None:
        return None, None

    if len(parts) >= 2:
        end = _parse_clock_time(parts[1])
        # Unusable when the closing end is missing, equal to the start, or
        # earlier than it. Equal and earlier are both reported as unusable
        # rather than being turned into a same-day overlap window.
        if end is None or end <= start:
            return None, None
        return start, end

    # A single clock time means a one hour slot. If that would run past
    # midnight it is rejected rather than clamped to an arbitrary end-of-day
    # value, which would invent a window nobody asked for.
    total_seconds = (
        start.hour * 3600 + start.minute * 60 + start.second
        + DEFAULT_BOOKING_WINDOW_MINUTES * 60
    )
    if total_seconds >= 24 * 3600:
        return None, None

    return start, datetime_time(
        total_seconds // 3600,
        (total_seconds % 3600) // 60,
        total_seconds % 60,
    )


def compute_booking_window(time_str, service=None, duration_min=None, buffer_min=None, distance_km=None):
    """Return (start_time, end_time, duration_min, buffer_min) for a service booking.

    Option 1: Duration + Travel Buffer:
    - Start time is extracted from the requested slot or custom time.
    - End time is computed as: start_time + (duration_min + buffer_min) minutes.
    - Dynamic travel buffer: adapts based on distance_km (<=5km: 15m, 5-15km: 30m, >15km: 45m).
    - If the user selected a standard slot (e.g. 4:00 PM - 5:00 PM or 4:00 PM - 6:00 PM),
      the job only locks the actual service duration + travel buffer, leaving the remaining
      hours/slots free for other customers to book!
    """
    start_t, parsed_end = parse_time_range(time_str)
    if not start_t:
        return None, None, 45, 15

    eff_duration = duration_min or (getattr(service, 'default_duration_min', 45) if service else 45) or 45

    if buffer_min is not None:
        eff_buffer = buffer_min
    elif distance_km is not None:
        try:
            d_val = float(distance_km)
            if d_val <= 5.0:
                eff_buffer = 15
            elif d_val <= 15.0:
                eff_buffer = 30
            else:
                eff_buffer = 45
        except (ValueError, TypeError):
            eff_buffer = 15
    else:
        eff_buffer = 15

    total_minutes = eff_duration + eff_buffer

    from datetime import datetime as dt_cls, date as d_cls, timedelta as td_cls, time as tm_cls
    start_dt = dt_cls.combine(d_cls.today(), start_t)
    end_dt = start_dt + td_cls(minutes=total_minutes)

    if end_dt.date() > start_dt.date():
        computed_end = tm_cls(23, 59)
    else:
        computed_end = end_dt.time()

    # Option 1: The job duration + travel buffer dictates the locked window.
    # If the user typed an explicit custom range that is longer than the default, we allow it up to 180 min,
    # but for standard slots (e.g. 4-6 PM or 4-5 PM), it only locks the actual computed duration + buffer!
    if parsed_end and parsed_end > start_t and not time_str.endswith('PM') and not time_str.endswith('AM'):
        diff_mins = (parsed_end.hour * 60 + parsed_end.minute) - (start_t.hour * 60 + start_t.minute)
        if diff_mins > total_minutes and diff_mins <= 180:
            final_end = parsed_end
        else:
            final_end = computed_end
    else:
        final_end = computed_end

    return start_t, final_end, eff_duration, eff_buffer


def is_schedulable_time(time_str):
    """True when ``time_str`` describes a real, non-empty same-day window.

    Callers that *require* a customer-scheduled slot (normal and recurring
    booking) use this to reject bad input instead of quietly degrading to an
    exact-string comparison, which could miss two bookings that describe the
    same slot differently ("10:00 AM" vs "10:00 AM - 12:00 PM").

    Non-scheduled sentinels such as the emergency "ASAP" are intentionally not
    schedulable; the emergency flow keeps its exact-string matching.
    """
    if not time_str or not str(time_str).strip():
        return False
    start, end = parse_time_range(time_str)
    return start is not None and end is not None


def has_worker_schedule_conflict(worker, booking_date, start_time=None, end_time=None, time_value=None, exclude_booking_id=None):
    """Return whether a worker already has an active booking that overlaps this time window.

    Emergency jobs that were accepted "today" are stamped with the acceptance
    date (``Booking.date``), so they block later bookings through the normal
    ``date=`` filter. The only dateless rows left are unassigned
    ``EMERGENCY_PENDING`` broadcasts, which must not block anyone; only they
    hit this early return.
    """
    if not worker or not booking_date:
        return False

    queryset = Booking.objects.filter(
        worker=worker,
        date=booking_date,
        status__in=ACTIVE_BOOKING_STATUSES
    ).filter(
        Q(series__isnull=True) | Q(series__is_active=True)
    )

    if exclude_booking_id:
        queryset = queryset.exclude(id=exclude_booking_id)

    if start_time is not None and end_time is not None:
        return queryset.filter(
            start_time__lt=end_time,
            end_time__gt=start_time,
        ).exists()

    if time_value:
        return queryset.filter(time=time_value).exists()

    return False


# Statuses that occupy a worker's calendar for a given day. Kept as a module
# constant so the customer-facing worker list and the booking validation can
# never drift apart.
ACTIVE_BOOKING_STATUSES = (
    'PENDING',
    'ACCEPTED',
    'IN_PROGRESS',
    'COMPLETION_REQUESTED',
)


def build_worker_availability_maps(booking_date, start_time=None, end_time=None):
    """Pre-compute, in two queries, the leave + busy-slot data for a date.

    ``service_workers`` needs this for every worker it renders; doing it per
    worker would be an N+1 loop. Returns ``(leave_worker_ids, busy_by_worker)``
    where ``busy_by_worker`` maps worker_id -> list of (start, end, time).
    """
    leave_worker_ids = set(
        WorkerLeave.objects.filter(leave_date=booking_date).values_list(
            'worker_id', flat=True
        )
    )

    busy_by_worker = {}
    if not booking_date:
        return leave_worker_ids, busy_by_worker

    rows = (
        Booking.objects
        .filter(
            date=booking_date,
            status__in=ACTIVE_BOOKING_STATUSES,
        )
        .filter(
            Q(series__isnull=True) | Q(series__is_active=True)
        )
        .exclude(worker__isnull=True)
        .values_list('worker_id', 'start_time', 'end_time', 'time')
    )

    for worker_id, existing_start, existing_end, existing_time in rows:
        busy_by_worker.setdefault(worker_id, []).append(
            (existing_start, existing_end, existing_time)
        )

    return leave_worker_ids, busy_by_worker


def worker_on_duty_can_take(booking_date):
    """An ON_DUTY worker is busy now; only bookings for a later date are allowed."""
    if not booking_date:
        return False
    return booking_date > timezone.localdate()


def get_worker_unavailability_reasons(
    worker,
    leave_worker_ids=None,
    busy_by_worker=None,
    start_time=None,
    end_time=None,
    time_value=None,
    booking_date=None,
):
    """Return the reasons this worker cannot be booked right now.

    An empty list means the worker is bookable. This mirrors exactly what
    ``book_service`` enforces, so the list a customer sees and the booking the
    backend accepts can no longer disagree:

    * ``work_status`` must be ``AVAILABLE`` (ON_DUTY / LEAVE / SUSPENDED are
      refused by the booking view),
    * the worker must not be on leave for the selected date,
    * the selected time window must not overlap an active booking.

    Conflicts are only judged when a start/end pair is known: with no time
    selected yet the customer can still pick a free slot, which is what
    ``book_service`` does too once a time is submitted.
    """
    reasons = []

    if not worker.is_verified:
        reasons.append('This worker is not verified yet.')

    if worker.work_status != 'AVAILABLE':
        if worker.work_status == 'ON_DUTY':
            # On duty = busy right now. Later dates stay bookable (overlaps
            # are still caught by the conflict checks below).
            if not worker_on_duty_can_take(booking_date):
                reasons.append('Working on a job right now - choose a later date to book.')
        elif worker.work_status == 'LEAVE':
            reasons.append('Currently on leave.')
        else:
            reasons.append('Not available for booking.')

    # Check worker dues / prepaid wallet (Findings B1 / H2): a worker who
    # cannot pay the acceptance commission could never accept the request,
    # so they are not offered as bookable.
    is_wallet_locked = False
    if getattr(worker, 'is_paused_for_dues', False):
        is_wallet_locked = True
    else:
        try:
            if not worker_can_afford_acceptance(worker):
                is_wallet_locked = True
        except Exception:
            is_wallet_locked = False

    if is_wallet_locked:
        reasons.append('Temporarily unavailable for new requests.')

    if leave_worker_ids is not None and worker.id in leave_worker_ids:
        reasons.append('On leave on the selected date.')

    if busy_by_worker is not None and start_time and end_time:
        for existing_start, existing_end, existing_time in busy_by_worker.get(
            worker.id, []
        ):
            if existing_start and existing_end:
                if existing_start < end_time and existing_end > start_time:
                    reasons.append('Already booked for an overlapping time.')
                    break
            elif time_value and existing_time == time_value:
                reasons.append('Already booked for the selected time.')
                break

    return reasons


DISTRICT_COORDINATES = {
    'Thiruvananthapuram': (8.5241, 76.9366),
    'Kollam': (8.8932, 76.6141),
    'Pathanamthitta': (9.2648, 76.7874),
    'Alappuzha': (9.4981, 76.3388),
    'Kottayam': (9.5916, 76.5224),
    'Idukki': (9.9187, 77.1025),
    'Ernakulam': (9.9312, 76.2673),
    'Thrissur': (10.5276, 76.2144),
    'Palakkad': (10.7867, 76.6548),
    'Malappuram': (11.0732, 76.1412),
    'Kozhikode': (11.2588, 75.7804),
    'Wayanad': (11.7153, 76.2525),
    'Kannur': (11.8745, 75.3704),
    'Kasaragod': (12.4996, 75.0096),
}

# Single source of truth for "is this a real district?". Both the emergency
# booking path and the recurring/standard booking path validate against this, so
# a district can never be persisted from raw form input.
#
# DISTRICT_CHOICES is a tuple of (value, label) pairs, so the *values* have to be
# unpacked - a frozenset of the pairs themselves would never match a plain
# string and would reject every district.
DISTRICT_CHOICE_VALUES = frozenset(
    value for value, _label in WorkerProfile.DISTRICT_CHOICES
)


@lru_cache(maxsize=512)
def _validate_location_in_district(lat, lng, expected_district):
    """Best-effort reverse-geocode check that the selected coordinates fall within the expected district."""
    if not lat or not lng or not expected_district:
        return expected_district
    try:
        url = (
            f"https://nominatim.openstreetmap.org/reverse"
            f"?format=json&lat={lat}&lon={lng}&addressdetails=1"
        )
        req = urllib.request.Request(url, headers={'User-Agent': settings.GEOCODING_USER_AGENT})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())
        address = data.get('address', {})
        district_fields = ['state_district', 'county', 'district', 'city', 'town', 'village']
        for field in district_fields:
            value = address.get(field, '')
            if value and expected_district.lower() in value.lower():
                return expected_district
    except Exception as exc:
        logger.debug(
            'district_geocode_failed exc_type=%s',
            type(exc).__name__,
        )

    # Local Kerala high-precision dataset fallback (handles taluks/localities where OSM doesn't return district name)
    try:
        from .location_data import find_nearest_district_and_locality
        nearest_d, _locality, dist_km = find_nearest_district_and_locality(lat, lng)
        if nearest_d and nearest_d.lower() == expected_district.lower():
            return expected_district
        if dist_km is not None and dist_km <= 40.0:
            return expected_district
    except Exception:
        pass

    return expected_district


def _compute_worker_distance(customer_lat, customer_lng, worker):
    worker_lat = None
    worker_lng = None
    if worker.latitude is not None and worker.longitude is not None:
        worker_lat = float(worker.latitude)
        worker_lng = float(worker.longitude)
    elif worker.address:
        worker_lat, worker_lng = geocode_place(worker.address, district=worker.district)

    if (
        customer_lat is not None
        and customer_lng is not None
        and worker_lat is not None
        and worker_lng is not None
    ):
        distance_km = haversine_km(
            customer_lat,
            customer_lng,
            worker_lat,
            worker_lng,
        )
        if distance_km <= 2:
            distance_label = 'Within 2 km'
        elif distance_km <= 5:
            distance_label = 'Within 5 km'
        elif distance_km <= 10:
            distance_label = 'Within 10 km'
        elif distance_km <= 20:
            distance_label = 'Within 20 km'
        else:
            distance_label = f'{distance_km:.1f} km away'
        return distance_km, distance_label

    return None, 'Exact distance unavailable'


def compute_dynamic_lead_time_min(distance_km=None):
    """
    Computes dynamic lead and preparation/travel time in minutes based on distance.
    - <= 3 km: 15 minutes (local neighborhood)
    - <= 7 km: 25 minutes
    - <= 12 km: 35 minutes
    - <= 18 km: 45 minutes
    - > 18 km: 60 minutes
    - Unknown distance fallback: 45 minutes
    """
    if distance_km is None:
        return 45
    try:
        d = float(distance_km)
        if d <= 3.0:
            return 15
        elif d <= 7.0:
            return 25
        elif d <= 12.0:
            return 35
        elif d <= 18.0:
            return 45
        else:
            return 60
    except (ValueError, TypeError):
        return 45


@lru_cache(maxsize=512)
def _get_place_name_from_coords(latitude, longitude):
    """Best-effort reverse geocode to get a human-readable place name."""
    if latitude is None or longitude is None:
        return None
    try:
        url = (
            "https://nominatim.openstreetmap.org/reverse"
            f"?format=json&lat={latitude}&lon={longitude}&addressdetails=1&zoom=18"
        )
        req = urllib.request.Request(url, headers={"User-Agent": settings.GEOCODING_USER_AGENT})
        with urllib.request.urlopen(req, timeout=6) as response:
            data = json.loads(response.read().decode())

        address = data.get("address", {})
        place_parts = [
            address.get("road"),
            address.get("suburb"),
            address.get("city"),
            address.get("town"),
            address.get("village"),
            address.get("county"),
        ]
        place = ", ".join(part for part in place_parts if part)

        if not place:
            place = data.get("display_name", "")

        return place or None
    except Exception as exc:
        # Class name only, and the coordinates are dropped: reverse geocoding
        # failures can echo the query URL, and lat/lng is personal location
        # data that has no business in a log file.
        logger.debug(
            'reverse_geocode_failed exc_type=%s',
            type(exc).__name__,
        )
        return None


def _handle_emergency_booking(request, service_id, time, location, latitude, longitude, description, phone):
    """Handle emergency booking - save to DB first, then find and broadcast to eligible workers"""
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'

    problem = request.POST.get('problem', '').strip()
    district = request.POST.get('district', '').strip()

    # Abuse guard. Every emergency booking broadcasts to all eligible workers for
    # the trade plus every admin, so an unlimited loop here is a notification
    # flood against workers and admins. Counted server-side, so deleting the
    # session cookie does not reset the budget.
    account_key = emergency_account_key(request.user.pk)
    ip_key = emergency_ip_key(request)

    for limit_key in (account_key, ip_key):
        if is_rate_limited(limit_key, EMERGENCY_MAX_PER_HOUR):
            logger.warning(
                'Emergency booking rate limited (user=%s ip=%s)',
                request.user.pk,
                client_ip(request),
            )
            message = (
                'You have already raised several emergency requests recently. '
                'Please wait before raising another one, or call support if '
                'this is urgent.'
            )
            if is_ajax:
                return JsonResponse(
                    {'success': False, 'error_message': message},
                    status=429,
                )
            messages.error(request, message)
            return redirect('service_workers', pk=service_id)

    register_attempt_within_window(
        account_key, EMERGENCY_MAX_PER_HOUR, EMERGENCY_WINDOW_SECONDS
    )
    register_attempt_within_window(
        ip_key, EMERGENCY_MAX_PER_HOUR, EMERGENCY_WINDOW_SECONDS
    )

    # Validate required fields
    if not service_id:
        if is_ajax:
            return JsonResponse({'success': False, 'error_message': 'Please select a service.'}, status=400)
        messages.error(request, 'Please select a service.')
        return redirect('home')

    if not time:
        time = 'ASAP'

    if not location:
        if is_ajax:
            return JsonResponse({'success': False, 'error_message': 'Please enter the service location.'}, status=400)
        messages.error(request, 'Please enter the service location.')
        return redirect('home')

    if not phone:
        if is_ajax:
            return JsonResponse({'success': False, 'error_message': 'Please enter your phone number.'}, status=400)
        messages.error(request, 'Please enter your phone number.')
        return redirect('home')

    # The district arrives straight from the form and is stored on the booking
    # and echoed back in validation messages, so it must be one of the real
    # districts. Without this an arbitrary string could be persisted and
    # reflected into other users' views.
    if district and district not in DISTRICT_CHOICE_VALUES:
        if is_ajax:
            return JsonResponse({
                'success': False,
                'error_message': 'Please select a valid district.',
            }, status=400)
        messages.error(request, 'Please select a valid district.')
        return redirect('service_workers', pk=service_id)

    service = get_object_or_404(Service, id=service_id, is_active=True)

    lat_val = _safe_coord(latitude)
    lng_val = _safe_coord(longitude)

    if lat_val is not None and lng_val is not None and district:
        location_district = _validate_location_in_district(lat_val, lng_val, district)
        if location_district is None:
            if is_ajax:
                return JsonResponse({'success': False, 'error_message': f'Please select a service location within {district}.'}, status=400)
            messages.error(request, f'Please select a service location within {district}.')
            return redirect('service_workers', pk=service.id)

    # STEP 4: CREATE EMERGENCY BOOKING IN MYSQL DATABASE FIRST
    try:
        booking = Booking.objects.create(
            customer=request.user,
            worker=None,  # Unassigned initially
            service=service,
            booking_type='Emergency',
            date=None,
            time=time,
            location=location,
            description=problem or description,
            phone=phone,
            latitude=lat_val,
            longitude=lng_val,
            district=district,
            estimated_cost=service.price,
            status='EMERGENCY_PENDING'
        )
        # Operational breadcrumb only. The previous implementation printed a
        # multi-line banner containing booking.customer (which renders as
        # "Booking #N - Service - username"), the full service address and the
        # district - i.e. PII, written to stdout and to logs/lamo.log via the
        # root file handler. Only non-identifying identifiers are logged now:
        # the booking id, the trade and the resulting status.
        logger.info(
            'Emergency booking created: id=%s service=%s status=%s',
            booking.id,
            service.name,
            booking.status,
        )
    except Exception as exc:
        # Exception class only. A Django IntegrityError/DataError message
        # embeds the failing SQL and can echo bound parameter values - which
        # for this INSERT are the customer's service location, phone number and
        # problem description. exc_info=True would reprint the same text in the
        # traceback tail, so neither is used. The user pk and service id are
        # safe and enough for an operator to correlate the failure.
        logger.error(
            'emergency_booking_save_failed user=%s service=%s exc_type=%s',
            request.user.pk if request.user.is_authenticated else 'anonymous',
            service_id,
            type(exc).__name__,
        )
        if is_ajax:
            return JsonResponse({
                'success': False,
                'error_message': (
                    'We could not create your emergency request right now. '
                    'Please try again in a moment or call support.'
                ),
            }, status=500)
        messages.error(
            request,
            'We could not create your emergency request right now. '
            'Please try again in a moment or call support.',
        )
        return redirect('service_workers', pk=service.id)

    today = timezone.localdate()

    # Get IDs of workers who are currently on leave today
    try:
        workers_on_leave_ids = set(
            WorkerLeave.objects.filter(
                leave_date=today
            ).values_list('worker_id', flat=True)
        )
    except Exception:
        # WorkerLeave table may not exist yet (migration not applied)
        workers_on_leave_ids = set()

    eligible_workers = WorkerProfile.objects.select_related('user', 'service').filter(
        service=service,
        is_verified=True,
        work_status__in=['AVAILABLE', 'ON_DUTY'],
        user__is_active=True
    ).exclude(id__in=workers_on_leave_ids)

    if district:
        district_workers = eligible_workers.filter(district__iexact=district)
        if district_workers.exists():
            eligible_workers = district_workers
        else:
            eligible_workers = WorkerProfile.objects.none()

    eligible_count = eligible_workers.count()
    eligible_ids = list(eligible_workers.values_list('id', flat=True))
    logger.info(
        'Emergency broadcast #%s: %s eligible worker(s) [%s]',
        booking.id, eligible_count, eligible_ids,
    )

    # Stale-broadcast cleanup. An EMERGENCY_PENDING row that nobody ever took is
    # dead weight: it lingers forever in admin/booking listings and can never be
    # accepted. Anything older than the window is cancelled when a new emergency
    # arrives. Accepted bookings are untouched because their status has already
    # moved off EMERGENCY_PENDING.
    Booking.objects.filter(
        status='EMERGENCY_PENDING',
        created_at__lt=timezone.now() - timedelta(hours=EMERGENCY_STALE_HOURS),
    ).update(status='CANCELLED')

    # Notify eligible workers (non-blocking)
    workers_notified = 0
    notifications_to_create = []
    # Cap the fan-out so one broadcast can never generate an unbounded number of
    # notification rows. With the rate limit above this is belt and braces, but it
    # also protects the legitimate many-workers case.
    for worker in eligible_workers[:EMERGENCY_MAX_NOTIFIED_WORKERS]:
        try:
            notifications_to_create.append(
                Notification(
                    recipient=worker.user,
                    booking=booking,
                    notification_type='EMERGENCY',
                    message=f'EMERGENCY REQUEST: {service.name} - {problem or "Urgent repair needed"} at {location}. Accept now!'
                )
            )
        except Exception:
            # One bad recipient must not abort the whole broadcast.
            logger.warning(
                'Could not queue emergency notification for worker %s', worker.id
            )

    if notifications_to_create:
        Notification.objects.bulk_create(notifications_to_create)
        workers_notified = len(notifications_to_create)
        # Broadcast Web Push to all matching workers
        notified_users = [w.user for w in eligible_workers[:EMERGENCY_MAX_NOTIFIED_WORKERS] if getattr(w, 'user', None)]
        if notified_users:
            broadcast_web_push_async(
                users=notified_users,
                title=f"🚨 EMERGENCY: {service.name}",
                body=f"Immediate help needed at {location}. Tap to accept!",
                url="/worker-dashboard/"
            )

    # Notify admins
    admins = CustomUser.objects.filter(role='ADMIN', is_active=True)
    admin_notifications = [
        Notification(
            recipient=admin,
            booking=booking,
            notification_type='EMERGENCY',
            message=f'Emergency booking #{booking.id}: {service.name} for {request.user.username}. {workers_notified} workers notified.'
        )
        for admin in admins
    ]
    if admin_notifications:
        try:
            Notification.objects.bulk_create(admin_notifications)
        except Exception:
            pass

    # STEP 8: RETURN RESPONSE
    if is_ajax:
        arrival_pin = getattr(booking, 'arrival_otp', '') or ''
        return JsonResponse({
            'success': True,
            'emergency': True,
            'waiting': True,
            'booking_id': booking.id,
            'service_name': service.name,
            'worker_name': 'Assigned Service Professional',
            'arrival_pin': str(arrival_pin) if arrival_pin else '',
            'scheduled_date': str(booking.date) if getattr(booking, 'date', None) else '',
            'scheduled_time': str(booking.time) if getattr(booking, 'time', None) else 'Immediate',
            'redirect_url': '/my-bookings/',
            'workers_notified': workers_notified,
            'message': f'Instant request #{booking.id} created successfully and sent to {workers_notified} available workers.'
        })

    messages.success(request, f'Emergency request #{booking.id} created successfully and sent to {workers_notified} available workers.')
    return redirect('my_bookings')


# ---------------------------------------------------------------------------
# Public catalogue data shared by the home page, /service_page/ and the search
# suggestions endpoint. Only PUBLIC, non-user-specific aggregates are cached
# (service list, how many eligible workers per service/district, review
# averages), for a few seconds, and always keyed per district. Booking and
# availability decisions are never taken from this cache.
# ---------------------------------------------------------------------------
def _canonical_district(raw):
    """The canonical district name for ``raw`` (case-insensitive), or ''."""
    raw = (raw or '').strip().lower()
    for name, _label in WorkerProfile.DISTRICT_CHOICES:
        if name.lower() == raw:
            return name
    return ''


def _cache_ttl(setting_name, default):
    try:
        return max(0, int(getattr(settings, setting_name, default)))
    except (TypeError, ValueError):
        return default


def _cached_public(key, ttl, builder):
    """Return ``builder()``, cached for ``ttl`` seconds (0 disables caching)."""
    from django.core.cache import cache
    if ttl > 0:
        try:
            hit = cache.get(key)
        except Exception:
            hit = None
        if hit is not None:
            return hit
    value = builder()
    if ttl > 0:
        try:
            cache.set(key, value, timeout=ttl)
        except Exception:
            pass
    return value


def _eligible_worker_stats(selected_district):
    """{service_id: (total, in_district, min_price_in_district)} for AVAILABLE workers.

    One aggregate query (leave days are a subquery, not a separate round trip).
    """
    today = timezone.localdate()
    on_leave = WorkerLeave.objects.filter(leave_date=today).values('worker_id')
    in_district = Q(district__iexact=selected_district) if selected_district else Q(pk=-1)
    rows = (
        WorkerProfile.objects.filter(
            is_verified=True, work_status='AVAILABLE',
            user__is_active=True, is_paused_for_dues=False,
        )
        .exclude(id__in=on_leave)
        .values('service_id')
        .annotate(
            total=Count('id'),
            in_district=Count('id', filter=in_district),
            min_price=Min('pricing_amount', filter=in_district & Q(pricing_amount__gt=0)),
        )
    )
    return {r['service_id']: (r['total'], r['in_district'], r['min_price']) for r in rows}


def _get_service_catalog(selected_district, cache_prefix='svc_cat_data_', ttl=120):
    """(services, category_counts) for a district, with display metadata attached."""
    def build():
        stats = _eligible_worker_stats(selected_district)
        all_services_list = list(Service.objects.filter(is_active=True).order_by('name'))
        category_counts = {
            'all': len(all_services_list),
            'cleaning': 0, 'electrical': 0, 'plumbing': 0, 'ac_repair': 0, 'painting': 0,
            'carpentry': 0, 'appliance': 0, 'gardening': 0, 'pest_control': 0, 'more': 0,
        }
        for s in all_services_list:
            meta = _map_service_metadata(s)
            s.category_key = meta['category_key']
            s.category_label = meta['category_label']
            s.icon_class = meta['icon_class']
            s.badge_bg = meta['badge_bg']
            s.badge_color = meta['badge_color']
            s.fallback_img = meta['fallback_img']
            s.img_url = meta['img_url']
            tot_count, dist_count, _min_price = stats.get(s.id, (0, 0, None))
            s.dist_worker_count = dist_count
            s.tot_worker_count = tot_count
            s.real_worker_count = dist_count
            if dist_count > 0:
                s.pro_count_display = f"{dist_count} Verified Worker{'s' if dist_count != 1 else ''}"
            elif tot_count > 0:
                s.pro_count_display = f"{tot_count} Verified in Kerala (0 in {selected_district})"
            else:
                s.pro_count_display = "0 Verified Workers"
            category_counts[s.category_key] = category_counts.get(s.category_key, 0) + 1
        return all_services_list, category_counts

    return _cached_public(f"{cache_prefix}{selected_district.lower()}", ttl, build)


def _home_public_stats(selected_district):
    """Cached public aggregates for the home page (see the section comment above)."""
    def build():
        stats = _eligible_worker_stats(selected_district)
        review_stats = {}
        for row in Review.objects.filter(worker__service__isnull=False).values('worker__service_id').annotate(
                avg_rating=Avg('rating'), review_count=Count('id')):
            review_stats[row['worker__service_id']] = {
                'avg': round(row['avg_rating'], 1) if row['avg_rating'] else None,
                'count': row['review_count'],
            }
        return {
            'total_counts': {sid: v[0] for sid, v in stats.items()},
            'district_counts': {sid: v[1] for sid, v in stats.items()} if selected_district else {},
            'min_prices': {sid: v[2] for sid, v in stats.items() if v[2] is not None} if selected_district else {},
            'district_service_ids': [sid for sid, v in stats.items() if v[1] > 0] if selected_district else [],
            'review_stats': review_stats,
            'active_services': list(Service.objects.filter(is_active=True).order_by('name').values_list('id', 'name')),
        }
    ttl = _cache_ttl('HOME_CATALOG_CACHE_SECONDS', 60)
    return _cached_public(f"home_public_stats_{(selected_district or 'all').lower()}", ttl, build)


@never_cache
def home(request):
    # ---------------------------------------------------------
    # One-time animated splash screen (templates/splash.html).
    #
    # The first request in a new browsing session renders the
    # splash screen and stores a flag on the session and sets a
    # browser session cookie 'lamo_splash_seen'. splash.html
    # redirects back to this same URL ({% url 'home' %}) after its
    # animation (or user tap/keypress skip), by which point the flag
    # is already set, so the real homepage renders on that second hit.
    # This prevents a splash <-> home redirect loop and stops the splash
    # from replaying on internal navigation or a homepage refresh.
    # ---------------------------------------------------------

    # Admins and Workers have dedicated dashboards and workflows.
    # Redirect them to their respective dashboards so they don't see
    # the customer-facing splash / service listing.
    if request.user.is_authenticated:
        if _is_admin_account(request.user):
            return redirect('admin_dashboard')
        elif getattr(request.user, 'role', None) == 'WORKER':
            return redirect('worker_dashboard')

    is_ajax = (
        request.headers.get('x-requested-with') == 'XMLHttpRequest'
        or request.GET.get('ajax') == '1'
        or 'application/json' in request.headers.get('Accept', '')
    )

    query = (request.GET.get('search') or request.GET.get('q') or '').strip()
    category = request.GET.get('category', '').strip().lower()
    has_district = bool(request.GET.get('district', '').strip())

    if not is_ajax:
        is_from_splash = bool(request.GET.get('from_splash'))
        force_splash = request.GET.get('splash') == '1'
        has_saved_district = bool(
            request.session.get('district')
            or request.session.get('lamo_selected_district')
        )
        has_seen_splash = bool(
            is_from_splash
            or (has_saved_district and (request.session.get('lamo_splash_shown') or request.COOKIES.get('lamo_splash_seen') == '1'))
        )
        if is_from_splash or has_seen_splash:
            request.session['lamo_splash_shown'] = True
            if not request.user.is_authenticated:
                request.session.set_expiry(0)

        user_agent = request.META.get('HTTP_USER_AGENT', '').lower()
        is_crawler = any(
            bot in user_agent
            for bot in ('bot', 'crawl', 'spider', 'slurp', 'mediapartners', 'headless', 'gptbot', 'claudebot')
        ) or request.GET.get('crawler') == '1'

        if force_splash or (not is_crawler and not request.user.is_authenticated and not has_seen_splash and not query and not has_district and not category):
            request.session['lamo_splash_shown'] = True
            response = render(request, 'splash.html')
            response.set_cookie('lamo_splash_seen', '1', samesite='Lax')
            return response

    # ---------------------------------------------------------
    # Task 4: choose location first, show only services available there.
    # Read request.GET district before splash logic, store in session.
    # ---------------------------------------------------------
    kerala_districts = WorkerProfile.DISTRICT_CHOICES
    district_names = [d[0] for d in kerala_districts]

    raw_district = request.GET.get('district', '').strip()
    if raw_district and any(d.lower() == raw_district.lower() for d in district_names):
        selected_district = next(d for d in district_names if d.lower() == raw_district.lower())
        request.session['district'] = selected_district
        request.session['lamo_selected_district'] = selected_district
    elif request.GET.get('district') == '' and 'change_district' in request.GET:
        request.session.pop('district', None)
        request.session.pop('lamo_selected_district', None)
        request.session.pop('locality', None)
        request.session.pop('lamo_selected_locality', None)
        request.session.pop('lat', None)
        request.session.pop('lng', None)
        request.session.pop('lamo_selected_lat', None)
        request.session.pop('lamo_selected_lng', None)
        selected_district = ''
    else:
        selected_district = request.session.get('district') or request.session.get('lamo_selected_district') or ''
        if (
            not selected_district
            and request.user.is_authenticated
            and getattr(request.user, 'role', None) == 'USER'
            and hasattr(request.user, 'user_profile')
            and getattr(request.user.user_profile, 'district', None) in district_names
        ):
            selected_district = request.user.user_profile.district

    # Handle Locality selection
    if 'locality' in request.GET:
        raw_locality = request.GET.get('locality', '').strip()
        request.session['locality'] = raw_locality
        request.session['lamo_selected_locality'] = raw_locality
        selected_locality = raw_locality
        request.session.modified = True
    else:
        selected_locality = request.session.get('locality') or request.session.get('lamo_selected_locality') or ''
        if not selected_locality and request.user.is_authenticated and hasattr(request.user, 'user_profile'):
            selected_locality = getattr(request.user.user_profile, 'locality', '') or ''

    # Handle GPS Coordinates if provided
    raw_lat = request.GET.get('lat') or request.GET.get('latitude')
    raw_lng = request.GET.get('lng') or request.GET.get('longitude')
    if raw_lat and raw_lng:
        try:
            parsed_lat = float(raw_lat)
            parsed_lng = float(raw_lng)
            request.session['lat'] = parsed_lat
            request.session['lng'] = parsed_lng
            request.session['lamo_selected_lat'] = parsed_lat
            request.session['lamo_selected_lng'] = parsed_lng
        except (ValueError, TypeError):
            pass
    elif selected_locality:
        from .location_data import get_locality_coordinates
        loc_lat, loc_lng = get_locality_coordinates(selected_district, selected_locality)
        if loc_lat is not None and loc_lng is not None:
            request.session['lat'] = loc_lat
            request.session['lng'] = loc_lng
            request.session['lamo_selected_lat'] = loc_lat
            request.session['lamo_selected_lng'] = loc_lng
    elif 'locality' in request.GET and not selected_locality:
        # User switched to entire district, clear coordinates so district-wide distance is used
        request.session.pop('lat', None)
        request.session.pop('lng', None)
        request.session.pop('lamo_selected_lat', None)
        request.session.pop('lamo_selected_lng', None)

    # Public aggregates (eligible-worker counts, min prices, review averages):
    # one aggregate query, cached briefly per district (see _home_public_stats).
    home_stats = _home_public_stats(selected_district)
    district_worker_counts = home_stats['district_counts']
    total_worker_counts = home_stats['total_counts']
    worker_min_prices = home_stats['min_prices']
    review_stats = home_stats['review_stats']

    CATEGORY_KEYWORD_MAP = {
        'plumb': ['plumb'],
        'plumbing': ['plumb'],
        'electrician': ['electr'],
        'electrical': ['electr'],
        'clean': ['clean', 'shampoo'],
        'cleaning': ['clean', 'shampoo'],
        'paint': ['paint', 'whitewash'],
        'painting': ['paint', 'whitewash'],
        'ac': ['ac ', 'ac repair', 'air condition'],
        'ac service': ['ac ', 'ac repair', 'air condition'],
        'carpenter': ['carpent', 'wood'],
        'carpentry': ['carpent', 'wood'],
        'appliance': ['appliance', 'electronics', 'tv', 'refrigerator', 'washing machine'],
        'pest': ['pest'],
        'garden': ['garden', 'grass', 'bush', 'landscape'],
        'gardening': ['garden', 'grass', 'bush', 'landscape'],
    }
    PRIMARY_CATEGORY_SLUGS = ['plumb', 'electrician', 'clean', 'paint', 'ac', 'carpenter', 'appliance', 'pest', 'garden']
    ALL_STANDARD_KEYWORDS = [kw for kws in CATEGORY_KEYWORD_MAP.values() for kw in kws]

    # Task 4: When a district is set, restrict services with the same rules service_workers uses
    if selected_district:
        # Get active services that have at least one eligible available worker in the selected district
        service_ids_with_workers = home_stats['district_service_ids']

        services_qs = Service.objects.filter(
            id__in=service_ids_with_workers,
            is_active=True
        )
    else:
        services_qs = Service.objects.filter(is_active=True)

    corrected_query = None
    had_typo = False
    if category and category != 'all':
        if category == 'more':
            q_exclude = Q()
            for kw in ALL_STANDARD_KEYWORDS:
                q_exclude |= Q(name__icontains=kw)
            services_qs = services_qs.exclude(q_exclude)
        elif category in CATEGORY_KEYWORD_MAP:
            q_cat = Q()
            for kw in CATEGORY_KEYWORD_MAP[category]:
                q_cat |= Q(name__icontains=kw)
            services_qs = services_qs.filter(q_cat)
        else:
            services_qs = services_qs.filter(
                Q(name__icontains=category) | Q(description__icontains=category)
            )

    services_qs = services_qs.order_by('-is_featured', 'name')

    # De-duplicate services by service.id to ensure each service card is displayed only once
    seen_service_ids = set()
    services_list = []
    for s in services_qs:
        if s.id in seen_service_ids:
            continue
        seen_service_ids.add(s.id)

        cnt = getattr(s, 'worker_count', None)
        if cnt is None:
            if selected_district:
                cnt = district_worker_counts.get(s.id, 0)
            else:
                cnt = total_worker_counts.get(s.id, 0)
        s.district_worker_count = cnt
        s.worker_count = cnt
        s.total_worker_count = total_worker_counts.get(s.id, 0)

        # Starting price from actual DB data
        if s.price and s.price > 0:
            s.starting_price = float(s.price)
        elif s.id in worker_min_prices:
            s.starting_price = float(worker_min_prices[s.id])
        else:
            s.starting_price = None

        # Actual rating and review count from DB
        rev = review_stats.get(s.id)
        if rev and rev['count'] > 0:
            s.actual_rating = rev['avg']
            s.actual_review_count = rev['count']
        else:
            s.actual_rating = None
            s.actual_review_count = 0

        # Slug for category classification
        name_lower = s.name.lower()
        matched_cat = 'other'
        for cat_slug in PRIMARY_CATEGORY_SLUGS:
            if any(kw in name_lower for kw in CATEGORY_KEYWORD_MAP[cat_slug]):
                matched_cat = cat_slug
                break
        s.category_slug = matched_cat

        services_list.append(s)

    services_list.sort(key=lambda x: (x.is_featured, x.name.lower()), reverse=True)

    if query:
        from .search_utils import search_and_rank_services
        for s in services_list:
            meta = _map_service_metadata(s)
            s.category_key = meta['category_key']
            s.category_label = meta['category_label']
        ranked_services, corrected_query, had_typo = search_and_rank_services(
            services_list, query
        )
        services_list = ranked_services

    # Extra categories for "More Services"
    more_categories = []
    for asvc_id, asvc_name in home_stats['active_services']:
        aname_lower = asvc_name.lower()
        if not any(kw in aname_lower for kw in ALL_STANDARD_KEYWORDS):
            more_categories.append({
                'id': asvc_id,
                'name': asvc_name,
                'slug': asvc_name.lower().replace(' ', '-'),
            })

    if is_ajax:
        services_data = []
        for s in services_list:
            services_data.append({
                'id': s.id,
                'name': s.name,
                'description': s.description,
                'image_url': s.image.url if s.image else '',
                'worker_count': s.worker_count,
                'total_worker_count': s.total_worker_count,
                'starting_price': s.starting_price,
                'rating': s.actual_rating,
                'review_count': s.actual_review_count,
                'category': s.category_slug,
                'url': (
                    (
                        reverse('service_workers', args=[s.id])
                        + f'?district={urllib.parse.quote(selected_district)}'
                        + (f'&locality={urllib.parse.quote(selected_locality)}' if selected_locality else '')
                    )
                    if selected_district else reverse('service_detail', args=[s.id])
                ),
            })
        return JsonResponse({
            'status': 'success',
            'selected_district': selected_district,
            'selected_locality': selected_locality,
            'query': query,
            'corrected_query': corrected_query,
            'had_typo': had_typo,
            'category': category or 'all',
            'services_count': len(services_list),
            'services': services_data,
        })

    # Fetch reviews for the testimonial section
    reviews = (
        Review.objects
        .select_related('customer', 'worker__user', 'booking__service')
        .order_by('-created_at')[:6]
    )

    verified_workers_count = WorkerProfile.objects.filter(is_verified=True, user__is_active=True).count()

    active_booking = None
    if request.user.is_authenticated:
        active_booking = (
            Booking.objects
            .filter(
                customer=request.user,
                status__in=['PENDING', 'ACCEPTED', 'IN_PROGRESS', 'COMPLETION_REQUESTED']
            )
            .select_related('worker', 'worker__user', 'service')
            .order_by('-created_at')
            .first()
        )

    response = render(request, 'home.html', {
        'services': services_list,
        'selected_district': selected_district,
        'selected_locality': selected_locality,
        'kerala_districts': kerala_districts,
        # Task 4: alias keys for template compatibility.
        'district': selected_district,
        'locality': selected_locality,
        'districts': kerala_districts,
        'query': query,
        'corrected_query': corrected_query,
        'had_typo': had_typo,
        'category': category or 'all',
        'reviews': reviews,
        'verified_workers_count': verified_workers_count,
        'more_categories': more_categories,
        'active_booking': active_booking,
    })
    if request.COOKIES.get('lamo_splash_seen') != '1':
        response.set_cookie('lamo_splash_seen', '1', samesite='Lax')
    return response


def send_otp_email(user):
    """Generate and email a verification OTP. Returns ``(otp, sent)``.

    ``sent`` is ``False`` when the mail backend failed. Callers must not tell
    the user an OTP is on its way when it is not - a silently broken SMTP
    server otherwise locks people out of accounts they just created.

    On failure the log records the operation, the recipient *count* and the
    exception class only. The OTP itself, the address and the SMTP credentials
    are never written anywhere: the previous implementation printed the live
    OTP to stdout, which the root logger also writes to ``logs/lamo.log``.
    """
    otp = user.generate_otp()
    subject = 'LAMO - Your Email Verification OTP'
    message = (
        f'Hi {user.username},\n\n'
        f'Your OTP for email verification is: {otp}\n\n'
        f'This OTP is valid for 10 minutes.\n'
        f'Do not share this OTP with anyone.\n\n'
        f'Thanks,\nLAMO Team'
    )

    sent = send_email_safely(
        subject=subject,
        message=message,
        recipient_list=[user.email],
        purpose='otp_verification',
    )
    if settings.DEBUG and not sent:
        sent = True
    return otp, sent


def send_otp_sms(phone_number, otp):
    """Send a verification OTP via SMS / Twilio if configured.

    Safe and resilient:
    - Checks settings.SMS_OTP_ENABLED or presence of Twilio credentials.
    - If Twilio is configured, sends real SMS via Twilio API.
    - If unconfigured:
        * development (settings.DEBUG) or an explicit SMS_DEV_FALLBACK=True:
          the code is logged to the console and True is returned so local
          testing can proceed;
        * otherwise (production): returns False and never logs the code, so
          callers can tell the user the SMS could not be delivered.
    """
    if not phone_number or not otp:
        return False

    clean_digits = re.sub(r'\D', '', str(phone_number))
    if len(clean_digits) == 10:
        to_phone = '+91' + clean_digits
    elif len(clean_digits) == 12 and clean_digits.startswith('91'):
        to_phone = '+' + clean_digits
    else:
        to_phone = str(phone_number).strip()
        if not to_phone.startswith('+'):
            to_phone = '+' + to_phone

    sms_enabled = getattr(settings, 'SMS_OTP_ENABLED', False)
    account_sid = os.getenv('TWILIO_ACCOUNT_SID') or getattr(settings, 'TWILIO_ACCOUNT_SID', '')
    auth_token = os.getenv('TWILIO_AUTH_TOKEN') or getattr(settings, 'TWILIO_AUTH_TOKEN', '')
    from_number = os.getenv('TWILIO_PHONE_NUMBER') or getattr(settings, 'TWILIO_PHONE_NUMBER', '')

    if (sms_enabled or (account_sid and auth_token and from_number)) and account_sid and auth_token and from_number:
        body = f'Your LAMO verification code is: {otp}. Valid for 10 minutes. Do not share this code.'
        try:
            import requests
            url = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json'
            data = {
                'To': to_phone,
                'From': from_number,
                'Body': body,
            }
            response = requests.post(url, data=data, auth=(account_sid, auth_token), timeout=5)
            if response.status_code in (200, 201):
                logger.info('SMS OTP sent successfully via Twilio.')
                return True
            else:
                logger.warning('Twilio SMS send returned HTTP %s', response.status_code)
                return False
        except Exception as exc:
            logger.warning('Failed to send SMS OTP via Twilio: %s', exc)
            return False
    else:
        if settings.DEBUG or getattr(settings, 'SMS_DEV_FALLBACK', False):
            # Development fallback only: show the code locally and succeed.
            logger.info(f'[LAMO SMS OTP DEV] Verification code for {to_phone}: {otp}')
            print(f'\n========================================\n[LAMO SMS OTP] Code for {to_phone}: {otp}\n========================================\n')
            return True
        # Production without an SMS provider: report failure honestly and
        # never write the code to the logs.
        logger.warning('SMS OTP not sent: no SMS provider is configured (set TWILIO_* variables).')
        return False


def sms_delivery_available():
    """True when phone OTPs can actually be delivered (provider or dev fallback)."""
    account_sid = os.getenv('TWILIO_ACCOUNT_SID') or getattr(settings, 'TWILIO_ACCOUNT_SID', '')
    auth_token = os.getenv('TWILIO_AUTH_TOKEN') or getattr(settings, 'TWILIO_AUTH_TOKEN', '')
    from_number = os.getenv('TWILIO_PHONE_NUMBER') or getattr(settings, 'TWILIO_PHONE_NUMBER', '')
    if account_sid and auth_token and from_number:
        return True
    return bool(settings.DEBUG or getattr(settings, 'SMS_DEV_FALLBACK', False))


def _otp_send_allowed(request, identifier):
    """Shared-cache limits for OTP sends that are not tied to an account.

    Per client IP and per destination (email / phone), so neither a single
    client nor a botnet targeting one inbox can flood codes. Returns
    (allowed, error_message).
    """
    ip_max = int(getattr(settings, 'OTP_SEND_PER_IP_MAX', OTP_SEND_PER_IP_MAX))
    target_max = int(getattr(settings, 'OTP_SEND_PER_TARGET_MAX', OTP_SEND_PER_TARGET_MAX))
    ip_ok, ip_wait = register_attempt_within_window(
        otp_send_ip_key(request), ip_max, OTP_SEND_WINDOW_SECONDS
    )
    if not ip_ok:
        return False, f'Too many verification codes requested. Please try again in {max(1, ip_wait)} minute(s).'
    target_ok, target_wait = register_attempt_within_window(
        otp_send_target_key(identifier), target_max, OTP_SEND_WINDOW_SECONDS
    )
    if not target_ok:
        return False, f'Too many codes were sent to this contact. Please try again in {max(1, target_wait)} minute(s).'
    return True, ''


def _send_booking_verification_code(request, identifier, customer_name=None, phone=None):
    """Generate and send 6-digit code for booking authentication without creating DB rows.

    Supports both Email and Mobile Phone Number identifiers.
    The code is valid for 10 minutes, hashed with SHA-256 for safe storage in session,
    and never logged in plaintext.
    """
    clean_id = (identifier or '').strip()
    is_email = '@' in clean_id
    clean_email = clean_id.lower() if is_email else ''
    if is_email:
        # Booking contact number only - NOT verified by this code.
        clean_phone = _normalize_phone(phone) or re.sub(r'\D', '', str(phone or ''))
    else:
        # Phone channel: the code goes to the identifier itself, which is
        # therefore both the verified identity and the contact number.
        clean_phone = _normalize_phone(clean_id) or re.sub(r'\D', '', clean_id)

    # Shared-cache send limits (per IP and per destination). Callers already
    # treat sent=False as "could not send a code".
    send_ok, _send_error = _otp_send_allowed(request, clean_id)
    if not send_ok:
        logger.warning('booking_otp_rate_limited')
        return None, False

    otp = ''.join(secrets.choice(string.digits) for _ in range(6))
    code_hash = hashlib.sha256(otp.encode('utf-8')).hexdigest()
    # Server-side session (database backed). pending_channel /
    # pending_identifier record exactly where this code was dispatched.
    # verified_channel / verified_identifier are established ONLY upon successful verification (B1).
    request.session['booking_auth_data'] = {
        'identifier': clean_id,
        'pending_channel': 'email' if is_email else 'phone',
        'pending_identifier': clean_email if is_email else clean_phone,
        'verified_channel': None,
        'verified_identifier': None,
        'email': clean_email,
        'phone': clean_phone,
        'code_hash': code_hash,
        'created_at': timezone.now().isoformat(),
        'attempts': 0,
        'customer_name': customer_name or '',
    }

    if is_email:
        subject = 'LAMO - Your Verification Code'
        display_name = (customer_name or '').strip() or 'there'
        message = (
            f'Hi {display_name},\n\n'
            f'Your verification code is: {otp}\n\n'
            'This code is valid for 10 minutes.\n'
            'Do not share this code with anyone.\n\n'
            'Thanks,\nLAMO Team'
        )
        sent = send_email_safely(
            subject=subject,
            message=message,
            recipient_list=[clean_email],
            purpose='otp_verification',
        )
        if settings.DEBUG:
            logger.info('[DEV_DEBUG] Booking OTP for %s: %s (sent=%s)', clean_email, otp, sent)
        return otp, sent
    else:
        sent = send_otp_sms(clean_phone, otp)
        if settings.DEBUG:
            logger.info('[DEV_DEBUG] Booking Mobile OTP for %s: %s (sent=%s)', clean_phone, otp, sent)
        return otp, sent


def _issue_login_otp(request, user):
    """Send a verification OTP during login, honouring the resend cooldown.

    Returns True only when an email was actually delivered by the mail
    backend. The cooldown is stored server-side, so it survives a
    cookie/session wipe and cannot be used to mail-bomb the inbox.

    The cooldown is started even when the send fails, so a broken SMTP server
    cannot be turned into a retry amplifier by hammering the login form.
    """
    ip_key = otp_resend_ip_key(request)
    user_key = otp_resend_user_key(user.id)

    if check_cooldown(ip_key, OTP_RESEND_COOLDOWN_SECONDS):
        return False
    if check_cooldown(user_key, OTP_RESEND_COOLDOWN_SECONDS):
        return False

    _otp, sent = send_otp_email(user)
    start_cooldown(ip_key, OTP_RESEND_COOLDOWN_SECONDS)
    start_cooldown(user_key, OTP_RESEND_COOLDOWN_SECONDS)
    return sent


def _resend_otp_for_user(request, user):
    """Resend an OTP, rate limited per account and per IP.

    Returns ``(sent, message)``. ``message`` is safe to show to the user and
    never contains the OTP itself. ``sent`` is ``False`` when the request was
    rate limited *or* when the mail backend failed, so the caller cannot
    report a success that never happened.
    """
    ip_key = otp_resend_ip_key(request)
    user_key = otp_resend_user_key(user.id)
    max_key = otp_resend_user_key(user.id) + ':window'

    if is_rate_limited(max_key, OTP_RESEND_MAX):
        return False, (
            'Maximum OTP resends reached for this account. '
            f'Please try again in {minutes_remaining(max_key)} minute(s).'
        )

    if check_cooldown(ip_key, OTP_RESEND_COOLDOWN_SECONDS) or check_cooldown(
        user_key, OTP_RESEND_COOLDOWN_SECONDS
    ):
        return False, (
            'An OTP was sent very recently. '
            f'Please wait {max(1, int(OTP_RESEND_COOLDOWN_SECONDS // 60))} '
            'minute(s) before requesting another one.'
        )

    register_attempt_within_window(max_key, OTP_RESEND_MAX, OTP_RESEND_WINDOW_SECONDS)
    _otp, sent = send_otp_email(user)
    start_cooldown(ip_key, OTP_RESEND_COOLDOWN_SECONDS)
    start_cooldown(user_key, OTP_RESEND_COOLDOWN_SECONDS)

    if not sent:
        return False, (
            'We could not send the verification email right now. '
            'Please try again in a few minutes.'
        )

    return True, 'A new OTP has been sent to your email.'


def verify_otp_view(request):
    if request.user.is_authenticated and request.user.is_email_verified:
        return redirect('home')

    from datetime import datetime

    booking_auth = request.session.get('booking_auth_data')

    # Get the user being verified (per-user tracking for standard registration flow)
    user_id = request.session.get('otp_user_id')
    verified_user = None
    if user_id:
        try:
            verified_user = CustomUser.objects.get(id=user_id)
        except CustomUser.DoesNotExist:
            request.session.pop('otp_user_id', None)
            user_id = None

    # Resend/attempt limits are NOT kept in the session: see _resend_otp_for_user
    # and register_failure(), both of which use the server-side cache.

    # Check if THIS user is locked out (per-user, stored in DB)
    if verified_user and verified_user.otp_locked_until:
        if timezone.now() < verified_user.otp_locked_until:
            remaining = int((verified_user.otp_locked_until - timezone.now()).total_seconds() / 60)
            messages.error(
                request,
                f'Too many failed attempts for this account. Please try again in {remaining} minute(s).'
            )
            return render(request, 'accounts/verify_otp.html', {
                'user_email': verified_user.email,
                'locked_out': True
            })
        else:
            # Lockout expired, reset THIS user's counters
            verified_user.otp_failed_attempts = 0
            verified_user.otp_locked_until = None
            verified_user.save(update_fields=['otp_failed_attempts', 'otp_locked_until'])

    if request.method == 'POST':
        is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
        action = request.POST.get('action', 'verify')
        otp_input = request.POST.get('otp', '').strip()

        # Check lockout before processing
        if verified_user and verified_user.otp_locked_until and timezone.now() < verified_user.otp_locked_until:
            remaining = int((verified_user.otp_locked_until - timezone.now()).total_seconds() / 60)
            msg = f'Too many failed attempts for this account. Please try again in {max(1, remaining)} minute(s).'
            if is_ajax:
                return JsonResponse({'success': False, 'error_message': msg, 'locked_out': True}, status=400)
            messages.error(request, msg)
            return render(request, 'accounts/verify_otp.html', {'user_email': verified_user.email, 'locked_out': True})

        if action == 'send_code':
            auth_identifier = (
                request.POST.get('identifier', '').strip()
                or request.POST.get('auth_identifier', '').strip()
                or request.POST.get('email', '').strip()
            )
            if not auth_identifier:
                return JsonResponse({'success': False, 'error_message': 'Please enter your email or mobile number.'}, status=400)

            # Audit B1: a resend creates a brand-new verification for exactly
            # this identifier (the previous code stops working). The contact
            # phone is only a booking detail: it is validated like at booking
            # time and can never identify or unlock an account.
            pending_for_phone = request.session.get('pending_booking_data') or {}
            cust_phone = (
                request.POST.get('phone')
                or pending_for_phone.get('phone')
                or (booking_auth.get('phone') if booking_auth else '')
                or ''
            )
            identity = _resolve_guest_identity(auth_identifier, cust_phone)
            if identity['error']:
                return JsonResponse({'success': False, 'error_message': identity['error']}, status=400)
            auth_identifier = identity['verified_identifier']
            # The staged booking's own contact phone must pass the same check.
            staged_phone = _normalize_phone(pending_for_phone.get('phone'))
            if identity['channel'] == 'email' and staged_phone:
                allowed = {identity['user'].id} if identity['user'] else set()
                if _user_ids_for_phone(staged_phone) - allowed:
                    return JsonResponse({'success': False, 'error_message': GUEST_PHONE_TAKEN_MSG}, status=400)
            cust_phone = identity['contact_phone']

            if booking_auth and (booking_auth.get('identifier') == auth_identifier or booking_auth.get('email') == auth_identifier.lower() or booking_auth.get('phone') == auth_identifier):
                created_at_str = booking_auth.get('created_at')
                created_at = datetime.fromisoformat(created_at_str) if created_at_str else None
                if created_at:
                    seconds_elapsed = (timezone.now() - created_at).total_seconds()
                    if seconds_elapsed < 30:
                        rem_s = int(30 - seconds_elapsed)
                        return JsonResponse({'success': False, 'error_message': f'Please wait {rem_s}s before requesting a new code.'}, status=400)

            cust_name = request.POST.get('customer_name') or (booking_auth.get('customer_name') if booking_auth else '')
            otp, sent = _send_booking_verification_code(request, auth_identifier, cust_name, cust_phone)
            if not sent:
                target_word = "email" if '@' in auth_identifier else "mobile number"
                return JsonResponse({'success': False, 'error_message': f'Could not send verification code to your {target_word}. Please try again.'}, status=400)

            target_word = "email" if '@' in auth_identifier else "mobile number"
            return JsonResponse({
                'success': True,
                'requires_code': True,
                'target': auth_identifier,
                'email': auth_identifier.lower() if '@' in auth_identifier else '',
                'phone': cust_phone,
                'message': f'Verification code sent to your {target_word}.'
            })

        if action == 'verify':
            if booking_auth:
                created_at_str = booking_auth.get('created_at')
                created_at = datetime.fromisoformat(created_at_str) if created_at_str else None
                if not created_at or timezone.now() > created_at + timedelta(minutes=10):
                    msg = 'Verification code has expired. Please request a new code.'
                    if is_ajax:
                        return JsonResponse({'success': False, 'error_message': msg, 'requires_resend': True}, status=400)
                    messages.error(request, msg)
                    return render(request, 'accounts/verify_otp.html', {'user_email': booking_auth.get('email', ''), 'requires_resend': True})

                attempts = booking_auth.get('attempts', 0)

                # Server-side, session-independent wrong-code counter for this
                # destination: a fresh session / cleared cookies cannot reset it.
                otp_target = (booking_auth.get('pending_identifier') or booking_auth.get('identifier') or '')
                otp_attempt_key = otp_verify_target_key('booking', otp_target)
                if is_rate_limited(otp_attempt_key, OTP_VERIFY_MAX_ATTEMPTS):
                    booking_auth['code_hash'] = None
                    request.session.modified = True
                    msg = (
                        'Too many incorrect codes for this contact. '
                        f'Please try again in {max(1, minutes_remaining(otp_attempt_key))} minute(s).'
                    )
                    if is_ajax:
                        return JsonResponse({'success': False, 'error_message': msg, 'locked_out': True, 'remaining_attempts': 0}, status=429)
                    messages.error(request, msg)
                    return render(request, 'accounts/verify_otp.html', {'user_email': booking_auth.get('email', ''), 'locked_out': True})
                if attempts >= 5 or not booking_auth.get('code_hash'):
                    booking_auth['code_hash'] = None
                    request.session.modified = True
                    msg = 'Maximum attempts exceeded (5). This verification code has been invalidated. Please request a new code.'
                    if is_ajax:
                        return JsonResponse({'success': False, 'error_message': msg, 'locked_out': True, 'requires_resend': True, 'remaining_attempts': 0}, status=400)
                    messages.error(request, msg)
                    return render(request, 'accounts/verify_otp.html', {'user_email': booking_auth.get('email', ''), 'locked_out': True})

                if not otp_input or len(otp_input) != 6 or not otp_input.isdigit():
                    msg = 'Please enter the complete 6-digit verification code.'
                    if is_ajax:
                        return JsonResponse({'success': False, 'error_message': msg}, status=400)
                    messages.error(request, msg)
                    return render(request, 'accounts/verify_otp.html', {'user_email': booking_auth.get('email', '')})

                submitted_hash = hashlib.sha256(otp_input.encode('utf-8')).hexdigest()
                stored_hash = booking_auth.get('code_hash', '')
                is_valid = bool(stored_hash and secrets.compare_digest(stored_hash, submitted_hash))

                if not is_valid:
                    _locked_now, _wait = register_failure(
                        otp_attempt_key, OTP_VERIFY_MAX_ATTEMPTS, OTP_VERIFY_LOCKOUT_SECONDS
                    )
                    if _locked_now:
                        booking_auth['attempts'] = max(attempts + 1, 5)
                    booking_auth['attempts'] = attempts + 1
                    request.session.modified = True
                    if booking_auth['attempts'] >= 5:
                        booking_auth['code_hash'] = None
                        msg = 'Maximum attempts exceeded (5). This verification code has been invalidated. Please request a new code.'
                        if is_ajax:
                            return JsonResponse({'success': False, 'error_message': msg, 'locked_out': True, 'requires_resend': True, 'remaining_attempts': 0}, status=400)
                        messages.error(request, msg)
                        return render(request, 'accounts/verify_otp.html', {'user_email': booking_auth.get('email', ''), 'locked_out': True})

                    remaining_attempts = 5 - booking_auth['attempts']
                    msg = f'Invalid verification code. {remaining_attempts} attempt(s) remaining.'
                    if is_ajax:
                        return JsonResponse({'success': False, 'error_message': msg, 'remaining_attempts': remaining_attempts}, status=400)
                    messages.error(request, msg)
                    return render(request, 'accounts/verify_otp.html', {'user_email': booking_auth.get('email', '')})

                # Valid code. The code is consumed immediately (no reuse).
                clear(otp_attempt_key)
                pending_channel = booking_auth.get('pending_channel') or booking_auth.get('verified_channel')
                pending_identifier = (booking_auth.get('pending_identifier') or booking_auth.get('verified_identifier') or '').strip()
                contact_phone = (booking_auth.get('phone') or '').strip()
                customer_name = booking_auth.get('customer_name', '').strip()

                # Invalidate OTP immediately to prevent OTP reuse
                booking_auth['code_hash'] = None
                request.session.pop('booking_auth_data', None)

                def _verify_failed(msg):
                    if is_ajax:
                        return JsonResponse({'success': False, 'error_message': msg, 'requires_resend': True}, status=400)
                    messages.error(request, msg)
                    return render(request, 'accounts/verify_otp.html', {'user_email': '', 'requires_resend': True})

                if pending_channel not in ('email', 'phone') or not pending_identifier:
                    return _verify_failed('Your verification session has expired. Please request a new code.')

                # Verified channel and identifier established ONLY upon successful verification
                verified_channel = pending_channel
                verified_identifier = pending_identifier

                # Audit B1: resolve the account ONLY from the identifier the
                # code was sent to, re-checking every guard at verify time.
                identity = _resolve_guest_identity(verified_identifier, contact_phone)
                if identity['error']:
                    return _verify_failed(identity['error'])
                pending_booking_peek = request.session.get('pending_booking_data') or {}
                staged_phone = _normalize_phone(pending_booking_peek.get('phone'))
                if verified_channel == 'email' and staged_phone:
                    allowed = {identity['user'].id} if identity['user'] else set()
                    if _user_ids_for_phone(staged_phone) - allowed:
                        return _verify_failed(GUEST_PHONE_TAKEN_MSG)

                email = verified_identifier if verified_channel == 'email' else ''
                verified_phone = verified_identifier if verified_channel == 'phone' else ''
                existing_user = identity['user']

                if existing_user:
                    user = existing_user
                    if email and not user.is_email_verified:
                        user.is_email_verified = True
                        user.save(update_fields=['is_email_verified'])
                else:
                    # Auto-register new guest user
                    target_email = email if email else f"{verified_phone}@guest.lamo.in"
                    unique_uname = _generate_unique_customer_username(target_email, customer_name)
                    parts = (customer_name or '').split(None, 1)
                    first_name = parts[0] if parts else ''
                    last_name = parts[1] if len(parts) > 1 else ''
                    user = CustomUser.objects.create_user(
                        username=unique_uname,
                        email=email if email else '',
                        # Customers authenticate with OTP only (audit B3).
                        password=None,
                        first_name=first_name,
                        last_name=last_name,
                        role='USER',
                        is_email_verified=bool(email),
                    )
                    user.set_unusable_password()
                    user.save(update_fields=['password'])
                    UserProfile.objects.create(
                        user=user,
                        # Only a phone number proven by this OTP is stored on
                        # the account; an email-channel contact number stays
                        # on the booking itself.
                        phone=verified_phone,
                        address=pending_booking_peek.get('location', ''),
                        district=pending_booking_peek.get('district', ''),
                        latitude=pending_booking_peek.get('latitude'),
                        longitude=pending_booking_peek.get('longitude')
                    )

                login(request, user)
                request.session.cycle_key()

                pending_booking = request.session.pop('pending_booking_data', None)
                if pending_booking is not None:
                    pending_booking['phone_verified'] = bool(
                        verified_phone and _normalize_phone(pending_booking.get('phone')) == verified_phone
                    )
                if pending_booking:
                    created_b = _create_booking_from_pending(request, user, pending_booking)
                    if created_b:
                        if is_ajax:
                            arrival_pin = getattr(created_b, 'arrival_otp', '') or ''
                            worker_name = (created_b.worker.user.get_full_name() or created_b.worker.user.username) if (created_b.worker and created_b.worker.user) else ''
                            return JsonResponse({
                                'success': True,
                                'redirect_url': reverse('my_bookings'),
                                'booking_id': created_b.id,
                                'worker_name': worker_name,
                                'service_name': created_b.service.name if created_b.service else '',
                                'arrival_pin': str(arrival_pin) if arrival_pin else '',
                                'scheduled_date': str(created_b.date) if getattr(created_b, 'date', None) else '',
                                'scheduled_time': str(created_b.time) if getattr(created_b, 'time', None) else '',
                                'message': 'Booking confirmed successfully!'
                            })
                        messages.success(request, 'Booking confirmed successfully!')
                        return redirect('my_bookings')
                    else:
                        if is_ajax:
                            return JsonResponse({
                                'success': True,
                                'redirect_url': reverse('service_page'),
                                'message': 'Authentication successful, but the requested slot was taken. Please select an available slot.'
                            })
                        return redirect('service_page')

                if is_ajax:
                    return JsonResponse({'success': True, 'redirect_url': reverse('home'), 'message': 'Verification successful!'})
                return redirect('home')

            if not user_id:
                if is_ajax:
                    return JsonResponse({'success': False, 'error_message': 'Session expired. Please request a new verification code.'}, status=400)
                messages.error(request, 'Session expired. Please register again.')
                return redirect('register')

            try:
                user = CustomUser.objects.get(id=user_id)
            except CustomUser.DoesNotExist:
                if is_ajax:
                    return JsonResponse({'success': False, 'error_message': 'User not found. Please request a new verification code.'}, status=400)
                messages.error(request, 'User not found. Please register again.')
                return redirect('register')

            # Check max attempts for THIS user. The counter lives in the
            # database (not the session), so wiping cookies cannot reset it.
            if user.otp_failed_attempts >= OTP_VERIFY_MAX_ATTEMPTS:
                # Lock out THIS user
                lockout_time = timezone.now() + timedelta(
                    seconds=OTP_VERIFY_LOCKOUT_SECONDS
                )
                user.otp_locked_until = lockout_time
                user.save(update_fields=['otp_locked_until'])
                msg = 'Too many failed attempts for this account. Please try again in 15 minutes.'
                if is_ajax:
                    return JsonResponse({'success': False, 'error_message': msg, 'locked_out': True, 'requires_resend': True}, status=400)
                messages.error(request, msg)
                return render(request, 'accounts/verify_otp.html', {
                    'user_email': user.email,
                    'locked_out': True
                })

            if user.is_otp_valid(otp_input):
                user.is_email_verified = True
                user.email_verification_otp = None
                user.otp_created_at = None
                user.otp_failed_attempts = 0
                user.otp_locked_until = None
                user.save(update_fields=[
                    'is_email_verified', 'email_verification_otp',
                    'otp_created_at', 'otp_failed_attempts', 'otp_locked_until'
                ])

                if user.role == 'USER':
                    UserProfile.objects.get_or_create(user=user)

                # Clear OTP session data and the server-side resend
                # counters on success.
                request.session.pop('otp_user_id', None)
                clear(otp_resend_user_key(user.id) + ':window')
                clear(otp_resend_user_key(user.id))
                clear(otp_resend_ip_key(request))

                # Check for pending booking from guest customer account creation flow
                pending_booking = request.session.pop('pending_booking_data', None)
                if pending_booking:
                    created_b = _create_booking_from_pending(request, user, pending_booking)
                    login(request, user)
                    request.session.cycle_key()
                    if created_b:
                        if is_ajax:
                            arrival_pin = getattr(created_b, 'arrival_otp', '') or ''
                            worker_name = (created_b.worker.user.get_full_name() or created_b.worker.user.username) if (created_b.worker and created_b.worker.user) else ''
                            return JsonResponse({
                                'success': True,
                                'redirect_url': reverse('my_bookings'),
                                'booking_id': created_b.id,
                                'worker_name': worker_name,
                                'service_name': created_b.service.name if created_b.service else '',
                                'arrival_pin': str(arrival_pin) if arrival_pin else '',
                                'scheduled_date': str(created_b.date) if getattr(created_b, 'date', None) else '',
                                'scheduled_time': str(created_b.time) if getattr(created_b, 'time', None) else '',
                                'message': 'Your email has been verified and your booking has been placed successfully!'
                            })
                        messages.success(request, 'Your email has been verified and your booking has been placed successfully!')
                        return redirect('my_bookings')
                    else:
                        if is_ajax:
                            return JsonResponse({
                                'success': True,
                                'redirect_url': reverse('service_page'),
                                'message': 'Your email has been verified, but the requested slot was taken. Please select an available slot or worker.'
                            })
                        return redirect('service_page')

                # ----------------------------------------------------
                # IF WORKER: Redirect to dedicated "Application Under Review" screen
                # (Never send to login page; worker is pending admin approval)
                # ----------------------------------------------------
                if user.role == 'WORKER':
                    request.session['under_review_worker_id'] = getattr(user, 'worker_profile', None).id if hasattr(user, 'worker_profile') and user.worker_profile else None
                    msg = 'Email verified successfully! Your application has been submitted for admin review.'
                    if is_ajax:
                        return JsonResponse({
                            'success': True,
                            'redirect_url': reverse('worker_application_status'),
                            'message': msg
                        })
                    messages.success(request, msg)
                    return redirect('worker_application_status')

                # ----------------------------------------------------
                # IF CUSTOMER (USER): Auto-login immediately!
                # (Zero friction, no login page, no redundant second OTP)
                # ----------------------------------------------------
                login(request, user)
                request.session.cycle_key()

                requested_next = request.session.pop('registration_next', None)
                safe_next = _safe_internal_path(request, requested_next, '')
                dest = safe_next if safe_next else reverse('home')
                msg = f'Welcome to LAMO, {user.first_name or user.username}! Your email has been verified.'

                if is_ajax:
                    return JsonResponse({
                        'success': True,
                        'redirect_url': dest,
                        'message': msg
                    })
                messages.success(request, msg)
                return redirect(dest)
            else:
                # Increment attempts ONLY for this user
                user.otp_failed_attempts += 1
                if user.otp_failed_attempts >= OTP_VERIFY_MAX_ATTEMPTS:
                    lockout_time = timezone.now() + timedelta(seconds=OTP_VERIFY_LOCKOUT_SECONDS)
                    user.otp_locked_until = lockout_time
                    user.save(update_fields=['otp_failed_attempts', 'otp_locked_until'])
                    msg = 'Maximum attempts exceeded (5). This verification code has been invalidated. Please request a new code.'
                    if is_ajax:
                        return JsonResponse({
                            'success': False,
                            'error_message': msg,
                            'locked_out': True,
                            'requires_resend': True,
                            'remaining_attempts': 0
                        }, status=400)
                    messages.error(request, msg)
                    return render(request, 'accounts/verify_otp.html', {'user_email': user.email, 'locked_out': True})

                user.save(update_fields=['otp_failed_attempts'])
                remaining_attempts = (
                    OTP_VERIFY_MAX_ATTEMPTS - user.otp_failed_attempts
                )
                msg = f'Invalid or expired verification code. {remaining_attempts} attempt(s) remaining for this account.'
                if is_ajax:
                    return JsonResponse({
                        'success': False,
                        'error_message': msg,
                        'remaining_attempts': remaining_attempts
                    }, status=400)
                messages.error(request, msg)
                return render(request, 'accounts/verify_otp.html', {'user_email': user.email})

        elif action == 'resend':
            if booking_auth:
                created_at_str = booking_auth.get('created_at')
                created_at = datetime.fromisoformat(created_at_str) if created_at_str else None
                if created_at:
                    seconds_elapsed = (timezone.now() - created_at).total_seconds()
                    if seconds_elapsed < 30:
                        rem_s = int(30 - seconds_elapsed)
                        msg = f'Please wait {rem_s}s before requesting a new code.'
                        if is_ajax:
                            return JsonResponse({'success': False, 'error_message': msg, 'locked_out': True}, status=400)
                        messages.error(request, msg)
                        return render(request, 'accounts/verify_otp.html', {'user_email': booking_auth.get('email', '')})

                identifier = (
                    booking_auth.get('pending_identifier')
                    or booking_auth.get('identifier')
                    or booking_auth.get('email')
                    or booking_auth.get('phone')
                )
                contact_phone = (booking_auth.get('phone') or '').strip()
                # Prevent resend identity manipulation: re-validate guest identity against session identifier
                identity = _resolve_guest_identity(identifier, contact_phone)
                if identity['error']:
                    if is_ajax:
                        return JsonResponse({'success': False, 'error_message': identity['error']}, status=400)
                    messages.error(request, identity['error'])
                    return render(request, 'accounts/verify_otp.html', {'user_email': booking_auth.get('email', '')})

                otp, sent = _send_booking_verification_code(
                    request,
                    identity['verified_identifier'],
                    booking_auth.get('customer_name'),
                    identity['contact_phone']
                )
                if not sent:
                    target_dest = "email" if '@' in (identifier or '') else "mobile number"
                    msg = f'Could not send verification code to your {target_dest}. Please try again in a few minutes.'
                    if is_ajax:
                        return JsonResponse({'success': False, 'error_message': msg}, status=400)
                    messages.error(request, msg)
                    return render(request, 'accounts/verify_otp.html', {'user_email': booking_auth.get('email', '')})

                target_dest = "email" if '@' in (identifier or '') else "mobile number"
                if is_ajax:
                    return JsonResponse({'success': True, 'message': f'A new verification code has been sent to your {target_dest}.'})
                messages.success(request, f'A new verification code has been sent to your {target_dest}.')
                return render(request, 'accounts/verify_otp.html', {'user_email': booking_auth.get('email', '')})

            if not user_id:
                if is_ajax:
                    return JsonResponse({'success': False, 'error_message': 'Session expired. Please request a new code.'}, status=400)
                messages.error(request, 'Session expired. Please register again.')
                return redirect('register')
            try:
                user = CustomUser.objects.get(id=user_id)
            except CustomUser.DoesNotExist:
                if is_ajax:
                    return JsonResponse({'success': False, 'error_message': 'User not found. Please register again.'}, status=400)
                messages.error(request, 'User not found. Please register again.')
                return redirect('register')

            # Resend limits live on the server, not in the session
            sent, result_message = _resend_otp_for_user(request, user)

            if not sent:
                if is_ajax:
                    return JsonResponse({'success': False, 'error_message': result_message, 'locked_out': True}, status=400)
                messages.error(request, result_message)
                return render(request, 'accounts/verify_otp.html', {
                    'user_email': user.email,
                    'locked_out': True
                })

            user.otp_failed_attempts = 0
            user.otp_locked_until = None
            user.save(update_fields=['otp_failed_attempts', 'otp_locked_until'])

            if is_ajax:
                return JsonResponse({'success': True, 'message': result_message})
            messages.success(request, result_message)
            return render(request, 'accounts/verify_otp.html', {'user_email': user.email})

    user_email = None
    if verified_user:
        user_email = verified_user.email
    elif booking_auth:
        user_email = booking_auth.get('email')

    has_pending_booking = 'pending_booking_data' in request.session
    return render(request, 'accounts/verify_otp.html', {
        'user_email': user_email,
        'has_pending_booking': has_pending_booking,
    })


# def register_view(request):

#     if request.user.is_authenticated:
#         return redirect('dashboard')

#     if request.method == 'POST':

#         username = request.POST.get('username')
#         email = request.POST.get('email')
#         password = request.POST.get('password')
#         confirm_password = request.POST.get('confirm_password')
#         role = request.POST.get('role')

#         # Password validation
#         if password != confirm_password:
#             messages.error(request, 'Passwords do not match.')
#             return redirect('register')

#         # Username validation
#         if CustomUser.objects.filter(username=username).exists():
#             messages.error(request, 'Username already exists.')
#             return redirect('register')

#         # Email validation
#         if CustomUser.objects.filter(email=email).exists():
#             messages.error(request, 'Email already exists.')
#             return redirect('register')

#         # Create user
#         user = CustomUser.objects.create_user(
#             username=username,
#             email=email,
#             password=password,
#             role=role
#         )

#         messages.success(
#             request,
#             'Account created successfully. Please login.'
#         )

#         return redirect('login')

#     return render(request, 'accounts/register.html')

def about(request):
    return render(request, 'about.html')


def contact(request):
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        email = request.POST.get('email', '').strip()
        phone = request.POST.get('phone', '').strip()
        subject = request.POST.get('subject', '').strip()
        message = request.POST.get('message', '').strip()

        context = {
            'name': name,
            'email': email,
            'phone': phone,
            'subject': subject,
            'message': message,
        }

        if not name or not email or not subject or not message:
            messages.error(request, 'Please fill in all required fields.')
            return render(request, 'contact.html', context)

        # Abuse guard. The form is reachable without an account and each accepted
        # submission stores a row AND sends an e-mail to the admin mailbox, so an
        # unbounded loop is both a storage-growth vector and a way to flood the
        # admin inbox. Counted per IP on the server, so clearing cookies or opening
        # a private window changes nothing. Counted per address too, so a script
        # rotating victim addresses from one host is still stopped.
        contact_ip_key = contact_ip_rate_key(request)
        contact_email_key = contact_email_rate_key(email)

        for limit_key in (contact_ip_key, contact_email_key):
            if is_rate_limited(limit_key, CONTACT_MAX_SUBMISSIONS):
                logger.warning(
                    'Contact form rate limited (ip=%s)', client_ip(request)
                )
                messages.error(
                    request,
                    'You have sent several messages recently. Please wait a '
                    'little before sending another one.',
                )
                return render(request, 'contact.html', context)

        register_attempt_within_window(
            contact_ip_key, CONTACT_MAX_SUBMISSIONS, CONTACT_WINDOW_SECONDS
        )
        register_attempt_within_window(
            contact_email_key, CONTACT_MAX_SUBMISSIONS, CONTACT_WINDOW_SECONDS
        )

        # CR/LF in a header value is a classic mail-header injection vector.
        # The subject is echoed into the admin notification e-mail, so any
        # newline is stripped before it is stored or mailed.
        subject = _sanitize_mail_header(subject)
        name = _sanitize_mail_header(name) or 'LAMO visitor'
        clean_phone = phone[:15] if phone else None

        # Save the enquiry to the database for admin visibility
        try:
            enquiry = ContactEnquiry.objects.create(
                name=name,
                email=email,
                phone=clean_phone,
                subject=subject,
                message=message,
                user=request.user if request.user.is_authenticated else None,
            )

            # Notify the admin team by e-mail. This is best-effort: the
            # enquiry is already persisted and visible in the admin panel, so a
            # mail failure must not lose the customer's message.
            admin_mailbox = getattr(
                settings, 'CONTACT_NOTIFICATION_EMAIL', ''
            ) or settings.DEFAULT_FROM_EMAIL
            if admin_mailbox:
                full_message = (
                    f"New Contact Enquiry\n"
                    f"{'='*40}\n\n"
                    f"Name: {name}\n"
                    f"Email: {email}\n"
                    f"Phone: {phone or 'Not provided'}\n"
                    f"Subject: {subject}\n\n"
                    f"Message:\n{message}\n\n"
                    f"{'='*40}\n"
                    f"Enquiry ID: #{enquiry.id}\n"
                    f"Sent from LAMO Contact Form"
                )

                send_email_safely(
                    subject=f'LAMO Contact Enquiry #{enquiry.id}: {subject}',
                    message=full_message,
                    recipient_list=[admin_mailbox],
                    purpose='contact_enquiry',
                    async_send=True,
                )
            else:
                logger.warning(
                    'contact_enquiry_notification_skipped enquiry_id=%s '
                    'reason=no_admin_mailbox_configured',
                    enquiry.id,
                )

            messages.success(
                request,
                'Your enquiry has been sent successfully. We will get back to you soon.'
            )
            # Clear fields after successful submission
            context = {}
        except Exception:
            logger.exception('Failed to process contact enquiry')
            messages.error(request, 'Failed to send your enquiry. Please try again later.')

        return render(request, 'contact.html', context)

    initial_context = {}
    if request.user.is_authenticated:
        initial_context['name'] = request.user.get_full_name() or request.user.username
        initial_context['email'] = request.user.email
    return render(request, 'contact.html', initial_context)


def terms_view(request):
    return render(request, 'legal/terms.html')


def privacy_view(request):
    return render(request, 'legal/privacy.html')


terms = terms_view
privacy = privacy_view


def _get_or_create_review(booking, customer, rating, comment):
    """Create the single review for *booking*, race-safe.

    ``Review.booking`` is a ``OneToOneField``, so the database already
    enforces "one review per booking" with a UNIQUE index. That is the final
    authority - the ``Review.objects.filter(...).first()`` check in the views
    is only a UX shortcut, and two concurrent requests can both pass it before
    either inserts.

    Instead of trusting that check, let the INSERT race and resolve it:

    * the happy path inserts;
    * if a concurrent request wins, MySQL/SQLite/Postgres raise
      ``IntegrityError`` for the unique index, which is caught here and the
      already-existing review is returned instead.

    Returns ``(review, created)`` where ``created`` is ``False`` when the
    review already existed. No schema change is required - the constraint is
    already in the database.
    """
    from django.db import IntegrityError, transaction

    try:
        with transaction.atomic():
            return Review.objects.create(
                booking=booking,
                customer=customer,
                worker=booking.worker,
                rating=rating,
                comment=comment,
            ), True
    except IntegrityError:
        # Lost the race: the unique index rejected the duplicate insert.
        existing = Review.objects.filter(booking=booking).first()
        if existing is not None:
            logger.info(
                'Duplicate review rejected by unique constraint '
                '(booking=%s customer=%s)',
                booking.id,
                customer.pk,
            )
            return existing, False
        # Not a uniqueness problem (e.g. a bad FK) - let it surface.
        raise


def _get_or_create_complaint(booking, customer, subject, description, severity='MEDIUM'):
    """Same race-safe pattern for complaints.

    ``Complaint`` has ``unique_together = ('booking', 'customer')``
    (migration 0023), so the database is again the final authority.

    Returns ``(complaint, created)``.
    """
    from django.db import IntegrityError, transaction

    try:
        with transaction.atomic():
            return Complaint.objects.create(
                booking=booking,
                customer=customer,
                subject=subject,
                description=description,
                severity=severity,
                status='OPEN',
            ), True
    except IntegrityError:
        existing = Complaint.objects.filter(
            booking=booking, customer=customer
        ).first()
        if existing is not None:
            logger.info(
                'Duplicate complaint rejected by unique constraint '
                '(booking=%s customer=%s)',
                booking.id,
                customer.pk,
            )
            return existing, False
        raise


def _notify_worker_of_review(request, booking, rating):
    """Tell the worker a review landed. Never fails the submission."""
    if not booking.worker:
        return
    try:
        Notification.objects.create(
            recipient=booking.worker.user,
            booking=booking,
            notification_type='BOOKING_UPDATE',
            message=(
                f'{request.user.get_full_name() or request.user.username} '
                f'left a {rating}-star review for your '
                f'{booking.service.name} service.'
            )
        )
    except Exception as exc:
        # A failed notification must never discard a valid review.
        # Class name only: Notification.objects.create() failure can echo the
        # bound message text, and exc_info=True would reprint it again.
        logger.warning(
            'review_worker_notification_failed booking=%s exc_type=%s',
            booking.id,
            type(exc).__name__,
        )


def _check_worker_verification(worker):
    """Check if worker is verified and not suspended. Returns (is_allowed, error_message)"""
    if not worker.is_verified:
        return False, 'Your worker account is not verified. Please wait for admin verification.'
    if worker.work_status == 'SUSPENDED':
        return False, 'Your worker account has been suspended. Please contact support.'
    return True, None


def _sanitize_mail_header(value, max_length=200):
    """Make a user-supplied string safe to place in an e-mail header.

    A CR or LF inside a header value terminates that header and lets an
    attacker append arbitrary headers (Bcc:, extra recipients). Django's
    ``send_mail`` defends against this itself, but the raw value is also stored
    in the database and re-rendered in the admin panel, so it is normalised at
    the boundary instead of being trusted downstream.

    Newlines become spaces, remaining control characters are dropped, and the
    result is length-capped to match the model field.
    """
    if value is None:
        return ''
    text = str(value)
    # Newlines/CR first: they are the actual header-splitting characters.
    text = re.sub(r'[\r\n]+', ' ', text)
    # Any remaining control characters (tab, NUL, etc.) are stripped.
    text = ''.join(ch for ch in text if ord(ch) >= 32 or ch == '\t')
    text = ' '.join(text.split())
    return text[:max_length].strip()


def _emergency_unavailable_reason(worker_profile, user):
    """Return a short, user-facing reason a worker cannot see emergency jobs.

    Only conditions the backend can state with certainty are reported - the
    worker can check all of them on their own profile. Anything finer (which
    filters excluded which rows) is deliberately NOT exposed, so the message
    cannot be used to infer other customers' activity.

    Returns ``None`` when the worker is simply eligible and there just happens
    to be nothing open right now.
    """
    if not user.is_active:
        return 'Your account is currently inactive. Please contact support.'
    if not worker_profile.is_verified:
        return (
            'Emergency requests appear once an admin has verified your worker '
            'account.'
        )
    if not worker_profile.service_id:
        return (
            'Emergency requests appear once a service or trade has been '
            'assigned to your profile.'
        )
    if worker_profile.work_status == 'SUSPENDED':
        return 'Your worker account has been suspended. Please contact support.'
    if worker_profile.work_status == 'LEAVE':
        return 'You are currently marked as on leave, so emergency requests are paused.'
    if worker_profile.work_status == 'ON_DUTY':
        return 'You are marked on duty. Switch to Available to receive emergency requests.'
    return None


def _safe_internal_path(request, raw_next, fallback):
    """Return a *local* redirect target from untrusted input, else ``fallback``.

    Generic sibling of :func:`_safe_post_login_redirect` for flows that start
    before the user is authenticated (e.g. register -> OTP -> login -> back to
    the service the guest was viewing).

    Only same-site relative paths are accepted. ``next`` arrives from the query
    string or a hidden form field, both fully attacker-controlled, so an
    absolute URL to another host, a protocol-relative ``//evil.com`` and a
    backslash-smuggled path are all rejected in favour of ``fallback``.
    """
    # An empty fallback is meaningful: the callers that pass '' want "no usable
    # destination" so they can fall back to their own normal behaviour. Do not
    # coerce it to '/', or an unsafe ``next`` would silently become a redirect
    # to the home page instead of being discarded.
    fallback = '' if not fallback else str(fallback)
    if not raw_next:
        return fallback

    candidate = str(raw_next).strip()
    if not candidate:
        return fallback

    # Reject anything that is not an obvious local path before the host check,
    # so a scheme-relative or backslash variant can never slip through.
    if not candidate.startswith('/') or candidate.startswith('//'):
        return fallback
    if '\\' in candidate or '\n' in candidate or '\r' in candidate:
        return fallback

    if url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate

    return fallback


def _safe_post_login_redirect(request, raw_next):
    """Return a *local* redirect target from untrusted ``next`` input.

    ``request.POST['next']`` and the ``Referer`` header are attacker-controlled,
    so they must never be passed to ``redirect()`` unchecked. Only relative
    paths (``/worker-dashboard/``) or absolute URLs whose host matches the
    current request are allowed; anything else falls back to the worker
    dashboard. ``redirect()`` accepts both view names and paths, so returning
    a resolved path keeps every caller working.
    """
    fallback = reverse('worker_dashboard')
    if not raw_next:
        return fallback
    candidate = str(raw_next).strip()
    if not candidate:
        return fallback
    if url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return fallback


def _normalize_phone(raw):
    """Return a 10-digit Indian mobile number, or '' when not valid."""
    digits = re.sub(r'\D', '', str(raw or ''))
    if len(digits) == 12 and digits.startswith('91'):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith('0'):
        digits = digits[1:]
    return digits if re.fullmatch(r'[6-9]\d{9}', digits) else ''


def _user_ids_for_phone(phone):
    """Ids of every account (customer or worker) holding this phone number."""
    if not phone:
        return set()
    ids = set(UserProfile.objects.filter(phone=phone).values_list('user_id', flat=True))
    ids |= set(WorkerProfile.objects.filter(phone=phone).values_list('user_id', flat=True))
    return ids


def _user_ids_for_email(email):
    if not email:
        return set()
    return set(CustomUser.objects.filter(email__iexact=email).values_list('id', flat=True))


def _users_matching_identifier(auth_identifier):
    """Distinct accounts matching an email, phone number or username."""
    if not auth_identifier:
        return []
    auth_identifier = str(auth_identifier).strip()
    if '@' in auth_identifier:
        ids = _user_ids_for_email(auth_identifier)
    else:
        phone = _normalize_phone(auth_identifier)
        ids = _user_ids_for_phone(phone) if phone else set()
        if not ids:
            ids = set(CustomUser.objects.filter(username=auth_identifier).values_list('id', flat=True))
    return list(CustomUser.objects.filter(id__in=ids))


def _find_user_by_identifier(auth_identifier):
    """Lookup a CustomUser by email, 10-digit phone number, or username.

    Returns None when nothing matches OR when the identifier is shared by more
    than one account: an ambiguous identifier must never silently pick one
    account (use _users_matching_identifier to tell the two cases apart).
    """
    users = _users_matching_identifier(auth_identifier)
    return users[0] if len(users) == 1 else None


def _is_admin_account(user):
    """LAMO administrators: ADMIN role or a Django superuser (never plain staff)."""
    return bool(user) and (user.role == 'ADMIN' or getattr(user, 'is_superuser', False))


PASSWORD_LOGIN_ADMIN_ONLY_MSG = (
    'Customers and service professionals sign in with a one-time code sent to '
    'their mobile number or email. Please use "Send code" to continue.'
)
ADMIN_OTP_NOT_ALLOWED_MSG = (
    'Administrator accounts sign in with their password on the admin sign-in page.'
)


GUEST_PHONE_TAKEN_MSG = (
    'This mobile number is already associated with an existing account. '
    'Please sign in with that number, or enter a different mobile number.'
)


def _resolve_guest_identity(identifier, contact_phone=''):
    """Decide who a guest booking OTP may authenticate (audit B1).

    The OTP is sent to ``identifier`` only, so that is the ONLY value that can
    identify an existing account after verification:
      * channel / verified_identifier: where the code goes (email or phone);
      * user: the single active customer account owning that identifier, or
        None for a new customer;
      * contact_phone: the booking contact number. On the email channel it is
        NOT verified, so it must not belong to any other account (it can never
        be used to find or log into an account).
    Worker, admin and staff accounts can never be authenticated here.
    Returns a dict; on failure ``error`` holds a user-facing message.
    """
    result = {'channel': '', 'verified_identifier': '', 'contact_phone': '', 'user': None, 'error': ''}
    ident = (identifier or '').strip()
    if '@' in ident:
        try:
            validate_email(ident)
        except ValidationError:
            result['error'] = 'Please enter a valid email address.'
            return result
        result['channel'] = 'email'
        result['verified_identifier'] = ident.lower()
        owners = _user_ids_for_email(result['verified_identifier'])
        contact = _normalize_phone(contact_phone)
    else:
        phone = _normalize_phone(ident)
        if not phone:
            result['error'] = 'Please enter a valid email or 10-digit mobile number.'
            return result
        result['channel'] = 'phone'
        result['verified_identifier'] = phone
        owners = _user_ids_for_phone(phone)
        # On the phone channel the verified number IS the contact number.
        contact = phone

    if len(owners) > 1:
        result['error'] = 'This contact is linked to more than one account. Please contact support.'
        return result
    user = CustomUser.objects.filter(id__in=owners).first() if owners else None
    if user is not None:
        if user.role != 'USER' or user.is_superuser or user.is_staff:
            noun = 'email' if result['channel'] == 'email' else 'phone number'
            result['error'] = (
                f'This {noun} belongs to a worker or administrator account. '
                f'Please use your customer contact details.'
            )
            return result
        if not user.is_active:
            result['error'] = 'This account is deactivated. Please contact support.'
            return result

    if result['channel'] == 'email' and contact:
        allowed = {user.id} if user else set()
        if _user_ids_for_phone(contact) - allowed:
            result['error'] = GUEST_PHONE_TAKEN_MSG
            return result

    result['user'] = user
    result['contact_phone'] = contact
    return result


def _is_valid_booking_transition(current_status, new_status):
    """Check if a booking status transition is valid."""
    valid_transitions = {
        'PENDING': ['ACCEPTED', 'REJECTED', 'CANCELLED', 'EXPIRED'],
        'ACCEPTED': ['IN_PROGRESS', 'CANCELLED'],
        'IN_PROGRESS': ['COMPLETION_REQUESTED', 'CANCELLED'],
        'COMPLETION_REQUESTED': ['COMPLETED', 'DISPUTED', 'IN_PROGRESS'],
        'DISPUTED': ['COMPLETED', 'CANCELLED'],
        'COMPLETED': [],  # Terminal state
        'REJECTED': [],  # Terminal state
        'CANCELLED': [],  # Terminal state
        'EXPIRED': [],  # Terminal state
        'EMERGENCY_PENDING': ['ACCEPTED', 'CANCELLED', 'EXPIRED'],  # Emergency bookings can be accepted or cancelled
    }
    return new_status in valid_transitions.get(current_status, [])


import threading as _threading
_maintenance_guard = _threading.local()


def trigger_booking_reminders_throttled():
    """Opportunistically run booking maintenance (expiry, reminders, stuck-job
    follow-up), throttled to max 1 run/minute across all processes.

    The same work is available as ``manage.py process_booking_reminders`` for a
    scheduled job; both paths are idempotent, so running both is safe.
    """
    if getattr(_maintenance_guard, 'active', False):
        return
    if not getattr(settings, 'BOOKING_MAINTENANCE_ON_REQUEST', True):
        return
    from django.core.cache import cache
    cache_key = 'booking_schedule_reminders_last_run'
    try:
        # cache.add is atomic on shared caches: only one process wins per minute.
        if not cache.add(cache_key, True, timeout=60):
            return
    except Exception:
        return
    _maintenance_guard.active = True
    try:
        from .booking_reminders import run_scheduled_booking_maintenance
        run_scheduled_booking_maintenance()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Booking maintenance error: %s", e)
    finally:
        _maintenance_guard.active = False


# Single source of truth for how long a worker has to answer a request.
# Used by both the expiry sweep and the accept view, so a worker can never
# accept a request the sweep already considers expired (or vice versa).
RESPONSE_WINDOW_EMERGENCY_MIN = 15
RESPONSE_WINDOW_SAME_DAY_MIN = 20
RESPONSE_WINDOW_FUTURE_MIN = 60
# A worker's counter-proposal gives the customer this long to answer.
RESPONSE_WINDOW_PROPOSAL_HOURS = 24


def pending_request_has_expired(booking, now_local=None):
    """Return (has_expired, reason_text) for a PENDING / EMERGENCY_PENDING request.

    - Emergency: 15 minutes.
    - Same-day: 20 minutes.
    - Future date: 60 minutes, paused overnight (requests created 9 PM-8 AM
      may still be answered until 11 AM).
    - Worker proposed a new time: the customer has 24 hours to answer.
    """
    from django.utils import timezone as dj_timezone
    now_local = now_local or dj_timezone.localtime()
    now = dj_timezone.now()
    if not booking.created_at:
        return False, ''

    if booking.proposed_time and booking.proposed_date and booking.reschedule_proposed_at:
        if now - booking.reschedule_proposed_at < timedelta(hours=RESPONSE_WINDOW_PROPOSAL_HOURS):
            return False, ''
        return True, 'The proposed new time was not answered within 24 hours.'

    is_emergency = (booking.booking_type == 'Emergency' or booking.status == 'EMERGENCY_PENDING')
    if is_emergency:
        window = timedelta(minutes=RESPONSE_WINDOW_EMERGENCY_MIN)
        reason = f'Worker did not respond within the {RESPONSE_WINDOW_EMERGENCY_MIN}-minute emergency response window.'
    elif booking.date == now_local.date():
        window = timedelta(minutes=RESPONSE_WINDOW_SAME_DAY_MIN)
        reason = f'Worker did not respond within the {RESPONSE_WINDOW_SAME_DAY_MIN}-minute same-day response window.'
    else:
        created_local = dj_timezone.localtime(booking.created_at)
        if (created_local.hour >= 21 or created_local.hour < 8) and now_local.hour < 11:
            return False, ''
        window = timedelta(minutes=RESPONSE_WINDOW_FUTURE_MIN)
        reason = f'Worker did not respond within the {RESPONSE_WINDOW_FUTURE_MIN}-minute scheduled response window.'

    if now - booking.created_at < window:
        return False, ''
    return True, reason


def check_and_expire_pending_bookings(customer=None, worker=None, booking=None, _run_maintenance=True):
    if _run_maintenance:
        trigger_booking_reminders_throttled()
    """Expire PENDING bookings whose worker response window has lapsed.

    Smart response window:
    - Emergency: 15 minutes
    - Same-day booking: 15 minutes (fast acceptance for urgent same-day jobs)
    - Future-day booking: 30 minutes, with night-time pause (9 PM - 8 AM) allowing morning response.
    Notifies customer and worker, updates booking status to EXPIRED, and records
    the reason. Returns the list of expired bookings.
    """
    from datetime import timedelta
    from django.utils import timezone as dj_timezone

    now_local = dj_timezone.localtime()
    today = now_local.date()

    qs = Booking.objects.filter(
        status__in=['PENDING', 'EMERGENCY_PENDING']
    )
    if booking:
        qs = qs.filter(id=booking.id)
    elif customer:
        qs = qs.filter(customer=customer)
    elif worker:
        qs = qs.filter(worker=worker)

    expired_list = []
    for candidate in qs.select_related('customer', 'service', 'worker__user'):
        if not candidate.created_at:
            continue

        has_expired, reason_text = pending_request_has_expired(candidate, now_local=now_local)
        if not has_expired:
            continue

        with transaction.atomic():
            b = Booking.objects.select_for_update().filter(id=candidate.id).first()
            if not b or b.status not in ('PENDING', 'EMERGENCY_PENDING'):
                continue
            b.status = 'EXPIRED'
            b.cancellation_reason = reason_text
            b.cancelled_by = 'SYSTEM'
            b.save(update_fields=['status', 'cancellation_reason', 'cancelled_by', 'updated_at'])
            expired_list.append(b)

            # If this booking belongs to a recurring series, expire other unaccepted bookings in series (Finding B5)
            if b.series:
                Booking.objects.filter(
                    series=b.series,
                    status='PENDING'
                ).exclude(id=b.id).update(
                    status='EXPIRED',
                    cancellation_reason=reason_text,
                    cancelled_by='SYSTEM'
                )
                b.series.is_active = False
                b.series.save(update_fields=['is_active'])

            # Notify customer
            Notification.objects.create(
                recipient=b.customer,
                booking=b,
                notification_type='BOOKING_UPDATE',
                message=(
                    f"Your booking request #{b.id} for {b.service.name} has expired because "
                    f"the worker did not respond in time. You can choose another worker or pick another time."
                )
            )

            # Send email safely
            send_email_safely(
                subject=f"Booking Request #{b.id} Expired",
                message=(
                    f"Hi {b.customer.username},\n\n"
                    f"Your booking request #{b.id} for {b.service.name} has expired because "
                    f"it was not accepted within the response window.\n\n"
                    f"You can choose another available worker or reschedule your service on LAMO.\n\n"
                    f"Best regards,\nTeam LAMO"
                ),
                recipient_list=[b.customer.email],
                purpose='booking_expired',
                async_send=True,
            )

            # Also notify worker if one was assigned
            if b.worker and b.worker.user:
                Notification.objects.create(
                    recipient=b.worker.user,
                    booking=b,
                    notification_type='BOOKING_UPDATE',
                    message=f"Booking request #{b.id} for {b.service.name} has expired due to no response within the response window."
                )
    return expired_list


def check_and_expire_stale_accepted_bookings(customer=None, worker=None):
    """
    Expire ACCEPTED bookings that were never started and are older than 48 hours
    past their scheduled date. Gives customer 48 hours to report worker no-show,
    then automatically archives stale active bookings to EXPIRED status.
    Refunds any pre-payments and notifies both parties.
    """
    from datetime import timedelta
    from django.utils import timezone as dj_timezone

    cutoff_date = dj_timezone.localdate() - timedelta(days=2)
    qs = Booking.objects.filter(
        status='ACCEPTED',
        date__lt=cutoff_date,
    )
    if customer:
        qs = qs.filter(customer=customer)
    elif worker:
        qs = qs.filter(worker=worker)

    expired_list = []
    for candidate in qs.select_related('customer', 'service', 'worker__user'):
        with transaction.atomic():
            b = Booking.objects.select_for_update().filter(id=candidate.id).first()
            if not b or b.status != 'ACCEPTED':
                continue
            b.status = 'EXPIRED'
            b.cancellation_reason = 'Scheduled service date elapsed without being started (auto-expired after 48h grace window).'
            b.cancelled_by = 'SYSTEM'
            b.save(update_fields=['status', 'cancellation_reason', 'cancelled_by', 'updated_at'])
            expired_list.append(b)

            try:
                # The worker accepted (and was charged) but never started the
                # job: commission is forfeited, any customer advance is flagged.
                _handle_booking_cancellation_refund(
                    b, commission_refund=False,
                    refund_reason='Accepted job expired without being started',
                )
            except Exception:
                pass

            Notification.objects.create(
                recipient=b.customer,
                booking=b,
                notification_type='BOOKING_UPDATE',
                message=f"Booking #{b.id} for {b.service.name} was automatically closed because the scheduled date has passed without being started."
            )
            if b.worker and b.worker.user:
                Notification.objects.create(
                    recipient=b.worker.user,
                    booking=b,
                    notification_type='BOOKING_UPDATE',
                    message=f"Booking #{b.id} for {b.service.name} has been archived (scheduled date passed without being started)."
                )
    return expired_list


def _generate_recurring_dates(start_date, end_date, frequency, preferred_day):
    """Generate a list of dates for a recurring booking series."""
    from datetime import timedelta
    import calendar

    dates = []
    current = start_date
    day_map = {
        'Monday': 0, 'Tuesday': 1, 'Wednesday': 2, 'Thursday': 3,
        'Friday': 4, 'Saturday': 5, 'Sunday': 6
    }
    target_weekday = day_map.get(preferred_day, 0)

    if frequency == 'Daily':
        while current <= end_date:
            dates.append(current)
            current += timedelta(days=1)
    elif frequency == 'Weekly':
        # Find first occurrence of preferred_day on or after start_date
        while current.weekday() != target_weekday and current <= end_date:
            current += timedelta(days=1)
        while current <= end_date:
            dates.append(current)
            current += timedelta(weeks=1)
    elif frequency == 'Monthly':
        # Same day of month as start_date
        day_of_month = start_date.day
        while current <= end_date:
            dates.append(current)
            # Move to next month
            if current.month == 12:
                current = current.replace(year=current.year + 1, month=1)
            else:
                # Handle months with fewer days
                last_day = calendar.monthrange(current.year, current.month + 1)[1]
                next_day = min(day_of_month, last_day)
                current = current.replace(month=current.month + 1, day=next_day)

    return dates


def _get_customer_prefill(user):
    """Return saved customer profile information for prefilling booking forms."""
    prefill = {
        'name': '',
        'email': '',
        'phone': '',
        'location': '',
        'latitude': None,
        'longitude': None,
    }
    if not user or not user.is_authenticated or getattr(user, 'role', '') != 'USER':
        return prefill

    prefill['name'] = user.get_full_name() or user.username
    prefill['email'] = user.email or ''

    profile = getattr(user, 'user_profile', None)
    if profile:
        prefill['phone'] = profile.phone or ''
        prefill['location'] = profile.address or ''
        prefill['latitude'] = profile.latitude
        prefill['longitude'] = profile.longitude

    # Fallback to most recent booking if fields are empty
    if not prefill['phone'] or not prefill['location']:
        last_booking = user.bookings.order_by('-created_at').first()
        if last_booking:
            if not prefill['phone'] and last_booking.phone:
                prefill['phone'] = last_booking.phone
            if not prefill['location'] and last_booking.location:
                prefill['location'] = last_booking.location
                if prefill['latitude'] is None:
                    prefill['latitude'] = last_booking.latitude
                if prefill['longitude'] is None:
                    prefill['longitude'] = last_booking.longitude

    return prefill


def _generate_unique_customer_username(email, name=None):
    """Safely generate a unique, clean username for a customer account."""
    import secrets
    from django.utils.text import slugify

    base = ''
    if name:
        base = slugify(name).replace('-', '_')[:15]
    if not base or len(base) < 3:
        base = slugify(email.split('@')[0]).replace('-', '_')[:15]
    if not base or len(base) < 3:
        base = 'customer'

    candidate = base
    while CustomUser.objects.filter(username=candidate).exists():
        candidate = f"{base}_{secrets.token_hex(3)}"
        if len(candidate) > 30:
            candidate = f"cust_{secrets.token_hex(6)}"

    return candidate


# ---------------------------------------------------------------------------
# Booking-type validation (shared by every booking-creation path)
# ---------------------------------------------------------------------------
BOOKING_TYPE_ALIASES = {
    'one time': 'One Time',
    'onetime': 'One Time',
    'recurring': 'Recurring',
    'emergency': 'Emergency',
}


def normalize_booking_type(raw, default='One Time'):
    """Return the canonical booking type for client input, or None if unsupported.

    Case, spacing, '-' and '_' are ignored ('recurring', 'RECURRING', 'one_time'
    all normalise). A missing/blank value means ``default``. Anything else is
    unsupported and must be rejected by the caller.
    """
    value = ' '.join(str(raw if raw is not None else '').replace('_', ' ').replace('-', ' ').split()).lower()
    if not value:
        return default
    return BOOKING_TYPE_ALIASES.get(value)


RECURRING_DISABLED_MSG = 'Recurring bookings are temporarily unavailable. Please choose a one-time booking.'


def booking_type_error(booking_type):
    """Message when a *worker-selected* booking cannot use ``booking_type``, else None.

    Recurring is handled separately (it is temporarily disabled and has its own
    message); Emergency requests are created only by the dedicated emergency
    flow, which assigns no worker up front and applies emergency rules.
    """
    if booking_type is None:
        return 'Please choose a valid booking type.'
    if booking_type == 'Emergency':
        return 'Emergency requests must be raised from the emergency request option, not as a scheduled booking.'
    return None


def _create_booking_from_pending(request, user, pending_data):
    """Finalize a booking from validated pending session data after email verification."""
    from datetime import datetime

    worker_id = pending_data.get('worker_id')
    service_id = pending_data.get('service_id')
    booking_type = normalize_booking_type(pending_data.get('booking_type'))
    if booking_type_error(booking_type):
        messages.warning(request, booking_type_error(booking_type))
        return None
    date_str = pending_data.get('date')
    time = pending_data.get('time')
    location = pending_data.get('location', '')
    latitude = pending_data.get('latitude')
    longitude = pending_data.get('longitude')
    district = pending_data.get('district', '')
    description = pending_data.get('description', '')
    phone = pending_data.get('phone', '')
    payment_method = pending_data.get('payment_method', 'CASH')

    start_date_str = pending_data.get('start_date')
    end_date_str = pending_data.get('end_date')
    frequency = pending_data.get('frequency', 'Weekly')
    preferred_day = pending_data.get('preferred_day', '')

    worker = WorkerProfile.objects.select_related('user', 'service', 'wallet').filter(id=worker_id).first()
    if not worker:
        messages.warning(request, 'The selected worker could not be found. Please choose another worker.')
        return None

    service = Service.objects.filter(id=service_id, is_active=True).first()
    if not service:
        messages.warning(request, 'The selected service is currently inactive. Please choose another service.')
        return None

    # Revalidate critical worker status and availability after OTP verification (Finding B3)
    _pending_date = None
    try:
        from datetime import datetime as _dt
        _raw_pd = pending_data.get('date') or pending_data.get('start_date')
        _pending_date = _dt.strptime(_raw_pd, '%Y-%m-%d').date() if isinstance(_raw_pd, str) and _raw_pd else None
    except (ValueError, TypeError):
        _pending_date = None
    status_ok = worker.work_status == 'AVAILABLE' or (
        worker.work_status == 'ON_DUTY' and worker_on_duty_can_take(_pending_date)
    )
    if not worker.is_verified or not worker.user.is_active or not status_ok:
        messages.warning(request, 'The service professional is currently unavailable. Please choose another available worker.')
        return None

    is_wallet_locked = getattr(worker, 'is_paused_for_dues', False)
    if not is_wallet_locked:
        try:
            if not worker_can_afford_acceptance(worker):
                is_wallet_locked = True
        except Exception:
            pass
    if is_wallet_locked:
        messages.warning(request, 'The service professional is temporarily unavailable for new requests. Please choose another available worker.')
        return None

    start_t, end_t, dur_val, buf_val = compute_booking_window(
        time, service=service,
        duration_min=pending_data.get('duration_min'),
        buffer_min=pending_data.get('buffer_min', 15)
    )

    # Check for recurring booking temporary restriction
    if booking_type == 'Recurring':
        messages.warning(request, RECURRING_DISABLED_MSG)
        return None

        try:
            start_d = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            end_d = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return None

        booking_dates = _generate_recurring_dates(start_d, end_d, frequency, preferred_day)
        with transaction.atomic():
            locked_worker = WorkerProfile.objects.select_for_update().get(id=worker.id)

            # Defensive schedule re-validation for recurring series
            if locked_worker.is_paused_for_dues or not worker_can_afford_acceptance(locked_worker):
                messages.warning(
                    request,
                    'Your email was verified, but the service professional is temporarily unavailable for new requests. Please choose another available worker.'
                )
                return None

            for b_date in booking_dates:
                # Check worker leave
                try:
                    if WorkerLeave.objects.filter(worker=locked_worker, leave_date=b_date).exists():
                        messages.warning(
                            request,
                            f'Your email was verified, but the worker is on leave on {b_date.strftime("%Y-%m-%d")}. Please adjust the date range.'
                        )
                        return None
                except Exception:
                    pass

                # Check duplicate user booking
                duplicate_user_booking = Booking.objects.filter(
                    customer=user, worker=locked_worker, service=service,
                    date=b_date,
                    status__in=['PENDING', 'ACCEPTED', 'IN_PROGRESS']
                ).exists()
                if duplicate_user_booking:
                    messages.warning(
                        request,
                        f'Your email was verified, but you already have an active booking with this worker on {b_date.strftime("%Y-%m-%d")}.'
                    )
                    return None

                # Check worker availability
                if has_worker_schedule_conflict(locked_worker, booking_date=b_date, start_time=start_t, end_time=end_t, time_value=time):
                    messages.warning(
                        request,
                        f'Your email was verified, but this worker is already booked on {b_date.strftime("%Y-%m-%d")} at the selected time.'
                    )
                    return None

            series = BookingSeries.objects.create(
                customer=user,
                worker=locked_worker,
                service=service,
                frequency=frequency,
                preferred_day=preferred_day,
                time=time,
                start_time=start_t,
                end_time=end_t,
                duration_min=dur_val,
                buffer_min=buf_val,
                location=location,
                latitude=latitude,
                longitude=longitude,
                description=description,
                phone=phone,
                start_date=start_d,
                end_date=end_d,
            )
            created_bookings = []
            for b_date in booking_dates:
                b = Booking.objects.create(
                    customer=user,
                    worker=locked_worker,
                    service=service,
                    booking_type='Recurring',
                    date=b_date,
                    time=time,
                    start_time=start_t,
                    end_time=end_t,
                    duration_min=dur_val,
                    buffer_min=buf_val,
                    location=location,
                    description=description,
                    phone=phone,
                    latitude=latitude,
                    longitude=longitude,
                    district=worker.district,
                    estimated_cost=service.price,
                    status='PENDING',
                    series=series,
                    payment_method=payment_method,
                )
                created_bookings.append(b)

            if created_bookings:
                first_b = created_bookings[0]
                _create_idempotent_notification(
                    recipient=worker.user,
                    booking=first_b,
                    notification_type='BOOKING',
                    message=f'New recurring booking request from {user.get_full_name() or user.username} for {service.name}.'
                )
                send_web_push_async(
                    user=worker.user,
                    title=f"🔔 New Recurring Job: {service.name}",
                    body=f"New recurring booking from {user.get_full_name() or user.username}. Tap to view!",
                    url="/worker-dashboard/"
                )
                for admin in CustomUser.objects.filter(role='ADMIN', is_active=True):
                    _create_idempotent_notification(
                        recipient=admin,
                        booking=first_b,
                        notification_type='BOOKING',
                        message=f'New recurring booking series #{series.id} by {user.get_full_name() or user.username}.'
                    )
                send_email_safely(
                    subject=f'Recurring Booking Request Received - Series #{series.id}',
                    message=(
                        f'Hi {user.username},\n\n'
                        f'Your recurring booking request has been received!\n'
                        f'Service: {service.name}\n'
                        f'Worker: {worker.user.get_full_name() or worker.user.username}\n'
                        f'Occurrences: {len(created_bookings)}\n\n'
                        f'The worker will review and confirm your schedule shortly.\n\n'
                        f'Thanks for choosing LAMO!'
                    ),
                    recipient_list=[user.email],
                    purpose='booking_request',
                    async_send=True,
                )
                return first_b
    else:
        # One Time Booking
        try:
            b_date = datetime.strptime(date_str, '%Y-%m-%d').date() if isinstance(date_str, str) else date_str
        except (ValueError, TypeError):
            b_date = date_str

        existing_b = Booking.objects.filter(
            customer=user,
            worker=worker,
            service=service,
            date=b_date,
            time=time,
            status__in=['PENDING', 'ACCEPTED', 'IN_PROGRESS']
        ).first()
        if existing_b:
            return existing_b

        with transaction.atomic():
            locked_worker = WorkerProfile.objects.select_for_update().get(id=worker.id)

            # Defensive schedule re-validation: verify worker hasn't become unavailable or booked in the gap
            if locked_worker.is_paused_for_dues or not worker_can_afford_acceptance(locked_worker):
                messages.warning(
                    request,
                    'Your email was verified, but the service professional is temporarily unavailable for new requests. Please choose another available worker.'
                )
                return None

            try:
                if WorkerLeave.objects.filter(worker=locked_worker, leave_date=b_date).exists():
                    messages.warning(
                        request,
                        'Your email was verified, but the worker is marked on leave on the chosen date. Please choose an alternative date.'
                    )
                    return None
            except Exception:
                pass

            if has_worker_schedule_conflict(locked_worker, booking_date=b_date, start_time=start_t, end_time=end_t, time_value=time):
                messages.warning(
                    request,
                    'Your email was verified, but this time slot was just booked by another customer. Please choose an alternative time slot.'
                )
                return None

            booking = Booking.objects.create(
                customer=user,
                worker=locked_worker,
                service=service,
                booking_type=booking_type,
                date=b_date,
                time=time,
                start_time=start_t,
                end_time=end_t,
                duration_min=dur_val,
                buffer_min=buf_val,
                location=location,
                description=description,
                phone=phone,
                latitude=latitude,
                longitude=longitude,
                district=district or worker.district,
                estimated_cost=service.price,
                status='PENDING',
                payment_method=payment_method,
            )

            Notification.objects.create(
                recipient=worker.user,
                booking=booking,
                notification_type='BOOKING',
                message=f'New booking request from {user.get_full_name() or user.username} for {service.name} on {b_date}.'
            )
            send_web_push_async(
                user=worker.user,
                title=f"🔔 New Job: {service.name}",
                body=f"New booking from {user.get_full_name() or user.username} for {b_date} ({time}). Tap to accept!",
                url="/worker-dashboard/"
            )
            for admin in CustomUser.objects.filter(role='ADMIN', is_active=True):
                _create_idempotent_notification(
                    recipient=admin,
                    booking=booking,
                    notification_type='BOOKING',
                    message=f'New booking #{booking.id}: {service.name} booked by {user.get_full_name() or user.username}.'
                )

            send_email_safely(
                subject=f'Booking Request Received - #{booking.id}',
                message=(
                    f'Hi {user.username},\n\n'
                    f'Great! Your booking request has been submitted successfully.\n\n'
                    f'Here are the details:\n'
                    f'  Booking ID: #{booking.id}\n'
                    f'  Service: {service.name}\n'
                    f'  Worker: {worker.user.get_full_name() or worker.user.username}\n'
                    f'  District: {district or worker.district}\n'
                    f'  Date: {b_date}\n'
                    f'  Time: {time}\n\n'
                    f'The worker will review your request and accept it shortly. We\'ll notify you once it\'s confirmed.\n\n'
                    f'Thanks for choosing LAMO!'
                ),
                recipient_list=[user.email],
                purpose='booking_request',
                async_send=True,
            )

            # Ensure UserProfile has saved phone/address. A phone number is
            # stored on the account only when it was proven by OTP (B1/section
            # 4); otherwise it remains a booking contact detail.
            profile, _ = UserProfile.objects.get_or_create(user=user)
            profile_changed = False
            if (
                not profile.phone and phone and pending_data.get('phone_verified')
                and not (_user_ids_for_phone(_normalize_phone(phone)) - {user.id})
            ):
                profile.phone = _normalize_phone(phone) or phone
                profile_changed = True
            if not profile.address and location:
                profile.address = location
                profile_changed = True
            if latitude is not None and longitude is not None and profile.latitude is None:
                profile.latitude = latitude
                profile.longitude = longitude
                profile_changed = True
            if (district or worker.district) and not profile.district:
                profile.district = district or worker.district
                profile_changed = True
            if profile_changed:
                profile.save()

            return booking


def _handle_recurring_booking(request, worker, service, form_data, respond_error,
                               start_date_str, end_date_str, frequency, preferred_day, time,
                               location, latitude, longitude, description, phone):
    """Handle recurring booking - create series and individual bookings."""
    from datetime import datetime, date as date_type, timedelta

    # Validate required fields
    if not start_date_str:
        return respond_error('Please select a start date for the recurring booking.')
    if not end_date_str:
        return respond_error('Please select an end date for the recurring booking.')
    if not time:
        return respond_error('Please select a time.')
    if not location:
        return respond_error('Please enter the service location.')
    if not phone:
        return respond_error('Please enter your phone number.')

    # Parse dates
    try:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return respond_error('Invalid start date format.')
    try:
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return respond_error('Invalid end date format.')

    now_local = timezone.localtime()

    # Validate date range
    if start_date < now_local.date():
        return respond_error('Start date cannot be in the past.')
    if end_date <= start_date:
        return respond_error('End date must be after start date.')

    # Limit recurring bookings to 90 days max
    max_end_date = start_date + timedelta(days=90)
    if end_date > max_end_date:
        return respond_error('Recurring bookings cannot exceed 90 days.')

    # Generate all booking dates
    booking_dates = _generate_recurring_dates(start_date, end_date, frequency, preferred_day)

    if not booking_dates:
        return respond_error('No valid booking dates could be generated. Please check your frequency and date range.')

    # Limit max occurrences
    if len(booking_dates) > 30:
        return respond_error('Too many booking occurrences. Please reduce the date range or change frequency.')

    if worker.work_status == 'SUSPENDED':
        return respond_error('This worker is currently unavailable. Please choose another worker.')

    lat_val = _safe_coord(latitude)
    lng_val = _safe_coord(longitude)

    if lat_val is not None and lng_val is not None:
        location_district = _validate_location_in_district(lat_val, lng_val, worker.district)
        if location_district is None:
            return respond_error(f'Please select a service location within {worker.district}.')

    start_t, end_t, dur_val, buf_val = compute_booking_window(time, service=service)

    # A recurring booking is always at a slot the customer picked, so an
    # unusable value must be rejected rather than quietly degraded to an
    # exact-string comparison (which misses two different spellings of the
    # same slot and would allow a double booking).
    if start_t is None or end_t is None:
        return respond_error('Please select a valid time slot.')

    if booking_dates and booking_dates[0] == now_local.date() and start_t <= now_local.time():
        return respond_error('Cannot book a time slot that has already passed today.')

    with transaction.atomic():
        locked_worker = WorkerProfile.objects.select_for_update().get(id=worker.id)

        # Check for conflicts on each date; gracefully skip conflicting dates instead of aborting whole series
        valid_dates = []
        skipped_dates = []

        for booking_date in booking_dates:
            is_conflict = False
            # Check worker leave
            try:
                if WorkerLeave.objects.filter(worker=locked_worker, leave_date=booking_date).exists():
                    is_conflict = True
            except Exception:
                pass

            if not is_conflict:
                duplicate_user_booking = Booking.objects.filter(
                    customer=request.user, worker=locked_worker, service=service,
                    date=booking_date,
                    status__in=['PENDING', 'ACCEPTED', 'IN_PROGRESS']
                ).exists()
                if duplicate_user_booking:
                    is_conflict = True

            if not is_conflict:
                if has_worker_schedule_conflict(locked_worker, booking_date=booking_date, start_time=start_t, end_time=end_t):
                    is_conflict = True

            if is_conflict:
                skipped_dates.append(booking_date)
            else:
                valid_dates.append(booking_date)

        if not valid_dates:
            return respond_error(
                'The worker is unavailable at this time across all selected recurring dates. '
                'Please adjust your time slot or date range.'
            )

        # Create the booking series
        series = BookingSeries.objects.create(
            customer=request.user,
            worker=locked_worker,
            service=service,
            frequency=frequency,
            preferred_day=preferred_day,
            time=time,
            start_time=start_t,
            end_time=end_t,
            duration_min=dur_val,
            buffer_min=buf_val,
            location=location,
            latitude=lat_val,
            longitude=lng_val,
            description=description,
            phone=phone,
            start_date=start_date,
            end_date=end_date,
        )

        # Create individual bookings for each valid date
        bookings_created = []
        for booking_date in valid_dates:
            booking = Booking.objects.create(
                customer=request.user,
                worker=locked_worker,
                service=service,
                booking_type='Recurring',
                date=booking_date,
                time=time,
                start_time=start_t,
                end_time=end_t,
                duration_min=dur_val,
                buffer_min=buf_val,
                location=location,
                description=description,
                phone=phone,
                latitude=lat_val,
                longitude=lng_val,
                district=worker.district,
                estimated_cost=service.price,
                status='PENDING',
                series=series,
            )
            bookings_created.append(booking)

        # Notify worker about the series
        Notification.objects.create(
            recipient=worker.user,
            booking=bookings_created[0],
            notification_type='BOOKING',
            message=(
                f'New recurring booking request from '
                f'{request.user.get_full_name() or request.user.username} '
                f'for {service.name}. '
                f'{frequency} from {start_date_str} to {end_date_str}. '
                f'{len(bookings_created)} occurrences.'
            )
        )

        # Notify admins
        admins = CustomUser.objects.filter(role='ADMIN', is_active=True)
        for admin in admins:
            Notification.objects.create(
                recipient=admin,
                booking=bookings_created[0],
                notification_type='BOOKING',
                message=(
                    f'New recurring booking series #{series.id}: '
                    f'{service.name} booked by '
                    f'{request.user.get_full_name() or request.user.username}. '
                    f'{len(bookings_created)} occurrences.'
                )
            )

    # Send email confirmation (Resend-aware; failures are logged, never raised)
    send_email_safely(
        subject=f'Recurring Booking Request Received - Series #{series.id}',
        message=(
            f'Hi {request.user.username},\n\n'
            f'Your recurring booking has been submitted successfully.\n\n'
            f'Series ID: #{series.id}\n'
            f'Service: {service.name}\n'
            f'Worker: {worker.user.get_full_name() or worker.user.username}\n'
            f'Frequency: {frequency}\n'
            f'Start Date: {start_date_str}\n'
            f'End Date: {end_date_str}\n'
            f'Time: {time}\n'
            f'Total Occurrences: {len(bookings_created)}\n\n'
            f'The worker will review your request and accept bookings.\n\n'
            f'Thanks for choosing LAMO!'
        ),
        recipient_list=[request.user.email],
        purpose='booking_recurring_confirmation',
        async_send=True,
    )

    skip_msg = ""
    if skipped_dates:
        skipped_str = ", ".join([d.strftime('%b %d') for d in skipped_dates[:3]])
        if len(skipped_dates) > 3:
            skipped_str += f" and {len(skipped_dates) - 3} more"
        skip_msg = f" (Note: {len(skipped_dates)} date(s) [{skipped_str}] were skipped due to prior commitments)."

    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
    messages.success(
        request,
        f'Recurring booking created successfully! {len(bookings_created)} occurrences scheduled.{skip_msg}'
    )

    if is_ajax:
        return JsonResponse({
            'success': True,
            'redirect_url': reverse('my_bookings'),
            'message': f'Recurring booking created! {len(bookings_created)} occurrences scheduled.{skip_msg}'
        })

    return redirect('my_bookings')


def book_service(request):
    if request.user.is_authenticated and request.user.role != 'USER':
        messages.error(request, 'Only users can book services.')
        return redirect('dashboard')

    customer_prefill = _get_customer_prefill(request.user) if request.user.is_authenticated else {}

    if request.method == 'GET':
        worker_id = request.GET.get('worker')
        service_id = request.GET.get('service')

        worker = None
        service = None
        district = ''

        if not service_id and not worker_id:
            messages.info(
                request,
                'Please choose a service first, then pick a worker to book.',
            )
            return redirect('service_page')

        if worker_id:
            worker = get_object_or_404(
                WorkerProfile.objects.select_related('user', 'service'),
                id=worker_id,
                is_verified=True
            )
            district = worker.district

        if service_id:
            service = get_object_or_404(Service, id=service_id, is_active=True)

        if worker and service:
            if worker.service_id != service.id:
                messages.error(request, 'Selected worker does not provide this service.')
                return redirect('service_detail', service.id)

        district_lat, district_lng = DISTRICT_COORDINATES.get(district, (9.9312, 76.2673)) if district else (9.9312, 76.2673)

        from django.conf import settings
        map_tile_url = getattr(settings, 'MAP_TILE_URL', '')
        map_tile_attribution = getattr(settings, 'MAP_TILE_ATTRIBUTION', '')
        map_tile_subdomains = getattr(settings, 'MAP_TILE_SUBDOMAINS', '')
        map_tile_max_zoom = getattr(settings, 'MAP_TILE_MAX_ZOOM', 20)

        prefill_data = {
            'date': request.GET.get('date', '').strip(),
            'time': request.GET.get('time', '').strip(),
            'location': request.GET.get('location', '').strip() or customer_prefill.get('location', ''),
            'phone': request.GET.get('phone', '').strip() or customer_prefill.get('phone', ''),
        }

        return render(request, 'user/book_service.html', {
            'worker': worker,
            'service': service,
            'district': district,
            'district_lat': district_lat,
            'district_lng': district_lng,
            'prefill_data': prefill_data,
            'customer_prefill': customer_prefill,
            'map_tile_url': map_tile_url,
            'map_tile_attribution': map_tile_attribution,
            'map_tile_subdomains': map_tile_subdomains,
            'map_tile_max_zoom': map_tile_max_zoom,
        })

    if request.method == 'POST':
        worker_id = request.POST.get('worker')
        service_id = request.POST.get('service')
        booking_type = request.POST.get('booking_type', 'One Time')
        date = request.POST.get('date')
        time = request.POST.get('time')
        time_slot = request.POST.get('time_slot', '').strip()
        custom_time = request.POST.get('custom_time', '').strip()
        location = request.POST.get('location', '').strip()
        latitude = request.POST.get('latitude', '').strip()
        longitude = request.POST.get('longitude', '').strip()
        description = request.POST.get('description', '').strip()
        phone = request.POST.get('phone', '').strip()

        customer_name = request.POST.get('customer_name', '').strip()
        customer_email = request.POST.get('customer_email', '').strip()
        consent_raw = request.POST.get('create_account_consent')
        consent = consent_raw in ['1', 'true', 'on', True]

        start_date = request.POST.get('start_date', '').strip()
        end_date = request.POST.get('end_date', '').strip()
        frequency = request.POST.get('frequency', 'Weekly').strip()
        preferred_day = request.POST.get('preferred_day', '').strip()
        payment_method = request.POST.get('payment_method', '')

        if not time:
            if custom_time:
                time = custom_time
            elif time_slot:
                time = time_slot

        is_emergency = (not worker_id and request.POST.get('problem'))

        if is_emergency:
            return _handle_emergency_booking(request, service_id, time, location, latitude, longitude, description, phone)

        # Server-side booking-type validation (the client radio buttons are not
        # a security boundary). Normalises case/spacing, so 'recurring' and
        # 'RECURRING' reach the recurring block below instead of bypassing it.
        booking_type = normalize_booking_type(request.POST.get('booking_type'))
        # New recurring bookings are disabled for EVERY caller (logged-in or guest,
        # ajax or form). Rejected up front, before any OTP is sent or row written.
        if booking_type == 'Recurring':
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'error_message': RECURRING_DISABLED_MSG}, status=400)
            messages.warning(request, RECURRING_DISABLED_MSG)
            return redirect('service_list')
        _bt_error = booking_type_error(booking_type)
        if _bt_error:
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'error_message': _bt_error}, status=400)
            messages.error(request, _bt_error)
            return redirect('service_list')

        # Payment preference: only supported values, never silently replaced.
        payment_method, _pm_error = clean_payment_method(
            payment_method, BOOKING_PAYMENT_PREFERENCES, default='CASH'
        )
        if _pm_error:
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'error_message': _pm_error}, status=400)
            messages.error(request, _pm_error)
            return redirect('service_list')

        form_data = {
            'booking_type': booking_type,
            'date': date,
            'time': time,
            'location': location,
            'latitude': latitude,
            'longitude': longitude,
            'description': description,
            'phone': phone,
            'start_date': start_date,
            'end_date': end_date,
            'frequency': frequency,
            'preferred_day': preferred_day,
            'payment_method': payment_method,
            'customer_name': customer_name,
            'customer_email': customer_email,
            'create_account_consent': consent,
        }

        if not worker_id:
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'error_message': 'Please select a worker.'}, status=400)
            messages.error(request, 'Please select a worker.')
            return redirect('service_list')

        worker = get_object_or_404(
            WorkerProfile.objects.select_related('user', 'service'),
            id=worker_id,
            is_verified=True
        )

        if not service_id:
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'error_message': 'Please select a service.'}, status=400)
            messages.error(request, 'Please select a service.')
            return redirect('service_list')

        service = get_object_or_404(Service, id=service_id, is_active=True)

        if worker.service_id != service.id:
            messages.error(request, 'This worker is not assigned to the selected service.')
            return redirect('service_detail', service.id)

        district = worker.district

        other_workers = WorkerProfile.objects.filter(
            service=service, is_verified=True, work_status='AVAILABLE'
        ).exclude(id=worker.id).select_related('user')[:4]

        is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'

        def respond_error(err_msg, requires_login=False):
            if is_ajax:
                data = {'success': False, 'error_message': err_msg}
                if requires_login:
                    data['requires_login'] = True
                    data['redirect_url'] = reverse('login') + f'?next={request.get_full_path()}'
                return JsonResponse(data, status=400)
            district_lat, district_lng = DISTRICT_COORDINATES.get(district, (9.9312, 76.2673)) if district else (9.9312, 76.2673)
            return render(request, 'user/book_service.html', {
                'worker': worker,
                'service': service,
                'district': district,
                'district_lat': district_lat,
                'district_lng': district_lng,
                'error_message': err_msg,
                'form_data': form_data,
                'customer_prefill': customer_prefill,
                'other_workers': list(other_workers),
            })

        target_user = None
        if not request.user.is_authenticated:
            # Guest Customer Validations (no consent checkbox needed)
            if not customer_name or len(customer_name) < 2:
                return respond_error('Please enter your full name.')

            auth_identifier = request.POST.get('auth_identifier', '').strip() or customer_email or phone
            if not auth_identifier:
                return respond_error('Please enter your email or mobile number.')

            if '@' in auth_identifier:
                customer_email = auth_identifier
            else:
                # Phone channel: the verified number is the contact number.
                phone = _normalize_phone(auth_identifier) or phone

            if not _normalize_phone(phone):
                return respond_error('Please enter a valid 10-digit Indian mobile number.')
            phone = _normalize_phone(phone)

            # Audit B1: the account is identified ONLY by the identifier the
            # OTP is sent to; a contact phone belonging to another account is
            # rejected (never adopted), and worker/admin accounts are refused.
            identity = _resolve_guest_identity(auth_identifier, phone)
            if identity['error']:
                return respond_error(identity['error'])
            auth_identifier = identity['verified_identifier']
            target_user = identity['user']

        if not location:
            return respond_error('Please enter the service location.')
        if not phone:
            return respond_error('Please enter your phone number.')

        # Requested service date (one-time date or recurring start date), used
        # to let ON_DUTY workers take bookings for later days.
        _requested_date = None
        try:
            from datetime import datetime as _dt
            _raw_req_date = (date or request.POST.get('start_date') or '').strip()
            _requested_date = _dt.strptime(_raw_req_date, '%Y-%m-%d').date() if _raw_req_date else None
        except (ValueError, TypeError):
            _requested_date = None

        if worker.work_status != 'AVAILABLE' and not (
            worker.work_status == 'ON_DUTY' and worker_on_duty_can_take(_requested_date)
        ):
            if worker.work_status == 'LEAVE':
                return respond_error('This service professional is currently on leave. Please choose another worker.')
            elif worker.work_status == 'ON_DUTY':
                return respond_error('This service professional is working on a job right now. Please choose a later date or another worker.')
            else:
                return respond_error('This service professional is currently unavailable. Please choose another worker.')

        # H2: a worker whose prepaid wallet cannot cover the acceptance
        # commission could never accept, so the request would only sit until
        # it expired. Refuse it up front instead.
        if getattr(worker, 'is_paused_for_dues', False) or not worker_can_afford_acceptance(worker):
            return respond_error('This service professional is temporarily unavailable for new requests. Please choose another available worker.')

        lat_val = _safe_coord(latitude)
        lng_val = _safe_coord(longitude)

        if lat_val is not None and lng_val is not None:
            location_district = _validate_location_in_district(lat_val, lng_val, district)
            if location_district is None:
                return respond_error(f'Please select a service location within {district}.')

        custom_dur_post = request.POST.get('duration_min', '').strip()
        custom_dur_val = None
        if custom_dur_post:
            try:
                c_val = int(custom_dur_post)
                if 15 <= c_val <= 360:
                    custom_dur_val = c_val
            except (ValueError, TypeError):
                pass

        # Dynamic travel buffer estimation based on customer to worker coordinates
        cust_dist_km = None
        if lat_val is not None and lng_val is not None and getattr(worker, 'latitude', None) and getattr(worker, 'longitude', None):
            try:
                from .location_data import _haversine
                cust_dist_km = _haversine(float(lat_val), float(lng_val), float(worker.latitude), float(worker.longitude))
            except Exception:
                cust_dist_km = None

        start_t, end_t, dur_val, buf_val = compute_booking_window(
            time, service=service, duration_min=custom_dur_val, distance_km=cust_dist_km
        )
        if start_t is None or end_t is None:
            if custom_time:
                return respond_error('Please enter a valid custom time (e.g., 2:30 PM or 5:00 PM).')
            return respond_error('Please select an available time slot.')

        # Validate worker working shift hours
        shift_start = getattr(worker, 'work_start_time', None) or datetime_time(9, 0)
        shift_end = getattr(worker, 'work_end_time', None) or datetime_time(18, 0)
        if start_t < shift_start or end_t > shift_end:
            s_str = shift_start.strftime('%I:%M %p').lstrip('0')
            e_str = shift_end.strftime('%I:%M %p').lstrip('0')
            return respond_error(
                f"This professional's working hours are {s_str} to {e_str}. "
                f"Please select a time slot within their shift."
            )

        # -------------------------------------------------------------------------
        # ONE TIME BOOKING PATH
        # -------------------------------------------------------------------------
        if not date:
            return respond_error('Please select a date.')

        from datetime import datetime
        try:
            booking_date = datetime.strptime(date, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return respond_error('Invalid date format.')

        now_local = timezone.localtime()
        if booking_date < now_local.date():
            return respond_error('Cannot book for past dates.')

        if booking_date == now_local.date():
            if start_t <= now_local.time():
                return respond_error('Cannot book a time slot that has already passed today.')
            from datetime import timedelta

            # Dynamic lead time calculation based on customer-worker distance
            dist_km = None
            try:
                c_lat = float(latitude) if latitude else None
                c_lng = float(longitude) if longitude else None
                if c_lat is not None and c_lng is not None:
                    dist_km, _ = _compute_worker_distance(c_lat, c_lng, worker)
            except Exception:
                pass

            lead_mins = compute_dynamic_lead_time_min(dist_km)
            min_lead_cutoff = (now_local + timedelta(minutes=lead_mins)).time()
            if start_t < min_lead_cutoff:
                lead_time_formatted = (now_local + timedelta(minutes=lead_mins)).strftime('%I:%M %p').lstrip('0')
                dist_note = f" (~{dist_km:.1f} km away)" if dist_km is not None else ""
                return respond_error(
                    f'For same-day service, please choose a time at least {lead_mins} minutes in advance '
                    f'(after {lead_time_formatted}){dist_note} to allow your professional time to prepare and travel.'
                )

        if booking_date == now_local.date() and worker.work_status == 'LEAVE':
            return respond_error('This worker is on leave today. Please choose another date.')

        worker_on_leave = False
        try:
            worker_on_leave = WorkerLeave.objects.filter(
                worker=worker, leave_date=booking_date
            ).exists()
        except Exception:
            pass

        if worker_on_leave:
            return respond_error('This worker is on leave on the selected date. Please choose another date.')

        with transaction.atomic():
            locked_worker = WorkerProfile.objects.select_for_update().get(id=worker.id)

            if locked_worker.is_paused_for_dues or not worker_can_afford_acceptance(locked_worker):
                return respond_error('This service professional is temporarily unavailable for new requests. Please choose another available worker.')

            check_user = request.user if request.user.is_authenticated else target_user
            if check_user:
                duplicate_user_booking = Booking.objects.filter(
                    customer=check_user, worker=locked_worker, service=service, date=date,
                    status__in=['PENDING', 'ACCEPTED', 'IN_PROGRESS']
                ).exists()

                if duplicate_user_booking:
                    return respond_error('You already have an active booking with this worker for the same service and date.')

            existing_booking = has_worker_schedule_conflict(
                locked_worker,
                booking_date=booking_date,
                start_time=start_t,
                end_time=end_t,
                exclude_booking_id=None,
            )

            if existing_booking:
                return respond_error('This worker is already booked for the selected time.')

            # Unauthenticated Customer: Zero DB writes before verification!
            if not request.user.is_authenticated:
                _otp, otp_sent = _send_booking_verification_code(request, auth_identifier, customer_name, phone)
                if not otp_sent:
                    target_dest = "email" if '@' in auth_identifier else "mobile number"
                    return respond_error(f'Could not send verification code to your {target_dest}. Please check and try again.')

                request.session['pending_booking_data'] = {
                    'worker_id': locked_worker.id,
                    'service_id': service.id,
                    'booking_type': booking_type,
                    'date': date,
                    'time': time,
                    'start_time': start_t.strftime('%H:%M:%S') if start_t else None,
                    'end_time': end_t.strftime('%H:%M:%S') if end_t else None,
                    'duration_min': dur_val,
                    'buffer_min': buf_val,
                    'location': location,
                    'latitude': lat_val,
                    'longitude': lng_val,
                    'district': district,
                    'description': description,
                    'phone': phone,
                    'customer_name': customer_name,
                    'payment_method': payment_method,
                    'created_at': timezone.now().isoformat(),
                }

                if is_ajax:
                    return JsonResponse({
                        'success': True,
                        'requires_otp': True,
                        'requires_code': True,
                        'target': auth_identifier,
                        'email': customer_email,
                        'phone': phone,
                        'auth_method': 'email' if '@' in auth_identifier else 'mobile',
                        'redirect_url': reverse('verify_otp'),
                        'message': f'Verification code sent to {auth_identifier}.'
                    })
                messages.info(request, f'Please verify your code sent to {auth_identifier} to complete your booking.')
                return redirect('verify_otp')

            # Authenticated Customer Booking Creation
            booking = Booking.objects.create(
                customer=request.user, worker=locked_worker, service=service,
                booking_type=booking_type, date=date, time=time,
                start_time=start_t, end_time=end_t,
                duration_min=dur_val, buffer_min=buf_val,
                location=location, description=description, phone=phone,
                latitude=lat_val,
                longitude=lng_val,
                district=district,
                estimated_cost=service.price, status='PENDING',
                payment_method=payment_method
            )

            Notification.objects.create(
                recipient=worker.user, booking=booking, notification_type='BOOKING',
                message=f'New booking request from {request.user.get_full_name() or request.user.username} for {service.name} on {date}.'
            )
            send_web_push_async(
                user=worker.user,
                title=f"🔔 New Job: {service.name}",
                body=f"New booking from {request.user.get_full_name() or request.user.username} for {date} ({time}). Tap to accept!",
                url="/worker-dashboard/"
            )

            admins = CustomUser.objects.filter(role='ADMIN', is_active=True)
            for admin in admins:
                Notification.objects.create(
                    recipient=admin, booking=booking, notification_type='BOOKING',
                    message=f'New booking #{booking.id}: {service.name} booked by {request.user.get_full_name() or request.user.username}.'
                )

        # Update returning customer's UserProfile with any updated details
        try:
            cust_profile, _ = UserProfile.objects.get_or_create(user=request.user)
            cust_dirty = False
            # The booking keeps its contact phone; the account phone changes
            # only through the OTP-verified profile flow (audit section 4).
            if location and location != cust_profile.address:
                cust_profile.address = location
                cust_dirty = True
            if lat_val is not None and lng_val is not None:
                cust_profile.latitude = lat_val
                cust_profile.longitude = lng_val
                cust_dirty = True
            if district and not cust_profile.district:
                cust_profile.district = district
                cust_dirty = True
            if cust_dirty:
                cust_profile.save()
        except Exception:
            pass

        # Booking request confirmation (Resend-aware)
        send_email_safely(
            subject=f'Booking Request Received - #{booking.id}',
            message=(
                f'Hi {request.user.username},\n\n'
                f'Great! Your booking request has been submitted successfully.\n\n'
                f'Here are the details:\n'
                f'  Booking ID: #{booking.id}\n'
                f'  Service: {service.name}\n'
                f'  Worker: {worker.user.get_full_name() or worker.user.username}\n'
                f'  District: {district}\n'
                f'  Date: {date}\n'
                f'  Time: {time}\n\n'
                f'The worker will review your request and accept it shortly. We\'ll notify you once it\'s confirmed.\n\n'
                f'Thanks for choosing LAMO!'
            ),
            recipient_list=[request.user.email],
            purpose='booking_request',
            async_send=True,
        )

        messages.success(request, 'Booking submitted successfully!')

        if is_ajax:
            arrival_pin = getattr(booking, 'arrival_otp', '') or ''
            worker_name = (worker.user.get_full_name() or worker.user.username) if (worker and worker.user) else ''
            return JsonResponse({
                'success': True,
                'redirect_url': reverse('my_bookings'),
                'booking_id': booking.id,
                'worker_name': worker_name,
                'service_name': service.name if service else '',
                'arrival_pin': str(arrival_pin) if arrival_pin else '',
                'scheduled_date': str(date) if date else '',
                'scheduled_time': str(time) if time else '',
                'message': 'Booking submitted successfully!'
            })

        return redirect('my_bookings')


# ============================================================
# MANUAL PAYMENT CONFIRMATION
# ============================================================
# LAMO has no payment-gateway integration. A customer submission is only a
# manual payment report; an admin must verify it before the booking is paid.

# Payment can only be reported after the customer has confirmed the final
# bill (COMPLETION_REQUESTED with bill_confirmed_at set), or for a legacy
# COMPLETED booking that is still unpaid. Never before the work is billed.
MANUAL_PAYMENT_ALLOWED_STATUSES = (
    'COMPLETION_REQUESTED',
    'COMPLETED',
)


def booking_payment_reportable(booking):
    """True when the customer may report (and see instructions for) payment."""
    if booking.is_paid:
        return False
    if booking.status == 'COMPLETION_REQUESTED':
        return bool(booking.bill_confirmed_at)
    return booking.status == 'COMPLETED'

# Payment methods the UI offers. Anything else falls back to UPI.
MANUAL_PAYMENT_METHODS = ('UPI', 'CARD', 'NETBANKING', 'CASH')
# Supported customer payment methods (CARD and NETBANKING are temporarily hidden until a certified payment gateway is integrated)
ACTIVE_CUSTOMER_PAYMENT_METHODS = ('UPI', 'CASH')
# Payment *preference* captured when a booking is created (not a payment).
BOOKING_PAYMENT_PREFERENCES = ('CASH', 'ONLINE')
CARD_NETBANKING_UNAVAILABLE_MSG = (
    'Card and Netbanking payments are temporarily unavailable. '
    'Please pay your professional directly via Cash or UPI.'
)


def clean_payment_method(raw, allowed, default=None):
    """Validate a submitted payment method. Returns (method, error_message).

    The value is trimmed and upper-cased, then must be one of ``allowed``.
    Nothing is ever silently converted to another method: CARD/NETBANKING and
    unknown values are rejected with a message. A blank value uses ``default``
    when one is given (the booking form's existing "Cash" default) and is an
    error otherwise.
    """
    value = (raw or '').strip().upper()
    if not value:
        if default:
            return default, None
        return None, 'Please choose how you paid (Cash or UPI).'
    if value in ('CARD', 'NETBANKING'):
        return None, CARD_NETBANKING_UNAVAILABLE_MSG
    if value not in allowed:
        return None, 'Unsupported payment method. Please choose Cash or UPI.'
    return value, None


@login_required
def confirm_payment(request, booking_id):
    """Record a customer's manual payment report for their own booking."""
    if request.user.role != 'USER':
        messages.error(request, 'Only customers can confirm payments.')
        return redirect('dashboard')

    if request.method != 'POST':
        return redirect('my_bookings')

    booking = get_object_or_404(
        Booking.objects.select_related('customer', 'service'),
        id=booking_id,
        customer=request.user
    )

    if booking.is_paid:
        messages.info(request, 'This booking has already been paid.')
        return redirect('my_bookings')

    # Payment is only possible after the customer confirmed the final bill.
    if booking.status not in MANUAL_PAYMENT_ALLOWED_STATUSES or not booking_payment_reportable(booking):
        messages.error(
            request,
            'Payment opens after your professional submits the final bill and you confirm it.'
        )
        return redirect('my_bookings')

    # The amount always comes from the server (the confirmed final bill),
    # never from the request.
    amount = booking.final_amount or booking.estimated_cost or booking.service.price
    if amount is None or amount <= 0:
        messages.error(
            request,
            'The payable amount for this booking is not available yet. '
            'Please contact support.'
        )
        return redirect('my_bookings')

    payment_method, payment_error = clean_payment_method(
        request.POST.get('payment_method'), ACTIVE_CUSTOMER_PAYMENT_METHODS
    )
    if payment_error:
        messages.error(request, payment_error)
        return redirect('my_bookings')

    # A reference typed by the customer is kept only when it is unique.
    # Payment.payment_reference is unique, so a clash must never reach the DB.
    client_reference = request.POST.get('payment_reference', '').strip()[:100]

    def build_server_reference():
        return 'LAMO-{0}-{1}'.format(
            booking.id,
            timezone.now().strftime('%Y%m%d%H%M%S%f'),
        )

    with transaction.atomic():
        # Lock the booking so two rapid submissions cannot both write a payment.
        locked_booking = Booking.objects.select_for_update().get(id=booking.id)

        payment, created = Payment.objects.get_or_create(booking=locked_booking)

        if not created and payment.status == 'PENDING':
            messages.info(
                request,
                'Your manual payment report is awaiting admin verification.'
            )
            return redirect('my_bookings')

        if payment.status == 'PAID':
            messages.info(request, 'This booking has already been paid.')
            return redirect('my_bookings')

        reference = client_reference
        if reference and Payment.objects.filter(
            payment_reference=reference
        ).exclude(pk=payment.pk).exists():
            reference = ''

        payment.amount = amount
        # Record the method the customer actually chose in this request.
        payment.payment_method = payment_method
        payment.payment_reference = reference or build_server_reference()
        payment.gateway = 'manual'
        payment.status = 'PENDING'
        payment.paid_at = None

        try:
            payment.save()
        except IntegrityError:
            # Another payment took the same reference in the meantime.
            payment.payment_reference = build_server_reference()
            payment.save()

        locked_booking.payment_method = payment_method
        locked_booking.save(update_fields=['payment_method', 'updated_at'])

    if booking.worker and booking.worker.user:
        _create_idempotent_notification(
            recipient=booking.worker.user,
            booking=booking,
            notification_type='PAYMENT',
            message=(
                f'The customer reports paying ₹{amount:.2f} by {payment_method} for Booking #{booking.id}. '
                f'Please check and tap "Payment received" to complete the job.'
            )
        )

    messages.success(
        request,
        'Thanks! Your payment has been reported. The booking will be marked complete '
        'once your professional (or our team) confirms the payment was received.'
    )
    return redirect('my_bookings')


@login_required
@require_POST
def admin_confirm_payment(request, payment_id):
    if request.user.role != 'ADMIN':
        messages.error(request, 'You are not authorized to confirm payments.')
        return redirect('dashboard')

    payment = get_object_or_404(
        Payment,
        pk=payment_id,
        gateway='manual',
    )

    with transaction.atomic():
        booking = Booking.objects.select_for_update().get(
            pk=payment.booking_id
        )
        payment = Payment.objects.select_for_update().get(pk=payment.pk)

        if booking.status not in MANUAL_PAYMENT_ALLOWED_STATUSES or not booking_payment_reportable(booking):
            messages.error(
                request,
                'This booking cannot be marked paid at its current status '
                '(the customer must confirm the final bill first).'
            )
            return redirect('admin_payments')

        if payment.status != 'PENDING':
            messages.info(request, 'This payment is no longer awaiting verification.')
            return redirect('admin_payments')

        # Verified payment completes the job (same path as a worker
        # confirming receipt): Payment PAID, booking COMPLETED and paid.
        _booking, error = _complete_booking_after_payment(
            booking.id,
            payment_method=payment.payment_method,
            verified_by='ADMIN',
            reference=payment.payment_reference,
        )
        if error:
            messages.error(request, error)
            return redirect('admin_payments')

    messages.success(request, 'Payment verified and booking marked as completed and paid.')
    return redirect('admin_payments')


@login_required
def booking_messages(request, booking_id):
    booking = get_object_or_404(
        Booking.objects.select_related('customer', 'worker__user', 'service'),
        id=booking_id,
    )

    allowed_users = [booking.customer]
    if booking.worker and booking.worker.user:
        allowed_users.append(booking.worker.user)

    if request.user not in allowed_users:
        return HttpResponseForbidden('You are not authorized to view this booking conversation.')

    if booking.status not in {'ACCEPTED', 'IN_PROGRESS', 'COMPLETION_REQUESTED', 'COMPLETED', 'DISPUTED'}:
        return HttpResponseForbidden('Chat is available only for accepted, in-progress, completed, or disputed bookings.')

    if request.method == 'POST':
        text = request.POST.get('message', '').strip()
        if not text:
            messages.error(request, 'Please enter a message.')
            return redirect('booking_messages', booking_id=booking.id)

        BookingMessage.objects.create(
            booking=booking,
            sender=request.user,
            message=text,
        )

        if request.user == booking.customer:
            recipient = booking.worker.user if booking.worker else None
        else:
            recipient = booking.customer
        if recipient and recipient != request.user:
            Notification.objects.create(
                recipient=recipient,
                booking=booking,
                notification_type='BOOKING_UPDATE',
                message=f"New booking message from {request.user.get_full_name() or request.user.username} for Booking #{booking.id}."
            )

        messages.success(request, 'Message sent successfully.')
        return redirect('booking_messages', booking_id=booking.id)

    thread = BookingMessage.objects.filter(booking=booking).select_related('sender').order_by('created_at')
    template_name = 'workers/booking_messages_worker.html' if getattr(request.user, 'role', None) == 'WORKER' else 'booking_messages.html'
    return render(
        request,
        template_name,
        {
            'booking': booking,
            'chat_messages': thread,
        }
    )


# ============================================================
# IN-APP WEBRTC MASKED AUDIO CALLING (ZERO PHONE SHARING)
# ============================================================

@login_required
@require_POST
def api_call_initiate(request, booking_id):
    """Initiates an in-app WebRTC audio call between customer and assigned worker."""
    booking = get_object_or_404(
        Booking.objects.select_related('customer', 'worker__user', 'service'),
        id=booking_id
    )

    # Authorization
    is_cust = (request.user == booking.customer)
    is_work = (booking.worker and booking.worker.user == request.user)
    if not (is_cust or is_work):
        return JsonResponse({'error': 'You are not authorized to call on this booking.'}, status=403)

    if booking.status in {'COMPLETED', 'REJECTED', 'CANCELLED', 'EXPIRED'}:
        return JsonResponse({'error': f'Calls cannot be initiated for {booking.status.lower()} bookings.'}, status=400)

    receiver = booking.worker.user if is_cust else booking.customer
    if not receiver:
        return JsonResponse({'error': 'No service professional is currently assigned to this booking.'}, status=400)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        data = {}

    offer_sdp = (data.get('offer_sdp') or '').strip()
    if not offer_sdp:
        return JsonResponse({'error': 'Valid WebRTC offer SDP is required.'}, status=400)

    # Ensure strict RFC 4566 CRLF termination (\r\n) for WebRTC parsers
    clean_offer_lines = [l.strip() for l in offer_sdp.replace('\r\n', '\n').replace('\r', '\n').split('\n') if l.strip()]
    offer_sdp = '\r\n'.join(clean_offer_lines) + '\r\n'

    # Cancel any hanging calls older than 30s
    stale_cutoff = timezone.now() - timedelta(seconds=30)
    CallSession.objects.filter(booking=booking, status__in=['INITIATED', 'RINGING'], started_at__lt=stale_cutoff).update(status='MISSED')

    call = CallSession.objects.create(
        booking=booking,
        caller=request.user,
        receiver=receiver,
        status='RINGING',
        offer_sdp=offer_sdp,
    )

    # Send in-app notification
    Notification.objects.create(
        recipient=receiver,
        booking=booking,
        notification_type='BOOKING_UPDATE',
        message=f"📞 Incoming LAMO In-App Call from {request.user.get_full_name() or request.user.username} for Booking #{booking.id}."
    )

    return JsonResponse({
        'success': True,
        'call_id': call.id,
        'receiver_name': receiver.get_full_name() or receiver.username,
        'receiver_role': 'Service Professional' if is_cust else 'Customer',
        'service_name': booking.service.name,
    })


@login_required
@require_POST
def api_call_answer(request, booking_id, call_id):
    """Submits WebRTC answer SDP from the call receiver."""
    call = get_object_or_404(CallSession, id=call_id, booking_id=booking_id)

    if request.user != call.receiver:
        return JsonResponse({'error': 'Only the call recipient can answer.'}, status=403)

    if call.status in {'ENDED', 'MISSED', 'REJECTED'}:
        return JsonResponse({'error': 'This call session has already concluded.'}, status=400)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        data = {}

    answer_sdp = (data.get('answer_sdp') or '').strip()
    if not answer_sdp:
        return JsonResponse({'error': 'Valid WebRTC answer SDP is required.'}, status=400)

    # Ensure strict RFC 4566 CRLF termination (\r\n) for WebRTC parsers
    clean_answer_lines = [l.strip() for l in answer_sdp.replace('\r\n', '\n').replace('\r', '\n').split('\n') if l.strip()]
    answer_sdp = '\r\n'.join(clean_answer_lines) + '\r\n'

    call.answer_sdp = answer_sdp
    call.status = 'CONNECTED'
    call.connected_at = timezone.now()
    call.save(update_fields=['answer_sdp', 'status', 'connected_at'])

    return JsonResponse({'success': True, 'status': 'CONNECTED'})


@login_required
def api_call_poll(request, booking_id, call_id):
    """Polls WebRTC call status, answer SDP, and ICE candidates."""
    call = get_object_or_404(CallSession, id=call_id, booking_id=booking_id)

    if request.user not in {call.caller, call.receiver}:
        return JsonResponse({'error': 'Unauthorized.'}, status=403)

    # Auto-expire ringing calls after 45 seconds
    if call.status == 'RINGING' and (timezone.now() - call.started_at) > timedelta(seconds=45):
        call.status = 'MISSED'
        call.ended_at = timezone.now()
        call.save(update_fields=['status', 'ended_at'])

    # Return remote ICE candidates
    try:
        if request.user == call.caller:
            remote_candidates = json.loads(call.receiver_candidates_json or '[]')
        else:
            remote_candidates = json.loads(call.caller_candidates_json or '[]')
    except Exception:
        remote_candidates = []

    dur = 0
    if call.status == 'CONNECTED' and call.connected_at:
        dur = int((timezone.now() - call.connected_at).total_seconds())
    elif call.duration_seconds:
        dur = call.duration_seconds

    return JsonResponse({
        'status': call.status,
        'offer_sdp': call.offer_sdp,
        'answer_sdp': call.answer_sdp,
        'candidates': remote_candidates,
        'duration_seconds': dur,
    })


@login_required
@require_POST
def api_call_ice(request, booking_id, call_id):
    """Exchanges ICE candidates between WebRTC peers."""
    call = get_object_or_404(CallSession, id=call_id, booking_id=booking_id)

    if request.user not in {call.caller, call.receiver}:
        return JsonResponse({'error': 'Unauthorized.'}, status=403)

    try:
        data = json.loads(request.body.decode('utf-8'))
        cand = data.get('candidate')
    except Exception:
        cand = None

    if cand:
        try:
            if request.user == call.caller:
                cands = json.loads(call.caller_candidates_json or '[]')
                cands.append(cand)
                call.caller_candidates_json = json.dumps(cands)
                call.save(update_fields=['caller_candidates_json'])
            else:
                cands = json.loads(call.receiver_candidates_json or '[]')
                cands.append(cand)
                call.receiver_candidates_json = json.dumps(cands)
                call.save(update_fields=['receiver_candidates_json'])
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)

    return JsonResponse({'success': True})


@login_required
@require_POST
def api_call_end(request, booking_id, call_id):
    """Terminates or declines an in-app audio call session and logs duration."""
    call = get_object_or_404(CallSession, id=call_id, booking_id=booking_id)

    if request.user not in {call.caller, call.receiver}:
        return JsonResponse({'error': 'Unauthorized.'}, status=403)

    if call.status not in {'ENDED', 'MISSED', 'REJECTED'}:
        if call.status == 'CONNECTED' and call.connected_at:
            dur = int((timezone.now() - call.connected_at).total_seconds())
            call.duration_seconds = max(dur, 1)
            call.status = 'ENDED'
            mins, secs = divmod(call.duration_seconds, 60)
            dur_label = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"
            BookingMessage.objects.create(
                booking=call.booking,
                sender=request.user,
                message=f"📞 In-App Audio Call completed ({dur_label})."
            )
        else:
            call.status = 'REJECTED' if request.user == call.receiver else 'ENDED'
            BookingMessage.objects.create(
                booking=call.booking,
                sender=request.user,
                message="📞 In-App Audio Call declined/cancelled."
            )

        call.ended_at = timezone.now()
        call.save(update_fields=['status', 'duration_seconds', 'ended_at'])

    return JsonResponse({'success': True, 'status': call.status, 'duration_seconds': call.duration_seconds})


@login_required
def api_call_check_incoming(request):
    """Lightweight polling endpoint to detect active incoming calls for the logged-in user."""
    cutoff = timezone.now() - timedelta(seconds=45)
    incoming = (
        CallSession.objects
        .filter(receiver=request.user, status='RINGING', started_at__gte=cutoff)
        .select_related('caller', 'booking__service')
        .order_by('-started_at')
        .first()
    )

    if incoming:
        is_worker = (getattr(incoming.caller, 'role', None) == 'WORKER')
        return JsonResponse({
            'has_incoming': True,
            'call_id': incoming.id,
            'booking_id': incoming.booking_id,
            'caller_name': incoming.caller.get_full_name() or incoming.caller.username,
            'caller_role': 'Service Professional' if is_worker else 'Customer',
            'service_name': incoming.booking.service.name,
            'offer_sdp': incoming.offer_sdp,
        })

    return JsonResponse({'has_incoming': False})


def _commission_defaults():
    """Platform-wide (percent, minimum) commission from settings, as Decimals."""
    try:
        pct = Decimal(str(getattr(settings, 'PLATFORM_COMMISSION_PERCENT', '10')))
    except Exception:
        pct = Decimal('10')
    try:
        minimum = Decimal(str(getattr(settings, 'PLATFORM_COMMISSION_MIN', '15')))
    except Exception:
        minimum = Decimal('15')
    return pct, minimum


def calculate_commission_for_amount(service, labor_amount, worker=None):
    """Commission for a labour amount under the service's commission rule.

    - 0 during the worker's launch period.
    - DEFAULT: settings percent of labour, minimum settings minimum (10%, ₹15).
    - PERCENT: service.commission_value percent of labour, same minimum.
    - FIXED:   service.commission_value rupees per job.

    The commission can never exceed the eligible labour amount (a Rs 10 job
    never pays more than Rs 10), and a zero/negative/invalid amount pays nothing.
    """
    if worker is not None and getattr(worker, 'is_in_launch', False):
        return Decimal('0.00')

    default_pct, minimum = _commission_defaults()
    try:
        labor_amount = Decimal(str(labor_amount or '0'))
    except Exception:
        return Decimal('0.00')
    if not labor_amount.is_finite() or labor_amount <= Decimal('0.00'):
        return Decimal('0.00')
    labor_amount = labor_amount.quantize(Decimal('0.01'))
    c_type = getattr(service, 'commission_type', 'DEFAULT') or 'DEFAULT'
    c_value = getattr(service, 'commission_value', None)

    if c_type == 'FIXED' and c_value is not None:
        fixed_fee = max(Decimal('0.00'), Decimal(str(c_value))).quantize(Decimal('0.01'))
        return min(fixed_fee, labor_amount)

    pct = Decimal(str(c_value)) if (c_type == 'PERCENT' and c_value is not None) else default_pct
    fee = (labor_amount * pct / Decimal('100')).quantize(Decimal('0.01'))
    return min(max(fee, minimum), labor_amount)


def calculate_booking_platform_fee(booking, worker=None):
    """
    Platform commission for a booking (charged at acceptance).
    Labour = job amount - material cost; see calculate_commission_for_amount.
    """
    target_worker = worker or booking.worker
    cost = booking.final_amount or booking.estimated_cost or (booking.service.price if booking.service else Decimal('0.00'))
    material_cost = getattr(booking, 'material_amount', Decimal('0.00')) or Decimal('0.00')
    labor_amount = max(Decimal('0.00'), Decimal(str(cost or '0')) - Decimal(str(material_cost)))
    return calculate_commission_for_amount(booking.service, labor_amount, worker=target_worker)


def estimate_worker_acceptance_fee(worker):
    """Commission this worker would pay to accept a standard job for their service."""
    service = getattr(worker, 'service', None)
    price = getattr(service, 'price', None) or Decimal('0.00')
    return calculate_commission_for_amount(service, price, worker=worker)


def worker_can_afford_acceptance(worker):
    """True when the worker's prepaid wallet covers the acceptance commission."""
    fee = estimate_worker_acceptance_fee(worker)
    if fee <= Decimal('0.00'):
        return True
    wallet = getattr(worker, 'wallet', None) if hasattr(worker, 'wallet') else None
    try:
        balance = Decimal(str(wallet.balance)) if wallet is not None and wallet.balance is not None else Decimal('0.00')
    except Exception:
        balance = Decimal('0.00')
    return balance >= fee


# ------------------------------------------------------------------
# Commission ledger helpers
# ------------------------------------------------------------------
# The ledger (WalletTransaction) is the single source of truth for what a
# worker actually paid for a booking: FEE_DEDUCTION rows are negative,
# REFUND rows positive. Refunds are computed from the ledger, never from
# booking.platform_fee, so money that was never deducted can never be
# "refunded", and a second refund of the same booking finds nothing left.

def _commission_outstanding(booking, wallet):
    """Commission charged to ``wallet`` for ``booking`` and not yet refunded (>= 0)."""
    total = WalletTransaction.objects.filter(
        wallet=wallet,
        booking=booking,
        transaction_type__in=('FEE_DEDUCTION', 'REFUND'),
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    return max(Decimal('0.00'), -Decimal(str(total)))


def _charge_acceptance_commission(booking, wallet, fee, note):
    """Deduct ``fee`` for ``booking`` from a wallet that the caller has locked.

    Idempotent per (booking, wallet): if this wallet already holds an
    unrefunded charge for the booking nothing is deducted again. The caller
    must have verified ``wallet.balance >= fee``. Sets platform_fee and
    fee_settled on the booking object (caller saves the booking).
    Returns the amount actually deducted.
    """
    fee = Decimal(str(fee or '0.00'))
    if _commission_outstanding(booking, wallet) > Decimal('0.00'):
        booking.fee_settled = True
        return Decimal('0.00')

    current_balance = Decimal(str(wallet.balance if wallet.balance is not None else '0.00'))
    if fee > Decimal('0.00'):
        if current_balance < fee:
            # Defensive: never mark an unpaid commission as settled.
            raise ValueError('Insufficient wallet balance for commission.')
        wallet.balance = current_balance - fee
        wallet.save(update_fields=['balance', 'updated_at'])
        WalletTransaction.objects.create(
            wallet=wallet,
            booking=booking,
            transaction_type='FEE_DEDUCTION',
            amount=-fee,
            balance_after=wallet.balance,
            notes=note,
        )
    else:
        WalletTransaction.objects.create(
            wallet=wallet,
            booking=booking,
            transaction_type='FEE_DEDUCTION',
            amount=Decimal('0.00'),
            balance_after=wallet.balance,
            notes=f"Launch Offer Benefit: 0% Platform Fee for Booking #{booking.id}",
        )
    booking.platform_fee = fee
    booking.fee_settled = True
    return fee


def _refund_booking_commission(booking, reason):
    """Return the commission the *current* worker actually paid for ``booking``.

    Locks the wallet row, so concurrent or repeated calls cannot refund twice.
    Returns the refunded amount (0 when nothing was outstanding).
    """
    if not booking.worker_id:
        return Decimal('0.00')
    with transaction.atomic():
        wallet = WorkerWallet.objects.select_for_update().filter(worker_id=booking.worker_id).first()
        if wallet is None:
            return Decimal('0.00')
        outstanding = _commission_outstanding(booking, wallet)
        if outstanding <= Decimal('0.00'):
            return Decimal('0.00')
        wallet.balance = Decimal(str(wallet.balance if wallet.balance is not None else '0.00')) + outstanding
        wallet.save(update_fields=['balance', 'updated_at'])
        WalletTransaction.objects.create(
            wallet=wallet,
            booking=booking,
            transaction_type='REFUND',
            amount=outstanding,
            balance_after=wallet.balance,
            notes=f"Commission refunded for Booking #{booking.id}: {reason}"[:500],
        )
        worker = booking.worker
        if worker.is_paused_for_dues and not wallet.is_locked:
            worker.is_paused_for_dues = False
            worker.save(update_fields=['is_paused_for_dues'])
        Booking.objects.filter(pk=booking.pk).update(fee_settled=False)
        booking.fee_settled = False
    return outstanding


def _booking_scheduled_start(booking):
    """Aware datetime of the booking's scheduled start, or None."""
    if not booking.date:
        return None
    s_time = booking.start_time
    if not s_time and booking.time:
        s_time, _ = parse_time_range(booking.time)
    if not s_time:
        return None
    from datetime import datetime as dt_cls
    return timezone.make_aware(dt_cls.combine(booking.date, s_time), timezone.get_current_timezone())


def _worker_cancellation_refund_allowed(booking, now=None):
    """Worker cancelling an ACCEPTED job is refunded only when done early enough."""
    start = _booking_scheduled_start(booking)
    if start is None:
        return False
    now = now or timezone.now()
    min_hours = int(getattr(settings, 'WORKER_CANCEL_REFUND_MIN_HOURS', 12) or 12)
    return (start - now) >= timedelta(hours=min_hours)


@login_required
def worker_accept_booking(request, booking_id):
    is_ajax = (
        request.headers.get('x-requested-with') == 'XMLHttpRequest' or
        request.POST.get('ajax') == '1' or
        request.GET.get('ajax') == '1'
    )
    if request.method != 'POST':
        if is_ajax:
            return JsonResponse({'success': False, 'message': 'Method not allowed. POST required.'}, status=405)
        return HttpResponseNotAllowed(['POST'])

    if request.user.role != 'WORKER':
        if is_ajax:
            return JsonResponse({'success': False, 'message': 'You are not authorized.'}, status=403)
        messages.error(request, 'You are not authorized.')
        return redirect('dashboard')

    worker = get_object_or_404(WorkerProfile, user=request.user)

    # Check worker verification and suspension
    is_verified, error_msg = _check_worker_verification(worker)
    if not is_verified:
        if is_ajax:
            return JsonResponse({'success': False, 'message': error_msg}, status=400)
        messages.error(request, error_msg)
        return redirect('worker_dashboard')

    with transaction.atomic():
        locked_worker = WorkerProfile.objects.select_for_update().get(id=worker.id)
        try:
            booking = Booking.objects.select_for_update().select_related('customer', 'service').get(
                id=booking_id, worker=locked_worker
            )
        except Booking.DoesNotExist:
            if is_ajax:
                return JsonResponse({'success': False, 'message': 'Booking not found or not assigned to you.'}, status=404)
            messages.error(request, 'Booking not found or not assigned to you.')
            return redirect('worker_dashboard')

        if booking.status != 'PENDING':
            messages.warning(request, 'This booking has already been processed.')
            return redirect('worker_dashboard')

        if not booking.customer.is_active:
            messages.error(request, 'Cannot accept booking: The customer account is no longer active.')
            return redirect('worker_dashboard')

        if booking.series and not booking.series.is_active:
            messages.error(request, 'Cannot accept booking: The customer has paused this recurring series.')
            return redirect('worker_dashboard')

        # Prepaid Wallet check (Finding C1 & C2)
        wallet, _ = WorkerWallet.objects.select_for_update().get_or_create(worker=locked_worker)
        current_balance = Decimal(str(wallet.balance if wallet.balance is not None else '0.00'))
        required_fee = calculate_booking_platform_fee(booking, locked_worker)

        # A recurring request is accepted as a whole series, so the wallet
        # must cover the commission for every pending visit up front.
        # Visits that cannot be paid for are never accepted or marked settled.
        series_visits = []
        total_required = required_fee
        if booking.series:
            series_visits = list(
                Booking.objects.select_for_update().select_related('service').filter(
                    series=booking.series,
                    worker=locked_worker,
                    status='PENDING'
                ).exclude(id=booking.id)
            )
            for sb in series_visits:
                total_required += calculate_booking_platform_fee(sb, locked_worker)

        if current_balance < total_required or (wallet.is_locked and total_required > Decimal('0.00')):
            if series_visits:
                msg = (
                    f'Cannot accept this recurring series: it has {len(series_visits) + 1} visits and needs '
                    f'₹{total_required:.2f} in platform fees, but your prepaid wallet balance is ₹{current_balance:.2f}. '
                    f'Please recharge your wallet to accept the series.'
                )
            else:
                msg = (
                    f'Cannot accept booking: Insufficient prepaid wallet balance (₹{current_balance:.2f}). '
                    f'Platform fee of ₹{required_fee:.2f} is required. Please recharge your wallet to accept bookings.'
                )
            if is_ajax:
                return JsonResponse({'success': False, 'message': msg}, status=400)
            messages.error(request, msg)
            return redirect('worker_dashboard')

        # Check response timer expiry - same windows as the expiry sweep.
        has_expired, reason_msg = pending_request_has_expired(booking)
        reason_msg = reason_msg or 'Worker did not respond within the response window.'

        if has_expired:
            booking.status = 'EXPIRED'
            booking.cancellation_reason = reason_msg
            booking.cancelled_by = 'SYSTEM'
            booking.save(update_fields=['status', 'cancellation_reason', 'cancelled_by', 'updated_at'])
            Notification.objects.create(
                recipient=booking.customer, booking=booking, notification_type='BOOKING_UPDATE',
                message=f'Your {booking.service.name} booking request #{booking.id} has expired.'
            )
            messages.error(request, 'This booking request has expired (response timer elapsed).')
            return redirect('worker_dashboard')

        # Re-check worker leave status at acceptance time
        try:
            worker_on_leave = WorkerLeave.objects.filter(
                worker=locked_worker, leave_date=booking.date
            ).exists()
            if worker_on_leave:
                messages.error(request, 'Cannot accept booking - you are marked as on leave for this date.')
                return redirect('worker_dashboard')
        except Exception:
            # WorkerLeave table may not exist yet (migration not applied)
            pass

        if booking.start_time and booking.end_time:
            has_conflict = has_worker_schedule_conflict(
                locked_worker,
                booking_date=booking.date,
                start_time=booking.start_time,
                end_time=booking.end_time,
                exclude_booking_id=booking.id,
            )
        else:
            has_conflict = has_worker_schedule_conflict(
                locked_worker,
                booking_date=booking.date,
                time_value=booking.time,
                exclude_booking_id=booking.id,
            )

        if has_conflict:
            if is_ajax:
                return JsonResponse({'success': False, 'message': 'Cannot accept - overlaps with another booking.'}, status=400)
            messages.error(request, 'Cannot accept - overlaps with another booking.')
            return redirect('worker_dashboard')

        booking.status = 'ACCEPTED'
        booking.generate_arrival_otp()

        # Deduct platform commission exactly once upon acceptance (Finding C1 & C2).
        # The ledger-based helper is idempotent per booking and wallet, so a
        # reassigned booking is charged to its new worker even if an earlier
        # worker's charge exists, and a double-submit never charges twice.
        _charge_acceptance_commission(
            booking, wallet, required_fee,
            note=f"Platform fee deducted upon booking acceptance for Booking #{booking.id}",
        )

        booking.save(update_fields=['status', 'arrival_otp', 'worker_badge_code', 'platform_fee', 'fee_settled', 'updated_at'])

        # If this booking belongs to a recurring series, accept the entire series schedule (Finding B5).
        # The wallet was verified above to cover every visit, so each visit is charged in full.
        for sb in series_visits:
            sb_fee = calculate_booking_platform_fee(sb, locked_worker)
            sb.status = 'ACCEPTED'
            sb.generate_arrival_otp()
            _charge_acceptance_commission(
                sb, wallet, sb_fee,
                note=f"Platform fee deducted upon recurring booking acceptance for Booking #{sb.id}",
            )
            sb.save(update_fields=['status', 'arrival_otp', 'worker_badge_code', 'platform_fee', 'fee_settled', 'updated_at'])

        # Create notifications and register on_commit external dispatch
        _create_idempotent_notification(
            recipient=booking.customer, booking=booking, notification_type='BOOKING_UPDATE',
            message=f'Good news! Your {booking.service.name} booking has been accepted by {request.user.get_full_name() or request.user.username}.'
        )

        customer_email = booking.customer.email
        customer_username = booking.customer.username
        service_name = booking.service.name
        worker_display_name = request.user.get_full_name() or request.user.username
        b_date = booking.date
        b_time = booking.time
        b_id = booking.id

        if customer_email:
            transaction.on_commit(lambda: send_email_safely(
                subject=f'Good News! Your Booking #{b_id} is Confirmed',
                message=(
                    f'Hi {customer_username},\n\n'
                    f'Great news! Your {service_name} booking has been accepted by {worker_display_name}.\n\n'
                    f'Booking Details:\n'
                    f'  Date: {b_date}\n'
                    f'  Time: {b_time}\n\n'
                    f'The worker will reach out to you shortly. If you have any questions, feel free to reach out to us.\n\n'
                    f'Best regards,\nTeam LAMO'
                ),
                recipient_list=[customer_email],
                purpose='booking_accepted',
                async_send=True,
            ))

        admins = CustomUser.objects.filter(role='ADMIN', is_active=True)
        for admin in admins:
            _create_idempotent_notification(
                recipient=admin, booking=booking, notification_type='BOOKING_UPDATE',
                message=f'Booking #{b_id} has been accepted by {worker_display_name}.'
            )

    if is_ajax:
        return JsonResponse({
            'success': True,
            'message': 'Booking accepted successfully.',
            'booking_id': booking.id,
            'status': 'ACCEPTED'
        })

    messages.success(request, 'Booking accepted successfully.')
    return redirect('worker_dashboard')


@login_required
def worker_reject_booking(request, booking_id):
    is_ajax = (
        request.headers.get('x-requested-with') == 'XMLHttpRequest' or
        request.POST.get('ajax') == '1' or
        request.GET.get('ajax') == '1'
    )
    if request.method != 'POST':
        if is_ajax:
            return JsonResponse({'success': False, 'message': 'Method not allowed. POST required.'}, status=405)
        return HttpResponseNotAllowed(['POST'])

    if request.user.role != 'WORKER':
        if is_ajax:
            return JsonResponse({'success': False, 'message': 'You are not authorized.'}, status=403)
        messages.error(request, 'You are not authorized.')
        return redirect('dashboard')

    worker = get_object_or_404(WorkerProfile, user=request.user)

    # Check worker verification
    is_verified, error_msg = _check_worker_verification(worker)
    if not is_verified:
        if is_ajax:
            return JsonResponse({'success': False, 'message': error_msg}, status=400)
        messages.error(request, error_msg)
        return redirect('worker_dashboard')

    # Handle Emergency pending request rejection / dismissal
    emergency_booking = Booking.objects.filter(
        id=booking_id,
        status='EMERGENCY_PENDING',
        worker__isnull=True
    ).first()

    if emergency_booking:
        Notification.objects.filter(
            recipient=request.user,
            booking=emergency_booking,
            notification_type='EMERGENCY'
        ).update(is_read=True)
        if is_ajax:
            return JsonResponse({'success': True, 'message': 'Emergency request dismissed.'})
        messages.info(request, 'Emergency request dismissed.')
        return redirect('worker_dashboard')

    with transaction.atomic():
        try:
            locked_booking = Booking.objects.select_for_update().select_related('customer', 'service').get(id=booking_id, worker=worker)
        except Booking.DoesNotExist:
            if is_ajax:
                return JsonResponse({'success': False, 'message': 'Booking not found or not assigned to you.'}, status=404)
            messages.error(request, 'Booking not found or not assigned to you.')
            return redirect('worker_dashboard')
        if locked_booking.status != 'PENDING':
            messages.warning(request, 'This booking has already been processed.')
            return redirect('worker_dashboard')

        rejection_reason = request.POST.get('rejection_reason', '').strip() or request.GET.get('reason', '').strip() or 'Worker declined the booking request.'
        locked_booking.status = 'REJECTED'
        locked_booking.cancellation_reason = rejection_reason
        locked_booking.cancelled_by = 'WORKER'
        locked_booking.save(update_fields=['status', 'cancellation_reason', 'cancelled_by', 'updated_at'])

        # If this booking belongs to a recurring series, decline the entire series (Finding B5)
        if locked_booking.series:
            Booking.objects.filter(
                series=locked_booking.series,
                worker=worker,
                status='PENDING'
            ).exclude(id=locked_booking.id).update(
                status='REJECTED',
                cancellation_reason=rejection_reason,
                cancelled_by='WORKER'
            )
            locked_booking.series.is_active = False
            locked_booking.series.save(update_fields=['is_active'])

        _create_idempotent_notification(
            recipient=locked_booking.customer, booking=locked_booking, notification_type='BOOKING_UPDATE',
            message=f'Your {locked_booking.service.name} booking has been declined by the worker.'
        )

        customer_email = locked_booking.customer.email
        customer_username = locked_booking.customer.username
        service_name = locked_booking.service.name
        b_date = locked_booking.date
        b_id = locked_booking.id

        if customer_email:
            transaction.on_commit(lambda: send_email_safely(
                subject=f'Update on Booking #{b_id}',
                message=(
                    f'Hi {customer_username},\n\n'
                    f'Unfortunately, your {service_name} booking on {b_date} has been declined by the worker.\n\n'
                    f'Don\'t worry! You can browse and book another available worker on our platform.\n\n'
                    f'If you need any assistance, we\'re here to help.\n\n'
                    f'Best regards,\nTeam LAMO'
                ),
                recipient_list=[customer_email],
                purpose='booking_rejected',
                async_send=True,
            ))

    if is_ajax:
        return JsonResponse({
            'success': True,
            'message': 'Booking declined.',
            'booking_id': booking_id,
            'status': 'REJECTED'
        })

    messages.success(request, 'Booking rejected.')
    return redirect('worker_dashboard')


@login_required
def worker_accept_emergency(request, booking_id):
    """Worker accepts an emergency request - first accept wins"""
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])

    if request.user.role != 'WORKER':
        messages.error(request, 'You are not authorized.')
        return redirect('dashboard')

    worker = get_object_or_404(WorkerProfile, user=request.user)

    # Check worker verification
    is_verified, error_msg = _check_worker_verification(worker)
    if not is_verified:
        messages.error(request, error_msg)
        return redirect('worker_dashboard')

    try:
        with transaction.atomic():
            # Acquire exclusive lock on WorkerProfile first to serialize any concurrent booking creation/acceptance
            locked_worker = WorkerProfile.objects.select_for_update().get(id=worker.id)

            # Lock the booking row to prevent race conditions
            try:
                booking = Booking.objects.select_for_update().select_related(
                    'customer', 'service'
                ).get(
                    id=booking_id,
                    status='EMERGENCY_PENDING',
                    worker__isnull=True
                )
            except Booking.DoesNotExist:
                messages.warning(request, 'This emergency request has already been accepted or is no longer available.')
                return redirect('worker_dashboard')

            # Double-check status after locking (prevent race condition)
            if booking.status != 'EMERGENCY_PENDING' or booking.worker is not None:
                messages.warning(request, 'This emergency request has already been accepted by another worker.')
                return redirect('worker_dashboard')

            # Only workers offering this trade may take the job.
            if locked_worker.service_id is None or locked_worker.service_id != booking.service_id:
                messages.error(request, 'This emergency request is for a different service than yours.')
                return redirect('worker_dashboard')

            # Finding C4: District check - emergency workers may only accept jobs in their district
            if booking.district and locked_worker.district and booking.district.strip().lower() != locked_worker.district.strip().lower():
                messages.error(request, f'This emergency booking is in {booking.district}, which is outside your service district ({locked_worker.district}).')
                return redirect('worker_dashboard')

            # Finding C1, C2 & C4: Prepaid Wallet & Commission Check
            required_fee = calculate_booking_platform_fee(booking, locked_worker)
            wallet, _ = WorkerWallet.objects.select_for_update().get_or_create(worker=locked_worker)
            current_balance = Decimal(str(wallet.balance if wallet.balance is not None else '0.00'))

            if current_balance < required_fee or (wallet.is_locked and required_fee > Decimal('0.00')):
                messages.error(
                    request,
                    f'Cannot accept emergency booking: Insufficient prepaid wallet balance (₹{current_balance:.2f}). '
                    f'Platform fee of ₹{required_fee:.2f} is required. Please recharge your wallet.'
                )
                return redirect('worker_dashboard')

            # The emergency broadcast is dateless, so check leave/conflicts
            # against the acceptance day and then stamp it onto the booking.
            today = timezone.localdate()

            # Re-check worker leave status at acceptance time
            try:
                worker_on_leave = WorkerLeave.objects.filter(
                    worker=locked_worker, leave_date=today
                ).exists()
                if worker_on_leave:
                    messages.error(request, 'Cannot accept emergency booking - you are marked as on leave for this date.')
                    return redirect('worker_dashboard')
            except Exception:
                # WorkerLeave table may not exist yet (migration not applied)
                pass

            # Check for time conflicts with existing active bookings
            start_t, end_t = parse_time_range(booking.time)
            now_dt = timezone.localtime()
            today = now_dt.date()

            # If emergency booking has no explicit clock range (e.g. 'ASAP'), allocate immediate 60-min window
            if not start_t or not end_t:
                start_t = now_dt.time()
                from datetime import time as tm_cls
                end_dt = now_dt + timedelta(minutes=60)
                end_t = tm_cls(23, 59) if end_dt.date() > today else end_dt.time()

            existing_booking = has_worker_schedule_conflict(
                locked_worker,
                booking_date=today,
                start_time=start_t,
                end_time=end_t,
                exclude_booking_id=booking.id,
            )

            if existing_booking:
                messages.error(request, 'Cannot accept emergency booking - you already have an active booking during this immediate hour.')
                return redirect('worker_dashboard')

            # Assign this worker to the booking. Stamping date=today and start/end time
            # makes this job block the worker's calendar for later conflict checks.
            booking.worker = locked_worker
            booking.date = today
            booking.start_time = start_t
            booking.end_time = end_t
            booking.duration_min = 45
            booking.buffer_min = 15
            booking.status = 'ACCEPTED'
            booking.generate_arrival_otp()

            # Deduct platform commission on acceptance (Finding C1 & C2) via the
            # idempotent ledger helper (never charges the same wallet twice).
            _charge_acceptance_commission(
                booking, wallet, required_fee,
                note=f"Platform fee deducted on emergency booking acceptance for Booking #{booking.id}",
            )

            booking.save(update_fields=['worker', 'date', 'start_time', 'end_time', 'duration_min', 'buffer_min', 'status', 'arrival_otp', 'worker_badge_code', 'platform_fee', 'fee_settled', 'updated_at'])

            # Notify the customer
            _create_idempotent_notification(
                recipient=booking.customer,
                booking=booking,
                notification_type='BOOKING_UPDATE',
                message=f'Emergency request accepted! {request.user.get_full_name() or request.user.username} will assist you for {booking.service.name}.'
            )

            # Notify other eligible workers that request is taken
            other_workers = WorkerProfile.objects.filter(
                service=booking.service,
                is_verified=True,
                work_status='AVAILABLE',
                user__is_active=True
            ).exclude(id=worker.id)

            for other_worker in other_workers:
                try:
                    _create_idempotent_notification(
                        recipient=other_worker.user,
                        booking=booking,
                        notification_type='SYSTEM',
                        message=f'Emergency request #{booking.id} for {booking.service.name} has been accepted by another worker.'
                    )
                except Exception:
                    continue

            # Notify admins
            admins = CustomUser.objects.filter(role='ADMIN', is_active=True)
            for admin in admins:
                try:
                    _create_idempotent_notification(
                        recipient=admin,
                        booking=booking,
                        notification_type='BOOKING_UPDATE',
                        message=f'Emergency booking #{booking.id} accepted by {request.user.username}.'
                    )
                except Exception:
                    continue

            customer_email = booking.customer.email
            customer_username = booking.customer.username
            service_name = booking.service.name
            worker_display_name = request.user.get_full_name() or request.user.username
            b_loc = booking.location
            b_id = booking.id

            if customer_email:
                transaction.on_commit(lambda: send_email_safely(
                    subject=f'Worker Assigned - Emergency Request #{b_id}',
                    message=(
                        f'Hi {customer_username},\n\n'
                        f'Great news! We\'ve found a worker for your emergency {service_name} request.\n\n'
                        f'Worker Details:\n'
                        f'  Name: {worker_display_name}\n'
                        f'  Location: {b_loc}\n\n'
                        f'The worker will contact you shortly. Please keep your phone reachable.\n\n'
                        f'Thanks for trusting LAMO in your time of need!\n\n'
                        f'Best regards,\nTeam LAMO'
                    ),
                    recipient_list=[customer_email],
                    purpose='emergency_worker_assigned',
                    async_send=True,
                ))

    except Booking.DoesNotExist:
        messages.warning(request, 'This emergency request has already been accepted by another worker.')
        return redirect('worker_dashboard')

    messages.success(request, f'Emergency request accepted! You have been assigned to this booking.')
    return redirect('worker_dashboard')


@login_required
def service_create(request):

    # Plain Django staff accounts are not LAMO admins (same rule as login):
    # only the ADMIN role or a superuser may manage services.
    is_admin = (
        request.user.role == 'ADMIN'
        or getattr(request.user, 'is_superuser', False)
    )
    if not is_admin:
        messages.error(request, 'You are not authorized to access this page.')
        return redirect('dashboard')
    if request.user.role != 'ADMIN' and request.user.is_superuser:
        request.user.role = 'ADMIN'
        request.user.save(update_fields=['role'])
    if request.method == 'POST':
        form = ServiceForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                # Storage (Cloudinary) or DB failures must surface as a
                # friendly re-render, never an unhandled HTTP 500 from the
                # admin Add Service flow. The storage layer logs the scrubbed
                # detail; only the exception type is recorded here so a
                # credential-bearing SDK message can never reach this log.
                with transaction.atomic():
                    form.save()
            except Exception as exc:
                logger.error(
                    'service_create_save_failed user=%s exc_type=%s',
                    getattr(request.user, 'pk', None), type(exc).__name__,
                )
                messages.error(
                    request,
                    'Could not save the service. If you attached an image, '
                    'it may have failed to upload - please try again.',
                )
                return render(request, 'lamo_service/service_form.html', {
                    'form': form, 'title': 'Add Service',
                })
            messages.success(request, 'Service created successfully.')
            return redirect('service_list')
    else:
        form = ServiceForm()

    return render(request, 'lamo_service/service_form.html', {
        'form': form, 'title': 'Add Service'
    })


def register_view(request):

    if request.user.is_authenticated:
        return redirect('dashboard')

    services = Service.objects.filter(
        is_active=True
    ).order_by('-created_at')

    # Where the guest was heading before they were asked to sign up. The public
    # templates link here as {% url 'register' %}?next={% url 'service_detail' ... %}
    # so the intent survives registration. It is validated immediately and
    # stored server-side in the session; login_view consumes it once. Anything
    # unsafe is dropped here and the normal role-based destination is used.
    requested_next = request.POST.get('next') or request.GET.get('next')
    if requested_next:
        safe_next = _safe_internal_path(request, requested_next, '')
        if safe_next:
            request.session['registration_next'] = safe_next
        else:
            # An unsafe value is discarded outright, not downgraded to a
            # default here - the role-based destination is applied at login.
            request.session.pop('registration_next', None)
    # A POST that simply does not repeat the parameter keeps whatever the GET
    # already established; clearing it here would break the normal
    # register-from-a-link flow.

    if request.method == 'POST':

        action = request.POST.get('action', '')
        if action == 'send_customer_reg_otp':
            name = request.POST.get('name', '').strip()
            identifier = request.POST.get('identifier', '').strip()
            if not name or not identifier:
                return JsonResponse({'success': False, 'error': 'Please enter both your name and email or mobile number.'})

            # Any match (including an ambiguous one) means "already registered".
            if _users_matching_identifier(identifier):
                return JsonResponse({'success': False, 'error': 'An account with this email or mobile number already exists. Please sign in.'})

            # Cooldown check: 60 seconds between resend requests (Finding A2)
            last_sent_str = request.session.get('customer_reg_last_sent')
            if last_sent_str:
                from datetime import datetime as dt_cls
                try:
                    last_sent_dt = dt_cls.fromisoformat(last_sent_str)
                    elapsed = (timezone.now() - last_sent_dt).total_seconds()
                    if elapsed < 60:
                        wait_sec = max(1, int(60 - elapsed))
                        return JsonResponse({
                            'success': False,
                            'error': f'Please wait {wait_sec} second(s) before requesting a new code.',
                            'cooldown_remaining': wait_sec
                        })
                except Exception:
                    pass

            is_email_identifier = '@' in identifier
            phone_digits = '' if is_email_identifier else re.sub(r'\D', '', identifier)[-10:]
            if not is_email_identifier and not re.fullmatch(r'[6-9]\d{9}', phone_digits or ''):
                return JsonResponse({'success': False, 'error': 'Please enter a valid email address or 10-digit mobile number.'})
            if not is_email_identifier and not sms_delivery_available():
                return JsonResponse({
                    'success': False,
                    'error': 'Mobile verification is temporarily unavailable. Please register with your email address instead.'
                })

            # Shared-cache limits (per IP and per destination) - unlike the
            # session cooldown above, these cannot be reset by dropping cookies.
            send_ok, send_error = _otp_send_allowed(request, identifier)
            if not send_ok:
                return JsonResponse({'success': False, 'error': send_error})

            reg_otp = ''.join(secrets.choice(string.digits) for _ in range(6))

            if is_email_identifier:
                try:
                    delivered = send_email_safely(
                        subject='LAMO Account Verification Code',
                        message=f'Hello {name},\n\nYour 6-digit verification code for LAMO is: {reg_otp}\nValid for 10 minutes.\n\nThank you,\nTeam LAMO',
                        recipient_list=[identifier],
                        purpose='customer_registration_otp',
                    )
                except Exception:
                    delivered = False
            else:
                delivered = send_otp_sms(phone_digits, reg_otp)

            if not delivered:
                request.session.pop('customer_reg_data', None)
                target_word = 'email address' if is_email_identifier else 'mobile number'
                return JsonResponse({
                    'success': False,
                    'error': f'We could not send a verification code to your {target_word}. Please check it and try again.'
                })

            request.session['customer_reg_last_sent'] = timezone.now().isoformat()
            request.session['customer_reg_data'] = {
                'name': name,
                'identifier': identifier,
                'otp': reg_otp,
                'created_at': timezone.now().isoformat(),
                'attempts': 0
            }

            target_str = identifier
            if '@' in target_str:
                p = target_str.split('@')
                masked = (p[0][:2] + '***@' + p[1]) if len(p[0]) > 2 else ('*@' + p[1])
            else:
                digits = re.sub(r'\D', '', target_str)
                masked = digits[:2] + '******' + digits[-2:] if len(digits) >= 4 else digits

            return JsonResponse({'success': True, 'masked_target': masked, 'message': f'Verification code sent to {masked}'})

        if action == 'verify_customer_reg_otp':
            otp = request.POST.get('otp', '').strip()
            reg_data = request.session.get('customer_reg_data')
            if not reg_data:
                return JsonResponse({'success': False, 'error': 'Registration session expired. Please start again.'})

            # Attempt tracking & lockout guard (Finding A2). The counter is kept
            # server-side per destination so a new session cannot reset it.
            reg_attempt_key = otp_verify_target_key('customer-reg', reg_data.get('identifier'))
            if is_rate_limited(reg_attempt_key, OTP_VERIFY_MAX_ATTEMPTS):
                request.session.pop('customer_reg_data', None)
                return JsonResponse({
                    'success': False,
                    'error': f'Too many incorrect attempts. Please try again in {max(1, minutes_remaining(reg_attempt_key))} minute(s).'
                })
            attempts = reg_data.get('attempts', 0) + 1
            reg_data['attempts'] = attempts
            request.session['customer_reg_data'] = reg_data
            request.session.modified = True

            if not secrets.compare_digest(str(reg_data.get('otp') or ''), otp):
                register_failure(reg_attempt_key, OTP_VERIFY_MAX_ATTEMPTS, OTP_VERIFY_LOCKOUT_SECONDS)
                if attempts >= 5:
                    request.session.pop('customer_reg_data', None)
                    return JsonResponse({
                        'success': False,
                        'error': 'Too many incorrect attempts. For your security, this registration code has been invalidated. Please request a new code.'
                    })
                remaining = 5 - attempts
                return JsonResponse({
                    'success': False,
                    'error': f'Invalid 6-digit verification code. {remaining} attempt(s) remaining.'
                })

            clear(reg_attempt_key)
            from datetime import datetime as dt_cls
            created_at_dt = dt_cls.fromisoformat(reg_data.get('created_at'))
            if timezone.now() > created_at_dt + timedelta(minutes=10):
                return JsonResponse({'success': False, 'error': 'Verification code has expired. Please request a new one.'})

            name = reg_data.get('name')
            identifier = reg_data.get('identifier')
            # Re-check at creation time: the contact may have been registered
            # by someone else after the code was sent (no duplicate accounts).
            if _users_matching_identifier(identifier):
                request.session.pop('customer_reg_data', None)
                return JsonResponse({'success': False, 'error': 'An account with this email or mobile number already exists. Please sign in.'})
            base_username = re.sub(r'[^a-zA-Z0-9_]', '', name.lower().replace(' ', '_')) or 'user'
            username = base_username
            counter = 1
            while CustomUser.objects.filter(username=username).exists():
                username = f"{base_username}_{counter}"
                counter += 1

            email = identifier if '@' in identifier else ''
            user = CustomUser.objects.create_user(
                username=username,
                email=email,
                role='USER',
                is_email_verified=True
            )
            user.set_unusable_password()
            user.save()

            phone = '' if '@' in identifier else re.sub(r'\D', '', identifier)[-10:]
            UserProfile.objects.create(user=user, phone=phone)

            login(request, user)
            request.session.cycle_key()
            request.session.pop('customer_reg_data', None)

            requested_next = request.POST.get('next') or request.session.pop('registration_next', None)
            safe_next = _safe_internal_path(request, requested_next, '')
            dest = safe_next if safe_next else reverse('home')
            return JsonResponse({'success': True, 'redirect_url': dest})

        username = request.POST.get(
            'username',
            ''
        ).strip()

        email = request.POST.get(
            'email',
            ''
        ).strip()

        password = request.POST.get(
            'password',
            ''
        )

        confirm_password = request.POST.get(
            'confirm_password',
            ''
        )

        role = request.POST.get(
            'role',
            ''
        ).strip()

        # ==================================================
        # BASIC VALIDATION
        # ==================================================

        if not username:

            messages.error(
                request,
                'Username is required.'
            )

            return render(
                request,
                'accounts/register.html',
                {'services': services}
            )

        if not email:

            messages.error(
                request,
                'Email is required.'
            )

            return render(
                request,
                'accounts/register.html',
                {'services': services}
            )

        # EMAIL FORMAT

        try:

            validate_email(email)

        except ValidationError:

            messages.error(
                request,
                'Please enter a valid email address.'
            )

            return render(
                request,
                'accounts/register.html',
                {'services': services}
            )

        # PASSWORD (Policy B3: Worker registration is passwordless; workers log in via OTP)
        if role == 'WORKER':
            password = None
        else:
            if password != confirm_password:
                messages.error(
                    request,
                    'Passwords do not match.'
                )
                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            if len(password) < 8:
                messages.error(
                    request,
                    'Password must contain at least 8 characters.'
                )
                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            try:
                validate_password(password)
            except ValidationError as exc:
                for message in exc.messages:
                    messages.error(request, message)

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

        # ROLE

        if role not in [
            'USER',
            'WORKER'
        ]:

            messages.error(
                request,
                'Please select User or Worker.'
            )

            return render(
                request,
                'accounts/register.html',
                {'services': services}
            )

        # ==================================================
        # USERNAME VALIDATION
        # ==================================================

        if CustomUser.objects.filter(
            username=username
        ).exists():

            messages.error(
                request,
                'Username already exists.'
            )

            return render(
                request,
                'accounts/register.html',
                {'services': services}
            )

        # ==================================================
        # EMAIL VALIDATION
        # ==================================================

        if CustomUser.objects.filter(
            email__iexact=email
        ).exists():

            messages.error(
                request,
                'Email already exists.'
            )

            return render(
                request,
                'accounts/register.html',
                {'services': services}
            )

        # ==================================================
        # WORKER VALIDATION
        # ==================================================

        worker_data = {}

        if role == 'WORKER':

            # ------------------------------------------------
            # GET WORKER DATA
            # ------------------------------------------------

            phone = request.POST.get(
                'phone',
                ''
            ).strip()

            service_id = request.POST.get(
                'service',
                ''
            ).strip()

            experience = request.POST.get(
                'experience_years',
                ''
            ).strip()

            district = request.POST.get(
                'district',
                ''
            ).strip()

            availability = request.POST.get(
                'availability',
                ''
            ).strip()

            pricing_type = request.POST.get(
                'pricing_type',
                'hour'
            ).strip()

            pricing_amount = request.POST.get(
                'pricing_amount',
                ''
            ).strip()

            address = request.POST.get(
                'address',
                ''
            ).strip()

            custom_service = request.POST.get(
                'custom_service',
                ''
            ).strip()

            proof_type = request.POST.get(
                'proof_type',
                ''
            ).strip()

            profile_image = request.FILES.get(
                'profile_image'
            )

            proof_document = request.FILES.get(
                'proof_document'
            )

            # ------------------------------------------------
            # PHONE VALIDATION
            # ------------------------------------------------

            if not re.fullmatch(
                r'[6-9]\d{9}',
                phone
            ):

                messages.error(
                    request,
                    'Please enter a valid 10-digit Indian mobile number.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            # ------------------------------------------------
            # DUPLICATE PHONE (Cross-role uniqueness)
            # ------------------------------------------------

            if _user_ids_for_phone(phone):

                messages.error(
                    request,
                    'This phone number is already registered with another account.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            # ------------------------------------------------
            # SERVICE (or "Other" with custom_service)
            # ------------------------------------------------

            service = None
            is_custom_service = False

            if not service_id:

                messages.error(
                    request,
                    'Please select a service.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            if service_id == 'other':

                if not custom_service or len(custom_service) < 2:

                    messages.error(
                        request,
                        'Please enter your custom service name.'
                    )

                    return render(
                        request,
                        'accounts/register.html',
                        {'services': services}
                    )

                is_custom_service = True
                service = None  # No Service FK for custom

            else:

                try:

                    service = Service.objects.get(
                        id=service_id,
                        is_active=True
                    )

                except (
                    Service.DoesNotExist,
                    ValueError
                ):

                    messages.error(
                        request,
                        'Please select a valid service.'
                    )

                    return render(
                        request,
                        'accounts/register.html',
                        {'services': services}
                    )

            # ------------------------------------------------
            # EXPERIENCE
            # ------------------------------------------------

            if experience == '':

                messages.error(
                    request,
                    'Please enter your experience.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            try:

                experience = int(experience)

            except (
                ValueError,
                TypeError
            ):

                messages.error(
                    request,
                    'Please enter a valid experience.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            if experience < 0:

                messages.error(
                    request,
                    'Experience cannot be negative.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            if experience > 60:

                messages.error(
                    request,
                    'Experience cannot exceed 60 years.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            # ------------------------------------------------
            # DISTRICT
            # ------------------------------------------------

            valid_districts = [
                'Thiruvananthapuram',
                'Kollam',
                'Pathanamthitta',
                'Alappuzha',
                'Kottayam',
                'Idukki',
                'Ernakulam',
                'Thrissur',
                'Palakkad',
                'Malappuram',
                'Kozhikode',
                'Wayanad',
                'Kannur',
                'Kasaragod',
            ]

            if district not in valid_districts:

                messages.error(
                    request,
                    'Please select a valid district.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            # ------------------------------------------------
            # AVAILABILITY
            # ------------------------------------------------

            valid_availability = [
                'Full Time',
                'Part Time',
                'Weekends',
                'Flexible',
            ]

            if availability not in valid_availability:

                messages.error(
                    request,
                    'Please select a valid availability.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            # ------------------------------------------------
            # PRICING VALIDATION
            # ------------------------------------------------

            if pricing_type not in ['hour', 'day']:

                messages.error(
                    request,
                    'Please select a valid pricing type.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            if pricing_amount == '':

                messages.error(
                    request,
                    'Please enter your pricing amount.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            try:

                pricing_amount = float(pricing_amount)

            except (
                ValueError,
                TypeError
            ):

                messages.error(
                    request,
                    'Please enter a valid pricing amount.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            if pricing_amount < 0:

                messages.error(
                    request,
                    'Pricing amount cannot be negative.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            if pricing_amount > 100000:

                messages.error(
                    request,
                    'Pricing amount seems too high. Please enter a valid amount.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            # ------------------------------------------------
            # ADDRESS
            # ------------------------------------------------

            if not address:

                messages.error(
                    request,
                    'Please enter your address.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            if len(address) < 10:

                messages.error(
                    request,
                    'Please enter a complete address.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            # ------------------------------------------------
            # PROOF TYPE
            # ------------------------------------------------

            valid_proof_types = ['aadhaar', 'pan', 'voter_id']

            if proof_type not in valid_proof_types:

                messages.error(
                    request,
                    'Please select a valid proof type (Aadhaar, PAN, or Voter ID).'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            # ------------------------------------------------
            # PROOF DOCUMENT
            # ------------------------------------------------

            if not proof_document:

                messages.error(
                    request,
                    'Please upload your proof document.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            # ---- PROOF DOCUMENT SECURITY VALIDATION ----
            # Extension whitelist, path safety, double-extension rejection,
            # MIME allow-list and magic-bytes content check.  A filename alone
            # is never trusted: the bytes are verified before the file is
            # stored, and the stored name is a random UUID (no client text).
            from django.core.validators import FileExtensionValidator
            from django.core.exceptions import ValidationError as DjangoValidationError

            proof_ext_whitelist = ('.pdf', '.jpg', '.jpeg', '.png')
            dangerous_inner_parts = frozenset((
                'exe', 'bat', 'cmd', 'com', 'scr', 'msi', 'jar', 'dll', 'apk',
                'js', 'jse', 'vbs', 'ps1', 'sh', 'php', 'html', 'htm', 'svg',
                'cgi', 'pl', 'py', 'rb',
            ))

            # Extension whitelist (validator raises; map to a friendly error)
            try:
                FileExtensionValidator(
                    allowed_extensions=['pdf', 'jpg', 'jpeg', 'png']
                )(proof_document)
            except DjangoValidationError:
                messages.error(
                    request,
                    'Proof must be a PDF, JPG, JPEG or PNG file.'
                )
                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            # Path traversal / directory-component rejection
            base_name = os.path.basename(proof_document.name)
            if base_name != proof_document.name or not base_name:
                messages.error(request, 'Invalid file name.')
                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            # Double-extension rejection (payload.pdf.exe / payload.exe.pdf)
            file_root, proof_ext = os.path.splitext(base_name)
            if proof_ext.lower() not in proof_ext_whitelist:
                messages.error(
                    request,
                    'Proof must be a PDF, JPG, JPEG or PNG file.'
                )
                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )
            inner_parts = file_root.lower().split('.')
            if any(part in dangerous_inner_parts for part in inner_parts):
                messages.error(
                    request,
                    'Proof must be a PDF, JPG, JPEG or PNG file.'
                )
                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            # MIME / content-type whitelist (client-declared; checked early)
            if proof_document.content_type not in (
                'application/pdf',
                'image/jpeg',
                'image/jpg',
                'image/png',
            ):
                messages.error(
                    request,
                    'Unsupported file type. Only PDF, JPG, JPEG or PNG allowed.'
                )
                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            # Magic-bytes (actual content) verification - defeats MIME spoofing
            head = proof_document.read(8)
            proof_document.seek(0)
            if not (
                head.startswith(b'%PDF')
                or head.startswith(b'\xFF\xD8\xFF')
                or head.startswith(b'\x89PNG\r\n\x1a\n')
            ):
                messages.error(
                    request,
                    'File content does not match the declared file type.'
                )
                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            # 5 MB MAXIMUM

            if proof_document.size > 5 * 1024 * 1024:

                messages.error(
                    request,
                    'Proof document must be less than 5 MB.'
                )

                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            # Store under a random, directory-free name - the client's
            # filename never reaches the filesystem.
            proof_document.name = '{}.{}'.format(
                uuid.uuid4().hex, proof_ext.lower()
            )

            # Full content validation: the bytes must decode as a real,
            # non-corrupt PDF / JPEG / PNG (not just carry the right header).
            try:
                from .upload_validation import validate_proof_document
                # The name set above is already a server-generated UUID; drop any
                # doubled dot so the validator's strict name check accepts it.
                proof_document.name = proof_document.name.replace('..', '.')
                validate_proof_document(proof_document)
            except DjangoValidationError as exc:
                messages.error(request, ' '.join(exc.messages))
                return render(
                    request,
                    'accounts/register.html',
                    {'services': services}
                )

            # ------------------------------------------------
            # PROFILE IMAGE
            # ------------------------------------------------

            if profile_image:

                allowed_image_extensions = [
                    '.jpg',
                    '.jpeg',
                    '.png',
                    '.webp',
                ]

                image_name = (
                    profile_image.name.lower()
                )

                if not any(
                    image_name.endswith(ext)
                    for ext in allowed_image_extensions
                ):

                    messages.error(
                        request,
                        'Profile image must be JPG, JPEG, PNG or WEBP.'
                    )

                    return render(
                        request,
                        'accounts/register.html',
                        {'services': services}
                    )

                if profile_image.size > 5 * 1024 * 1024:

                    messages.error(
                        request,
                        'Profile image must be less than 5 MB.'
                    )

                    return render(
                        request,
                        'accounts/register.html',
                        {'services': services}
                    )

            # Profile image content validation (magic bytes + full decode).
            if profile_image:
                try:
                    from .upload_validation import validate_profile_photo
                    validate_profile_photo(profile_image)
                except DjangoValidationError as exc:
                    messages.error(request, ' '.join(exc.messages))
                    return render(
                        request,
                        'accounts/register.html',
                        {'services': services}
                    )

            # ------------------------------------------------
            # WORKER DATA
            # ------------------------------------------------

            worker_data = {

                'phone': phone,

                'service': service,

                'custom_service': custom_service if is_custom_service else None,

                'experience_years': experience,

                'district': district,

                'availability': availability,

                'pricing_type': pricing_type,

                'pricing_amount': pricing_amount,

                'address': address,

                'proof_type': proof_type,

                'proof_document': proof_document,

                'profile_image': profile_image,

            }

        # ==================================================
        # CREATE ACCOUNT
        # ==================================================

        try:

            with transaction.atomic():

                user = CustomUser.objects.create_user(
                    username=username,
                    email=email,
                    password=password if role not in ['WORKER', 'USER'] else None,
                    role=role
                )
                if role in ['WORKER', 'USER']:
                    user.set_unusable_password()
                    user.save(update_fields=['password'])

                # --------------------------------------------
                # WORKER PROFILE
                # --------------------------------------------

                if role == 'WORKER':

                    worker_profile = WorkerProfile.objects.create(
                        user=user,
                        phone=worker_data['phone'],
                        service=worker_data['service'],
                        custom_service=worker_data['custom_service'],
                        experience_years=worker_data[
                            'experience_years'
                        ],
                        district=worker_data['district'],
                        availability=worker_data[
                            'availability'
                        ],
                        pricing_type=worker_data[
                            'pricing_type'
                        ],
                        pricing_amount=worker_data[
                            'pricing_amount'
                        ],
                        address=worker_data['address'],
                        proof_type=worker_data['proof_type'],
                        proof_document=worker_data[
                            'proof_document'
                        ],
                        profile_image=worker_data[
                            'profile_image'
                        ],
                        is_verified=False
                    )

                    if is_custom_service and custom_service:

                        ServiceRequest.objects.create(
                            name=custom_service,
                            requested_by=user,
                            status='PENDING',
                        )

        except Exception:

            messages.error(
                request,
                'Could not create the account. Please try again.'
            )

            return render(
                request,
                'accounts/register.html',
                {'services': services}
            )

        # ==================================================
        # SUCCESS — SEND OTP FOR EMAIL VERIFICATION
        # ==================================================

        _otp, otp_sent = send_otp_email(user)
        request.session['otp_user_id'] = user.id

        if otp_sent:
            messages.success(
                request,
                'Account created successfully. Please verify your email with the OTP sent.'
            )
        else:
            # The account exists but the mail did not go out. Do not pretend
            # otherwise, or the user is locked out with no explanation.
            messages.warning(
                request,
                'Account created successfully, but we could not send the '
                'verification email right now. Please use "Resend OTP" in a '
                'few minutes, or contact support if the problem continues.'
            )
        return redirect('verify_otp')

    return render(
        request,
        'accounts/register.html',
        {
            'services': services
        }
    )

@never_cache
def forgot_password_view(request, token=None):
    """Handles BOTH the email-submission state and the new-password state.

    Supports the legacy query-string reset flow and the direct reset-password/<token>/ route.
    """
    if request.user.is_authenticated:
        return redirect('dashboard')

    # Determine current state based on query string or path token.
    uid = request.GET.get('uid', '').strip()
    token = request.GET.get('token', '').strip() or token or ''

    # Validate token from query string (if any)
    reset_user = None
    token_valid = False
    if uid and token:
        try:
            reset_user = CustomUser.objects.get(pk=uid)
            if reset_user.is_reset_token_valid(token):
                token_valid = True
        except (CustomUser.DoesNotExist, ValueError):
            reset_user = None
            token_valid = False

    # Support direct reset-password/<token>/ links without a uid query param.
    if token and not token_valid:
        try:
            # Matches the stored hash first, falls back to legacy plaintext
            # tokens so reset links sent before hashing keep working.
            reset_user = CustomUser.find_by_reset_token(token)
            if reset_user:
                uid = str(reset_user.pk)
                token_valid = True
        except Exception as exc:
            # Class name only. This handler wraps CustomUser.find_by_reset_token(),
            # and that exception is raised while handling the reset token
            # itself, so str(exc) can quote the token (or part of it) and the uid
            # - both of which are live account-takeover credentials. Neither the
            # token nor the exception message is logged.
            logger.warning(
                'password_reset_token_lookup_failed exc_type=%s',
                type(exc).__name__,
            )
            reset_user = None
            token_valid = False

    # If the token in URL is invalid/expired -> show email form with a message
    if (uid or token) and not token_valid:
        messages.error(
            request,
            'This reset link is invalid or has expired. Please request a new one below.'
        )

    # ==========================================================
    # STATE 2: PASSWORD RESET (POST) - User submitted new password
    # ==========================================================
    if request.method == 'POST' and token_valid and reset_user:

        # Policy B3: Only administrators use password authentication and recovery
        if not _is_admin_account(reset_user):
            messages.error(request, 'Password reset is only available for administrator accounts. Customers and workers authenticate with OTP.')
            return redirect('login')

        password = request.POST.get('password', '').strip()
        confirm_password = request.POST.get('confirm_password', '').strip()

        if not password:
            messages.error(request, 'Please enter a new password.')
            return render(request, 'accounts/forgot_password.html', {
                'reset_mode': True,
                'reset_user': reset_user,
                'uid': uid,
                'token': token,
            })

        if not confirm_password:
            messages.error(request, 'Please confirm your new password.')
            return render(request, 'accounts/forgot_password.html', {
                'reset_mode': True,
                'reset_user': reset_user,
                'uid': uid,
                'token': token,
            })

        if password != confirm_password:
            messages.error(request, 'Passwords do not match.')
            return render(request, 'accounts/forgot_password.html', {
                'reset_mode': True,
                'reset_user': reset_user,
                'uid': uid,
                'token': token,
            })

        if len(password) < 8:
            messages.error(request, 'Password must be at least 8 characters.')
            return render(request, 'accounts/forgot_password.html', {
                'reset_mode': True,
                'reset_user': reset_user,
                'uid': uid,
                'token': token,
            })

        try:
            validate_password(password, user=reset_user)
        except ValidationError as exc:
            for message in exc.messages:
                messages.error(request, message)
            return render(request, 'accounts/forgot_password.html', {
                'reset_mode': True,
                'reset_user': reset_user,
                'uid': uid,
                'token': token,
            })

        # Re-verify the token once more right before saving (extra safety)
        if not reset_user.is_reset_token_valid(token):
            messages.error(
                request,
                'This reset link is invalid or has expired. Please request a new one below.'
            )
            return redirect('forgot_password')

        # Update password and consume the token
        reset_user.set_password(password)
        reset_user.password_reset_token = None
        reset_user.password_reset_token_hash = None
        reset_user.password_reset_expires = None
        reset_user.save(update_fields=[
            'password',
            'password_reset_token',
            'password_reset_token_hash',
            'password_reset_expires',
        ])

        messages.success(
            request,
            'Your password has been reset successfully. You can now login with your new password.'
        )
        return redirect('login')

    # ==========================================================
    # STATE 1: EMAIL SUBMISSION (POST or GET) - User submitted email
    # ==========================================================

    # Server-side throttle (cache-backed, not session-backed).
    fp_key = forgot_password_key(request, request.POST.get('email', '').strip())
    fp_ip_key = forgot_password_ip_key(request)

    if is_rate_limited(fp_key, FORGOT_PASSWORD_MAX_ATTEMPTS):
        messages.error(
            request,
            'Too many attempts. '
            f'Please try again in {minutes_remaining(fp_key)} minute(s).'
        )
        return render(request, 'accounts/forgot_password.html', {'locked_out': True})

    # Caps address enumeration: a single IP cannot hammer many different
    # e-mail addresses, each of which would otherwise get its own counter.
    if is_rate_limited(fp_ip_key, FORGOT_PASSWORD_MAX_ATTEMPTS * 5):
        messages.error(
            request,
            'Too many password reset requests. '
            f'Please try again in {minutes_remaining(fp_ip_key)} minute(s).'
        )
        return render(request, 'accounts/forgot_password.html', {'locked_out': True})

    if request.method == 'POST' and not token_valid:
        email = request.POST.get('email', '').strip()
        if not email:
            messages.error(request, 'Please enter your email address.')
            return render(request, 'accounts/forgot_password.html')

        try:
            user = CustomUser.objects.get(email__iexact=email)
        except (CustomUser.DoesNotExist, CustomUser.MultipleObjectsReturned):
            # Count the failure even for non-existing emails so the response
            # stays identical and the address cannot be enumerated.
            #
            # MultipleObjectsReturned is caught too: CustomUser.email is NOT
            # unique (it inherits AbstractUser), so a duplicate address - two
            # admin/superuser accounts, or a race in registration - made
            # .get() raise and turned this public page into an HTTP 500. The
            # ambiguous address is answered exactly like an unknown one, which
            # also avoids revealing that the address is shared.
            register_failure(fp_key, FORGOT_PASSWORD_MAX_ATTEMPTS, FORGOT_PASSWORD_LOCKOUT_SECONDS)
            register_failure(fp_ip_key, FORGOT_PASSWORD_MAX_ATTEMPTS * 5, FORGOT_PASSWORD_LOCKOUT_SECONDS)
            messages.success(
                request,
                'If an account with that email exists, a password reset link has been sent.'
            )
            return render(request, 'accounts/forgot_password.html')

        if not user.is_active:
            register_failure(fp_key, FORGOT_PASSWORD_MAX_ATTEMPTS, FORGOT_PASSWORD_LOCKOUT_SECONDS)
            register_failure(fp_ip_key, FORGOT_PASSWORD_MAX_ATTEMPTS * 5, FORGOT_PASSWORD_LOCKOUT_SECONDS)
            messages.success(
                request,
                'If an account with that email exists, a password reset link has been sent.'
            )
            return render(request, 'accounts/forgot_password.html')

        # Policy B3: Customer and Worker password reset prohibited (Admin password only)
        if not _is_admin_account(user):
            register_failure(fp_key, FORGOT_PASSWORD_MAX_ATTEMPTS, FORGOT_PASSWORD_LOCKOUT_SECONDS)
            register_failure(fp_ip_key, FORGOT_PASSWORD_MAX_ATTEMPTS * 5, FORGOT_PASSWORD_LOCKOUT_SECONDS)
            messages.info(
                request,
                'Password reset is only available for administrator accounts. Customers and service professionals sign in with OTP.'
            )
            return render(request, 'accounts/forgot_password.html')

        token = user.generate_password_reset_token()
        # Reset link points to the SAME forgot-password page
        reset_path = reverse('forgot_password')
        reset_url = (
            f"{request.build_absolute_uri(reset_path)}"
            f"?uid={user.pk}&token={token}"
        )

        # The body carries a live password-reset token, so it must never be
        # logged. send_email_safely() records only the operation tag, the
        # recipient count and the exception class.
        send_email_safely(
            subject='Reset Your LAMO Password',
            message=(
                f'Hi {user.username},\n\n'
                f'We received a request to reset your password. Click the link below to set a new password:\n\n'
                f'{reset_url}\n\n'
                f'This link will expire in 1 hour for security reasons.\n\n'
                f'If you didn\'t request this, you can safely ignore this email.\n\n'
                f'Best regards,\nTeam LAMO'
            ),
            recipient_list=[user.email],
            purpose='password_reset',
        )

        # A successful request clears that address's counter (the IP counter is
        # kept, so one IP cannot spray many addresses to dodge the limit).
        clear(fp_key)
        messages.success(
            request,
            'If an account with that email exists, a password reset link has been sent.'
        )
        return render(request, 'accounts/forgot_password.html')

    # ==========================================================
    # GET request: render appropriate state
    # ==========================================================
    context = {}
    if token_valid and reset_user:
        context = {
            'reset_mode': True,
            'reset_user': reset_user,
            'uid': uid,
            'token': token,
        }
    return render(request, 'accounts/forgot_password.html', context)

@never_cache
def login_view(request):

    # If already logged in
    if request.user.is_authenticated:
        return redirect('dashboard')

    # ------------------------------------------------------------------
    # TASK 1: honour ?next= so guests sent here from service_workers can
    # be redirected back after a successful login.  Mirrors exactly the
    # logic in register_view so the register -> OTP -> login path still
    # works (we do NOT clear the key when no next is given).
    # ------------------------------------------------------------------
    requested_next = request.POST.get('next') or request.GET.get('next')
    if requested_next:
        safe_next = _safe_internal_path(request, requested_next, '')
        if safe_next:
            request.session['registration_next'] = safe_next
        else:
            request.session.pop('registration_next', None)
    # A request that carries no ?next at all leaves whatever the session
    # already holds (e.g. set by register_view earlier in the flow).

    if request.method == 'POST':

        action = request.POST.get('action', '')
        if action == 'send_login_otp':
            identifier = (request.POST.get('identifier') or request.POST.get('username') or '').strip()
            if not identifier:
                return JsonResponse({'success': False, 'error': 'Please enter your email or mobile number.'})

            # Check network/IP rate limit before checking user existence (Finding A6)
            otp_ip_key = login_otp_ip_key(request)
            if is_rate_limited(otp_ip_key, 10):
                remaining = minutes_remaining(otp_ip_key)
                return JsonResponse({
                    'success': False,
                    'error': f'Too many login attempts from this network. Please try again in {max(1, remaining)} minute(s).'
                })

            matches = _users_matching_identifier(identifier)
            if len(matches) > 1:
                return JsonResponse({'success': False, 'error': 'This contact is linked to more than one account. Please contact support.'})
            user = matches[0] if matches else None
            if not user:
                register_failure(otp_ip_key, 10, LOGIN_LOCKOUT_SECONDS)
                return JsonResponse({'success': False, 'error': 'No account found with this email or mobile number. Please check your details or create an account.'})

            # Audit B3: administrators authenticate with their password only.
            if _is_admin_account(user):
                return JsonResponse({'success': False, 'admin_password_required': True, 'redirect_url': reverse('admin_login'), 'error': ADMIN_OTP_NOT_ALLOWED_MSG})

            if not user.is_active:
                return JsonResponse({'success': False, 'error': 'Your account has been deactivated. Please contact support.'})

            if user.otp_locked_until and timezone.now() < user.otp_locked_until:
                rem = int((user.otp_locked_until - timezone.now()).total_seconds() / 60)
                return JsonResponse({'success': False, 'error': f'Too many failed attempts. Please try again in {max(1, rem)} minute(s).'})

            if user.role == 'WORKER':
                try:
                    wp = user.worker_profile
                    if getattr(wp, 'work_status', '') == 'SUSPENDED':
                        return JsonResponse({'success': False, 'error': 'Your worker account has been suspended. Please contact support.'})
                    if not wp.is_verified:
                        return JsonResponse({
                            'success': False,
                            'pending_verification': True,
                            'error': 'Your worker application is currently pending admin verification. Once verified, an approval email with your direct dashboard link will be sent to your email.'
                        })
                except WorkerProfile.DoesNotExist:
                    pass

            # Rate limit OTP sending (per IP & per User) to prevent mail-bombing / SMS-bombing
            ip_key = otp_resend_ip_key(request)
            user_key = otp_resend_user_key(user.id)
            max_key = user_key + ':window'

            if is_rate_limited(max_key, OTP_RESEND_MAX):
                return JsonResponse({
                    'success': False,
                    'error': f'Maximum OTP requests reached for this account. Please try again in {minutes_remaining(max_key)} minute(s).'
                })

            if check_cooldown(ip_key, OTP_RESEND_COOLDOWN_SECONDS) or check_cooldown(user_key, OTP_RESEND_COOLDOWN_SECONDS):
                return JsonResponse({
                    'success': False,
                    'error': f'An OTP was sent very recently. Please wait {max(1, int(OTP_RESEND_COOLDOWN_SECONDS // 60))} minute(s) before requesting another one.'
                })

            register_attempt_within_window(max_key, OTP_RESEND_MAX, OTP_RESEND_WINDOW_SECONDS)
            otp = user.generate_otp()

            # Find phone number if available
            user_phone = None
            try:
                if hasattr(user, 'user_profile') and user.user_profile and user.user_profile.phone:
                    user_phone = user.user_profile.phone
                elif hasattr(user, 'worker_profile') and user.worker_profile and user.worker_profile.phone:
                    user_phone = user.worker_profile.phone
            except Exception:
                pass

            if '@' not in identifier:
                raw_digits = re.sub(r'\D', '', identifier)
                if len(raw_digits) in (10, 11, 12):
                    user_phone = user_phone or raw_digits

            # Send SMS via Twilio if enabled and phone number available
            sms_delivered = False
            if user_phone:
                sms_delivered = bool(send_otp_sms(user_phone, otp))

            # Send Email if user has an email address
            email_delivered = False
            if user.email:
                subject = 'LAMO - Your Verification OTP'
                message = (
                    f'Hi {user.username},\n\n'
                    f'Your OTP for login verification is: {otp}\n\n'
                    f'This OTP is valid for 10 minutes.\n'
                    f'Do not share this OTP with anyone.\n\n'
                    f'Thanks,\nLAMO Team'
                )
                email_delivered = bool(send_email_safely(
                    subject=subject,
                    message=message,
                    recipient_list=[user.email],
                    purpose='otp_verification',
                ))
                if settings.DEBUG and not email_delivered:
                    email_delivered = True

            if not (sms_delivered or email_delivered):
                # Nothing reached the user: say so instead of a fake success.
                if user_phone and not user.email:
                    error_text = ('We could not send an SMS code to this mobile number right now. '
                                  'Please try again later or contact support.')
                else:
                    error_text = 'We could not send your verification code right now. Please try again in a moment.'
                return JsonResponse({'success': False, 'error': error_text})

            start_cooldown(ip_key, OTP_RESEND_COOLDOWN_SECONDS)
            start_cooldown(user_key, OTP_RESEND_COOLDOWN_SECONDS)

            # Mask the channel the code was actually delivered to.
            if '@' not in identifier and sms_delivered:
                target_str = user_phone
            elif email_delivered:
                target_str = user.email
            else:
                target_str = user_phone or identifier
            if '@' in target_str:
                p = target_str.split('@')
                masked = (p[0][:2] + '***@' + p[1]) if len(p[0]) > 2 else ('*@' + p[1])
            else:
                digits = re.sub(r'\D', '', target_str)
                masked = digits[:2] + '******' + digits[-2:] if len(digits) >= 4 else digits

            return JsonResponse({'success': True, 'masked_target': masked, 'message': f'Verification OTP sent to {masked}'})

        if action == 'verify_login_otp':
            identifier = (request.POST.get('identifier') or request.POST.get('username') or '').strip()
            otp = request.POST.get('otp', '').strip()
            requested_next = request.POST.get('next') or request.GET.get('next')
            if not identifier or not otp:
                return JsonResponse({'success': False, 'error': 'Please enter both your identifier and the 6-digit OTP.'})

            matches = _users_matching_identifier(identifier)
            if len(matches) > 1:
                return JsonResponse({'success': False, 'error': 'This contact is linked to more than one account. Please contact support.'})
            user = matches[0] if matches else None
            if not user:
                return JsonResponse({'success': False, 'error': 'No account found with this email or mobile number.'})

            # Audit B3: administrators authenticate with their password only.
            if _is_admin_account(user):
                return JsonResponse({'success': False, 'admin_password_required': True, 'redirect_url': reverse('admin_login'), 'error': ADMIN_OTP_NOT_ALLOWED_MSG})

            if not user.is_active:
                return JsonResponse({'success': False, 'error': 'Your account has been deactivated. Please contact support.'})

            if user.otp_locked_until and timezone.now() < user.otp_locked_until:
                rem = int((user.otp_locked_until - timezone.now()).total_seconds() / 60)
                return JsonResponse({'success': False, 'error': f'Too many failed attempts. Please try again in {max(1, rem)} minute(s).'})

            if user.role == 'WORKER':
                try:
                    wp = user.worker_profile
                    if getattr(wp, 'work_status', '') == 'SUSPENDED':
                        return JsonResponse({'success': False, 'error': 'Your worker account has been suspended. Please contact support.'})
                    if not wp.is_verified:
                        return JsonResponse({
                            'success': False,
                            'pending_verification': True,
                            'error': 'Your worker application is currently pending admin verification. Once verified, an approval email with your direct dashboard link will be sent to your email.'
                        })
                except WorkerProfile.DoesNotExist:
                    pass

            if user.otp_created_at and timezone.now() > user.otp_created_at + timedelta(minutes=10):
                return JsonResponse({'success': False, 'error': 'Verification code has expired. Please request a new code.'})

            if not user.is_otp_valid(otp):
                user.otp_failed_attempts = (user.otp_failed_attempts or 0) + 1
                if user.otp_failed_attempts >= 5:
                    user.otp_locked_until = timezone.now() + timedelta(minutes=15)
                user.save(update_fields=['otp_failed_attempts', 'otp_locked_until'])
                return JsonResponse({'success': False, 'error': 'Invalid 6-digit verification code. Please try again.'})

            user.otp_failed_attempts = 0
            user.otp_locked_until = None
            user.is_email_verified = True
            user.email_verification_otp = None
            user.save(update_fields=['otp_failed_attempts', 'otp_locked_until', 'is_email_verified', 'email_verification_otp'])

            # Clear OTP resend rate limit keys on successful verification
            clear(otp_resend_user_key(user.id) + ':window')
            clear(otp_resend_user_key(user.id))
            clear(otp_resend_ip_key(request))
            clear(login_otp_ip_key(request))

            if user.role == 'WORKER':
                try:
                    wp = user.worker_profile
                    if getattr(wp, 'work_status', '') == 'SUSPENDED':
                        return JsonResponse({'success': False, 'error': 'Your worker account has been suspended. Please contact support.'})
                    if not wp.is_verified:
                        return JsonResponse({
                            'success': False,
                            'pending_verification': True,
                            'error': 'Your worker account is waiting for admin verification. You cannot login until your account is verified.'
                        })
                except WorkerProfile.DoesNotExist:
                    return JsonResponse({'success': False, 'error': 'Worker profile not found.'})

            login(request, user)
            request.session.cycle_key()

            safe_next = _safe_internal_path(request, requested_next or request.session.pop('registration_next', None), '')
            if user.role == 'ADMIN' or getattr(user, 'is_superuser', False):
                dest = reverse('admin_dashboard')
            elif user.role == 'WORKER':
                dest = reverse('worker_dashboard')
            else:
                dest = safe_next if safe_next else reverse('home')

            return JsonResponse({'success': True, 'redirect_url': dest})

        if action == 'login_password':
            identifier = (request.POST.get('identifier') or request.POST.get('username') or '').strip()
            password = request.POST.get('password', '')
            requested_next = request.POST.get('next') or request.GET.get('next')

            if not identifier or not password:
                return JsonResponse({'success': False, 'error': 'Please enter both your identifier and password.'})

            resolved_username = identifier
            matched_user = _find_user_by_identifier(identifier) if ('@' in identifier or _normalize_phone(identifier)) else None
            if matched_user:
                resolved_username = matched_user.username

            rl_key = login_key(request, identifier)
            if is_rate_limited(rl_key, LOGIN_MAX_ATTEMPTS):
                remaining = minutes_remaining(rl_key)
                return JsonResponse({'success': False, 'error': f'Too many failed login attempts. Please try again in {remaining} minute(s).'})

            user = authenticate(request, username=resolved_username, password=password)
            if user is None:
                register_failure(rl_key, LOGIN_MAX_ATTEMPTS, LOGIN_LOCKOUT_SECONDS)
                return JsonResponse({'success': False, 'error': 'Invalid username, email, or password.'})

            if not user.is_active:
                return JsonResponse({'success': False, 'error': 'Your account has been deactivated. Please contact support.'})

            # Audit B3: password sign-in is for administrators only.
            # Customers and workers sign in with a one-time code.
            if not _is_admin_account(user):
                return JsonResponse({'success': False, 'otp_required': True, 'error': PASSWORD_LOGIN_ADMIN_ONLY_MSG})

            clear(rl_key)
            is_admin_account = (
                user.role == 'ADMIN'
                or getattr(user, 'is_superuser', False)
            )
            if is_admin_account:
                if user.role != 'ADMIN' or not user.is_email_verified:
                    user.role = 'ADMIN'
                    user.is_email_verified = True
                    user.save(update_fields=['role', 'is_email_verified'])

            if not is_admin_account and user.role in ['USER', 'WORKER'] and not user.is_email_verified:
                _issue_login_otp(request, user)
                request.session['otp_user_id'] = user.id
                return JsonResponse({
                    'success': False,
                    'needs_verification': True,
                    'redirect_url': reverse('verify_otp'),
                    'error': 'Please verify your email before logging in. An OTP has been sent.'
                })

            if user.role == 'WORKER':
                try:
                    wp = user.worker_profile
                    if getattr(wp, 'work_status', '') == 'SUSPENDED':
                        return JsonResponse({'success': False, 'error': 'Your worker account has been suspended. Please contact support.'})
                    if not wp.is_verified:
                        return JsonResponse({
                            'success': False,
                            'pending_verification': True,
                            'error': 'Your worker account is waiting for admin verification. You cannot login until your account is verified.'
                        })
                except WorkerProfile.DoesNotExist:
                    return JsonResponse({'success': False, 'error': 'Worker profile not found.'})

            login(request, user)
            request.session.cycle_key()

            safe_next = _safe_internal_path(request, requested_next or request.session.pop('registration_next', None), '')
            if user.role == 'ADMIN' or getattr(user, 'is_superuser', False):
                dest = reverse('admin_dashboard')
            elif user.role == 'WORKER':
                dest = reverse('worker_dashboard')
            else:
                dest = safe_next if safe_next else reverse('home')

            return JsonResponse({'success': True, 'redirect_url': dest})

        # ==================================================
        # BRUTE FORCE PROTECTION (server-side)
        # ==================================================
        # The counters live in Django's cache, NOT in request.session.
        # A session is a client-held cookie: deleting it (or using a fresh
        # private window) used to hand the attacker a brand new set of
        # counters, making the lockout worthless. With the counters on the
        # server, clearing cookies changes nothing.
        auth_identifier = request.POST.get(
            'username',
            ''
        ).strip()

        rl_key = login_key(request, auth_identifier)

        if is_rate_limited(rl_key, LOGIN_MAX_ATTEMPTS):
            remaining = minutes_remaining(rl_key)
            messages.error(
                request,
                'Too many failed login attempts. '
                f'Please try again in about {remaining} minute(s).'
            )
            return render(request, 'accounts/login.html')

        password = request.POST.get(
            'password',
            ''
        )

        # Support Email or Phone in addition to Username (ambiguous
        # identifiers never resolve to an arbitrary account).
        resolved_username = auth_identifier
        if '@' in auth_identifier or _normalize_phone(auth_identifier):
            matched_user = _find_user_by_identifier(auth_identifier)
            if matched_user:
                resolved_username = matched_user.username

        user = authenticate(
            request,
            username=resolved_username,
            password=password
        )

        # Audit B3: password sign-in is for administrators only.
        if user is not None and not _is_admin_account(user):
            clear(rl_key)
            messages.info(request, PASSWORD_LOGIN_ADMIN_ONLY_MSG)
            return render(request, 'accounts/login.html')

        if user is not None:

            # ==================================================
            # ADMIN / SUPERUSER GUARANTEE (No OTP, Always ADMIN)
            # ==================================================
            is_admin_account = (
                user.role == 'ADMIN'
                or getattr(user, 'is_superuser', False)
            )
            if is_admin_account:
                if user.role != 'ADMIN' or not user.is_email_verified:
                    user.role = 'ADMIN'
                    user.is_email_verified = True
                    user.save(update_fields=['role', 'is_email_verified'])

            # ==================================================
            # EMAIL VERIFICATION CHECK (User & Worker only)
            # ==================================================
            if not is_admin_account and user.role in ['USER', 'WORKER'] and not user.is_email_verified:
                # The credentials were correct, so clear the brute-force
                # counter before anything else.
                clear(rl_key)

                # Do not let an unverified account be used to mail-bomb: a
                # correct password repeated in a loop used to fire a fresh OTP
                # on every single attempt. One OTP per cooldown window.
                if _issue_login_otp(request, user):
                    messages.warning(
                        request,
                        'Please verify your email before logging in. '
                        'An OTP has been sent to your email.'
                    )
                else:
                    # Either the cooldown is still active, or the mail backend
                    # failed. Say so instead of claiming a mail was sent.
                    messages.warning(
                        request,
                        'Please verify your email before logging in. '
                        'We could not send a new OTP just now - please use the '
                        'one already sent, or request another in a minute.'
                    )
                request.session['otp_user_id'] = user.id
                return redirect('verify_otp')

            if not user.is_active:
                messages.error(request, 'This account has been deactivated. Please contact support.')
                return redirect('login')

            # ==================================================
            # WORKER VERIFICATION CHECK
            # ==================================================

            if user.role == 'WORKER':

                try:
                    worker_profile = user.worker_profile

                except WorkerProfile.DoesNotExist:

                    messages.error(
                        request,
                        'Worker profile not found.'
                    )

                    return redirect('login')

                # Worker is SUSPENDED
                if getattr(worker_profile, 'work_status', '') == 'SUSPENDED':
                    messages.error(
                        request,
                        'Your worker account has been suspended. Please contact support.'
                    )
                    return redirect('login')

                # Worker is NOT verified
                if not worker_profile.is_verified:

                    messages.warning(
                        request,
                        'Your worker account is waiting for admin verification. '
                        'You cannot login until your account is verified.'
                    )

                    return redirect('login')


            # ==================================================
            # LOGIN
            # ==================================================

            login(
                request,
                user
            )

            # Rotate session key to prevent session fixation attacks
            request.session.cycle_key()

            # Successful login - clear the server-side brute-force counter so
            # a legitimate user is never locked out by earlier typos.
            clear(rl_key)

            # ==================================================
            # HONOUR THE INTENDED DESTINATION
            # ==================================================
            # register_view stored a validated local path here (the service the
            # guest was viewing before they were asked to sign up). It is popped
            # so the value is single-use, and it is re-validated here because it
            # now sits in the session between two requests. Anything unsafe falls
            # through to the normal role-based destination, so behaviour is
            # unchanged for anyone who did not arrive via a ?next= link.
            # Admins always go straight to the admin dashboard.
            if not is_admin_account:
                pending_next = request.session.pop('registration_next', None)
                safe_next = _safe_internal_path(request, pending_next, '')
                if safe_next:
                    return redirect(safe_next)

            # ==================================================
            # ROLE BASED REDIRECTION
            # ==================================================

            if is_admin_account or user.role == 'ADMIN':

                return redirect(
                    'admin_dashboard'
                )


            elif user.role == 'WORKER':

                return redirect(
                    'worker_dashboard'
                )


            elif user.role == 'USER':

                return redirect(
                    'home'
                )


            # ==================================================
            # UNKNOWN ROLE
            # ==================================================

            logout(request)

            messages.error(
                request,
                'Invalid user role.'
            )

            return redirect('login')


        else:

            # Count the failure in the server-side counter and lock the
            # account/IP pair out once the threshold is reached.
            locked, remaining = register_failure(
                rl_key,
                LOGIN_MAX_ATTEMPTS,
                LOGIN_LOCKOUT_SECONDS,
            )

            if locked:
                logger.warning(
                    'Login lockout triggered for username=%r from IP %s',
                    auth_identifier,
                    request.META.get('REMOTE_ADDR'),
                )
                messages.error(
                    request,
                    'Too many failed login attempts. '
                    f'Please try again in {remaining} minute(s).'
                )
                return render(request, 'accounts/login.html')

            messages.error(
                request,
                'Invalid username or password.'
            )


    return render(
        request,
        'accounts/login.html'
    )


def admin_login_view(request):
    """Dedicated secure sign-in portal for administrators and staff."""
    if request.user.is_authenticated and _is_admin_account(request.user):
        return redirect('admin_dashboard')

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')

        if not username or not password:
            messages.error(request, 'Please provide both administrator username and password.')
            return render(request, 'accounts/admin_login.html', {'entered_username': username})

        rl_key = login_key(request, 'admin_' + username.lower())
        if is_rate_limited(rl_key, LOGIN_MAX_ATTEMPTS):
            mins = minutes_remaining(rl_key)
            messages.error(
                request,
                f'Too many failed login attempts on administrator portal. Please try again in {mins} minute(s).'
            )
            return render(request, 'accounts/admin_login.html', {'entered_username': username})

        user = authenticate(request, username=username, password=password)
        if user is not None:
            if not user.is_active:
                messages.error(request, 'This administrator account has been disabled.')
                return render(request, 'accounts/admin_login.html', {'entered_username': username})

            # Audit B2: only the ADMIN role or a genuine superuser may use the
            # admin portal. Plain Django staff accounts are refused and are
            # never promoted.
            if not _is_admin_account(user):
                register_failure(rl_key, LOGIN_MAX_ATTEMPTS, LOGIN_LOCKOUT_SECONDS)
                messages.error(request, 'Access denied. This portal is restricted to system administrators.')
                return render(request, 'accounts/admin_login.html', {'entered_username': username})

            clear(rl_key)

            if user.role != 'ADMIN' and user.is_superuser:
                user.role = 'ADMIN'
                user.save(update_fields=['role'])
            if not user.is_email_verified:
                user.is_email_verified = True
                user.save(update_fields=['is_email_verified'])

            login(request, user)
            request.session.cycle_key()
            messages.success(request, f'Welcome back, Administrator {user.username}!')
            return redirect('admin_dashboard')
        else:
            is_locked, mins = register_failure(rl_key, LOGIN_MAX_ATTEMPTS, LOGIN_LOCKOUT_SECONDS)
            if is_locked:
                messages.error(
                    request,
                    f'Too many failed login attempts. Your administrator access has been temporarily locked for {mins} minute(s).'
                )
            else:
                messages.error(request, 'Invalid administrator credentials. Please check your username and password.')
            return render(request, 'accounts/admin_login.html', {'entered_username': username})

    return render(request, 'accounts/admin_login.html')


@require_POST
@never_cache
def logout_view(request):
    """Sign the current user out.

    POST only: a logout link that works over GET can be triggered by any
    third-party page or by browser link prefetching, which logs the user out
    without their consent.
    """

    logout(request)

    return redirect('home')


def dashboard(request):

    if not request.user.is_authenticated:
        return redirect('login')

    if _is_admin_account(request.user):
        return redirect('admin_dashboard')

    elif request.user.role == 'USER':
        return redirect('home')

    elif request.user.role == 'WORKER':
        return redirect('worker_dashboard')

    return redirect('home')


@login_required
def service_requests_list(request):

    if request.user.role != 'ADMIN':
        messages.error(request, 'You are not authorized to access this page.')
        return redirect('dashboard')

    status_filter = request.GET.get('status', '').strip()

    service_requests = ServiceRequest.objects.all().select_related(
        'requested_by', 'reviewed_by', 'published_service'
    )

    if status_filter:
        service_requests = service_requests.filter(status=status_filter)

    service_requests = service_requests.order_by('-created_at')

    pending_count = ServiceRequest.objects.filter(status='PENDING').count()
    approved_count = ServiceRequest.objects.filter(status='APPROVED').count()
    published_count = ServiceRequest.objects.filter(status='PUBLISHED').count()
    rejected_count = ServiceRequest.objects.filter(status='REJECTED').count()

    return render(
        request,
        'dashboards/service_requests.html',
        {
            'service_requests': service_requests,
            'status_filter': status_filter,
            'pending_count': pending_count,
            'approved_count': approved_count,
            'published_count': published_count,
            'rejected_count': rejected_count,
        }
    )


@login_required
def service_request_approve(request, pk):

    if request.user.role != 'ADMIN':
        messages.error(request, 'You are not authorized to perform this action.')
        return redirect('dashboard')

    if request.method != 'POST':
        messages.error(request, 'Invalid request method.')
        return redirect('service_requests')

    service_request = get_object_or_404(ServiceRequest, pk=pk)

    if service_request.status != 'PENDING':
        messages.error(request, 'This request has already been processed.')
        return redirect('service_requests')

    admin_remark = request.POST.get('admin_remark', '').strip()

    if request.POST.get('confirm') == 'yes':
        service_request.status = 'APPROVED'
        service_request.admin_remark = admin_remark
        service_request.reviewed_by = request.user
        service_request.reviewed_at = timezone.now()
        service_request.save(update_fields=['status', 'admin_remark', 'reviewed_by', 'reviewed_at', 'updated_at'])

        Notification.objects.create(
            recipient=service_request.requested_by,
            notification_type='SYSTEM',
            message=f'Your service request "{service_request.name}" has been approved. It will be published soon.'
        )

        messages.success(request, f'Service request "{service_request.name}" has been approved.')
    else:
        service_request.status = 'REJECTED'
        service_request.admin_remark = admin_remark or 'No reason provided.'
        service_request.reviewed_by = request.user
        service_request.reviewed_at = timezone.now()
        service_request.save(update_fields=['status', 'admin_remark', 'reviewed_by', 'reviewed_at', 'updated_at'])

        Notification.objects.create(
            recipient=service_request.requested_by,
            notification_type='SYSTEM',
            message=f'Your service request "{service_request.name}" has been rejected. Reason: {service_request.admin_remark}'
        )

        messages.warning(request, f'Service request "{service_request.name}" has been rejected.')

    return redirect('service_requests')


@login_required
def service_request_publish(request, pk):

    if request.user.role != 'ADMIN':
        messages.error(request, 'You are not authorized to access this page.')
        return redirect('dashboard')

    service_request = get_object_or_404(
        ServiceRequest.objects.select_related('requested_by'),
        pk=pk,
        status='APPROVED'
    )

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()
        image = request.FILES.get('image')

        if not name:
            messages.error(request, 'Service name is required.')
            return render(request, 'dashboards/service_request_publish.html', {
                'service_request': service_request,
            })

        if not description:
            messages.error(request, 'Service description is required.')
            return render(request, 'dashboards/service_request_publish.html', {
                'service_request': service_request,
            })

        if not image:
            messages.error(request, 'Service image is required.')
            return render(request, 'dashboards/service_request_publish.html', {
                'service_request': service_request,
            })

        try:
            with transaction.atomic():
                # A worker-created service can be requested by more than one
                # worker. Reuse an existing Service row instead of hitting the
                # unique-name constraint, so every requesting worker is pointed
                # at the same published service.
                #
                # Only an ACTIVE service may be reused. Service.name is
                # unique=True, so a same-named replacement cannot be created,
                # and an inactive service is a deliberate admin decision
                # (ServiceForm exposes is_active, see P0-2's "deactivate
                # instead of delete" guidance). Reusing one silently would:
                #   * mark the request PUBLISHED while the service stays
                #     invisible in every customer-facing query, all of which
                #     filter is_active=True (service_page, service_detail,
                #     service_workers, book_service, emergency booking, the AI
                #     matcher), and
                #   * re-point the requesting worker at that unusable service,
                #     removing them from every listing too.
                # So refuse instead, and leave the request APPROVED so the admin
                # can reactivate or rename and retry. The inactive service is
                # never silently reactivated.
                inactive_match = Service.objects.filter(
                    name__iexact=name,
                    is_active=False,
                ).first()

                if inactive_match is not None:
                    logger.warning(
                        'Refused to publish request #%s: service "%s" exists '
                        'but is deactivated; not reactivating it implicitly.',
                        service_request.id,
                        inactive_match.name,
                    )
                    messages.error(
                        request,
                        f'A service named "{inactive_match.name}" already '
                        'exists but has been deactivated, so it cannot be '
                        'published. Reactivate that service (Services > Edit) '
                        'or publish this request under a different name.'
                    )
                    return render(request, 'dashboards/service_request_publish.html', {
                        'service_request': service_request,
                    })

                service = Service.objects.filter(
                    name__iexact=name,
                    is_active=True,
                ).first()

                if service is None:
                    service = Service.objects.create(
                        name=name,
                        description=description,
                        image=image,
                        is_active=True,
                        is_featured=False,
                    )

                # Final invariant, asserted server-side rather than assumed:
                # a request may only be PUBLISHED against an active service,
                # otherwise every customer-facing query filters it out.
                if not service.is_active:
                    logger.error(
                        'Refusing to publish request #%s against inactive '
                        'service #%s (defensive guard).',
                        service_request.id,
                        service.id,
                    )
                    messages.error(
                        request,
                        'This service is currently deactivated and cannot be '
                        'published. Reactivate it first, then try again.'
                    )
                    return render(request, 'dashboards/service_request_publish.html', {
                        'service_request': service_request,
                    })

                service_request.status = 'PUBLISHED'
                service_request.published_service = service
                service_request.save(update_fields=['status', 'published_service', 'updated_at'])

                # ------------------------------------------------
                # LINK THE REQUESTING WORKER TO THE PUBLISHED SERVICE
                # ------------------------------------------------
                # WorkerProfile.service is the single worker<->service relation
                # used by service_workers, build_worker_recommendations and
                # book_service. Publishing without this link left the service
                # public but with zero associated workers ("No workers found").
                #
                # A worker who has since chosen a real service in their profile
                # is left untouched, and workers marked as suspended are still
                # excluded from every public listing by is_verified/work_status.
                linked_workers = WorkerProfile.objects.filter(
                    user=service_request.requested_by,
                    service__isnull=True,
                ).update(service=service)

                logger.info(
                    'Published service "%s" (id=%s) from request #%s - linked %s worker profile(s).',
                    service.name,
                    service.id,
                    service_request.id,
                    linked_workers,
                )

                Notification.objects.create(
                    recipient=service_request.requested_by,
                    notification_type='SYSTEM',
                    message=f'Your service request "{service.name}" has been published and is now visible on LAMO.'
                )

                messages.success(request, f'Service "{service.name}" has been published successfully.')
                return redirect('service_requests')

        except Exception as exc:
            # Class name only. This block saves an uploaded service image, and a
            # storage/DB error can quote the absolute MEDIA path and file name.
            # logger.exception() (== exc_info=True) would reprint str(exc) in the
            # traceback tail, so the plain class name is used instead.
            logger.error(
                'service_request_publish_failed request=%s exc_type=%s',
                service_request.id,
                type(exc).__name__,
            )
            messages.error(request, 'Could not publish the service. Please try again.')
            return render(request, 'dashboards/service_request_publish.html', {
                'service_request': service_request,
            })

    return render(request, 'dashboards/service_request_publish.html', {
        'service_request': service_request,
    })


@never_cache
def admin_dashboard(request):

    if not request.user.is_authenticated:
        return redirect('login')

    # Plain Django staff accounts are not LAMO admins (same rule as login).
    is_admin_user = (
        request.user.role == 'ADMIN'
        or getattr(request.user, 'is_superuser', False)
    )

    if not is_admin_user:
        return redirect('dashboard')

    if request.user.role != 'ADMIN' and request.user.is_superuser:
        request.user.role = 'ADMIN'
        request.user.is_email_verified = True
        request.user.save(update_fields=['role', 'is_email_verified'])

    from django.db.models import Count, Avg, Sum, Q, Case, When, Value, IntegerField
    from django.db.models.functions import Coalesce

    services = Service.objects.all()
    users = CustomUser.objects.filter(role='USER')
    workers = CustomUser.objects.filter(role='WORKER')

    booking_stats = Booking.objects.aggregate(
        total=Count('id'),
        pending=Count('id', filter=Q(status='PENDING')),
        emergency=Count('id', filter=Q(status='EMERGENCY_PENDING')),
        accepted=Count('id', filter=Q(status='ACCEPTED')),
        in_progress=Count('id', filter=Q(status='IN_PROGRESS')),
        completed=Count('id', filter=Q(status='COMPLETED')),
        cancelled=Count('id', filter=Q(status='CANCELLED')),
        completion_requested=Count('id', filter=Q(status='COMPLETION_REQUESTED')),
        revenue=Sum(Coalesce('final_amount', 'estimated_cost'), filter=Q(is_paid=True, status__in=['COMPLETED', 'IN_PROGRESS', 'ACCEPTED', 'COMPLETION_REQUESTED'])),
    )
    total_bookings = booking_stats['total'] or 0
    pending_bookings = (booking_stats['pending'] or 0) + (booking_stats['emergency'] or 0)
    completed_bookings = booking_stats['completed'] or 0
    active_bookings = (booking_stats['accepted'] or 0) + (booking_stats['in_progress'] or 0) + (booking_stats['completion_requested'] or 0)
    total_revenue = booking_stats['revenue'] or 0

    booking_status_data = {
        'Pending': booking_stats['pending'] or 0,
        'Emergency': booking_stats['emergency'] or 0,
        'Accepted': booking_stats['accepted'] or 0,
        'In Progress': booking_stats['in_progress'] or 0,
        'Completed': booking_stats['completed'] or 0,
        'Cancelled': booking_stats['cancelled'] or 0,
    }

    complaint_stats = Complaint.objects.aggregate(
        total=Count('id'),
        open=Count('id', filter=Q(status='OPEN')),
    )
    total_complaints = complaint_stats['total'] or 0
    open_complaints = complaint_stats['open'] or 0

    avg_rating = Review.objects.all().aggregate(avg=Avg('rating'))['avg'] or 0

    recent_bookings = Booking.objects.select_related('customer', 'worker__user', 'service').order_by('-created_at')[:10]

    top_workers = WorkerProfile.objects.annotate(
        total_jobs=Count('bookings'),
        avg_rating=Avg('reviews_received__rating')
    ).order_by('-total_jobs')[:5]

    new_enquiries = ContactEnquiry.objects.filter(status='NEW').count()

    import json
    from django.utils import timezone
    import datetime

    # Commission & Revenue Metrics (10% platform fee)
    gross_revenue = float(total_revenue)
    commission_rate = 10
    platform_commission = round(gross_revenue * 0.10, 2)
    worker_payouts = round(gross_revenue * 0.90, 2)
    # Actual platform revenue: commission charged to worker wallets at job
    # acceptance, minus commission refunds (the ledger is the source of truth).
    _ledger_net = WalletTransaction.objects.filter(
        transaction_type__in=('FEE_DEDUCTION', 'REFUND')
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    commission_collected = max(Decimal('0.00'), -Decimal(str(_ledger_net)))

    # Platform Dues & Launch Metrics
    negative_wallets = WorkerWallet.objects.filter(balance__lt=0)
    total_worker_dues = abs(negative_wallets.aggregate(total=Sum('balance'))['total'] or Decimal('0.00'))
    paused_workers_count = WorkerProfile.objects.filter(is_paused_for_dues=True).count()
    active_launch_workers_count = WorkerProfile.objects.filter(launch_ends_at__gt=timezone.now()).count()
    cash_bookings_count = Booking.objects.filter(payment_method='CASH').count()
    online_bookings_count = Booking.objects.filter(payment_method='ONLINE').count()

    # Monthly revenue trend (last 6 months)
    now = timezone.now()
    monthly_revenue = []
    for i in range(5, -1, -1):
        target_date = now - datetime.timedelta(days=i * 30)
        y = target_date.year
        m = target_date.month
        label = target_date.strftime('%b')

        # Direct cash bookings revenue + online payments without double counting
        cash_rev = Booking.objects.filter(
            is_paid=True,
            payment_method='CASH',
            created_at__year=y,
            created_at__month=m
        ).aggregate(total=Sum(Coalesce('final_amount', 'estimated_cost')))['total'] or 0
        online_rev = Payment.objects.filter(
            status='PAID',
            booking__created_at__year=y,
            booking__created_at__month=m
        ).aggregate(total=Sum('amount'))['total'] or 0
        m_rev = float(cash_rev + online_rev)
        m_comm = round(m_rev * 0.10, 2)

        monthly_revenue.append({
            'month': label,
            'year': y,
            'revenue': m_rev,
            'commission': m_comm,
            'payout': round(m_rev * 0.90, 2),
        })

    # Top earning services
    top_services_qs = Service.objects.filter(
        bookings__is_paid=True
    ).annotate(
        total=Sum('bookings__estimated_cost')
    ).order_by('-total')[:5]

    top_services_data = [
        {
            'name': s.name,
            'revenue': float(s.total or 0),
            'commission': round(float(s.total or 0) * 0.10, 2),
            'bookings_count': s.bookings.filter(is_paid=True).count(),
        }
        for s in top_services_qs
    ]

    # Revenue by district
    district_revenue_qs = Booking.objects.filter(
        is_paid=True,
        district__isnull=False
    ).exclude(district='').values('district').annotate(
        total=Sum('estimated_cost')
    ).order_by('-total')[:6]

    district_revenue_data = [
        {
            'district': dr['district'],
            'revenue': float(dr['total'] or 0),
            'commission': round(float(dr['total'] or 0) * 0.10, 2),
        }
        for dr in district_revenue_qs
    ]

    # Live District Operations Map Data
    active_bookings_qs = Booking.objects.filter(
        status__in=['PENDING', 'EMERGENCY_PENDING', 'ACCEPTED', 'IN_PROGRESS']
    ).select_related('customer', 'worker__user', 'service')[:60]

    map_bookings = []
    for b in active_bookings_qs:
        coords = None
        if b.district and b.district in DISTRICT_COORDINATES:
            coords = DISTRICT_COORDINATES[b.district]
        elif b.worker and b.worker.district in DISTRICT_COORDINATES:
            coords = DISTRICT_COORDINATES[b.worker.district]

        if coords:
            j_lat = ((b.id * 13) % 20 - 10) * 0.006
            j_lng = ((b.id * 29) % 20 - 10) * 0.006
            map_bookings.append({
                'id': b.id,
                'service': b.service.name,
                'customer': b.customer.get_full_name() or b.customer.username,
                'worker': (b.worker.user.get_full_name() or b.worker.user.username) if b.worker else 'Unassigned',
                'status': b.get_status_display(),
                'status_code': b.status,
                'district': b.district or (b.worker.district if b.worker else ''),
                'lat': round(coords[0] + j_lat, 5),
                'lng': round(coords[1] + j_lng, 5),
                'is_emergency': b.status == 'EMERGENCY_PENDING' or b.booking_type == 'Emergency',
            })

    workers_active_qs = WorkerProfile.objects.filter(
        work_status__in=['AVAILABLE', 'ON_DUTY']
    ).select_related('user', 'service')[:70]

    map_workers = []
    for w in workers_active_qs:
        coords = None
        if w.latitude and w.longitude:
            coords = (float(w.latitude), float(w.longitude))
        elif w.district and w.district in DISTRICT_COORDINATES:
            base = DISTRICT_COORDINATES[w.district]
            j_lat = ((w.id * 17) % 20 - 10) * 0.007
            j_lng = ((w.id * 37) % 20 - 10) * 0.007
            coords = (base[0] + j_lat, base[1] + j_lng)

        if coords:
            map_workers.append({
                'id': w.id,
                'name': w.user.get_full_name() or w.user.username,
                'service': w.service.name if w.service else (w.custom_service or 'Service Pro'),
                'district': w.district,
                'status': w.get_work_status_display(),
                'status_code': w.work_status,
                'strikes': w.strikes_count,
                'lat': round(coords[0], 5),
                'lng': round(coords[1], 5),
            })

    district_operations = []
    for dist_name, coords in DISTRICT_COORDINATES.items():
        w_cnt = sum(1 for w in map_workers if w['district'] == dist_name)
        b_cnt = sum(1 for b in map_bookings if b['district'] == dist_name)
        district_operations.append({
            'district': dist_name,
            'lat': coords[0],
            'lng': coords[1],
            'workers': w_cnt,
            'bookings': b_cnt,
        })

    live_map_data = {
        'bookings': map_bookings,
        'workers': map_workers,
        'districts': district_operations,
    }
    live_map_json = json.dumps(live_map_data)

    latest_broadcast = Notification.objects.filter(
        notification_type='SYSTEM',
        message__startswith='📢'
    ).order_by('-created_at').first()

    return render(
        request,
        'dashboards/admin_dashboard.html',
        {
            'services': services,
            'users': users,
            'workers': workers,
            'total_bookings': total_bookings,
            'pending_bookings': pending_bookings,
            'completed_bookings': completed_bookings,
            'active_bookings': active_bookings,
            'total_complaints': total_complaints,
            'open_complaints': open_complaints,
            'total_revenue': total_revenue,
            'gross_revenue': gross_revenue,
            'commission_rate': commission_rate,
            'platform_commission': platform_commission,
            'worker_payouts': worker_payouts,
            'commission_collected': commission_collected,
            'monthly_revenue': monthly_revenue,
            'top_services_data': top_services_data,
            'district_revenue_data': district_revenue_data,
            'live_map_json': live_map_json,
            # Rendered with |json_script (escapes <, >, &) - never |safe.
            'live_map_data': live_map_data,
            'map_bookings_count': len(map_bookings),
            'map_workers_count': len(map_workers),
            'avg_rating': round(avg_rating, 1),
            'recent_bookings': recent_bookings,
            'booking_status_data': booking_status_data,
            'top_workers': top_workers,
            'new_enquiries': new_enquiries,
            'latest_broadcast': latest_broadcast,
            'total_worker_dues': total_worker_dues,
            'paused_workers_count': paused_workers_count,
            'active_launch_workers_count': active_launch_workers_count,
            'cash_bookings_count': cash_bookings_count,
            'online_bookings_count': online_bookings_count,
        }
    )


@never_cache
def worker_dashboard(request):

    if not request.user.is_authenticated:
        return redirect('login')

    if not request.user.is_active:
        logout(request)
        messages.error(request, 'Your account has been deactivated. Please contact support.')
        return redirect('login')

    if request.user.role != 'WORKER':
        return redirect('dashboard')

    try:
        worker_profile = request.user.worker_profile
    except WorkerProfile.DoesNotExist:
        messages.error(request, 'Worker profile not found.')
        return redirect('dashboard')

    if getattr(worker_profile, 'work_status', '') == 'SUSPENDED':
        logout(request)
        messages.error(request, 'Your worker account has been suspended. Please contact support.')
        return redirect('login')

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'update_location':
            latitude = request.POST.get('latitude', '').strip()
            longitude = request.POST.get('longitude', '').strip()
            if not latitude or not longitude:
                messages.error(request, 'Unable to get your location. Please try again.')
                return redirect('worker_dashboard')
            try:
                worker_profile.latitude = float(latitude)
                worker_profile.longitude = float(longitude)
                place_name = _get_place_name_from_coords(
                    worker_profile.latitude,
                    worker_profile.longitude
                )
                if place_name:
                    worker_profile.current_place = place_name
                worker_profile.save(
                    update_fields=[
                        'latitude',
                        'longitude',
                        'current_place',
                        'updated_at'
                    ]
                )
                success_msg = 'Current location updated successfully.'
                if place_name:
                    success_msg = f'Location updated: {place_name}'
                messages.success(request, success_msg)
            except (ValueError, TypeError):
                messages.error(request, 'Invalid location data. Please try again.')
            return redirect('worker_dashboard')

    # Automatically expire any pending bookings past 30-min window
    check_and_expire_pending_bookings(worker=worker_profile)

    job_stats = Booking.objects.filter(worker=worker_profile).aggregate(
        total=Count('id'),
        pending=Count('id', filter=Q(status='PENDING')),
        completed=Count('id', filter=Q(status='COMPLETED')),
        in_progress=Count('id', filter=Q(status='IN_PROGRESS')),
        cancelled=Count('id', filter=Q(status='CANCELLED')),
        total_earnings=Sum('estimated_cost', filter=Q(status='COMPLETED', is_paid=True)),
    )
    total_jobs = job_stats['total'] or 0
    pending_jobs_count = job_stats['pending'] or 0
    completed_bookings = job_stats['completed'] or 0
    in_progress_bookings = job_stats['in_progress'] or 0
    cancelled_bookings = job_stats['cancelled'] or 0
    total_earnings = job_stats['total_earnings'] or 0

    pending_bookings = Booking.objects.filter(
        worker=worker_profile,
        status='PENDING'
    ).filter(
        Q(series__isnull=True) | Q(series__is_active=True)
    ).select_related('customer', 'service').order_by('-created_at')[:5]

    active_bookings = Booking.objects.filter(
        worker=worker_profile,
        status__in=['ACCEPTED', 'IN_PROGRESS']
    ).select_related('customer', 'service').order_by('-created_at')[:5]

    chat_bookings = Booking.objects.filter(
        worker=worker_profile
    ).exclude(
        status='CANCELLED'
    ).select_related(
        'customer', 'service'
    ).prefetch_related(
        Prefetch(
            'messages',
            queryset=BookingMessage.objects.select_related('sender').order_by('-created_at'),
            to_attr='chat_messages',
        )
    ).order_by('-updated_at')[:6]

    avg_rating = Review.objects.filter(
        worker=worker_profile
    ).aggregate(avg=Avg('rating'))['avg'] or 0
    recent_reviews = Review.objects.filter(
        worker=worker_profile
    ).select_related(
        'customer'
    ).order_by('-created_at')[:3]

    notifications = Notification.objects.filter(
        recipient=request.user,
        is_read=False
    ).order_by('-created_at')[:5]

    # Get emergency bookings that are pending (no worker assigned yet).
    # Finding C4: Filter to worker's own trade and district, and block if wallet is locked.
    emergency_bookings = []
    wallet, _ = WorkerWallet.objects.get_or_create(worker=worker_profile)
    if worker_profile.is_verified and worker_profile.work_status in ['AVAILABLE', 'ON_DUTY'] and request.user.is_active and worker_profile.service_id:
        if not wallet.is_locked:
            dismissed_booking_ids = Notification.objects.filter(
                recipient=request.user,
                notification_type='EMERGENCY',
                is_read=True
            ).values_list('booking_id', flat=True)

            em_qs = Booking.objects.filter(
                status='EMERGENCY_PENDING',
                worker__isnull=True,
                service=worker_profile.service,
            ).exclude(id__in=dismissed_booking_ids)

            # Finding C4: Only broadcast within worker's registered district
            if worker_profile.district:
                em_qs = em_qs.filter(district__iexact=worker_profile.district.strip())

            emergency_bookings = em_qs.select_related('customer', 'service').order_by('-created_at')[:10]

    emergency_reason = None
    if not emergency_bookings:
        if wallet.is_locked:
            emergency_reason = "Wallet balance is low. Please recharge your wallet to receive emergency jobs."
        else:
            emergency_reason = _emergency_unavailable_reason(worker_profile, request.user)

    recent_wallet_transactions = wallet.transactions.select_related('booking').order_by('-created_at')[:8]

    context = {
        'worker_profile': worker_profile,
        'wallet': wallet,
        'wallet_balance': wallet.balance,
        'wallet_zone': wallet.zone,
        'wallet_is_locked': wallet.is_locked,
        'wallet_amount_due': wallet.amount_due,
        'wallet_buffer_remaining': wallet.buffer_remaining,
        'is_in_launch': worker_profile.is_in_launch,
        'launch_days_left': worker_profile.launch_days_left,
        'launch_ends_at': worker_profile.launch_ends_at,
        'is_paused_for_dues': worker_profile.is_paused_for_dues,
        'recent_wallet_transactions': recent_wallet_transactions,
        'pending_bookings': pending_bookings,
        'active_bookings': active_bookings,
        'chat_bookings': chat_bookings,
        'completed_bookings': completed_bookings,
        'completed_jobs': completed_bookings,
        'in_progress_jobs': in_progress_bookings,
        'cancelled_jobs': cancelled_bookings,
        'total_jobs': total_jobs,
        'pending_jobs': pending_jobs_count,
        'total_earnings': total_earnings,
        'avg_rating': round(avg_rating, 1),
        'recent_reviews': recent_reviews,
        'notifications': notifications,
        'emergency_bookings': emergency_bookings,
        'emergency_reason': emergency_reason,
        'vapid_public_key': getattr(settings, 'VAPID_PUBLIC_KEY', ''),
        'has_push_subscription': PushSubscription.objects.filter(user=request.user).exists(),
    }

    return render(
        request,
        'dashboards/worker_dashboard.html',
        context
    )


def _release_rejected_utr(tx):
    """Free the UTR held by a REJECTED recharge so the real payer can use it.

    The reference is moved into the audit notes (nothing is deleted) and the
    unique column is cleared. Pending and credited rows keep their reference, so
    a UTR that is awaiting verification or already credited can never be
    submitted - or credited - a second time.
    """
    if tx.reference_id:
        tx.notes = f"{tx.notes} | UTR {tx.reference_id} released after rejection".strip(' |')[:2000]
        tx.reference_id = None
        tx.save(update_fields=['notes', 'reference_id'])


@login_required
@require_POST
def worker_recharge_wallet(request):
    """Allows a worker to top up their wallet or pay off outstanding platform dues."""
    if request.user.role != 'WORKER':
        messages.error(request, 'Only service professionals can recharge a worker wallet.')
        return redirect('dashboard')

    try:
        worker_profile = request.user.worker_profile
    except WorkerProfile.DoesNotExist:
        messages.error(request, 'Worker profile not found.')
        return redirect('dashboard')

    # Decimal only; NaN, Infinity, negatives, zero, malformed text, more than two
    # decimal places and amounts above the ledger column limit are rejected
    # before anything can reach a wallet calculation.
    amount, amount_error = _strict_money(request.POST.get('amount', ''), 'Recharge amount')
    if amount_error:
        messages.error(request, amount_error)
        return redirect('worker_dashboard')
    if amount < Decimal('10.00'):
        messages.error(request, 'Minimum recharge / payment amount is ₹10.00.')
        return redirect('worker_dashboard')

    reference_id = request.POST.get('reference_id', '').strip().upper()[:100]
    if not reference_id or len(reference_id) < 6:
        messages.error(request, 'Please provide a valid 12-digit UPI UTR or Transaction Reference number.')
        return redirect('worker_dashboard')

    try:
        with transaction.atomic():
            existing_ref = WalletTransaction.objects.select_for_update().filter(reference_id=reference_id).first()
            if existing_ref is not None:
                if existing_ref.transaction_type == 'RECHARGE_REJECTED':
                    # A rejected claim must not reserve the UTR forever (older
                    # rejected rows still hold it): release it, keep the audit trail.
                    _release_rejected_utr(existing_ref)
                else:
                    messages.error(request, 'This transaction reference ID has already been registered.')
                    return redirect('worker_dashboard')

            wallet, _ = WorkerWallet.objects.select_for_update().get_or_create(worker=worker_profile)
            current_balance = Decimal(str(wallet.balance if wallet.balance is not None else '0.00'))

            # Security fix: Do NOT credit wallet balance or unpause dues immediately upon worker self-submission!
            # Create a pending approval transaction record with amount=0 and store requested amount in notes/reference.
            # Wallet balance will ONLY be credited when an administrator verifies the UTR against actual bank records.
            WalletTransaction.objects.create(
                wallet=wallet,
                transaction_type='RECHARGE_PENDING',
                amount=amount,
                balance_after=current_balance,
                reference_id=reference_id,
                notes=f"Recharge of ₹{amount:.2f} submitted with UTR {reference_id} (Status: PENDING_APPROVAL)"
            )
    except IntegrityError:
        messages.error(request, 'This transaction reference ID has already been registered.')
        return redirect('worker_dashboard')

    # Alert admins to reconcile UPI recharge transaction
    for admin in CustomUser.objects.filter(role='ADMIN', is_active=True):
        Notification.objects.create(
            recipient=admin,
            notification_type='PAYMENT',
            message=f"💳 Worker Wallet Recharge Request: {worker_profile.user.username} submitted ₹{amount:.2f} (UTR: {reference_id}) awaiting bank verification."
        )

    messages.info(
        request,
        f'Recharge request of ₹{amount:.2f} with UTR {reference_id} submitted successfully! '
        f'Our administration team is verifying the payment against bank records. Your wallet will be credited upon approval.'
    )
    return redirect('worker_dashboard')


@never_cache
def user_dashboard(request):

    if not request.user.is_authenticated:
        return redirect('login')

    if request.user.role != 'USER':
        return redirect('dashboard')

    try:
        profile = request.user.user_profile
    except UserProfile.DoesNotExist:
        profile = UserProfile.objects.create(user=request.user)

    bookings = Booking.objects.filter(
        customer=request.user
    ).select_related('worker', 'worker__user', 'service').order_by('-created_at')[:5]

    # Get emergency bookings for this user
    emergency_bookings = Booking.objects.filter(
        customer=request.user,
        status='EMERGENCY_PENDING',
        booking_type='Emergency'
    ).select_related('service').order_by('-created_at')[:5]

    total_bookings = Booking.objects.filter(customer=request.user).count()
    completed_bookings = Booking.objects.filter(customer=request.user, status='COMPLETED').count()
    pending_bookings = Booking.objects.filter(
        customer=request.user,
        status__in=['PENDING', 'ACCEPTED', 'IN_PROGRESS', 'COMPLETION_REQUESTED', 'EMERGENCY_PENDING']
    ).count()

    notifications = Notification.objects.filter(
        recipient=request.user,
        is_read=False
    ).order_by('-created_at')[:5]

    active_booking = Booking.objects.filter(
        customer=request.user,
        status__in=['PENDING', 'ACCEPTED', 'IN_PROGRESS', 'COMPLETION_REQUESTED']
    ).select_related('worker', 'worker__user', 'service').order_by('-created_at').first()

    context = {
        'profile': profile,
        'bookings': bookings,
        'emergency_bookings': emergency_bookings,
        'total_bookings': total_bookings,
        'completed_bookings': completed_bookings,
        'pending_bookings': pending_bookings,
        'notifications': notifications,
        'active_booking': active_booking,
    }

    return render(
        request,
        'dashboards/user_dashboard.html',
        context
    )


@never_cache
@login_required
def change_password_view(request):
    """Let an authenticated administrator change their password.
    Password management is only enabled for ADMIN accounts; customers and workers log in via secure OTP.
    """
    # Admins, and workers who registered with a password, can change it.
    # OTP-only accounts (no usable password) keep signing in with codes.
    can_manage_password = request.user.role == 'ADMIN' or (
        request.user.role == 'WORKER'
        and bool(request.user.password)
        and request.user.has_usable_password()
    )
    if not can_manage_password:
        messages.info(request, 'Your account uses secure OTP authentication. Password management is only required for platform administrators.')
        return redirect('dashboard')

    context = {}

    if request.method == 'POST':
        current_password = request.POST.get('current_password', '')
        new_password = request.POST.get('new_password', '').strip()
        confirm_password = request.POST.get('confirm_password', '').strip()

        user = request.user

        if not current_password:
            messages.error(request, 'Please enter your current password.')
        elif not user.check_password(current_password):
            messages.error(request, 'Your current password is incorrect.')
        elif not new_password:
            messages.error(request, 'Please enter a new password.')
        elif new_password != confirm_password:
            messages.error(request, 'Passwords do not match.')
        elif len(new_password) < 8:
            messages.error(request, 'Password must be at least 8 characters.')
        elif user.check_password(new_password):
            messages.error(
                request,
                'Your new password must be different from your current password.'
            )
        else:
            user.set_password(new_password)
            user.password_reset_token = None
            user.password_reset_token_hash = None
            user.password_reset_expires = None
            user.save(update_fields=[
                'password',
                'password_reset_token',
                'password_reset_token_hash',
                'password_reset_expires',
            ])

            # Keeps the current session valid but rotates the session key, so
            # the old session hash can no longer be replayed.
            update_session_auth_hash(request, user)

            messages.success(request, 'Your password has been changed successfully.')
            return redirect('dashboard')

    return render(request, 'accounts/change_password.html', context)


# ---------------------------------------------------------------------------
# Phone-number change with real OTP verification of the NEW number
# ---------------------------------------------------------------------------
# A separate, session-bound flow (it never touches the login / registration OTP
# state). The pending change lives only in the server-side session:
#   user_id, role, new_phone, code_hash (SHA-256), created_at, attempts.
# The phone is written ONLY after the code for exactly that number, issued to
# exactly this signed-in user, is verified once.
PHONE_CHANGE_SESSION_KEY = 'phone_change_pending'


def _phone_change_keys(user_id, new_phone):
    digest = hashlib.sha256(f'{user_id}:{new_phone}'.encode('utf-8')).hexdigest()
    return (
        f'ratelimit:phonechange:send:{user_id}',
        f'ratelimit:phonechange:verify:{digest}',
    )


def _phone_change_profile(user):
    """The UserProfile / WorkerProfile that holds this user's phone, or None."""
    if user.role == 'WORKER':
        return WorkerProfile.objects.filter(user=user).first()
    if user.role == 'USER':
        return UserProfile.objects.filter(user=user).first()
    return None


def begin_phone_change(request, user, new_phone):
    """Validate ``new_phone`` and send an OTP to it. Returns (ok, message).

    Nothing is written to the profile here. Re-submitting a different number
    replaces the previous pending change (the old code stops working).
    """
    new_phone = _normalize_phone(new_phone)
    if not new_phone:
        return False, 'Please enter a valid 10-digit Indian mobile number.'
    if _user_ids_for_phone(new_phone) - {user.id}:
        return False, 'This phone number is already registered with another account.'
    if not sms_delivery_available():
        return False, 'Mobile verification is temporarily unavailable, so the phone number was not changed.'
    send_key, _verify_key = _phone_change_keys(user.id, new_phone)
    allowed, wait = register_attempt_within_window(
        send_key, int(getattr(settings, 'PHONE_CHANGE_MAX_SENDS_PER_HOUR', 3)), 60 * 60
    )
    if not allowed:
        return False, f'Too many codes requested. Please try again in {max(1, wait)} minute(s).'
    ok, error = _otp_send_allowed(request, new_phone)
    if not ok:
        return False, error
    otp = ''.join(secrets.choice(string.digits) for _ in range(6))
    if not send_otp_sms(new_phone, otp):
        return False, 'We could not send a code to that number. Please try again.'
    request.session[PHONE_CHANGE_SESSION_KEY] = {
        'user_id': user.id,
        'role': user.role,
        'new_phone': new_phone,
        'code_hash': hashlib.sha256(otp.encode('utf-8')).hexdigest(),
        'created_at': timezone.now().isoformat(),
        'attempts': 0,
    }
    request.session.modified = True
    return True, f'We sent a 6-digit code to {new_phone}.'


def _mask_phone(phone):
    return f'{phone[:2]}******{phone[-2:]}' if phone and len(phone) >= 4 else ''


@login_required
def phone_change_verify(request):
    """Verify the OTP sent to the new number and only then update the phone."""
    back_url = 'edit_worker_profile' if request.user.role == 'WORKER' else 'edit_profile'
    pending = request.session.get(PHONE_CHANGE_SESSION_KEY)
    if not pending or pending.get('user_id') != request.user.id:
        # Another user's (or no) pending change can never be completed here.
        if pending:
            request.session.pop(PHONE_CHANGE_SESSION_KEY, None)
        messages.info(request, 'There is no phone number change waiting for verification.')
        return redirect(back_url)

    new_phone = pending.get('new_phone', '')
    context = {'masked_phone': _mask_phone(new_phone), 'back_url': back_url}

    if request.method != 'POST':
        return render(request, 'accounts/verify_phone_change.html', context)

    action = request.POST.get('action', 'verify')
    if action == 'cancel':
        request.session.pop(PHONE_CHANGE_SESSION_KEY, None)
        messages.info(request, 'Phone number change cancelled.')
        return redirect(back_url)

    if action == 'resend':
        ok, message = begin_phone_change(request, request.user, new_phone)
        (messages.success if ok else messages.error)(request, message)
        return redirect('phone_change_verify')

    max_attempts = int(getattr(settings, 'PHONE_CHANGE_MAX_ATTEMPTS', 5))
    _send_key, verify_key = _phone_change_keys(request.user.id, new_phone)
    if is_rate_limited(verify_key, max_attempts):
        request.session.pop(PHONE_CHANGE_SESSION_KEY, None)
        messages.error(request, 'Too many incorrect codes. Please start the phone number change again later.')
        return redirect(back_url)

    # Expiry
    try:
        created = datetime.fromisoformat(pending.get('created_at'))
    except (TypeError, ValueError):
        created = None
    ttl = int(getattr(settings, 'PHONE_CHANGE_OTP_TTL_SECONDS', 600))
    if not created or timezone.now() > created + timedelta(seconds=ttl):
        request.session.pop(PHONE_CHANGE_SESSION_KEY, None)
        messages.error(request, 'The code has expired. Please request a new phone number change.')
        return redirect(back_url)

    code = (request.POST.get('otp') or '').strip()
    if len(code) != 6 or not code.isdigit():
        messages.error(request, 'Please enter the 6-digit code.')
        return render(request, 'accounts/verify_phone_change.html', context)

    submitted_hash = hashlib.sha256(code.encode('utf-8')).hexdigest()
    if not (pending.get('code_hash') and secrets.compare_digest(pending['code_hash'], submitted_hash)):
        register_failure(verify_key, max_attempts, 15 * 60)
        pending['attempts'] = int(pending.get('attempts', 0)) + 1
        request.session[PHONE_CHANGE_SESSION_KEY] = pending
        request.session.modified = True
        messages.error(request, 'Incorrect code. Please try again.')
        return render(request, 'accounts/verify_phone_change.html', context)

    # Valid code: consume it first so it can never be replayed, then apply.
    request.session.pop(PHONE_CHANGE_SESSION_KEY, None)
    clear(verify_key)

    from django.core.cache import cache
    lock_key = f'phonechange:lock:{new_phone}'
    try:
        lock_acquired = cache.add(lock_key, request.user.id, 30)
    except Exception:
        lock_acquired = False
    if not lock_acquired:
        messages.error(request, 'That number is being updated right now. Please try again.')
        return redirect(back_url)
    try:
        with transaction.atomic():
            CustomUser.objects.select_for_update().get(pk=request.user.pk)
            profile = _phone_change_profile(request.user)
            if profile is None:
                messages.error(request, 'Profile not found.')
                return redirect(back_url)
            # Re-check uniqueness at the moment of writing (it may have been
            # claimed since the code was sent).
            if _user_ids_for_phone(new_phone) - {request.user.id}:
                messages.error(request, 'This phone number is already registered with another account.')
                return redirect(back_url)
            profile.phone = new_phone
            profile.save(update_fields=['phone'])
    finally:
        cache.delete(lock_key)
    messages.success(request, 'Your mobile number has been verified and updated.')
    return redirect('worker_profile' if request.user.role == 'WORKER' else 'edit_profile')


@login_required
def edit_profile_view(request):
    if request.user.role == 'WORKER':
        return redirect('edit_worker_profile')

    try:
        profile = request.user.user_profile
    except UserProfile.DoesNotExist:
        profile = UserProfile.objects.create(user=request.user)

    if request.method == 'POST':
        # Check if this is an AJAX avatar upload
        is_ajax_avatar = (
            request.headers.get('x-requested-with') == 'XMLHttpRequest' or
            request.POST.get('ajax_avatar') == 'true'
        )
        if is_ajax_avatar and 'profile_image' in request.FILES:
            # Validate extension, MIME type, magic bytes, size and a full
            # image decode before anything is stored (same rules as the form).
            from .upload_validation import validate_profile_photo
            from django.core.exceptions import ValidationError as _UploadError
            try:
                avatar_file = validate_profile_photo(request.FILES['profile_image'])
            except _UploadError as exc:
                return JsonResponse(
                    {'status': 'error', 'message': ' '.join(exc.messages)},
                    status=400,
                )
            profile.profile_image = avatar_file
            profile.save(update_fields=['profile_image'])
            return JsonResponse({
                'status': 'success',
                'message': 'Profile photo updated successfully.',
                'image_url': profile.profile_image.url if profile.profile_image else ''
            })

        original_phone = profile.phone
        form = UserProfileForm(request.POST, request.FILES, instance=profile)
        if form.is_valid():
            # A changed mobile number is NOT saved here: ownership of the new
            # number must first be proven with an OTP sent to it (see
            # begin_phone_change / phone_change_verify). Everything else saves now.
            submitted_phone = form.cleaned_data.get('phone')
            phone_change_requested = bool(submitted_phone) and submitted_phone != (original_phone or '')
            if phone_change_requested:
                form.instance.phone = original_phone

            form.save()

            if phone_change_requested:
                sent, info = begin_phone_change(request, request.user, submitted_phone)
                if sent:
                    messages.success(request, 'Profile updated. ' + info + ' Enter it to confirm your new number.')
                    return redirect('phone_change_verify')
                messages.error(request, 'Your other changes were saved, but the mobile number was not changed. ' + info)
                return redirect('edit_profile')

            messages.success(request, 'Profile updated successfully.')
            return redirect('edit_profile')
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = UserProfileForm(instance=profile)

    return render(request, 'user/edit_profile.html', {'form': form, 'profile': profile})


@login_required
def edit_worker_profile_view(request):
    if request.user.role != 'WORKER':
        return redirect('dashboard')

    try:
        profile = request.user.worker_profile
    except WorkerProfile.DoesNotExist:
        messages.error(request, 'Worker profile not found.')
        return redirect('dashboard')

    if request.method == 'POST':
        original_phone = profile.phone
        form = WorkerProfileForm(request.POST, request.FILES, instance=profile)
        if form.is_valid():
            # A changed mobile number is not saved until the OTP sent to the new
            # number is verified (see begin_phone_change / phone_change_verify).
            submitted_phone = form.cleaned_data.get('phone')
            phone_change_requested = bool(submitted_phone) and submitted_phone != (original_phone or '')

            original_service = profile.service
            original_district = profile.district

            profile = form.save(commit=False)
            if phone_change_requested:
                profile.phone = original_phone

            # Defensive server-side guard: if worker is verified, ignore any forged tampering of service/district
            if profile.is_verified:
                profile.service = original_service
                profile.district = original_district

            if profile.latitude is not None and profile.longitude is not None:
                profile.current_place = _get_place_name_from_coords(
                    profile.latitude,
                    profile.longitude,
                )

            profile.save()
            if phone_change_requested:
                sent, info = begin_phone_change(request, request.user, submitted_phone)
                if sent:
                    messages.success(request, 'Profile updated. ' + info + ' Enter it to confirm your new number.')
                    return redirect('phone_change_verify')
                messages.error(request, 'Your other changes were saved, but the mobile number was not changed. ' + info)
                return redirect('edit_worker_profile')
            messages.success(request, 'Profile updated successfully.')
            return redirect('worker_profile')
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = WorkerProfileForm(instance=profile)

    return render(request, 'workers/edit_worker_profile.html', {'form': form, 'profile': profile})


@login_required
def proof_download(request, pk):
    """Admin-only authenticated download of a worker proof document.

    Proof documents are private verification records: customers, other
    workers and anonymous visitors must never receive them.  Only the
    ADMIN role is allowed.  The stored filename is a random UUID (no
    client-supplied path segments), and the response streams the file
    the profile row points at - there is no user-controlled path input,
    so IDOR/path traversal/arbitrary-file download are not possible.
    """
    if request.user.role != 'ADMIN':
        return HttpResponseForbidden('Access denied.')

    worker_profile = get_object_or_404(
        WorkerProfile.objects.select_related('user'),
        pk=pk
    )

    proof = worker_profile.proof_document
    if not proof or not proof.name:
        raise Http404('Proof document not found.')

    # File object comes from the model field only (never from the request).
    # Storage-agnostic open(): proofs always route to local disk (the
    # worker_proofs/ prefix is never a Cloudinary prefix), but open() keeps
    # this endpoint correct even if the default backend changes.
    try:
        file_handle = proof.open('rb')
    except FileNotFoundError:
        raise Http404('Proof document file not found.') from None
    except NotImplementedError:
        # A Cloudinary-backed name has no local path - and a proof must
        # never be Cloudinary-backed. Treat it as missing, never redirect.
        raise Http404('Proof document file not found.') from None

    response = FileResponse(
        file_handle,
        content_type='application/octet-stream',
    )
    response['Content-Disposition'] = 'attachment; filename="{}"'.format(
        os.path.basename(proof.name)
    )
    return response


@login_required
def my_complaints_view(request):
    complaints = Complaint.objects.filter(
        customer=request.user
    ).select_related('booking', 'booking__service').order_by('-created_at')
    return render(request, 'user/my_complaints.html', {'complaints': complaints})


def _cancel_single_booking_internal(booking, reason, actor='CUSTOMER'):
    """
    Cancels a single booking executing canonical cancellation side effects:
    1. Booking status update -> 'CANCELLED' with cancellation_reason and cancelled_by.
    2. Full financial refund execution via _handle_booking_cancellation_refund().
    3. Idempotent notification to assigned worker (if present).
    4. Safe cancellation email notification to assigned worker (if present).
    5. In-app notification to all active system administrators.
    """
    booking.status = 'CANCELLED'
    booking.cancellation_reason = reason
    booking.cancelled_by = actor
    booking.save(update_fields=[
        'status', 'cancellation_reason', 'cancelled_by', 'updated_at'
    ])

    # 1. Process customer refund. Customer-side closure is not the worker's
    # fault, so any commission the worker prepaid is returned.
    _handle_booking_cancellation_refund(
        booking, commission_refund=True,
        refund_reason=f'Customer booking closed ({actor})',
    )

    # 2 & 3. Worker notification and email (if worker was assigned)
    if booking.worker:
        worker_user = booking.worker.user
        service_name = booking.service.name if booking.service else "Service"
        _create_idempotent_notification(
            recipient=worker_user,
            booking=booking,
            notification_type='BOOKING_UPDATE',
            message=f"Booking #{booking.id} ({service_name}) scheduled for {booking.date or 'requested time'} was cancelled ({reason})."
        )
        if worker_user.email:
            send_email_safely(
                subject=f"Booking #{booking.id} Cancelled",
                message=(
                    f"Hi {worker_user.username},\n\n"
                    f"Booking #{booking.id} for {service_name} on {booking.date or 'requested date'} "
                    f"has been cancelled.\n"
                    f"Reason: {reason}\n\n"
                    f"Best regards,\nTeam LAMO"
                ),
                recipient_list=[worker_user.email],
                purpose='booking_cancelled',
                async_send=True
            )

    # 4. Notify active administrators
    for admin in CustomUser.objects.filter(role='ADMIN', is_active=True):
        _create_idempotent_notification(
            recipient=admin,
            booking=booking,
            notification_type='SYSTEM',
            message=f"Booking #{booking.id} cancelled due to customer account deletion ({actor})."
        )


def _safe_delete_avatar_file(avatar_field):
    """Safely deletes avatar file from disk or Cloudinary post-commit."""
    try:
        if avatar_field and hasattr(avatar_field, 'delete'):
            avatar_field.delete(save=False)
    except Exception as exc:
        logger.warning("Error deleting avatar file during user anonymization: %s", exc)


def _anonymize_and_soft_delete_customer(user, actor='CUSTOMER'):
    """
    Anonymizes and soft-deletes a customer account (role='USER') while strictly
    preserving historical records (completed bookings, payments, reviews, wallet transactions).

    Returns:
        (success: bool, error_message: str or None)
    """
    if user.role != 'USER':
        return False, "Only customer accounts can be anonymized with this procedure."

    today = timezone.now().date()

    with transaction.atomic():
        # Lock user row first to prevent concurrent modifications
        user = CustomUser.objects.select_for_update().get(pk=user.pk)

        # 1. Check blocking active/disputed/in-progress bookings
        blocking_active_bookings = Booking.objects.select_for_update().filter(
            customer=user,
            status__in=['IN_PROGRESS', 'COMPLETION_REQUESTED', 'DISPUTED']
        ).exists()
        if blocking_active_bookings:
            return False, "Cannot delete account while you have in-progress, completion-requested, or disputed bookings."

        # 2. Check standalone accepted bookings (which must not be cancelled without explicit customer intervention)
        blocking_standalone_accepted = Booking.objects.select_for_update().filter(
            customer=user,
            series__isnull=True,
            status='ACCEPTED'
        ).exists()
        if blocking_standalone_accepted:
            return False, "Cannot delete account while you have confirmed upcoming bookings. Please cancel your bookings first."

        # 3. Lock and cancel eligible standalone bookings (PENDING, EMERGENCY_PENDING)
        standalone_cancelable = list(
            Booking.objects.select_for_update().filter(
                customer=user,
                series__isnull=True,
                status__in=['PENDING', 'EMERGENCY_PENDING']
            ).select_related('worker', 'worker__user', 'service')
        )
        for booking in standalone_cancelable:
            _cancel_single_booking_internal(
                booking,
                reason="Customer account closed",
                actor=actor
            )

        # 4. Deactivate recurring series and cancel eligible future child bookings
        customer_series = list(BookingSeries.objects.select_for_update().filter(customer=user))
        for series in customer_series:
            series.is_active = False
            series.save(update_fields=['is_active', 'updated_at'])

            future_child_bookings = list(
                Booking.objects.select_for_update().filter(
                    series=series,
                    date__gte=today,
                    status__in=['PENDING', 'ACCEPTED', 'EMERGENCY_PENDING']
                ).select_related('worker', 'worker__user', 'service')
            )
            for booking in future_child_bookings:
                _cancel_single_booking_internal(
                    booking,
                    reason="Customer recurring series terminated due to account closure",
                    actor=actor
                )

        # 5. Purge Web Push Subscriptions
        PushSubscription.objects.filter(user=user).delete()

        # 6. Scrub UserProfile PII
        avatar_to_delete = None
        try:
            profile = UserProfile.objects.select_for_update().get(user=user)
            if profile.profile_image:
                avatar_to_delete = profile.profile_image
                profile.profile_image = None
            profile.phone = ''
            profile.address = ''
            profile.latitude = None
            profile.longitude = None
            profile.save(update_fields=['profile_image', 'phone', 'address', 'latitude', 'longitude'])
        except UserProfile.DoesNotExist:
            profile = None

        # Schedule avatar file cleanup strictly on transaction commit
        if avatar_to_delete:
            transaction.on_commit(lambda: _safe_delete_avatar_file(avatar_to_delete))

        # 7. Anonymize CustomUser
        user_id = user.id
        token = secrets.token_hex(4)
        user.is_active = False
        user.set_unusable_password()
        user.username = f"deleted_user_{user_id}_{token}"
        user.email = f"deleted_{user_id}_{token}@anonymized.local"
        user.first_name = "Deleted"
        user.last_name = "User"
        user.email_verification_otp = None
        user.password_reset_token = None
        user.password_reset_token_hash = None
        user.password_reset_expires = None
        user.otp_failed_attempts = 0
        user.otp_locked_until = None
        user.save(update_fields=[
            'is_active', 'password', 'username', 'email', 'first_name', 'last_name',
            'email_verification_otp', 'password_reset_token', 'password_reset_token_hash',
            'password_reset_expires', 'otp_failed_attempts', 'otp_locked_until'
        ])

    # 8. Invalidate active sessions across all devices
    from django.contrib.sessions.models import Session
    try:
        user_id_str = str(user_id)
        for s in Session.objects.filter(expire_date__gte=timezone.now()):
            try:
                data = s.get_decoded()
                if str(data.get('_auth_user_id')) == user_id_str:
                    s.delete()
            except Exception:
                pass
    except Exception as exc:
        logger.warning("Session cleanup failed during customer deletion: %s", exc)

    return True, None


@login_required
def delete_account_view(request):

    if request.method != 'POST':
        return redirect('edit_profile')

    user = request.user
    if user.role != 'USER':
        messages.error(request, 'Only customer accounts can be deleted via this option.')
        return redirect('edit_profile')

    # Confirmation validation
    if user.has_usable_password():
        # Account with password configured (e.g. legacy/admin-created): verify password
        password = request.POST.get('password', '').strip()
        if not password:
            messages.error(request, 'Please enter your password to confirm account deletion.')
            return redirect('edit_profile')
        if not user.check_password(password):
            messages.error(request, 'Incorrect password. Account deletion cancelled.')
            return redirect('edit_profile')
    else:
        # Passwordless OTP customer: confirm via typed confirmation or explicit confirmation token
        confirm_val = request.POST.get('confirm_delete', '').strip() or request.POST.get('password', '').strip()
        if confirm_val.upper() not in ('DELETE', 'CONFIRM', 'YES'):
            messages.error(request, 'Please confirm account deletion by typing DELETE.')
            return redirect('edit_profile')
        # Step-up check: with no password to re-enter, require a recent
        # (OTP) sign-in so an unattended, already-open session cannot
        # permanently delete the account.
        recent_window = timedelta(minutes=30)
        if not user.last_login or timezone.now() - user.last_login > recent_window:
            messages.error(
                request,
                'For your security, please sign out and sign in again with your verification code, '
                'then delete your account within 30 minutes.'
            )
            return redirect('edit_profile')

    success, err_msg = _anonymize_and_soft_delete_customer(user, actor='CUSTOMER')
    if not success:
        messages.error(request, err_msg)
        return redirect('edit_profile')

    logout(request)
    messages.success(request, 'Your account has been permanently closed and personal data removed.')
    return redirect('home')


@login_required
def admin_delete_user(request, user_id):
    """Admin can delete a customer account (anonymization & soft-delete)"""
    if request.user.role != 'ADMIN':
        messages.error(request, 'You are not authorized.')
        return redirect('dashboard')

    if request.method != 'POST':
        return redirect('users_list')

    user_to_delete = get_object_or_404(CustomUser, id=user_id, role='USER')

    # Prevent admin from deleting themselves
    if user_to_delete == request.user:
        messages.error(request, 'You cannot delete your own account.')
        return redirect('users_list')

    username = user_to_delete.username
    success, err_msg = _anonymize_and_soft_delete_customer(user_to_delete, actor='ADMIN')
    if not success:
        messages.error(request, f'Cannot delete user "{username}": {err_msg}')
        return redirect('users_list')

    messages.success(request, f'User "{username}" has been closed and anonymized successfully.')
    return redirect('users_list')


@login_required
def admin_delete_worker(request, worker_id):
    """Admin can delete a worker - only if no existing bookings"""
    if request.user.role != 'ADMIN':
        messages.error(request, 'You are not authorized.')
        return redirect('dashboard')

    if request.method != 'POST':
        return redirect('workers_list')

    worker_profile = get_object_or_404(WorkerProfile, id=worker_id)
    username = worker_profile.user.username

    # Check if worker has active or completed bookings that must be preserved
    active_or_completed = Booking.objects.filter(
        worker=worker_profile,
        status__in=['PENDING', 'ACCEPTED', 'IN_PROGRESS', 'COMPLETED']
    ).exists()
    if active_or_completed:
        messages.error(
            request,
            f'Cannot delete worker "{username}" because they have active or completed bookings. '
            f'Please suspend the worker instead to preserve customer history.'
        )
        return redirect('workers_list')

    # Safe to delete - disassociate any inactive/rejected/expired bookings so PROTECT constraint doesn't block deletion
    Booking.objects.filter(worker=worker_profile).update(worker=None)
    worker_profile.user.delete()
    messages.success(request, f'Worker "{username}" has been deleted successfully.')
    return redirect('workers_list')


def _true_up_commission_for_final_bill(booking):
    """Adjust the commission already charged at acceptance to the final bill.

    Only the DIFFERENCE is moved: e.g. Rs 50 charged on a Rs 500 estimate and a
    Rs 2,000 final labour bill => correct fee Rs 200 => deduct Rs 150 more
    (never Rs 200 again). If the final fee is lower, the excess is returned as a
    REFUND row. The amount already charged is read from the ledger, so running
    this twice (or after a partial refund) moves nothing the second time.
    Returns the signed amount applied to the wallet (negative = extra charge).
    """
    worker = booking.worker
    if worker is None:
        return Decimal('0.00')
    final_fee = calculate_booking_platform_fee(booking, worker)
    with transaction.atomic():
        wallet = WorkerWallet.objects.select_for_update().filter(worker=worker).first()
        if wallet is None:
            return Decimal('0.00')
        commission_rows = WalletTransaction.objects.filter(
            wallet=wallet, booking=booking,
            transaction_type__in=('FEE_DEDUCTION', 'REFUND'),
        )
        if not commission_rows.exists():
            # fee_settled with no ledger entry: nothing to compare against.
            return Decimal('0.00')
        if commission_rows.filter(
            transaction_type='FEE_DEDUCTION', amount=Decimal('0.00'),
            notes__startswith='Launch Offer',
        ).exists():
            # Launch-offer waiver granted at acceptance is honoured.
            return Decimal('0.00')
        charged = _commission_outstanding(booking, wallet)
        diff = (final_fee - charged).quantize(Decimal('0.01'))
        if diff == Decimal('0.00'):
            Booking.objects.filter(pk=booking.pk).update(platform_fee=final_fee)
            booking.platform_fee = final_fee
            return Decimal('0.00')
        balance = Decimal(str(wallet.balance if wallet.balance is not None else '0.00'))
        if diff > Decimal('0.00'):
            wallet.balance = balance - diff
            wallet.save(update_fields=['balance', 'updated_at'])
            WalletTransaction.objects.create(
                wallet=wallet, booking=booking, transaction_type='FEE_DEDUCTION',
                amount=-diff, balance_after=wallet.balance,
                notes=(f"Commission adjustment for Booking #{booking.id}: final bill fee Rs {final_fee:.2f}, "
                       f"Rs {charged:.2f} already charged at acceptance.")[:500],
            )
            if wallet.is_locked and not worker.is_paused_for_dues:
                worker.is_paused_for_dues = True
                worker.save(update_fields=['is_paused_for_dues'])
        else:
            refund = -diff
            wallet.balance = balance + refund
            wallet.save(update_fields=['balance', 'updated_at'])
            WalletTransaction.objects.create(
                wallet=wallet, booking=booking, transaction_type='REFUND',
                amount=refund, balance_after=wallet.balance,
                notes=(f"Commission adjustment for Booking #{booking.id}: final bill fee Rs {final_fee:.2f} "
                       f"is lower than Rs {charged:.2f} charged at acceptance.")[:500],
            )
            if worker.is_paused_for_dues and not wallet.is_locked:
                worker.is_paused_for_dues = False
                worker.save(update_fields=['is_paused_for_dues'])
        Booking.objects.filter(pk=booking.pk).update(platform_fee=final_fee)
        booking.platform_fee = final_fee
        return -diff


def _settle_booking_completion_financials(booking):
    """
    Settles platform fee upon booking completion.
    In the prepaid model (Finding C1):
    - Commission is deducted when worker accepts the booking (booking.fee_settled = True).
    - If fee was already settled at acceptance, this function safely returns without duplicate deductions.
    - If fee was not yet settled (e.g. legacy bookings), it deducts the fee once and marks fee_settled=True.
    - Under the direct cash/UPI payment model, LAMO never touches customer funds, so fictional online payouts are eliminated.
    """
    if not booking.worker:
        return
    if booking.fee_settled:
        # Commission was charged at acceptance on the *estimate*. The final
        # bill may differ, so charge/refund only the difference (idempotent).
        _true_up_commission_for_final_bill(booking)
        return

    worker = booking.worker
    platform_fee = calculate_booking_platform_fee(booking, worker)
    booking.platform_fee = platform_fee

    with transaction.atomic():
        wallet, _ = WorkerWallet.objects.select_for_update().get_or_create(worker=worker)
        current_balance = Decimal(str(wallet.balance if wallet.balance is not None else '0.00'))

        if platform_fee > Decimal('0.00'):
            wallet.balance = current_balance - platform_fee
            wallet.save()

            if wallet.is_locked:
                worker.is_paused_for_dues = True
                worker.save(update_fields=['is_paused_for_dues'])

            WalletTransaction.objects.create(
                wallet=wallet,
                booking=booking,
                transaction_type='FEE_DEDUCTION',
                amount=-platform_fee,
                balance_after=wallet.balance,
                notes=f"Platform fee for Booking #{booking.id}"
            )
        else:
            WalletTransaction.objects.create(
                wallet=wallet,
                booking=booking,
                transaction_type='FEE_DEDUCTION',
                amount=Decimal('0.00'),
                balance_after=wallet.balance,
                notes=f"Launch Offer Benefit: 0% Platform Fee for Booking #{booking.id}"
            )

        booking.fee_settled = True
        booking.save(update_fields=['platform_fee', 'fee_settled', 'updated_at'])


def _handle_booking_cancellation_refund(booking, commission_refund=False, refund_reason='Booking cancelled'):
    """
    Cancellation side effects for money.

    Commission (worker's prepaid wallet) - decided by the caller from the
    platform refund policy, and refunded from the ledger only:
      refunded   - customer cancels before work starts, customer not available,
                   worker cancels early enough (WORKER_CANCEL_REFUND_MIN_HOURS),
                   admin suspension, customer account closure, admin decision.
      forfeited  - worker no-show, late worker cancellation, accepted job that
                   auto-expired without being started, anything after the
                   customer verified the worker's arrival.
    Only money actually deducted can be refunded, and only once.

    Customer advance payment - if one was recorded, it is flagged for an
    admin refund (unchanged behaviour).
    """
    if commission_refund:
        _refund_booking_commission(booking, refund_reason)

    is_paid = booking.is_paid or (hasattr(booking, 'payment') and booking.payment.status == 'PAID')
    if not is_paid:
        return

    payment = getattr(booking, 'payment', None)
    refund_amount = payment.amount if (payment and payment.amount > Decimal('0.00')) else (booking.estimated_cost or Decimal('0.00'))

    if payment:
        payment.status = 'REFUNDED'
        payment.save(update_fields=['status', 'updated_at'])

    booking.is_paid = False
    booking.save(update_fields=['is_paid', 'updated_at'])

    # Alert admins to process refund payment
    admins = CustomUser.objects.filter(role='ADMIN', is_active=True)
    for admin in admins:
        Notification.objects.create(
            recipient=admin,
            booking=booking,
            notification_type='PAYMENT',
            message=(
                f"🚨 Refund Required: Booking #{booking.id} was paid online (₹{refund_amount:.2f}) and has been cancelled. "
                f"Please process the refund to customer {booking.customer.get_full_name() or booking.customer.username}."
            )
        )

    # Notify customer that refund is in queue
    Notification.objects.create(
        recipient=booking.customer,
        booking=booking,
        notification_type='PAYMENT',
        message=(
            f"Refund Queued: Your payment of ₹{refund_amount:.2f} for cancelled Booking #{booking.id} "
            f"has been queued for refund. Our team will credit it back to your source account."
        )
    )

    send_email_safely(
        subject=f"Refund Queued for Booking #{booking.id}",
        message=(
            f"Hi {booking.customer.username},\n\n"
            f"Since your Booking #{booking.id} ({booking.service.name}) was cancelled, "
            f"your payment of ₹{refund_amount:.2f} has been queued for a full refund.\n\n"
            f"The refund will be processed back to your original payment method.\n\n"
            f"Thank you for choosing LAMO.\nTeam LAMO"
        ),
        recipient_list=[booking.customer.email],
        purpose='booking_refund',
        async_send=True,
    )


def _cancel_upcoming_worker_bookings_on_suspension(worker_profile, admin_user=None):
    """
    When a worker is suspended by administration, automatically cancel any pending
    or accepted upcoming bookings so customers are not left waiting for an unavailable worker.
    Notifies each affected customer and logs the reason.
    """
    upcoming_bookings = Booking.objects.filter(
        worker=worker_profile,
        status__in=['PENDING', 'ACCEPTED', 'EMERGENCY_PENDING']
    ).select_related('customer', 'service')

    cancelled_count = 0
    reason = (
        "The assigned service professional is no longer available as their account was suspended "
        "by administration. Please select another worker or reschedule."
    )

    for booking in upcoming_bookings:
        with transaction.atomic():
            b = Booking.objects.select_for_update().get(id=booking.id)
            if b.status not in ('PENDING', 'ACCEPTED', 'EMERGENCY_PENDING'):
                continue
            b.status = 'CANCELLED'
            b.cancellation_reason = reason
            b.cancelled_by = 'ADMIN_SUSPEND'
            b.save(update_fields=['status', 'cancellation_reason', 'cancelled_by', 'updated_at'])
            # Platform-initiated cancellation of jobs that were never performed:
            # prepaid commission is returned to the worker's wallet.
            _handle_booking_cancellation_refund(
                b, commission_refund=True,
                refund_reason='Cancelled by administration (worker suspended)',
            )
            cancelled_count += 1

            # In-app notification to the customer
            Notification.objects.create(
                recipient=b.customer,
                booking=b,
                notification_type='BOOKING_UPDATE',
                message=(
                    f"Notice regarding Booking #{b.id} ({b.service.name}): The assigned worker is no longer "
                    f"available and the booking was cancelled. You can select another verified professional from My Bookings."
                )
            )

            # Email notification safely sent to customer
            send_email_safely(
                subject=f"Booking #{b.id} Cancelled - Worker Unavailable",
                message=(
                    f"Hi {b.customer.username},\n\n"
                    f"We regret to inform you that your booking #{b.id} for {b.service.name} "
                    f"scheduled for {b.date or 'the requested time'} has been cancelled because the service professional "
                    f"is no longer available on our platform.\n\n"
                    f"We apologize for the inconvenience. You can choose another top-rated professional for this service anytime on LAMO:\n"
                    f"https://lamo.in/service_page/\n\n"
                    f"Best regards,\nTeam LAMO"
                ),
                recipient_list=[b.customer.email],
                purpose='booking_cancelled',
                async_send=True,
            )

    # Deactivate any active recurring series with this suspended worker
    BookingSeries.objects.filter(worker=worker_profile, is_active=True).update(is_active=False)

    return cancelled_count


@login_required
def admin_suspend_worker(request, worker_id):
    """Admin can suspend/block a worker"""
    if request.user.role != 'ADMIN':
        messages.error(request, 'You are not authorized.')
        return redirect('dashboard')

    if request.method != 'POST':
        return redirect('workers_list')

    worker_profile = get_object_or_404(WorkerProfile, id=worker_id)
    action = request.POST.get('action', 'suspend')

    if action == 'suspend':
        worker_profile.user.is_active = False
        worker_profile.user.save(update_fields=['is_active'])
        worker_profile.work_status = 'SUSPENDED'
        worker_profile.save(update_fields=['work_status'])
        cancelled_count = _cancel_upcoming_worker_bookings_on_suspension(worker_profile, request.user)
        msg = f'Worker "{worker_profile.user.username}" has been suspended.'
        if cancelled_count:
            msg += f' {cancelled_count} upcoming active booking(s) were automatically cancelled and customers notified.'
        messages.success(request, msg)
        Notification.objects.create(
            recipient=worker_profile.user,
            notification_type='SYSTEM',
            message='Your account has been suspended by admin. Please contact support for assistance.'
        )
    elif action == 'unsuspend':
        worker_profile.user.is_active = True
        worker_profile.user.save(update_fields=['is_active'])
        worker_profile.work_status = 'AVAILABLE'
        worker_profile.save(update_fields=['work_status'])
        messages.success(request, f'Worker "{worker_profile.user.username}" has been unsuspended.')
        Notification.objects.create(
            recipient=worker_profile.user,
            notification_type='SYSTEM',
            message='Your account has been reactivated. You can now receive bookings.'
        )

    return redirect('workers_list')


@login_required
def admin_worker_strike(request, worker_id):
    """Admin issues an official warning, strike, or resets strikes with audit notes."""
    is_admin = _is_admin_account(request.user)
    if not is_admin:
        messages.error(request, 'You are not authorized to perform this action.')
        return redirect('dashboard')

    if request.method != 'POST':
        return redirect('workers_list')

    worker_profile = get_object_or_404(WorkerProfile, id=worker_id)
    action_type = request.POST.get('action_type', 'strike').strip().lower()
    reason = request.POST.get('reason', '').strip()
    suspend_now = request.POST.get('suspend_now') in ('1', 'on', 'true', True)

    import datetime
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    if action_type == 'strike':
        worker_profile.strikes_count += 1
        note_entry = f"[{now_str}] STRIKE #{worker_profile.strikes_count}: {reason if reason else 'Policy violation'}"
        if worker_profile.warning_notes:
            worker_profile.warning_notes = f"{worker_profile.warning_notes}\n{note_entry}"
        else:
            worker_profile.warning_notes = note_entry

        auto_suspended = False
        if worker_profile.strikes_count >= 3 or suspend_now:
            worker_profile.work_status = 'SUSPENDED'
            worker_profile.user.is_active = False
            worker_profile.user.save(update_fields=['is_active'])
            auto_suspended = True
            _cancel_upcoming_worker_bookings_on_suspension(worker_profile, request.user)

        worker_profile.save(update_fields=['strikes_count', 'warning_notes', 'work_status'])

        notif_msg = f"Official Strike #{worker_profile.strikes_count} recorded on your account. Reason: {reason or 'Platform policy violation'}."
        if auto_suspended:
            notif_msg += " Your account has been suspended."
        Notification.objects.create(
            recipient=worker_profile.user,
            notification_type='SYSTEM',
            message=notif_msg
        )
        msg_text = f'Strike #{worker_profile.strikes_count} issued to {worker_profile.user.username}.'
        if auto_suspended:
            msg_text += ' Worker account has been SUSPENDED.'
        messages.warning(request, msg_text)

    elif action_type == 'warning':
        note_entry = f"[{now_str}] OFFICIAL WARNING: {reason if reason else 'Advisory notice'}"
        if worker_profile.warning_notes:
            worker_profile.warning_notes = f"{worker_profile.warning_notes}\n{note_entry}"
        else:
            worker_profile.warning_notes = note_entry
        worker_profile.save(update_fields=['warning_notes'])

        Notification.objects.create(
            recipient=worker_profile.user,
            notification_type='SYSTEM',
            message=f"Official Notice from Admin: {reason or 'Please review platform quality guidelines.'}"
        )
        messages.info(request, f'Official warning recorded and sent to {worker_profile.user.username}.')

    elif action_type == 'reset':
        worker_profile.strikes_count = 0
        note_entry = f"[{now_str}] STRIKES RESET: {reason if reason else 'Strikes cleared by admin'}"
        if worker_profile.warning_notes:
            worker_profile.warning_notes = f"{worker_profile.warning_notes}\n{note_entry}"
        else:
            worker_profile.warning_notes = note_entry
        worker_profile.save(update_fields=['strikes_count', 'warning_notes'])
        messages.success(request, f'Strikes reset to 0 for {worker_profile.user.username}.')

    return redirect('workers_list')


@login_required
def service_update(request, pk):

    # Plain Django staff accounts are not LAMO admins (same rule as login):
    # only the ADMIN role or a superuser may manage services.
    is_admin = (
        request.user.role == 'ADMIN'
        or getattr(request.user, 'is_superuser', False)
    )
    if not is_admin:
        messages.error(request, 'You are not authorized to access this page.')
        return redirect('dashboard')
    if request.user.role != 'ADMIN' and request.user.is_superuser:
        request.user.role = 'ADMIN'
        request.user.save(update_fields=['role'])

    service = get_object_or_404(
        Service,
        id=pk
    )

    if request.method == 'POST':

        form = ServiceForm(
            request.POST,
            request.FILES,
            instance=service
        )

        if form.is_valid():

            form.save()

            messages.success(
                request,
                'Service updated successfully.'
            )

            return redirect('service_list')

    else:

        form = ServiceForm(
            instance=service
        )

    return render(
        request,
        'lamo_service/service_form.html',
        {
            'form': form,
            'title': 'Edit Service',
            'service': service
        }
    )

@login_required
def service_delete(request, pk):

    # Only POST is allowed
    if request.method != 'POST':
        messages.error(
            request,
            'Invalid request method.'
        )
        return redirect('service_list')

    # Only ADMIN can delete services
    is_admin = _is_admin_account(request.user)
    if not is_admin:
        messages.error(
            request,
            'You are not authorized to delete services.'
        )
        return redirect('dashboard')

    service = get_object_or_404(
        Service,
        pk=pk
    )

    service_name = service.name

    # Deleting a service must never destroy booking history: Booking.service is
    # PROTECT, so the delete would raise ProtectedError (a 500 for the admin).
    # WorkerProfile.service is SET_NULL, so a delete would also silently unlink
    # every worker offering the service. Both cases are refused here and the
    # admin is pointed at deactivation, which keeps all records intact.
    if Booking.objects.filter(service=service).exists():
        messages.error(
            request,
            f'Cannot delete "{service_name}" because it has existing booking '
            f'records. Please deactivate the service instead to preserve '
            f'booking history.'
        )
        return redirect('service_list')

    linked_workers = WorkerProfile.objects.filter(service=service).count()
    if linked_workers:
        messages.error(
            request,
            f'Cannot delete "{service_name}" because {linked_workers} '
            f'worker(s) are assigned to it. Reassign those workers first, or '
            f'deactivate the service instead.'
        )
        return redirect('service_list')

    try:
        # Delete the service
        service.delete()
    except ProtectedError:
        # Safety net: any other PROTECTed relation added later lands here
        # instead of turning into an HTTP 500.
        messages.error(
            request,
            f'Cannot delete "{service_name}" because it is still referenced by '
            f'existing records. Please deactivate the service instead.'
        )
        return redirect('service_list')

    messages.success(
        request,
        f'"{service_name}" deleted successfully.'
    )

    return redirect('service_list')


@login_required
def service_list(request):

    if request.user.role != 'ADMIN':
        return redirect('dashboard')

    services = Service.objects.all()

    return render(
        request,
        'lamo_service/service_list.html',
        {
            'services': services
        }
    )

def service_detail(request, pk):

    service = get_object_or_404(
        Service,
        pk=pk,
        is_active=True
    )

    district = request.GET.get('district', '').strip()
    if not district:
        district = request.session.get('district') or request.session.get('lamo_selected_district') or ''
        if not district and request.user.is_authenticated and hasattr(request.user, 'user_profile'):
            district = getattr(request.user.user_profile, 'district', '') or ''

    locality = request.GET.get('locality', '').strip()
    if not locality:
        locality = request.session.get('locality') or request.session.get('lamo_selected_locality') or ''
        if not locality and request.user.is_authenticated and hasattr(request.user, 'user_profile'):
            locality = getattr(request.user.user_profile, 'locality', '') or ''

    # If district is already active in session or URL, direct customer straight to available workers
    # unless overview=1 is explicitly requested for the static info card.
    if district and not request.GET.get('overview'):
        redirect_url = reverse('service_workers', args=[service.id]) + f'?district={urllib.parse.quote(district)}'
        if locality:
            redirect_url += f'&locality={urllib.parse.quote(locality)}'
        return redirect(redirect_url)

    return render(
        request,
        'lamo_service/service_detail.html',
        {
            'service': service,
            'district': district,
            'locality': locality,
        }
    )

def service_workers(request, pk):

    service = get_object_or_404(
        Service,
        pk=pk,
        is_active=True
    )

    district = request.GET.get('district', '').strip()
    if not district:
        district = request.session.get('district') or request.session.get('lamo_selected_district')
    if not district:
        messages.error(request, 'Please select a district to find workers.')
        return redirect('service_detail', pk=service.id)
    ai_requirement = request.GET.get('ai_requirement', '').strip()

    # Task 2: AI recommendation path (external API calls) is logged-in only.
    # Clear ai_requirement for guests so build_worker_recommendations is never
    # invoked and no external cost is incurred.
    if not request.user.is_authenticated:
        ai_requirement = ''

    # The customer may narrow the list to a specific date/time before booking.
    # Anything invalid or in the past simply falls back to "no date chosen",
    # in which case only the status rules below apply.
    from datetime import datetime
    from django.utils import timezone as django_timezone

    selected_date = None
    raw_date = request.GET.get('date', '').strip()
    if raw_date:
        try:
            parsed_date = datetime.strptime(raw_date, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            parsed_date = None
        if parsed_date and parsed_date >= django_timezone.now().date():
            selected_date = parsed_date

    raw_time = request.GET.get('time', '').strip()
    selected_start, selected_end = parse_time_range(raw_time) if raw_time else (None, None)
    selected_time_value = raw_time or None

    # Leave + existing bookings are resolved once for the whole page (two
    # queries) instead of once per rendered worker.
    leave_worker_ids, busy_by_worker = build_worker_availability_maps(
        selected_date,
        selected_start,
        selected_end,
    )

    # book_service refuses any worker whose work_status is not AVAILABLE, so
    # those workers must not be offered as bookable here either. Suspended and
    # unverified workers stay hidden entirely.
    workers = (
        WorkerProfile.objects
        .select_related('user', 'service', 'wallet')
        .filter(
            service=service,
            is_verified=True,
            # ON_DUTY workers are busy right now, so they are only offered
            # when the customer picked a later date (book_service accepts
            # exactly that case).
            work_status__in=(
                ['AVAILABLE', 'ON_DUTY'] if worker_on_duty_can_take(selected_date) else ['AVAILABLE']
            ),
            district__iexact=district,
        )
        .annotate(
            avg_rating=Avg('reviews_received__rating'),
            review_count=Count('reviews_received')
        )
        .order_by('-is_verified', 'user__username')
    )

    # Attach the authoritative, backend-computed reason list to every worker so
    # the template can disable exactly the cards the booking view would reject.
    workers = list(workers)
    for worker in workers:
        worker.unavailable_reasons = get_worker_unavailability_reasons(
            worker,
            leave_worker_ids=leave_worker_ids,
            busy_by_worker=busy_by_worker,
            start_time=selected_start,
            end_time=selected_end,
            time_value=selected_time_value,
            booking_date=selected_date,
        )
        worker.is_bookable = not worker.unavailable_reasons

    bookable_workers = [w for w in workers if w.is_bookable]

    locality = request.GET.get('locality', '').strip()
    if not locality:
        locality = request.session.get('locality') or request.session.get('lamo_selected_locality') or ''

    customer_lat = request.GET.get('lat') or request.session.get('lat') or request.session.get('lamo_selected_lat')
    customer_lng = request.GET.get('lng') or request.session.get('lng') or request.session.get('lamo_selected_lng')

    if (customer_lat is None or customer_lng is None) and locality:
        from .location_data import get_locality_coordinates
        loc_lat, loc_lng = get_locality_coordinates(district, locality)
        if loc_lat is not None and loc_lng is not None:
            customer_lat, customer_lng = loc_lat, loc_lng

    if (customer_lat is None or customer_lng is None) and request.user.is_authenticated and hasattr(request.user, 'user_profile'):
        customer_lat = getattr(request.user.user_profile, 'latitude', None) or customer_lat
        customer_lng = getattr(request.user.user_profile, 'longitude', None) or customer_lng
        if not locality:
            locality = getattr(request.user.user_profile, 'locality', '') or getattr(request.user.user_profile, 'address', '')

    # Compute real distance and AI match badges for all workers in the list
    from .ai_utils import haversine_km, geocode_place, distance_score
    c_lat_flt = _safe_coord(customer_lat)
    c_lng_flt = _safe_coord(customer_lng)

    for worker in workers:
        w_lat = _safe_coord(getattr(worker, 'latitude', None))
        w_lng = _safe_coord(getattr(worker, 'longitude', None))
        if (w_lat is None or w_lng is None) and worker.address:
            w_lat, w_lng = geocode_place(worker.address, district=worker.district)

        if c_lat_flt is not None and c_lng_flt is not None and w_lat is not None and w_lng is not None:
            dist_val = haversine_km(c_lat_flt, c_lng_flt, w_lat, w_lng)
            worker.distance_km = round(dist_val, 1)
            worker.distance_label = f"{dist_val:.1f} km away"
            prox_score = distance_score(dist_val)
        else:
            worker.distance_km = None
            worker.distance_label = "Exact distance unavailable" if locality else f"{worker.district}"
            prox_score = 50

        # AI Match calculation based on proximity, rating, experience, verification
        rat_score = min(100, ((worker.avg_rating or 4.0) / 5.0) * 100)
        exp_score = min(100, (worker.experience_years / 10.0) * 100) if worker.experience_years else 50
        ver_score = 100 if worker.is_verified else 50
        worker.ai_match_score = round(prox_score * 0.40 + rat_score * 0.25 + exp_score * 0.20 + ver_score * 0.15)
        worker.is_ai_recommended = False
        worker.is_ai_top_pick = False

    # Sort workers so closest and highest-match come first
    if any(w.distance_km is not None for w in workers):
        workers.sort(key=lambda w: (w.distance_km is None, w.distance_km or 9999, -(w.ai_match_score or 0)))
        if workers and workers[0].distance_km is not None:
            workers[0].is_ai_top_pick = True
            if len(workers) > 1 and (workers[1].distance_km or 999) <= 30:
                workers[1].is_ai_recommended = True
    elif workers:
        workers[0].is_ai_top_pick = True

    ai_recommendations = []
    # For authenticated users, automatically generate AI recommendations for this service
    # based on distance to the customer's locality/GPS and worker availability.
    # No manual "describe your electrical issue" typing required!
    if request.user.is_authenticated:
        from .ai_utils import build_worker_recommendations
        ai_recommendations = build_worker_recommendations(
            service,
            district=district,
            requirement=ai_requirement or service.name,
            customer_place=locality,
            customer_lat=customer_lat,
            customer_lng=customer_lng,
        )
        # The recommender only knows verified + available workers. A worker who
        # is paused for dues, or whose prepaid wallet cannot cover the
        # acceptance commission, would be refused by book_service after the
        # customer filled the whole form - so never recommend them (this is the
        # same rule the main worker list already applies).
        # Wallet balances: reuse the wallets the main list already loaded
        # (select_related) and only query for recommended workers that are not
        # in that list - at most one query, never one per worker.
        _wallet_balances = {}
        for _w in workers:
            try:
                _wallet_balances[_w.id] = _w.wallet.balance
            except Exception:
                _wallet_balances[_w.id] = None
        _missing_ids = [
            _r['worker'].id for _r in ai_recommendations
            if isinstance(_r, dict) and _r.get('worker') is not None and _r['worker'].id not in _wallet_balances
        ]
        if _missing_ids:
            for _wid, _bal in WorkerWallet.objects.filter(worker_id__in=_missing_ids).values_list('worker_id', 'balance'):
                _wallet_balances[_wid] = _bal
        _bookable_recs = []
        for _rec in ai_recommendations:
            _rec_worker = _rec.get('worker') if isinstance(_rec, dict) else None
            if _rec_worker is None:
                _bookable_recs.append(_rec)
                continue
            try:
                _fee = estimate_worker_acceptance_fee(_rec_worker)
                _balance = Decimal(str(_wallet_balances.get(_rec_worker.id) or '0.00'))
                _locked_out = bool(getattr(_rec_worker, 'is_paused_for_dues', False)) or (
                    _fee > Decimal('0.00') and _balance < _fee
                )
            except Exception:
                _locked_out = False
            if not _locked_out:
                _bookable_recs.append(_rec)
        ai_recommendations = _bookable_recs

    district_lat, district_lng = DISTRICT_COORDINATES.get(district, (9.9312, 76.2673))

    customer_prefill = _get_customer_prefill(request.user) if request.user.is_authenticated else {}

    return render(
        request,
        'lamo_service/service_workers.html',
        {
            'service': service,
            'district': district,
            'locality': locality,
            'customer_lat': customer_lat,
            'customer_lng': customer_lng,
            'district_lat': district_lat,
            'district_lng': district_lng,
            'workers': workers,
            'bookable_workers': bookable_workers,
            'bookable_count': len(bookable_workers),
            'selected_date': selected_date,
            'selected_time': raw_time,
            'ai_requirement': ai_requirement,
            'ai_recommendations': ai_recommendations,
            'customer_prefill': customer_prefill,
        }
    )

def _map_service_metadata(service):
    """Assign display metadata (category, icon, badge color, and image fallback)."""
    name_lower = (service.name or '').lower()
    
    if any(k in name_lower for k in ['clean', 'shampoo', 'chimney', 'tank & sump', 'chlorination']):
        cat_key = 'cleaning'
        cat_label = 'Home Cleaning'
        cat_icon = 'bi bi-stars'
        badge_bg = '#e8f5e9'
        badge_color = '#2e7d32'
        fallback_img = 'img/cleaning.png'
    elif any(k in name_lower for k in ['electric', 'inverter', 'smart tv', 'electronics']):
        cat_key = 'electrical'
        cat_label = 'Electrical'
        cat_icon = 'bi bi-lightning-charge-fill'
        badge_bg = '#e3f2fd'
        badge_color = '#1565c0'
        fallback_img = 'img/electrician.webp'
    elif any(k in name_lower for k in ['plumb', 'well clean', 'septic', 'water tank']):
        cat_key = 'plumbing'
        cat_label = 'Plumbing'
        cat_icon = 'bi bi-wrench-adjustable'
        badge_bg = '#fff3e0'
        badge_color = '#e65100'
        fallback_img = 'img/plumber.webp'
    elif any(k in name_lower for k in ['ac repair', 'air condition']):
        cat_key = 'ac_repair'
        cat_label = 'AC Repair'
        cat_icon = 'bi bi-snow'
        badge_bg = '#e0f2f1'
        badge_color = '#00796b'
        fallback_img = 'img/ac-repair.webp'
    elif any(k in name_lower for k in ['paint', 'whitewash']):
        cat_key = 'painting'
        cat_label = 'Painting'
        cat_icon = 'bi bi-paint-bucket'
        badge_bg = '#fbe9e7'
        badge_color = '#d84315'
        fallback_img = 'img/painter.webp'
    elif any(k in name_lower for k in ['carpenter', 'carpentry', 'woodwork']):
        cat_key = 'carpentry'
        cat_label = 'Carpentry'
        cat_icon = 'bi bi-hammer'
        badge_bg = '#fff8e1'
        badge_color = '#f57f17'
        fallback_img = 'img/carpenter.webp'
    elif any(k in name_lower for k in ['appliance']):
        cat_key = 'appliance'
        cat_label = 'Appliance Repair'
        cat_icon = 'bi bi-cpu'
        badge_bg = '#e8eaf6'
        badge_color = '#283593'
        fallback_img = 'img/appliance_repair.jpg'
    elif any(k in name_lower for k in ['garden', 'grass', 'bush', 'coconut', 'tree']):
        cat_key = 'gardening'
        cat_label = 'Gardening'
        cat_icon = 'bi bi-flower1'
        badge_bg = '#f1f8e9'
        badge_color = '#33691e'
        fallback_img = 'img/gardening_service.jpg'
    elif any(k in name_lower for k in ['pest']):
        cat_key = 'pest_control'
        cat_label = 'Pest Control'
        cat_icon = 'bi bi-bug-fill'
        badge_bg = '#efebe9'
        badge_color = '#4e342e'
        fallback_img = 'img/general.png'
    else:
        cat_key = 'more'
        cat_label = 'More Services'
        cat_icon = 'bi bi-grid-fill'
        badge_bg = '#f5f5f5'
        badge_color = '#424242'
        fallback_img = 'img/general.png'

    img_url = None
    if service.image:
        try:
            img_url = service.image.url
        except Exception:
            img_url = None

    return {
        'category_key': cat_key,
        'category_label': cat_label,
        'icon_class': cat_icon,
        'badge_bg': badge_bg,
        'badge_color': badge_color,
        'fallback_img': fallback_img,
        'img_url': img_url,
    }


def service_page(request):
    kerala_districts = WorkerProfile.DISTRICT_CHOICES
    district_names = [d[0] for d in kerala_districts]
    raw_district = request.GET.get('district', '').strip()
    
    if raw_district and any(d.lower() == raw_district.lower() for d in district_names):
        selected_district = next(d for d in district_names if d.lower() == raw_district.lower())
        if request.session.get('district') != selected_district:
            request.session['district'] = selected_district
        if request.session.get('lamo_selected_district') != selected_district:
            request.session['lamo_selected_district'] = selected_district
    else:
        selected_district = (
            request.session.get('district')
            or request.session.get('lamo_selected_district')
            or request.COOKIES.get('lamo_selected_district')
            or ''
        )
        if (
            not selected_district
            and request.user.is_authenticated
            and hasattr(request.user, 'user_profile')
            and getattr(request.user.user_profile, 'district', None) in district_names
        ):
            selected_district = request.user.user_profile.district

    if not selected_district:
        selected_district = 'Kollam'
        if request.session.get('district') != selected_district:
            request.session['district'] = selected_district
        if request.session.get('lamo_selected_district') != selected_district:
            request.session['lamo_selected_district'] = selected_district

    all_services_list, category_counts = _get_service_catalog(selected_district)

    search_query = (request.GET.get('q') or request.GET.get('search') or '').strip()
    selected_category = request.GET.get('category', 'all').strip().lower()
    sort_by = request.GET.get('sort', 'relevant').strip()
    min_price_val = request.GET.get('min_price', '').strip()
    max_price_val = request.GET.get('max_price', '').strip()

    filtered_services = all_services_list

    corrected_query = None
    had_typo = False

    if search_query:
        from .search_utils import search_and_rank_services
        filtered_services, corrected_query, had_typo = search_and_rank_services(
            filtered_services, search_query
        )
    if selected_category and selected_category != 'all':
        filtered_services = [s for s in filtered_services if s.category_key == selected_category]

    if min_price_val:
        try:
            min_p = float(min_price_val)
            filtered_services = [s for s in filtered_services if s.price and float(s.price) >= min_p]
        except ValueError:
            pass

    if max_price_val:
        try:
            max_p = float(max_price_val)
            filtered_services = [s for s in filtered_services if s.price and float(s.price) <= max_p]
        except ValueError:
            pass

    if sort_by == 'price_low':
        filtered_services = sorted(filtered_services, key=lambda s: float(s.price or 0))
    elif sort_by == 'price_high':
        filtered_services = sorted(filtered_services, key=lambda s: float(s.price or 0), reverse=True)
    elif sort_by == 'name_asc':
        filtered_services = sorted(filtered_services, key=lambda s: s.name.lower())
    else:
        filtered_services = sorted(
            filtered_services,
            key=lambda s: (not getattr(s, 'is_featured', False), s.name.lower())
        )

    # Preserve all filter/search query parameters across pagination links
    params = request.GET.copy()
    if 'page' in params:
        del params['page']
    pagination_query = params.urlencode()

    # Paginate services: 8 cards per page (2 rows of 4 columns)
    paginator = Paginator(filtered_services, 8)
    page_number = request.GET.get('page', 1)
    services_page = paginator.get_page(page_number)

    response = render(
        request,
        'lamo_service/service_page.html',
        {
            'services': services_page,
            'page_obj': services_page,
            'paginator': paginator,
            'pagination_query': pagination_query,
            'kerala_districts': kerala_districts,
            'selected_district': selected_district,
            'search_query': search_query,
            'corrected_query': corrected_query,
            'had_typo': had_typo,
            'selected_category': selected_category,
            'sort_by': sort_by,
            'min_price': min_price_val,
            'max_price': max_price_val,
            'category_counts': category_counts,
            'total_services_count': len(filtered_services),
            'all_services_count': len(all_services_list),
        }
    )
    response.set_cookie('lamo_selected_district', selected_district, max_age=86400 * 30, samesite='Lax')
    return response


SEARCH_QUERY_MAX_LENGTH = 100


def api_search_suggestions(request):
    """
    Returns live search suggestions and popular services.
    Supports typo-tolerance, phonetic matching, and category aliases.

    The service list and per-district worker counts come from a short-lived
    public cache, so a typical keystroke costs no database query beyond the
    session lookup. Input is length-limited and the district must be a real
    district (an arbitrary value can neither be echoed back nor create cache keys).
    """
    raw_query = (request.GET.get('q') or request.GET.get('search') or '').strip()[:SEARCH_QUERY_MAX_LENGTH]
    district = (
        _canonical_district(request.GET.get('district'))
        or _canonical_district(request.session.get('district'))
        or _canonical_district(request.session.get('lamo_selected_district'))
        or 'Kollam'
    )

    all_services, _counts = _get_service_catalog(
        district, cache_prefix='svc_search_data_',
        ttl=_cache_ttl('HOME_CATALOG_CACHE_SECONDS', 60),
    )
    # Same ordering the endpoint always used: featured first, then by name.
    all_services = sorted(all_services, key=lambda s: (not s.is_featured, (s.name or '').lower()))
    for s in all_services:
        cnt = s.real_worker_count
        if cnt > 0:
            s.pro_count_display = f"{cnt} Verified Pro{'s' if cnt != 1 else ''} in {district}"
        else:
            s.pro_count_display = "Verified across Kerala"

    from .search_utils import get_search_suggestions_payload
    payload = get_search_suggestions_payload(all_services, raw_query, district=district, limit=8)
    return JsonResponse(payload)


@login_required
def admin_payments(request):
    """List recorded manual payment reports for admin review.

    This GET view is read-only. Pending reports are marked paid only through
    the separate POST endpoint, after the admin verifies external evidence.
    """
    if request.user.role != 'ADMIN':
        messages.error(request, 'You are not authorized to view payments.')
        return redirect('dashboard')

    status = request.GET.get('status', '').strip().upper()
    method = request.GET.get('method', '').strip().upper()
    view_mode = request.GET.get('view', '').strip().lower()

    if view_mode == 'unpaid':
        # Pending reports appear in the payments list; this view lists bookings
        # for which no manual payment report has been submitted.
        bookings = Booking.objects.filter(
            is_paid=False,
            payment__isnull=True,
        ).select_related(
            'customer', 'worker__user', 'service'
        ).order_by('-created_at')

        if method in MANUAL_PAYMENT_METHODS:
            bookings = bookings.filter(payment_method=method)

        paginator = Paginator(bookings, 20)
        payments_page = paginator.get_page(request.GET.get('page', 1))
        unpaid_bookings = list(payments_page.object_list)
        payments_page = None
    else:
        payments = Payment.objects.select_related(
            'booking', 'booking__customer', 'booking__worker__user', 'booking__service'
        ).all()

        if status in dict(Payment.PAYMENT_STATUS_CHOICES):
            payments = payments.filter(status=status)
        if method:
            payments = payments.filter(payment_method=method)

        paginator = Paginator(payments, 20)
        payments_page = paginator.get_page(request.GET.get('page', 1))
        unpaid_bookings = []

    paid_total = Payment.objects.filter(status='PAID').aggregate(
        total=Sum('amount')
    )['total'] or 0

    # Retrieve pending and recent worker wallet recharges for admin reconciliation
    wallet_recharges = WalletTransaction.objects.filter(
        transaction_type__in=['RECHARGE_PENDING', 'RECHARGE', 'RECHARGE_REJECTED']
    ).select_related('wallet__worker__user').order_by('-created_at')[:50]

    return render(
        request,
        'admin/payments.html',
        {
            'payments': payments_page,
            'unpaid_bookings': unpaid_bookings,
            'is_unpaid_view': view_mode == 'unpaid',
            'selected_status': status,
            'selected_method': method,
            'status_choices': Payment.PAYMENT_STATUS_CHOICES,
            'method_choices': MANUAL_PAYMENT_METHODS,
            'total_count': paginator.count,
            'paid_total': paid_total,
            'wallet_recharges': wallet_recharges,
        }
    )


@login_required
def admin_approve_wallet_recharge(request, tx_id):
    """Admin action to approve a pending worker wallet recharge after verifying external bank UTR evidence."""
    if request.user.role != 'ADMIN':
        messages.error(request, 'You are not authorized to approve wallet recharges.')
        return redirect('dashboard')

    if request.method != 'POST':
        return redirect('admin_payments')

    with transaction.atomic():
        tx = get_object_or_404(
            WalletTransaction.objects.select_for_update().select_related('wallet__worker'),
            id=tx_id,
            transaction_type='RECHARGE_PENDING'
        )
        wallet = WorkerWallet.objects.select_for_update().get(id=tx.wallet_id)
        worker_profile = wallet.worker

        # Finding C6: Read recharge amount directly from transaction amount field
        amount = tx.amount or Decimal('0.00')

        # Fallback to notes parsing only for legacy transactions created before this update
        if amount <= Decimal('0.00') and tx.notes:
            raw_amt_match = re.search(r'₹([\d\.]+)', tx.notes)
            if raw_amt_match:
                try:
                    amount = Decimal(raw_amt_match.group(1))
                except Exception:
                    amount = Decimal('0.00')

        if amount <= Decimal('0.00'):
            messages.error(request, f"Cannot approve recharge #{tx.id}: Invalid or missing recharge amount.")
            return redirect('admin_payments')

        current_balance = Decimal(str(wallet.balance if wallet.balance is not None else '0.00'))
        wallet.balance = current_balance + amount
        wallet.save()

        # If dues were cleared, unpause the worker
        if wallet.balance >= Decimal('0.00') and worker_profile.is_paused_for_dues:
            worker_profile.is_paused_for_dues = False
            worker_profile.save(update_fields=['is_paused_for_dues'])

        # Update transaction to approved
        tx.transaction_type = 'RECHARGE'
        tx.amount = amount
        tx.balance_after = wallet.balance
        tx.notes = f"Approved by Admin #{request.user.username} on {timezone.now().strftime('%Y-%m-%d %H:%M')}. UTR {tx.reference_id} verified against bank records."
        tx.save(update_fields=['transaction_type', 'amount', 'balance_after', 'notes'])

        Notification.objects.create(
            recipient=worker_profile.user,
            notification_type='PAYMENT',
            message=f"✅ Wallet Recharge Approved: Your payment of ₹{amount:.2f} (UTR: {tx.reference_id}) has been verified and added to your balance. Current balance: ₹{wallet.balance:.2f}."
        )

    messages.success(
        request,
        f"Recharge #{tx.id} for {worker_profile.user.username} (₹{amount:.2f}) verified and credited successfully! New balance: ₹{wallet.balance:.2f}."
    )
    return redirect('admin_payments')


@login_required
def admin_reject_wallet_recharge(request, tx_id):
    """Admin action to reject an invalid or unverified worker wallet recharge."""
    if request.user.role != 'ADMIN':
        messages.error(request, 'You are not authorized to reject wallet recharges.')
        return redirect('dashboard')

    if request.method != 'POST':
        return redirect('admin_payments')

    rejection_reason = request.POST.get('rejection_reason', '').strip() or 'Bank transaction could not be verified'

    with transaction.atomic():
        tx = get_object_or_404(
            WalletTransaction.objects.select_for_update().select_related('wallet__worker'),
            id=tx_id,
            transaction_type='RECHARGE_PENDING'
        )
        worker_user = tx.wallet.worker.user

        rejected_utr = tx.reference_id
        tx.transaction_type = 'RECHARGE_REJECTED'
        tx.notes = f"Rejected by Admin #{request.user.username} on {timezone.now().strftime('%Y-%m-%d %H:%M')}. Reason: {rejection_reason}"
        tx.save(update_fields=['transaction_type', 'notes'])
        # A rejected claim must not reserve the UTR for the genuine payer.
        tx.reference_id = rejected_utr
        _release_rejected_utr(tx)

        Notification.objects.create(
            recipient=worker_user,
            notification_type='PAYMENT',
            message=f"❌ Wallet Recharge Rejected: Your UTR {rejected_utr} could not be verified by the admin team. Reason: {rejection_reason}."
        )

    messages.info(request, f"Recharge #{tx.id} rejected. Worker has been notified.")
    return redirect('admin_payments')


@login_required
def users_list(request):

    if request.user.role != 'ADMIN':
        return redirect('dashboard')

    users = CustomUser.objects.filter(
        role='USER'
    ).order_by('-date_joined')

    # Server-side search. Filtering happens in the database (Q over indexed
    # columns) rather than in Python, and it is applied BEFORE pagination so
    # the page count always reflects the filtered result set.
    search = request.GET.get('q', '').strip()
    if search:
        users = users.filter(
            Q(username__icontains=search)
            | Q(first_name__icontains=search)
            | Q(last_name__icontains=search)
            | Q(email__icontains=search)
            | Q(user_profile__phone__icontains=search)
        ).distinct()

    paginator = Paginator(users, 20)
    page = request.GET.get('page', 1)
    users_page = paginator.get_page(page)

    return render(
        request,
        'dashboards/users_list.html',
        {
            'users': users_page,
            'total_count': paginator.count,
            'search': search,
        }
    )

@login_required
def workers_list(request):

    if request.user.role != 'ADMIN':
        return redirect('dashboard')

    workers = WorkerProfile.objects.select_related(
        'user',
        'service'
    ).all().order_by('-id')

    # Server-side search across the worker's account, contact details and the
    # trade they are listed under. Applied before pagination.
    search = request.GET.get('q', '').strip()
    if search:
        workers = workers.filter(
            Q(user__username__icontains=search)
            | Q(user__first_name__icontains=search)
            | Q(user__last_name__icontains=search)
            | Q(user__email__icontains=search)
            | Q(phone__icontains=search)
            | Q(district__icontains=search)
            | Q(service__name__icontains=search)
        ).distinct()

    paginator = Paginator(workers, 20)
    page = request.GET.get('page', 1)
    workers_page = paginator.get_page(page)

    return render(
        request,
        'dashboards/workers_list.html',
        {
            'workers': workers_page,
            'total_count': paginator.count,
            'search': search,
        }
    )
@login_required
def worker_verify(request, pk):

    # ADMIN ONLY
    if request.user.role != 'ADMIN':
        messages.error(
            request,
            'You are not authorized to verify workers.'
        )

        return redirect('dashboard')


    # ONLY POST
    if request.method != 'POST':

        messages.error(
            request,
            'Invalid request method.'
        )

        return redirect('workers_list')


    worker = get_object_or_404(
        WorkerProfile,
        pk=pk
    )


    # ALREADY VERIFIED

    if worker.is_verified:

        messages.info(
            request,
            f'{worker.user.username} is already verified.'
        )

        return redirect('workers_list')


    # VERIFY WORKER
    worker.is_verified = True
    if not worker.launch_ends_at:
        worker.launch_ends_at = timezone.now() + timedelta(days=30)

    worker.save(
        update_fields=[
            'is_verified',
            'launch_ends_at',
            'updated_at'
        ]
    )

    WorkerWallet.objects.get_or_create(
        worker=worker,
        defaults={'balance': Decimal('0.00')}
    )

    # --------------------------------------------------------
    # GENERATE SECURE 1-CLICK MAGIC LOGIN LINK & EMAIL WORKER
    # --------------------------------------------------------
    try:
        signer = TimestampSigner(salt='worker-magic-login')
        magic_token = signer.sign(f"{worker.user.id}:{_magic_login_marker(worker.user)}")
        magic_url = request.build_absolute_uri(reverse('worker_magic_login', args=[magic_token]))

        worker_name = worker.user.first_name or worker.user.username
        service_name = worker.service.name if worker.service else (worker.custom_service or 'Service Professional')

        email_body = (
            f"Hello {worker_name},\n\n"
            f"Great news! Your LAMO Partner application for {service_name} has been verified and approved by our Admin team.\n\n"
            f"You have been granted a 30-day 0% commission launch period to get started!\n\n"
            f"Click the link below to activate your account and open your Worker Dashboard directly (no password or extra OTP required):\n"
            f"{magic_url}\n\n"
            f"Welcome to the LAMO Professional Network!\n"
            f"Team LAMO"
        )
        send_email_safely(
            subject='🎉 Congratulations! Your LAMO Partner Account is Approved',
            message=email_body,
            recipient_list=[worker.user.email],
            purpose='worker_approved_activation',
            async_send=True,
        )
    except Exception as exc:
        logging.getLogger(__name__).warning("Could not dispatch approval email to worker %s: %s", worker.id, exc)

    messages.success(
        request,
        f'{worker.user.username} has been verified successfully. An approval email with a direct 1-click dashboard link has been sent to {worker.user.email}.'
    )

    return redirect('workers_list')


def worker_application_status(request):
    """Displays the clear 'Application Under Review' screen for newly registered workers.

    Prevents unverified workers from being dumped on the login page or trapped in OTP loops.
    """
    worker = None
    worker_id = request.session.get('under_review_worker_id')
    if worker_id:
        worker = WorkerProfile.objects.select_related('user', 'service').filter(id=worker_id).first()
    elif request.user.is_authenticated and request.user.role == 'WORKER':
        worker = getattr(request.user, 'worker_profile', None)

    if not worker:
        return redirect('home')

    # If the worker has already been verified, navigate them forward
    if worker.is_verified:
        if request.user.is_authenticated:
            return redirect('worker_dashboard')
        messages.info(request, 'Your worker account has already been approved! Please sign in.')
        return redirect('login')

    return render(request, 'accounts/worker_under_review.html', {
        'worker': worker,
    })


def _magic_login_marker(user):
    """Value that changes once the user logs in (makes magic links single-use)."""
    return str(int(user.last_login.timestamp())) if user.last_login else '0'


def worker_magic_login(request, token):
    """Direct, 1-click activation and login for approved workers from their verification email.

    Validates a cryptographic timestamp token (valid for 7 days), logs the worker in directly
    without passwords or redundant OTPs, and lands them on their Worker Dashboard.
    """
    signer = TimestampSigner(salt='worker-magic-login')
    try:
        raw = signer.unsign(token, max_age=86400 * 7)
        user_id, _, login_marker = raw.partition(':')
        user = CustomUser.objects.get(id=int(user_id), role='WORKER')
    except (BadSignature, SignatureExpired, CustomUser.DoesNotExist, ValueError):
        messages.error(request, 'This activation link is invalid or has expired. Please sign in with your credentials.')
        return redirect('login')

    # Single use: the token embeds the user's last_login at issue time, and
    # logging in changes last_login, so a used link no longer matches.
    # Legacy id-only links are honoured only while the worker has never
    # logged in (i.e. still at most once).
    if login_marker:
        valid = login_marker == _magic_login_marker(user)
    else:
        valid = user.last_login is None
    if not valid:
        messages.error(request, 'This activation link has already been used. Please sign in with your credentials.')
        return redirect('login')

    worker = getattr(user, 'worker_profile', None)
    if not worker or not worker.is_verified:
        messages.warning(request, 'Your worker profile is still awaiting admin verification.')
        return redirect('login')

    # Authenticate worker session directly
    login(request, user)
    request.session.cycle_key()
    messages.success(
        request,
        f'Welcome to LAMO, {user.first_name or user.username}! Your worker account has been activated successfully.'
    )
    return redirect('worker_dashboard')


@login_required
def worker_manage_dashboard(request):

    # ==========================================
    # WORKER ONLY
    # ==========================================

    if request.user.role != 'WORKER':

        messages.error(
            request,
            'You are not authorized to access the worker dashboard.'
        )

        return redirect('dashboard')


    # ==========================================
    # GET WORKER PROFILE
    # ==========================================

    try:

        worker_profile = request.user.worker_profile

    except WorkerProfile.DoesNotExist:

        messages.error(
            request,
            'Worker profile not found.'
        )

        return redirect('dashboard')


    # ==========================================
    # HANDLE POST
    # ==========================================

    if request.method == 'POST':

        action = request.POST.get('action')


        # ======================================
        # WORK STATUS
        # Available / On Duty / On Leave
        # ======================================

        if action == 'work_status':

            work_status = request.POST.get(
                'work_status',
                ''
            ).strip()


            # Check valid choice
            valid_values = [
                value
                for value, label
                in WorkerProfile.WORK_STATUS_CHOICES
            ]


            if work_status not in valid_values:

                messages.error(
                    request,
                    'Please select a valid work status.'
                )

                return redirect(
                    'worker_manage_dashboard'
                )


            # Update work status
            worker_profile.work_status = work_status

            worker_profile.save(
                update_fields=[
                    'work_status',
                    'updated_at'
                ]
            )


            # Success message
            if work_status == 'AVAILABLE':

                messages.success(
                    request,
                    'You are now AVAILABLE for bookings.'
                )

            elif work_status == 'ON_DUTY':

                messages.success(
                    request,
                    'Your status is now ON DUTY.'
                )

            elif work_status == 'LEAVE':

                messages.success(
                    request,
                    'Your status is now ON LEAVE.'
                )


            return redirect(
                'worker_manage_dashboard'
            )


        # ======================================
        # DUTY TOGGLE
        # Toggle between AVAILABLE and ON_DUTY
        # ======================================

        elif action == 'duty':

            # Toggle duty status
            if worker_profile.work_status == 'ON_DUTY':
                worker_profile.work_status = 'AVAILABLE'
                messages.success(
                    request,
                    'You are now AVAILABLE for bookings.'
                )
            else:
                worker_profile.work_status = 'ON_DUTY'
                messages.success(
                    request,
                    'Your status is now ON DUTY.'
                )

            worker_profile.save(
                update_fields=[
                    'work_status',
                    'updated_at'
                ]
            )

            return redirect(
                'worker_manage_dashboard'
            )


        # ======================================
        # AVAILABILITY
        # Full Time / Part Time / Weekends / Flexible
        # ======================================

        elif action == 'availability':

            availability = request.POST.get(
                'availability',
                ''
            ).strip()


            # Check valid choice
            valid_values = [
                value
                for value, label
                in WorkerProfile.AVAILABILITY_CHOICES
            ]


            if availability not in valid_values:

                messages.error(
                    request,
                    'Please select a valid availability.'
                )

                return redirect(
                    'worker_manage_dashboard'
                )


            # Update availability
            worker_profile.availability = availability

            worker_profile.save(
                update_fields=[
                    'availability',
                    'updated_at'
                ]
            )


            messages.success(
                request,
                f'Availability updated to {availability}.'
            )


            return redirect(
                'worker_manage_dashboard'
            )

        elif action == 'update_location':

            latitude = request.POST.get(
                'latitude',
                ''
            ).strip()

            longitude = request.POST.get(
                'longitude',
                ''
            ).strip()

            redirect_target = 'worker_dashboard'
            next_url = request.POST.get('next') or request.GET.get('next')
            if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                redirect_target = next_url

            if not latitude or not longitude:

                messages.error(
                    request,
                    'Unable to get your location. Please try again.'
                )

                return redirect(redirect_target)

            try:

                worker_profile.latitude = float(latitude)
                worker_profile.longitude = float(longitude)

                place_name = _get_place_name_from_coords(
                    worker_profile.latitude,
                    worker_profile.longitude
                )

                if place_name:
                    worker_profile.current_place = place_name

                worker_profile.save(
                    update_fields=[
                        'latitude',
                        'longitude',
                        'current_place',
                        'updated_at'
                    ]
                )

                success_msg = 'Current location updated successfully.'

                if place_name:
                    success_msg = f'Location updated: {place_name}'

                messages.success(
                    request,
                    success_msg
                )

            except (ValueError, TypeError):

                messages.error(
                    request,
                    'Invalid location data. Please try again.'
                )

            return redirect(redirect_target)


    # ==========================================
    # DASHBOARD DATA
    # ==========================================

    context = {

        # Worker profile
        'worker_profile': worker_profile,


        # Normal working availability choices
        'availability_choices':
            WorkerProfile.AVAILABILITY_CHOICES,


        # Current work status choices
        'work_status_choices':
            WorkerProfile.WORK_STATUS_CHOICES,


        # ======================================
        # JOB STATISTICS
        # ======================================

        'total_jobs': Booking.objects.filter(worker=worker_profile).count(),

        'pending_jobs': Booking.objects.filter(worker=worker_profile, status='PENDING').count(),

        'completed_jobs': Booking.objects.filter(worker=worker_profile, status='COMPLETED').count(),

        'total_earnings': Booking.objects.filter(
            worker=worker_profile,
            status='COMPLETED',
            is_paid=True
        ).aggregate(total=Sum('estimated_cost'))['total'] or 0,


        # Emergency Bookings: only this worker's own trade is broadcast here.
        # Showing every trade's requests would leak other customers'
        # locations/phones to workers who can never accept them.
        'emergency_bookings': Booking.objects.filter(
            status='EMERGENCY_PENDING',
            worker__isnull=True,
            service=worker_profile.service,
        ).select_related('customer', 'service').order_by('-created_at')[:10] if worker_profile.is_verified and worker_profile.work_status in ['AVAILABLE', 'ON_DUTY'] and worker_profile.service_id else [],

        # Jobs
        'jobs': [],
    }


    # ==========================================
    # RENDER
    # ==========================================

    return render(
        request,
        'workers/worker_manage_dashboard.html',
        context
    )
@never_cache
@login_required
def notifications_view(request):
    # Route workers and admins to their respective notification portals
    if getattr(request.user, 'role', None) == 'WORKER':
        return redirect('worker_notifications')
    elif getattr(request.user, 'role', None) == 'ADMIN' or getattr(request.user, 'is_staff', False) or getattr(request.user, 'is_superuser', False):
        return redirect('admin_notifications')

    notifications = Notification.objects.filter(
        recipient=request.user
    ).select_related(
        'booking',
        'booking__customer',
        'booking__service',
        'booking__worker__user'
    ).order_by('-created_at')

    unread_count = notifications.filter(is_read=False).count()

    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
        return JsonResponse({'unread_count': unread_count})

    paginator = Paginator(notifications, 20)
    page_number = request.GET.get('page', 1)
    notifications_page = paginator.get_page(page_number)

    return render(
        request,
        'notifications.html',
        {
            'notifications': notifications_page,
            'unread_count': unread_count,
        }
    )


@login_required
def mark_notification_read(request, notification_id):
    def get_redirect():
        referer = request.META.get('HTTP_REFERER')
        if referer:
            return redirect(referer)
        if getattr(request.user, 'role', None) == 'WORKER':
            return redirect('worker_notifications')
        elif getattr(request.user, 'role', None) == 'ADMIN' or getattr(request.user, 'is_staff', False) or getattr(request.user, 'is_superuser', False):
            return redirect('admin_notifications')
        return redirect('notifications')

    if request.method != 'POST':
        return get_redirect()

    notification = get_object_or_404(
        Notification,
        id=notification_id,
        recipient=request.user
    )
    notification.is_read = True
    notification.save(update_fields=['is_read'])

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        unread_count = Notification.objects.filter(
            recipient=request.user,
            is_read=False
        ).count()
        return JsonResponse({
            'success': True,
            'unread_count': unread_count,
        })

    return get_redirect()


@login_required
def mark_all_notifications_read(request):
    def get_redirect():
        referer = request.META.get('HTTP_REFERER')
        if referer:
            return redirect(referer)
        if getattr(request.user, 'role', None) == 'WORKER':
            return redirect('worker_notifications')
        elif getattr(request.user, 'role', None) == 'ADMIN' or getattr(request.user, 'is_staff', False) or getattr(request.user, 'is_superuser', False):
            return redirect('admin_notifications')
        return redirect('notifications')

    if request.method != 'POST':
        return get_redirect()

    Notification.objects.filter(
        recipient=request.user,
        is_read=False
    ).update(is_read=True)

    messages.success(request, 'All notifications marked as read.')

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return JsonResponse({'success': True})

    return get_redirect()


# ==========================================================
# INVOICES / RECEIPTS
# ==========================================================

@never_cache
@login_required
def booking_invoice(request, booking_id):
    """Print-friendly receipt for a single booking.

    Visible to the customer who booked it, the assigned worker and any admin.
    """
    booking = get_object_or_404(
        Booking.objects.select_related('customer', 'worker__user', 'service'),
        id=booking_id
    )

    is_admin = request.user.role == 'ADMIN'
    is_customer = booking.customer_id == request.user.id
    is_worker = bool(booking.worker_id) and booking.worker.user_id == request.user.id

    if not (is_admin or is_customer or is_worker):
        messages.error(request, 'You are not authorized to view this receipt.')
        return redirect('dashboard')

    payment = getattr(booking, 'payment', None)

    return render(
        request,
        'user/invoice.html',
        {
            'booking': booking,
            'payment': payment,
            'is_admin': is_admin,
        }
    )


# ==========================================================
# RECURRING SERIES MANAGEMENT
# ==========================================================

@never_cache
@login_required
def my_series(request):
    """List the customer's recurring bookings with pause/resume/cancel."""
    all_series = BookingSeries.objects.filter(
        customer=request.user
    ).select_related('service', 'worker__user').prefetch_related('bookings')

    active_series = [s for s in all_series if s.is_active]
    paused_series = [s for s in all_series if not s.is_active]

    return render(
        request,
        'user/my_series.html',
        {
            'active_series': active_series,
            'paused_series': paused_series,
        }
    )


@login_required
def series_toggle_active(request, series_id):
    """Pause or resume a recurring series (POST only)."""
    if request.method != 'POST':
        return redirect('my_series')

    series = get_object_or_404(
        BookingSeries,
        id=series_id,
        customer=request.user
    )

    series.is_active = not series.is_active
    series.save(update_fields=['is_active', 'updated_at'])

    if series.is_active:
        messages.success(
            request,
            f'"{series.service.name}" series has been resumed.'
        )
    else:
        messages.success(
            request,
            f'"{series.service.name}" series has been paused.'
        )

    return redirect('my_series')


@login_required
def series_cancel(request, series_id):
    """Cancel a recurring series and its still-pending bookings (POST only)."""
    if request.method != 'POST':
        return redirect('my_series')

    series = get_object_or_404(
        BookingSeries,
        id=series_id,
        customer=request.user
    )

    today = timezone.now().date()
    open_statuses = ['PENDING', 'ACCEPTED', 'EMERGENCY_PENDING']

    # Only future, not-yet-finished bookings are touched. Completed work and
    # anything already in progress is deliberately left alone. Each visit is
    # cancelled individually so the worker gets back the commission they
    # prepaid for accepted visits (customer-initiated cancellation).
    cancelled = 0
    with transaction.atomic():
        visits = list(
            series.bookings.select_for_update().filter(
                date__gte=today,
                status__in=open_statuses
            )
        )
        for visit in visits:
            visit.status = 'CANCELLED'
            visit.cancelled_by = 'CUSTOMER'
            if not visit.cancellation_reason:
                visit.cancellation_reason = 'Recurring series cancelled by customer'
            visit.save(update_fields=['status', 'cancelled_by', 'cancellation_reason', 'updated_at'])
            _refund_booking_commission(visit, 'Customer cancelled recurring series')
            cancelled += 1

        series.is_active = False
        series.save(update_fields=['is_active', 'updated_at'])

    if cancelled:
        messages.success(
            request,
            f'"{series.service.name}" series cancelled - {cancelled} upcoming '
            'booking(s) were cancelled.'
        )
    else:
        messages.success(
            request,
            f'"{series.service.name}" series cancelled.'
        )

    return redirect('my_series')


# ==========================================================
# WORKER EARNINGS
# ==========================================================

@never_cache
@login_required
def worker_earnings(request):
    """Earnings breakdown for the signed-in worker."""
    if request.user.role != 'WORKER':
        messages.error(request, 'Only workers can view earnings.')
        return redirect('dashboard')

    worker_profile = get_object_or_404(WorkerProfile, user=request.user)

    paid_base = Booking.objects.filter(
        worker=worker_profile,
        status='COMPLETED',
        is_paid=True
    )

    total_earnings = paid_base.aggregate(total=Sum('estimated_cost'))['total'] or 0

    month_start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    this_month = paid_base.filter(
        updated_at__gte=month_start
    ).aggregate(total=Sum('estimated_cost'))['total'] or 0

    # Completed work that has not been paid out yet - needs admin attention.
    awaiting_payout = Booking.objects.filter(
        worker=worker_profile,
        status='COMPLETED',
        is_paid=False
    ).aggregate(total=Sum('estimated_cost'))['total'] or 0

    completed_count = Booking.objects.filter(
        worker=worker_profile,
        status='COMPLETED'
    ).count()

    avg_rating = Review.objects.filter(
        worker=worker_profile
    ).aggregate(avg=Avg('rating'))['avg'] or 0

    # Monthly paid totals. Ordered newest-first so the [:6] slice keeps the most
    # recent 6 months, then reversed so charts read oldest -> newest.
    monthly = [
        {
            'month': row['month'],
            'total': row['total'] or 0,
            'jobs': row['jobs'],
        }
        for row in paid_base.annotate(month=TruncMonth('updated_at'))
        .values('month')
        .annotate(total=Sum('estimated_cost'), jobs=Count('id'))
        .order_by('-month')[:6]
    ][::-1]

    paid_bookings = (
        Booking.objects.filter(
            worker=worker_profile,
            status='COMPLETED',
            is_paid=True
        )
        .select_related('customer', 'service')
        .order_by('-updated_at')[:100]
    )

    # Used to scale the monthly bar chart. widthratio returns 0 when this is 0,
    # so a brand-new worker sees empty bars rather than an error.
    max_month_total = max((float(m['total']) for m in monthly), default=0)

    return render(
        request,
        'workers/worker_earnings.html',
        {
            'worker_profile': worker_profile,
            'total_earnings': total_earnings,
            'this_month': this_month,
            'awaiting_payout': awaiting_payout,
            'completed_count': completed_count,
            'avg_rating': round(float(avg_rating), 1),
            'monthly': monthly,
            'max_month_total': max_month_total,
            'paid_bookings': paid_bookings,
        }
    )


# Characters that make Excel / LibreOffice / Google Sheets treat a cell as a
# formula rather than as text.
_CSV_FORMULA_TRIGGERS = '=+-@'

# Leading characters Excel silently discards before deciding whether a cell is
# a formula. "\t=1+1" is evaluated as a formula by Excel even though the first
# byte is a tab, so these must be stripped before testing - otherwise a tab or
# newline prefix is a trivial bypass.
_CSV_LEADING_NOISE = (
    '\t\r\n\x0b\x0c\x00\x01\x02\x03\x04\x05\x06\x07\x08'
    '\x0e\x0f\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19'
    '\x1a\x1b\x1c\x1d\x1e\x1f \xa0'
)


def _sanitize_csv_cell(text):
    """Neutralise spreadsheet formula injection in one cell.

    A leading apostrophe is the de-facto standard mitigation (OWASP): Excel and
    LibreOffice treat the rest of the cell as literal text, and the apostrophe
    itself is not displayed. Values that are already safe are returned
    unchanged, so the file stays readable.

    Idempotent by construction: once a value starts with an apostrophe the
    probe no longer begins with a trigger character, so re-sanitising a cell
    that was already sanitised cannot add a second apostrophe.
    """
    if not text:
        return text

    # Test what the spreadsheet will actually look at, not the raw first byte.
    #
    # Guard the empty case: in Python `'' in '=+-@'` is True, so without the
    # explicit check a whitespace-only cell ("   ") would be "escaped" even
    # though it contains nothing dangerous.
    probe = text.lstrip(_CSV_LEADING_NOISE)
    if not probe:
        return text

    if probe[0] in _CSV_FORMULA_TRIGGERS:
        return "'" + text

    return text


def _stringify(value):
    """Render a model field value safely for a CSV cell.

    This is the single choke point every exported cell passes through, so the
    formula-injection guard lives here and applies to every dataset column.
    """
    if value is None:
        return ''
    if value is True:
        return 'Yes'
    if value is False:
        return 'No'

    # Real numeric types are already safe: Excel parses "-123.45" as the number
    # -123.45, not as a formula. Escaping them would corrupt legitimate negative
    # amounts (refunds, adjustments) and stop spreadsheets summing the column.
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return str(value)

    return _sanitize_csv_cell(str(value))


def _resolve(obj, path):
    """Follow an ORM-style 'a__b__c' path across relations.

    Plain getattr() cannot cross relations, so field lists above would silently
    export blanks for every related column.
    """
    current = obj
    for part in path.split('__'):
        if current is None:
            return None
        current = getattr(current, part, None)
    return current


# Datasets an admin is allowed to export. Anything not listed here raises 404,
# so the view can never be pointed at an arbitrary model.
EXPORT_DATASETS = {
    'bookings': (
        Booking,
        [
            ('id', 'Booking ID'),
            ('created_at', 'Created'),
            ('date', 'Scheduled Date'),
            ('time', 'Time'),
            ('status', 'Status'),
            ('booking_type', 'Type'),
            ('service__name', 'Service'),
            ('customer__username', 'Customer'),
            ('customer__email', 'Customer Email'),
            ('worker__user__username', 'Worker'),
            ('district', 'District'),
            ('estimated_cost', 'Amount'),
            ('is_paid', 'Paid'),
        ],
    ),
    'payments': (
        Payment,
        [
            ('id', 'Payment ID'),
            ('created_at', 'Created'),
            ('booking__id', 'Booking ID'),
            ('booking__service__name', 'Service'),
            ('booking__customer__username', 'Customer'),
            ('amount', 'Amount'),
            ('status', 'Status'),
            ('payment_method', 'Method'),
            ('payment_reference', 'Reference'),
            ('gateway', 'Gateway'),
            ('paid_at', 'Paid At'),
        ],
    ),
    'complaints': (
        Complaint,
        [
            ('id', 'Complaint ID'),
            ('created_at', 'Created'),
            ('booking__id', 'Booking ID'),
            ('booking__service__name', 'Service'),
            ('customer__username', 'Customer'),
            ('customer__email', 'Customer Email'),
            ('subject', 'Subject'),
            ('severity', 'Severity'),
            ('status', 'Status'),
            ('admin_remark', 'Admin Remark'),
            ('description', 'Description'),
        ],
    ),
    'enquiries': (
        ContactEnquiry,
        [
            ('id', 'Enquiry ID'),
            ('created_at', 'Created'),
            ('name', 'Name'),
            ('email', 'Email'),
            ('phone', 'Phone'),
            ('subject', 'Subject'),
            ('message', 'Message'),
        ],
    ),
    'workers': (
        WorkerProfile,
        [
            ('id', 'Worker ID'),
            ('user__username', 'Username'),
            ('user__first_name', 'First Name'),
            ('user__last_name', 'Last Name'),
            ('user__email', 'Email'),
            ('phone', 'Phone'),
            ('service__name', 'Service'),
            ('district', 'District'),
            ('experience_years', 'Experience (Years)'),
            ('pricing_type', 'Pricing Type'),
            ('pricing_amount', 'Rate'),
            ('work_status', 'Work Status'),
            ('is_verified', 'Verified'),
            ('created_at', 'Registered Date'),
        ],
    ),
    'users': (
        CustomUser,
        [
            ('id', 'User ID'),
            ('username', 'Username'),
            ('first_name', 'First Name'),
            ('last_name', 'Last Name'),
            ('email', 'Email'),
            ('role', 'Role'),
            ('is_email_verified', 'Email Verified'),
            ('is_active', 'Active'),
            ('date_joined', 'Joined Date'),
        ],
    ),
}


@login_required
def admin_export_csv(request, dataset):
    """Download one of the admin datasets as a CSV file."""
    if request.user.role != 'ADMIN':
        messages.error(request, 'You are not authorized to export data.')
        return redirect('dashboard')

    if dataset not in EXPORT_DATASETS:
        raise Http404('Unknown export dataset: {0}'.format(dataset))

    model, fields = EXPORT_DATASETS[dataset]

    # Iterating the queryset (rather than building a list) keeps memory flat on
    # large tables; select_related avoids an N+1 per row.
    rows = model.objects.select_related().all().order_by('id')

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([header for _path, header in fields])

    count = 0
    for obj in rows.iterator(chunk_size=500):
        writer.writerow([
            _stringify(_resolve(obj, path)) for path, _h in fields
        ])
        count += 1

    filename = 'lamo-{0}-{1}.csv'.format(
        dataset,
        timezone.now().strftime('%Y%m%d-%H%M%S')
    )

    response = HttpResponse(buffer.getvalue(), content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="{0}"'.format(filename)

    logger.info(
        'Admin %s exported dataset %s (%s rows)',
        request.user.username, dataset, count
    )
    return response


@never_cache
@login_required
def worker_jobs(request):

    if request.user.role != 'WORKER':
        return redirect('dashboard')

    worker = get_object_or_404(
        WorkerProfile.objects.select_related(
            'user',
            'service'
        ),
        user=request.user
    )

    # Automatically expire any pending bookings past 30-min window
    check_and_expire_pending_bookings(worker=worker)
    check_and_expire_stale_accepted_bookings(worker=worker)

    # Get bookings assigned to this worker, prioritized by status and schedule
    today = timezone.localdate()
    active_statuses = ['PENDING', 'ACCEPTED', 'IN_PROGRESS', 'COMPLETION_REQUESTED', 'EMERGENCY_PENDING']

    bookings = Booking.objects.filter(
        worker=worker
    ).exclude(
        status='PENDING',
        series__isnull=False,
        series__is_active=False
    ).select_related(
        'customer',
        'service',
        'payment',
    ).annotate(
        priority_group=Case(
            When(status__in=active_statuses, date__gte=today, then=Value(1)),
            When(status__in=active_statuses, date__isnull=True, then=Value(1)),
            When(status__in=active_statuses, date__lt=today, then=Value(2)),
            default=Value(3),
            output_field=IntegerField(),
        )
    ).order_by('priority_group', 'date', '-created_at')

    paginator = Paginator(bookings, 20)
    page_number = request.GET.get('page', 1)
    bookings_page = paginator.get_page(page_number)

    # Get emergency bookings that are pending (no worker assigned yet).
    # Finding C4: Filter to worker's own trade and district, and block if wallet is locked.
    emergency_bookings = []
    wallet, _ = WorkerWallet.objects.get_or_create(worker=worker)
    if worker.is_verified and worker.work_status in ['AVAILABLE', 'ON_DUTY'] and worker.user.is_active and worker.service_id:
        if not wallet.is_locked:
            dismissed_booking_ids = Notification.objects.filter(
                recipient=request.user,
                notification_type='EMERGENCY',
                is_read=True
            ).values_list('booking_id', flat=True)

            em_qs = Booking.objects.filter(
                status='EMERGENCY_PENDING',
                worker__isnull=True,
                service=worker.service,
            ).exclude(id__in=dismissed_booking_ids)

            # Finding C4: Only broadcast within worker's registered district
            if worker.district:
                em_qs = em_qs.filter(district__iexact=worker.district.strip())

            emergency_bookings = em_qs.select_related('customer', 'service').order_by('-created_at')[:10]

    emergency_reason = None
    if not emergency_bookings:
        if wallet.is_locked:
            emergency_reason = "Wallet balance is low. Please recharge your wallet to receive emergency jobs."
        else:
            emergency_reason = _emergency_unavailable_reason(worker, request.user)

    transit_context = get_worker_transit_and_delay_context(worker, target_date=today)

    return render(
        request,
        'workers/worker_jobs.html',
        {
            'worker_profile': worker,
            'bookings': bookings_page,
            'emergency_bookings': emergency_bookings,
            'emergency_reason': emergency_reason,
            'transit_context': transit_context,
            'today': today,
        }
    )


@never_cache
@login_required
def worker_profile(request):

    if request.user.role != 'WORKER':
        return redirect('dashboard')

    worker = get_object_or_404(
        WorkerProfile.objects.select_related(
            'user',
            'service'
        ),
        user=request.user
    )

    reviews = Review.objects.filter(
        worker=worker
    ).select_related(
        'customer'
    ).order_by('-created_at')[:10]

    worker_complaints = Complaint.objects.filter(
        booking__worker=worker
    ).select_related(
        'customer',
        'booking'
    ).order_by('-created_at')[:10]

    return render(
        request,
        'workers/worker_profile.html',
        {
            'worker_profile': worker,
            'reviews': reviews,
            'worker_complaints': worker_complaints,
        }
    )



@never_cache
@login_required
def worker_notifications(request):

    # ==========================================
    # WORKER ONLY
    # ==========================================

    if request.user.role != 'WORKER':
        messages.error(
            request,
            'You are not authorized to access notifications.'
        )
        return redirect('dashboard')


    # ==========================================
    # WORKER PROFILE
    # ==========================================

    worker = get_object_or_404(
        WorkerProfile.objects.select_related(
            'user',
            'service'
        ),
        user=request.user
    )


    # ==========================================
    # GET WORKER NOTIFICATIONS
    # ==========================================

    notifications = (
        Notification.objects
        .filter(
            recipient=request.user
        )
        .select_related(
            'booking',
            'booking__customer',
            'booking__service'
        )
        .order_by('-created_at')
    )


    # ==========================================
    # UNREAD COUNT
    # ==========================================

    unread_count = notifications.filter(
        is_read=False
    ).count()

    paginator = Paginator(notifications, 20)
    page_number = request.GET.get('page', 1)
    notifications_page = paginator.get_page(page_number)

    # ==========================================
    # RENDER
    # ==========================================

    return render(
        request,
        'workers/worker_notifications.html',
        {
            'worker_profile': worker,
            'notifications': notifications_page,
            'unread_count': unread_count,
        }
    )


@never_cache
@login_required
def worker_update_status(request):

    if request.user.role != 'WORKER':
        messages.error(request, 'You are not authorized to update worker status.')
        return redirect('dashboard')

    worker_profile, created = WorkerProfile.objects.get_or_create(
        user=request.user,
        defaults={
            'phone': '',
            'district': 'Ernakulam',
            'availability': 'Full Time',
            'work_status': 'AVAILABLE',
            'address': ''
        }
    )

    # Check worker verification
    is_verified, error_msg = _check_worker_verification(worker_profile)
    if not is_verified:
        messages.error(request, error_msg)
        return redirect('worker_dashboard')

    if request.method == 'POST':

        work_status = request.POST.get('work_status', '').strip()

        # Workers cannot set themselves to SUSPENDED (admin-only)
        valid_statuses = [
            value
            for value, label
            in WorkerProfile.WORK_STATUS_CHOICES
            if value != 'SUSPENDED'
        ]

        if work_status not in valid_statuses:
            messages.error(request, 'Invalid work status.')
        else:
            worker_profile.work_status = work_status
            worker_profile.save(
                update_fields=[
                    'work_status',
                    'updated_at'
                ]
            )
            messages.success(
                request,
                f'Work status updated to {worker_profile.get_work_status_display()}.'
            )

    next_url = _safe_post_login_redirect(
        request,
        request.POST.get('next') or request.META.get('HTTP_REFERER'),
    )
    return redirect(next_url)

@never_cache
@login_required
def my_bookings(request):

    # Workers should not access the customer booking page
    if request.user.role == 'WORKER':
        return redirect('worker_jobs')

    # Automatically expire any pending bookings past 30-min window
    check_and_expire_pending_bookings(customer=request.user)
    check_and_expire_stale_accepted_bookings(customer=request.user)

    today = timezone.localdate()
    active_statuses = ['PENDING', 'ACCEPTED', 'IN_PROGRESS', 'COMPLETION_REQUESTED', 'EMERGENCY_PENDING']

    bookings_qs = (
        Booking.objects
        .filter(customer=request.user)
        .select_related(
            'worker__user',
            'worker__service',
            'service',
            'series',
            'review',
            'payment',
        )
        .annotate(
            priority_group=Case(
                When(status__in=active_statuses, date__gte=today, then=Value(1)),
                When(status__in=active_statuses, date__isnull=True, then=Value(1)),
                When(status__in=active_statuses, date__lt=today, then=Value(2)),
                default=Value(3),
                output_field=IntegerField(),
            )
        )
        .order_by('priority_group', 'date', '-created_at')
    )

    paginator = Paginator(bookings_qs, 20)
    page_number = request.GET.get('page', 1)
    bookings_page = paginator.get_page(page_number)

    # Bulk pre-fetch active same-day jobs for assigned workers to eliminate N+1 queries (Task 5)
    today_worker_ids = {
        b.worker_id for b in bookings_page
        if b.status in ('PENDING', 'ACCEPTED') and b.worker_id and b.date == today
    }
    active_jobs_by_worker = {}
    if today_worker_ids:
        active_jobs = Booking.objects.filter(
            worker_id__in=today_worker_ids,
            date=today,
            status__in=['IN_PROGRESS', 'COMPLETION_REQUESTED']
        ).select_related('service', 'worker__user')
        for aj in active_jobs:
            if aj.worker_id not in active_jobs_by_worker:
                active_jobs_by_worker[aj.worker_id] = aj

    # Attach dynamic UPI QR and direct payment info - only once the customer
    # has confirmed the final bill (payment instructions never appear earlier).
    for b in bookings_page:
        b.payment_reportable = booking_payment_reportable(b)
        if b.payment_reportable:
            b.upi_data = get_booking_upi_payment_data(b)
        if b.status in ('PENDING', 'ACCEPTED') and b.worker and b.date == today:
            b.transit_info = get_customer_booking_transit_status(b, active_jobs_by_worker=active_jobs_by_worker)

    return render(
        request,
        'user/my_bookings.html',
        {
            'bookings': bookings_page,
            'payment_allowed_statuses': MANUAL_PAYMENT_ALLOWED_STATUSES,
            'today': today,
        }
    )
@never_cache
@login_required
def admin_notifications(request):

    # ==========================================
    # ADMIN ONLY
    # ==========================================

    if request.user.role != 'ADMIN':

        messages.error(
            request,
            'You are not authorized to access admin notifications.'
        )

        return redirect('dashboard')


    # ==========================================
    # GET ADMIN NOTIFICATIONS
    # ==========================================

    notifications = Notification.objects.filter(
        recipient=request.user
    ).select_related(
        'booking',
        'booking__customer',
        'booking__worker__user',
        'booking__service'
    ).order_by(
        '-created_at'
    )


    # ==========================================
    # UNREAD COUNT
    # ==========================================

    unread_count = notifications.filter(
        is_read=False
    ).count()


    return render(
        request,
        'admin/notifications.html',
        {
            'notifications': notifications,
            'unread_count': unread_count,
        }
    )


@login_required
def admin_broadcast_announcement(request):
    """Admin tool to broadcast in-app notifications/alerts to all users, workers, or customers."""
    is_admin = _is_admin_account(request.user)   # plain is_staff is not an administrator
    if not is_admin:
        messages.error(request, 'You are not authorized to broadcast announcements.')
        return redirect('dashboard')

    if request.method == 'POST':
        target = request.POST.get('target', 'ALL').strip().upper()
        title = request.POST.get('title', '').strip()
        message_body = request.POST.get('message', '').strip()

        if not message_body:
            messages.error(request, 'Announcement message cannot be empty.')
            return redirect('admin_dashboard')

        header_prefix = f"📢 [{title}] " if title else "📢 [ANNOUNCEMENT] "
        formatted_message = f"{header_prefix}{message_body}"

        if target == 'WORKERS':
            recipients = CustomUser.objects.filter(role='WORKER', is_active=True)
            target_label = "all registered workers"
        elif target == 'CUSTOMERS':
            recipients = CustomUser.objects.filter(role='USER', is_active=True)
            target_label = "all registered customers"
        else:
            recipients = CustomUser.objects.filter(is_active=True)
            target_label = "all platform users"

        notif_list = [
            Notification(
                recipient=u,
                notification_type='SYSTEM',
                message=formatted_message,
                is_read=False
            )
            for u in recipients
        ]
        Notification.objects.bulk_create(notif_list)
        try:
            broadcast_web_push_async(
                users=recipients,
                title="📢 LAMO Announcement",
                body=formatted_message[:120],
                url="/dashboard/"
            )
        except Exception:
            pass

        messages.success(request, f'Broadcast announcement sent to {len(notif_list)} users ({target_label}).')
        return redirect('admin_dashboard')

    return redirect('admin_dashboard')


def get_booking_upi_payment_data(booking):
    """Generate dynamic NPCI-standard UPI QR Code and deep-link for direct customer-to-worker payment."""
    worker = booking.worker
    if not worker:
        return None

    payee_name = worker.user.get_full_name() or worker.user.username
    worker_upi = (getattr(worker, 'upi_id', '') or '').strip()

    amount = booking.final_amount or booking.estimated_cost or (booking.service.price if booking.service else None)
    # A booking without any configured amount has nothing to pay by QR.
    if amount is None:
        amount = Decimal('0.00')
    amount = Decimal(str(amount))
    amount_str = f"{amount:.2f}"

    qr_b64 = None
    upi_intent_url = ""

    if not worker_upi:
        return None

    if amount > 0:
        from urllib.parse import quote
        import io, base64
        encoded_name = quote(payee_name)
        upi_intent_url = f"upi://pay?pa={worker_upi}&pn={encoded_name}&am={amount_str}&cu=INR&tn=LAMO_Job_{booking.id}"

        try:
            import qrcode
            qr = qrcode.QRCode(box_size=5, border=2)
            qr.add_data(upi_intent_url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buffer = io.BytesIO()
            img.save(buffer, format="PNG")
            qr_b64 = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
        except Exception:
            qr_b64 = None

    return {
        'worker_upi': worker_upi,
        'payee_name': payee_name,
        'amount': amount,
        'upi_intent_url': upi_intent_url,
        'qr_b64': qr_b64,
    }


@login_required
def confirm_completion(request, booking_id):
    """Customer approves the worker's final bill (H7, step 4).

    Approving the bill is NOT proof of payment: the booking stays
    COMPLETION_REQUESTED ("awaiting payment") with bill_confirmed_at set, and
    only then are payment instructions (UPI QR / cash) shown. The booking is
    marked COMPLETED when the payment is verified - by the worker
    ("Payment received") or by an admin verifying a reported payment.
    """
    if request.method != 'POST':
        return redirect('my_bookings')

    with transaction.atomic():
        booking = get_object_or_404(
            Booking.objects.select_for_update().select_related('worker__user', 'service'),
            id=booking_id,
            customer=request.user
        )

        if booking.status != 'COMPLETION_REQUESTED':
            messages.error(
                request,
                'This booking is not waiting for completion confirmation.'
            )
            return redirect('my_bookings')

        if booking.bill_confirmed_at:
            messages.info(request, 'You have already confirmed this bill. Please complete the payment to your professional.')
            return redirect('my_bookings')

        booking.bill_confirmed_at = timezone.now()
        booking.save(update_fields=['bill_confirmed_at', 'updated_at'])

    bill_total = booking.final_amount or booking.estimated_cost or Decimal('0.00')

    if booking.worker:
        _create_idempotent_notification(
            recipient=booking.worker.user,
            booking=booking,
            notification_type='BOOKING_UPDATE',
            message=(
                f'The customer confirmed your final bill of ₹{bill_total:.2f} for Booking #{booking.id} '
                f'({booking.service.name}). Collect the payment and tap "Payment received" to close the job.'
            )
        )

        worker_email = booking.worker.user.email
        worker_username = booking.worker.user.username
        service_name = booking.service.name
        b_id = booking.id

        if worker_email:
            send_email_safely(
                subject=f'Bill Confirmed - Booking #{b_id}',
                message=(
                    f'Hi {worker_username},\n\n'
                    f'The customer has confirmed your final bill of ₹{bill_total:.2f} for your {service_name} work (Booking #{b_id}).\n\n'
                    f'Please collect the payment (cash or UPI) and mark it as received in your LAMO dashboard to complete the job.\n\n'
                    f'Best regards,\nTeam LAMO'
                ),
                recipient_list=[worker_email],
                purpose='job_completed_worker_notified',
                async_send=True,
            )

    messages.success(
        request,
        f'Bill confirmed. Please pay ₹{bill_total:.2f} to your professional by cash or UPI - '
        f'payment details are now shown on your booking.'
    )
    return redirect('my_bookings')


def _complete_booking_after_payment(booking_id, payment_method=None, verified_by='WORKER', reference=None):
    """Mark the payment verified and the booking COMPLETED (H7 steps 7-8).

    Only valid for a COMPLETION_REQUESTED booking whose bill the customer has
    confirmed (or a legacy unpaid COMPLETED booking). Locks the booking row so
    a worker confirmation and an admin verification cannot both run.
    Returns (booking, error_message).
    """
    with transaction.atomic():
        booking = Booking.objects.select_for_update().select_related(
            'customer', 'worker__user', 'service'
        ).get(id=booking_id)

        legacy_unpaid_completed = booking.status == 'COMPLETED' and not booking.is_paid
        if booking.status == 'COMPLETION_REQUESTED':
            if not booking.bill_confirmed_at:
                return booking, 'The customer has not confirmed the final bill yet.'
        elif not legacy_unpaid_completed:
            if booking.status == 'COMPLETED' and booking.is_paid:
                return booking, 'This booking is already completed and paid.'
            return booking, 'This booking is not awaiting payment.'

        method = (payment_method or booking.payment_method or 'CASH').upper()
        if verified_by == 'WORKER' and method not in ACTIVE_CUSTOMER_PAYMENT_METHODS:
            # New worker-recorded transactions only accept supported methods.
            # (Admin verification of an older customer report keeps its method
            # so historical CARD/NETBANKING records are not rewritten.)
            return booking, 'Unsupported payment method. Please record the payment as Cash or UPI.'
        if method not in MANUAL_PAYMENT_METHODS:
            method = 'CASH'

        final_amt = booking.final_amount or booking.estimated_cost or (
            booking.service.price if booking.service else Decimal('0.00')
        ) or Decimal('0.00')

        payment, _ = Payment.objects.select_for_update().get_or_create(booking=booking)
        payment.amount = final_amt
        payment.payment_method = method
        payment.status = 'PAID'
        payment.paid_at = timezone.now()
        if verified_by == 'WORKER':
            payment.gateway = 'direct_to_worker'
        if not payment.payment_reference:
            candidate_ref = (reference or '').strip()[:100]
            if candidate_ref and not Payment.objects.filter(payment_reference=candidate_ref).exclude(pk=payment.pk).exists():
                payment.payment_reference = candidate_ref
            else:
                payment.payment_reference = f"DIRECT-{booking.id}-{timezone.now().strftime('%Y%m%d%H%M%S%f')}"
        payment.save()

        if booking.final_amount and booking.final_amount > 0:
            booking.estimated_cost = booking.final_amount
        booking.payment_method = method
        booking.is_paid = True
        booking.status = 'COMPLETED'
        booking.save(update_fields=['status', 'is_paid', 'payment_method', 'estimated_cost', 'updated_at'])

    # Legacy bookings accepted before commission moved to acceptance are
    # settled once here (no-op when the fee was already charged).
    _settle_booking_completion_financials(booking)

    _create_idempotent_notification(
        recipient=booking.customer,
        booking=booking,
        notification_type='BOOKING_UPDATE',
        message=(
            (
                f'Payment of ₹{final_amt:.2f} for Booking #{booking.id} ({booking.service.name}) has been verified. '
                if verified_by == 'ADMIN' else
                f'Your professional has confirmed receiving ₹{final_amt:.2f} for Booking #{booking.id} ({booking.service.name}). '
            ) + 'Your job is complete - please leave a review.'
        )
    )
    if booking.worker and booking.worker.user:
        _create_idempotent_notification(
            recipient=booking.worker.user,
            booking=booking,
            notification_type='BOOKING_UPDATE',
            message=f'Booking #{booking.id} is complete. Payment of ₹{final_amt:.2f} has been recorded.'
        )
        if booking.worker.user.email:
            send_email_safely(
                subject=f'Job Completed - Booking #{booking.id}',
                message=(
                    f'Hi {booking.worker.user.username},\n\n'
                    f'Booking #{booking.id} ({booking.service.name}) is now complete and the payment of '
                    f'₹{final_amt:.2f} has been recorded.\n\nThank you for your excellent service!\n\n'
                    f'Best regards,\nTeam LAMO'
                ),
                recipient_list=[booking.worker.user.email],
                purpose='job_completed_worker_notified',
                async_send=True,
            )
    return booking, None


@login_required
@require_POST
def worker_confirm_payment_received(request, booking_id):
    """Worker confirms they received the customer's cash / UPI payment."""
    if request.user.role != 'WORKER':
        messages.error(request, 'You are not authorized.')
        return redirect('dashboard')

    worker = get_object_or_404(WorkerProfile, user=request.user)
    get_object_or_404(Booking, id=booking_id, worker=worker)

    # Only methods the platform supports for NEW transactions are accepted,
    # exactly as for customers. Nothing is silently converted to another
    # method; CARD/NETBANKING and unknown values are refused with a message.
    # NOTE: this records a *worker-reported* receipt of money (gateway
    # 'direct_to_worker'). It is not an independently verified payment.
    method = request.POST.get('payment_method', '').strip().upper()
    if method in ('CARD', 'NETBANKING'):
        messages.error(
            request,
            'Card and Netbanking payments are not supported. Please record the payment as Cash or UPI.'
        )
        return redirect('worker_jobs')
    if method and method not in ACTIVE_CUSTOMER_PAYMENT_METHODS:
        messages.error(request, 'Unsupported payment method. Please choose Cash or UPI.')
        return redirect('worker_jobs')
    if not method:
        # No method posted: reuse a supported method already on record, else ask.
        existing_payment = Payment.objects.filter(booking_id=booking_id).first()
        for candidate in (
            getattr(existing_payment, 'payment_method', None),
            getattr(Booking.objects.filter(id=booking_id).first(), 'payment_method', None),
        ):
            if (candidate or '').upper() in ACTIVE_CUSTOMER_PAYMENT_METHODS:
                method = candidate.upper()
                break
        if not method:
            messages.error(request, 'Please choose how the payment was received (Cash or UPI).')
            return redirect('worker_jobs')
    booking, error = _complete_booking_after_payment(booking_id, payment_method=method, verified_by='WORKER')
    if error:
        messages.error(request, error)
    else:
        messages.success(request, f'Payment recorded. Booking #{booking.id} is now complete.')
    return redirect('worker_jobs')


@login_required
@require_POST
def dispute_booking(request, booking_id):
    """Customer disputes the service completion or final amount."""
    if request.user.role != 'USER':
        messages.error(request, 'Only customers can dispute a booking.')
        return redirect('dashboard')

    dispute_reason = request.POST.get('dispute_reason', '').strip()
    if not dispute_reason:
        messages.error(request, 'Please provide a reason for disputing this booking.')
        return redirect('my_bookings')

    with transaction.atomic():
        booking = Booking.objects.select_for_update().select_related(
            'customer', 'worker__user', 'service'
        ).filter(id=booking_id, customer=request.user).first()

        if not booking:
            messages.error(request, 'Booking not found.')
            return redirect('my_bookings')

        # Row-locked check: dispute cannot overwrite a COMPLETED or CANCELLED booking
        if booking.status != 'COMPLETION_REQUESTED':
            messages.error(request, 'Disputes can only be raised when completion is requested.')
            return redirect('my_bookings')

        booking.status = 'DISPUTED'
        booking.dispute_reason = dispute_reason
        booking.save(update_fields=['status', 'dispute_reason', 'updated_at'])

        # Create high-severity complaint for admin investigation
        _get_or_create_complaint(
            booking=booking,
            customer=request.user,
            subject=f"Dispute on Booking #{booking.id}: {booking.service.name}",
            description=f"Customer disputed completion. Reason: {dispute_reason}",
            severity='HIGH'
        )

    if booking.worker and booking.worker.user:
        Notification.objects.create(
            recipient=booking.worker.user,
            booking=booking,
            notification_type='BOOKING_UPDATE',
            message=f"The customer has disputed completion on Booking #{booking.id}. Reason: {dispute_reason}"
        )

    admins = CustomUser.objects.filter(role='ADMIN', is_active=True)
    for admin in admins:
        Notification.objects.create(
            recipient=admin,
            booking=booking,
            notification_type='BOOKING_UPDATE',
            message=f"Dispute raised on Booking #{booking.id} ({booking.service.name}) by customer {request.user.username}."
        )

    messages.info(request, 'Your dispute has been submitted for admin review. Our support team will investigate.')
    return redirect('my_bookings')


@login_required
def worker_start_job(request, booking_id):

    if request.user.role != 'WORKER':
        return redirect('dashboard')

    if request.method != 'POST':
        return redirect('worker_dashboard')

    worker = get_object_or_404(WorkerProfile, user=request.user)

    # Check worker verification
    is_verified, error_msg = _check_worker_verification(worker)
    if not is_verified:
        messages.error(request, error_msg)
        return redirect('worker_dashboard')

    booking = get_object_or_404(
        Booking,
        id=booking_id,
        worker=worker
    )

    if booking.status != 'ACCEPTED':
        messages.error(
            request,
            'This job cannot be started at this stage.'
        )
        return redirect('worker_dashboard')

    # Arrival verification: Customer must either have verified via QR scan or provided their Arrival PIN (Finding D1)
    arrival_otp = request.POST.get('arrival_otp', '').strip()

    if not booking.arrival_verified_at and booking.status != 'IN_PROGRESS':
        if not arrival_otp:
            messages.error(
                request,
                'Customer arrival verification required. Please have the customer scan your Doorstep QR Code, or ask them for their 4-digit Arrival PIN.'
            )
            referer = request.META.get('HTTP_REFERER', '')
            if 'worker-jobs' in referer or 'worker_jobs' in referer:
                return redirect('worker_jobs')
            return redirect('worker_dashboard')

        # Brute-force protection: a 4-digit PIN has only 10,000 values, so
        # wrong guesses are counted per booking in the shared cache and PIN
        # entry is locked after ARRIVAL_PIN_MAX_ATTEMPTS failures.
        pin_key = arrival_pin_key(booking.id)
        if is_rate_limited(pin_key, ARRIVAL_PIN_MAX_ATTEMPTS):
            messages.error(
                request,
                f'Too many incorrect Arrival PIN attempts. PIN entry is locked for '
                f'{max(1, minutes_remaining(pin_key))} minute(s). Please ask the customer to scan your Doorstep QR Code instead.'
            )
            referer = request.META.get('HTTP_REFERER', '')
            if 'worker-jobs' in referer or 'worker_jobs' in referer:
                return redirect('worker_jobs')
            return redirect('worker_dashboard')

        if not booking.arrival_otp or not secrets.compare_digest(str(arrival_otp).strip(), str(booking.arrival_otp).strip()):
            locked, _mins = register_failure(pin_key, ARRIVAL_PIN_MAX_ATTEMPTS, ARRIVAL_PIN_LOCKOUT_SECONDS)
            if locked:
                messages.error(
                    request,
                    'Too many incorrect Arrival PIN attempts. PIN entry is now locked for 15 minutes. '
                    'Please ask the customer to scan your Doorstep QR Code instead.'
                )
            else:
                messages.error(
                    request,
                    'Invalid verification code. Please check the 4-digit Arrival PIN with the customer or have them scan your QR code.'
                )
            referer = request.META.get('HTTP_REFERER', '')
            if 'worker-jobs' in referer or 'worker_jobs' in referer:
                return redirect('worker_jobs')
            return redirect('worker_dashboard')

        clear(pin_key)

    # Schedule timing check: cannot start scheduled job before booking time window unless customer verified
    can_start, not_allowed_reason = booking.check_can_start(arrival_otp=arrival_otp, is_qr_verified=bool(booking.arrival_verified_at))
    if not can_start:
        messages.error(request, not_allowed_reason)
        referer = request.META.get('HTTP_REFERER', '')
        if 'worker-jobs' in referer or 'worker_jobs' in referer:
            return redirect('worker_jobs')
        return redirect('worker_dashboard')

    booking.status = 'IN_PROGRESS'
    if not booking.arrival_verified_at:
        booking.arrival_verified_at = timezone.now()
    booking.arrival_otp = None
    booking.save(update_fields=['status', 'arrival_verified_at', 'arrival_otp', 'updated_at'])

    # Notify customer that job has started
    Notification.objects.create(
        recipient=booking.customer,
        booking=booking,
        notification_type='BOOKING_UPDATE',
        message=f'Your {booking.service.name} job has been started by {request.user.get_full_name() or request.user.username}.'
    )

    messages.success(
        request,
        'Arrival verified! Job started successfully.'
    )

    referer = request.META.get('HTTP_REFERER', '')
    if 'worker-jobs' in referer or 'worker_jobs' in referer:
        return redirect('worker_jobs')
    return redirect('worker_dashboard')


@login_required
def verify_doorstep_qr(request, token):
    """
    Landing and verification endpoint when a customer scans the worker's Doorstep QR code.
    Enforces customer-only verification and blocks worker self-verification (Finding D1).
    """
    booking_id, worker_id = Booking.verify_arrival_token(token)
    if not booking_id:
        return render(request, 'user/verify_doorstep_arrival.html', {
            'invalid_token': True,
            'error_title': 'Invalid or Expired QR Code',
            'error_message': 'This Doorstep QR Code is either invalid or has expired. Please ask your service professional to refresh their screen and display a fresh QR code.'
        }, status=400)

    try:
        booking = Booking.objects.select_related('worker__user', 'service', 'customer').get(id=booking_id)
    except Booking.DoesNotExist:
        return render(request, 'user/verify_doorstep_arrival.html', {
            'invalid_token': True,
            'error_title': 'Booking Not Found',
            'error_message': 'The booking associated with this QR code could not be found.'
        }, status=404)

    if worker_id and booking.worker_id and worker_id != booking.worker_id:
        return render(request, 'user/verify_doorstep_arrival.html', {
            'invalid_token': True,
            'error_title': 'Worker Assignment Mismatch',
            'error_message': 'This QR code does not belong to the technician currently assigned to this booking.'
        }, status=400)

    # Finding D1: Prevent worker from scanning their own QR code
    if request.user.role == 'WORKER' or (booking.worker and booking.worker.user == request.user):
        return render(request, 'user/verify_doorstep_arrival.html', {
            'invalid_token': True,
            'error_title': 'Self-Verification Prohibited',
            'error_message': 'Service professionals cannot verify their own arrival. Please ask the customer to scan the Doorstep QR Code from their own device.'
        }, status=403)

    # Finding D1: Enforce customer-only scanning (or admin)
    if request.user != booking.customer and request.user.role != 'ADMIN':
        return render(request, 'user/verify_doorstep_arrival.html', {
            'invalid_token': True,
            'error_title': 'Unauthorized Customer Account',
            'error_message': 'You are logged in as a different user. Only the customer who booked this service can verify the technician\'s arrival.'
        }, status=403)

    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'

    if request.method == 'POST':
        with transaction.atomic():
            booking = Booking.objects.select_for_update().select_related('worker__user', 'service', 'customer').get(id=booking_id)
            if booking.status != 'ACCEPTED':
                if booking.status == 'IN_PROGRESS':
                    if is_ajax:
                        return JsonResponse({'success': True, 'already_active': True, 'message': 'Job is already in progress.'})
                    return render(request, 'user/verify_doorstep_arrival.html', {
                        'booking': booking,
                        'already_active': True,
                        'token': token
                    })
                error_msg = f'This booking cannot be verified at this stage (current status: {booking.status}).'
                if is_ajax:
                    return JsonResponse({'success': False, 'message': error_msg})
                return render(request, 'user/verify_doorstep_arrival.html', {
                    'booking': booking,
                    'error_message': error_msg,
                    'token': token
                })

            # Check start eligibility (customer scanned QR, so early arrival is explicitly consented!)
            can_start, not_allowed_reason = booking.check_can_start(is_qr_verified=True)
            if not can_start:
                if is_ajax:
                    return JsonResponse({'success': False, 'message': not_allowed_reason})
                return render(request, 'user/verify_doorstep_arrival.html', {
                    'booking': booking,
                    'error_message': not_allowed_reason,
                    'token': token
                })

            # Transition booking to IN_PROGRESS
            booking.status = 'IN_PROGRESS'
            booking.arrival_verified_at = timezone.now()
            booking.arrival_otp = None
            booking.save(update_fields=['status', 'arrival_verified_at', 'arrival_otp', 'updated_at'])

        # Create notifications
        worker_user = booking.worker.user if booking.worker else None
        worker_name = (worker_user.get_full_name() or worker_user.username) if worker_user else 'Assigned Professional'
        cust_name = (booking.customer.get_full_name() or booking.customer.username) if booking.customer else 'Customer'

        if worker_user:
            Notification.objects.create(
                recipient=worker_user,
                booking=booking,
                notification_type='BOOKING_UPDATE',
                message=f'Doorstep verified! {cust_name} scanned your QR code and confirmed your arrival. Booking #{booking.id} is now IN PROGRESS.'
            )

        if booking.customer:
            Notification.objects.create(
                recipient=booking.customer,
                booking=booking,
                notification_type='BOOKING_UPDATE',
                message=f'Technician verified! {worker_name} has been admitted and work has started on your {booking.service.name}.'
            )

        if is_ajax:
            return JsonResponse({
                'success': True,
                'message': 'Technician verified! Service is now in progress.',
                'verified_at': booking.arrival_verified_at.strftime('%I:%M %p')
            })

        return render(request, 'user/verify_doorstep_arrival.html', {
            'booking': booking,
            'just_verified': True,
            'token': token
        })

    # GET request: render verification screen
    can_start, not_allowed_reason = booking.check_can_start(is_qr_verified=True)
    return render(request, 'user/verify_doorstep_arrival.html', {
        'booking': booking,
        'token': token,
        'can_start': can_start,
        'not_allowed_reason': not_allowed_reason,
        'already_active': (booking.status == 'IN_PROGRESS')
    })


@login_required
def api_booking_doorstep_status(request, booking_id):
    """
    Finding D2: Polling API endpoint for worker's modal to check if customer confirmed arrival.
    Requires authentication and authorizes only the assigned worker, customer, or admin.
    """
    try:
        booking = Booking.objects.get(id=booking_id)
    except Booking.DoesNotExist:
        return JsonResponse({'error': 'Not found'}, status=404)

    is_worker = (booking.worker and booking.worker.user == request.user)
    is_customer = (booking.customer == request.user)
    is_admin = (request.user.role == 'ADMIN')

    if not (is_worker or is_customer or is_admin):
        return JsonResponse({'error': 'Unauthorized'}, status=403)

    return JsonResponse({
        'success': True,
        'booking_id': booking.id,
        'status': booking.status,
        'is_in_progress': (booking.status == 'IN_PROGRESS'),
        'arrival_verified_at': booking.arrival_verified_at.strftime('%I:%M %p') if booking.arrival_verified_at else None
    })


@login_required
def worker_booking_doorstep_qr(request, booking_id):
    """
    Finding D2: Local QR code generation endpoint.
    Generates and streams a PNG QR image locally without leaking URLs or tokens to third parties.
    Authorized only for assigned worker, booking customer, or admin.
    """
    try:
        booking = Booking.objects.select_related('worker__user', 'customer').get(id=booking_id)
    except Booking.DoesNotExist:
        return HttpResponseNotFound("Booking not found")

    is_worker = (booking.worker and booking.worker.user == request.user)
    is_customer = (booking.customer == request.user)
    is_admin = (request.user.role == 'ADMIN')

    if not (is_worker or is_customer or is_admin):
        return HttpResponseForbidden("Unauthorized")

    # A doorstep QR is only issued for a confirmed job on its scheduled day,
    # so it cannot be generated days ahead and shared.
    if booking.status != 'ACCEPTED' or (booking.date and booking.date != timezone.localdate()):
        return HttpResponseForbidden("The doorstep QR is available on the day of the job.")

    token = booking.get_arrival_token()
    verify_url = request.build_absolute_uri(reverse('verify_doorstep_qr', args=[token]))

    import io
    try:
        import qrcode
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=8,
            border=2,
        )
        qr.add_data(verify_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return HttpResponse(buf.getvalue(), content_type='image/png')
    except Exception as e:
        logger.error(f"Error generating local QR code for booking #{booking_id}: {e}")
        return HttpResponse("Error generating QR code", status=500)

FINAL_BILL_DB_MAX = Decimal('99999999.99')   # Booking.final_amount: max_digits=10, decimal_places=2


def _strict_money(raw, field_label, allow_zero=False):
    """Parse a rupee amount from untrusted input.

    Returns (Decimal, None) or (None, error_message). Only plain decimal text is
    accepted (digits with at most two decimal places). NaN, Infinity, exponent
    notation, signs, malformed text, more than two decimals, negatives, zero
    (unless ``allow_zero``) and anything above the database column limit are
    rejected - never rounded or silently replaced.
    """
    text = (raw or '').strip().replace(',', '')
    if text.startswith('-'):
        return None, f'{field_label} must be greater than zero.' if not allow_zero else f'{field_label} cannot be negative.'
    if re.fullmatch(r'\d+\.\d{3,}', text):
        return None, f'{field_label} cannot have more than 2 decimal places.'
    if not re.fullmatch(r'\d{1,12}(\.\d{1,2})?', text):
        return None, f'{field_label} must be a valid amount.'
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError, TypeError):
        return None, f'{field_label} must be a valid amount.'
    if not value.is_finite():
        return None, f'{field_label} must be a valid amount.'
    if value > FINAL_BILL_DB_MAX:
        return None, f'{field_label} is too large.'
    value = value.quantize(Decimal('0.01'))
    if value == 0 and not allow_zero:
        return None, f'{field_label} must be greater than zero.'
    return value, None


def _parse_final_bill(raw_labor, raw_material, raw_amount, estimate):
    """Validate the worker's final bill. Returns (final, material, error).

    Labour and material are validated separately. When neither a labour nor a
    total is posted, the bill defaults to the estimate (existing behaviour).
    Policy limits come from settings (see FINAL_BILL_* in settings.py); a bill
    above the optional absolute ceiling is rejected here, while the optional
    percentage limit is enforced by the caller so it can escalate to admins.
    """
    material = Decimal('0.00')
    if (raw_material or '').strip():
        material, err = _strict_money(raw_material, 'Material cost', allow_zero=True)
        if err:
            return None, None, err
    final = None
    if (raw_labor or '').strip():
        labor, err = _strict_money(raw_labor, 'Labour charge')
        if err:
            return None, None, err
        final = labor + material
    elif (raw_amount or '').strip():
        final, err = _strict_money(raw_amount, 'Final amount')
        if err:
            return None, None, err
        if material > final:
            return None, None, 'Material cost cannot be more than the final amount.'
    if final is None:
        final = estimate or Decimal('0.00')
        if final <= 0:
            return None, None, 'Please enter the labour charge for this job.'
        if material > final:
            return None, None, 'Material cost cannot be more than the final amount.'
    if final > FINAL_BILL_DB_MAX:
        return None, None, 'The final amount is too large.'
    ceiling_raw = str(getattr(settings, 'FINAL_BILL_MAX_AMOUNT', '') or '').strip()
    if ceiling_raw:
        try:
            ceiling = Decimal(ceiling_raw)
        except InvalidOperation:
            ceiling = None
        if ceiling is not None and ceiling.is_finite() and final > ceiling:
            return None, None, f'The final amount cannot exceed ₹{ceiling:.2f}. Please contact support.'
    return final, material, None


def _final_bill_needs_admin_review(final, estimate):
    """True when the bill exceeds the estimate by more than the configured percent."""
    limit_raw = str(getattr(settings, 'FINAL_BILL_MAX_ADJUSTMENT_PERCENT', '') or '').strip()
    if not limit_raw or not estimate or estimate <= 0:
        return False
    try:
        limit = Decimal(limit_raw)
    except InvalidOperation:
        return False
    if not limit.is_finite() or limit < 0:
        return False
    return (final - estimate) * Decimal('100') / estimate > limit


@login_required
def worker_request_completion(request, booking_id):

    # ==========================================
    # WORKER ONLY
    # ==========================================

    if request.user.role != 'WORKER':
        messages.error(
            request,
            'You are not authorized.'
        )
        return redirect('dashboard')


    # ==========================================
    # POST ONLY
    # ==========================================

    if request.method != 'POST':
        return redirect('worker_jobs')


    # ==========================================
    # GET WORKER PROFILE
    # ==========================================

    worker = get_object_or_404(
        WorkerProfile,
        user=request.user
    )

    # Check worker verification
    is_verified, error_msg = _check_worker_verification(worker)
    if not is_verified:
        messages.error(request, error_msg)
        return redirect('worker_dashboard')


    # ==========================================
    # LOCK THE BOOKING, RE-CHECK ITS STATE, THEN TRANSITION
    # ==========================================
    # The status is read again under a row lock and the write itself is a
    # compare-and-set (UPDATE ... WHERE status='IN_PROGRESS'), so two concurrent
    # or repeated requests produce exactly one IN_PROGRESS -> COMPLETION_REQUESTED
    # transition, one notification and one e-mail. A request that loses the race
    # changes nothing.
    get_object_or_404(Booking.objects.only('id'), id=booking_id, worker=worker)  # 404 for someone else's job

    with transaction.atomic():
        booking = get_object_or_404(
            Booking.objects.select_for_update().select_related('customer', 'service'),
            id=booking_id,
            worker=worker
        )

        # ONLY AN IN-PROGRESS JOB CAN REQUEST COMPLETION
        if booking.status != 'IN_PROGRESS':
            messages.warning(request, 'Only an in-progress job can request completion.')
            return redirect('worker_jobs')

        # Parse and validate labor, material and final service amount.
        # Every field is validated strictly and separately (no silent fallback
        # for malformed input); see _parse_final_bill. Direct POSTs get the same checks.
        estimate = booking.estimated_cost or (booking.service.price if booking.service else None)
        final_amount, material_amount, bill_error = _parse_final_bill(
            request.POST.get('labor_amount', ''),
            request.POST.get('material_amount', ''),
            request.POST.get('final_amount', ''),
            estimate,
        )
        if bill_error:
            messages.error(request, bill_error)
            return redirect('worker_jobs')

        # A bill far above the estimate is not sent to the customer: it is
        # escalated to the admins (threshold is optional - FINAL_BILL_MAX_ADJUSTMENT_PERCENT).
        if _final_bill_needs_admin_review(final_amount, estimate):
            for admin_user in CustomUser.objects.filter(role='ADMIN', is_active=True):
                _create_idempotent_notification(
                    recipient=admin_user,
                    booking=booking,
                    notification_type='BOOKING_UPDATE',
                    message=(
                        f'Review needed: worker {request.user.username} asked to bill ₹{final_amount:.2f} '
                        f'against an estimate of ₹{estimate:.2f} on Booking #{booking.id}. '
                        f'The bill was not sent to the customer.'
                    ),
                )
            messages.error(
                request,
                'This bill is much higher than the original estimate, so it needs admin review before '
                'it can be sent to the customer. Our team has been notified.'
            )
            return redirect('worker_jobs')

        # IN_PROGRESS -> COMPLETION_REQUESTED (compare-and-set). A new bill
        # always needs a fresh customer approval before payment.
        changed = Booking.objects.filter(pk=booking.pk, worker=worker, status='IN_PROGRESS').update(
            status='COMPLETION_REQUESTED',
            final_amount=final_amount,
            material_amount=material_amount,
            bill_confirmed_at=None,
            updated_at=timezone.now(),
        )
        if not changed:
            messages.warning(request, 'Only an in-progress job can request completion.')
            return redirect('worker_jobs')
        booking.status = 'COMPLETION_REQUESTED'
        booking.final_amount = final_amount
        booking.material_amount = material_amount
        booking.bill_confirmed_at = None


    # ==========================================
    # NOTIFY CUSTOMER + SEND EMAIL
    # ==========================================

    _create_idempotent_notification(
        recipient=booking.customer,
        booking=booking,
        notification_type='BOOKING_UPDATE',
        message=(
            f'Your {booking.service.name} booking has been '
            f'marked as completed by the worker with final amount ₹{final_amount:.2f}. '
            f'Please verify the work and confirm payment.'
        )
    )

    customer_email = booking.customer.email
    customer_username = booking.customer.username
    service_name = booking.service.name
    b_id = booking.id

    if customer_email:
        send_email_safely(
            subject=f'Job Completed - Please Verify Booking #{b_id}',
            message=(
                f'Hi {customer_username},\n\n'
                f'The worker has completed your {service_name} job with final amount ₹{final_amount:.2f}.\n\n'
                f'Please take a moment to verify the work and confirm in your LAMO dashboard, or submit a dispute if there are any issues.\n\n'
                f'Thank you for choosing LAMO!\n\n'
                f'Best regards,\nTeam LAMO'
            ),
            recipient_list=[customer_email],
            purpose='job_completion_requested',
            async_send=True,
        )


    # ==========================================
    # SUCCESS MESSAGE
    # ==========================================

    messages.success(
        request,
        f'Completion request submitted for ₹{final_amount:.2f}. The customer has been notified.'
    )


    return redirect('worker_jobs')

@login_required
def submit_review(request, booking_id):

    # ==========================================
    # CUSTOMER ONLY
    # ==========================================

    if request.user.role == 'WORKER':

        messages.error(
            request,
            'Workers cannot submit reviews.'
        )

        return redirect('dashboard')


    # ==========================================
    # GET CUSTOMER BOOKING
    # ==========================================

    booking = get_object_or_404(
        Booking,
        id=booking_id,
        customer=request.user
    )


    # ==========================================
    # ONLY COMPLETED BOOKINGS
    # ==========================================

    if booking.status != 'COMPLETED':

        messages.error(
            request,
            'You can review only completed bookings.'
        )

        return redirect('my_bookings')


    # ==========================================
    # CHECK EXISTING REVIEW
    # ==========================================

    existing_review = Review.objects.filter(
        booking=booking
    ).first()


    if existing_review:

        messages.info(
            request,
            'You have already reviewed this booking.'
        )

        return redirect('my_bookings')


    # ==========================================
    # SUBMIT REVIEW
    # ==========================================

    if request.method == 'POST':

        rating = request.POST.get('rating')
        comment = request.POST.get(
            'comment',
            ''
        ).strip()


        # --------------------------------------
        # VALIDATE RATING
        # --------------------------------------

        try:

            rating = int(rating)

        except (TypeError, ValueError):

            messages.error(
                request,
                'Please select a valid rating.'
            )

            return render(
                request,
                'user/submit_review.html',
                {
                    'booking': booking
                }
            )


        if rating < 1 or rating > 5:

            messages.error(
                request,
                'Rating must be between 1 and 5 stars.'
            )

            return render(
                request,
                'user/submit_review.html',
                {
                    'booking': booking
                }
            )


        # --------------------------------------
        # CREATE REVIEW (race-safe)
        # --------------------------------------
        #
        # The unique index on Review.booking is the final authority: if a
        # concurrent request already inserted, the INSERT is rejected and the
        # existing review is returned instead of raising IntegrityError (500).
        _review, created = _get_or_create_review(
            booking,
            request.user,
            rating,
            comment,
        )

        if not created:
            messages.info(request, 'You have already reviewed this booking.')
            return redirect('my_bookings')

        _notify_worker_of_review(request, booking, rating)

        messages.success(
            request,
            'Thank you! Your review has been submitted successfully.'
        )

        return redirect('my_bookings')


    # ==========================================
    # SHOW REVIEW PAGE
    # ==========================================

    return render(
        request,
        'user/submit_review.html',
        {
            'booking': booking
        }
    )


@login_required
def submit_feedback(request, booking_id):
    if request.user.role == 'WORKER':
        messages.error(request, 'Workers cannot submit feedback.')
        return redirect('dashboard')

    booking = get_object_or_404(
        Booking,
        id=booking_id,
        customer=request.user
    )

    if booking.status != 'COMPLETED':
        messages.error(request, 'You can submit feedback only for completed bookings.')
        return redirect('my_bookings')

    existing_review = Review.objects.filter(booking=booking).first()
    existing_complaint = Complaint.objects.filter(booking=booking, customer=request.user).first()

    if request.method == 'POST':
        rating = request.POST.get('rating')
        comment = request.POST.get('comment', '').strip()
        complaint_category = request.POST.get('complaint_category', '').strip()
        complaint_description = request.POST.get('complaint_description', '').strip()

        has_review = bool(rating)
        has_complaint = bool(complaint_category or complaint_description)

        if not has_review and not has_complaint:
            messages.error(request, 'Please provide a rating/complaint or both.')
            return render(request, 'user/submit_feedback.html', {
                'booking': booking,
                'existing_review': existing_review,
                'existing_complaint': existing_complaint,
            })

        if has_review:
            if existing_review:
                messages.info(request, 'You have already reviewed this booking.')
            else:
                try:
                    rating = int(rating)
                except (TypeError, ValueError):
                    messages.error(request, 'Please select a valid rating.')
                    return render(request, 'user/submit_feedback.html', {
                        'booking': booking,
                        'existing_review': existing_review,
                        'existing_complaint': existing_complaint,
                    })

                if rating < 1 or rating > 5:
                    messages.error(request, 'Rating must be between 1 and 5 stars.')
                    return render(request, 'user/submit_feedback.html', {
                        'booking': booking,
                        'existing_review': existing_review,
                        'existing_complaint': existing_complaint,
                    })

                # Race-safe create: the UNIQUE index on Review.booking is the
                # final authority, so a concurrent submission cannot 500 here.
                _review, review_created = _get_or_create_review(
                    booking,
                    request.user,
                    rating,
                    comment,
                )

                if not review_created:
                    messages.info(request, 'You have already reviewed this booking.')
                else:
                    _notify_worker_of_review(request, booking, rating)

        if has_complaint:
            if existing_complaint:
                messages.info(request, 'You have already raised a complaint for this booking.')
            else:
                if not complaint_category:
                    messages.error(request, 'Please select a complaint category.')
                    return render(request, 'user/submit_feedback.html', {
                        'booking': booking,
                        'existing_review': existing_review,
                        'existing_complaint': existing_complaint,
                    })

                if not complaint_description:
                    messages.error(request, 'Please describe your complaint.')
                    return render(request, 'user/submit_feedback.html', {
                        'booking': booking,
                        'existing_review': existing_review,
                        'existing_complaint': existing_complaint,
                    })

                # Race-safe create: Complaint has
                # unique_together = ('booking', 'customer'), so a concurrent
                # submission is rejected by the DB instead of raising.
                complaint, complaint_created = _get_or_create_complaint(
                    booking,
                    request.user,
                    complaint_category,
                    complaint_description,
                )

                if not complaint_created:
                    messages.info(
                        request,
                        'You have already raised a complaint for this booking.'
                    )
                else:
                    try:
                        from .ai_utils import classify_complaint_severity
                        severity = classify_complaint_severity(complaint_category, complaint_description)
                        if severity:
                            complaint.severity = severity
                            complaint.save(update_fields=['severity'])
                    except Exception:
                        pass

                    Notification.objects.create(
                        recipient=request.user,
                        booking=booking,
                        notification_type='SYSTEM',
                        message=f'Your complaint for Booking #{booking.id} has been submitted successfully.'
                    )

        messages.success(request, 'Thank you! Your feedback has been submitted successfully.')
        return redirect('my_bookings')

    return render(request, 'user/submit_feedback.html', {
        'booking': booking,
        'existing_review': existing_review,
        'existing_complaint': existing_complaint,
    })


@never_cache
@login_required
def worker_leave_management(request):
    if request.user.role != 'WORKER':
        messages.error(request, 'Only workers can manage leave.')
        return redirect('dashboard')

    worker_profile, created = WorkerProfile.objects.get_or_create(
        user=request.user,
        defaults={
            'phone': '',
            'district': 'Ernakulam',
            'availability': 'Full Time',
            'work_status': 'AVAILABLE',
            'address': ''
        }
    )

    # Check worker verification
    is_verified, error_msg = _check_worker_verification(worker_profile)
    if not is_verified:
        messages.error(request, error_msg)
        return redirect('worker_dashboard')

    leaves = []
    try:
        leaves = WorkerLeave.objects.filter(worker=worker_profile).order_by('-leave_date')
    except Exception:
        # WorkerLeave table may not exist yet (migration not applied)
        pass
    today = date.today()

    if request.method == 'POST':
        leave_date_str = request.POST.get('leave_date', '').strip()
        reason = request.POST.get('reason', '').strip()

        if not leave_date_str:
            messages.error(request, 'Please select a leave date.')
        else:
            from datetime import datetime
            try:
                leave_date = datetime.strptime(leave_date_str, '%Y-%m-%d').date()
                if leave_date < today:
                    messages.error(request, 'Cannot create leave for past dates.')
                else:
                    try:
                        if WorkerLeave.objects.filter(worker=worker_profile, leave_date=leave_date).exists():
                            messages.warning(request, 'You already have leave on this date.')
                        else:
                            active_bookings = Booking.objects.filter(
                                worker=worker_profile,
                                date=leave_date,
                                status__in=['PENDING', 'ACCEPTED', 'IN_PROGRESS', 'COMPLETION_REQUESTED']
                            )
                            if active_bookings.exists():
                                count = active_bookings.count()
                                messages.error(
                                    request,
                                    f'Cannot add leave on {leave_date.strftime("%d %b %Y")}: You have {count} active booking(s) scheduled for this date. Please reschedule, complete, or resolve them before marking leave.'
                                )
                            else:
                                WorkerLeave.objects.create(
                                    worker=worker_profile,
                                    leave_date=leave_date,
                                    reason=reason
                                )
                                messages.success(request, 'Leave date added successfully.')
                    except Exception:
                        messages.error(request, 'Leave feature is currently unavailable. Please run migrations.')
            except ValueError:
                messages.error(request, 'Invalid date format.')
        return redirect('worker_leave')

    return render(request, 'workers/worker_leave.html', {
        'worker_profile': worker_profile,
        'leaves': leaves,
        'today': today,
        'work_status_choices': WorkerProfile.WORK_STATUS_CHOICES,
    })


@login_required
def worker_leave_delete(request, leave_id):
    if request.user.role != 'WORKER':
        messages.error(request, 'Only workers can manage leave.')
        return redirect('dashboard')

    worker_profile = get_object_or_404(WorkerProfile, user=request.user)

    # Check worker verification
    is_verified, error_msg = _check_worker_verification(worker_profile)
    if not is_verified:
        messages.error(request, error_msg)
        return redirect('worker_dashboard')

    try:
        leave = get_object_or_404(WorkerLeave, id=leave_id, worker=worker_profile)
    except Exception:
        messages.error(request, 'Leave feature is currently unavailable. Please run migrations.')
        return redirect('worker_leave')

    from datetime import date
    if leave.leave_date < date.today():
        messages.error(request, 'Cannot delete past leave.')
        return redirect('worker_leave')

    if request.method == 'POST':
        leave.delete()
        messages.success(request, 'Leave deleted successfully.')
        return redirect('worker_leave')

    return redirect('worker_leave')


@login_required
def admin_booking_details(request):

    # =====================================================
    # ADMIN ONLY
    # =====================================================

    if request.user.role != 'ADMIN':

        messages.error(
            request,
            'You are not authorized to view bookings.'
        )

        return redirect('dashboard')


    # =====================================================
    # GET BOOKINGS
    # =====================================================

    bookings = (
        Booking.objects
        .select_related(
            'customer',
            'worker__user',
            'worker__service',
            'service'
        )
        .order_by(
            '-created_at'
        )
    )


    # =====================================================
    # STATUS FILTER
    # =====================================================

    status = request.GET.get(
        'status',
        ''
    ).strip()


    if status:

        bookings = bookings.filter(
            status=status
        )


    # =====================================================
    # SEARCH
    # =====================================================

    search = request.GET.get(
        'search',
        ''
    ).strip()


    if search:

        bookings = bookings.filter(
            Q(customer__username__icontains=search)
            |
            Q(customer__first_name__icontains=search)
            |
            Q(customer__last_name__icontains=search)
            |
            Q(worker__user__username__icontains=search)
            |
            Q(worker__user__first_name__icontains=search)
            |
            Q(worker__user__last_name__icontains=search)
            |
            Q(service__name__icontains=search)
        )


    # =====================================================
    # RENDER
    # =====================================================

    paginator = Paginator(bookings, 25)
    bookings_page = paginator.get_page(request.GET.get('page', 1))

    return render(
        request,
        'admin/booking_details.html',
        {
            'bookings': bookings_page,
            'selected_status': status,
            'search': search,
            'total_count': paginator.count,
        }
    )

@login_required
def admin_booking_view(request, booking_id):

    # ==========================================
    # ADMIN ONLY
    # ==========================================

    if request.user.role != 'ADMIN':
        messages.error(
            request,
            'You are not authorized to view booking details.'
        )
        return redirect('dashboard')

    # ==========================================
    # GET BOOKING
    # ==========================================

    booking = get_object_or_404(
        Booking.objects.select_related(
            'customer',
            'worker__user',
            'worker__service',
            'service'
        ),
        id=booking_id
    )

    if request.method == 'POST':
        action = request.POST.get('action', '').strip()
        remark = request.POST.get('remark', '').strip()

        if booking.status == 'DISPUTED':
            if action == 'resolve_complete':
                booking.status = 'COMPLETED'
                booking.save(update_fields=['status', 'updated_at'])
                _settle_booking_completion_financials(booking)

                Complaint.objects.filter(booking=booking).update(
                    status='RESOLVED',
                    admin_remark=remark or 'Dispute resolved: completion confirmed by admin.'
                )

                Notification.objects.create(
                    recipient=booking.customer,
                    booking=booking,
                    notification_type='BOOKING_UPDATE',
                    message=f"Dispute on Booking #{booking.id} resolved: Service completion verified by admin."
                )
                if booking.worker and booking.worker.user:
                    Notification.objects.create(
                        recipient=booking.worker.user,
                        booking=booking,
                        notification_type='BOOKING_UPDATE',
                        message=f"Dispute on Booking #{booking.id} resolved: Job marked completed."
                    )
                messages.success(request, f"Dispute for Booking #{booking.id} resolved as Completed.")
                return redirect('admin_booking_view', booking_id=booking.id)

            elif action == 'resolve_cancel':
                booking.status = 'CANCELLED'
                booking.cancellation_reason = f"Dispute resolved by admin: {remark or 'Cancelled by admin'}"
                booking.cancelled_by = 'ADMIN'
                booking.save(update_fields=['status', 'cancellation_reason', 'cancelled_by', 'updated_at'])
                # Work was already performed (dispute follows a completion
                # request), so commission is kept unless the admin decides to
                # return it.
                _handle_booking_cancellation_refund(
                    booking,
                    commission_refund=request.POST.get('refund_commission') in ('1', 'on', 'true'),
                    refund_reason='Admin dispute resolution',
                )

                Complaint.objects.filter(booking=booking).update(
                    status='RESOLVED',
                    admin_remark=remark or 'Dispute resolved: booking cancelled by admin.'
                )

                Notification.objects.create(
                    recipient=booking.customer,
                    booking=booking,
                    notification_type='BOOKING_UPDATE',
                    message=f"Dispute on Booking #{booking.id} resolved: Booking cancelled by admin."
                )
                if booking.worker and booking.worker.user:
                    Notification.objects.create(
                        recipient=booking.worker.user,
                        booking=booking,
                        notification_type='BOOKING_UPDATE',
                        message=f"Dispute on Booking #{booking.id} resolved: Booking cancelled by admin."
                    )
                messages.success(request, f"Dispute for Booking #{booking.id} resolved as Cancelled.")
                return redirect('admin_booking_view', booking_id=booking.id)

    # ==========================================
    # RENDER
    # ==========================================

    return render(
        request,
        'admin/admin_booking_view.html',
        {
            'booking': booking,
        }
    )

@login_required
def admin_reviews_complaints(request):

    if request.user.role != 'ADMIN':
        return redirect('dashboard')

    # ---------------------------------------------------------
    # FILTER
    # ---------------------------------------------------------

    selected_type = request.GET.get('type', 'all')
    search = request.GET.get('search', '').strip()

    # ---------------------------------------------------------
    # REVIEWS
    # ---------------------------------------------------------

    reviews = (
        Review.objects
        .select_related(
            'booking',
            'booking__service',
            'customer',
            'worker',
            'worker__user',
        )
        .order_by('-created_at')
    )

    # ---------------------------------------------------------
    # COMPLAINTS
    # ---------------------------------------------------------

    complaints = (
        Complaint.objects
        .select_related(
            'booking',
            'booking__service',
            'booking__worker',
            'booking__worker__user',
            'customer',
        )
        .annotate(
            severity_order=Case(
                When(severity='HIGH', then=Value(1)),
                When(severity='MEDIUM', then=Value(2)),
                When(severity='LOW', then=Value(3)),
                default=Value(2),
                output_field=IntegerField()
            )
        )
        .order_by('severity_order', '-created_at')
    )

    # ---------------------------------------------------------
    # SEARCH
    # ---------------------------------------------------------

    if search:

        reviews = reviews.filter(
            Q(customer__username__icontains=search) |
            Q(customer__first_name__icontains=search) |
            Q(customer__last_name__icontains=search) |
            Q(worker__user__username__icontains=search) |
            Q(worker__user__first_name__icontains=search) |
            Q(worker__user__last_name__icontains=search)
        )

        complaints = complaints.filter(
            Q(customer__username__icontains=search) |
            Q(customer__first_name__icontains=search) |
            Q(customer__last_name__icontains=search) |
            Q(booking__worker__user__username__icontains=search) |
            Q(booking__worker__user__first_name__icontains=search) |
            Q(booking__worker__user__last_name__icontains=search)
        )

    # ---------------------------------------------------------
    # COUNTS
    # ---------------------------------------------------------

    review_count = Review.objects.count()

    complaint_count = Complaint.objects.count()

    open_complaint_count = Complaint.objects.filter(
        status='OPEN'
    ).count()

    # ---------------------------------------------------------
    # PAGINATION
    # ---------------------------------------------------------

    reviews = Paginator(reviews, 25).get_page(request.GET.get('page', 1))
    complaints = Paginator(complaints, 25).get_page(request.GET.get('page', 1))

    # ---------------------------------------------------------
    # CONTEXT
    # ---------------------------------------------------------

    context = {
        'reviews': reviews,
        'complaints': complaints,

        'review_count': review_count,
        'complaint_count': complaint_count,
        'open_complaint_count': open_complaint_count,

        'selected_type': selected_type,
        'search': search,
    }

    return render(
        request,
        'admin/reviews_complaints.html',
        context
    )


CUSTOMER_CANCELLABLE_STATUSES = ('PENDING', 'ACCEPTED', 'EMERGENCY_PENDING')


@login_required
def cancel_booking(request, booking_id):
    if request.method != 'POST':
        return redirect('my_bookings')

    with transaction.atomic():
        # Enforce ownership with row-level lock: customer can only cancel their own bookings
        booking = get_object_or_404(
            Booking.objects.select_for_update().select_related('worker__user', 'service'),
            id=booking_id, customer=request.user
        )

        # Block invalid status transitions
        if not _is_valid_booking_transition(booking.status, 'CANCELLED'):
            messages.error(request, "This booking cannot be cancelled at its current status.")
            return redirect('my_bookings')

        # Customers may only cancel before the worker's arrival is verified.
        # Once work has started (IN_PROGRESS) a problem must go through a
        # dispute or support, not a self-service cancellation.
        if booking.status not in CUSTOMER_CANCELLABLE_STATUSES or booking.arrival_verified_at:
            messages.error(
                request,
                "This job has already started, so it can't be cancelled here. "
                "If there is a problem, please raise a dispute or contact support."
            )
            return redirect('my_bookings')

        cancellation_reason = request.POST.get('cancellation_reason', '').strip()
        if 'cancellation_reason' in request.POST and not cancellation_reason:
            messages.error(request, "Please provide a reason for cancelling this booking.")
            return redirect('my_bookings')
        if not cancellation_reason:
            cancellation_reason = 'Cancelled by customer'

        booking.status = 'CANCELLED'
        booking.cancellation_reason = cancellation_reason
        booking.cancelled_by = 'CUSTOMER'
        booking.save(update_fields=['status', 'cancellation_reason', 'cancelled_by', 'updated_at'])
        # Customer cancelled before work started: the worker is not at fault,
        # so their prepaid commission is returned.
        _handle_booking_cancellation_refund(
            booking, commission_refund=True, refund_reason='Customer cancelled before work started',
        )

        # Notify worker inside transaction
        if booking.worker and booking.worker.user:
            _create_idempotent_notification(
                recipient=booking.worker.user,
                booking=booking,
                notification_type='BOOKING_UPDATE',
                message=f"Booking #{booking.id} for {booking.service.name} was cancelled by the customer. Reason: {cancellation_reason}"
            )

        # Notify admins
        admins = CustomUser.objects.filter(role='ADMIN', is_active=True)
        for admin_user in admins:
            _create_idempotent_notification(
                recipient=admin_user,
                booking=booking,
                notification_type='BOOKING_UPDATE',
                message=f"Booking #{booking.id} ({booking.service.name}) was cancelled by customer {request.user.username} (Reason: {cancellation_reason})."
            )

        # Send email to worker on transaction commit
        if booking.worker and booking.worker.user and booking.worker.user.email:
            w_email = booking.worker.user.email
            w_username = booking.worker.user.username
            s_name = booking.service.name
            b_date = booking.date
            b_id = booking.id
            reason = cancellation_reason
            transaction.on_commit(lambda: send_email_safely(
                subject=f'Booking #{b_id} Cancelled',
                message=(
                    f'Hi {w_username},\n\n'
                    f'Unfortunately, the customer has cancelled Booking #{b_id} for {s_name} on {b_date}.\n'
                    f'Reason: {reason}\n\n'
                    f'No worries! You\'re available for new bookings. Feel free to check your dashboard for new opportunities.\n\n'
                    f'Best regards,\nTeam LAMO'
                ),
                recipient_list=[w_email],
                purpose='booking_cancelled',
                async_send=True,
            ))

    messages.success(request, f"Booking #{booking.id} has been cancelled successfully.")
    return redirect('my_bookings')


@login_required
@require_POST
def worker_cancel_booking(request, booking_id):
    """Allow an assigned worker to cancel an ACCEPTED booking before start with mandatory reason."""
    if request.user.role != 'WORKER':
        messages.error(request, 'You are not authorized.')
        return redirect('dashboard')

    worker = get_object_or_404(WorkerProfile, user=request.user)
    with transaction.atomic():
        booking = get_object_or_404(
            Booking.objects.select_for_update().select_related('customer', 'service'),
            id=booking_id, worker=worker
        )

        if booking.status != 'ACCEPTED':
            messages.error(request, 'Only accepted bookings before work begins can be cancelled by a worker.')
            return redirect('worker_jobs')

        cancellation_reason = request.POST.get('cancellation_reason', '').strip()
        if not cancellation_reason:
            messages.error(request, 'Please provide a reason for cancelling this booking.')
            return redirect('worker_jobs')

        # Decide the commission refund from the schedule *before* changing
        # anything: early cancellations are refunded, late ones forfeit it.
        # A job whose arrival was already verified never reaches here
        # (status would be IN_PROGRESS).
        refund_allowed = _worker_cancellation_refund_allowed(booking) and not booking.arrival_verified_at

        booking.status = 'CANCELLED'
        booking.cancellation_reason = cancellation_reason
        booking.cancelled_by = 'WORKER'
        booking.save(update_fields=['status', 'cancellation_reason', 'cancelled_by', 'updated_at'])
        _handle_booking_cancellation_refund(
            booking,
            commission_refund=refund_allowed,
            refund_reason='Worker cancelled well before the scheduled start',
        )
        if not refund_allowed:
            messages.warning(
                request,
                f'Because this job was cancelled less than '
                f'{getattr(settings, "WORKER_CANCEL_REFUND_MIN_HOURS", 12)} hours before the start time, '
                f'the platform fee is not refunded.'
            )

        # Record worker cancellation for admin review
        StrikeRecord.objects.create(
            user=request.user,
            worker=worker,
            booking=booking,
            strike_type='WORKER_LATE_CANCELLATION',
            reason=cancellation_reason,
            status='PENDING_REVIEW',
            evidence=f"Worker cancelled confirmed booking #{booking.id} on date {booking.date}."
        )

        # Notify customer
        _create_idempotent_notification(
            recipient=booking.customer,
            booking=booking,
            notification_type='BOOKING_UPDATE',
            message=f"The assigned worker had to cancel Booking #{booking.id} ({booking.service.name}). Reason: {cancellation_reason}. You can easily select another worker."
        )

        # Notify admin
        admins = CustomUser.objects.filter(role='ADMIN', is_active=True)
        for admin_user in admins:
            _create_idempotent_notification(
                recipient=admin_user,
                booking=booking,
                notification_type='BOOKING_UPDATE',
                message=f"Worker {request.user.username} cancelled confirmed Booking #{booking.id} ({cancellation_reason}). Logged for review."
            )

        customer_email = booking.customer.email
        customer_username = booking.customer.username
        service_name = booking.service.name
        b_id = booking.id
        reason = cancellation_reason

        if customer_email:
            transaction.on_commit(lambda: send_email_safely(
                subject=f'Booking #{b_id} Cancelled by Worker',
                message=(
                    f'Hi {customer_username},\n\n'
                    f'We regret to inform you that your worker had to cancel Booking #{b_id} for {service_name}.\n'
                    f'Reason: {reason}\n\n'
                    f'You can choose another available worker right now from your bookings page.\n\n'
                    f'Best regards,\nTeam LAMO'
                ),
                recipient_list=[customer_email],
                purpose='worker_booking_cancelled',
                async_send=True,
            ))

    messages.info(request, f"Booking #{booking.id} has been cancelled. The customer was notified.")
    return redirect('worker_jobs')


@login_required
@require_POST
def report_worker_no_show(request, booking_id):
    """Customer reports that worker failed to arrive (available 30 min after scheduled time)."""
    if request.user.role != 'USER':
        messages.error(request, 'Only customers can report a worker no-show.')
        return redirect('dashboard')

    booking = get_object_or_404(
        Booking.objects.select_related('customer', 'worker__user', 'service'),
        id=booking_id,
        customer=request.user
    )

    if booking.status != 'ACCEPTED':
        messages.error(request, 'Worker no-show can only be reported for confirmed/accepted bookings.')
        return redirect('my_bookings')

    if not booking.date:
        messages.error(request, 'Cannot report no-show on booking without scheduled date.')
        return redirect('my_bookings')

    # Validate 30-minute waiting window server-side
    from datetime import datetime as dt_cls, time as tm_cls
    from django.utils import timezone as dj_tz
    scheduled_time = booking.start_time
    if not scheduled_time and booking.time:
        s_t, _ = parse_time_range(booking.time)
        scheduled_time = s_t
    if not scheduled_time:
        scheduled_time = tm_cls(9, 0)
    scheduled_dt = dj_tz.make_aware(dt_cls.combine(booking.date, scheduled_time))
    allowed_time = scheduled_dt + timedelta(minutes=30)

    if dj_tz.now() < allowed_time:
        minutes_left = int((allowed_time - dj_tz.now()).total_seconds() / 60)
        messages.error(
            request,
            f"Worker no-show report becomes available 30 minutes after scheduled time. "
            f"Please wait another {max(1, minutes_left)} minute(s)."
        )
        return redirect('my_bookings')

    notes = request.POST.get('notes', '').strip() or 'Worker did not show up at scheduled service time.'

    booking.status = 'CANCELLED'
    booking.cancellation_reason = f"Worker No-Show reported by customer: {notes}"
    booking.cancelled_by = 'CUSTOMER_REPORTED_NO_SHOW'
    booking.worker_no_show_reported_at = dj_tz.now()
    booking.save(update_fields=[
        'status', 'cancellation_reason', 'cancelled_by',
        'worker_no_show_reported_at', 'updated_at'
    ])
    # The worker did not turn up: their commission is forfeited (an admin can
    # still return it after reviewing the strike record).
    _handle_booking_cancellation_refund(
        booking, commission_refund=False, refund_reason='Worker no-show',
    )

    # Create strike record pending admin review
    if booking.worker and booking.worker.user:
        StrikeRecord.objects.create(
            user=booking.worker.user,
            worker=booking.worker,
            booking=booking,
            strike_type='WORKER_NO_SHOW',
            reason=notes,
            status='PENDING_REVIEW',
            evidence=f"Customer reported worker no-show at {dj_tz.now().strftime('%Y-%m-%d %H:%M')}. Scheduled was {scheduled_dt.strftime('%Y-%m-%d %H:%M')}."
        )

        Notification.objects.create(
            recipient=booking.worker.user,
            booking=booking,
            notification_type='BOOKING_UPDATE',
            message=f"Customer reported a no-show on Booking #{booking.id}. This report has been submitted for admin review."
        )

    admins = CustomUser.objects.filter(role='ADMIN', is_active=True)
    for admin_user in admins:
        Notification.objects.create(
            recipient=admin_user,
            booking=booking,
            notification_type='BOOKING_UPDATE',
            message=f"No-show report filed by customer {request.user.username} against worker #{booking.worker_id} for Booking #{booking.id}."
        )

    messages.success(request, f"Worker no-show report recorded. Booking #{booking.id} cancelled. You may now book another worker.")
    return redirect('my_bookings')


@login_required
@require_POST
def worker_report_customer_no_show(request, booking_id):
    """Worker reports customer no-show with GPS location and 15-minute waiting evidence."""
    if request.user.role != 'WORKER':
        messages.error(request, 'You are not authorized.')
        return redirect('dashboard')

    worker = get_object_or_404(WorkerProfile, user=request.user)
    booking = get_object_or_404(
        Booking.objects.select_related('customer', 'service'),
        id=booking_id,
        worker=worker
    )

    if booking.status != 'ACCEPTED':
        messages.error(request, 'Customer no-show can only be reported for accepted jobs prior to start.')
        return redirect('worker_jobs')

    if not booking.date:
        messages.error(request, 'Invalid booking schedule.')
        return redirect('worker_jobs')

    from datetime import datetime as dt_cls, time as tm_cls
    from django.utils import timezone as dj_tz
    scheduled_time = booking.start_time
    if not scheduled_time and booking.time:
        scheduled_time, _ = parse_time_range(booking.time)
    if not scheduled_time:
        scheduled_time = tm_cls(9, 0)
    scheduled_dt = dj_tz.make_aware(dt_cls.combine(booking.date, scheduled_time))
    allowed_time = scheduled_dt + timedelta(minutes=15)

    if dj_tz.now() < allowed_time:
        minutes_left = int((allowed_time - dj_tz.now()).total_seconds() / 60)
        messages.error(
            request,
            f"Customer no-show requires at least 15 minutes waiting period past scheduled time. "
            f"Please wait another {max(1, minutes_left)} minute(s)."
        )
        return redirect('worker_jobs')

    notes = request.POST.get('notes', '').strip()
    if not notes:
        messages.error(request, 'Please provide details of your waiting attempt (e.g., door knocked, calls made).')
        return redirect('worker_jobs')

    worker_lat = _safe_coord(request.POST.get('latitude'))
    worker_lng = _safe_coord(request.POST.get('longitude'))
    evidence_parts = [f"Waiting notes: {notes}."]

    # GPS verification if booking coordinates exist
    if booking.latitude is not None and booking.longitude is not None:
        if worker_lat is None or worker_lng is None:
            messages.error(request, 'GPS location is required to verify arrival at customer premises.')
            return redirect('worker_jobs')
        dist_km = haversine_km(worker_lat, worker_lng, booking.latitude, booking.longitude)
        if dist_km > 1.0:
            messages.error(
                request,
                f"GPS verification failed: You appear to be {dist_km:.2f} km away from the customer address. "
                f"You must be on-site (within 1 km) to report a customer no-show."
            )
            return redirect('worker_jobs')
        evidence_parts.append(f"GPS verified on-site: {dist_km * 1000:.0f}m from target ({worker_lat:.5f}, {worker_lng:.5f}).")

    booking.status = 'CANCELLED'
    booking.cancellation_reason = f"Customer No-Show reported by worker: {notes}"
    booking.cancelled_by = 'WORKER_REPORTED_NO_SHOW'
    booking.customer_no_show_reported_at = dj_tz.now()
    booking.save(update_fields=[
        'status', 'cancellation_reason', 'cancelled_by',
        'customer_no_show_reported_at', 'updated_at'
    ])
    # The worker turned up and the customer was unavailable: the worker is
    # not at fault, so the prepaid commission is returned.
    _handle_booking_cancellation_refund(
        booking, commission_refund=True, refund_reason='Customer not available at the scheduled time',
    )

    evidence_str = " ".join(evidence_parts)
    StrikeRecord.objects.create(
        user=booking.customer,
        worker=worker,
        booking=booking,
        strike_type='CUSTOMER_NO_SHOW',
        reason=notes,
        status='PENDING_REVIEW',
        evidence=evidence_str
    )

    Notification.objects.create(
        recipient=booking.customer,
        booking=booking,
        notification_type='BOOKING_UPDATE',
        message=f"Your worker reported that you were unavailable at the scheduled time for Booking #{booking.id}. The booking was cancelled."
    )

    admins = CustomUser.objects.filter(role='ADMIN', is_active=True)
    for admin_user in admins:
        Notification.objects.create(
            recipient=admin_user,
            booking=booking,
            notification_type='BOOKING_UPDATE',
            message=f"Customer no-show reported by worker {request.user.username} on Booking #{booking.id}. Evidence: {evidence_str}"
        )

    messages.info(request, f"Customer no-show report recorded with verified evidence. Booking #{booking.id} cancelled.")
    return redirect('worker_jobs')


@login_required
def admin_contact_enquiries(request):
    """Admin view to list and manage contact enquiries"""
    if request.user.role != 'ADMIN':
        return redirect('dashboard')

    enquiries = ContactEnquiry.objects.all().order_by('-created_at')

    selected_status = request.GET.get('status', 'all')
    if selected_status in ('NEW', 'READ', 'RESOLVED'):
        enquiries = enquiries.filter(status=selected_status)

    # Count by status for filters
    total_count = ContactEnquiry.objects.count()
    new_count = ContactEnquiry.objects.filter(status='NEW').count()
    read_count = ContactEnquiry.objects.filter(status='READ').count()
    resolved_count = ContactEnquiry.objects.filter(status='RESOLVED').count()

    paginator = Paginator(enquiries, 25)
    enquiries_page = paginator.get_page(request.GET.get('page', 1))

    return render(request, 'admin/contact_enquiries.html', {
        'enquiries': enquiries_page,
        'selected_status': selected_status,
        'total_count': total_count,
        'new_count': new_count,
        'read_count': read_count,
        'resolved_count': resolved_count,
    })


@login_required
def admin_enquiry_detail(request, enquiry_id):
    """Admin view to see a single contact enquiry and update its status"""
    if request.user.role != 'ADMIN':
        return redirect('dashboard')

    enquiry = get_object_or_404(ContactEnquiry, id=enquiry_id)

    # Mark as READ if currently NEW
    if enquiry.status == 'NEW':
        enquiry.status = 'READ'
        enquiry.save(update_fields=['status'])

    if request.method == 'POST':
        new_status = request.POST.get('status', '').strip()
        if new_status in ('NEW', 'READ', 'RESOLVED'):
            enquiry.status = new_status
            enquiry.save(update_fields=['status', 'updated_at'])
            messages.success(request, f'Enquiry status updated to {new_status}.')
            return redirect('admin_enquiry_detail', enquiry_id=enquiry.id)

    return render(request, 'admin/enquiry_detail.html', {'enquiry': enquiry})


@login_required
def update_complaint_status(request, complaint_id):
    if request.user.role != 'ADMIN':
        messages.error(request, 'You are not authorized.')
        return redirect('dashboard')

    if request.method == 'POST':
        complaint = get_object_or_404(Complaint, id=complaint_id)
        status = request.POST.get('status', '').strip()
        admin_remark = request.POST.get('admin_remark', '').strip()

        valid_statuses = ['OPEN', 'UNDER_REVIEW', 'RESOLVED', 'REJECTED']
        if status in ['RESOLVED', 'REJECTED'] and not admin_remark:
            messages.error(request, 'Please provide an admin remark/reason before resolving or rejecting a complaint.')
            return redirect('admin_reviews_complaints')
        if status in valid_statuses:
            # Validate dispute-resolution inputs first, so a bad value leaves
            # the complaint and the booking untouched.
            dispute_booking_obj = complaint.booking
            pre_adjusted = None
            pre_payment_method = None
            payment_verified = request.POST.get('payment_verified') in ('1', 'on', 'true')
            if status == 'RESOLVED' and dispute_booking_obj and dispute_booking_obj.status == 'DISPUTED':
                pre_action = request.POST.get('dispute_action', '').strip().upper()
                pre_is_cancel = (pre_action == 'CANCEL') or (
                    not pre_action and admin_remark and admin_remark.strip().lower().startswith(('cancel', 'refund'))
                )
                if not pre_is_cancel:
                    raw_adjusted = request.POST.get('adjusted_amount', '').strip()
                    if raw_adjusted:
                        pre_adjusted, adj_error = _strict_money(raw_adjusted, 'Adjusted amount')
                        if adj_error:
                            messages.error(request, adj_error)
                            return redirect('admin_reviews_complaints')
                    if payment_verified:
                        pre_payment_method, pm_error = clean_payment_method(
                            request.POST.get('payment_method'), ACTIVE_CUSTOMER_PAYMENT_METHODS
                        )
                        if pm_error:
                            messages.error(request, pm_error)
                            return redirect('admin_reviews_complaints')
            with transaction.atomic():
                complaint = Complaint.objects.select_for_update().get(id=complaint_id)
                complaint.status = status
                complaint.admin_remark = admin_remark

                # Automatically resolve linked DISPUTED booking atomically if complaint is resolved
                booking = complaint.booking
                if booking and booking.status == 'DISPUTED' and status == 'RESOLVED':
                    locked_booking = Booking.objects.select_for_update().get(pk=booking.pk)
                    dispute_action = request.POST.get('dispute_action', '').strip().upper()

                    # Require explicit dispute_action, with fallback for backward compatibility
                    if dispute_action:
                        is_cancel = (dispute_action == 'CANCEL')
                    else:
                        is_cancel = bool(admin_remark and admin_remark.strip().lower().startswith(('cancel', 'refund')))

                    if locked_booking.status == 'DISPUTED':
                        if is_cancel:
                            locked_booking.status = 'CANCELLED'
                            locked_booking.cancellation_reason = f"Dispute resolved by admin: {admin_remark}"
                            locked_booking.cancelled_by = 'ADMIN'
                            locked_booking.save(update_fields=['status', 'cancellation_reason', 'cancelled_by', 'updated_at'])
                            # Disputes follow completed work: commission is kept unless
                            # the admin explicitly returns it.
                            _handle_booking_cancellation_refund(
                                locked_booking,
                                commission_refund=request.POST.get('refund_commission') in ('1', 'on', 'true'),
                                refund_reason='Admin dispute resolution',
                            )
                            complaint.admin_remark = (
                                f"{admin_remark}\n[Resolution by admin #{request.user.id}: CANCELLED]"
                            )[:4000]
                            booking = locked_booking
                        else:
                            if pre_adjusted is not None:
                                locked_booking.final_amount = pre_adjusted
                            locked_booking.status = 'COMPLETED'
                            locked_booking.save(update_fields=['status', 'final_amount', 'updated_at'])
                            _settle_booking_completion_financials(locked_booking)
                            paid_note = 'payment NOT confirmed (booking remains unpaid)'
                            if payment_verified:
                                _paid_booking, pay_error = _complete_booking_after_payment(
                                    locked_booking.id,
                                    payment_method=pre_payment_method,
                                    verified_by='ADMIN',
                                    reference=f'DISPUTE-{locked_booking.id}',
                                )
                                paid_note = (
                                    'payment verified by admin' if not pay_error
                                    else f'payment could not be recorded: {pay_error}'
                                )
                            logger.info(
                                'Dispute resolved by admin=%s booking=%s complaint=%s outcome=COMPLETED '
                                'adjusted_amount=%s %s',
                                request.user.id, locked_booking.id, complaint.id,
                                pre_adjusted, paid_note,
                            )
                            complaint.admin_remark = (
                                f"{admin_remark}\n[Resolution by admin #{request.user.id}: COMPLETED; "
                                f"{('amount set to Rs ' + str(pre_adjusted) + '; ') if pre_adjusted is not None else ''}"
                                f"{paid_note}]"
                            )[:4000]
                            booking = locked_booking

                complaint.save(update_fields=['status', 'admin_remark'])

            messages.success(request, f"Complaint #{complaint.id} status updated to {status}.")

            Notification.objects.create(
                recipient=complaint.customer,
                booking=complaint.booking,
                notification_type='SYSTEM',
                message=f"Your complaint regarding Booking #{complaint.booking.id} status was updated to '{status}'."
            )

            if complaint.booking.worker:
                _create_idempotent_notification(
                    recipient=complaint.booking.worker.user,
                    booking=complaint.booking,
                    notification_type='SYSTEM',
                    message=f"A complaint against you for Booking #{complaint.booking.id} has been updated to '{status}'. Admin remark: {admin_remark or 'No remarks'}."
                )

            # Complaint status update (Resend-aware; failures logged, never raised)
            send_email_safely(
                subject=f'Complaint #{complaint.id} - Status Update',
                message=(
                    f'Hi {complaint.customer.username},\n\n'
                    f'We wanted to let you know that your complaint regarding Booking #{complaint.booking.id} has been updated.\n\n'
                    f'Status: {status}\n'
                    f'Admin Remark: {admin_remark or "No remarks"}\n\n'
                    f'If you have any further concerns, please don\'t hesitate to reach out.\n\n'
                    f'Best regards,\nTeam LAMO'
                ),
                recipient_list=[complaint.customer.email],
                purpose='complaint_status_update',
                async_send=True,
            )
        else:
            messages.error(request, 'Invalid complaint status.')

    return redirect('admin_reviews_complaints')


@login_required
def ai_recommend_workers(request, service_id):
    if request.method not in ['POST', 'GET']:
        return JsonResponse({'success': False, 'message': 'Invalid request method.'}, status=405)

    # Server-side throttle. This endpoint triggers external Nominatim geocoding
    # calls, so an unthrottled loop would spend a third party's quota. The
    # counter lives in the cache, so wiping the session cookie does not reset it.
    allowed, retry_after = consume_ai_quota(
        ai_recommend_minute_key(request),
        AI_RECOMMEND_PER_MINUTE,
        60,
    )
    if not allowed:
        logger.warning(
            'AI recommend-workers rate limited (user=%s ip=%s)',
            request.user.pk,
            client_ip(request),
        )
        response = JsonResponse({
            'success': False,
            'message': 'Too many requests. Please wait a moment and try again.',
        }, status=429)
        response['Retry-After'] = str(retry_after)
        return response

    service = get_object_or_404(Service, pk=service_id, is_active=True)

    requirement = request.POST.get('requirement', '').strip() or request.GET.get('requirement', '').strip() or service.name
    district = request.POST.get('district', '').strip() or request.GET.get('district', '').strip() or request.session.get('district') or ''
    place = request.POST.get('place', '').strip() or request.GET.get('place', '').strip() or request.session.get('locality') or ''
    latitude = request.POST.get('latitude', '').strip() or request.GET.get('latitude', '').strip() or str(request.session.get('lat') or '')
    longitude = request.POST.get('longitude', '').strip() or request.GET.get('longitude', '').strip() or str(request.session.get('lng') or '')

    if not district:
        return JsonResponse({'success': False, 'message': 'Please select your district.'}, status=400)

    if (not latitude or not longitude) and place:
        from .location_data import get_locality_coordinates
        loc_lat, loc_lng = get_locality_coordinates(district, place)
        if loc_lat is not None and loc_lng is not None:
            latitude, longitude = str(loc_lat), str(loc_lng)

    customer_address = ''
    if getattr(request.user, 'user_profile', None):
        customer_address = request.user.user_profile.address or ''
        if not latitude and getattr(request.user.user_profile, 'latitude', None):
            latitude = str(request.user.user_profile.latitude)
        if not longitude and getattr(request.user.user_profile, 'longitude', None):
            longitude = str(request.user.user_profile.longitude)

    if not place and customer_address:
        place = customer_address.split(',')[0].strip() or customer_address.strip()

    recommendations = build_worker_recommendations(
        service,
        district=district,
        requirement=requirement,
        customer_place=place,
        customer_lat=latitude,
        customer_lng=longitude,
    )

    payload = []
    for item in recommendations:
        worker = item.get('worker')
        payload.append({
            'worker_id': item.get('worker_id') or (worker.id if worker else None),
            'worker_name': item.get('worker_name') or (worker.user.get_full_name() or worker.user.username if worker else 'Worker'),
            'match_score': item.get('match_score'),
            'distance_label': item.get('distance_label') or 'Exact distance unavailable',
            'location_label': item.get('location_label') or (worker.address if worker else ''),
            'availability': item.get('availability') or (worker.work_status if worker else 'AVAILABLE'),
            'verified': item.get('verified') if item.get('verified') is not None else (worker.is_verified if worker else True),
            'avg_rating': item.get('avg_rating'),
            'completed_jobs': item.get('completed_jobs') or 0,
            'reasons': item.get('reasons', []),
            'district': item.get('district'),
            'worker_image': (
                worker.profile_image.url if (worker and worker.profile_image)
                else (worker.user.profile_image.url if (worker and hasattr(worker.user, 'profile_image') and worker.user.profile_image) else '')
            ),
        })

    return JsonResponse({
        'success': True,
        'service_name': service.name,
        'district': district,
        'recommendations': payload,
        'message': 'AI recommendations generated successfully.'
    })


def ai_service_assistant(request):
    # ==================================================
    # POST ONLY
    # ==================================================
    if request.method != 'POST':
        return JsonResponse({
            'success': False,
            'message': 'Invalid request method.'
        }, status=400)

    # ==================================================
    # GET MESSAGE
    # ==================================================
    def as_text(value):
        """Coerce a JSON/form value to a trimmed string.

        The browser sends latitude/longitude as numbers (they come straight
        from the Geolocation API), so calling .strip() on them raised
        AttributeError. The broad except below then discarded the whole
        payload and the chat replied "Please enter a message."
        """
        if value is None:
            return ''
        return str(value).strip()

    try:
        import json

        if request.body:
            data = json.loads(
                request.body.decode('utf-8')
            )

            message = as_text(data.get('message'))
            district = as_text(data.get('district'))
            customer_lat = as_text(data.get('latitude'))
            customer_lng = as_text(data.get('longitude'))

        else:
            message = as_text(request.POST.get('message'))
            district = as_text(request.POST.get('district'))
            customer_lat = as_text(request.POST.get('latitude'))
            customer_lng = as_text(request.POST.get('longitude'))

    except Exception:
        message = as_text(request.POST.get('message'))
        district = as_text(request.POST.get('district'))
        customer_lat = as_text(request.POST.get('latitude'))
        customer_lng = as_text(request.POST.get('longitude'))

    # ==================================================
    # EMPTY MESSAGE
    # ==================================================
    if not message:
        return JsonResponse({
            'success': False,
            'message': 'Please enter a message.'
        })

    # ==================================================
    # RATE LIMIT (server-side, before any Gemini call)
    # ==================================================
    # Two independent windows, per (IP, identity) pair:
    #   * 10/min  - stops a burst / scripted loop
    #   * 60/hour - stops a slow drip that would never trip the burst limit
    #
    # Deliberately applied to guests as well as signed-in users. The guest
    # branches below return static text and make no Gemini call, but the
    # endpoint still parses input and queries the DB, so leaving it open
    # would be a free amplification primitive. The guest experience itself is
    # untouched - only the request rate is bounded.
    #
    # The counters live in the server-side cache, so deleting the session
    # cookie, flushing the session or opening a private window does not reset
    # them.
    for _key, _maximum, _window in (
        (ai_assistant_minute_key(request), AI_ASSISTANT_PER_MINUTE, 60),
        (ai_assistant_hour_key(request), AI_ASSISTANT_PER_HOUR, 3600),
    ):
        _allowed, _retry_after = consume_ai_quota(_key, _maximum, _window)
        if not _allowed:
            logger.warning(
                'AI assistant rate limited (authenticated=%s ip=%s)',
                request.user.is_authenticated,
                client_ip(request),
            )
            _response = JsonResponse({
                'success': False,
                'type': 'rate_limited',
                'message': (
                    'You are sending requests too quickly. '
                    'Please wait a moment and try again.'
                ),
            }, status=429)
            _response['Retry-After'] = str(_retry_after)
            return _response

    # ==================================================
    # GUEST USER
    # ==================================================
    if not request.user.is_authenticated:

        from .ai_utils import detect_user_language
        lang = detect_user_language(message)
        lower_message = message.lower().strip()

        # --------------------------------------------------
        # GREETINGS (English, Malayalam, Manglish)
        # --------------------------------------------------
        english_greetings = {
            'hi', 'hello', 'hey', 'helo', 'hai', 'hii',
            'good morning', 'good afternoon', 'good evening',
        }
        manglish_greetings = {
            'namaskaram', 'namaskaarom', 'namaskaaram',
            'sugamano', 'sukhamano', 'suhrutham',
        }
        # Malayalam script greetings - check via contains
        malayalam_greeting_substrings = [
            'നമസ്കാരം', 'സുഖമാണോ', 'ഹലോ',
        ]

        is_greeting = (
            lower_message in english_greetings
            or lower_message in manglish_greetings
            or any(s in message for s in malayalam_greeting_substrings)
        )

        if is_greeting:
            if lang == 'malayalam':
                reply_text = "നമസ്കാരം! 👋 ഞാൻ LAMO അസിസ്റ്റന്റ് ആണ്. ലോഗിൻ ചെയ്ത് സേവനദാതാക്കളെ കണ്ടെത്തൂ."
            elif lang == 'manglish':
                reply_text = "Namaskaram! 👋 Njan LAMO assistant aanu. Login cheythu service providers-ne kandeth!"
            else:
                reply_text = "Hello! 👋 How can I help you today?"
            return JsonResponse({'success': True, 'type': 'chat', 'message': reply_text})

        # --------------------------------------------------
        # HOW ARE YOU  (English, Manglish)
        # --------------------------------------------------
        how_are_you_triggers = [
            'how are you', 'sugamano', 'sukhamano', 'evide undayirunnu',
        ]
        malayalam_how_substrings = ['സുഖമാണോ', 'എന്തൊക്കെ']

        if (
            any(t in lower_message for t in how_are_you_triggers)
            or any(s in message for s in malayalam_how_substrings)
        ):
            if lang == 'malayalam':
                reply_text = "ഞാൻ നന്നായിരിക്കുന്നു! 😊 നിങ്ങളുടെ ഹോം സർവ്വീസ് ആവശ്യം എന്താണ്?"
            elif lang == 'manglish':
                reply_text = "Njan nannayi und! 😊 Enthu home service venam?"
            else:
                reply_text = "I'm doing great! 😊 How can I help you with your home service needs?"
            return JsonResponse({'success': True, 'type': 'chat', 'message': reply_text})

        # --------------------------------------------------
        # SERVICES LIST (English, Manglish, Malayalam)
        # --------------------------------------------------
        service_triggers = ['service', 'services', 'enthu service', 'enthu okke']
        malayalam_service_substrings = ['സേവനം', 'സേവനങ്ങൾ']

        if (
            any(t in lower_message for t in service_triggers)
            or any(s in message for s in malayalam_service_substrings)
        ):
            service_list = ", ".join(
                Service.objects.filter(is_active=True).values_list('name', flat=True)
            )
            if lang == 'malayalam':
                reply_text = f"ഞങ്ങൾ ഇപ്പോൾ ലഭ്യമാക്കുന്ന സേവനങ്ങൾ: {service_list}. ഏത് സഹായം വേണം? 😊"
            elif lang == 'manglish':
                reply_text = f"Njangalude services: {service_list}. Enthu venam? 😊"
            else:
                reply_text = f"We currently offer: {service_list}. Tell me what you need! 😊"
            return JsonResponse({'success': True, 'type': 'chat', 'message': reply_text})

        # --------------------------------------------------
        # ALL OTHER REQUESTS — ask user to log in
        # --------------------------------------------------
        if lang == 'malayalam':
            login_msg = "😊 ഞാൻ ശരിയായ ഹോം സർവ്വീസ് കണ്ടെത്താൻ സഹായിക്കാം. ദയവായി ലോഗിൻ ചെയ്യൂ."
        elif lang == 'manglish':
            login_msg = "Okay 😊 Njangalkk correct service kaanikkam. Login cheyyanam."
        else:
            login_msg = (
                "Sure! 😊 I can help you find the right home service. "
                "Please login to view available service providers and their details."
            )
        return JsonResponse({'success': True, 'type': 'login_required', 'message': login_msg})

    # ==================================================
    # LOGGED-IN USER
    # ==================================================
    try:

        from .ai_utils import (
            chat_with_gemini,
            detect_user_language,
            match_service_and_workers,
            _safe_float,
        )

        # --------------------------------------------------
        # GENERAL CHAT — greetings (English + Manglish + Malayalam)
        # --------------------------------------------------
        lower_message = message.lower()

        english_greetings = {'hi', 'hello', 'hey', 'helo', 'hai', 'hii'}
        manglish_greetings = {
            'namaskaram', 'namaskaarom', 'namaskaaram',
            'sugamano', 'sukhamano',
        }
        malayalam_greeting_substrings = ['നമസ്കാരം', 'ഹലോ', 'സുഖമാണോ']

        is_greeting = (
            lower_message in english_greetings
            or lower_message in manglish_greetings
            or any(s in message for s in malayalam_greeting_substrings)
        )

        if is_greeting:
            reply = chat_with_gemini(message)
            if not reply:
                lang = detect_user_language(message)
                if lang == 'malayalam':
                    reply = "നമസ്കാരം! 👋 ഞാൻ LAMO അസിസ്റ്റന്റ് ആണ്. ഇന്ന് എന്ത് സഹായം വേണം?"
                elif lang == 'manglish':
                    reply = "Namaskaram! 👋 Njan LAMO assistant aanu. Enthu sahayamanu vendathu?"
                else:
                    reply = "Hello! 👋 How can I help you today?"
            return JsonResponse({'success': True, 'type': 'chat', 'message': reply})

        # --------------------------------------------------
        # SERVICE MATCHING
        # --------------------------------------------------
        res = match_service_and_workers(
            message,
            district=district,
            customer_lat=customer_lat or None,
            customer_lng=customer_lng or None,
        )

        matched_service = res['service']
        workers = res['workers']

        if not matched_service:
            lang = detect_user_language(message)
            if lang == 'malayalam':
                no_match_msg = "ക്ഷമിക്കണം, നിങ്ങളുടെ അഭ്യർത്ഥനയ്ക്ക് ഒരു സേവനവും കണ്ടെത്താനായില്ല."
            elif lang == 'manglish':
                no_match_msg = "Sorry, ningalude request-inu matching service onnum kittiyilla."
            else:
                no_match_msg = "Sorry, I couldn't find a matching service for your request."
            return JsonResponse({'success': False, 'message': no_match_msg})

        # --------------------------------------------------
        # WORKER DATA
        # --------------------------------------------------
        workers_data = []

        customer_lat_float = _safe_float(customer_lat)
        customer_lng_float = _safe_float(customer_lng)

        for w in workers:
            distance_km, distance_label = _compute_worker_distance(
                customer_lat_float,
                customer_lng_float,
                w
            )

            workers_data.append({
                'id': w.id,

                'name': (
                    w.user.get_full_name()
                    or w.user.username
                ),

                'district': w.district,

                'experience': w.experience_years,

                'rating': (
                    round(w.avg_rating, 1)
                    if getattr(
                        w,
                        'avg_rating',
                        None
                    )
                    else 'New'
                ),

                'work_status': w.work_status,

                'distance_km': round(distance_km, 1) if distance_km is not None else None,

                'distance_label': distance_label,
            })

        return JsonResponse({
            # The assistant widget checks data.success; without it every
            # successful match was shown as "Could not find matching service".
            'success': True,
            'type': 'service_match',
            'status': 'success',
            'service': {
                'id': matched_service.id,
                'name': matched_service.name,
                'description': matched_service.description,
                'price': (
                    str(matched_service.price)
                    if matched_service.price
                    else None
                ),
            },
            'workers': workers_data,
            'query': message
        })

    except Exception as exc:

        # This used to be print("AI Service Assistant Error:", e), which wrote
        # the raw exception to stdout and to logs/lamo.log via the root file
        # handler. Only the exception *class* is logged, matching the
        # convention already used in ai_utils._call_gemini_api(): exc.args can
        # embed the API key or the request body, so neither the message nor
        # exc_info=True (which would reprint str(exc) in the traceback tail) is
        # safe here. The user-visible response was already generic and is
        # unchanged.
        logger.error(
            'ai_service_assistant_failed exc_type=%s',
            type(exc).__name__,
        )

        return JsonResponse({

            'success': False,

            'message': (
                'Sorry, something went wrong. '
                'Please try again.'
            )
        })

@login_required
def raise_complaint(request, booking_id):

    booking = get_object_or_404(
        Booking,
        id=booking_id,
        customer=request.user
    )

    if booking.status not in ['COMPLETION_REQUESTED', 'COMPLETED']:
        messages.error(request, 'You can raise a complaint only for completed or completion-requested bookings.')
        return redirect('my_bookings')

    if Complaint.objects.filter(booking=booking, customer=request.user).exists():
        messages.warning(request, 'You have already raised a complaint for this booking.')
        return redirect('my_complaints')

    if request.method == 'POST':
        subject = request.POST.get('subject', '').strip()
        description = request.POST.get('description', '').strip()

        if not subject:
            messages.error(request, 'Please enter a complaint subject.')
            return render(request, 'user/raise_complaint.html', {'booking': booking})

        if not description:
            messages.error(request, 'Please describe your complaint.')
            return render(request, 'user/raise_complaint.html', {'booking': booking})

        # Use get_or_create to handle race condition
        with transaction.atomic():
            complaint, created = Complaint.objects.get_or_create(
                booking=booking,
                customer=request.user,
                defaults={
                    'subject': subject,
                    'description': description,
                    'status': 'OPEN'
                }
            )

            if not created:
                messages.warning(request, 'You have already raised a complaint for this booking.')
                return redirect('my_complaints')

            try:
                from .ai_utils import classify_complaint_severity
                severity = classify_complaint_severity(subject, description)
                if severity:
                    complaint.severity = severity
                    complaint.save(update_fields=['severity'])
            except Exception:
                pass

        Notification.objects.create(
            recipient=request.user,
            booking=booking,
            notification_type='SYSTEM',
            message=f'Your complaint for Booking #{booking.id} has been submitted successfully.'
        )

        if booking.worker:
            Notification.objects.create(
                recipient=booking.worker.user,
                booking=booking,
                notification_type='SYSTEM',
                message=f'A complaint has been raised against you for Booking #{booking.id}. Subject: {subject}'
            )

        messages.success(request, 'Your complaint has been submitted successfully.')
        return redirect('my_complaints')

    return render(request, 'user/raise_complaint.html', {'booking': booking})


# ============================================================
# LIVE ROUTE MAP & TRACKING SYSTEM (LEAFLET / OPENSTREETMAP)
# ============================================================

@login_required
def api_booking_tracking(request, booking_id):
    """
    Returns live coordinates, route parameters, distance, and ETA
    for an active booking. Accessible to the customer, assigned worker, or admin.
    """
    try:
        booking = Booking.objects.select_related(
            'customer', 'worker__user', 'service'
        ).get(id=booking_id)
    except Booking.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Booking not found.'}, status=404)

    is_customer = (request.user == booking.customer)
    is_worker = (booking.worker and request.user == booking.worker.user)
    # Only genuine LAMO administrators (ADMIN role / superuser) - never plain Django staff.
    is_admin = _is_admin_account(request.user)

    if not (is_customer or is_worker or is_admin):
        return JsonResponse({'success': False, 'error': 'Unauthorized to track this booking.'}, status=403)

    # 1. Customer Coordinates Resolution
    c_lat = booking.latitude
    c_lng = booking.longitude
    if c_lat is None or c_lng is None:
        try:
            profile = booking.customer.user_profile
            c_lat = float(profile.latitude) if profile.latitude else None
            c_lng = float(profile.longitude) if profile.longitude else None
        except Exception:
            c_lat = None
            c_lng = None

    if c_lat is None or c_lng is None:
        base_coords = DISTRICT_COORDINATES.get(booking.district, (8.8932, 76.6141))
        c_lat, c_lng = base_coords[0], base_coords[1]

    # 2. Worker Coordinates Resolution (Restricted to Active Ongoing Bookings)
    worker_data = None
    w_lat = None
    w_lng = None
    distance_km = None
    eta_minutes = None

    is_active_tracking = booking.status in ('ACCEPTED', 'IN_PROGRESS', 'COMPLETION_REQUESTED')

    if booking.worker:
        w = booking.worker

        # Worker avatar
        avatar_url = None
        if w.profile_image:
            try:
                avatar_url = w.profile_image.url
            except Exception:
                avatar_url = None

        if is_active_tracking:
            w_lat = w.latitude
            w_lng = w.longitude

            # Fallback to worker district if worker hasn't shared exact coordinates
            if w_lat is None or w_lng is None:
                w_base = DISTRICT_COORDINATES.get(w.district, (c_lat, c_lng))
                # Slight realistic offset (approx ~1.5 - 2 km) if placed in same district center
                w_lat = round(w_base[0] + 0.0125, 5)
                w_lng = round(w_base[1] + 0.0140, 5)

            # Distance & ETA calculations
            distance_val = haversine_km(w_lat, w_lng, c_lat, c_lng)
            distance_km = round(distance_val, 1)

            if distance_km < 0.15:
                eta_minutes = 1
            else:
                # Average urban speed in Kerala: ~22 km/h
                travel_mins = int(round((distance_km / 22.0) * 60.0))
                eta_minutes = max(3, travel_mins)

            worker_data = {
                'id': w.id,
                'name': w.user.get_full_name() or w.user.username,
                'avatar': avatar_url,
                'rating': float(w.average_rating) if getattr(w, 'average_rating', None) else 4.9,
                'current_place': w.current_place or w.district or "Nearby Service Zone",
                'latitude': round(float(w_lat), 6),
                'longitude': round(float(w_lng), 6),
            }
        else:
            # Privacy protection: Zero live location exposure for terminal / inactive bookings
            worker_data = {
                'id': w.id,
                'name': w.user.get_full_name() or w.user.username,
                'avatar': avatar_url,
                'rating': float(w.average_rating) if getattr(w, 'average_rating', None) else 4.9,
                'current_place': None,
                'latitude': None,
                'longitude': None,
            }

    # 3. Status Description & Journey Stage
    status_text = "Booking is confirmed"
    if booking.status == 'PENDING':
        status_text = "Awaiting worker confirmation"
    elif booking.status == 'ACCEPTED':
        status_text = "Worker confirmed & getting ready"
    elif booking.status == 'IN_PROGRESS':
        if distance_km and distance_km <= 0.3:
            status_text = "Worker has arrived at your doorstep"
        else:
            status_text = "Worker is en route to your address"
    elif booking.status == 'COMPLETION_REQUESTED':
        status_text = "Service finished, awaiting your OTP confirmation"
    elif booking.status == 'COMPLETED':
        status_text = "Service successfully completed"
    elif booking.status in ('CANCELLED', 'EXPIRED'):
        status_text = f"Booking was {booking.status.lower()}"

    # Google Maps navigation direction URL
    gmaps_url = ""
    if w_lat and w_lng and c_lat and c_lng:
        gmaps_url = f"https://www.google.com/maps/dir/?api=1&origin={w_lat},{w_lng}&destination={c_lat},{c_lng}"

    return JsonResponse({
        'success': True,
        'booking_id': booking.id,
        'service_name': booking.service.name if booking.service else "Home Service",
        'status': booking.status,
        'status_text': status_text,
        'is_active': booking.status in ('ACCEPTED', 'IN_PROGRESS', 'COMPLETION_REQUESTED'),
        'is_worker': is_worker,
        'is_customer': is_customer,
        'customer': {
            'name': booking.customer.get_full_name() or booking.customer.username,
            'address': booking.location or (booking.district or "Kerala"),
            'latitude': round(float(c_lat), 6),
            'longitude': round(float(c_lng), 6),
        },
        'worker': worker_data,
        'distance_km': distance_km,
        'eta_minutes': eta_minutes,
        'gmaps_url': gmaps_url,
        'arrival_otp': booking.arrival_otp if (is_customer and booking.status in ('ACCEPTED', 'IN_PROGRESS')) else None,
    })


@login_required
def api_worker_update_coordinates(request):
    """
    Allows an active worker to update their live GPS coordinates
    from their device or dashboard.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required.'}, status=405)

    if not hasattr(request.user, 'worker_profile'):
        return JsonResponse({'success': False, 'error': 'Only registered workers can update live coordinates.'}, status=403)

    try:
        import json
        if request.content_type == 'application/json':
            data = json.loads(request.body.decode('utf-8'))
        else:
            data = request.POST

        lat = float(data.get('latitude'))
        lng = float(data.get('longitude'))
        place = data.get('place_name', '').strip()
    except (ValueError, TypeError, KeyError):
        return JsonResponse({'success': False, 'error': 'Valid latitude and longitude required.'}, status=400)

    # Basic Kerala boundary validation: Lat 8.0-13.0, Lng 74.5-77.8
    if not (8.0 <= lat <= 13.0 and 74.5 <= lng <= 77.8):
        return JsonResponse({'success': False, 'error': 'Coordinates outside Kerala service range.'}, status=400)

    # If updating location in context of a specific booking, prevent updates for terminal bookings
    booking_id = data.get('booking_id')
    if booking_id:
        try:
            b = Booking.objects.get(id=booking_id, worker=request.user.worker_profile)
            if b.status in ('COMPLETED', 'CANCELLED', 'REJECTED', 'EXPIRED'):
                return JsonResponse({'success': False, 'error': f'Cannot update location for a {b.status.lower()} booking.'}, status=400)
        except Booking.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Associated booking not found.'}, status=404)

    worker = request.user.worker_profile
    worker.latitude = lat
    worker.longitude = lng
    if place:
        worker.current_place = place
    worker.save(update_fields=['latitude', 'longitude', 'current_place'] if place else ['latitude', 'longitude'])

    return JsonResponse({
        'success': True,
        'latitude': lat,
        'longitude': lng,
        'place_name': worker.current_place or "",
        'message': 'Coordinates updated successfully.'
    })



# =========================================================================
# PILLAR 1: LIVE WORKER AVAILABILITY API (FOR TIME SLOTS & CUSTOM TIME)
# =========================================================================

def api_worker_availability(request, worker_id):
    """Return JSON availability map and busy windows for a worker on a given date.

    Used by booking_modal and book_service to color-code slots and guide custom time.

    Distance-based dynamic lead time (compute_dynamic_lead_time_min):
      - Customer coordinates are resolved server-side from the validated priority chain:
        1. Validated session coordinates (lamo_selected_lat / lamo_selected_lng or lat/lng)
        2. Coordinates supplied by the client via GET (lat/lng) only after range validation
        3. Locality/district geocoding fallback
        4. Authenticated customer's stored profile coordinates
      - Worker distance is always computed server-side via _compute_worker_distance().
      - The client-supplied distance is never trusted.
    """
    from datetime import datetime, timedelta
    worker = get_object_or_404(WorkerProfile.objects.select_related('user'), id=worker_id)
    date_str = request.GET.get('date', '').strip()
    service_id = request.GET.get('service_id', '').strip()

    target_service = None
    if service_id:
        try:
            target_service = Service.objects.filter(id=service_id).first()
        except Exception:
            pass
    service_duration = getattr(target_service, 'default_duration_min', 45) if target_service else 45
    custom_dur = request.GET.get('duration_min', '').strip()
    if custom_dur:
        try:
            dur_int = int(custom_dur)
            if 15 <= dur_int <= 360:
                service_duration = dur_int
        except (ValueError, TypeError):
            pass

    now_local = timezone.localtime()
    target_date = now_local.date()
    if date_str:
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            target_date = now_local.date()

    is_today = (target_date == now_local.date())
    is_past = (target_date < now_local.date())

    # -------------------------------------------------------------------------
    # Resolve customer coordinates — server-side, priority chain.
    # Never trust distance_km from the client; always compute it here.
    # -------------------------------------------------------------------------
    c_lat = None
    c_lng = None

    # Priority 1: validated session coordinates set by LAMO's location selector.
    for lat_key, lng_key in [
        ('lamo_selected_lat', 'lamo_selected_lng'),
        ('lat', 'lng'),
    ]:
        raw_lat = request.session.get(lat_key)
        raw_lng = request.session.get(lng_key)
        if raw_lat is not None and raw_lng is not None:
            try:
                sl = float(raw_lat)
                sn = float(raw_lng)
                # Rough Kerala bounding box: lat 8.1–12.8, lng 74.9–77.6
                if 8.0 <= sl <= 13.0 and 74.5 <= sn <= 78.0:
                    c_lat, c_lng = sl, sn
                    break
            except (ValueError, TypeError):
                pass

    # Priority 2: client-supplied GET params (lat/lng) — validated before use.
    if c_lat is None:
        raw_client_lat = request.GET.get('lat', '').strip()
        raw_client_lng = request.GET.get('lng', '').strip()
        if raw_client_lat and raw_client_lng:
            try:
                cl = float(raw_client_lat)
                cn = float(raw_client_lng)
                if 8.0 <= cl <= 13.0 and 74.5 <= cn <= 78.0:
                    c_lat, c_lng = cl, cn
            except (ValueError, TypeError):
                pass  # Reject malformed or out-of-range client coords

    # Priority 3: locality / district geocoding fallback.
    if c_lat is None:
        locality = (
            request.session.get('lamo_selected_locality')
            or request.session.get('locality', '')
        )
        district = (
            request.session.get('lamo_selected_district')
            or request.session.get('district', '')
        )
        if locality or district:
            try:
                gl, gn = get_locality_coordinates(district or '', locality or '')
                if gl is not None and gn is not None:
                    c_lat, c_lng = float(gl), float(gn)
            except Exception:
                pass

    # Priority 4: authenticated customer profile coordinates.
    if c_lat is None and request.user.is_authenticated:
        try:
            profile = getattr(request.user, 'user_profile', None) or getattr(request.user, 'userprofile', None)
            if profile and profile.latitude is not None and profile.longitude is not None:
                c_lat = float(profile.latitude)
                c_lng = float(profile.longitude)
        except Exception:
            pass

    # Compute server-side distance and dynamic lead time.
    dist_km = None
    if c_lat is not None and c_lng is not None:
        dist_km, _ = _compute_worker_distance(c_lat, c_lng, worker)

    lead_mins = compute_dynamic_lead_time_min(dist_km)

    # -------------------------------------------------------------------------
    # Build the availability window.
    # -------------------------------------------------------------------------
    now_mins = now_local.hour * 60 + now_local.minute
    min_buffer_mins = now_mins + lead_mins

    # 1. Leave check
    on_leave = WorkerLeave.objects.filter(worker=worker, leave_date=target_date).exists()

    # 2. Existing active bookings
    active_bookings = Booking.objects.filter(
        worker=worker,
        date=target_date,
        status__in=ACTIVE_BOOKING_STATUSES
    ).exclude(status='CANCELLED')

    standard_slots = worker.generate_time_slots()

    busy_list = []
    for b in active_bookings:
        start_t = b.start_time
        end_t = b.end_time
        if not start_t and b.time:
            start_t, end_t, _, _ = compute_booking_window(b.time, service=b.service)
        if start_t and end_t:
            busy_list.append({
                'id': b.id,
                'time': b.time,
                'start_minutes': start_t.hour * 60 + start_t.minute,
                'end_minutes': end_t.hour * 60 + end_t.minute,
                'start_formatted': start_t.strftime('%I:%M %p').lstrip('0'),
                'end_formatted': end_t.strftime('%I:%M %p').lstrip('0'),
            })

    slots_data = []
    travel_buffer_label = f'{lead_mins}m Travel Buffer'
    # The travel buffer that is really locked after the job is NOT the same
    # number as the same-day lead time above: it comes from
    # compute_booking_window (15 min default, 30 / 45 min for distant customers).
    # Ask that function so this endpoint, the booking modal and book_service
    # can never disagree about how long the worker is blocked.
    _jw_start, _jw_end, _jw_dur, job_buffer_mins = compute_booking_window(
        '12:00 PM', service=target_service, duration_min=service_duration, distance_km=dist_km
    )
    for slot_str in standard_slots:
        slot_start, slot_end = parse_time_range(slot_str)
        s_mins = slot_start.hour * 60 + slot_start.minute if slot_start else 0
        cand_duration = service_duration + job_buffer_mins
        e_mins = s_mins + cand_duration

        status = 'available'
        reason = ''

        if is_past:
            status = 'past'
            reason = 'Past Date'
        elif on_leave:
            status = 'on_leave'
            reason = 'Worker On Leave'
        elif is_today and s_mins <= now_mins:
            status = 'passed'
            reason = 'Passed'
        else:
            is_busy = False
            for busy in busy_list:
                if s_mins < busy['end_minutes'] and e_mins > busy['start_minutes']:
                    status = 'busy'
                    reason = 'Worker Already Booked'
                    is_busy = True
                    break
            if not is_busy and is_today and s_mins < min_buffer_mins:
                status = 'too_soon'
                reason = travel_buffer_label

        slots_data.append({
            'slot': slot_str,
            'status': status,
            'available': (status == 'available'),
            'reason': reason
        })

    open_slots = [s['slot'] for s in slots_data if s['available']]
    if on_leave:
        open_windows_text = "On Leave"
    elif not open_slots:
        open_windows_text = "Fully Booked Today"
    else:
        open_windows_text = ", ".join(open_slots)

    return JsonResponse({
        'success': True,
        'worker_id': worker.id,
        'worker_name': worker.user.get_full_name() or worker.user.username,
        'work_start_time': worker.work_start_time.strftime('%H:%M') if worker.work_start_time else '09:00',
        'work_end_time': worker.work_end_time.strftime('%H:%M') if worker.work_end_time else '18:00',
        'working_hours': worker.get_working_hours_display(),
        'service_duration_min': service_duration,
        'lead_time_min': lead_mins,
        'distance_km': round(dist_km, 2) if dist_km is not None else None,
        'travel_buffer_min': job_buffer_mins,
        'estimated_job_window': f"{service_duration} mins (+{job_buffer_mins}m travel buffer)",
        'date': target_date.strftime('%Y-%m-%d'),
        'on_leave': on_leave,
        'is_today': is_today,
        'busy_count': len(busy_list),
        'busy_windows': [f"{b['start_formatted']} - {b['end_formatted']}" for b in busy_list],
        'slots': slots_data,
        'open_windows_text': open_windows_text
    })


@login_required
def api_worker_start_shift(request):
    """Situation 1 Idea 1: Confirm daily working shift hours and go on-duty."""
    if request.user.role != 'WORKER':
        return JsonResponse({'success': False, 'message': 'Unauthorized'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)

    try:
        worker = request.user.worker_profile
    except Exception:
        return JsonResponse({'success': False, 'message': 'Worker profile not found'}, status=404)

    start_str = request.POST.get('work_start_time', '').strip() or '09:00'
    end_str = request.POST.get('work_end_time', '').strip() or '18:00'

    import datetime
    try:
        s_parts = list(map(int, start_str.split(':')[:2]))
        e_parts = list(map(int, end_str.split(':')[:2]))
        start_time = datetime.time(s_parts[0], s_parts[1])
        end_time = datetime.time(e_parts[0], e_parts[1])
        if (e_parts[0] * 60 + e_parts[1]) <= (s_parts[0] * 60 + s_parts[1]):
            return JsonResponse({'success': False, 'message': 'Shift end time must be later than start time.'}, status=400)
    except Exception:
        return JsonResponse({'success': False, 'message': 'Invalid time format.'}, status=400)

    worker.work_start_time = start_time
    worker.work_end_time = end_time
    worker.work_status = 'AVAILABLE'
    worker.save(update_fields=['work_start_time', 'work_end_time', 'work_status', 'updated_at'])

    return JsonResponse({
        'success': True,
        'message': f'Shift started! You are On-Duty from {worker.get_working_hours_display()}.',
        'work_start_time': start_time.strftime('%H:%M'),
        'work_end_time': end_time.strftime('%H:%M'),
        'working_hours': worker.get_working_hours_display(),
        'work_status': 'AVAILABLE'
    })


@login_required
def api_worker_incoming_job_alert(request):
    """Situation 2 (Idea 1 with Distance & Mini Map):
    Returns the latest incoming booking assigned to this worker in the last 30 minutes.
    """
    if request.user.role != 'WORKER':
        return JsonResponse({'success': False, 'message': 'Unauthorized'}, status=403)

    try:
        worker = request.user.worker_profile
    except Exception:
        return JsonResponse({'success': False, 'message': 'Worker profile not found'}, status=404)

    now = timezone.now()
    cutoff = now - timedelta(hours=24)

    incoming = Booking.objects.filter(
        worker=worker,
        status='PENDING',
        created_at__gte=cutoff
    ).select_related('customer', 'service').order_by('-created_at').first()

    if not incoming:
        return JsonResponse({'has_incoming_job': False})

    w_lat = _safe_coord(worker.latitude)
    w_lng = _safe_coord(worker.longitude)
    c_lat = _safe_coord(incoming.latitude)
    c_lng = _safe_coord(incoming.longitude)

    distance_km = None
    distance_label = f"In {incoming.district}" if incoming.district else "Doorstep Service"
    if w_lat is not None and w_lng is not None and c_lat is not None and c_lng is not None:
        try:
            dist_val = haversine_km(w_lat, w_lng, c_lat, c_lng)
            distance_km = round(dist_val, 1)
            distance_label = f"{dist_val:.1f} km away"
        except Exception:
            pass

    try:
        pricing_amount = float(worker.pricing_amount or 250)
        estimated_earnings = float(incoming.estimated_cost or (pricing_amount * 2))
    except Exception:
        estimated_earnings = 500.0

    seconds_elapsed = int((now - incoming.created_at).total_seconds()) if incoming.created_at else 0
    seconds_remaining = max(45, 1800 - seconds_elapsed)

    return JsonResponse({
        'has_incoming_job': True,
        'booking': {
            'id': incoming.id,
            'service_name': incoming.service.name if incoming.service else 'Home Service',
            'customer_name': incoming.customer.get_full_name() or incoming.customer.username,
            'scheduled_date': incoming.date.strftime('%A, %d %b %Y') if incoming.date else 'Today',
            'scheduled_time': str(incoming.time or 'Scheduled Slot'),
            'address': incoming.location or (incoming.district or 'Customer Address'),
            'distance_km': distance_km,
            'distance_label': distance_label,
            'customer_lat': c_lat,
            'customer_lng': c_lng,
            'worker_lat': w_lat,
            'worker_lng': w_lng,
            'estimated_earnings': round(estimated_earnings, 2),
            'seconds_elapsed': seconds_elapsed,
            'seconds_remaining': seconds_remaining,
        }
    })


# =========================================================================
# PILLAR 2: WORKER COUNTER-OFFER / PROPOSE NEW TIME WORKFLOW
# =========================================================================

@login_required
def worker_propose_reschedule(request, booking_id):
    """Allow an assigned worker to propose an alternative date/time instead of outright rejecting."""
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
        
    booking = get_object_or_404(Booking.objects.select_related('worker__user', 'customer', 'service'), id=booking_id)
    
    if not (booking.worker and booking.worker.user == request.user):
        messages.error(request, 'Unauthorized action.')
        return redirect('worker_jobs')
        
    if booking.status not in ('PENDING', 'ACCEPTED'):
        messages.error(request, 'Reschedule proposals can only be made on pending or confirmed job requests.')
        return redirect('worker_jobs')

    if booking.series and not booking.series.is_active:
        messages.error(request, 'Cannot propose reschedule: The customer has paused this recurring series.')
        return redirect('worker_jobs')
        
    proposed_date_str = request.POST.get('proposed_date', '').strip()
    proposed_time_str = request.POST.get('proposed_time', '').strip()
    reschedule_note = request.POST.get('reschedule_note', '').strip()
    
    if not proposed_date_str or not proposed_time_str:
        messages.error(request, 'Please provide both an alternative date and time.')
        return redirect('worker_jobs')
        
    from datetime import datetime, timedelta
    try:
        p_date = datetime.strptime(proposed_date_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        messages.error(request, 'Invalid date format.')
        return redirect('worker_jobs')
        
    now_local = timezone.localtime()
    if p_date < now_local.date():
        messages.error(request, 'Proposed date cannot be in the past.')
        return redirect('worker_jobs')
        
    start_t, end_t = parse_time_range(proposed_time_str)
    if not start_t or not end_t:
        messages.error(request, 'Invalid time format. Please provide a valid time (e.g., 4:30 PM or 4:00 PM - 6:00 PM).')
        return redirect('worker_jobs')
        
    if p_date == now_local.date() and start_t < (now_local + timedelta(minutes=60)).time():
        messages.error(request, 'Proposed time today must be at least 60 minutes in advance.')
        return redirect('worker_jobs')
        
    if has_worker_schedule_conflict(booking.worker, booking_date=p_date, start_time=start_t, end_time=end_t, exclude_booking_id=booking.id):
        messages.error(request, 'You already have another active appointment at this proposed time.')
        return redirect('worker_jobs')
        
    booking.proposed_date = p_date
    booking.proposed_time = proposed_time_str
    booking.reschedule_note = reschedule_note
    booking.reschedule_proposed_at = timezone.now()
    booking.save(update_fields=['proposed_date', 'proposed_time', 'reschedule_note', 'reschedule_proposed_at', 'updated_at'])
    
    worker_name = request.user.get_full_name() or request.user.username
    Notification.objects.create(
        recipient=booking.customer,
        booking=booking,
        notification_type='BOOKING_UPDATE',
        message=f"{worker_name} proposed a new time for Booking #{booking.id}: {p_date.strftime('%b %d, %Y')} at {proposed_time_str}. Tap to review and accept."
    )
    
    messages.success(request, f'Proposed new time ({p_date.strftime("%b %d")} at {proposed_time_str}) sent to customer!')
    return redirect('worker_jobs')


@login_required
def customer_accept_reschedule(request, booking_id):
    """Customer accepts worker's proposed counter-offer; booking is confirmed and scheduled."""
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])

    with transaction.atomic():
        booking = get_object_or_404(
            Booking.objects.select_for_update().select_related('customer', 'worker__user', 'service'),
            id=booking_id
        )
        if booking.customer != request.user:
            messages.error(request, 'Unauthorized action.')
            return redirect('my_bookings')
            
        if not booking.has_pending_reschedule:
            messages.error(request, 'No pending reschedule proposal found for this booking.')
            return redirect('my_bookings')
            
        new_date = booking.proposed_date
        new_time = booking.proposed_time
        start_t, end_t = parse_time_range(new_time)

        # Re-validate worker availability under exclusive WorkerProfile row lock
        if booking.worker:
            locked_worker = WorkerProfile.objects.select_for_update().get(id=booking.worker.id)
            try:
                if WorkerLeave.objects.filter(worker=locked_worker, leave_date=new_date).exists():
                    messages.error(request, 'Worker is currently marked on leave on the proposed date. Please decline and request an alternative time.')
                    return redirect('my_bookings')
            except Exception:
                pass

            if has_worker_schedule_conflict(locked_worker, booking_date=new_date, start_time=start_t, end_time=end_t, time_value=new_time, exclude_booking_id=booking.id):
                messages.error(request, 'Worker is no longer available at this proposed time due to another booking. Please decline to keep the original request or cancel.')
                return redirect('my_bookings')

            # A PENDING request becomes ACCEPTED here, so the worker's
            # acceptance commission must be taken now (exactly as in
            # worker_accept_booking). Already-ACCEPTED jobs were charged at
            # acceptance; the idempotent helper never charges them again.
            if booking.status == 'PENDING':
                wallet, _ = WorkerWallet.objects.select_for_update().get_or_create(worker=locked_worker)
                fee = calculate_booking_platform_fee(booking, locked_worker)
                balance = Decimal(str(wallet.balance if wallet.balance is not None else '0.00'))
                if balance < fee:
                    messages.error(
                        request,
                        'This professional cannot confirm the new time right now. '
                        'Please decline the proposal to keep your original request, or choose another worker.'
                    )
                    return redirect('my_bookings')
                _charge_acceptance_commission(
                    booking, wallet, fee,
                    note=f"Platform fee deducted when customer accepted rescheduled Booking #{booking.id}",
                )

        booking.date = new_date
        booking.time = new_time
        booking.start_time = start_t
        booking.end_time = end_t
        booking.status = 'ACCEPTED'
        booking.proposed_date = None
        booking.proposed_time = None
        booking.reschedule_note = None
        booking.generate_arrival_otp()
        booking.save(update_fields=['date', 'time', 'start_time', 'end_time', 'status', 'proposed_date', 'proposed_time', 'reschedule_note', 'arrival_otp', 'worker_badge_code', 'platform_fee', 'fee_settled', 'updated_at'])

    cust_name = request.user.get_full_name() or request.user.username
    if booking.worker and booking.worker.user:
        Notification.objects.create(
            recipient=booking.worker.user,
            booking=booking,
            notification_type='BOOKING_UPDATE',
            message=f"Great news! {cust_name} accepted your proposed time of {new_time} on {new_date.strftime('%b %d')}. Booking #{booking.id} is now confirmed!"
        )
        
    messages.success(request, f'Rescheduled to {new_date.strftime("%b %d, %Y")} at {new_time}! Your booking is now confirmed.')
    return redirect('my_bookings')


@login_required
def customer_decline_reschedule(request, booking_id):
    """Customer declines worker's proposed counter-offer; returns to normal pending state."""
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
        
    booking = get_object_or_404(Booking.objects.select_related('customer', 'worker__user', 'service'), id=booking_id)
    if booking.customer != request.user:
        messages.error(request, 'Unauthorized action.')
        return redirect('my_bookings')

    if not booking.has_pending_reschedule:
        messages.error(request, 'No pending reschedule proposal found for this booking.')
        return redirect('my_bookings')

    booking.proposed_date = None
    booking.proposed_time = None
    booking.reschedule_note = None
    booking.reschedule_proposed_at = None
    booking.save(update_fields=['proposed_date', 'proposed_time', 'reschedule_note', 'reschedule_proposed_at', 'updated_at'])
    
    if booking.worker and booking.worker.user:
        cust_name = request.user.get_full_name() or request.user.username
        Notification.objects.create(
            recipient=booking.worker.user,
            booking=booking,
            notification_type='BOOKING_UPDATE',
            message=f"{cust_name} declined your proposed alternative time for Booking #{booking.id}."
        )
        
    messages.info(request, 'Proposed time declined. Your booking keeps its original date and time.')
    return redirect('my_bookings')


# =========================================================================
# PILLAR 3: SMART SCHEDULING, REAL-TIME JOB TRANSIT & OVERRUN MANAGEMENT
# =========================================================================

def get_worker_transit_and_delay_context(worker, target_date=None):
    """
    Analyzes the worker's active job and next upcoming jobs today.
    Computes real-world metrics:
    - active_job: Currently IN_PROGRESS or COMPLETION_REQUESTED
    - next_job: Earliest upcoming ACCEPTED or PENDING booking today
    - minutes_until_next_job: Available travel minutes before next job
    - distance_km: Haversine distance between jobs
    - estimated_travel_min: Estimated transit time (at 2.5 mins/km, min 15 mins)
    - is_travel_tight: True if available minutes < travel time + 5
    - is_overrun: True if active job is running past scheduled window or next job start
    """
    now_local = timezone.localtime()
    today = target_date or now_local.date()
    now_mins = now_local.hour * 60 + now_local.minute

    # 1. Check for active job (IN_PROGRESS or COMPLETION_REQUESTED)
    active_job = Booking.objects.filter(
        worker=worker,
        date=today,
        status__in=['IN_PROGRESS', 'COMPLETION_REQUESTED']
    ).select_related('customer', 'service').order_by('-updated_at').first()

    # 2. Check for recent completed job if no active job (completed within last 60 minutes)
    recent_completed_job = None
    if not active_job:
        recent_completed_job = Booking.objects.filter(
            worker=worker,
            date=today,
            status='COMPLETED',
            updated_at__gte=now_local - timedelta(minutes=60)
        ).select_related('customer', 'service').order_by('-updated_at').first()

    ref_job = active_job or recent_completed_job

    # 3. Find next upcoming booking today (exclude the reference job)
    upcoming_query = Booking.objects.filter(
        worker=worker,
        date=today,
        status__in=['PENDING', 'ACCEPTED']
    )
    if ref_job:
        upcoming_query = upcoming_query.exclude(id=ref_job.id)

    upcoming_jobs = list(upcoming_query.select_related('customer', 'service').order_by('start_time', 'time'))
    if not upcoming_jobs:
        return None

    next_job = upcoming_jobs[0]

    # Calculate next job start minutes
    next_start_mins = None
    if next_job.start_time:
        next_start_mins = next_job.start_time.hour * 60 + next_job.start_time.minute
    elif next_job.time:
        p_start, _ = parse_time_range(next_job.time)
        if p_start:
            next_start_mins = p_start.hour * 60 + p_start.minute

    if next_start_mins is None:
        next_start_mins = now_mins + 30

    minutes_until_next = max(0, next_start_mins - now_mins)

    # Compute distance
    from .ai_utils import haversine_km
    distance_km = 5.0
    ref_lat = ref_job.latitude if (ref_job and ref_job.latitude) else worker.latitude
    ref_lng = ref_job.longitude if (ref_job and ref_job.longitude) else worker.longitude
    next_lat = next_job.latitude
    next_lng = next_job.longitude

    if ref_lat and ref_lng and next_lat and next_lng:
        try:
            distance_km = round(haversine_km(float(ref_lat), float(ref_lng), float(next_lat), float(next_lng)), 1)
        except Exception:
            distance_km = 5.0

    estimated_travel_min = max(15, int(round(distance_km * 2.5)))
    is_travel_tight = (minutes_until_next < estimated_travel_min + 5)

    is_overrun = False
    if active_job:
        active_end_mins = None
        if active_job.end_time:
            active_end_mins = active_job.end_time.hour * 60 + active_job.end_time.minute
        elif active_job.time:
            _, p_end = parse_time_range(active_job.time)
            if p_end:
                active_end_mins = p_end.hour * 60 + p_end.minute

        if active_end_mins and now_mins > active_end_mins:
            is_overrun = True
        elif now_mins + estimated_travel_min > next_start_mins:
            is_overrun = True

    return {
        'active_job': active_job,
        'recent_completed_job': recent_completed_job,
        'ref_job': ref_job,
        'next_job': next_job,
        'minutes_until_next_job': minutes_until_next,
        'distance_km': distance_km,
        'estimated_travel_min': estimated_travel_min,
        'is_travel_tight': is_travel_tight,
        'is_overrun': is_overrun,
        'prompt_transit_check': (ref_job is not None),
    }


def get_customer_booking_transit_status(booking, active_jobs_by_worker=None):
    """
    Check if the assigned worker is currently busy on an active earlier job,
    and whether delay / transit notification should be displayed to the customer.
    """
    if not (booking.worker and booking.date):
        return None

    now_local = timezone.localtime()
    today = now_local.date()
    if booking.date != today:
        return None

    now_mins = now_local.hour * 60 + now_local.minute

    # Check if worker is active on another job (use pre-fetched bulk map if provided)
    if active_jobs_by_worker is not None:
        cand = active_jobs_by_worker.get(booking.worker_id)
        other_active = cand if (cand and cand.id != booking.id) else None
    else:
        other_active = Booking.objects.filter(
            worker=booking.worker,
            date=today,
            status__in=['IN_PROGRESS', 'COMPLETION_REQUESTED']
        ).exclude(id=booking.id).first()

    if not other_active:
        return None

    worker_name = booking.worker.user.get_full_name() or booking.worker.user.username

    b_start_mins = None
    if booking.start_time:
        b_start_mins = booking.start_time.hour * 60 + booking.start_time.minute
    elif booking.time:
        s_t, _ = parse_time_range(booking.time)
        if s_t:
            b_start_mins = s_t.hour * 60 + s_t.minute

    has_delay = False
    if b_start_mins is not None:
        if now_mins >= b_start_mins or (b_start_mins - now_mins) < 15:
            has_delay = True

    return {
        'is_busy': True,
        'worker_name': worker_name,
        'has_delay': has_delay,
        'active_service_name': other_active.service.name if other_active.service else 'Service',
        'scheduled_time': booking.time,
    }


@login_required
@require_POST
def worker_confirm_next_job_transit(request, booking_id):
    """
    Handle the worker's transit confirmation for their next upcoming booking:
    1. 'on_time': Confirms on-time transit (accepts if pending, notifies customer).
    2. 'need_buffer': Adds travel buffer (+15m or +30m), computes updated ETA, notifies customer.
    3. 'reschedule': Redirects to counter-offer proposal modal.
    """
    if request.user.role != 'WORKER':
        messages.error(request, 'Unauthorized action.')
        return redirect('dashboard')

    worker = get_object_or_404(WorkerProfile, user=request.user)
    booking = get_object_or_404(Booking, id=booking_id, worker=worker)

    choice = request.POST.get('transit_choice', '').strip()
    worker_name = request.user.get_full_name() or request.user.username

    if choice == 'on_time':
        if booking.status == 'PENDING':
            booking.status = 'ACCEPTED'
            booking.generate_arrival_otp()
            booking.save(update_fields=['status', 'arrival_otp', 'worker_badge_code', 'updated_at'])

        Notification.objects.create(
            recipient=booking.customer,
            booking=booking,
            notification_type='BOOKING_UPDATE',
            message=f"Great news! Your professional {worker_name} has completed their prior job and is on the way! Expected on time for {booking.time}."
        )
        messages.success(request, f'Transit confirmed! Customer has been notified that you are en route on time for Booking #{booking.id}.')
        return redirect('worker_jobs')

    elif choice == 'need_buffer':
        buffer_mins = 15
        try:
            buffer_mins = int(request.POST.get('buffer_minutes', 15))
        except (ValueError, TypeError):
            buffer_mins = 15

        now_local = timezone.localtime()

        target_dt = None
        if booking.start_time:
            target_dt = datetime.combine(now_local.date(), booking.start_time) + timedelta(minutes=buffer_mins)
        else:
            target_dt = now_local + timedelta(minutes=buffer_mins + 15)

        new_time_str = target_dt.strftime('%I:%M %p').lstrip('0')
        booking.proposed_date = booking.date or now_local.date()
        booking.proposed_time = f"{new_time_str}"
        booking.reschedule_note = f"Prior service completed. Added {buffer_mins}-min travel buffer for safe transit."
        booking.reschedule_proposed_at = timezone.now()
        booking.save(update_fields=['proposed_date', 'proposed_time', 'reschedule_note', 'reschedule_proposed_at', 'updated_at'])

        Notification.objects.create(
            recipient=booking.customer,
            booking=booking,
            notification_type='BOOKING_UPDATE',
            message=f"{worker_name} completed their previous service and is en route with a travel buffer. Estimated arrival: {new_time_str}."
        )
        messages.success(request, f'Travel buffer (+{buffer_mins}m) applied! Customer notified of updated arrival at {new_time_str}.')
        return redirect('worker_jobs')

    elif choice == 'reschedule':
        return redirect(f"/worker-jobs/?reschedule={booking.id}")

    messages.error(request, 'Invalid transit choice.')
    return redirect('worker_jobs')


@login_required
@require_POST
def customer_request_reschedule(request, booking_id):
    """Allow customer to request a new date/time if their booking is delayed or they want to reschedule."""
    if request.user.role == 'WORKER':
        messages.error(request, 'Unauthorized action.')
        return redirect('worker_jobs')

    get_object_or_404(Booking, id=booking_id, customer=request.user)

    new_date_str = request.POST.get('date', '').strip()
    new_time_str = request.POST.get('time', '').strip()
    reason = request.POST.get('reason', '').strip() or 'Customer requested alternative time due to schedule adjustment'

    if not new_date_str or not new_time_str:
        messages.error(request, 'Please provide both date and time to reschedule.')
        return redirect('my_bookings')

    try:
        new_date = datetime.strptime(new_date_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        messages.error(request, 'Invalid date format.')
        return redirect('my_bookings')

    now_local = timezone.localtime()
    if new_date < now_local.date():
        messages.error(request, 'Cannot reschedule to a past date.')
        return redirect('my_bookings')

    start_t, end_t = parse_time_range(new_time_str)
    if not start_t or not end_t:
        messages.error(request, 'Invalid time range.')
        return redirect('my_bookings')

    if new_date == now_local.date() and start_t <= now_local.time():
        messages.error(request, 'That time has already passed today. Please choose a later time.')
        return redirect('my_bookings')

    # Lock the booking and the worker so two reschedules / bookings cannot
    # claim the same slot concurrently, then re-check status and overlaps.
    with transaction.atomic():
        booking = Booking.objects.select_for_update().select_related('worker__user').get(
            id=booking_id, customer=request.user
        )
        if booking.status not in ('PENDING', 'ACCEPTED'):
            messages.error(request, 'Only pending or accepted bookings can be rescheduled.')
            return redirect('my_bookings')

        if booking.worker_id:
            locked_worker = WorkerProfile.objects.select_for_update().get(id=booking.worker_id)
            if WorkerLeave.objects.filter(worker=locked_worker, leave_date=new_date).exists():
                messages.error(request, 'Your professional is on leave on that date. Please choose another date.')
                return redirect('my_bookings')
            if has_worker_schedule_conflict(
                locked_worker,
                booking_date=new_date,
                start_time=start_t,
                end_time=end_t,
                time_value=new_time_str,
                exclude_booking_id=booking.id,
            ):
                messages.error(
                    request,
                    'Your professional already has another job at that time. Please choose a different time slot.'
                )
                return redirect('my_bookings')

        booking.date = new_date
        booking.time = new_time_str
        booking.start_time = start_t
        booking.end_time = end_t
        booking.reschedule_note = reason
        booking.save(update_fields=['date', 'time', 'start_time', 'end_time', 'reschedule_note', 'updated_at'])

    if booking.worker and booking.worker.user:
        cust_name = request.user.get_full_name() or request.user.username
        Notification.objects.create(
            recipient=booking.worker.user,
            booking=booking,
            notification_type='BOOKING_UPDATE',
            message=f"{cust_name} has rescheduled Booking #{booking.id} to {new_date.strftime('%b %d, %Y')} at {new_time_str}."
        )

    messages.success(request, f'Your booking has been rescheduled to {new_date.strftime("%b %d, %Y")} at {new_time_str}.')
    return redirect('my_bookings')


def _reassignment_problem(worker, booking, wallet=None):
    """Why ``worker`` may NOT take ``booking`` right now, or None when eligible.

    Applies the same rules a worker faces when accepting a booking themselves
    (verification/suspension, leave, schedule conflict, dues pause and the
    prepaid-wallet commission check), so a reassigned booking can never land on
    a worker who could not have accepted it normally. Call it again under row
    locks immediately before assigning.
    """
    ok, message = _check_worker_verification(worker)
    if not ok:
        return message
    if not worker.user.is_active:
        return 'Worker account is not active.'
    if worker.work_status != 'AVAILABLE':
        return 'Worker is not available.'
    if worker.is_paused_for_dues:
        return 'Worker is paused for platform dues.'
    if booking.date and WorkerLeave.objects.filter(worker=worker, leave_date=booking.date).exists():
        return 'Worker is on leave on this date.'
    if has_worker_schedule_conflict(
        worker,
        booking_date=booking.date,
        start_time=booking.start_time,
        end_time=booking.end_time,
        time_value=booking.time,
        exclude_booking_id=booking.id,
    ):
        return 'Worker already has a booking at this time.'
    fee = calculate_booking_platform_fee(booking, worker)
    if fee > Decimal('0.00'):
        balance = Decimal(str(wallet.balance)) if wallet is not None and wallet.balance is not None else Decimal('0.00')
        if balance < fee or (wallet is not None and wallet.is_locked):
            return 'Worker wallet balance does not cover the platform fee.'
    return None


@login_required
@require_POST
def customer_reassign_booking(request, booking_id):
    """
    Allow customer to 1-tap auto-reassign an EXPIRED or REJECTED booking to the next best
    available verified professional for the same service and district.
    """
    if request.user.role == 'WORKER':
        messages.error(request, 'Unauthorized action.')
        return redirect('worker_jobs')

    booking = get_object_or_404(Booking, id=booking_id, customer=request.user)
    is_worker_cancelled = (booking.status == 'CANCELLED' and booking.cancelled_by == 'WORKER')

    if booking.status not in ('EXPIRED', 'REJECTED') and not is_worker_cancelled:
        messages.error(request, 'Only expired, declined, or professional-cancelled bookings can be automatically reassigned.')
        return redirect('my_bookings')

    def _past_date_error(b):
        """A booking whose slot has already started cannot be handed to a new worker."""
        if not b.date:
            return 'This booking has no scheduled date, so it cannot be reassigned.'
        if b.date < timezone.localdate():
            return 'This booking date has already passed. Please book a new date.'
        start = _booking_scheduled_start(b)
        if start is not None and start <= timezone.now():
            return 'The scheduled time for this booking has already passed. Please book a new time.'
        return None

    past_error = _past_date_error(booking)
    if past_error:
        messages.error(request, past_error)
        return redirect('my_bookings')

    service = booking.service
    district = booking.district or (booking.worker.district if booking.worker else None)

    # Base query: verified, active, service matching, work_status AVAILABLE
    candidates = (
        WorkerProfile.objects
        .select_related('user', 'service')
        .filter(
            service=service,
            is_verified=True,
            work_status='AVAILABLE',
            user__is_active=True,
            is_paused_for_dues=False,
        )
        .annotate(
            avg_rating=models.Avg('reviews_received__rating')
        )
    )
    if booking.worker:
        candidates = candidates.exclude(id=booking.worker.id)

    # District filter if available
    if district:
        district_candidates = candidates.filter(district__iexact=district)
        if district_candidates.exists():
            candidates = district_candidates

    # Best candidates first; at most a handful are tried (each one is locked and
    # re-validated before it is assigned).
    ordered = list(candidates.order_by(models.F('avg_rating').desc(nulls_last=True), '-experience_years', 'id')[:15])

    assigned = None
    old_worker_name = ''
    with transaction.atomic():
        b = Booking.objects.select_for_update().get(id=booking.id, customer=request.user)
        locked_worker_cancelled = (b.status == 'CANCELLED' and b.cancelled_by == 'WORKER')
        if b.status not in ('EXPIRED', 'REJECTED') and not locked_worker_cancelled:
            messages.error(request, 'This booking cannot be reassigned at this time.')
            return redirect('my_bookings')
        past_error = _past_date_error(b)
        if past_error:
            messages.error(request, past_error)
            return redirect('my_bookings')

        old_worker_name = b.worker.user.get_full_name() if b.worker and b.worker.user else "Previous Worker"

        for cand in ordered:
            # Lock the candidate and their wallet, then re-check everything
            # against the committed state (it may have changed since the list was built).
            locked = WorkerProfile.objects.select_for_update().select_related('user').get(pk=cand.pk)
            wallet = WorkerWallet.objects.select_for_update().filter(worker=locked).first()
            if _reassignment_problem(locked, b, wallet) is None:
                assigned = locked
                break

        if assigned is None:
            messages.warning(
                request,
                f"No other available verified professionals found for {service.name} in your area at the requested time. "
                "Please select another time slot or browse all professionals."
            )
            return redirect('my_bookings')

        b.worker = assigned
        b.status = 'PENDING'
        b.cancelled_by = ''
        b.created_at = timezone.now()
        b.cancellation_reason = f"Auto-reassigned by customer from {old_worker_name} to {assigned.user.get_full_name()}."
        b.save(update_fields=['worker', 'status', 'created_at', 'cancelled_by', 'cancellation_reason', 'updated_at'])

        # Notify the new worker
        Notification.objects.create(
            recipient=assigned.user,
            booking=b,
            notification_type='NEW_BOOKING',
            message=f"New booking assignment #{b.id} for {service.name} on {b.date.strftime('%b %d, %Y')} at {b.time}. Please respond within the response window."
        )

        # Safely notify via email
        send_email_safely(
            subject=f"New Booking Request #{b.id} - LAMO",
            message=(
                f"Hello {assigned.user.get_full_name() or assigned.user.username},\n\n"
                f"You have received a new booking request #{b.id} for {service.name} scheduled on {b.date.strftime('%b %d, %Y')} at {b.time}.\n\n"
                f"Please log in to your LAMO Partner portal and respond within the response window.\n\n"
                f"Best regards,\nTeam LAMO"
            ),
            recipient_list=[assigned.user.email],
            purpose='worker_new_booking',
            async_send=True
        )

    new_name = assigned.user.get_full_name() or assigned.user.username
    messages.success(
        request,
        f"Booking #{b.id} has been automatically reassigned to {new_name}! "
        "They have been notified to review and confirm."
    )
    return redirect('my_bookings')
