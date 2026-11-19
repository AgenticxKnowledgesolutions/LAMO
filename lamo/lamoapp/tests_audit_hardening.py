"""Regression tests for the audit fixes (CR3, CR4, H1-H7, N1-N9).

Each test reproduces the originally reported problem and checks the
corrected behaviour. Financial tests always check wallet balances.
"""
from decimal import Decimal
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.core.signing import TimestampSigner
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone

from lamoapp.models import (
    CustomUser, UserProfile, WorkerProfile, WorkerWallet, Service, Booking,
    Payment, WalletTransaction, Notification, WorkerLeave,
)


def make_user(username, role='USER', **extra):
    return CustomUser.objects.create_user(username, f'{username}@x.in', 'Pass123!x', role=role, **extra)


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.service = Service.objects.create(name='Plumbing', description='x', price=600, is_active=True)
        self.customer = make_user('h_cust')
        self.worker_user = make_user('h_wrk', role='WORKER', first_name='Wally')
        self.worker = WorkerProfile.objects.create(
            user=self.worker_user, service=self.service, district='Kollam',
            work_status='AVAILABLE', is_verified=True,
        )
        self.wallet = WorkerWallet.objects.create(worker=self.worker, balance=Decimal('500.00'))
        self.c = Client()
        self.c.force_login(self.customer)
        self.w = Client()
        self.w.force_login(self.worker_user)

    def booking(self, status='ACCEPTED', days=2, start_offset=None, **extra):
        from datetime import time as dt_time
        # Real bookings always carry start/end times (book_service sets them).
        b = Booking.objects.create(
            customer=self.customer, worker=self.worker, service=self.service,
            date=timezone.localdate() + timedelta(days=days), time='10:00 AM - 10:30 AM',
            start_time=dt_time(10, 0), end_time=dt_time(10, 30),
            location='Kollam', phone='9876543210', status=status,
            estimated_cost=Decimal('600.00'), district='Kollam', **extra,
        )
        if start_offset is not None:
            start = timezone.localtime() + start_offset
            Booking.objects.filter(pk=b.pk).update(
                date=start.date(), start_time=start.time(),
                end_time=(start + timedelta(minutes=30)).time(), time=start.strftime('%I:%M %p'),
            )
            b.refresh_from_db()
        return b

    def balance(self):
        return WorkerWallet.objects.get(pk=self.wallet.pk).balance


# ---------------------------------------------------------------- H1
class ArrivalPinBruteForceTests(Base):
    def test_pin_locks_after_five_wrong_attempts(self):
        b = self.booking(start_offset=timedelta(minutes=5))
        correct = b.arrival_otp
        wrong = [f'{i:04d}' for i in range(1000, 1020) if f'{i:04d}' != correct][:5]
        for pin in wrong:
            self.w.post(reverse('worker_start_job', args=[b.id]), {'arrival_otp': pin})
        # Even the correct PIN is refused while locked.
        self.w.post(reverse('worker_start_job', args=[b.id]), {'arrival_otp': correct})
        b.refresh_from_db()
        self.assertEqual(b.status, 'ACCEPTED')

    def test_correct_pin_still_works_before_lockout(self):
        b = self.booking(start_offset=timedelta(minutes=5))
        self.w.post(reverse('worker_start_job', args=[b.id]), {'arrival_otp': '0000' if b.arrival_otp != '0000' else '1111'})
        self.w.post(reverse('worker_start_job', args=[b.id]), {'arrival_otp': b.arrival_otp})
        b.refresh_from_db()
        self.assertEqual(b.status, 'IN_PROGRESS')

    def test_customer_qr_unaffected_by_pin_lockout(self):
        b = self.booking(start_offset=timedelta(minutes=5))
        for i in range(6):
            self.w.post(reverse('worker_start_job', args=[b.id]), {'arrival_otp': f'90{i:02d}'})
        self.c.post(reverse('verify_doorstep_qr', args=[b.get_arrival_token()]))
        b.refresh_from_db()
        self.assertEqual(b.status, 'IN_PROGRESS')


