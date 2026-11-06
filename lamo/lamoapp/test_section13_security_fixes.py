import secrets
from decimal import Decimal
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

from django.test import TestCase, Client
from django.utils import timezone
from django.contrib.sessions.models import Session
from django.urls import reverse

from lamoapp.models import (
    CustomUser, UserProfile, WorkerProfile, Service,
    Booking, BookingSeries, Payment, Review, PushSubscription
)
from lamoapp.views import (
    _anonymize_and_soft_delete_customer,
    _cancel_single_booking_internal,
    get_booking_upi_payment_data
)


class Section13SecurityFixesTests(TestCase):
    """
    Test suite for Section 13 Security Fixes (SEC-01 and SEC-02).
    """

    def setUp(self):
        # Create Customer
        self.customer = CustomUser.objects.create_user(
            username='test_sec13_cust',
            email='cust_sec13@example.com',
            password='TestPassword123!',
            role='USER'
        )
        self.cust_profile = UserProfile.objects.create(
            user=self.customer,
            phone='9876543210',
            address='123 Test Street, Kochi',
            latitude=Decimal('9.9312'),
            longitude=Decimal('76.2673')
        )

        # Create Worker
        self.worker_user = CustomUser.objects.create_user(
            username='test_sec13_worker',
            email='worker_sec13@example.com',
            password='TestPassword123!',
            role='WORKER'
        )
        self.service = Service.objects.create(
            name='Plumbing SEC13',
            price=Decimal('250.00'),
            description='Test service description'
        )
        self.worker_profile = WorkerProfile.objects.create(
            user=self.worker_user,
            service=self.service,
            phone='9123456780',
            district='Ernakulam',
            is_verified=True,
            work_status='AVAILABLE'
        )

        # Create Admin
        self.admin_user = CustomUser.objects.create_user(
            username='test_sec13_admin',
            email='admin_sec13@example.com',
            password='TestPassword123!',
            role='ADMIN'
        )

    # -------------------------------------------------------------
    # SEC-01: Customer Account Deletion & Anonymization
    # -------------------------------------------------------------

    def test_customer_soft_delete_preserves_completed_booking_and_payment(self):
        """
        Completed bookings, payments, and reviews must remain in the DB with customer intact.
        CustomUser must be anonymized and deactivated. UserProfile PII cleared.
        """
        # Create completed booking
        booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker_profile,
            service=self.service,
            status='COMPLETED',
            date=timezone.now().date() - timedelta(days=2),
            time='10:00 AM',
            location='123 Test Street',
            final_amount=Decimal('300.00')
        )
        payment = Payment.objects.create(
            booking=booking,
            amount=Decimal('300.00'),
            status='PAID'
        )
        review = Review.objects.create(
            booking=booking,
            customer=self.customer,
            worker=self.worker_profile,
            rating=5,
            comment='Excellent service!'
        )

        # Create push subscription
        PushSubscription.objects.create(
            user=self.customer,
            endpoint='https://fcm.googleapis.com/fcm/send/sec13_test',
            p256dh='sec13_p256dh',
            auth='sec13_auth'
        )

        success, err = _anonymize_and_soft_delete_customer(self.customer, actor='CUSTOMER')
        self.assertTrue(success)
        self.assertIsNone(err)

        # Refresh instances
        self.customer.refresh_from_db()
        self.cust_profile.refresh_from_db()
        booking.refresh_from_db()
        payment.refresh_from_db()
        review.refresh_from_db()

        # Check Booking, Payment, Review preserved
        self.assertEqual(booking.customer_id, self.customer.id)
        self.assertEqual(booking.payment.id, payment.id)
        self.assertEqual(review.customer_id, self.customer.id)

        # Check User anonymized
        self.assertFalse(self.customer.is_active)
        self.assertFalse(self.customer.has_usable_password())
        self.assertTrue(self.customer.username.startswith(f"deleted_user_{self.customer.id}_"))
        self.assertTrue(self.customer.email.startswith(f"deleted_{self.customer.id}_"))
        self.assertTrue(self.customer.email.endswith("@anonymized.local"))
        self.assertEqual(self.customer.first_name, "Deleted")
        self.assertEqual(self.customer.last_name, "User")

        # Check UserProfile PII scrubbed
        self.assertEqual(self.cust_profile.phone, '')
        self.assertEqual(self.cust_profile.address, '')
        self.assertIsNone(self.cust_profile.latitude)
        self.assertIsNone(self.cust_profile.longitude)

        # Check push subscriptions purged
        self.assertFalse(PushSubscription.objects.filter(user=self.customer).exists())

    def test_customer_deletion_blocked_by_active_or_disputed_booking(self):
        """Active statuses IN_PROGRESS, COMPLETION_REQUESTED, DISPUTED must block deletion."""
        for blocking_status in ['IN_PROGRESS', 'COMPLETION_REQUESTED', 'DISPUTED']:
            booking = Booking.objects.create(
                customer=self.customer,
                worker=self.worker_profile,
                service=self.service,
                status=blocking_status,
                date=timezone.now().date(),
                time='10:00 AM'
            )
            success, err = _anonymize_and_soft_delete_customer(self.customer, actor='CUSTOMER')
            self.assertFalse(success)
            self.assertIn("in-progress, completion-requested, or disputed", err)

            # Cleanup
            booking.delete()

    def test_customer_deletion_blocked_by_standalone_accepted_booking(self):
        """Standalone ACCEPTED booking must block account deletion."""
        booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker_profile,
            service=self.service,
            status='ACCEPTED',
            date=timezone.now().date() + timedelta(days=1),
            time='10:00 AM'
        )
        success, err = _anonymize_and_soft_delete_customer(self.customer, actor='CUSTOMER')
        self.assertFalse(success)
        self.assertIn("confirmed upcoming bookings", err)

    def test_customer_deletion_cancels_standalone_pending_booking(self):
        """Standalone PENDING booking is cancelled via cancellation pipeline."""
        booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker_profile,
            service=self.service,
            status='PENDING',
            date=timezone.now().date() + timedelta(days=1),
            time='10:00 AM'
        )

        success, err = _anonymize_and_soft_delete_customer(self.customer, actor='CUSTOMER')
        self.assertTrue(success)

        booking.refresh_from_db()
        self.assertEqual(booking.status, 'CANCELLED')
        self.assertEqual(booking.cancelled_by, 'CUSTOMER')
        self.assertEqual(booking.cancellation_reason, 'Customer account closed')

    def test_recurring_series_deactivation_and_future_child_cancellation(self):
        """
        Recurring series is marked is_active=False.
        Future child bookings (PENDING, ACCEPTED) are safely cancelled.
        """
        today = timezone.now().date()
        series = BookingSeries.objects.create(
            customer=self.customer,
            worker=self.worker_profile,
            service=self.service,
            frequency='WEEKLY',
            start_date=today,
            end_date=today + timedelta(days=30),
            is_active=True
        )
        child_accepted = Booking.objects.create(
            customer=self.customer,
            worker=self.worker_profile,
            service=self.service,
            series=series,
            status='ACCEPTED',
            date=today + timedelta(days=7),
            time='10:00 AM'
        )
        child_pending = Booking.objects.create(
            customer=self.customer,
            worker=self.worker_profile,
            service=self.service,
            series=series,
            status='PENDING',
            date=today + timedelta(days=14),
            time='10:00 AM'
        )

        success, err = _anonymize_and_soft_delete_customer(self.customer, actor='CUSTOMER')
        self.assertTrue(success)

        series.refresh_from_db()
        child_accepted.refresh_from_db()
        child_pending.refresh_from_db()

        self.assertFalse(series.is_active)
        self.assertEqual(child_accepted.status, 'CANCELLED')
        self.assertEqual(child_accepted.cancelled_by, 'CUSTOMER')
        self.assertEqual(child_pending.status, 'CANCELLED')
        self.assertEqual(child_pending.cancelled_by, 'CUSTOMER')

    def test_worker_cannot_accept_booking_of_deactivated_customer(self):
        """worker_accept_booking returns error if customer is_active=False."""
        client = Client()
        client.force_login(self.worker_user)

        # Deactivate customer
        self.customer.is_active = False
        self.customer.save(update_fields=['is_active'])

        booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker_profile,
            service=self.service,
            status='PENDING',
            date=timezone.now().date() + timedelta(days=1),
            time='10:00 AM'
        )

        accept_url = reverse('worker_accept_booking', kwargs={'booking_id': booking.id})
        response = client.post(accept_url)
        self.assertEqual(response.status_code, 302)

        booking.refresh_from_db()
        self.assertEqual(booking.status, 'PENDING')  # Must NOT be changed to ACCEPTED

    def test_inactive_customer_cannot_authenticate(self):
        """Deactivated customer cannot login with password."""
        client = Client()

        # Save username before anonymizing
        orig_username = self.customer.username

        # Anonymize/deactivate customer
        _anonymize_and_soft_delete_customer(self.customer, actor='CUSTOMER')

        # Password login attempt with original credentials
        resp = client.post(reverse('login'), {
            'action': 'login_password',
            'identifier': orig_username,
            'password': 'TestPassword123!'
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data['success'])

    def test_admin_delete_user_performs_anonymization(self):
        """Admin deleting a customer invokes anonymization with actor='ADMIN'."""
        client = Client()
        client.force_login(self.admin_user)

        del_url = reverse('admin_delete_user', kwargs={'user_id': self.customer.id})
        response = client.post(del_url)
        self.assertEqual(response.status_code, 302)

        self.customer.refresh_from_db()
        self.assertFalse(self.customer.is_active)
        self.assertTrue(self.customer.username.startswith(f"deleted_user_{self.customer.id}_"))

    # -------------------------------------------------------------
    # SEC-02: UPI QR Code Unverified Phone Fallback Removal
    # -------------------------------------------------------------

    def test_upi_payment_data_returns_none_when_worker_upi_not_configured(self):
        """
        When worker.upi_id is empty, get_booking_upi_payment_data MUST return None,
        even if worker.phone is a valid 10-digit number.
        """
        self.worker_profile.upi_id = ''
        self.worker_profile.phone = '9876543210'
        self.worker_profile.save()

        booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker_profile,
            service=self.service,
            status='ACCEPTED',
            final_amount=Decimal('500.00')
        )

        data = get_booking_upi_payment_data(booking)
        self.assertIsNone(data)

    def test_upi_payment_data_returns_valid_data_when_upi_id_configured(self):
        """
        When worker.upi_id is explicitly configured, get_booking_upi_payment_data
        returns valid qr_b64 and upi_intent_url.
        """
        self.worker_profile.upi_id = 'verifiedworker@okhdfcbank'
        self.worker_profile.save()

        booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker_profile,
            service=self.service,
            status='ACCEPTED',
            final_amount=Decimal('500.00')
        )

        data = get_booking_upi_payment_data(booking)
        self.assertIsNotNone(data)
        self.assertEqual(data['worker_upi'], 'verifiedworker@okhdfcbank')
        self.assertEqual(data['amount'], Decimal('500.00'))
        self.assertIn('upi://pay?', data['upi_intent_url'])
        self.assertIn('verifiedworker@okhdfcbank', data['upi_intent_url'])
        self.assertTrue(len(data['qr_b64']) > 50)
