import re
import csv
import json
import io
import logging
import urllib.request
import urllib.parse
import os
import uuid
from decimal import Decimal
from functools import lru_cache

from django.db import IntegrityError, transaction
from django.db.models import Q, Sum, Avg, Count, Case, When, Value, IntegerField, Prefetch, ProtectedError
from django.db.models.functions import TruncMonth
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth import update_session_auth_hash
from django.contrib import messages
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse, HttpResponseForbidden, Http404, FileResponse
from django.utils.http import url_has_allowed_host_and_scheme
from .models import CustomUser, UserProfile, Service, WorkerProfile, Booking, BookingSeries, BookingMessage, Notification, Complaint, Review, WorkerLeave, ContactEnquiry, ServiceRequest, Payment, StrikeRecord
from .email_utils import send_email_safely
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
    minutes_remaining,
    otp_resend_ip_key,
    otp_resend_user_key,
    register_attempt_within_window,
    register_failure,
    start_cooldown,
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
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
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
        .exclude(worker__isnull=True)
        .values_list('worker_id', 'start_time', 'end_time', 'time')
    )

    for worker_id, existing_start, existing_end, existing_time in rows:
        busy_by_worker.setdefault(worker_id, []).append(
            (existing_start, existing_end, existing_time)
        )

    return leave_worker_ids, busy_by_worker