# ---------------------------------------------------------------- CR3
class StoredXssTests(Base):
    def test_admin_dashboard_escapes_worker_name_in_map_data(self):
        admin = make_user('h_admin', role='ADMIN', is_email_verified=True)
        self.worker_user.first_name = '</script><script>alert(1)</script>'
        self.worker_user.save()
        ac = Client()
        ac.force_login(admin)
        html = ac.get(reverse('admin_dashboard')).content.decode()
        self.assertNotIn('</script><script>alert(1)', html)
        self.assertIn('id="liveMapData"', html)

    def test_location_tree_is_json_script_escaped(self):
        html = self.c.get(reverse('home')).content.decode()
        self.assertIn('id="keralaLocationTreeData"', html)


# ---------------------------------------------------------------- CR4 / N1
class PhoneOtpTests(TestCase):
    def setUp(self):
        cache.clear()

    @override_settings(DEBUG=False, SMS_DEV_FALLBACK=False)
    def test_sms_without_provider_fails_in_production_and_never_logs_code(self):
        from lamoapp.views import send_otp_sms
        with self.assertLogs('lamoapp.views', level='WARNING') as logs:
            self.assertFalse(send_otp_sms('9876543210', '482913'))
        self.assertFalse(any('482913' in line for line in logs.output))

    @override_settings(DEBUG=False, SMS_DEV_FALLBACK=False)
    def test_phone_registration_is_refused_honestly_without_sms_provider(self):
        res = self.client.post(reverse('register'), {
            'action': 'send_customer_reg_otp', 'name': 'Asha', 'identifier': '9876543210',
        })
        data = res.json()
        self.assertFalse(data['success'])
        self.assertIn('email', data['error'].lower())
        self.assertNotIn('customer_reg_data', self.client.session)

    def test_phone_registration_sends_sms_when_available(self):
        with mock.patch('lamoapp.views.send_otp_sms', return_value=True) as sms:
            res = self.client.post(reverse('register'), {
                'action': 'send_customer_reg_otp', 'name': 'Asha', 'identifier': '9876543210',
            })
        self.assertTrue(res.json()['success'])
        self.assertTrue(sms.called)
        self.assertEqual(sms.call_args.args[0], '9876543210')

    def test_failed_sms_is_reported_not_faked(self):
        with mock.patch('lamoapp.views.send_otp_sms', return_value=False):
            res = self.client.post(reverse('register'), {
                'action': 'send_customer_reg_otp', 'name': 'Asha', 'identifier': '9876543210',
            })
        self.assertFalse(res.json()['success'])

    @override_settings(DEBUG=False, SMS_DEV_FALLBACK=False)
    def test_phone_only_login_reports_failure_without_sms(self):
        user = make_user('phone_only')
        user.email = ''
        user.save()
        UserProfile.objects.create(user=user, phone='9876500000')
        res = self.client.post(reverse('login'), {'action': 'send_login_otp', 'identifier': '9876500000'})
        self.assertFalse(res.json()['success'])

    @override_settings(OTP_SEND_PER_TARGET_MAX=2, OTP_SEND_PER_IP_MAX=100)
    def test_target_rate_limit_survives_cookie_reset(self):
        results = []
        for _ in range(3):
            client = Client()  # fresh session each time (cookie dropped)
            res = client.post(reverse('register'), {
                'action': 'send_customer_reg_otp', 'name': 'Bot', 'identifier': 'victim@example.com',
            })
            results.append(res.json()['success'])
        self.assertEqual(results, [True, True, False])


