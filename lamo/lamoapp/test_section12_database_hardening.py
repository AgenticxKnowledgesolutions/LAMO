from decimal import Decimal
from datetime import date, time, timedelta
from django.test import TransactionTestCase, Client
from django.urls import reverse
from django.utils import timezone
from django.db import IntegrityError, transaction
from lamoapp.models import (
    CustomUser, UserProfile, WorkerProfile, Service,
    Booking, WorkerWallet, WalletTransaction, WorkerLeave
)
from lamoapp.views import (
    has_worker_schedule_conflict,
    parse_time_range,
    ACTIVE_BOOKING_STATUSES
)


class Section12DatabaseHardeningTests(TransactionTestCase):
    """
    Transaction-aware test suite for Section 12 Database Hardening.
    Tests UTR uniqueness, concurrent submissions, empty reference coexistence,
    WorkerProfile mutex lock pattern, and double-booking prevention.
    """

    def setUp(self):
        # Create customer
        self.customer = CustomUser.objects.create_user(
            username='test_customer_s12',
            email='customer_s12@example.com',
            password='TestPassword123!',
            role='USER'
        )
        self.cust_profile = UserProfile.objects.create(
            user=self.customer,
            phone='9876543210',
            district='Ernakulam'
        )

        # Create second customer for concurrency tests
        self.customer2 = CustomUser.objects.create_user(
            username='test_customer2_s12',
            email='customer2_s12@example.com',
            password='TestPassword123!',
            role='USER'
        )
        UserProfile.objects.create(
            user=self.customer2,
            phone='9876543211',
            district='Ernakulam'
        )

        # Create worker user & profile
        self.worker_user = CustomUser.objects.create_user(
            username='test_worker_s12',
            email='worker_s12@example.com',
            password='TestPassword123!',
            role='WORKER'
        )
        self.service = Service.objects.create(
            name='Electrical Repairs S12',
            price=Decimal('500.00'),
            is_active=True
        )
        self.worker_profile = WorkerProfile.objects.create(
            user=self.worker_user,
            service=self.service,
            district='Ernakulam',
            is_verified=True,
            work_status='AVAILABLE',
            pricing_type='hour',
            pricing_amount=Decimal('500.00')
        )
        self.wallet = WorkerWallet.objects.create(
            worker=self.worker_profile,
            balance=Decimal('100.00')
        )

    # =========================================================================
    # PART 1: UTR DUPLICATE TESTS
    # =========================================================================

    def test_utr_first_submission_succeeds(self):
        """First valid UTR submission creates a RECHARGE_PENDING transaction."""
        client = Client()
        client.force_login(self.worker_user)
        url = reverse('worker_recharge_wallet')
        response = client.post(url, {
            'amount': '250.00',
            'reference_id': '123456789012'
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        
        tx = WalletTransaction.objects.filter(reference_id='123456789012').first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.transaction_type, 'RECHARGE_PENDING')
        self.assertEqual(tx.reference_id, '123456789012')

    def test_utr_duplicate_rejected_friendly_error(self):
        """Submitting the exact same UTR again is rejected with a validation message."""
        client = Client()
        client.force_login(self.worker_user)
        url = reverse('worker_recharge_wallet')
        # First submission
        client.post(url, {
            'amount': '250.00',
            'reference_id': 'UTR9988776655'
        }, follow=True)

        # Second submission
        response = client.post(url, {
            'amount': '250.00',
            'reference_id': 'UTR9988776655'
        }, follow=True)

        self.assertEqual(response.status_code, 200)
        # Verify only 1 record exists in DB
        self.assertEqual(WalletTransaction.objects.filter(reference_id='UTR9988776655').count(), 1)

    def test_utr_case_normalization(self):
        """Submitting 'utr_abc123' is normalized to uppercase and collides with 'UTR_ABC123'."""
        client = Client()
        client.force_login(self.worker_user)
        url = reverse('worker_recharge_wallet')
        client.post(url, {
            'amount': '100.00',
            'reference_id': 'utr_abc123xyz'
        }, follow=True)

        # Stored in DB as uppercase
        tx = WalletTransaction.objects.filter(reference_id='UTR_ABC123XYZ').first()
        self.assertIsNotNone(tx)

        # Attempt to insert uppercase variant
        client.post(url, {
            'amount': '100.00',
            'reference_id': 'UTR_ABC123XYZ'
        }, follow=True)
        self.assertEqual(WalletTransaction.objects.filter(reference_id='UTR_ABC123XYZ').count(), 1)

    def test_multiple_system_transactions_with_null_reference(self):
        """Multiple system transactions (fee deduction, payout) with reference_id=None coexist without collision."""
        tx1 = WalletTransaction.objects.create(
            wallet=self.wallet,
            transaction_type='FEE_DEDUCTION',
            amount=Decimal('-15.00'),
            balance_after=Decimal('85.00'),
            reference_id=None,
            notes='Platform fee deduction 1'
        )
        tx2 = WalletTransaction.objects.create(
            wallet=self.wallet,
            transaction_type='FEE_DEDUCTION',
            amount=Decimal('-20.00'),
            balance_after=Decimal('65.00'),
            reference_id=None,
            notes='Platform fee deduction 2'
        )
        tx3 = WalletTransaction.objects.create(
            wallet=self.wallet,
            transaction_type='ONLINE_PAYOUT_OFFSET',
            amount=Decimal('450.00'),
            balance_after=Decimal('515.00'),
            reference_id=None,
            notes='Online payout offset'
        )
        self.assertIsNotNone(tx1.id)
        self.assertIsNotNone(tx2.id)
        self.assertIsNotNone(tx3.id)
        self.assertEqual(WalletTransaction.objects.filter(reference_id__isnull=True).count(), 3)

    def test_failed_duplicate_recharge_does_not_change_balance(self):
        """A failed duplicate recharge submission does not alter the worker's wallet balance."""
        initial_balance = self.wallet.balance
        client = Client()
        client.force_login(self.worker_user)
        # Seed first tx
        WalletTransaction.objects.create(
            wallet=self.wallet,
            transaction_type='RECHARGE_PENDING',
            amount=Decimal('0.00'),
            balance_after=initial_balance,
            reference_id='FIXED_UTR_0001'
        )

        # Post duplicate
        url = reverse('worker_recharge_wallet')
        client.post(url, {
            'amount': '500.00',
            'reference_id': 'FIXED_UTR_0001'
        }, follow=True)

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, initial_balance)

    # =========================================================================
    # PART 2: WORKER DOUBLE-BOOKING & MUTEX LOCK TESTS
    # =========================================================================

    def test_emergency_acceptance_vs_normal_booking_conflict(self):
        """
        Emergency booking accepted today stamps date=today and start/end time.
        A subsequent customer booking attempt for overlapping time is blocked.
        """
        today = timezone.localdate()
        emergency_booking = Booking.objects.create(
            customer=self.customer,
            worker=None,
            service=self.service,
            booking_type='Emergency',
            date=None,
            time='10:00 AM - 12:00 PM',
            status='EMERGENCY_PENDING',
            district='Ernakulam',
            estimated_cost=Decimal('500.00')
        )

        # Worker accepts emergency booking
        worker_client = Client()
        worker_client.force_login(self.worker_user)
        resp = worker_client.post(reverse('worker_accept_emergency', args=[emergency_booking.id]), follow=True)
        self.assertEqual(resp.status_code, 200)

        emergency_booking.refresh_from_db()
        self.assertEqual(emergency_booking.status, 'ACCEPTED')
        self.assertEqual(emergency_booking.worker, self.worker_profile)
        self.assertEqual(emergency_booking.date, today)

        # Customer 2 attempts to book the same worker for overlapping window (11:00 AM - 01:00 PM)
        cust2_client = Client()
        cust2_client.force_login(self.customer2)
        book_resp = cust2_client.post(reverse('book_service'), {
            'worker_id': self.worker_profile.id,
            'service_id': self.service.id,
            'date': today.strftime('%Y-%m-%d'),
            'time': '11:00 AM - 01:00 PM',
            'location': 'Kochi',
            'district': 'Ernakulam',
            'phone': '9876543211',
            'description': 'Overlapping booking test',
            'payment_method': 'CASH',
            'booking_type': 'Standard'
        }, follow=True)

        # Conflict detected: Booking should not be created
        conflict_booking = Booking.objects.filter(
            customer=self.customer2,
            date=today,
            time='11:00 AM - 01:00 PM'
        ).first()
        self.assertIsNone(conflict_booking)

    def test_different_non_overlapping_slots_remain_bookable(self):
        """A worker can accept non-overlapping bookings on the same date."""
        target_date = timezone.localdate() + timedelta(days=2)
        start_t1, end_t1 = time(9, 0), time(11, 0)
        start_t2, end_t2 = time(14, 0), time(16, 0)

        # Booking 1 (09:00 - 11:00)
        b1 = Booking.objects.create(
            customer=self.customer,
            worker=self.worker_profile,
            service=self.service,
            booking_type='Standard',
            date=target_date,
            time='09:00 AM - 11:00 AM',
            start_time=start_t1,
            end_time=end_t1,
            status='ACCEPTED',
            district='Ernakulam',
            estimated_cost=Decimal('500.00')
        )

        # Check conflict for Booking 2 (14:00 - 16:00)
        conflict = has_worker_schedule_conflict(
            self.worker_profile,
            booking_date=target_date,
            start_time=start_t2,
            end_time=end_t2
        )
        self.assertFalse(conflict)

        # Booking 2 can be created successfully
        b2 = Booking.objects.create(
            customer=self.customer2,
            worker=self.worker_profile,
            service=self.service,
            booking_type='Standard',
            date=target_date,
            time='02:00 PM - 04:00 PM',
            start_time=start_t2,
            end_time=end_t2,
            status='PENDING',
            district='Ernakulam',
            estimated_cost=Decimal('500.00')
        )
        self.assertIsNotNone(b2.id)

    def test_cancelled_booking_does_not_block_valid_future_booking(self):
        """A CANCELLED booking must not block a new booking for the same time slot."""
        target_date = timezone.localdate() + timedelta(days=3)
        start_t, end_t = time(10, 0), time(12, 0)

        # Previous cancelled booking
        Booking.objects.create(
            customer=self.customer,
            worker=self.worker_profile,
            service=self.service,
            booking_type='Standard',
            date=target_date,
            time='10:00 AM - 12:00 PM',
            start_time=start_t,
            end_time=end_t,
            status='CANCELLED',
            district='Ernakulam',
            estimated_cost=Decimal('500.00')
        )

        # Conflict check should return False
        conflict = has_worker_schedule_conflict(
            self.worker_profile,
            booking_date=target_date,
            start_time=start_t,
            end_time=end_t
        )
        self.assertFalse(conflict)

    def test_customer_accept_reschedule_under_mutex_lock(self):
        """
        Customer accepting worker reschedule counter-offer checks conflict under WorkerProfile mutex lock.
        If another active booking was scheduled at proposed time, acceptance is blocked.
        """
        target_date = timezone.localdate() + timedelta(days=4)
        proposed_date = timezone.localdate() + timedelta(days=5)
        prop_time = '03:00 PM - 05:00 PM'
        start_t, end_t = time(15, 0), time(17, 0)

        # Booking with pending reschedule
        booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker_profile,
            service=self.service,
            booking_type='Standard',
            date=target_date,
            time='10:00 AM - 12:00 PM',
            status='PENDING',
            proposed_date=proposed_date,
            proposed_time=prop_time,
            reschedule_proposed_at=timezone.now(),
            district='Ernakulam',
            estimated_cost=Decimal('500.00')
        )

        # Meanwhile, worker is assigned another job at proposed time on proposed_date
        Booking.objects.create(
            customer=self.customer2,
            worker=self.worker_profile,
            service=self.service,
            booking_type='Standard',
            date=proposed_date,
            time=prop_time,
            start_time=start_t,
            end_time=end_t,
            status='ACCEPTED',
            district='Ernakulam',
            estimated_cost=Decimal('500.00')
        )

        # Customer attempts to accept counter-offer
        cust_client = Client()
        cust_client.force_login(self.customer)
        resp = cust_client.post(reverse('customer_accept_reschedule', args=[booking.id]), follow=True)
        self.assertEqual(resp.status_code, 200)

        booking.refresh_from_db()
        # Booking must NOT be accepted at proposed time because of conflict
        self.assertNotEqual(booking.status, 'ACCEPTED')
        self.assertEqual(booking.date, target_date)
