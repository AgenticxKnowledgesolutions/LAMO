from django.core.management.base import BaseCommand, CommandError
from lamoapp.booking_reminders import run_scheduled_booking_maintenance


class Command(BaseCommand):
    help = (
        "Run time-based booking maintenance: expire unanswered requests and "
        "abandoned accepted jobs, send schedule reminders / arrival checks / "
        "no-show alerts, and follow up stuck jobs. Safe to run every few minutes "
        "(idempotent; can overlap with the page-load trigger)."
    )

    def handle(self, *args, **options):
        results = run_scheduled_booking_maintenance()
        if results.get('skipped'):
            self.stdout.write("Booking maintenance skipped: another run is in progress.")
            return
        if results.get('errors'):
            # Non-zero exit so the scheduler (Render cron) shows the run as failed.
            raise CommandError(f"{results['errors']} maintenance step(s) failed; see the logs.")
        self.stdout.write(
            self.style.SUCCESS(
                "Booking maintenance complete: "
                f"{results['expired_requests']} request(s) expired, "
                f"{results['expired_accepted']} abandoned job(s) expired, "
                f"{results['reminders']} reminder batch(es), "
                f"{results['stuck_followups']} stuck-job follow-up(s)."
            )
        )