# ---------------------------------------------------------------- H7
class BillThenPaymentFlowTests(Base):
    def requested(self):
        return self.booking(status='COMPLETION_REQUESTED', final_amount=Decimal('900.00'))

    def test_bill_confirmation_is_not_payment(self):
        b = self.requested()
        self.c.post(reverse('confirm_completion', args=[b.id]), {'payment_method': 'CASH'})
        b.refresh_from_db()
        self.assertEqual(b.status, 'COMPLETION_REQUESTED')
        self.assertIsNotNone(b.bill_confirmed_at)
        self.assertFalse(b.is_paid)
        self.assertFalse(Payment.objects.filter(booking=b).exists())

    def test_payment_cannot_be_reported_before_bill_confirmation(self):
        for status in ('ACCEPTED', 'IN_PROGRESS', 'COMPLETION_REQUESTED'):
            b = self.booking(status=status)
            self.c.post(reverse('confirm_payment', args=[b.id]), {'payment_method': 'UPI', 'payment_reference': f'R-{status}'})
            self.assertFalse(Payment.objects.filter(booking=b).exists(), status)

    def test_payment_instructions_hidden_until_bill_confirmed(self):
        b = self.requested()
        html = self.c.get(reverse('my_bookings')).content.decode()
        self.assertNotIn(reverse('confirm_payment', args=[b.id]), html)
        self.c.post(reverse('confirm_completion', args=[b.id]))
        html = self.c.get(reverse('my_bookings')).content.decode()
        self.assertIn(reverse('confirm_payment', args=[b.id]), html)

    def test_full_flow_worker_verifies_payment(self):
        b = self.requested()
        # Worker cannot close the job before the customer confirms the bill.
        self.w.post(reverse('worker_confirm_payment_received', args=[b.id]))
        b.refresh_from_db()
        self.assertEqual(b.status, 'COMPLETION_REQUESTED')

        self.c.post(reverse('confirm_completion', args=[b.id]))
        self.c.post(reverse('confirm_payment', args=[b.id]), {'payment_method': 'UPI', 'payment_reference': 'UTR998877'})
        payment = Payment.objects.get(booking=b)
        self.assertEqual((payment.status, payment.amount, payment.payment_method), ('PENDING', Decimal('900.00'), 'UPI'))

        self.w.post(reverse('worker_confirm_payment_received', args=[b.id]), {'payment_method': 'UPI'})
        b.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(b.status, 'COMPLETED')
        self.assertTrue(b.is_paid)
        self.assertEqual(payment.status, 'PAID')
        # A second confirmation is harmless.
        self.w.post(reverse('worker_confirm_payment_received', args=[b.id]))
        self.assertEqual(Payment.objects.filter(booking=b).count(), 1)

    def test_admin_verification_completes_booking(self):
        admin = make_user('h_admin2', role='ADMIN', is_email_verified=True)
        b = self.requested()
        self.c.post(reverse('confirm_completion', args=[b.id]))
        self.c.post(reverse('confirm_payment', args=[b.id]), {'payment_method': 'CASH'})
        payment = Payment.objects.get(booking=b)
        ac = Client()
        ac.force_login(admin)
        ac.post(reverse('admin_confirm_payment', args=[payment.id]))
        b.refresh_from_db()
        self.assertEqual(b.status, 'COMPLETED')
        self.assertTrue(b.is_paid)

    def test_completion_does_not_change_wallet_for_prepaid_job(self):
        b = self.booking(status='PENDING')
        self.w.post(reverse('worker_accept_booking', args=[b.id]))
        self.assertEqual(self.balance(), Decimal('440.00'))
        Booking.objects.filter(pk=b.pk).update(status='COMPLETION_REQUESTED', final_amount=Decimal('5000.00'))
        self.c.post(reverse('confirm_completion', args=[b.id]), {'payment_method': 'ONLINE'})
        self.w.post(reverse('worker_confirm_payment_received', args=[b.id]))
        self.assertEqual(self.balance(), Decimal('440.00'))


# ---------------------------------------------------------------- H3
class CustomerRescheduleConflictTests(Base):
    def test_cannot_move_onto_workers_other_job(self):
        b1 = self.booking(days=2)
        b2 = self.booking(days=3)
        self.c.post(reverse('customer_request_reschedule', args=[b2.id]), {
            'date': b1.date.strftime('%Y-%m-%d'), 'time': '10:00 AM - 10:30 AM',
        })
        b2.refresh_from_db()
        self.assertNotEqual(b2.date, b1.date)

    def test_free_slot_reschedule_still_works(self):
        b = self.booking(days=2)
        new_date = timezone.localdate() + timedelta(days=5)
        self.c.post(reverse('customer_request_reschedule', args=[b.id]), {
            'date': new_date.strftime('%Y-%m-%d'), 'time': '02:00 PM - 02:30 PM',
        })
        b.refresh_from_db()
        self.assertEqual(b.date, new_date)

    def test_cannot_move_onto_worker_leave_day(self):
        b = self.booking(days=2)
        leave_day = timezone.localdate() + timedelta(days=6)
        WorkerLeave.objects.create(worker=self.worker, leave_date=leave_day, reason='x')
        self.c.post(reverse('customer_request_reschedule', args=[b.id]), {
            'date': leave_day.strftime('%Y-%m-%d'), 'time': '02:00 PM - 02:30 PM',
        })
        b.refresh_from_db()
        self.assertNotEqual(b.date, leave_day)


