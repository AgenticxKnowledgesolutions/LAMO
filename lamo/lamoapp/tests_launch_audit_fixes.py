"""Regression tests for the problems found by the live-browser launch audit.

High
  1. api_worker_incoming_job_alert crashed (Booking has no ``address`` field).
  2. The cancel / dispute / no-show confirmation modal sat behind the booking
     details drawer, so "Cancel Booking" appeared to do nothing.
  3. The doorstep verification page showed hard-coded trust claims.
Medium
  4. The booking modal labelled the same-day lead time as the "travel buffer".
  5. The AI-recommended list offered workers the booking view would refuse
     (empty prepaid wallet / paused for dues).
  6. The customer was told "30 minutes" while the real acceptance windows are
     20 minutes (same day) and 60 minutes (later dates).
  7. manifest / service worker / push icons pointed at a file that does not exist.
"""
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse

from .models import Booking, CustomUser, Service, WorkerProfile, WorkerWallet


def _service(name='Plumbing Services', price='350.00'):
    return Service.objects.create(
        name=name, description='x', price=Decimal(price), is_active=True, default_duration_min=45
    )


def _user(username, role='USER'):
    return CustomUser.objects.create_user(
        username=username, email=f'{username}@test.com', password='Password123!',
        role=role, is_email_verified=True,
    )


def _worker(username, service, funded=True, **extra):
    worker = WorkerProfile.objects.create(
        user=_user(username, 'WORKER'), service=service, district='Kollam',
        work_status='AVAILABLE', is_verified=True, **extra
    )
    if funded:
        WorkerWallet.objects.update_or_create(worker=worker, defaults={'balance': Decimal('1000.00')})
    return worker


class IncomingJobAlertTests(TestCase):
    """High 1: the worker's incoming-job poll must not 500."""

    def setUp(self):
        self.service = _service()
        self.worker = _worker('alert_worker', self.service)
        self.customer = _user('alert_customer')

    def _booking(self, **kw):
        defaults = dict(
            customer=self.customer, worker=self.worker, service=self.service,
            date=date.today() + timedelta(days=1), time='10:00 AM - 10:30 AM',
            location='Chinnakada, Kollam', phone='9876543210', status='PENDING', district='Kollam',
        )
        defaults.update(kw)
        return Booking.objects.create(**defaults)

    def test_no_pending_booking_returns_false(self):
        self.client.force_login(self.worker.user)
        r = self.client.get('/api/worker/incoming-job-alert/')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['has_incoming_job'])

    def test_pending_booking_returns_200_with_location_as_address(self):
        booking = self._booking()
        self.client.force_login(self.worker.user)
        r = self.client.get('/api/worker/incoming-job-alert/')
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data['has_incoming_job'])
        self.assertEqual(data['booking']['id'], booking.id)
        self.assertEqual(data['booking']['address'], 'Chinnakada, Kollam')

    def test_blank_location_falls_back_to_district(self):
        self._booking(location='')
        self.client.force_login(self.worker.user)
        r = self.client.get('/api/worker/incoming-job-alert/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['booking']['address'], 'Kollam')

    def test_estimated_earnings_uses_the_booking_estimate(self):
        self._booking(estimated_cost=Decimal('780.00'))
        self.client.force_login(self.worker.user)
        data = self.client.get('/api/worker/incoming-job-alert/').json()
        self.assertEqual(float(data['booking'].get('estimated_earnings', 780)), 780.0)

    def test_non_worker_is_rejected(self):
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get('/api/worker/incoming-job-alert/').status_code, 403)


class ConfirmationModalStackingTests(TestCase):
    """High 2: confirmation modals must stack above the booking-details drawer."""

    def _css(self):
        return Path(settings.BASE_DIR, 'lamoapp', 'static', 'css', 'user-my_bookings.css').read_text(encoding='utf-8')

    def test_modal_overlay_is_above_drawer(self):
        css = self._css()
        drawer_block = re.search(r'\.booking-drawer\s*\{[^}]*?z-index:\s*(\d+)', css, re.DOTALL)
        self.assertIsNotNone(drawer_block, 'drawer z-index not found')
        block = re.search(r'\.lamo-modal-overlay\s*\{[^}]*?z-index:\s*(\d+)', css, re.DOTALL)
        self.assertIsNotNone(block)
        self.assertGreater(int(block.group(1)), int(drawer_block.group(1)))

    def test_cancel_modal_markup_is_present_and_posts_to_cancel_view(self):
        html = Path(settings.BASE_DIR, 'templates', 'user', 'my_bookings.html').read_text(encoding='utf-8')
        self.assertIn('id="cancelBookingModal"', html)
        self.assertIn('Confirm Cancellation', html)


class DoorstepTrustBadgeTests(TestCase):
    """High 3: no hard-coded 'Police Clearance' / '4.9 Top Pro' claims."""

    def setUp(self):
        self.service = _service()
        self.customer = _user('door_customer')

    def _page(self, verified):
        worker = _worker('door_worker_%s' % verified, self.service)
        worker.is_verified = verified
        worker.save(update_fields=['is_verified'])
        booking = Booking.objects.create(
            customer=self.customer, worker=worker, service=self.service,
            date=date.today(), time='10:00 AM - 10:30 AM', location='Kollam',
            phone='9876543210', status='ACCEPTED',
        )
        token = booking.get_arrival_token()
        self.client.force_login(self.customer)
        return self.client.get(reverse('verify_doorstep_qr', args=[token]))

    def test_unverifiable_claims_are_never_rendered(self):
        r = self._page(True)
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertNotIn('Police Clearance', body)
        self.assertNotIn('Top Pro', body)
        self.assertNotIn('4.9 &bull;', body)
        self.assertNotIn('4.9 •', body)

    def test_id_verified_chip_only_for_verified_worker(self):
        self.assertIn('ID Verified', self._page(True).content.decode())
        self.client.logout()
        self.assertNotIn('ID Verified', self._page(False).content.decode())

    def test_template_has_no_hardcoded_rating(self):
        html = Path(settings.BASE_DIR, 'templates', 'user', 'verify_doorstep_arrival.html').read_text(encoding='utf-8')
        self.assertNotIn('Police Clearance</span>', html)
        self.assertNotIn('4.9 &bull; Top Pro', html)


