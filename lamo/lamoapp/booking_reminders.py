import logging
from datetime import datetime, timedelta
from django.utils import timezone
from .models import Booking, Notification
from .push_utils import send_web_push_async

logger = logging.getLogger(__name__)


def process_booking_schedule_notifications(now=None):
    """
    Scans active accepted bookings and delivers:
      1. Upcoming appointment reminders (30 to 60 minutes before slot start).
      2. Arrival check notifications to customer (10 to 30 minutes after slot start if job not started).
      3. No-show eligibility notices (30+ minutes after slot start if job still not started).

    All notifications are idempotent: sent at most once per booking/milestone.
    """
    if now is None:
        now = timezone.localtime()

    today = now.date()

    # We only look at ACCEPTED bookings scheduled for today
    bookings = Booking.objects.filter(
        status='ACCEPTED',
        date=today
    ).select_related('customer', 'worker__user', 'service')

    reminders_sent = 0

    for booking in bookings:
        try:
            # Determine slot start time
            start_t = booking.start_time
            if not start_t and booking.time:
                from .views import parse_time_range
                start_t, _ = parse_time_range(booking.time)

            if not start_t:
                continue

            try:
                scheduled_dt = timezone.make_aware(
                    datetime.combine(today, start_t),
                    timezone.get_current_timezone()
                )
            except Exception:
                continue

            time_diff = (now - scheduled_dt).total_seconds() / 60.0  # in minutes

            # -------------------------------------------------------------
            # Milestone 1: Upcoming Slot Reminder (-60m to 0m before start)
            # -------------------------------------------------------------
            if -60.0 <= time_diff < 0.0:
                already_reminded = Notification.objects.filter(
                    booking=booking,
                    notification_type='BOOKING_UPDATE',
                    message__startswith='Upcoming Service Reminder:'
                ).exists()

                if not already_reminded:
                    worker_name = (
                        booking.worker.user.get_full_name() or booking.worker.user.username
                        if booking.worker and booking.worker.user else "Your worker"
                    )
                    customer_name = booking.customer.get_full_name() or booking.customer.username

                    # Customer Reminder
                    Notification.objects.create(
                        recipient=booking.customer,
                        booking=booking,
                        notification_type='BOOKING_UPDATE',
                        message=(
                            f"Upcoming Service Reminder: Your {booking.service.name} appointment is scheduled for "
                            f"today at {booking.time} with {worker_name}."
                        )
                    )

                    # Worker Reminder
                    if booking.worker and booking.worker.user:
                        Notification.objects.create(
                            recipient=booking.worker.user,
                            booking=booking,
                            notification_type='BOOKING_UPDATE',
                            message=(
                                f"Upcoming Service Reminder: You have a {booking.service.name} booking today at "
                                f"{booking.time} for {customer_name} at {booking.location}."
                            )
                        )

                    reminders_sent += 1

            # -------------------------------------------------------------
            # Milestone 2: Slot Started / Arrival Check (+10m to +30m)
            # -------------------------------------------------------------
            elif 10.0 <= time_diff < 30.0:
                already_checked = Notification.objects.filter(
                    booking=booking,
                    notification_type='BOOKING_UPDATE',
                    message__startswith='Arrival Check:'
                ).exists()

                if not already_checked:
                    worker_name = (
                        booking.worker.user.get_full_name() or booking.worker.user.username
                        if booking.worker and booking.worker.user else "your service professional"
                    )

                    # Prompt customer if worker arrived (Two-Way Handshake with Doorstep QR Code)
                    pin_fallback = f" (or share Arrival PIN {booking.arrival_otp} if camera scanning is unavailable)" if booking.arrival_otp else ""
                    badge_code = booking.get_worker_badge_code
                    Notification.objects.create(
                        recipient=booking.customer,
                        booking=booking,
                        notification_type='BOOKING_UPDATE',
                        message=(
                            f"Arrival Check: Your {booking.service.name} service slot started at {booking.time}. "
                            f"Has {worker_name} arrived? First check their worker badge (#{badge_code}), then scan their Doorstep QR code{pin_fallback} to begin."
                        )
                    )

                    # Worker prompt to verify arrival
                    if booking.worker and booking.worker.user:
                        Notification.objects.create(
                            recipient=booking.worker.user,
                            booking=booking,
                            notification_type='BOOKING_UPDATE',
                            message=(
                                f"Arrival Check: Your scheduled slot for Booking #{booking.id} ({booking.service.name}) "
                                f"started at {booking.time}. Show your badge (#{badge_code}) and present your Doorstep QR code for customer to scan (or enter their Arrival PIN as backup) to start."
                            )
                        )

                    reminders_sent += 1

            # -------------------------------------------------------------
            # Milestone 3: No-Show Eligibility Alert (30m+ past start time)
            # -------------------------------------------------------------
            elif time_diff >= 30.0:
                already_notified = Notification.objects.filter(
                    booking=booking,
                    notification_type='BOOKING_UPDATE',
                    message__startswith='Service Alert:'
                ).exists()

                if not already_notified:
                    Notification.objects.create(
                        recipient=booking.customer,
                        booking=booking,
                        notification_type='BOOKING_UPDATE',
                        message=(
                            f"Service Alert: Your {booking.service.name} booking was scheduled for {booking.time} and has not "
                            f"yet started. If the professional did not arrive, you are now eligible to report a Worker No-Show "
                            f"from My Bookings to cancel and rebook."
                        )
                    )
                    reminders_sent += 1
        except Exception:
            logger.exception("Error processing schedule notifications for booking #%s", getattr(booking, 'id', None))

    return reminders_sent


