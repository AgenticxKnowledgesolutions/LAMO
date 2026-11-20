from decimal import Decimal
from datetime import timedelta
from .upload_test_fixtures import VALID_PDF
from django.utils import timezone
from django.test import TestCase, Client, RequestFactory
from django.urls import reverse
from django.contrib.auth.hashers import make_password
from lamoapp.models import CustomUser, UserProfile, WorkerProfile, WorkerWallet, Service, Booking, Notification, BookingSeries
from lamoapp.rate_limit import login_otp_ip_key, clear
import json

class AuditAuthFixesRegressionTest(TestCase):
    def setUp(self):
        self.client = Client()

    def test_change_1_otp_customer_delete_account(self):
        """Change 1: OTP customer (no usable password) can delete account with confirmation string."""
        user = CustomUser.objects.create(
            username='otp_delete_user',
            email='otp_delete@example.com',
            role='USER',
            is_email_verified=True
        )
        user.set_unusable_password()
        user.save()
        UserProfile.objects.create(user=user, phone='9876543210')

        self.client.force_login(user)

        # 1. Without confirmation string -> rejected (redirects back to edit_profile)
        res1 = self.client.post(reverse('delete_account'), {'confirm_delete': 'NO'})
        self.assertRedirects(res1, reverse('edit_profile'))
        user.refresh_from_db()
        self.assertTrue(user.is_active)

        # 2. With confirmation string 'DELETE' -> successfully processed (redirects to home)
        res2 = self.client.post(reverse('delete_account'), {'confirm_delete': 'DELETE'})
        self.assertRedirects(res2, reverse('home'))
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_change_2_change_password_restricted_to_admin(self):
        """Change 2: Only Admin role can access change_password view; workers and customers redirected."""
        customer = CustomUser.objects.create(
            username='customer_pw_user',
            email='cust_pw@example.com',
            role='USER',
            is_email_verified=True
        )
        self.client.force_login(customer)
        res_cust = self.client.get(reverse('change_password'))
        self.assertRedirects(res_cust, reverse('dashboard'), target_status_code=302)

        # Worker
        worker_user = CustomUser.objects.create(
            username='worker_pw_user',
            email='worker_pw@example.com',
            role='WORKER',
            is_email_verified=True
        )
        self.client.force_login(worker_user)
        res_worker = self.client.get(reverse('change_password'))
        self.assertRedirects(res_worker, reverse('dashboard'), target_status_code=302)

        # Admin
        admin_user = CustomUser.objects.create(
            username='admin_pw_user',
            email='admin_pw@example.com',
            role='ADMIN',
            is_superuser=True,
            is_email_verified=True
        )
        admin_user.set_password('AdminPass123!')
        admin_user.save()
        self.client.force_login(admin_user)
        res_admin = self.client.get(reverse('change_password'))
        self.assertEqual(res_admin.status_code, 200)

    def test_change_3_customer_reg_otp_cooldown(self):
        """Change 3: Cooldown blocks rapid OTP resend during customer registration."""
        res1 = self.client.post(
            reverse('register'),
            {'action': 'send_customer_reg_otp', 'name': 'Audit User', 'identifier': 'cooldowntest@example.com'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(res1.status_code, 200)
        d1 = res1.json()
        self.assertTrue(d1.get('success'))

        # Immediate second request should be blocked by cooldown
        res2 = self.client.post(
            reverse('register'),
            {'action': 'send_customer_reg_otp', 'name': 'Audit User', 'identifier': 'cooldowntest@example.com'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(res2.status_code, 200)
        d2 = res2.json()
        self.assertFalse(d2.get('success'))
        self.assertIn('cooldown_remaining', d2)
        self.assertGreater(d2['cooldown_remaining'], 0)

    def test_change_4_customer_reg_otp_attempt_limits(self):
        """Change 4: 5 failed OTP attempts invalidates registration session."""
        self.client.post(
            reverse('register'),
            {'action': 'send_customer_reg_otp', 'name': 'Audit Lockout', 'identifier': 'lockouttest@example.com'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )

        for attempt in range(1, 5):
            res = self.client.post(
                reverse('register'),
                {'action': 'verify_customer_reg_otp', 'otp': '000000'},
                HTTP_X_REQUESTED_WITH='XMLHttpRequest'
            )
            d = res.json()
            self.assertFalse(d.get('success'))
            self.assertIn(f'{5 - attempt} attempt(s) remaining', d.get('error', ''))

        # 5th attempt invalidates
        res5 = self.client.post(
            reverse('register'),
            {'action': 'verify_customer_reg_otp', 'otp': '000000'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        d5 = res5.json()
        self.assertFalse(d5.get('success'))
        self.assertIn('invalidated', d5.get('error', '').lower())

    def test_change_5_staff_user_not_promoted_to_admin(self):
        """Change 5: Non-superuser staff account is rejected on admin portal, cannot password login, and never promoted to ADMIN."""
        staff_user = CustomUser.objects.create(
            username='staff_plain_user',
            email='staff_plain@example.com',
            role='USER',
            is_staff=True,
            is_superuser=False,
            is_email_verified=True
        )
        staff_user.set_password('StaffPassword123!')
        staff_user.save()

        # 1. Admin login portal rejects ordinary staff
        res_admin = self.client.post(
            reverse('admin_login'),
            {'username': 'staff_plain_user', 'password': 'StaffPassword123!'}
        )
        self.assertEqual(res_admin.status_code, 200)
        self.assertContains(res_admin, 'Access denied. This portal is restricted to system administrators.')
        staff_user.refresh_from_db()
        self.assertEqual(staff_user.role, 'USER')

        # 2. Main login password rejects non-admin accounts (Policy B3: OTP required)
        res_login = self.client.post(
            reverse('login'),
            {'action': 'login_password', 'identifier': 'staff_plain_user', 'password': 'StaffPassword123!'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(res_login.status_code, 200)
        d = res_login.json()
        self.assertFalse(d.get('success'))
        self.assertTrue(d.get('otp_required'))

        # 3. Dashboard redirect avoids infinite loop and routes to role home
        self.client.force_login(staff_user)
        res_dash = self.client.get(reverse('dashboard'))
        self.assertRedirects(res_dash, reverse('home'))

    def test_change_6_login_otp_ip_enumeration_throttling(self):
        """Change 6: Repeated attempts from same IP looking for unmapped accounts are rate-limited."""
        rf = RequestFactory()
        dummy_req = rf.post('/login/', REMOTE_ADDR='192.0.2.99')
        ip_key = login_otp_ip_key(dummy_req)
        clear(ip_key)

        enum_client = Client(REMOTE_ADDR='192.0.2.99')

        for i in range(10):
            res = enum_client.post(
                reverse('login'),
                {'action': 'send_login_otp', 'identifier': f'nonexistent_enum_{i}@fake.domain'},
                HTTP_X_REQUESTED_WITH='XMLHttpRequest'
            )
            self.assertEqual(res.status_code, 200)
            d = res.json()
            self.assertFalse(d.get('success'))

        # 11th request triggers network throttle
        res_blocked = enum_client.post(
            reverse('login'),
            {'action': 'send_login_otp', 'identifier': 'any_account@example.com'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        d_blocked = res_blocked.json()
        self.assertFalse(d_blocked.get('success'))
        self.assertIn('Too many login attempts from this network', d_blocked.get('error', ''))
        clear(ip_key)

    def test_finding_b1_worker_wallet_locked_unbookable(self):
        """Finding B1: Worker with locked wallet balance (< -500) is marked unbookable upfront."""
        from decimal import Decimal
        from lamoapp.models import Service, WorkerWallet
        from lamoapp.views import get_worker_unavailability_reasons

        w_user = CustomUser.objects.create_user('b1_worker_user', 'b1w@lamo.in', 'Pass123!', role='WORKER')
        srv = Service.objects.create(name='B1 Service', price=500, is_active=True)
        worker = WorkerProfile.objects.create(user=w_user, service=srv, district='Kollam', work_status='AVAILABLE', is_verified=True)
        wallet = WorkerWallet.objects.create(worker=worker, balance=Decimal('-600.00'))

        reasons = get_worker_unavailability_reasons(worker)
        self.assertIn('Temporarily unavailable for new requests.', reasons)

        # In service_workers view, is_bookable should be False
        res = self.client.get(reverse('service_workers', args=[srv.id]) + '?district=Kollam')
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Temporarily unavailable for new requests.')

    def test_finding_b2_worker_leave_status_rejected(self):
        """Finding B2: Workers marked 'LEAVE' or 'ON_DUTY' cannot be booked."""
        from lamoapp.models import Service

        cust = CustomUser.objects.create_user('b2_cust', 'b2c@lamo.in', 'Pass123!', role='USER')
        w_user = CustomUser.objects.create_user('b2_w', 'b2w@lamo.in', 'Pass123!', role='WORKER')
        srv = Service.objects.create(name='B2 Service', price=500, is_active=True)
        worker = WorkerProfile.objects.create(user=w_user, service=srv, district='Kollam', work_status='LEAVE', is_verified=True)

        self.client.force_login(cust)
        res = self.client.post(
            reverse('book_service'),
            {
                'worker': worker.id,
                'service': srv.id,
                'date': '2026-11-25',
                'time': '10:00 AM - 10:30 AM',
                'location': 'Kollam beach road',
                'phone': '9876543210'
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(res.status_code, 400)
        d = res.json()
        self.assertFalse(d['success'])
        self.assertIn('currently on leave', d['error_message'].lower())

    def test_finding_b3_revalidation_after_otp(self):
        """Finding B3: Revalidation blocks booking creation if service deactivated or worker unavailable."""
        from decimal import Decimal
        from lamoapp.models import Service, WorkerWallet
        from lamoapp.views import _create_booking_from_pending

        cust = CustomUser.objects.create_user('b3_cust', 'b3c@lamo.in', 'Pass123!', role='USER')
        w_user = CustomUser.objects.create_user('b3_w', 'b3w@lamo.in', 'Pass123!', role='WORKER')
        srv = Service.objects.create(name='B3 Service', price=500, is_active=True)
        worker = WorkerProfile.objects.create(user=w_user, service=srv, district='Kollam', work_status='AVAILABLE', is_verified=True)

        rf = RequestFactory()
        req = rf.post('/verify-otp/')
        from django.contrib.messages.storage.fallback import FallbackStorage
        setattr(req, 'session', {})
        setattr(req, '_messages', FallbackStorage(req))

        pending_data = {
            'worker_id': worker.id,
            'service_id': srv.id,
            'booking_type': 'One Time',
            'date': '2026-11-28',
            'time': '11:00 AM - 11:30 AM',
            'location': 'Kollam town',
            'phone': '9876543210'
        }

        # Case 1: Inactive service
        srv.is_active = False
        srv.save()
        b_res1 = _create_booking_from_pending(req, cust, pending_data)
        self.assertIsNone(b_res1)

        # Restore service, make worker SUSPENDED
        srv.is_active = True
        srv.save()
        worker.work_status = 'SUSPENDED'
        worker.save()
        b_res2 = _create_booking_from_pending(req, cust, pending_data)
        self.assertIsNone(b_res2)

        # Restore worker, lock wallet
        worker.work_status = 'AVAILABLE'
        worker.save()
        wallet = WorkerWallet.objects.create(worker=worker, balance=Decimal('-700.00'))
        b_res3 = _create_booking_from_pending(req, cust, pending_data)
        self.assertIsNone(b_res3)

        # Unlock wallet -> succeeds
        wallet.balance = Decimal('100.00')
        wallet.save()
        b_res4 = _create_booking_from_pending(req, cust, pending_data)
        self.assertIsNotNone(b_res4)
        self.assertEqual(b_res4.status, 'PENDING')

    def test_finding_b4_pending_email_subject_accuracy(self):
        """Finding B4: Pending booking email subject uses 'Booking Request Received', not 'Confirmed'."""
        from unittest.mock import patch
        from lamoapp.models import Service

        cust = CustomUser.objects.create_user('b4_cust', 'b4c@lamo.in', 'Pass123!', role='USER')
        w_user = CustomUser.objects.create_user('b4_w', 'b4w@lamo.in', 'Pass123!', role='WORKER')
        srv = Service.objects.create(name='B4 Service', price=500, is_active=True)
        worker = WorkerProfile.objects.create(user=w_user, service=srv, district='Kollam', work_status='AVAILABLE', is_verified=True)
        WorkerWallet.objects.create(worker=worker, balance=Decimal('500.00'))

        self.client.force_login(cust)
        with patch('lamoapp.views.send_email_safely') as mock_send:
            res = self.client.post(
                reverse('book_service'),
                {
                    'worker': worker.id,
                    'service': srv.id,
                    'date': '2026-11-28',
                    'time': '10:00 AM - 10:30 AM',
                    'location': 'Kollam beach road',
                    'phone': '9876543210'
                },
                HTTP_X_REQUESTED_WITH='XMLHttpRequest'
            )
            self.assertEqual(res.status_code, 200)
            mock_send.assert_called()
            # Inspect subject of the email sent to customer
            call_kwargs = mock_send.call_args.kwargs
            subject = call_kwargs.get('subject', '')
            self.assertIn('Booking Request Received', subject)
            self.assertNotIn('Booking Confirmed', subject)

    def test_finding_b5_recurring_series_lifecycle_sync(self):
        """Finding B5: Accepting/rejecting a recurring series booking syncs all pending occurrences in series."""
        from lamoapp.models import Service, Booking, BookingSeries

        cust = CustomUser.objects.create_user('b5_cust', 'b5c@lamo.in', 'Pass123!', role='USER')
        w_user = CustomUser.objects.create_user('b5_w', 'b5w@lamo.in', 'Pass123!', role='WORKER')
        srv = Service.objects.create(name='B5 Service', price=500, is_active=True)
        worker = WorkerProfile.objects.create(user=w_user, service=srv, district='Kollam', work_status='AVAILABLE', is_verified=True)
        WorkerWallet.objects.create(worker=worker, balance=Decimal('500.00'))

        series = BookingSeries.objects.create(
            customer=cust, worker=worker, service=srv,
            frequency='Daily', time='10:00 AM - 10:30 AM',
            location='Kollam', phone='9876543210',
            start_date='2026-11-20', end_date='2026-11-22'
        )
        b1 = Booking.objects.create(customer=cust, worker=worker, service=srv, series=series, date='2026-11-20', time='10:00 AM - 10:30 AM', status='PENDING')
        b2 = Booking.objects.create(customer=cust, worker=worker, service=srv, series=series, date='2026-11-21', time='10:00 AM - 10:30 AM', status='PENDING')
        b3 = Booking.objects.create(customer=cust, worker=worker, service=srv, series=series, date='2026-11-22', time='10:00 AM - 10:30 AM', status='PENDING')

        self.client.force_login(w_user)
        res = self.client.post(reverse('worker_accept_booking', args=[b1.id]), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)

        b1.refresh_from_db()
        b2.refresh_from_db()
        b3.refresh_from_db()
        self.assertEqual(b1.status, 'ACCEPTED')
        self.assertEqual(b2.status, 'ACCEPTED')
        self.assertEqual(b3.status, 'ACCEPTED')

        # Test Rejection Sync on a second series
        series2 = BookingSeries.objects.create(
            customer=cust, worker=worker, service=srv,
            frequency='Daily', time='11:00 AM - 11:30 AM',
            location='Kollam', phone='9876543210',
            start_date='2026-11-25', end_date='2026-11-26'
        )
        sb1 = Booking.objects.create(customer=cust, worker=worker, service=srv, series=series2, date='2026-11-25', time='11:00 AM - 11:30 AM', status='PENDING')
        sb2 = Booking.objects.create(customer=cust, worker=worker, service=srv, series=series2, date='2026-11-26', time='11:00 AM - 11:30 AM', status='PENDING')

        res_rej = self.client.post(reverse('worker_reject_booking', args=[sb1.id]), {'rejection_reason': 'Busy schedule'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_rej.status_code, 200)

        sb1.refresh_from_db()
        sb2.refresh_from_db()
        series2.refresh_from_db()
        self.assertEqual(sb1.status, 'REJECTED')
        self.assertEqual(sb2.status, 'REJECTED')
        self.assertFalse(series2.is_active)

    def test_finding_b6_cash_on_hand_only_and_default(self):
        """Finding B6: Online payment is hidden; Cash on Hand is the sole, checked payment method."""
        from lamoapp.models import Service

        cust = CustomUser.objects.create_user('b6_cust', 'b6c@lamo.in', 'Pass123!', role='USER')
        w_user = CustomUser.objects.create_user('b6_w', 'b6w@lamo.in', 'Pass123!', role='WORKER')
        srv = Service.objects.create(name='B6 Service', price=500, is_active=True)
        worker = WorkerProfile.objects.create(user=w_user, service=srv, district='Kollam', work_status='AVAILABLE', is_verified=True)
        WorkerWallet.objects.create(worker=worker, balance=Decimal('500.00'))

        self.client.force_login(cust)
        res = self.client.get(reverse('book_service') + f'?worker={worker.id}&service={srv.id}')
        self.assertEqual(res.status_code, 200)

        # Ensure Online Payment radio is absent/hidden
        self.assertNotContains(res, 'id="payOnlineRadio"')
        # Ensure Cash on Hand is present and checked
        self.assertContains(res, 'id="payCashRadio"')
        self.assertContains(res, 'Cash on Hand')

        # Booking submitted without explicit payment_method defaults to CASH in database
        res_book = self.client.post(
            reverse('book_service'),
            {
                'worker': worker.id,
                'service': srv.id,
                'date': '2026-11-28',
                'time': '10:00 AM - 10:30 AM',
                'location': 'Kollam beach road',
                'phone': '9876543210'
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(res_book.status_code, 200)
        from lamoapp.models import Booking
        latest_b = Booking.objects.filter(customer=cust, service=srv).latest('id')
        self.assertEqual(latest_b.payment_method, 'CASH')

    def test_lifecycle_workflows_1_to_10(self):
        """End-to-End verification of all 10 core workflows."""
        from decimal import Decimal
        from django.utils import timezone
        from datetime import timedelta
        from lamoapp.models import Service, Booking, BookingSeries, WorkerWallet, WorkerLeave
        from lamoapp.views import _settle_booking_completion_financials, check_and_expire_pending_bookings

        # Setup standard actors
        c_user = CustomUser.objects.create_user('lifecycle_cust', 'lc_c@lamo.in', 'Pass123!', role='USER')
        w_user = CustomUser.objects.create_user('lifecycle_w', 'lc_w@lamo.in', 'Pass123!', role='WORKER')
        srv = Service.objects.create(name='Lifecycle Service', price=600, is_active=True)
        worker = WorkerProfile.objects.create(user=w_user, service=srv, district='Kollam', work_status='AVAILABLE', is_verified=True)
        wallet = WorkerWallet.objects.create(worker=worker, balance=Decimal('500.00'))

        self.client.force_login(c_user)

        # 1. Normal Booking
        res_normal = self.client.post(reverse('book_service'), {
            'worker': worker.id, 'service': srv.id,
            'date': (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%d'),
            'time': '10:00 AM - 10:30 AM', 'location': 'Kollam town', 'phone': '9876543210'
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_normal.status_code, 200)
        b_normal = Booking.objects.get(id=res_normal.json()['booking_id'])
        self.assertEqual(b_normal.status, 'PENDING')
        self.assertEqual(b_normal.payment_method, 'CASH')

        # 2. Emergency Booking
        res_emer = self.client.post(reverse('book_service'), {
            'service': srv.id, 'problem': 'Sparking switch board emergency',
            'time': 'ASAP', 'location': 'Kollam town', 'phone': '9876543210'
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_emer.status_code, 200)
        b_emer = Booking.objects.get(id=res_emer.json()['booking_id'])
        self.assertIn(b_emer.status, ['PENDING', 'EMERGENCY_PENDING'])

        # 3. Future Booking (5 days out)
        res_future = self.client.post(reverse('book_service'), {
            'worker': worker.id, 'service': srv.id,
            'date': (timezone.now() + timedelta(days=5)).strftime('%Y-%m-%d'),
            'time': '02:00 PM - 02:30 PM', 'location': 'Kollam town', 'phone': '9876543210'
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_future.status_code, 200)
        b_future = Booking.objects.get(id=res_future.json()['booking_id'])

        # 4. Recurring Booking (temporarily disabled per audit requirement)
        res_rec = self.client.post(reverse('book_service'), {
            'worker': worker.id, 'service': srv.id, 'booking_type': 'Recurring',
            'start_date': (timezone.now() + timedelta(days=2)).strftime('%Y-%m-%d'),
            'end_date': (timezone.now() + timedelta(days=4)).strftime('%Y-%m-%d'),
            'frequency': 'Daily', 'time': '09:00 AM - 09:30 AM',
            'location': 'Kollam town', 'phone': '9876543210'
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_rec.status_code, 400)
        self.assertIn('Recurring bookings are temporarily unavailable', res_rec.json().get('error_message', ''))

        # 5. Worker Leave Check
        leave_date = (timezone.now() + timedelta(days=10)).date()
        WorkerLeave.objects.create(worker=worker, leave_date=leave_date, reason='Doctor appointment')
        res_leave = self.client.post(reverse('book_service'), {
            'worker': worker.id, 'service': srv.id,
            'date': leave_date.strftime('%Y-%m-%d'),
            'time': '10:00 AM - 10:30 AM', 'location': 'Kollam town', 'phone': '9876543210'
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_leave.status_code, 400)
        self.assertIn('on leave', res_leave.json()['error_message'].lower())

        # 6. Locked Wallet Check
        wallet.balance = Decimal('-600.00')
        wallet.save()
        res_locked = self.client.post(reverse('book_service'), {
            'worker': worker.id, 'service': srv.id,
            'date': (timezone.now() + timedelta(days=11)).strftime('%Y-%m-%d'),
            'time': '10:00 AM - 10:30 AM', 'location': 'Kollam town', 'phone': '9876543210'
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_locked.status_code, 400)
        self.assertIn('temporarily unavailable', res_locked.json()['error_message'].lower())
        wallet.balance = Decimal('500.00')
        wallet.save()

        # 7. OTP-based Guest Booking
        self.client.logout()
        res_guest_post = self.client.post(reverse('book_service'), {
            'worker': worker.id, 'service': srv.id,
            'date': (timezone.now() + timedelta(days=12)).strftime('%Y-%m-%d'),
            'time': '10:00 AM - 10:30 AM', 'location': 'Kollam beach',
            'phone': '9847112233', 'customer_name': 'Guest Customer',
            'auth_identifier': 'guest_unique_e2e@lamo.in'
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_guest_post.status_code, 200)
        d_guest = res_guest_post.json()
        self.assertTrue(d_guest.get('requires_otp'))

        # 8. Worker Acceptance & Rejection
        self.client.force_login(w_user)
        res_acc = self.client.post(reverse('worker_accept_booking', args=[b_normal.id]), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_acc.status_code, 200)
        b_normal.refresh_from_db()
        self.assertEqual(b_normal.status, 'ACCEPTED')

        res_rej = self.client.post(reverse('worker_reject_booking', args=[b_future.id]), {'rejection_reason': 'Out of range'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_rej.status_code, 200)
        b_future.refresh_from_db()
        self.assertEqual(b_future.status, 'REJECTED')

        # 9. Booking Completion & Financial Settlement (Exact-once guarantee - Finding C1)
        # In the prepaid model, commission was deducted upon acceptance in Step 8
        wallet.refresh_from_db()
        bal_at_acceptance = wallet.balance
        self.assertLess(bal_at_acceptance, Decimal('500.00'))
        self.assertTrue(b_normal.fee_settled)

        b_normal.status = 'COMPLETED'
        b_normal.final_amount = Decimal('600.00')
        b_normal.save()
        _settle_booking_completion_financials(b_normal)
        wallet.refresh_from_db()
        b_normal.refresh_from_db()
        self.assertTrue(b_normal.fee_settled)
        # Attempt second deduction -> should do nothing (exact once guarantee: not deducted again at completion)
        self.assertEqual(wallet.balance, bal_at_acceptance)
        _settle_booking_completion_financials(b_normal)
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, bal_at_acceptance)

        # 10. Cancellation & Expiry
        b_expire_target = Booking.objects.create(
            customer=c_user, worker=worker, service=srv,
            date=(timezone.now() + timedelta(days=1)).date(),
            time='11:00 AM - 11:30 AM', status='PENDING'
        )
        Booking.objects.filter(id=b_expire_target.id).update(created_at=timezone.now() - timedelta(minutes=70))
        check_and_expire_pending_bookings(booking=b_expire_target)
        b_expire_target.refresh_from_db()
        self.assertEqual(b_expire_target.status, 'EXPIRED')

    def test_finding_c1_commission_deducted_at_acceptance_and_no_fictional_credits(self):
        """Finding C1: Platform commission is deducted at acceptance, exactly once, and fictional credits removed."""
        from lamoapp.models import Service, Booking, WorkerWallet, WalletTransaction
        from lamoapp.views import _settle_booking_completion_financials

        cust = CustomUser.objects.create_user('c1_cust', 'c1c@lamo.in', 'Pass123!', role='USER')
        w_user = CustomUser.objects.create_user('c1_worker', 'c1w@lamo.in', 'Pass123!', role='WORKER')
        srv = Service.objects.create(name='C1 Service', price=500, is_active=True)
        worker = WorkerProfile.objects.create(user=w_user, service=srv, district='Kollam', work_status='AVAILABLE', is_verified=True)
        # Expire launch so commission is active
        worker.launch_ends_at = timezone.now() - timedelta(days=1)
        worker.save()
        wallet = WorkerWallet.objects.create(worker=worker, balance=Decimal('300.00'))

        booking = Booking.objects.create(
            customer=cust, worker=worker, service=srv,
            date=(timezone.now() + timedelta(days=2)).date(),
            time='10:00 AM - 10:30 AM', status='PENDING',
            estimated_cost=Decimal('500.00'), payment_method='ONLINE'
        )

        # Worker accepts booking -> commission debited immediately
        self.client.force_login(w_user)
        res = self.client.post(reverse('worker_accept_booking', args=[booking.id]), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)

        booking.refresh_from_db()
        wallet.refresh_from_db()
        self.assertEqual(booking.status, 'ACCEPTED')
        self.assertTrue(booking.fee_settled)
        self.assertEqual(booking.platform_fee, Decimal('50.00'))  # 10% of 500
        self.assertEqual(wallet.balance, Decimal('250.00'))  # 300 - 50

        # Completion should NOT deduct fee again and should NOT credit fictional ONLINE amount
        b_id = booking.id
        booking.status = 'COMPLETED'
        booking.final_amount = Decimal('500.00')
        booking.save()
        _settle_booking_completion_financials(booking)
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal('250.00'))  # Unchanged: no second deduction, no fictional online credit

        # Verify no ONLINE_PAYOUT_OFFSET transaction exists
        has_online_credit = WalletTransaction.objects.filter(booking_id=b_id, transaction_type='ONLINE_PAYOUT_OFFSET').exists()
        self.assertFalse(has_online_credit)

    def test_finding_c2_prepaid_wallet_insufficient_balance_blocks_acceptance(self):
        """Finding C2: Worker balance must be >= required platform fee to accept; blocked otherwise."""
        from lamoapp.models import Service, Booking, WorkerWallet

        cust = CustomUser.objects.create_user('c2_cust', 'c2c@lamo.in', 'Pass123!', role='USER')
        w_user = CustomUser.objects.create_user('c2_worker', 'c2w@lamo.in', 'Pass123!', role='WORKER')
        srv = Service.objects.create(name='C2 Service', price=500, is_active=True)
        worker = WorkerProfile.objects.create(user=w_user, service=srv, district='Kollam', work_status='AVAILABLE', is_verified=True)
        worker.launch_ends_at = timezone.now() - timedelta(days=1)
        worker.save()
        # Insufficient prepaid balance: ₹20 balance, but ₹50 fee required
        wallet = WorkerWallet.objects.create(worker=worker, balance=Decimal('20.00'))

        booking = Booking.objects.create(
            customer=cust, worker=worker, service=srv,
            date=(timezone.now() + timedelta(days=2)).date(),
            time='10:00 AM - 10:30 AM', status='PENDING',
            estimated_cost=Decimal('500.00')
        )

        self.client.force_login(w_user)
        res = self.client.post(reverse('worker_accept_booking', args=[booking.id]), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 400)
        self.assertIn('insufficient', res.json()['message'].lower())

        booking.refresh_from_db()
        self.assertEqual(booking.status, 'PENDING')

        # Recharge wallet to ₹100 -> acceptance succeeds
        wallet.balance = Decimal('100.00')
        wallet.save()
        res_ok = self.client.post(reverse('worker_accept_booking', args=[booking.id]), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_ok.status_code, 200)
        booking.refresh_from_db()
        wallet.refresh_from_db()
        self.assertEqual(booking.status, 'ACCEPTED')
        self.assertEqual(wallet.balance, Decimal('50.00'))

    def test_finding_c3_radar_accept_and_decline_urls(self):
        """Finding C3: Radar endpoints resolve and function correctly via plural and singular aliases."""
        from lamoapp.models import Service, Booking, WorkerWallet

        cust = CustomUser.objects.create_user('c3_cust', 'c3c@lamo.in', 'Pass123!', role='USER')
        w_user = CustomUser.objects.create_user('c3_worker', 'c3w@lamo.in', 'Pass123!', role='WORKER')
        srv = Service.objects.create(name='C3 Service', price=500, is_active=True)
        worker = WorkerProfile.objects.create(user=w_user, service=srv, district='Kollam', work_status='AVAILABLE', is_verified=True)
        WorkerWallet.objects.create(worker=worker, balance=Decimal('200.00'))

        b_acc = Booking.objects.create(
            customer=cust, worker=worker, service=srv,
            date=(timezone.now() + timedelta(days=2)).date(),
            time='10:00 AM - 10:30 AM', status='PENDING'
        )
        b_rej = Booking.objects.create(
            customer=cust, worker=worker, service=srv,
            date=(timezone.now() + timedelta(days=3)).date(),
            time='10:00 AM - 10:30 AM', status='PENDING'
        )

        self.client.force_login(w_user)
        # Test plural URL (configured in urls.py)
        res1 = self.client.post(f'/worker/bookings/{b_acc.id}/accept/', {'ajax': '1'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res1.status_code, 200)

        # Test singular alias URL (radar JS compatibility)
        res2 = self.client.post(f'/worker/booking/{b_rej.id}/reject/', {'ajax': '1'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res2.status_code, 200)

    def test_finding_c4_emergency_booking_district_and_wallet_protection(self):
        """Finding C4: Emergency acceptance enforces district matching and wallet prepaid balance."""
        from lamoapp.models import Service, Booking, WorkerWallet

        cust = CustomUser.objects.create_user('c4_cust', 'c4c@lamo.in', 'Pass123!', role='USER')
        w_user = CustomUser.objects.create_user('c4_worker', 'c4w@lamo.in', 'Pass123!', role='WORKER')
        srv = Service.objects.create(name='C4 Service', price=500, is_active=True)
        worker = WorkerProfile.objects.create(user=w_user, service=srv, district='Kollam', work_status='AVAILABLE', is_verified=True)
        wallet = WorkerWallet.objects.create(worker=worker, balance=Decimal('100.00'))

        # 1. District mismatch: Booking is in Ernakulam, worker is in Kollam
        b_other_dist = Booking.objects.create(
            customer=cust, service=srv, booking_type='Emergency', district='Ernakulam',
            status='EMERGENCY_PENDING', time='ASAP', estimated_cost=Decimal('500.00')
        )

        self.client.force_login(w_user)
        res_mismatch = self.client.post(reverse('worker_accept_emergency', args=[b_other_dist.id]))
        b_other_dist.refresh_from_db()
        self.assertEqual(b_other_dist.status, 'EMERGENCY_PENDING')
        self.assertIsNone(b_other_dist.worker)

        # 2. Locked / insufficient wallet: worker wallet balance drops below 0
        wallet.balance = Decimal('-100.00')
        wallet.save()
        b_same_dist = Booking.objects.create(
            customer=cust, service=srv, booking_type='Emergency', district='Kollam',
            status='EMERGENCY_PENDING', time='ASAP', estimated_cost=Decimal('500.00')
        )
        res_locked = self.client.post(reverse('worker_accept_emergency', args=[b_same_dist.id]))
        b_same_dist.refresh_from_db()
        self.assertEqual(b_same_dist.status, 'EMERGENCY_PENDING')
        self.assertIsNone(b_same_dist.worker)

        # 3. Valid district & sufficient wallet balance -> succeeds and fee is deducted
        wallet.balance = Decimal('100.00')
        wallet.save()
        res_ok = self.client.post(reverse('worker_accept_emergency', args=[b_same_dist.id]))
        self.assertEqual(res_ok.status_code, 302)
        b_same_dist.refresh_from_db()
        self.assertEqual(b_same_dist.status, 'ACCEPTED')
        self.assertEqual(b_same_dist.worker, worker)
        self.assertTrue(b_same_dist.fee_settled)

    def test_finding_c5_http_method_restriction_and_invalid_id_safety(self):
        """Finding C5: GET requests to accept/reject return 405; invalid booking IDs return 404, not 500."""
        from lamoapp.models import Service, Booking, WorkerWallet

        cust = CustomUser.objects.create_user('c5_cust', 'c5c@lamo.in', 'Pass123!', role='USER')
        w_user1 = CustomUser.objects.create_user('c5_worker1', 'c5w1@lamo.in', 'Pass123!', role='WORKER')
        w_user2 = CustomUser.objects.create_user('c5_worker2', 'c5w2@lamo.in', 'Pass123!', role='WORKER')
        srv = Service.objects.create(name='C5 Service', price=500, is_active=True)
        worker1 = WorkerProfile.objects.create(user=w_user1, service=srv, district='Kollam', work_status='AVAILABLE', is_verified=True)
        worker2 = WorkerProfile.objects.create(user=w_user2, service=srv, district='Kollam', work_status='AVAILABLE', is_verified=True)
        WorkerWallet.objects.create(worker=worker1, balance=Decimal('200.00'))

        booking_for_w1 = Booking.objects.create(
            customer=cust, worker=worker1, service=srv,
            date=(timezone.now() + timedelta(days=2)).date(),
            time='10:00 AM - 10:30 AM', status='PENDING'
        )

        # GET request should be rejected with 405 Method Not Allowed
        self.client.force_login(w_user1)
        res_get_acc = self.client.get(reverse('worker_accept_booking', args=[booking_for_w1.id]))
        self.assertEqual(res_get_acc.status_code, 405)
        res_get_rej = self.client.get(reverse('worker_reject_booking', args=[booking_for_w1.id]))
        self.assertEqual(res_get_rej.status_code, 405)

        # Worker 2 trying to reject Worker 1's booking -> returns 404 (not a 500 error!)
        self.client.force_login(w_user2)
        res_other_rej = self.client.post(reverse('worker_reject_booking', args=[booking_for_w1.id]), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_other_rej.status_code, 404)

        # Non-existent booking ID -> returns 404 (not a 500 error!)
        res_bad_id = self.client.post(reverse('worker_reject_booking', args=[999999]), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_bad_id.status_code, 404)

    def test_finding_c6_wallet_recharge_uses_actual_amount_field(self):
        """Finding C6: Recharge amount is stored in tx.amount and approved without fallback."""
        from lamoapp.models import Service, WorkerWallet, WalletTransaction

        admin_user = CustomUser.objects.create_user('c6_admin', 'c6a@lamo.in', 'Pass123!', role='ADMIN')
        w_user = CustomUser.objects.create_user('c6_worker', 'c6w@lamo.in', 'Pass123!', role='WORKER')
        srv = Service.objects.create(name='C6 Service', price=500, is_active=True)
        worker = WorkerProfile.objects.create(user=w_user, service=srv, district='Kollam', work_status='AVAILABLE', is_verified=True)
        wallet = WorkerWallet.objects.create(worker=worker, balance=Decimal('50.00'))

        # Worker submits recharge for ₹350.00
        self.client.force_login(w_user)
        res_sub = self.client.post(reverse('worker_recharge_wallet'), {
            'amount': '350.00',
            'reference_id': 'UTR9876543210'
        })
        self.assertEqual(res_sub.status_code, 302)

        tx = WalletTransaction.objects.get(reference_id='UTR9876543210')
        self.assertEqual(tx.amount, Decimal('350.00'))
        self.assertEqual(tx.transaction_type, 'RECHARGE_PENDING')

        # Admin approves recharge -> wallet is credited with exactly ₹350.00
        self.client.force_login(admin_user)
        res_app = self.client.post(reverse('admin_approve_wallet_recharge', args=[tx.id]))
        self.assertEqual(res_app.status_code, 302)

        wallet.refresh_from_db()
        tx.refresh_from_db()
        self.assertEqual(tx.transaction_type, 'RECHARGE')
        self.assertEqual(wallet.balance, Decimal('400.00'))  # 50 + 350

    def test_finding_d1_doorstep_qr_authentication_and_self_scan_block(self):
        """Finding D1: QR requires login; worker self-scan is blocked; unauthorized users blocked; PIN fallback works."""
        from lamoapp.models import Service, Booking, WorkerWallet

        cust = CustomUser.objects.create_user('d1_cust', 'd1c@lamo.in', 'Pass123!', role='USER')
        other_cust = CustomUser.objects.create_user('d1_other', 'd1o@lamo.in', 'Pass123!', role='USER')
        w_user = CustomUser.objects.create_user('d1_worker', 'd1w@lamo.in', 'Pass123!', role='WORKER')
        srv = Service.objects.create(name='D1 Service', price=500, is_active=True)
        worker = WorkerProfile.objects.create(user=w_user, service=srv, district='Kollam', work_status='AVAILABLE', is_verified=True)
        WorkerWallet.objects.create(worker=worker, balance=Decimal('200.00'))

        booking = Booking.objects.create(
            customer=cust, worker=worker, service=srv,
            date=timezone.now().date(), time='10:00 AM - 10:30 AM',
            status='ACCEPTED', arrival_otp='4321'
        )
        token = booking.get_arrival_token()

        # 1. Unauthenticated request redirects to login
        self.client.logout()
        res_anon = self.client.get(reverse('verify_doorstep_qr', args=[token]))
        self.assertEqual(res_anon.status_code, 302)
        self.assertIn('/login/', res_anon.url)

        # 2. Worker scanning their own QR code is blocked with 403 Forbidden
        self.client.force_login(w_user)
        res_worker = self.client.get(reverse('verify_doorstep_qr', args=[token]))
        self.assertEqual(res_worker.status_code, 403)
        self.assertContains(res_worker, 'Self-Verification Prohibited', status_code=403)

        # 3. Another customer scanning is blocked with 403 Forbidden
        self.client.force_login(other_cust)
        res_other = self.client.get(reverse('verify_doorstep_qr', args=[token]))
        self.assertEqual(res_other.status_code, 403)
        self.assertContains(res_other, 'Unauthorized Customer Account', status_code=403)

        # 4. Legitimate booking customer confirms arrival via QR -> booking becomes IN_PROGRESS
        self.client.force_login(cust)
        res_cust = self.client.post(reverse('verify_doorstep_qr', args=[token]), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res_cust.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'IN_PROGRESS')
        self.assertIsNotNone(booking.arrival_verified_at)

        # Complete first booking so worker has no active in-progress job
        booking.status = 'COMPLETED'
        booking.save()

        # 5. Worker starting job without customer verification is blocked; valid Arrival PIN succeeds
        b2 = Booking.objects.create(
            customer=cust, worker=worker, service=srv,
            date=timezone.now().date(), time='11:00 AM - 11:30 AM',
            status='ACCEPTED', arrival_otp='7890'
        )
        self.client.force_login(w_user)
        # Missing OTP / no QR scan -> blocked
        res_no_otp = self.client.post(reverse('worker_start_job', args=[b2.id]), {'arrival_otp': ''})
        b2.refresh_from_db()
        self.assertEqual(b2.status, 'ACCEPTED')

        # Correct Arrival PIN provided by customer -> starts successfully
        res_with_otp = self.client.post(reverse('worker_start_job', args=[b2.id]), {'arrival_otp': '7890'})
        b2.refresh_from_db()
        self.assertEqual(b2.status, 'IN_PROGRESS')

    def test_finding_d2_doorstep_local_qr_and_status_api_security(self):
        """Finding D2: Doorstep QR code is generated locally; status API requires authorization."""
        from lamoapp.models import Service, Booking, WorkerWallet

        cust = CustomUser.objects.create_user('d2_cust', 'd2c@lamo.in', 'Pass123!', role='USER')
        other_user = CustomUser.objects.create_user('d2_other', 'd2o@lamo.in', 'Pass123!', role='USER')
        w_user = CustomUser.objects.create_user('d2_worker', 'd2w@lamo.in', 'Pass123!', role='WORKER')
        srv = Service.objects.create(name='D2 Service', price=500, is_active=True)
        worker = WorkerProfile.objects.create(user=w_user, service=srv, district='Kollam', work_status='AVAILABLE', is_verified=True)
        WorkerWallet.objects.create(worker=worker, balance=Decimal('200.00'))

        booking = Booking.objects.create(
            customer=cust, worker=worker, service=srv,
            date=timezone.now().date(), time='10:00 AM - 10:30 AM',
            status='ACCEPTED'
        )

        # 1. Local QR generation generates a valid PNG image directly
        self.client.force_login(w_user)
        res_qr = self.client.get(reverse('worker_booking_doorstep_qr', args=[booking.id]))
        self.assertEqual(res_qr.status_code, 200)
        self.assertEqual(res_qr['Content-Type'], 'image/png')
        self.assertGreater(len(res_qr.content), 100)

        # 2. Status API unauthorized probe is rejected with 403
        self.client.force_login(other_user)
        res_unauth = self.client.get(reverse('api_booking_doorstep_status', args=[booking.id]))
        self.assertEqual(res_unauth.status_code, 403)

        # 3. Status API authorized query succeeds
        self.client.force_login(w_user)
        res_auth = self.client.get(reverse('api_booking_doorstep_status', args=[booking.id]))
        self.assertEqual(res_auth.status_code, 200)
        self.assertTrue(res_auth.json()['success'])

    def test_finding_d3_notification_doorstep_qr_text_consistency(self):
        """Finding D3: Arrival notification text prioritizes Doorstep QR Code scan with PIN as fallback."""
        from lamoapp.models import Service, Booking, Notification
        from lamoapp.booking_reminders import process_booking_schedule_notifications

        cust = CustomUser.objects.create_user('d3_cust', 'd3c@lamo.in', 'Pass123!', role='USER')
        w_user = CustomUser.objects.create_user('d3_worker', 'd3w@lamo.in', 'Pass123!', role='WORKER')
        srv = Service.objects.create(name='D3 Plumbing', price=500, is_active=True)
        worker = WorkerProfile.objects.create(user=w_user, service=srv, district='Kollam', work_status='AVAILABLE', is_verified=True)

        now_time = timezone.localtime()
        start_dt = now_time - timedelta(minutes=15)
        end_dt = now_time + timedelta(minutes=30)
        booking = Booking.objects.create(
            customer=cust, worker=worker, service=srv,
            date=now_time.date(),
            start_time=start_dt.time(),
            end_time=end_dt.time(),
            time=f"{start_dt.strftime('%I:%M %p')} - {end_dt.strftime('%I:%M %p')}",
            status='ACCEPTED', arrival_otp='5555'
        )

        process_booking_schedule_notifications(now=now_time)

        notif_cust = Notification.objects.filter(recipient=cust, booking=booking).first()
        self.assertIsNotNone(notif_cust)
        self.assertIn('scan their Doorstep QR code', notif_cust.message)
        self.assertIn('5555', notif_cust.message)

        notif_worker = Notification.objects.filter(recipient=w_user, booking=booking).first()
        self.assertIsNotNone(notif_worker)
        self.assertIn('present your Doorstep QR code', notif_worker.message)


class FinalSecurityAuditRegressionTest(TestCase):
    """
    Comprehensive regression tests for:
    - B1: Guest booking OTP identity verification and account takeover prevention.
    - B2: Admin privilege escalation prevention and redirect loop elimination.
    - B3: Authentication policy enforcement (Admin=Password only, Customer/Worker=OTP only).
    - Section 5: Cross-role phone uniqueness across UserProfile and WorkerProfile.
    - Section 7: Complete booking data preservation through OTP handshake.
    """
    def setUp(self):
        self.client = Client()
        self.service = Service.objects.create(name='Test Electrician', price=Decimal('450.00'), is_active=True)
        self.w_user = CustomUser.objects.create_user('worker_tester', 'worker_t@lamo.in', role='WORKER')
        self.worker = WorkerProfile.objects.create(
            user=self.w_user, service=self.service, district='Ernakulam',
            work_status='AVAILABLE', is_verified=True, phone='9876543000'
        )
        self.wallet = WorkerWallet.objects.create(worker=self.worker, balance=Decimal('500.00'))

    def _setup_session_otp(self, identifier, customer_name='Test User', phone='9876543099'):
        """Helper to simulate an active booking OTP session with a known code '654321'."""
        import hashlib
        code = '654321'
        code_hash = hashlib.sha256(code.encode('utf-8')).hexdigest()
        is_email = '@' in identifier
        session = self.client.session
        session['booking_auth_data'] = {
            'identifier': identifier,
            'pending_channel': 'email' if is_email else 'phone',
            'pending_identifier': identifier.lower() if is_email else identifier,
            'verified_channel': None,
            'verified_identifier': None,
            'email': identifier.lower() if is_email else '',
            'phone': phone,
            'code_hash': code_hash,
            'created_at': timezone.now().isoformat(),
            'attempts': 0,
            'customer_name': customer_name,
        }
        session['pending_booking_data'] = {
            'worker_id': self.worker.id,
            'service_id': self.service.id,
            'booking_type': 'One Time',
            'date': (timezone.now() + timedelta(days=2)).strftime('%Y-%m-%d'),
            'time': '10:00 AM - 11:00 AM',
            'start_time': '10:00:00',
            'end_time': '11:00:00',
            'duration_min': 60,
            'buffer_min': 15,
            'location': 'Kaloor, Kochi',
            'latitude': 9.9880,
            'longitude': 76.2990,
            'district': 'Ernakulam',
            'description': 'Fan repair in master bedroom',
            'phone': phone,
            'customer_name': customer_name,
            'payment_method': 'Cash',
            'created_at': timezone.now().isoformat(),
        }
        session.save()
        return code

    def test_01_new_guest_email_otp(self):
        """1. New guest using email OTP auto-creates user with unusable password and creates booking."""
        code = self._setup_session_otp('newguest1@lamo.in', 'New Guest One', '9876540001')
        res = self.client.post(reverse('verify_otp'), {'action': 'verify', 'otp': code}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)

        user = CustomUser.objects.filter(email='newguest1@lamo.in').first()
        self.assertIsNotNone(user)
        self.assertEqual(user.role, 'USER')
        self.assertFalse(user.has_usable_password())
        self.assertTrue(user.is_email_verified)

        b = Booking.objects.filter(customer=user).first()
        self.assertIsNotNone(b)
        self.assertEqual(b.service_id, self.service.id)
        self.assertEqual(b.worker_id, self.worker.id)

    def test_02_new_guest_phone_otp(self):
        """2. New guest using phone OTP auto-creates user with verified phone on UserProfile."""
        code = self._setup_session_otp('9876540002', 'New Guest Two', '9876540002')
        res = self.client.post(reverse('verify_otp'), {'action': 'verify', 'otp': code}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)

        profile = UserProfile.objects.filter(phone='9876540002').first()
        self.assertIsNotNone(profile)
        self.assertEqual(profile.user.role, 'USER')
        self.assertFalse(profile.user.has_usable_password())

    def test_03_existing_customer_email_otp(self):
        """3. Existing customer using email OTP logs in to existing account without duplicating user."""
        existing = CustomUser.objects.create_user('existing_c1', 'existing_c1@lamo.in', role='USER')
        existing.set_unusable_password()
        existing.save()
        UserProfile.objects.create(user=existing, phone='9876540003')

        code = self._setup_session_otp('existing_c1@lamo.in', 'Existing Cust', '9876540003')
        res = self.client.post(reverse('verify_otp'), {'action': 'verify', 'otp': code}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)

        self.assertEqual(int(self.client.session['_auth_user_id']), existing.id)
        self.assertEqual(CustomUser.objects.filter(email='existing_c1@lamo.in').count(), 1)

    def test_04_existing_customer_phone_otp(self):
        """4. Existing customer using phone OTP logs in to existing account."""
        existing = CustomUser.objects.create_user('existing_c2', 'existing_c2@lamo.in', role='USER')
        existing.set_unusable_password()
        existing.save()
        UserProfile.objects.create(user=existing, phone='9876540004')

        code = self._setup_session_otp('9876540004', 'Existing Cust 2', '9876540004')
        res = self.client.post(reverse('verify_otp'), {'action': 'verify', 'otp': code}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)

        self.assertEqual(int(self.client.session['_auth_user_id']), existing.id)

    def test_05_guest_using_another_customer_phone(self):
        """5. Guest booking with their own email but entering another customer's phone is blocked."""
        victim = CustomUser.objects.create_user('victim_cust', 'victim@lamo.in', role='USER')
        UserProfile.objects.create(user=victim, phone='9876540005')

        code = self._setup_session_otp('attacker@lamo.in', 'Attacker', '9876540005')
        res = self.client.post(reverse('verify_otp'), {'action': 'verify', 'otp': code}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 400)
        self.assertIn('already associated with an existing account', res.json().get('error_message', ''))

    def test_06_guest_using_admin_phone(self):
        """6. Guest entering admin phone number is rejected."""
        admin = CustomUser.objects.create_superuser('super_adm_p', 'admin_p@lamo.in', 'AdminPass123!')
        UserProfile.objects.create(user=admin, phone='9876540006')

        code = self._setup_session_otp('attacker2@lamo.in', 'Attacker 2', '9876540006')
        res = self.client.post(reverse('verify_otp'), {'action': 'verify', 'otp': code}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 400)
        self.assertIn('already associated with an existing account', res.json().get('error_message', ''))

    def test_07_guest_using_worker_phone(self):
        """7. Guest entering worker phone number is rejected."""
        code = self._setup_session_otp('attacker3@lamo.in', 'Attacker 3', self.worker.phone)
        res = self.client.post(reverse('verify_otp'), {'action': 'verify', 'otp': code}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 400)
        self.assertIn('already associated with an existing account', res.json().get('error_message', ''))

    def test_08_modified_session_data(self):
        """8. Modified session with missing or tampered pending identifier is rejected."""
        session = self.client.session
        session['booking_auth_data'] = {'pending_channel': 'invalid', 'pending_identifier': ''}
        session.save()

        res = self.client.post(reverse('verify_otp'), {'action': 'verify', 'otp': '123456'}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 400)
        err = res.json().get('error_message', '').lower()
        self.assertTrue('expired' in err or 'session' in err)

    def test_09_modified_resend_request(self):
        """9. Resend OTP request cannot manipulate identity via POST params."""
        code = self._setup_session_otp('original@lamo.in', 'Original Name', '9876540009')
        # Attempt to spoof resend to a different email or phone in POST
        res = self.client.post(
            reverse('verify_otp'),
            {'action': 'resend', 'identifier': 'spoofed@lamo.in', 'phone': '9876549999'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        # Resend honours the session's pending_identifier
        session = self.client.session
        self.assertEqual(session['booking_auth_data']['identifier'], 'original@lamo.in')

    def test_10_otp_reuse(self):
        """10. OTP code cannot be reused once verified."""
        code = self._setup_session_otp('reuse_test@lamo.in', 'Reuse User', '9876540010')
        res1 = self.client.post(reverse('verify_otp'), {'action': 'verify', 'otp': code}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res1.status_code, 200)

        # Logging out to test unauthenticated endpoint reuse
        self.client.logout()
        res2 = self.client.post(reverse('verify_otp'), {'action': 'verify', 'otp': code}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res2.status_code, 400)

    def test_11_admin_password_login(self):
        """11. Admin password login on admin_login_view succeeds."""
        admin = CustomUser.objects.create_user('real_admin', 'admin@lamo.in', 'AdminPass123!', role='ADMIN')
        res = self.client.post(reverse('admin_login'), {'username': 'real_admin', 'password': 'AdminPass123!'})
        self.assertRedirects(res, reverse('admin_dashboard'))

    def test_12_admin_otp_login_rejection(self):
        """12. Admin accounts are prohibited from OTP login."""
        admin = CustomUser.objects.create_user('adm_no_otp', 'adm_no_otp@lamo.in', 'AdminPass123!', role='ADMIN')
        res = self.client.post(
            reverse('login'),
            {'action': 'send_login_otp', 'identifier': 'adm_no_otp@lamo.in'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(res.status_code, 200)
        d = res.json()
        self.assertFalse(d.get('success'))
        self.assertTrue(d.get('admin_password_required'))

    def test_13_ordinary_staff_admin_access_rejection(self):
        """13. Ordinary staff account is rejected from admin portal and never promoted."""
        staff = CustomUser.objects.create_user('staff_only', 'staff@lamo.in', 'StaffPass123!', role='USER')
        staff.is_staff = True
        staff.save()

        res = self.client.post(reverse('admin_login'), {'username': 'staff_only', 'password': 'StaffPass123!'})
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Access denied. This portal is restricted to system administrators.')
        staff.refresh_from_db()
        self.assertEqual(staff.role, 'USER')

        # Dashboard routing sends staff to home, not admin_dashboard
        self.client.force_login(staff)
        res_dash = self.client.get(reverse('dashboard'))
        self.assertRedirects(res_dash, reverse('home'))

    def test_14_superuser_admin_access(self):
        """14. Genuine superuser can sign in and role is synced to ADMIN."""
        super_u = CustomUser.objects.create_superuser('superuser1', 'sup1@lamo.in', 'SupPass123!')
        res = self.client.post(reverse('admin_login'), {'username': 'superuser1', 'password': 'SupPass123!'})
        self.assertRedirects(res, reverse('admin_dashboard'))
        super_u.refresh_from_db()
        self.assertEqual(super_u.role, 'ADMIN')

    def test_15_customer_password_login_rejection(self):
        """15. Customer password login is rejected under Policy B3."""
        cust = CustomUser.objects.create_user('cust_pw_rej', 'cust_rej@lamo.in', 'CustPass123!', role='USER')
        res = self.client.post(
            reverse('login'),
            {'action': 'login_password', 'identifier': 'cust_pw_rej', 'password': 'CustPass123!'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(res.status_code, 200)
        d = res.json()
        self.assertFalse(d.get('success'))
        self.assertTrue(d.get('otp_required'))

    def test_16_worker_password_login_rejection(self):
        """16. Worker password login is rejected under Policy B3."""
        w_acc = CustomUser.objects.create_user('work_pw_rej', 'work_rej@lamo.in', 'WorkPass123!', role='WORKER')
        res = self.client.post(
            reverse('login'),
            {'action': 'login_password', 'identifier': 'work_pw_rej', 'password': 'WorkPass123!'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(res.status_code, 200)
        d = res.json()
        self.assertFalse(d.get('success'))
        self.assertTrue(d.get('otp_required'))

    def test_17_customer_password_reset_rejection(self):
        """17. Customer password reset is prohibited under Policy B3."""
        cust = CustomUser.objects.create_user('cust_reset_rej', 'cust_rst@lamo.in', role='USER')
        res = self.client.post(reverse('forgot_password'), {'email': 'cust_rst@lamo.in'})
        self.assertEqual(res.status_code, 200)
        cust.refresh_from_db()
        self.assertIsNone(cust.password_reset_token)

    def test_18_worker_password_reset_rejection(self):
        """18. Worker password reset is prohibited under Policy B3."""
        worker_u = CustomUser.objects.create_user('w_reset_rej', 'w_rst@lamo.in', role='WORKER')
        res = self.client.post(reverse('forgot_password'), {'email': 'w_rst@lamo.in'})
        self.assertEqual(res.status_code, 200)
        worker_u.refresh_from_db()
        self.assertIsNone(worker_u.password_reset_token)

    def test_19_passwordless_worker_registration(self):
        """19. Worker registers with trade and KYC details without requiring a password."""
        from django.core.files.uploadedfile import SimpleUploadedFile
        pdf_bytes = VALID_PDF
        proof_file = SimpleUploadedFile("aadhaar_proof.pdf", pdf_bytes, content_type="application/pdf")

        res = self.client.post(reverse('register'), {
            'role': 'WORKER',
            'username': 'plumber_kumar',
            'email': 'kumar@plumber.in',
            'phone': '9876540019',
            'service': self.service.id,
            'experience_years': 5,
            'district': 'Ernakulam',
            'availability': 'Full Time',
            'pricing_type': 'hour',
            'pricing_amount': 350,
            'address': 'Marine Drive, Kochi, Ernakulam',
            'proof_type': 'aadhaar',
            'proof_document': proof_file,
            # No password or confirm_password provided!
        })
        self.assertRedirects(res, reverse('verify_otp'))
        w_created = CustomUser.objects.filter(username='plumber_kumar').first()
        self.assertIsNotNone(w_created)
        self.assertEqual(w_created.role, 'WORKER')
        self.assertFalse(w_created.has_usable_password())

    def test_20_passwordless_guest_account_creation(self):
        """20. Guest account auto-created after OTP verification has unusable password."""
        code = self._setup_session_otp('guest_pwdless@lamo.in', 'Passwordless Guest', '9876540020')
        res = self.client.post(reverse('verify_otp'), {'action': 'verify', 'otp': code}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)

        created_user = CustomUser.objects.filter(email='guest_pwdless@lamo.in').first()
        self.assertIsNotNone(created_user)
        self.assertFalse(created_user.has_usable_password())

    def test_21_duplicate_phone_across_customer_and_worker(self):
        """21. Duplicate phone validation prevents collisions across UserProfile and WorkerProfile."""
        from lamoapp.forms import UserProfileForm, WorkerProfileForm
        # Worker has phone '9876543000'
        # Customer tries to update profile to worker's phone
        c_user = CustomUser.objects.create_user('cust_phone_t', 'cpt@lamo.in', role='USER')
        c_prof = UserProfile.objects.create(user=c_user)

        form_c = UserProfileForm(data={'phone': '9876543000'}, instance=c_prof)
        self.assertFalse(form_c.is_valid())
        self.assertIn('phone', form_c.errors)
        self.assertIn('already registered with another account', form_c.errors['phone'][0])

        # Conversely, worker tries to take an existing customer's phone
        c_user2 = CustomUser.objects.create_user('cust_phone_t2', 'cpt2@lamo.in', role='USER')
        UserProfile.objects.create(user=c_user2, phone='9876540021')

        w_user2 = CustomUser.objects.create_user('w_phone_t2', 'wpt2@lamo.in', role='WORKER')
        w_prof2 = WorkerProfile.objects.create(user=w_user2, service=self.service, district='Ernakulam', work_status='AVAILABLE')

        form_w = WorkerProfileForm(data={
            'phone': '9876540021',
            'service': self.service.id,
            'district': 'Ernakulam',
            'availability': 'Full Time',
            'pricing_type': 'hour',
            'pricing_amount': 300,
            'experience_years': 4,
            'address': 'MG Road, Kochi'
        }, instance=w_prof2)
        self.assertFalse(form_w.is_valid())
        self.assertIn('phone', form_w.errors)
        self.assertIn('already registered with another account', form_w.errors['phone'][0])

    def test_22_concurrent_phone_updates(self):
        """22. UserProfileForm and WorkerProfileForm cleanly permit saving unchanged self phone."""
        from lamoapp.forms import UserProfileForm, WorkerProfileForm
        from django.core.files.uploadedfile import SimpleUploadedFile

        # 1. Customer saving unchanged self phone
        c_user = CustomUser.objects.create_user('cust_self', 'self@lamo.in', role='USER')
        c_prof = UserProfile.objects.create(user=c_user, phone='9876540022')
        form_c = UserProfileForm(data={'phone': '9876540022'}, instance=c_prof)
        self.assertTrue(form_c.is_valid(), form_c.errors)

        # 2. Worker saving unchanged self phone
        proof_file = SimpleUploadedFile("proof.pdf", VALID_PDF, content_type="application/pdf")
        form_w = WorkerProfileForm(data={
            'phone': self.worker.phone,
            'service': self.service.id,
            'district': 'Ernakulam',
            'availability': 'Full Time',
            'pricing_type': 'hour',
            'pricing_amount': 400,
            'experience_years': 3,
            'address': 'Kaloor Stadium, Kochi'
        }, files={'proof_document': proof_file}, instance=self.worker)
        self.assertTrue(form_w.is_valid(), form_w.errors)

    def test_23_booking_regression_all_fields_intact(self):
        """23. All original booking fields survive OTP verification and create booking accurately."""
        code = self._setup_session_otp('fieldcheck@lamo.in', 'Field Check User', '9876540023')
        res = self.client.post(reverse('verify_otp'), {'action': 'verify', 'otp': code}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)

        created_b = Booking.objects.filter(customer__email='fieldcheck@lamo.in').first()
        self.assertIsNotNone(created_b)
        self.assertEqual(created_b.service_id, self.service.id)
        self.assertEqual(created_b.worker_id, self.worker.id)
        self.assertEqual(created_b.district, 'Ernakulam')
        self.assertEqual(created_b.location, 'Kaloor, Kochi')
        self.assertEqual(created_b.booking_type, 'One Time')
        self.assertEqual(created_b.description, 'Fan repair in master bedroom')
        self.assertEqual(created_b.estimated_cost, self.service.price)
        self.assertEqual(created_b.payment_method, 'Cash')
        self.assertEqual(created_b.status, 'PENDING')
        self.assertEqual(created_b.phone, '9876540023')


class AuditP0P1ImplementationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.customer = CustomUser.objects.create_user(
            username='audit_cust_user',
            email='audit_cust@lamo.in',
            role='USER',
            is_email_verified=True
        )
        self.customer.last_login = timezone.now()
        self.customer.save()
        self.customer_profile = UserProfile.objects.create(
            user=self.customer,
            phone='9811111111'
        )

        self.service = Service.objects.create(
            name='AC Cleaning',
            price=Decimal('500.00'),
            is_active=True
        )

        self.worker_user1 = CustomUser.objects.create_user(
            username='audit_worker_user1',
            email='audit_worker1@lamo.in',
            role='WORKER',
            is_email_verified=True
        )
        self.worker_user1.last_login = timezone.now()
        self.worker_user1.save()
        self.worker1 = WorkerProfile.objects.create(
            user=self.worker_user1,
            service=self.service,
            phone='9822222221',
            district='Ernakulam',
            is_verified=True,
            work_status='AVAILABLE',
            pricing_type='fixed',
            pricing_amount=Decimal('500.00')
        )
        self.wallet1 = WorkerWallet.objects.create(worker=self.worker1, balance=Decimal('1000.00'))

        self.worker_user2 = CustomUser.objects.create_user(
            username='audit_worker_user2',
            email='audit_worker2@lamo.in',
            role='WORKER',
            is_email_verified=True
        )
        self.worker_user2.last_login = timezone.now()
        self.worker_user2.save()
        self.worker2 = WorkerProfile.objects.create(
            user=self.worker_user2,
            service=self.service,
            phone='9822222222',
            district='Ernakulam',
            is_verified=True,
            work_status='AVAILABLE',
            pricing_type='fixed',
            pricing_amount=Decimal('500.00')
        )
        self.wallet2 = WorkerWallet.objects.create(worker=self.worker2, balance=Decimal('1000.00'))

    def test_task1_confirm_payment_rejects_card_and_netbanking(self):
        """Task 1: confirm_payment rejects CARD and NETBANKING with clear guidance."""
        booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker1,
            service=self.service,
            status='COMPLETED',
            is_paid=False,
            final_amount=Decimal('500.00'),
            date=timezone.now().date(),
            time='10:00 AM'
        )
        self.client.force_login(self.customer)

        # CARD attempt
        res_card = self.client.post(
            reverse('confirm_payment', args=[booking.id]),
            {'payment_method': 'CARD'},
            follow=True
        )
        self.assertContains(res_card, 'Card and Netbanking payments are temporarily unavailable')
        booking.refresh_from_db()
        self.assertFalse(booking.is_paid)

        # NETBANKING attempt
        res_net = self.client.post(
            reverse('confirm_payment', args=[booking.id]),
            {'payment_method': 'NETBANKING'},
            follow=True
        )
        self.assertContains(res_net, 'Card and Netbanking payments are temporarily unavailable')
        booking.refresh_from_db()
        self.assertFalse(booking.is_paid)

    def test_task1_confirm_payment_accepts_upi_and_cash(self):
        """Task 1: confirm_payment allows UPI and Cash reporting."""
        from lamoapp.models import Payment
        booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker1,
            service=self.service,
            status='COMPLETED',
            is_paid=False,
            final_amount=Decimal('500.00'),
            date=timezone.now().date(),
            time='10:00 AM'
        )
        self.client.force_login(self.customer)

        res_upi = self.client.post(
            reverse('confirm_payment', args=[booking.id]),
            {'payment_method': 'UPI', 'payment_reference': 'UPI1234567890'},
            follow=True
        )
        self.assertContains(res_upi, 'Your payment has been reported')
        booking.refresh_from_db()
        self.assertEqual(booking.payment_method, 'UPI')
        self.assertTrue(Payment.objects.filter(booking=booking, payment_method='UPI').exists())

    def test_task2_recurring_booking_disabled_in_book_service(self):
        """Task 2: book_service rejects new recurring booking creation."""
        self.client.force_login(self.customer)
        res = self.client.post(
            reverse('book_service'),
            {
                'service': self.service.id,
                'worker': self.worker1.id,
                'booking_type': 'Recurring',
                'start_date': timezone.now().date().strftime('%Y-%m-%d'),
                'time': '10:00 AM',
                'frequency': 'Weekly',
                'location': 'Kaloor, Kochi',
                'phone': '9811111111'
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(res.status_code, 400)
        data = res.json()
        self.assertIn('Recurring bookings are temporarily unavailable', data.get('error_message', ''))
        self.assertEqual(BookingSeries.objects.count(), 0)

    def test_task2_existing_recurring_series_preserved(self):
        """Task 2: Existing recurring series and history remain fully accessible."""
        series = BookingSeries.objects.create(
            customer=self.customer,
            worker=self.worker1,
            service=self.service,
            frequency='Weekly',
            start_date=timezone.now().date(),
            end_date=timezone.now().date() + timedelta(days=30),
            time='10:00 AM',
            is_active=True
        )
        self.client.force_login(self.customer)
        res = self.client.get(reverse('my_series'))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, self.service.name)

    def test_task3_reassign_worker_cancelled_booking(self):
        """Task 3: customer_reassign_booking allows reassignment when cancelled_by == WORKER."""
        booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker1,
            service=self.service,
            district='Ernakulam',
            status='CANCELLED',
            cancelled_by='WORKER',
            cancellation_reason='Emergency leave',
            date=timezone.now().date() + timedelta(days=2),
            time='10:00 AM'
        )
        self.client.force_login(self.customer)
        res = self.client.post(reverse('customer_reassign_booking', args=[booking.id]))
        self.assertRedirects(res, reverse('my_bookings'))

        booking.refresh_from_db()
        self.assertEqual(booking.status, 'PENDING')
        self.assertEqual(booking.worker, self.worker2)
        self.assertEqual(booking.cancelled_by, '')

    def test_task3_reassign_customer_cancelled_booking_rejected(self):
        """Task 3: customer_reassign_booking rejects customer cancellations."""
        booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker1,
            service=self.service,
            district='Ernakulam',
            status='CANCELLED',
            cancelled_by='CUSTOMER',
            cancellation_reason='Change of plans',
            date=timezone.now().date() + timedelta(days=2),
            time='10:00 AM'
        )
        self.client.force_login(self.customer)
        res = self.client.post(reverse('customer_reassign_booking', args=[booking.id]))
        self.assertRedirects(res, reverse('my_bookings'))

        booking.refresh_from_db()
        self.assertEqual(booking.status, 'CANCELLED')
        self.assertEqual(booking.cancelled_by, 'CUSTOMER')

    def test_task4_my_bookings_pagination(self):
        """Task 4: my_bookings returns paginated Page object with 20 items per page."""
        for i in range(25):
            Booking.objects.create(
                customer=self.customer,
                worker=self.worker1,
                service=self.service,
                district='Ernakulam',
                status='PENDING',
                date=timezone.now().date() + timedelta(days=i + 1),
                time='10:00 AM'
            )
        self.client.force_login(self.customer)
        res = self.client.get(reverse('my_bookings'))
        self.assertEqual(res.status_code, 200)
        page_obj = res.context['bookings']
        self.assertEqual(len(page_obj), 20)
        self.assertTrue(page_obj.has_next())
        self.assertEqual(page_obj.paginator.num_pages, 2)

    def test_task4_worker_jobs_pagination(self):
        """Task 4: worker_jobs returns paginated Page object with 20 items per page."""
        for i in range(25):
            Booking.objects.create(
                customer=self.customer,
                worker=self.worker1,
                service=self.service,
                district='Ernakulam',
                status='ACCEPTED',
                date=timezone.now().date() + timedelta(days=i + 1),
                time='10:00 AM'
            )
        self.client.force_login(self.worker_user1)
        res = self.client.get(reverse('worker_jobs'))
        self.assertEqual(res.status_code, 200)
        page_obj = res.context['bookings']
        self.assertEqual(len(page_obj), 20)
        self.assertTrue(page_obj.has_next())
        self.assertEqual(page_obj.paginator.num_pages, 2)

    def test_task5_transit_status_prefetch_optimization(self):
        """Task 5: get_customer_booking_transit_status uses pre-fetched active jobs map."""
        from lamoapp.views import get_customer_booking_transit_status
        booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker1,
            service=self.service,
            status='ACCEPTED',
            date=timezone.now().date(),
            time='10:00 AM'
        )
        active_job = Booking.objects.create(
            customer=self.customer,
            worker=self.worker1,
            service=self.service,
            status='IN_PROGRESS',
            date=timezone.now().date(),
            time='09:00 AM'
        )
        active_jobs_map = {self.worker1.id: active_job}
        transit_info = get_customer_booking_transit_status(booking, active_jobs_by_worker=active_jobs_map)
        self.assertIsNotNone(transit_info)
        self.assertTrue(transit_info.get('is_busy'))
        self.assertEqual(transit_info.get('worker_name'), self.worker_user1.get_full_name() or self.worker_user1.username)

    def test_task6_step_up_phone_change_customer(self):
        """Task 6 (superseded by OTP): a phone change is never saved straight from the form.

        The number only changes after an OTP sent to the NEW number is verified;
        see lamoapp/tests_high_severity_fixes.py::PhoneChangeOtpTests for the full matrix.
        """
        self.client.force_login(self.customer)
        self.customer.last_login = timezone.now() - timedelta(minutes=5)  # recent login no longer enough
        self.customer.save()

        res = self.client.post(reverse('edit_profile'), {'phone': '9899999999'})
        self.assertRedirects(res, reverse('phone_change_verify'))
        self.customer_profile.refresh_from_db()
        self.assertEqual(self.customer_profile.phone, '9811111111')  # unchanged until OTP verified