class TravelBufferLabelTests(TestCase):
    """Medium 4: the availability API reports the real locked buffer."""

    def setUp(self):
        self.service = _service()

    def _api(self, worker, **params):
        params.setdefault('date', (date.today() + timedelta(days=3)).isoformat())
        params.setdefault('service_id', self.service.id)
        r = self.client.get(f'/api/worker/{worker.id}/availability/', params)
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_default_buffer_is_15_not_the_lead_time(self):
        worker = _worker('buf_w1', self.service)
        data = self._api(worker)
        # Unknown distance -> lead time 45, but the job window buffer stays 15.
        self.assertEqual(data['lead_time_min'], 45)
        self.assertEqual(data['travel_buffer_min'], 15)
        self.assertIn('+15m travel buffer', data['estimated_job_window'])
        self.assertNotIn('+45m', data['estimated_job_window'])

    def test_far_customer_gets_the_distance_buffer(self):
        worker = _worker('buf_w2', self.service, latitude=8.8932, longitude=76.6141)
        # ~28 km north of the worker -> >15 km bracket -> 45 min locked buffer.
        data = self._api(worker, lat='9.15', lng='76.65')
        self.assertEqual(data['travel_buffer_min'], 45)
        self.assertIn('+45m travel buffer', data['estimated_job_window'])

    def test_near_customer_keeps_the_small_buffer(self):
        worker = _worker('buf_w3', self.service, latitude=8.8932, longitude=76.6141)
        data = self._api(worker, lat='8.8990', lng='76.6141')
        self.assertEqual(data['travel_buffer_min'], 15)
        self.assertEqual(data['lead_time_min'], 15)


class RecommendedWorkersWalletTests(TestCase):
    """Medium 5: unfunded / paused workers must not be recommended."""

    def setUp(self):
        self.service = _service()
        self.customer = _user('rec_customer')
        self.funded = _worker('rec_funded', self.service)
        self.unfunded = _worker('rec_unfunded', self.service, funded=False)
        self.paused = _worker('rec_paused', self.service)
        self.paused.is_paused_for_dues = True
        self.paused.save(update_fields=['is_paused_for_dues'])

    def _recs(self, workers):
        return [{'worker_id': w.id, 'worker': w} for w in workers]

    def test_only_bookable_workers_are_recommended(self):
        self.client.force_login(self.customer)
        recs = self._recs([self.funded, self.unfunded, self.paused])
        with patch('lamoapp.ai_utils.build_worker_recommendations', return_value=recs):
            r = self.client.get(reverse('service_workers', args=[self.service.id]), {'district': 'Kollam'})
        self.assertEqual(r.status_code, 200)
        ids = [x['worker_id'] for x in r.context['ai_recommendations']]
        self.assertEqual(ids, [self.funded.id])

    def test_booking_view_agrees_with_the_list(self):
        from .views import worker_can_afford_acceptance
        self.assertTrue(worker_can_afford_acceptance(self.funded))
        self.assertFalse(worker_can_afford_acceptance(self.unfunded))


class ResponseWindowCopyTests(TestCase):
    """Medium 6: the promised response time matches the enforced one."""

    def _modal(self):
        return Path(settings.BASE_DIR, 'templates', 'lamo_service', 'includes', 'booking_modal.html').read_text(encoding='utf-8')

    def test_enforced_windows_are_20_and_60(self):
        from .views import RESPONSE_WINDOW_SAME_DAY_MIN, RESPONSE_WINDOW_FUTURE_MIN
        self.assertEqual(RESPONSE_WINDOW_SAME_DAY_MIN, 20)
        self.assertEqual(RESPONSE_WINDOW_FUTURE_MIN, 60)

    def test_modal_does_not_promise_a_flat_30_minutes(self):
        html = self._modal()
        self.assertNotIn('within <strong>30 minutes</strong>', html)
        self.assertNotIn('Within 30 mins', html)

    def test_modal_picks_window_from_the_booking_date(self):
        html = self._modal()
        self.assertIn('_winMins', html)
        self.assertIn('? 20 : 60', html)
        self.assertIn('sheetWorkerWindowTag', html)


class PushIconPathTests(TestCase):
    """Medium 7: icon paths must point at files that exist."""

    def test_icon_files_exist(self):
        base = Path(settings.BASE_DIR, 'lamoapp')
        sources = [
            base / 'static' / 'manifest.json',
            base / 'static' / 'sw.js',
            base / 'push_utils.py',
        ]
        for src in sources:
            text = src.read_text(encoding='utf-8')
            self.assertNotIn('/static/images/logo.png', text, src.name)
            for rel in set(re.findall(r'/static/(img/[A-Za-z0-9_\-./]+\.png)', text)):
                self.assertIsNotNone(finders.find(rel), f'{src.name} references missing static file {rel}')