# ---------------------------------------------------------------------------
# Stuck-booking follow-up
# ---------------------------------------------------------------------------
# These never change a booking's status or move money: a job that is stuck
# after work started involves the customer's and worker's word, so it is
# escalated to people (reminder to the party who must act, then an admin
# alert). Every message is sent at most once per booking (idempotent markers).

IN_PROGRESS_ADMIN_ALERT_HOURS = 12
BILL_REMINDER_HOURS = 12
BILL_ADMIN_ALERT_HOURS = 48
PAYMENT_ADMIN_ALERT_HOURS = 48


def _notify_once(recipient, booking, prefix, message):
    """Create a notification unless one with this prefix already exists for the booking."""
    if recipient is None:
        return False
    if Notification.objects.filter(recipient=recipient, booking=booking, message__startswith=prefix).exists():
        return False
    Notification.objects.create(
        recipient=recipient,
        booking=booking,
        notification_type='BOOKING_UPDATE',
        message=f"{prefix} {message}",
    )
    return True


def process_stuck_bookings(now=None):
    """Remind / escalate bookings stuck after work started. Returns messages sent."""
    from .models import Booking, CustomUser

    now = now or timezone.now()
    sent = 0
    admins = list(CustomUser.objects.filter(role='ADMIN', is_active=True))

    # 1. Jobs left IN_PROGRESS for too long (worker never submitted the bill).
    stale_in_progress = Booking.objects.filter(
        status='IN_PROGRESS',
        arrival_verified_at__lt=now - timedelta(hours=IN_PROGRESS_ADMIN_ALERT_HOURS),
    ).select_related('worker__user', 'service')
    for b in stale_in_progress:
        try:
            worker_user = b.worker.user if b.worker else None
            sent += _notify_once(
                worker_user, b, 'Job Still Open:',
                f"Booking #{b.id} ({b.service.name}) has been in progress for over "
                f"{IN_PROGRESS_ADMIN_ALERT_HOURS} hours. Please submit the final bill when the work is done."
            )
            for admin in admins:
                sent += _notify_once(
                    admin, b, 'Stuck Job:',
                    f"Booking #{b.id} has been IN PROGRESS for over {IN_PROGRESS_ADMIN_ALERT_HOURS} hours without a bill."
                )
        except Exception:
            logger.exception("Error processing stale in-progress booking #%s", getattr(b, 'id', None))

    # 2. Bill submitted, customer has not confirmed it.
    awaiting_bill = Booking.objects.filter(
        status='COMPLETION_REQUESTED',
        bill_confirmed_at__isnull=True,
        updated_at__lt=now - timedelta(hours=BILL_REMINDER_HOURS),
    ).select_related('customer', 'service')
    for b in awaiting_bill:
        try:
            sent += _notify_once(
                b.customer, b, 'Bill Waiting:',
                f"Your professional submitted the final bill for Booking #{b.id} ({b.service.name}). "
                f"Please review and confirm it, or raise a dispute, from My Bookings."
            )
            if b.updated_at < now - timedelta(hours=BILL_ADMIN_ALERT_HOURS):
                for admin in admins:
                    sent += _notify_once(
                        admin, b, 'Unconfirmed Bill:',
                        f"Booking #{b.id}: the customer has not confirmed the final bill for over {BILL_ADMIN_ALERT_HOURS} hours."
                    )
        except Exception:
            logger.exception("Error processing unconfirmed bill booking #%s", getattr(b, 'id', None))

    # 3. Bill confirmed, payment not yet verified.
    awaiting_payment = Booking.objects.filter(
        status='COMPLETION_REQUESTED',
        bill_confirmed_at__isnull=False,
        bill_confirmed_at__lt=now - timedelta(hours=PAYMENT_ADMIN_ALERT_HOURS),
    ).select_related('worker__user')
    for b in awaiting_payment:
        try:
            worker_user = b.worker.user if b.worker else None
            sent += _notify_once(
                worker_user, b, 'Payment Pending:',
                f"Booking #{b.id}: the customer confirmed the bill. Once you receive the payment, "
                f"tap 'Payment received' to close the job."
            )
            for admin in admins:
                sent += _notify_once(
                    admin, b, 'Unverified Payment:',
                    f"Booking #{b.id}: bill confirmed over {PAYMENT_ADMIN_ALERT_HOURS} hours ago but payment is not verified."
                )
        except Exception:
            logger.exception("Error processing awaiting payment booking #%s", getattr(b, 'id', None))
    return sent


