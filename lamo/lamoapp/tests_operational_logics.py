from datetime import time, timedelta
from decimal import Decimal
from django.test import TestCase, Client
from django.utils import timezone
from lamoapp.models import CustomUser, Service, WorkerProfile, Booking, WorkerLeave, Complaint, WorkerWallet
from lamoapp.views import compute_booking_window, _validate_location_in_district


class OperationalLogicsTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.service = Service.objects.create(
            name='Electrician',
            price=Decimal('250.00'),
            is_active=True,
            default_duration_min=45
        )
        self.user = CustomUser.objects.create_user(
            username='testcustomer',
            email='customer@example.com',
            password='password123',
            role='USER'
        )
        self.worker_user = CustomUser.objects.create_user(
            username='testworker',
            email='worker@example.com',
            password='password123',
            role='WORKER'
        )
        self.worker = WorkerProfile.objects.create(
            user=self.worker_user,
            service=self.service,
            district='Kollam',
            is_verified=True,
            work_status='AVAILABLE',
            work_start_time=time(9, 0),
            work_end_time=time(18, 0),
            latitude=Decimal('8.8932'),
            longitude=Decimal('76.6141'),
        )

    def test_dynamic_travel_buffer_scaling(self):
        """Travel buffer scales: <=5km -> 15m, 5-15km -> 30m, >15km -> 45m"""
        # <= 5km
        _, _, _, buf1 = compute_booking_window('10:00 AM - 11:00 AM', service=self.service, distance_km=3.5)
        self.assertEqual(buf1, 15)

        # 5-15km
        _, _, _, buf2 = compute_booking_window('10:00 AM - 11:00 AM', service=self.service, distance_km=12.0)
        self.assertEqual(buf2, 30)

        # > 15km
        _, _, _, buf3 = compute_booking_window('10:00 AM - 11:00 AM', service=self.service, distance_km=25.0)
        self.assertEqual(buf3, 45)

    def test_early_arrival_with_arrival_pin_consent(self):
        """Worker can start early (>30 min before schedule) IF valid arrival PIN is supplied."""
        now = timezone.localtime()
        # Schedule for 2 hours in the future today
        future_time = (now + timedelta(hours=2)).time()
        booking = Booking.objects.create(
            customer=self.user,
            worker=self.worker,
            service=self.service,
            date=now.date(),
            time=f"{future_time.strftime('%I:%M %p')}",
            start_time=future_time,
            status='ACCEPTED',
            arrival_otp='4321'
        )

        # Without PIN -> blocked (>30 mins early)
        can_start, reason = booking.check_can_start(arrival_otp='')
        self.assertFalse(can_start)
        self.assertIn('You can start up to 30 minutes early', reason)

        # With wrong PIN -> blocked
        can_start_wrong, _ = booking.check_can_start(arrival_otp='9999')
        self.assertFalse(can_start_wrong)

        # With correct PIN -> allowed! (Customer consent verified at doorstep)
        can_start_ok, _ = booking.check_can_start(arrival_otp='4321')
        self.assertTrue(can_start_ok)

    def test_kerala_taluk_district_fallback(self):
        """Locations within Kerala district coordinates correctly resolve even if OSM doesn't return district name."""
        # Coordinates in Kollam (Chavara: 8.9989, 76.5398)
        resolved = _validate_location_in_district('8.9989', '76.5398', 'Kollam')
        self.assertEqual(resolved, 'Kollam')

    def test_prevent_start_job_when_another_job_in_progress(self):
        """A worker cannot start a new job while another job is currently IN_PROGRESS."""
        now = timezone.localtime()
        # Active job 1
        job1 = Booking.objects.create(
            customer=self.user,
            worker=self.worker,
            service=self.service,
            date=now.date(),
            time="2:00 PM",
            status='IN_PROGRESS'
        )

        # Job 2 arrives
        job2 = Booking.objects.create(
            customer=self.user,
            worker=self.worker,
            service=self.service,
            date=now.date(),
            time="2:30 PM",
            status='ACCEPTED',
            arrival_otp='5555'
        )

        # Worker attempts to start job2 while job1 is in progress
        can_start, reason = job2.check_can_start(arrival_otp='5555')
        self.assertFalse(can_start)
        self.assertIn('another job currently in progress', reason)

        # Once job1 is completed, job2 can be started
        job1.status = 'COMPLETED'
        job1.save(update_fields=['status'])

        can_start_after, _ = job2.check_can_start(arrival_otp='5555')
        self.assertTrue(can_start_after)

    def test_worker_confirm_transit_with_buffer(self):
        """Worker can confirm transit to next job with a travel buffer, automatically updating arrival ETA."""
        now = timezone.localtime()
        self.client.force_login(self.worker_user)

        job2 = Booking.objects.create(
            customer=self.user,
            worker=self.worker,
            service=self.service,
            date=now.date(),
            time="2:30 PM - 3:00 PM",
            start_time=time(14, 30),
            end_time=time(15, 0),
            status='PENDING'
        )

        resp = self.client.post(f'/worker/jobs/{job2.id}/confirm-transit/', {
            'transit_choice': 'need_buffer',
            'buffer_minutes': '15',
        })
        self.assertEqual(resp.status_code, 302)

        job2.refresh_from_db()
        self.assertIn('2:45 PM', job2.proposed_time)
        self.assertIn('15-min travel buffer', job2.reschedule_note)

    def test_customer_request_reschedule_flow(self):
        """Customer can reschedule their delayed booking directly."""
        now = timezone.localtime()
        self.client.force_login(self.user)

        job = Booking.objects.create(
            customer=self.user,
            worker=self.worker,
            service=self.service,
            date=now.date(),
            time="2:30 PM - 3:00 PM",
            start_time=time(14, 30),
            end_time=time(15, 0),
            status='ACCEPTED'
        )

        resp = self.client.post(f'/booking/{job.id}/request-reschedule/', {
            'date': str(now.date()),
            'time': '3:30 PM - 4:00 PM',
            'reason': 'Professional running late on prior job',
        })
        self.assertEqual(resp.status_code, 302)

        job.refresh_from_db()
        self.assertEqual(job.time, '3:30 PM - 4:00 PM')
        self.assertEqual(job.start_time, time(15, 30))
        self.assertEqual(job.end_time, time(16, 0))

    def test_tiered_acceptance_expiry_same_day_and_future_day(self):
        """Tiered response window: same-day expires at 20m, future-day expires at 60m."""
        from lamoapp.views import check_and_expire_pending_bookings
        now = timezone.localtime()

        # Same-day booking created 19 minutes ago -> should NOT expire
        b_same_day_active = Booking.objects.create(
            customer=self.user,
            worker=self.worker,
            service=self.service,
            date=now.date(),
            time="3:00 PM - 3:30 PM",
            status='PENDING'
        )
        Booking.objects.filter(id=b_same_day_active.id).update(
            created_at=now - timedelta(minutes=19)
        )

        # Same-day booking created 21 minutes ago -> SHOULD expire
        b_same_day_expired = Booking.objects.create(
            customer=self.user,
            worker=self.worker,
            service=self.service,
            date=now.date(),
            time="4:00 PM - 4:30 PM",
            status='PENDING'
        )
        Booking.objects.filter(id=b_same_day_expired.id).update(
            created_at=now - timedelta(minutes=21)
        )

        # Future-day booking created 55 minutes ago -> should NOT expire
        b_future_active = Booking.objects.create(
            customer=self.user,
            worker=self.worker,
            service=self.service,
            date=now.date() + timedelta(days=2),
            time="11:00 AM - 11:30 AM",
            status='PENDING'
        )
        # Avoid night-time pause during test by pinning created_at to 2 PM
        Booking.objects.filter(id=b_future_active.id).update(
            created_at=now - timedelta(minutes=55)
        )

        # Future-day booking created 65 minutes ago -> SHOULD expire
        b_future_expired = Booking.objects.create(
            customer=self.user,
            worker=self.worker,
            service=self.service,
            date=now.date() + timedelta(days=2),
            time="2:00 PM - 2:30 PM",
            status='PENDING'
        )
        Booking.objects.filter(id=b_future_expired.id).update(
            created_at=now - timedelta(minutes=65)
        )

        check_and_expire_pending_bookings()

        b_same_day_active.refresh_from_db()
        self.assertEqual(b_same_day_active.status, 'PENDING')

        b_same_day_expired.refresh_from_db()
        self.assertEqual(b_same_day_expired.status, 'EXPIRED')
        self.assertIn('20-minute same-day', b_same_day_expired.cancellation_reason)

        b_future_active.refresh_from_db()
        self.assertEqual(b_future_active.status, 'PENDING')

        b_future_expired.refresh_from_db()
        # If running during night-time hours (9 PM - 8 AM), night pause intentionally applies
        now_local = timezone.localtime()
        if now_local.hour >= 21 or now_local.hour < 8:
            self.assertEqual(b_future_expired.status, 'PENDING')
        else:
            self.assertEqual(b_future_expired.status, 'EXPIRED')
            self.assertIn('60-minute scheduled', b_future_expired.cancellation_reason)

    def test_customer_reassign_expired_booking(self):
        """Customer can 1-tap reassign an expired booking to the next best pro."""
        # Create a second verified worker in the same district and service
        worker_user2 = CustomUser.objects.create_user(
            username='secondworker',
            email='worker2@example.com',
            password='password123',
            role='WORKER'
        )
        worker2 = WorkerProfile.objects.create(
            user=worker_user2,
            service=self.service,
            district='Kollam',
            is_verified=True,
            work_status='AVAILABLE',
            experience_years=5,
        )
        # A reassigned booking must go to a worker who could accept it normally,
        # which includes having enough prepaid wallet balance for the commission.
        WorkerWallet.objects.create(worker=worker2, balance=Decimal('1000.00'))

        expired_booking = Booking.objects.create(
            customer=self.user,
            worker=self.worker,
            service=self.service,
            district='Kollam',
            date=timezone.localdate() + timedelta(days=1),
            time="2:00 PM - 2:30 PM",
            start_time=time(14, 0),
            end_time=time(14, 30),
            status='EXPIRED',
            cancellation_reason='Worker did not respond within the 30-minute response window.'
        )

        self.client.force_login(self.user)
        resp = self.client.post(f'/booking/{expired_booking.id}/reassign/')
        self.assertEqual(resp.status_code, 302)

        expired_booking.refresh_from_db()
        self.assertEqual(expired_booking.status, 'PENDING')
        self.assertEqual(expired_booking.worker, worker2)
        self.assertIn('Auto-reassigned', expired_booking.cancellation_reason)

