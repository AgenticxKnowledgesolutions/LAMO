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

                # Prompt customer if worker arrived
                pin_text = f" Doorstep PIN: {booking.arrival_otp}." if booking.arrival_otp else ""
                Notification.objects.create(
                    recipient=booking.customer,
                    booking=booking,
                    notification_type='BOOKING_UPDATE',
                    message=(
                        f"Arrival Check: Your {booking.service.name} service slot started at {booking.time}. "
                        f"Has {worker_name} arrived? When they arrive at your door, share your{pin_text} to start the job."
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
                            f"started at {booking.time}. Please verify the customer's Doorstep PIN upon arrival to start."
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

    return reminders_sent