def get_worker_unavailability_reasons(
    worker,
    leave_worker_ids=None,
    busy_by_worker=None,
    start_time=None,
    end_time=None,
    time_value=None,
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
            reasons.append('Currently on duty and not taking bookings.')
        elif worker.work_status == 'LEAVE':
            reasons.append('Currently on leave.')
        else:
            reasons.append('Not available for booking.')

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
        return None
    except Exception as exc:
        # Never break booking creation because of a geocoding hiccup.
        # Class name only: a URLError/JSONDecodeError message can echo the
        # full request URL and its response body. The coordinates are personal
        # location data, so they are not logged either.
        logger.debug(
            'district_geocode_failed exc_type=%s',
            type(exc).__name__,
        )
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
        return JsonResponse({
            'success': True,
            'emergency': True,
            'waiting': True,
            'booking_id': booking.id,
            'redirect_url': '/my-bookings/',
            'workers_notified': workers_notified,
            'message': f'Emergency request #{booking.id} created successfully and sent to {workers_notified} available workers.'
        })

    messages.success(request, f'Emergency request #{booking.id} created successfully and sent to {workers_notified} available workers.')
    return redirect('my_bookings')


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

    # Admins have no business on the public homepage; send them
    # directly to the admin dashboard so they don't see the
    # customer-facing splash / service listing.
    if request.user.is_authenticated and (
        getattr(request.user, 'role', None) == 'ADMIN'
        or getattr(request.user, 'is_superuser', False)
        or getattr(request.user, 'is_staff', False)
    ):
        return redirect('admin_dashboard')

    is_from_splash = bool(request.GET.get('from_splash'))
    if is_from_splash:
        request.session['lamo_splash_shown'] = True
        if not request.user.is_authenticated:
            request.session.set_expiry(0)

    query = request.GET.get('q', '').strip()

    # Replay splash screen on direct visits and page refreshes for unauthenticated
    # visitors before login, ensuring a consistent premium brand intro.
    # Authenticated users (customers/workers) and direct queries/splash redirects bypass it.
    if not request.user.is_authenticated and not is_from_splash and not query:
        request.session['lamo_splash_shown'] = True
        response = render(request, 'splash.html')
        response.set_cookie('lamo_splash_seen', '1', samesite='Lax')
        return response

    services = Service.objects.filter(is_active=True)
    if query:
        services = services.filter(
            Q(name__icontains=query) | Q(description__icontains=query)
        )
    services = services.order_by('-created_at')

    # Show only 4 services on the home page preview
    home_services = services[:4]
    has_more_services = services.count() > 4

    # Fetch real reviews from database
    reviews = (
        Review.objects
        .select_related('customer', 'worker__user', 'booking__service')
        .order_by('-created_at')[:6]
    )

    response = render(request, 'home.html', {
        'services': home_services,
        'has_more_services': has_more_services,
        'query': query,
        'reviews': reviews,
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

    # Get the user being verified (per-user tracking, NOT global session)
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
        action = request.POST.get('action', 'verify')
        otp_input = request.POST.get('otp', '').strip()

        if action == 'verify':
            if not user_id:
                messages.error(request, 'Session expired. Please register again.')
                return redirect('register')

            try:
                user = CustomUser.objects.get(id=user_id)
            except CustomUser.DoesNotExist:
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
                messages.error(
                    request,
                    'Too many failed attempts for this account. Please try again in 15 minutes.'
                )
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

                messages.success(request, 'Email verified successfully! You can now login.')
                return redirect('login')
            else:
                # Increment attempts ONLY for this user
                user.otp_failed_attempts += 1
                user.save(update_fields=['otp_failed_attempts'])
                remaining_attempts = (
                    OTP_VERIFY_MAX_ATTEMPTS - user.otp_failed_attempts
                )
                messages.error(
                    request,
                    f'Invalid or expired OTP. {remaining_attempts} attempt(s) remaining for this account.'
                )
                return render(request, 'accounts/verify_otp.html', {'user_email': user.email})

        elif action == 'resend':
            if not user_id:
                messages.error(request, 'Session expired. Please register again.')
                return redirect('register')
            try:
                user = CustomUser.objects.get(id=user_id)
            except CustomUser.DoesNotExist:
                messages.error(request, 'User not found. Please register again.')
                return redirect('register')

            # Resend limits live on the server, not in the session: deleting
            # the session cookie used to reset the counter and allow unlimited
            # OTP emails to the same inbox.
            sent, result_message = _resend_otp_for_user(request, user)

            if not sent:
                messages.error(request, result_message)
                return render(request, 'accounts/verify_otp.html', {
                    'user_email': user.email,
                    'locked_out': True
                })

            # A resend issues a brand new code, so the previous guesses must
            # not carry over - otherwise "guess 4 times, resend, guess 4 more"
            # would be an unlimited oracle against a 6-digit space.
            user.otp_failed_attempts = 0
            user.otp_locked_until = None
            user.save(update_fields=['otp_failed_attempts', 'otp_locked_until'])

            messages.success(request, result_message)
            return render(request, 'accounts/verify_otp.html', {'user_email': user.email})

    user_email = None
    if verified_user:
        user_email = verified_user.email

    return render(request, 'accounts/verify_otp.html', {'user_email': user_email})


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


def check_and_expire_pending_bookings(customer=None, worker=None, booking=None):
    """Expire PENDING bookings whose 30-minute worker response window has lapsed.

    Notifies customer and worker, updates booking status to EXPIRED, and records
    the reason. Returns the list of expired bookings.
    """
    from datetime import timedelta
    from django.utils import timezone as dj_timezone

    expiry_threshold = dj_timezone.now() - timedelta(minutes=30)
    qs = Booking.objects.filter(
        status__in=['PENDING', 'EMERGENCY_PENDING'],
        created_at__lt=expiry_threshold
    )
    if booking:
        qs = qs.filter(id=booking.id)
    elif customer:
        qs = qs.filter(customer=customer)
    elif worker:
        qs = qs.filter(worker=worker)

    expired_list = []
    for candidate in qs.select_related('customer', 'service', 'worker__user'):
        with transaction.atomic():
            b = Booking.objects.select_for_update().filter(id=candidate.id).first()
            if not b or b.status not in ('PENDING', 'EMERGENCY_PENDING'):
                continue
            b.status = 'EXPIRED'
            b.cancellation_reason = 'Worker did not respond within the 30-minute response window.'
            b.cancelled_by = 'SYSTEM'
            b.save(update_fields=['status', 'cancellation_reason', 'cancelled_by', 'updated_at'])
            expired_list.append(b)

            # Notify customer
            Notification.objects.create(
                recipient=b.customer,
                booking=b,
                notification_type='BOOKING_UPDATE',
                message=(
                    f"Your booking request #{b.id} for {b.service.name} has expired because "
                    f"the worker did not respond within 30 minutes. You can choose another worker or pick another time."
                )
            )

            # Send email safely
            send_email_safely(
                subject=f"Booking Request #{b.id} Expired",
                message=(
                    f"Hi {b.customer.username},\n\n"
                    f"Your booking request #{b.id} for {b.service.name} has expired because "
                    f"it was not accepted within the 30-minute response window.\n\n"
                    f"You can choose another available worker or reschedule your service on LAMO.\n\n"
                    f"Best regards,\nTeam LAMO"
                ),
                recipient_list=[b.customer.email],
                purpose='booking_expired',
            )

            # Also notify worker if one was assigned
            if b.worker and b.worker.user:
                Notification.objects.create(
                    recipient=b.worker.user,
                    booking=b,
                    notification_type='BOOKING_UPDATE',
                    message=f"Booking request #{b.id} for {b.service.name} has expired due to no response within 30 minutes."
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

    # Validate date range
    if start_date < timezone.localdate():
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

    if worker.work_status != 'AVAILABLE':
        return respond_error('This worker is currently unavailable. Please choose another worker.')

    lat_val = _safe_coord(latitude)
    lng_val = _safe_coord(longitude)

    if lat_val is not None and lng_val is not None:
        location_district = _validate_location_in_district(lat_val, lng_val, worker.district)
        if location_district is None:
            return respond_error(f'Please select a service location within {worker.district}.')

    start_t, end_t = parse_time_range(time)

    # A recurring booking is always at a slot the customer picked, so an
    # unusable value must be rejected rather than quietly degraded to an
    # exact-string comparison (which misses two different spellings of the
    # same slot and would allow a double booking).
    if start_t is None or end_t is None:
        return respond_error('Please select a valid time slot.')

    with transaction.atomic():
        locked_worker = WorkerProfile.objects.select_for_update().get(id=worker.id)

        # Check for conflicts on each date
        for booking_date in booking_dates:
            # Check worker leave
            try:
                worker_on_leave = WorkerLeave.objects.filter(
                    worker=locked_worker, leave_date=booking_date
                ).exists()
                if worker_on_leave:
                    return respond_error(
                        f'Worker is on leave on {booking_date.strftime("%Y-%m-%d")}. '
                        f'Please adjust the date range.'
                    )
            except Exception:
                pass

            # Check duplicate user booking
            duplicate_user_booking = Booking.objects.filter(
                customer=request.user, worker=locked_worker, service=service,
                date=booking_date,
                status__in=['PENDING', 'ACCEPTED', 'IN_PROGRESS']
            ).exists()
            if duplicate_user_booking:
                return respond_error(
                    f'You already have an active booking with this worker on '
                    f'{booking_date.strftime("%Y-%m-%d")}.'
                )

            # Check worker availability
            if start_t and end_t:
                existing_booking = Booking.objects.filter(
                    worker=locked_worker, date=booking_date,
                    status__in=['PENDING', 'ACCEPTED', 'IN_PROGRESS', 'COMPLETION_REQUESTED'],
                    start_time__lt=end_t, end_time__gt=start_t
                ).exists()
            else:
                existing_booking = Booking.objects.filter(
                    worker=locked_worker, date=booking_date, time=time,
                    status__in=['PENDING', 'ACCEPTED', 'IN_PROGRESS', 'COMPLETION_REQUESTED']
                ).exists()

            if existing_booking:
                return respond_error(
                    f'Worker is already booked on {booking_date.strftime("%Y-%m-%d")} '
                    f'at the selected time.'
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
            location=location,
            latitude=lat_val,
            longitude=lng_val,
            description=description,
            phone=phone,
            start_date=start_date,
            end_date=end_date,
        )

        # Create individual bookings for each date
        bookings_created = []
        for booking_date in booking_dates:
            booking = Booking.objects.create(
                customer=request.user,
                worker=locked_worker,
                service=service,
                booking_type='Recurring',
                date=booking_date,
                time=time,
                start_time=start_t,
                end_time=end_t,
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
        subject=f'Recurring Booking Confirmed - Series #{series.id}',
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
    )

    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
    messages.success(
        request,
        f'Recurring booking created successfully! {len(bookings_created)} occurrences scheduled.'
    )

    if is_ajax:
        return JsonResponse({
            'success': True,
            'redirect_url': reverse('my_bookings'),
            'message': f'Recurring booking created! {len(bookings_created)} occurrences scheduled.'
        })

    return redirect('my_bookings')


@login_required
def book_service(request):
    if request.user.role != 'USER':
        messages.error(request, 'Only users can book services.')
        return redirect('dashboard')

    if request.method == 'GET':
        worker_id = request.GET.get('worker')
        service_id = request.GET.get('service')

        worker = None
        service = None
        district = ''

        # A bare /book-service/ with no service can only come from a stale or
        # hand-typed URL. The booking form is built entirely around one specific
        # service, so send the customer back to the catalogue instead of
        # rendering a page whose "Back to <service> Workers" link has no id to
        # reverse (which raised NoReverseMatch and produced an HTTP 500).
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
            'location': request.GET.get('location', '').strip(),
            'phone': request.GET.get('phone', '').strip(),
        }

        return render(request, 'user/book_service.html', {
            'worker': worker,
            'service': service,
            'district': district,
            'district_lat': district_lat,
            'district_lng': district_lng,
            'prefill_data': prefill_data,
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

        start_date = request.POST.get('start_date', '').strip()
        end_date = request.POST.get('end_date', '').strip()
        frequency = request.POST.get('frequency', 'Weekly').strip()
        preferred_day = request.POST.get('preferred_day', '').strip()

        if not time:
            if custom_time:
                time = custom_time
            elif time_slot:
                time = time_slot

        is_emergency = (not worker_id and request.POST.get('problem'))

        if is_emergency:
            return _handle_emergency_booking(request, service_id, time, location, latitude, longitude, description, phone)

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

        def respond_error(err_msg):
            if is_ajax:
                return JsonResponse({'success': False, 'error_message': err_msg}, status=400)
            district_lat, district_lng = DISTRICT_COORDINATES.get(district, (9.9312, 76.2673)) if district else (9.9312, 76.2673)
            return render(request, 'user/book_service.html', {
                'worker': worker,
                'service': service,
                'district': district,
                'district_lat': district_lat,
                'district_lng': district_lng,
                'error_message': err_msg,
                'form_data': form_data,
                'other_workers': list(other_workers),
            })

        if booking_type == 'Recurring':
            return _handle_recurring_booking(
                request, worker, service, form_data, respond_error,
                start_date, end_date, frequency, preferred_day, time,
                location, latitude, longitude, description, phone
            )

        if not date:
            return respond_error('Please select a date.')
        if not time:
            return respond_error('Please select a time.')
        if not location:
            return respond_error('Please enter the service location.')
        if not phone:
            return respond_error('Please enter your phone number.')

        if worker.work_status != 'AVAILABLE':
            return respond_error('This worker is currently unavailable. Please choose another worker.')

        from datetime import datetime
        try:
            booking_date = datetime.strptime(date, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return respond_error('Invalid date format.')

        if booking_date < timezone.localdate():
            return respond_error('Cannot book for past dates.')

        worker_on_leave = False
        try:
            worker_on_leave = WorkerLeave.objects.filter(
                worker=worker, leave_date=booking_date
            ).exists()
        except Exception:
            pass

        if worker_on_leave:
            return respond_error('This worker is on leave on the selected date. Please choose another date.')

        start_t, end_t = parse_time_range(time)

        # Same rule as the recurring path: this is a customer-scheduled slot,
        # so anything that is not a real, non-empty same-day window is
        # rejected instead of falling back to exact-string matching.
        if start_t is None or end_t is None:
            return respond_error('Please select a valid time slot.')

        with transaction.atomic():
            locked_worker = WorkerProfile.objects.select_for_update().get(id=worker.id)

            duplicate_user_booking = Booking.objects.filter(
                customer=request.user, worker=locked_worker, service=service, date=date,
                status__in=['PENDING', 'ACCEPTED', 'IN_PROGRESS']
            ).exists()

            if duplicate_user_booking:
                return respond_error('You already have an active booking with this worker for the same service and date.')

            if start_t and end_t:
                existing_booking = has_worker_schedule_conflict(
                    locked_worker,
                    booking_date=booking_date,
                    start_time=start_t,
                    end_time=end_t,
                    exclude_booking_id=None,
                )
            else:
                existing_booking = has_worker_schedule_conflict(
                    locked_worker,
                    booking_date=booking_date,
                    time_value=time,
                    exclude_booking_id=None,
                )

            if existing_booking:
                return respond_error('This worker is already booked for the selected time.')

            lat_val = _safe_coord(latitude)
            lng_val = _safe_coord(longitude)

            if lat_val is not None and lng_val is not None:
                location_district = _validate_location_in_district(lat_val, lng_val, district)
                if location_district is None:
                    return respond_error(f'Please select a service location within {district}.')

            booking = Booking.objects.create(
                customer=request.user, worker=locked_worker, service=service,
                booking_type=booking_type, date=date, time=time,
                start_time=start_t, end_time=end_t,
                location=location, description=description, phone=phone,
                latitude=lat_val,
                longitude=lng_val,
                district=district,
                estimated_cost=service.price, status='PENDING'
            )
            booking.generate_arrival_otp()

            Notification.objects.create(
                recipient=worker.user, booking=booking, notification_type='BOOKING',
                message=f'New booking request from {request.user.get_full_name() or request.user.username} for {service.name} on {date}.'
            )

            admins = CustomUser.objects.filter(role='ADMIN', is_active=True)
            for admin in admins:
                Notification.objects.create(
                    recipient=admin, booking=booking, notification_type='BOOKING',
                    message=f'New booking #{booking.id}: {service.name} booked by {request.user.get_full_name() or request.user.username}.'
                )

        # Booking request confirmation (Resend-aware; failures are logged, never raised)
        send_email_safely(
            subject=f'Booking Confirmed - #{booking.id}',
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
        )

        messages.success(request, 'Booking submitted successfully!')

        if is_ajax:
            return JsonResponse({
                'success': True,
                'redirect_url': reverse('my_bookings'),
                'message': 'Booking submitted successfully!'
            })

        return redirect('my_bookings')


# ============================================================
# MANUAL PAYMENT CONFIRMATION
# ============================================================
# LAMO has no payment-gateway integration. A customer submission is only a
# manual payment report; an admin must verify it before the booking is paid.

# A booking can only be paid once a worker has accepted it.
MANUAL_PAYMENT_ALLOWED_STATUSES = (
    'ACCEPTED',
    'IN_PROGRESS',
    'COMPLETION_REQUESTED',
    'COMPLETED',
)

# Payment methods the UI offers. Anything else falls back to UPI.
MANUAL_PAYMENT_METHODS = ('UPI', 'CARD', 'NETBANKING', 'CASH')


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

    # Cancelled / rejected / not-yet-accepted bookings are never payable.
    if booking.status not in MANUAL_PAYMENT_ALLOWED_STATUSES:
        messages.error(
            request,
            'This booking cannot be paid for at its current status. Please '
            'wait until the request is accepted by a worker.'
        )
        return redirect('my_bookings')

    if booking.is_paid:
        messages.info(request, 'This booking has already been paid.')
        return redirect('my_bookings')

    # The amount always comes from the server, never from the request.
    amount = booking.estimated_cost or booking.service.price
    if amount is None or amount <= 0:
        messages.error(
            request,
            'The payable amount for this booking is not available yet. '
            'Please contact support.'
        )
        return redirect('my_bookings')

    payment_method = request.POST.get('payment_method', '').strip().upper()
    if payment_method not in MANUAL_PAYMENT_METHODS:
        payment_method = 'UPI'

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

    messages.success(
        request,
        'Your manual payment report was submitted and is awaiting admin '
        'verification. The booking will remain unpaid until it is verified.'
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

        if booking.status not in MANUAL_PAYMENT_ALLOWED_STATUSES:
            messages.error(
                request,
                'This booking cannot be marked paid at its current status.'
            )
            return redirect('admin_payments')

        if payment.status != 'PENDING':
            messages.info(request, 'This payment is no longer awaiting verification.')
            return redirect('admin_payments')

        payment.status = 'PAID'
        payment.paid_at = timezone.now()
        payment.save(update_fields=['status', 'paid_at', 'updated_at'])

        booking.is_paid = True
        booking.payment_method = payment.payment_method
        booking.save(update_fields=['is_paid', 'payment_method', 'updated_at'])

    messages.success(request, 'Payment verified and booking marked as paid.')
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
    return render(
        request,
        'booking_messages.html',
        {
            'booking': booking,
            'chat_messages': thread,
        }
    )


@login_required
def worker_accept_booking(request, booking_id):
    if request.user.role != 'WORKER':
        messages.error(request, 'You are not authorized.')
        return redirect('dashboard')

    worker = get_object_or_404(WorkerProfile, user=request.user)

    # Check worker verification and suspension
    is_verified, error_msg = _check_worker_verification(worker)
    if not is_verified:
        messages.error(request, error_msg)
        return redirect('worker_dashboard')

    with transaction.atomic():
        locked_worker = WorkerProfile.objects.select_for_update().get(id=worker.id)
        booking = get_object_or_404(
            Booking.objects.select_related('customer', 'worker__user', 'service'),
            id=booking_id, worker=locked_worker
        )

        if booking.status != 'PENDING':
            messages.warning(request, 'This booking has already been processed.')
            return redirect('worker_dashboard')

        # Check 30-minute response timer expiry
        if booking.created_at and timezone.now() > booking.created_at + timedelta(minutes=30):
            booking.status = 'EXPIRED'
            booking.cancellation_reason = 'Worker did not respond within the 30-minute response window.'
            booking.cancelled_by = 'SYSTEM'
            booking.save(update_fields=['status', 'cancellation_reason', 'cancelled_by', 'updated_at'])
            Notification.objects.create(
                recipient=booking.customer, booking=booking, notification_type='BOOKING_UPDATE',
                message=f'Your {booking.service.name} booking request #{booking.id} has expired because it was not accepted within 30 minutes.'
            )
            messages.error(request, 'This booking request has expired (30-minute response timer elapsed).')
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
            messages.error(request, 'Cannot accept - overlaps with another booking.')
            return redirect('worker_dashboard')

        booking.status = 'ACCEPTED'
        booking.generate_arrival_otp()
        booking.save(update_fields=['status', 'arrival_otp', 'updated_at'])

    Notification.objects.create(
        recipient=booking.customer, booking=booking, notification_type='BOOKING_UPDATE',
        message=f'Good news! Your {booking.service.name} booking has been accepted by {request.user.get_full_name() or request.user.username}.'
    )

    # Booking accepted (Resend-aware; failures are logged, never raised)
    send_email_safely(
        subject=f'Good News! Your Booking #{booking.id} is Confirmed',
        message=(
            f'Hi {booking.customer.username},\n\n'
            f'Great news! Your {booking.service.name} booking has been accepted by {request.user.get_full_name() or request.user.username}.\n\n'
            f'Booking Details:\n'
            f'  Date: {booking.date}\n'
            f'  Time: {booking.time}\n\n'
            f'The worker will reach out to you shortly. If you have any questions, feel free to reach out to us.\n\n'
            f'Best regards,\nTeam LAMO'
        ),
        recipient_list=[booking.customer.email],
        purpose='booking_accepted',
    )

    admins = CustomUser.objects.filter(role='ADMIN', is_active=True)
    for admin in admins:
        Notification.objects.create(
            recipient=admin, booking=booking, notification_type='BOOKING_UPDATE',
            message=f'Booking #{booking.id} has been accepted by {request.user.get_full_name() or request.user.username}.'
        )

    messages.success(request, 'Booking accepted successfully.')
    return redirect('worker_dashboard')


@login_required
def worker_reject_booking(request, booking_id):
    if request.user.role != 'WORKER':
        messages.error(request, 'You are not authorized.')
        return redirect('dashboard')

    worker = get_object_or_404(WorkerProfile, user=request.user)

    # Check worker verification
    is_verified, error_msg = _check_worker_verification(worker)
    if not is_verified:
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
        messages.info(request, 'Emergency request dismissed.')
        return redirect('worker_dashboard')

    booking = get_object_or_404(Booking, id=booking_id, worker=worker)

    if booking.status != 'PENDING':
        messages.warning(request, 'This booking has already been processed.')
        return redirect('worker_dashboard')

    rejection_reason = request.POST.get('rejection_reason', '').strip() or request.GET.get('reason', '').strip() or 'Worker declined the booking request.'
    booking.status = 'REJECTED'
    booking.cancellation_reason = rejection_reason
    booking.cancelled_by = 'WORKER'
    booking.save(update_fields=['status', 'cancellation_reason', 'cancelled_by', 'updated_at'])

    Notification.objects.create(
        recipient=booking.customer, booking=booking, notification_type='BOOKING_UPDATE',
        message=f'Your {booking.service.name} booking has been declined by the worker.'
    )

    # Booking rejected (Resend-aware; failures are logged, never raised)
    send_email_safely(
        subject=f'Update on Booking #{booking.id}',
        message=(
            f'Hi {booking.customer.username},\n\n'
            f'Unfortunately, your {booking.service.name} booking on {booking.date} has been declined by the worker.\n\n'
            f'Don\'t worry! You can browse and book another available worker on our platform.\n\n'
            f'If you need any assistance, we\'re here to help.\n\n'
            f'Best regards,\nTeam LAMO'
        ),
        recipient_list=[booking.customer.email],
        purpose='booking_rejected',
    )

    messages.success(request, 'Booking rejected.')
    return redirect('worker_dashboard')


@login_required
def worker_accept_emergency(request, booking_id):
    """Worker accepts an emergency request - first accept wins"""
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
            # Lock the booking row to prevent race conditions
            booking = Booking.objects.select_for_update().select_related(
                'customer', 'service'
            ).get(
                id=booking_id,
                status='EMERGENCY_PENDING',
                worker__isnull=True
            )

            # Double-check status after locking (prevent race condition)
            if booking.status != 'EMERGENCY_PENDING' or booking.worker is not None:
                messages.warning(request, 'This emergency request has already been accepted by another worker.')
                return redirect('worker_dashboard')

            # Only workers offering this trade may take the job. Without this
            # check any worker could accept any other trade's emergency
            # request (an electrician accepting a plumbing job, etc.).
            if worker.service_id is None or worker.service_id != booking.service_id:
                messages.error(request, 'This emergency request is for a different service than yours.')
                return redirect('worker_dashboard')

            # The emergency broadcast is dateless, so check leave/conflicts
            # against the acceptance day and then stamp it onto the booking.
            # Stamping Booking.date is what makes the accepted job visible to
            # later has_worker_schedule_conflict() checks (they filter on
            # date=) and to WorkerLeave matching.
            today = timezone.localdate()

            # Re-check worker leave status at acceptance time
            try:
                worker_on_leave = WorkerLeave.objects.filter(
                    worker=worker, leave_date=today
                ).exists()
                if worker_on_leave:
                    messages.error(request, 'Cannot accept emergency booking - you are marked as on leave for this date.')
                    return redirect('worker_dashboard')
            except Exception:
                # WorkerLeave table may not exist yet (migration not applied)
                pass

            # Check for time conflicts with existing active bookings
            start_t, end_t = parse_time_range(booking.time)

            if start_t and end_t:
                existing_booking = has_worker_schedule_conflict(
                    worker,
                    booking_date=today,
                    start_time=start_t,
                    end_time=end_t,
                    exclude_booking_id=booking.id,
                )
            else:
                existing_booking = has_worker_schedule_conflict(
                    worker,
                    booking_date=today,
                    time_value=booking.time,
                    exclude_booking_id=booking.id,
                )

            if existing_booking:
                messages.error(request, 'Cannot accept emergency booking - you already have an active booking at this time today.')
                return redirect('worker_dashboard')

            # Assign this worker to the booking. Stamping date=today (the
            # acceptance day) makes this job block the worker's calendar for
            # later conflict checks and lets WorkerLeave matching see it.
            booking.worker = worker
            booking.date = today
            booking.status = 'ACCEPTED'
            booking.save(update_fields=['worker', 'date', 'status', 'updated_at'])

            # Notify the customer
            Notification.objects.create(
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
                    Notification.objects.create(
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
                    Notification.objects.create(
                        recipient=admin,
                        booking=booking,
                        notification_type='BOOKING_UPDATE',
                        message=f'Emergency booking #{booking.id} accepted by {request.user.username}.'
                    )
                except Exception:
                    continue

    except Booking.DoesNotExist:
        messages.warning(request, 'This emergency request has already been accepted by another worker.')
        return redirect('worker_dashboard')

    # Send email to customer (Resend-aware; failures are logged, never raised)
    send_email_safely(
        subject=f'Worker Assigned - Emergency Request #{booking.id}',
        message=(
            f'Hi {booking.customer.username},\n\n'
            f'Great news! We\'ve found a worker for your emergency {booking.service.name} request.\n\n'
            f'Worker Details:\n'
            f'  Name: {request.user.get_full_name() or request.user.username}\n'
            f'  Location: {booking.location}\n\n'
            f'The worker will contact you shortly. Please keep your phone reachable.\n\n'
            f'Thanks for trusting LAMO in your time of need!\n\n'
            f'Best regards,\nTeam LAMO'
        ),
        recipient_list=[booking.customer.email],
        purpose='emergency_worker_assigned',
    )

    messages.success(request, f'Emergency request accepted! You have been assigned to this booking.')
    return redirect('worker_dashboard')


@login_required
def service_create(request):

    is_admin = (
        request.user.role == 'ADMIN'
        or getattr(request.user, 'is_superuser', False)
        or getattr(request.user, 'is_staff', False)
    )
    if not is_admin:
        messages.error(request, 'You are not authorized to access this page.')
        return redirect('dashboard')
    if request.user.role != 'ADMIN':
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

        # PASSWORD

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
            # DUPLICATE PHONE
            # ------------------------------------------------

            if WorkerProfile.objects.filter(
                phone=phone
            ).exists():

                messages.error(
                    request,
                    'This phone number is already registered.'
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
                    password=password,
                    role=role
                )

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

    if request.method == 'POST':

        # ==================================================
        # BRUTE FORCE PROTECTION (server-side)
        # ==================================================
        # The counters live in Django's cache, NOT in request.session.
        # A session is a client-held cookie: deleting it (or using a fresh
        # private window) used to hand the attacker a brand new set of
        # counters, making the lockout worthless. With the counters on the
        # server, clearing cookies changes nothing.
        username = request.POST.get(
            'username',
            ''
        ).strip()

        rl_key = login_key(request, username)

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

        user = authenticate(
            request,
            username=username,
            password=password
        )

        if user is not None:

            # ==================================================
            # ADMIN / SUPERUSER GUARANTEE (No OTP, Always ADMIN)
            # ==================================================
            is_admin_account = (
                user.role == 'ADMIN'
                or getattr(user, 'is_superuser', False)
                or getattr(user, 'is_staff', False)
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
                    username,
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
@require_POST
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

    if (
        request.user.role == 'ADMIN'
        or getattr(request.user, 'is_superuser', False)
        or getattr(request.user, 'is_staff', False)
    ):
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

    is_admin_user = (
        request.user.role == 'ADMIN'
        or getattr(request.user, 'is_superuser', False)
        or getattr(request.user, 'is_staff', False)
    )

    if not is_admin_user:
        return redirect('dashboard')

    if request.user.role != 'ADMIN':
        request.user.role = 'ADMIN'
        request.user.is_email_verified = True
        request.user.save(update_fields=['role', 'is_email_verified'])

    from django.db.models import Count, Avg

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
        revenue=Sum('estimated_cost', filter=Q(is_paid=True)),
    )
    total_bookings = booking_stats['total'] or 0
    pending_bookings = (booking_stats['pending'] or 0) + (booking_stats['emergency'] or 0)
    completed_bookings = booking_stats['completed'] or 0
    active_bookings = (booking_stats['accepted'] or 0) + (booking_stats['in_progress'] or 0)
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
            'avg_rating': round(avg_rating, 1),
            'recent_bookings': recent_bookings,
            'booking_status_data': booking_status_data,
            'top_workers': top_workers,
            'new_enquiries': new_enquiries,
        }
    )


@never_cache
def worker_dashboard(request):

    if not request.user.is_authenticated:
        return redirect('login')

    if request.user.role != 'WORKER':
        return redirect('dashboard')

    try:
        worker_profile = request.user.worker_profile
    except WorkerProfile.DoesNotExist:
        messages.error(request, 'Worker profile not found.')
        return redirect('dashboard')

    # Automatically expire any pending bookings past 30-min window
    check_and_expire_pending_bookings(worker=worker_profile)

    job_stats = Booking.objects.filter(worker=worker_profile).aggregate(
        total=Count('id'),
        pending=Count('id', filter=Q(status='PENDING')),
    )
    total_jobs = job_stats['total'] or 0
    pending_jobs_count = job_stats['pending'] or 0

    pending_bookings = Booking.objects.filter(
        worker=worker_profile,
        status='PENDING'
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

    completed_bookings = Booking.objects.filter(
        worker=worker_profile,
        status='COMPLETED'
    ).count()
    in_progress_bookings = Booking.objects.filter(
        worker=worker_profile,
        status='IN_PROGRESS'
    ).count()
    cancelled_bookings = Booking.objects.filter(
        worker=worker_profile,
        status='CANCELLED'
    ).count()

    total_earnings = Booking.objects.filter(
        worker=worker_profile,
        status='COMPLETED',
        is_paid=True
    ).aggregate(total=Sum('estimated_cost'))['total'] or 0

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
    # Only this worker's own trade is broadcast here: without the service
    # filter a plumber would see electricians' emergency requests, including
    # other customers' locations/phones, and could accept a job outside
    # their trade.
    emergency_bookings = []
    if worker_profile.is_verified and worker_profile.work_status in ['AVAILABLE', 'ON_DUTY'] and request.user.is_active and worker_profile.service_id:
        dismissed_booking_ids = Notification.objects.filter(
            recipient=request.user,
            notification_type='EMERGENCY',
            is_read=True
        ).values_list('booking_id', flat=True)

        emergency_bookings = Booking.objects.filter(
            status='EMERGENCY_PENDING',
            worker__isnull=True,
            service=worker_profile.service,
        ).exclude(id__in=dismissed_booking_ids).select_related('customer', 'service').order_by('-created_at')[:10]

    emergency_reason = None
    if not emergency_bookings:
        emergency_reason = _emergency_unavailable_reason(worker_profile, request.user)

    context = {
        'worker_profile': worker_profile,
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
    }

    return render(
        request,
        'dashboards/worker_dashboard.html',
        context
    )


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
        status__in=['PENDING', 'ACCEPTED', 'IN_PROGRESS', 'EMERGENCY_PENDING']
    ).count()

    notifications = Notification.objects.filter(
        recipient=request.user,
        is_read=False
    ).order_by('-created_at')[:5]

    context = {
        'profile': profile,
        'bookings': bookings,
        'emergency_bookings': emergency_bookings,
        'total_bookings': total_bookings,
        'completed_bookings': completed_bookings,
        'pending_bookings': pending_bookings,
        'notifications': notifications,
    }

    return render(
        request,
        'dashboards/user_dashboard.html',
        context
    )


@never_cache
@login_required
def change_password_view(request):
    """Let a signed-in user change their own password.

    Unlike the forgot-password flow this asks for the *current* password, so a
    hijacked session on its own cannot be used to lock the real owner out.
    Any outstanding reset token is consumed so a previously emailed link stops
    working once the password changes.
    """
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


@login_required
def edit_profile_view(request):
    if request.user.role == 'WORKER':
        return redirect('edit_worker_profile')

    try:
        profile = request.user.user_profile
    except UserProfile.DoesNotExist:
        profile = UserProfile.objects.create(user=request.user)

    if request.method == 'POST':
        form = UserProfileForm(request.POST, request.FILES, instance=profile)
        if form.is_valid():
            form.save()
            messages.success(request, 'Profile updated successfully.')
            return redirect('user_dashboard')
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
        form = WorkerProfileForm(request.POST, request.FILES, instance=profile)
        if form.is_valid():
            profile = form.save(commit=False)

            if profile.latitude is not None and profile.longitude is not None:
                profile.current_place = _get_place_name_from_coords(
                    profile.latitude,
                    profile.longitude,
                )

            profile.save()
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


@login_required
def delete_account_view(request):

    if request.method != 'POST':
        return redirect('edit_profile')

    # Require password confirmation
    password = request.POST.get('password', '').strip()
    if not password:
        messages.error(request, 'Please enter your password to confirm account deletion.')
        return redirect('edit_profile')

    user = request.user
    if not user.check_password(password):
        messages.error(request, 'Incorrect password. Account deletion cancelled.')
        return redirect('edit_profile')

    active_bookings = Booking.objects.filter(
        customer=user,
        status__in=['ACCEPTED', 'IN_PROGRESS', 'COMPLETION_REQUESTED', 'DISPUTED']
    ).exists()
    if active_bookings:
        messages.error(request, 'Cannot delete account while you have active, in-progress, or disputed bookings.')
        return redirect('edit_profile')

    logout(request)
    user.delete()
    messages.success(request, 'Your account has been permanently deleted.')
    return redirect('home')


@login_required
def admin_delete_user(request, user_id):
    """Admin can delete a user"""
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
    user_to_delete.delete()
    messages.success(request, f'User "{username}" has been deleted successfully.')
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

    # Check if worker has any existing bookings (PROTECT constraint)
    has_bookings = Booking.objects.filter(worker=worker_profile).exists()
    if has_bookings:
        messages.error(
            request,
            f'Cannot delete worker "{username}" because they have existing booking records. '
            f'Please suspend the worker instead to preserve booking history.'
        )
        return redirect('workers_list')

    # Safe to delete - no bookings exist
    worker_profile.user.delete()
    messages.success(request, f'Worker "{username}" has been deleted successfully.')
    return redirect('workers_list')


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
        messages.success(request, f'Worker "{worker_profile.user.username}" has been suspended.')
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
def service_update(request, pk):

    is_admin = (
        request.user.role == 'ADMIN'
        or getattr(request.user, 'is_superuser', False)
        or getattr(request.user, 'is_staff', False)
    )
    if not is_admin:
        messages.error(request, 'You are not authorized to access this page.')
        return redirect('dashboard')
    if request.user.role != 'ADMIN':
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
    is_admin = (
        request.user.role == 'ADMIN'
        or getattr(request.user, 'is_superuser', False)
        or getattr(request.user, 'is_staff', False)
    )
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

    return render(
        request,
        'lamo_service/service_detail.html',
        {
            'service': service
        }
    )

@login_required
def service_workers(request, pk):

    service = get_object_or_404(
        Service,
        pk=pk,
        is_active=True
    )

    district = request.GET.get('district', '').strip()
    ai_requirement = request.GET.get('ai_requirement', '').strip()

    if not district:
        messages.error(request, 'Please select a district to find workers.')
        return redirect('service_detail', pk=service.id)

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
        .select_related('user', 'service')
        .filter(
            service=service,
            is_verified=True,
            work_status='AVAILABLE',
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
        )
        worker.is_bookable = not worker.unavailable_reasons

    bookable_workers = [w for w in workers if w.is_bookable]

    ai_recommendations = []
    if ai_requirement:
        from .ai_utils import build_worker_recommendations
        ai_recommendations = build_worker_recommendations(
            service,
            district=district,
            requirement=ai_requirement,
        )

    district_lat, district_lng = DISTRICT_COORDINATES.get(district, (9.9312, 76.2673))

    return render(
        request,
        'lamo_service/service_workers.html',
        {
            'service': service,
            'district': district,
            'district_lat': district_lat,
            'district_lng': district_lng,
            'workers': workers,
            'bookable_workers': bookable_workers,
            'bookable_count': len(bookable_workers),
            'selected_date': selected_date,
            'selected_time': raw_time,
            'ai_requirement': ai_requirement,
            'ai_recommendations': ai_recommendations,
        }
    )

def service_page(request):
    services = Service.objects.filter(
        is_active=True
    ).order_by('name')

    return render(
        request,
        'lamo_service/service_page.html',
        {
            'services': services
        }
    )
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
        }
    )


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

    worker.save(
        update_fields=[
            'is_verified',
            'updated_at'
        ]
    )


    messages.success(
        request,
        f'{worker.user.username} has been verified successfully.'
    )


    return redirect('workers_list')


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

            if not latitude or not longitude:

                messages.error(
                    request,
                    'Unable to get your location. Please try again.'
                )

                return redirect(
                    'worker_manage_dashboard'
                )

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

            return redirect(
                'worker_manage_dashboard'
            )


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

    return render(
        request,
        'notifications.html',
        {
            'notifications': notifications,
            'unread_count': unread_count,
        }
    )


@login_required
def mark_notification_read(request, notification_id):
    if request.method != 'POST':
        return redirect('notifications')

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

    return redirect('notifications')


@login_required
def mark_all_notifications_read(request):
    if request.method != 'POST':
        return redirect('notifications')

    Notification.objects.filter(
        recipient=request.user,
        is_read=False
    ).update(is_read=True)

    messages.success(request, 'All notifications marked as read.')

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return JsonResponse({'success': True})

    return redirect('notifications')


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
    # anything already in progress is deliberately left alone.
    cancelled = series.bookings.filter(
        date__gte=today,
        status__in=open_statuses
    ).update(status='CANCELLED')

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

    # Get bookings assigned to this worker
    bookings = Booking.objects.filter(
        worker=worker
    ).select_related(
        'customer',
        'service'
    ).order_by(
        '-created_at'
    )

    # Get emergency bookings that are pending (no worker assigned yet).
    # Only this worker's own trade is broadcast here: without the service
    # filter a plumber would see electricians' emergency requests, including
    # other customers' locations/phones, and could accept a job outside
    # their trade.
    emergency_bookings = []
    if worker.is_verified and worker.work_status in ['AVAILABLE', 'ON_DUTY'] and worker.user.is_active and worker.service_id:
        dismissed_booking_ids = Notification.objects.filter(
            recipient=request.user,
            notification_type='EMERGENCY',
            is_read=True
        ).values_list('booking_id', flat=True)

        emergency_bookings = Booking.objects.filter(
            status='EMERGENCY_PENDING',
            worker__isnull=True,
            service=worker.service,
        ).exclude(id__in=dismissed_booking_ids).select_related('customer', 'service').order_by('-created_at')[:10]

    emergency_reason = None
    if not emergency_bookings:
        emergency_reason = _emergency_unavailable_reason(worker, request.user)

    return render(
        request,
        'workers/worker_jobs.html',
        {
            'worker_profile': worker,
            'bookings': bookings,
            'emergency_bookings': emergency_bookings,
            'emergency_reason': emergency_reason,
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


    # ==========================================
    # RENDER
    # ==========================================

    return render(
        request,
        'workers/worker_notifications.html',
        {
            'worker_profile': worker,
            'notifications': notifications,
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

    bookings = (
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
        .order_by('-created_at')
    )

    return render(
        request,
        'user/my_bookings.html',
        {
            'bookings': bookings,
            'payment_allowed_statuses': MANUAL_PAYMENT_ALLOWED_STATUSES,
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
def confirm_completion(request, booking_id):

    # ==========================================
    # POST ONLY
    # ==========================================

    if request.method != 'POST':
        return redirect('my_bookings')


    # ==========================================
    # GET CUSTOMER'S BOOKING
    # ==========================================

    booking = get_object_or_404(
        Booking,
        id=booking_id,
        customer=request.user
    )


    # ==========================================
    # CHECK STATUS
    # ==========================================

    if booking.status != 'COMPLETION_REQUESTED':

        messages.error(
            request,
            'This booking is not waiting for completion confirmation.'
        )

        return redirect('my_bookings')


    # ==========================================
    # COMPLETION REQUESTED → COMPLETED
    # ==========================================

    if booking.final_amount and booking.final_amount > 0:
        booking.estimated_cost = booking.final_amount

    booking.status = 'COMPLETED'

    booking.save(
        update_fields=[
            'status',
            'estimated_cost',
            'updated_at'
        ]
    )


    # ==========================================
    # NOTIFY WORKER + SEND EMAIL
    # ==========================================

    if booking.worker:

        Notification.objects.create(
            recipient=booking.worker.user,
            booking=booking,
            notification_type='BOOKING_UPDATE',
            message=(
                f'The customer has confirmed that the '
                f'{booking.service.name} work is complete.'
            )
        )

        # Job completed, worker notified (Resend-aware; failures logged, never raised)
        send_email_safely(
            subject=f'Job Completed - Booking #{booking.id}',
            message=(
                f'Hi {booking.worker.user.username},\n\n'
                f'Congratulations! The customer has confirmed that your {booking.service.name} work for Booking #{booking.id} is complete.\n\n'
                f'Payment will be processed shortly. Thank you for your excellent service!\n\n'
                f'Best regards,\nTeam LAMO'
            ),
            recipient_list=[booking.worker.user.email],
            purpose='job_completed_worker_notified',
        )


    # ==========================================
    # SUCCESS MESSAGE
    # ==========================================

    messages.success(
        request,
        'Work confirmed successfully. You can now record payment and leave a review.'
    )


    # ==========================================
    # BACK TO MY BOOKINGS
    # ==========================================

    return redirect('my_bookings')


@login_required
@require_POST
def dispute_booking(request, booking_id):
    """Customer disputes the service completion or final amount."""
    if request.user.role != 'USER':
        messages.error(request, 'Only customers can dispute a booking.')
        return redirect('dashboard')

    booking = get_object_or_404(
        Booking.objects.select_related('customer', 'worker__user', 'service'),
        id=booking_id,
        customer=request.user
    )

    if booking.status != 'COMPLETION_REQUESTED':
        messages.error(request, 'Disputes can only be raised when completion is requested.')
        return redirect('my_bookings')

    dispute_reason = request.POST.get('dispute_reason', '').strip()
    if not dispute_reason:
        messages.error(request, 'Please provide a reason for disputing this booking.')
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

    # Arrival OTP verification: only enforced if booking has an arrival_otp set
    arrival_otp = request.POST.get('arrival_otp', '').strip()
    if booking.arrival_otp:
        if not arrival_otp or arrival_otp != booking.arrival_otp:
            messages.error(
                request,
                'Invalid Arrival OTP. Please ask the customer for their 6-digit verification code.'
            )
            return redirect('worker_jobs')

    booking.status = 'IN_PROGRESS'
    booking.arrival_verified_at = timezone.now()
    booking.save(update_fields=['status', 'arrival_verified_at', 'updated_at'])

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

    return redirect('worker_dashboard')

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
    # GET THIS WORKER'S BOOKING
    # ==========================================

    booking = get_object_or_404(
        Booking,
        id=booking_id,
        worker=worker
    )


    # ==========================================
    # ONLY IN-PROGRESS JOB CAN REQUEST COMPLETION
    # ==========================================

    if booking.status != 'IN_PROGRESS':

        messages.warning(
            request,
            'Only an in-progress job can request completion.'
        )

        return redirect('worker_jobs')

    # Parse and validate final service amount
    raw_amount = request.POST.get('final_amount', '').strip()
    final_amount = None
    if raw_amount:
        try:
            parsed = Decimal(raw_amount)
            if parsed > 0:
                final_amount = parsed
        except (ValueError, TypeError, ArithmeticError):
            final_amount = None

    if not final_amount:
        final_amount = booking.estimated_cost or (booking.service.price if booking.service else Decimal('0.00'))

    # ==========================================
    # CHANGE STATUS
    # IN_PROGRESS → COMPLETION_REQUESTED
    # ==========================================

    booking.final_amount = final_amount
    booking.status = 'COMPLETION_REQUESTED'

    booking.save(
        update_fields=[
            'status',
            'final_amount',
            'updated_at'
        ]
    )


    # ==========================================
    # NOTIFY CUSTOMER + SEND EMAIL
    # ==========================================

    Notification.objects.create(
        recipient=booking.customer,
        booking=booking,
        notification_type='BOOKING_UPDATE',
        message=(
            f'Your {booking.service.name} booking has been '
            f'marked as completed by the worker with final amount ₹{final_amount:.2f}. '
            f'Please verify the work and confirm payment.'
        )
    )

    # Worker requested completion, customer notified (Resend-aware; failures logged, never raised)
    send_email_safely(
        subject=f'Job Completed - Please Verify Booking #{booking.id}',
        message=(
            f'Hi {booking.customer.username},\n\n'
            f'The worker has completed your {booking.service.name} job with final amount ₹{final_amount:.2f}.\n\n'
            f'Please take a moment to verify the work and confirm in your LAMO dashboard, or submit a dispute if there are any issues.\n\n'
            f'Thank you for choosing LAMO!\n\n'
            f'Best regards,\nTeam LAMO'
        ),
        recipient_list=[booking.customer.email],
        purpose='job_completion_requested',
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


@login_required
def cancel_booking(request, booking_id):
    if request.method != 'POST':
        return redirect('my_bookings')

    # Enforce ownership: customer can only cancel their own bookings
    booking = get_object_or_404(Booking, id=booking_id, customer=request.user)

    # Block invalid status transitions
    if not _is_valid_booking_transition(booking.status, 'CANCELLED'):
        messages.error(request, "This booking cannot be cancelled at its current status.")
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

    # Notify worker
    if booking.worker and booking.worker.user:
        Notification.objects.create(
            recipient=booking.worker.user,
            booking=booking,
            notification_type='BOOKING_UPDATE',
            message=f"Booking #{booking.id} for {booking.service.name} was cancelled by the customer. Reason: {cancellation_reason}"
        )

    # Notify admins
    admins = CustomUser.objects.filter(role='ADMIN', is_active=True)
    for admin_user in admins:
        Notification.objects.create(
            recipient=admin_user,
            booking=booking,
            notification_type='BOOKING_UPDATE',
            message=f"Booking #{booking.id} ({booking.service.name}) was cancelled by customer {request.user.username} (Reason: {cancellation_reason})."
        )

    # Send email to worker (Resend-aware; failures logged, never raised)
    if booking.worker and booking.worker.user:
        send_email_safely(
            subject=f'Booking #{booking.id} Cancelled',
            message=(
                f'Hi {booking.worker.user.username},\n\n'
                f'Unfortunately, the customer has cancelled Booking #{booking.id} for {booking.service.name} on {booking.date}.\n'
                f'Reason: {cancellation_reason}\n\n'
                f'No worries! You\'re available for new bookings. Feel free to check your dashboard for new opportunities.\n\n'
                f'Best regards,\nTeam LAMO'
            ),
            recipient_list=[booking.worker.user.email],
            purpose='booking_cancelled',
        )

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
    booking = get_object_or_404(Booking, id=booking_id, worker=worker)

    if booking.status != 'ACCEPTED':
        messages.error(request, 'Only accepted bookings before work begins can be cancelled by a worker.')
        return redirect('worker_jobs')

    cancellation_reason = request.POST.get('cancellation_reason', '').strip()
    if not cancellation_reason:
        messages.error(request, 'Please provide a reason for cancelling this booking.')
        return redirect('worker_jobs')

    booking.status = 'CANCELLED'
    booking.cancellation_reason = cancellation_reason
    booking.cancelled_by = 'WORKER'
    booking.save(update_fields=['status', 'cancellation_reason', 'cancelled_by', 'updated_at'])

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
    Notification.objects.create(
        recipient=booking.customer,
        booking=booking,
        notification_type='BOOKING_UPDATE',
        message=f"The assigned worker had to cancel Booking #{booking.id} ({booking.service.name}). Reason: {cancellation_reason}. You can easily select another worker."
    )

    # Notify admin
    admins = CustomUser.objects.filter(role='ADMIN', is_active=True)
    for admin_user in admins:
        Notification.objects.create(
            recipient=admin_user,
            booking=booking,
            notification_type='BOOKING_UPDATE',
            message=f"Worker {request.user.username} cancelled confirmed Booking #{booking.id} ({cancellation_reason}). Logged for review."
        )

    send_email_safely(
        subject=f'Booking #{booking.id} Cancelled by Worker',
        message=(
            f'Hi {booking.customer.username},\n\n'
            f'We regret to inform you that your worker had to cancel Booking #{booking.id} for {booking.service.name}.\n'
            f'Reason: {cancellation_reason}\n\n'
            f'You can choose another available worker right now from your bookings page.\n\n'
            f'Best regards,\nTeam LAMO'
        ),
        recipient_list=[booking.customer.email],
        purpose='worker_booking_cancelled',
    )

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
    scheduled_time = booking.start_time or tm_cls(9, 0)
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
    scheduled_time = booking.start_time or tm_cls(9, 0)
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
        if status in valid_statuses:
            complaint.status = status
            complaint.admin_remark = admin_remark
            complaint.save(update_fields=['status', 'admin_remark'])
            messages.success(request, f"Complaint #{complaint.id} status updated to {status}.")

            Notification.objects.create(
                recipient=complaint.customer,
                booking=complaint.booking,
                notification_type='SYSTEM',
                message=f"Your complaint regarding Booking #{complaint.booking.id} status was updated to '{status}'."
            )

            if complaint.booking.worker:
                Notification.objects.create(
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

    requirement = request.POST.get('requirement', '').strip() or request.GET.get('requirement', '').strip()
    district = request.POST.get('district', '').strip() or request.GET.get('district', '').strip()
    place = request.POST.get('place', '').strip() or request.GET.get('place', '').strip()
    latitude = request.POST.get('latitude', '').strip() or request.GET.get('latitude', '').strip()
    longitude = request.POST.get('longitude', '').strip() or request.GET.get('longitude', '').strip()

    if not requirement:
        return JsonResponse({'success': False, 'message': 'Please describe your requirement.'}, status=400)

    if not district:
        return JsonResponse({'success': False, 'message': 'Please select your district.'}, status=400)

    customer_address = ''
    if getattr(request.user, 'user_profile', None):
        customer_address = request.user.user_profile.address or ''

    if not place and customer_address:
        place = customer_address.split(',')[0].strip() or customer_address.strip()

    recommendations = build_worker_recommendations(
        service,
        district=district,
        requirement=requirement,
        customer_lat=latitude,
        customer_lng=longitude,
        customer_place=place,
        customer_address=customer_address,
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
            'worker_image': worker.profile_image.url if worker and worker.profile_image else '',
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

        # --------------------------------------------------
        # RESPONSE
        # --------------------------------------------------
        return JsonResponse({

            'success': True,

            'type': 'service_match',

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