MAINTENANCE_LOCK_KEY = 'booking_maintenance_run_lock'
MAINTENANCE_LOCK_SECONDS = 300


def _acquire_maintenance_lock():
    """Claim the run lock. Returns (acquired, token).

    ``cache.add`` is atomic on the shared (Redis) cache, so only ONE process
    runs a maintenance pass at a time - the cron job and the on-request trigger
    can never process the same bookings concurrently. The lock expires by
    itself, so a crashed run cannot block maintenance forever. If the cache is
    unreachable the pass still runs (the per-notification markers keep it
    idempotent); losing reminders would be worse than the small duplicate risk.
    """
    import uuid
    from django.core.cache import cache
    token = uuid.uuid4().hex
    try:
        return bool(cache.add(MAINTENANCE_LOCK_KEY, token, timeout=MAINTENANCE_LOCK_SECONDS)), token
    except Exception:
        logger.warning("Booking maintenance lock unavailable (cache error); running unlocked.", exc_info=True)
        return True, None


def _release_maintenance_lock(token):
    if token is None:
        return
    from django.core.cache import cache
    try:
        if cache.get(MAINTENANCE_LOCK_KEY) == token:
            cache.delete(MAINTENANCE_LOCK_KEY)
    except Exception:
        pass


def run_scheduled_booking_maintenance():
    """One pass of every time-based booking task (safe to run repeatedly).

    Expires unanswered requests and abandoned accepted jobs, sends schedule
    reminders and escalates stuck jobs.

    * Only one pass runs at a time (shared-cache lock); a second caller returns
      immediately with ``results['skipped'] = True``.
    * Each step is isolated: one failing step is logged and does not stop the
      others, and the work it did not finish is picked up by the next run
      (reminders are marked only when created, so a failed one is retried).
    * Reminder rows are the idempotency markers, so repeated passes never
      send the same reminder twice.
    """
    from . import views as lamo_views

    results = {'expired_requests': 0, 'expired_accepted': 0, 'reminders': 0,
               'stuck_followups': 0, 'errors': 0, 'skipped': False}
    acquired, token = _acquire_maintenance_lock()
    if not acquired:
        results['skipped'] = True
        logger.info("Booking maintenance skipped: another run is in progress.")
        return results

    steps = (
        ('expired_requests', lambda: len(lamo_views.check_and_expire_pending_bookings(_run_maintenance=False))),
        ('expired_accepted', lambda: len(lamo_views.check_and_expire_stale_accepted_bookings())),
        ('reminders', process_booking_schedule_notifications),
        ('stuck_followups', process_stuck_bookings),
    )
    try:
        for name, step in steps:
            try:
                results[name] = step()
            except Exception:
                results['errors'] += 1
                logger.exception("Booking maintenance step %s failed", name)
    finally:
        _release_maintenance_lock(token)
    logger.info("Booking maintenance finished: %s", results)
    return results
