from decimal import Decimal
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from lamoapp.models import CustomUser, WorkerProfile, Service, Booking, Complaint, WorkerWallet

class DisputeHardeningTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.customer = CustomUser.objects.create_user(
            username='cust_dispute_user',
            email='cust_dispute@example.com',
            password='Password123!',
            role='USER',
            is_email_verified=True
        )
        self.worker_user = CustomUser.objects.create_user(
            username='wkr_dispute_user',
            email='wkr_dispute@example.com',
            password='Password123!',
            role='WORKER',
            is_email_verified=True
        )
        self.admin_user = CustomUser.objects.create_user(
            username='admin_dispute_user',
            email='admin_dispute@example.com',
            password='Password123!',
            role='ADMIN',
            is_staff=True,
            is_email_verified=True
        )
        self.service = Service.objects.create(
            name='Cleaning',
            price=Decimal('500.00'),
            is_active=True
        )
        self.worker_profile = WorkerProfile.objects.create(
            user=self.worker_user,
            service=self.service,
            is_verified=True,
            work_status='AVAILABLE'
        )
        self.wallet, _ = WorkerWallet.objects.get_or_create(
            worker=self.worker_profile,
            defaults={'balance': Decimal('1000.00')}
        )

    def test_c2_dispute_booking_cannot_overwrite_completed_booking(self):
        """C2: dispute_booking takes a row lock and cannot overwrite a COMPLETED booking."""
        booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker_profile,
            service=self.service,
            date=timezone.localtime().date(),
            time="10:00 AM - 11:00 AM",
            status='COMPLETED',  # Already completed
            final_amount=Decimal('500.00')
        )

        self.client.login(username='cust_dispute_user', password='Password123!')
        dispute_url = reverse('dispute_booking', args=[booking.id])
        resp = self.client.post(dispute_url, {'dispute_reason': 'Late dispute attempt after payment'})
        
        # Must redirect with error, and status must remain COMPLETED
        self.assertEqual(resp.status_code, 302)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'COMPLETED')
        self.assertFalse(Complaint.objects.filter(booking=booking).exists())

    def test_c3_c4_c5_admin_dispute_resolve_atomic_and_explicit_action(self):
        """C3-C5: Admin complaint & booking update is atomic, respects dispute_action, and is idempotent."""
        booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker_profile,
            service=self.service,
            date=timezone.localtime().date(),
            time="11:00 AM - 12:00 PM",
            status='DISPUTED',
            dispute_reason='Work incomplete'
        )
        complaint = Complaint.objects.create(
            booking=booking,
            customer=self.customer,
            subject="Dispute test",
            description="Work incomplete",
            status='OPEN'
        )

        self.client.login(username='admin_dispute_user', password='Password123!')
        url = reverse('update_complaint_status', args=[complaint.id])

        # 1. Resolve as CANCEL using explicit dispute_action
        resp = self.client.post(url, {
            'status': 'RESOLVED',
            'admin_remark': 'Service was not provided properly',
            'dispute_action': 'CANCEL'
        })
        self.assertEqual(resp.status_code, 302)

        booking.refresh_from_db()
        complaint.refresh_from_db()
        self.assertEqual(booking.status, 'CANCELLED')
        self.assertEqual(booking.cancelled_by, 'ADMIN')
        self.assertEqual(complaint.status, 'RESOLVED')
        self.assertIn('CANCELLED', complaint.admin_remark)

        # 2. Idempotency test: calling resolve again on already CANCELLED booking is safe
        resp2 = self.client.post(url, {
            'status': 'RESOLVED',
            'admin_remark': 'Repeat resolution call',
            'dispute_action': 'CANCEL'
        })
        self.assertEqual(resp2.status_code, 302)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'CANCELLED')
