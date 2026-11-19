from io import StringIO
from django.core.management import call_command
from django.test import TestCase
from django.core.cache import cache

from lamoapp.booking_reminders import (
    run_scheduled_booking_maintenance,
    MAINTENANCE_LOCK_KEY,
)


class BookingCronMaintenanceTest(TestCase):
    def setUp(self):
        cache.clear()

    def test_management_command_runs_successfully(self):
        """Management command executes cleanly and prints summary output."""
        out = StringIO()
        call_command('process_booking_reminders', stdout=out)
        output = out.getvalue()
        self.assertIn('Booking maintenance complete', output)

    def test_concurrency_lock_prevents_overlapping_runs(self):
        """When a lock is held, a concurrent run skips gracefully without error."""
        cache.set(MAINTENANCE_LOCK_KEY, 'another_process_token', timeout=300)
        out = StringIO()
        call_command('process_booking_reminders', stdout=out)
        output = out.getvalue()
        self.assertIn('Booking maintenance skipped', output)

    def test_run_scheduled_booking_maintenance_idempotent(self):
        """Repeated maintenance passes execute idempotently without errors."""
        res1 = run_scheduled_booking_maintenance()
        self.assertEqual(res1['errors'], 0)
        self.assertFalse(res1['skipped'])

        res2 = run_scheduled_booking_maintenance()
        self.assertEqual(res2['errors'], 0)
        self.assertFalse(res2['skipped'])