# ---------------------------------------------------------------- H2 / N7
class WalletEligibilityAndOnDutyTests(Base):
    def test_worker_who_cannot_afford_commission_is_not_bookable(self):
        from lamoapp.views import get_worker_unavailability_reasons
        self.wallet.balance = Decimal('5.00')
        self.wallet.save()
        self.worker.refresh_from_db()
        self.assertTrue(get_worker_unavailability_reasons(self.worker))
        res = self.c.post(reverse('book_service'), {
            'worker': self.worker.id, 'service': self.service.id,
            'date': (timezone.localdate() + timedelta(days=3)).strftime('%Y-%m-%d'),
            'time': '10:00 AM - 10:30 AM', 'location': 'Kollam town', 'phone': '9876543210',
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 400)
        self.assertFalse(Booking.objects.filter(worker=self.worker).exists())

    def test_launch_period_worker_with_empty_wallet_is_bookable(self):
        from lamoapp.views import get_worker_unavailability_reasons
        self.wallet.balance = Decimal('0.00')
        self.wallet.save()
        self.worker.launch_ends_at = timezone.now() + timedelta(days=10)
        self.worker.save()
        self.worker.refresh_from_db()
        self.assertEqual(get_worker_unavailability_reasons(self.worker), [])

    def _book(self, days, time='10:00 AM - 10:30 AM'):
        return self.c.post(reverse('book_service'), {
            'worker': self.worker.id, 'service': self.service.id,
            'date': (timezone.localdate() + timedelta(days=days)).strftime('%Y-%m-%d'),
            'time': time, 'location': 'Kollam town', 'phone': '9876543210',
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def test_on_duty_worker_bookable_for_later_dates_only(self):
        self.worker.work_status = 'ON_DUTY'
        self.worker.save()
        today = self._book(days=0, time='05:00 PM - 05:30 PM')
        self.assertEqual(today.status_code, 400)
        self.assertIn('working on a job right now', today.json()['error_message'])
        self.assertEqual(self._book(days=2).status_code, 200)


# ---------------------------------------------------------------- H6
class LifecycleAutomationTests(Base):
    def test_accept_view_uses_same_window_as_sweep(self):
        b = self.booking(status='PENDING', days=0)
        Booking.objects.filter(pk=b.pk).update(created_at=timezone.now() - timedelta(minutes=25))
        self.w.post(reverse('worker_accept_booking', args=[b.id]))
        b.refresh_from_db()
        self.assertEqual(b.status, 'EXPIRED')  # 20-minute same-day window
        self.assertEqual(self.balance(), Decimal('500.00'))

    def test_open_worker_proposal_is_not_expired_by_the_sweep(self):
        from lamoapp.views import check_and_expire_pending_bookings
        b = self.booking(status='PENDING', days=3)
        Booking.objects.filter(pk=b.pk).update(
            created_at=timezone.now() - timedelta(hours=5),
            proposed_date=timezone.localdate() + timedelta(days=4), proposed_time='11:00 AM - 11:30 AM',
            reschedule_proposed_at=timezone.now() - timedelta(hours=1),
        )
        check_and_expire_pending_bookings(booking=b, _run_maintenance=False)
        b.refresh_from_db()
        self.assertEqual(b.status, 'PENDING')

    def test_maintenance_command_alerts_stuck_jobs_once(self):
        admin = make_user('h_admin3', role='ADMIN', is_email_verified=True)
        b = self.booking(status='IN_PROGRESS', arrival_verified_at=timezone.now() - timedelta(hours=20))
        call_command('process_booking_reminders', verbosity=0)
        call_command('process_booking_reminders', verbosity=0)
        self.assertEqual(Notification.objects.filter(recipient=admin, booking=b, message__startswith='Stuck Job:').count(), 1)
        b.refresh_from_db()
        self.assertEqual(b.status, 'IN_PROGRESS')  # no automatic money/status change


# ---------------------------------------------------------------- N4
class DoorstepQrTokenTests(Base):
    def test_token_expires_after_ten_minutes(self):
        b = self.booking(start_offset=timedelta(minutes=5))
        with mock.patch('django.core.signing.time.time', return_value=timezone.now().timestamp() - 11 * 60):
            old_token = b.get_arrival_token()
        self.assertEqual(Booking.verify_arrival_token(old_token), (None, None))
        self.c.post(reverse('verify_doorstep_qr', args=[old_token]))
        b.refresh_from_db()
        self.assertEqual(b.status, 'ACCEPTED')

    def test_token_cannot_be_used_twice(self):
        b = self.booking(start_offset=timedelta(minutes=5))
        token = b.get_arrival_token()
        self.c.post(reverse('verify_doorstep_qr', args=[token]))
        b.refresh_from_db()
        first_verified = b.arrival_verified_at
        res = self.c.post(reverse('verify_doorstep_qr', args=[token]), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertTrue(res.json().get('already_active'))
        b.refresh_from_db()
        self.assertEqual(b.arrival_verified_at, first_verified)

    def test_qr_only_issued_on_job_day(self):
        future = self.booking(days=3)
        self.assertEqual(self.w.get(reverse('worker_booking_doorstep_qr', args=[future.id])).status_code, 403)


# ---------------------------------------------------------------- N8 / N9
class AccountSecurityTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_worker_with_password_can_change_it(self):
        worker = make_user('pw_worker', role='WORKER')
        self.client.force_login(worker)
        self.assertEqual(self.client.get(reverse('change_password')).status_code, 200)

    def test_passwordless_deletion_requires_recent_sign_in(self):
        user = CustomUser.objects.create(username='old_session', email='old@x.in', role='USER')
        user.set_unusable_password()
        user.save()
        self.client.force_login(user)
        CustomUser.objects.filter(pk=user.pk).update(last_login=timezone.now() - timedelta(hours=3))
        self.client.post(reverse('delete_account'), {'confirm_delete': 'DELETE'})
        user.refresh_from_db()
        self.assertTrue(user.is_active)

    def test_magic_login_link_is_single_use(self):
        from lamoapp.views import _magic_login_marker
        service = Service.objects.create(name='Magic', description='x', price=100, is_active=True)
        wu = make_user('magic_w', role='WORKER')
        WorkerProfile.objects.create(user=wu, service=service, district='Kollam', is_verified=True)
        token = TimestampSigner(salt='worker-magic-login').sign(f'{wu.id}:{_magic_login_marker(wu)}')
        first = self.client.get(reverse('worker_magic_login', args=[token]))
        self.assertRedirects(first, reverse('worker_dashboard'), fetch_redirect_response=False)
        self.client.logout()
        second = self.client.get(reverse('worker_magic_login', args=[token]))
        self.assertRedirects(second, reverse('login'), fetch_redirect_response=False)
        self.assertNotIn('_auth_user_id', self.client.session)


# ---------------------------------------------------------------- N2 / N3 / extras
class ServiceAdminTests(TestCase):
    def setUp(self):
        self.admin = make_user('svc_admin', role='ADMIN', is_email_verified=True)
        self.service = Service.objects.create(name='Painting', description='walls', price=800, is_active=True)

    def test_editing_service_keeps_price_and_saves_commission(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('service_update', args=[self.service.id]), {
            'name': 'Painting', 'description': 'walls', 'price': '800.00', 'is_active': 'on',
            'commission_type': 'FIXED', 'commission_value': '40',
        })
        self.service.refresh_from_db()
        self.assertEqual(self.service.price, Decimal('800.00'))
        self.assertEqual((self.service.commission_type, self.service.commission_value), ('FIXED', Decimal('40.00')))

    def test_service_form_renders_price_field(self):
        self.client.force_login(self.admin)
        html = self.client.get(reverse('service_update', args=[self.service.id])).content.decode()
        self.assertIn('name="price"', html)
        self.assertIn('name="commission_type"', html)

    def test_invalid_percentage_rejected(self):
        from lamoapp.forms import ServiceForm
        form = ServiceForm(data={'name': 'X', 'description': 'y', 'commission_type': 'PERCENT', 'commission_value': '150'})
        self.assertFalse(form.is_valid())

    def test_plain_staff_is_not_promoted_to_admin(self):
        staff = make_user('just_staff', role='USER', is_staff=True)
        self.client.force_login(staff)
        self.client.get(reverse('service_update', args=[self.service.id]))
        self.client.get(reverse('admin_dashboard'))
        staff.refresh_from_db()
        self.assertEqual(staff.role, 'USER')

    def test_transaction_labels_cover_all_written_types(self):
        labels = dict(WalletTransaction.TRANSACTION_TYPES)
        for code in ('FEE_DEDUCTION', 'REFUND', 'RECHARGE', 'RECHARGE_PENDING', 'RECHARGE_REJECTED'):
            self.assertIn(code, labels)
