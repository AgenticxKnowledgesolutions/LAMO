from django.core.management.base import BaseCommand
from lamoapp.booking_reminders import process_booking_schedule_notifications


class Command(BaseCommand):
    help = "Process scheduled booking reminders, worker arrival checks, and no-show alerts"

    def handle(self, *args, **options):
        sent = process_booking_schedule_notifications()
        self.stdout.write(
            self.style.SUCCESS(f"Successfully processed booking schedule notifications. ({sent} notification batches sent)")
        )

