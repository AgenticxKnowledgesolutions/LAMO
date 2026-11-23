"""Real-database double-booking race test (MySQL / TiDB).

SQLite ignores ``SELECT ... FOR UPDATE``, so the normal suite cannot prove the
booking lock works. This test mirrors the exact pattern ``book_service`` uses:

    atomic():  lock the worker row  ->  plain SELECT for overlapping bookings  ->  INSERT

and fires many threads at the same worker and time slot. Exactly ONE may win.

It is SKIPPED unless you opt in, because it needs a real server and creates a
temporary ``test_<name>`` database there:

    # use a THROWAWAY cluster / database, never production
    set LAMO_REAL_DB_RACE_TEST=1
    set DATABASE_URL=mysql://USER:PASSWORD@HOST:4000/anyname
    python manage.py test lamoapp.tests_real_db_booking_race --noinput

Run it once without DJANGO_DB_ISOLATION_LEVEL and once with
DJANGO_DB_ISOLATION_LEVEL="read committed". If the first run lets two bookings
through on TiDB, set the variable in Render and re-run to prove the fix.
"""
import os
import threading
from datetime import date, time, timedelta
from decimal import Decimal
from unittest import skipUnless

from django.db import OperationalError, connection, connections, transaction
from django.test import TransactionTestCase

from .models import Booking, CustomUser, Service, WorkerProfile, WorkerWallet
from .views import has_worker_schedule_conflict

ENABLED = os.environ.get('LAMO_REAL_DB_RACE_TEST') == '1' and connection.vendor == 'mysql'


@skipUnless(ENABLED, 'Set LAMO_REAL_DB_RACE_TEST=1 and point DATABASE_URL at a throwaway MySQL/TiDB database.')
class RealDbDoubleBookingRaceTest(TransactionTestCase):
    THREADS = 8

    def setUp(self):
        self.service = Service.objects.create(
            name='Race Plumbing', description='x', price=Decimal('350.00'), is_active=True, default_duration_min=45
        )
        worker_user = CustomUser.objects.create_user(
            username='race_worker', email='race_worker@test.com', password='Password123!', role='WORKER'
        )
        self.worker = WorkerProfile.objects.create(
            user=worker_user, service=self.service, district='Kollam', is_verified=True, work_status='AVAILABLE',
            work_start_time=time(9, 0), work_end_time=time(21, 0),
        )
        WorkerWallet.objects.create(worker=self.worker, balance=Decimal('1000.00'))
        self.customers = [
            CustomUser.objects.create_user(
                username=f'race_cust_{i}', email=f'race_cust_{i}@test.com', password='Password123!', role='USER'
            )
            for i in range(self.THREADS)
        ]
        self.day = date.today() + timedelta(days=3)

    def _attempt(self, customer, results, errors, barrier):
        try:
            barrier.wait(timeout=30)
            for attempt in range(8):
                try:
                    with transaction.atomic():
                        locked = WorkerProfile.objects.select_for_update().get(id=self.worker.id)
                        busy = has_worker_schedule_conflict(
                            locked, booking_date=self.day, start_time=time(10, 0), end_time=time(11, 0)
                        )
                        if busy:
                            results.append('conflict')
                            return
                        Booking.objects.create(
                            customer=customer, worker=locked, service=self.service, date=self.day,
                            time='10:00 AM - 10:30 AM', start_time=time(10, 0), end_time=time(11, 0),
                            location='Kollam', phone='9876543210', status='PENDING',
                        )
                        results.append('created')
                        return
                except OperationalError:
                    # Lock wait timeout / write conflict: retry like a client would.
                    continue
            errors.append('retries exhausted')
        except Exception as exc:  # noqa: BLE001 - reported by the assertion
            errors.append(repr(exc))
        finally:
            connections.close_all()

    def test_only_one_booking_wins_the_slot(self):
        results, errors = [], []
        barrier = threading.Barrier(self.THREADS)
        threads = [
            threading.Thread(target=self._attempt, args=(c, results, errors, barrier)) for c in self.customers
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        self.assertEqual(errors, [], errors)
        stored = Booking.objects.filter(worker=self.worker, date=self.day).count()
        self.assertEqual(
            stored, 1,
            f'DOUBLE BOOKING: {stored} bookings were stored for one worker/slot '
            f'(results={sorted(results)}). On TiDB set DJANGO_DB_ISOLATION_LEVEL="read committed".',
        )
        self.assertEqual(results.count('created'), 1)
        self.assertEqual(results.count('conflict'), self.THREADS - 1)
