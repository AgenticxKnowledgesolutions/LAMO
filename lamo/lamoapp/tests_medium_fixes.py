"""Regression tests for medium-priority bugs 13-26.

13 payment-method validation            19 dispute resolution vs payment state
14 reassignment eligibility             20 is_staff must not grant tracking access
15 completion race / duplicates         21 predictable /admin/ URL
16 recharge amount validation           22 rate limiter must not fail open
17 UTR uniqueness / duplicate credit    23 OTP attempt counter survives new sessions
18 pending recharge vs ledger           24-26 query counts / caching / input limits

All tests use the in-memory test database (lamo.settings_test).
"""
import hashlib
import importlib
import os
import time as time_module
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import Client, RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import Resolver404, clear_url_caches, resolve, reverse
from django.utils import timezone

from lamoapp import rate_limit, views
from lamoapp.models import (
    Booking, Complaint, CustomUser, Notification, Payment, Review, Service,
    UserProfile, WalletTransaction, WorkerLeave, WorkerProfile, WorkerWallet,
)
from lamoapp.tests_high_severity_fixes import _Base, _future


class BrokenCache:
    """A cache backend that is down: every operation raises."""

    def _boom(self, *a, **k):
        raise ConnectionError('cache is down')

    get = set = incr = delete = add = get_many = set_many = _boom


# ===========================================================================
# BUG 13 - payment method validation
# ===========================================================================
class PaymentMethodValidationTests(_Base):

    def awaiting_payment(self):
        b = self.accepted_in_progress()
        self.bill(b, labor_amount='800')
        self.c.post(reverse('confirm_completion', args=[b.id]))
        b.refresh_from_db()
        return b

    def test_customer_payment_report_rejects_everything_unsupported(self):
        b = self.awaiting_payment()
        for method in ('CARD', 'NETBANKING', 'card', 'BITCOIN', 'ONLINE', 'upi;cash', '', '   '):
            with self.subTest(method=method):
                self.c.post(reverse('confirm_payment', args=[b.id]), {'payment_method': method})
                self.assertFalse(Payment.objects.filter(booking=b).exists())   # nothing recorded, nothing converted to UPI

    def test_customer_payment_report_shows_a_clear_message(self):
        b = self.awaiting_payment()
        res = self.c.post(reverse('confirm_payment', args=[b.id]), {'payment_method': 'BITCOIN'}, follow=True)
        self.assertContains(res, 'Unsupported payment method')
        res = self.c.post(reverse('confirm_payment', args=[b.id]), {'payment_method': 'CARD'}, follow=True)
        self.assertContains(res, 'Card and Netbanking payments are temporarily unavailable')

    def test_valid_upi_and_cash_still_work(self):
        for method in ('UPI', 'CASH', 'upi', ' cash '):
            with self.subTest(method=method):
                b = self.awaiting_payment()
                self.c.post(reverse('confirm_payment', args=[b.id]), {'payment_method': method})
                payment = Payment.objects.get(booking=b)
                self.assertEqual(payment.payment_method, method.strip().upper())
                self.assertEqual(payment.status, 'PENDING')

    def _book(self, **extra):
        data = {'worker': self.worker.id, 'service': self.service.id, 'booking_type': 'One Time',
                'date': _future(5), 'time': '10:00-12:00', 'location': 'Kollam', 'phone': '9876543210'}
        data.update(extra)
        return self.c.post(reverse('book_service'), data, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def test_booking_form_rejects_unsupported_preferences(self):
        for method in ('CARD', 'NETBANKING', 'BITCOIN', 'upi;x'):
            with self.subTest(method=method):
                res = self._book(payment_method=method)
                self.assertEqual(res.status_code, 400)
                self.assertEqual(Booking.objects.filter(customer=self.customer).count(), 0)

    def test_booking_form_accepts_supported_preferences_and_blank_defaults_to_cash(self):
        for sent, stored, day in (('CASH', 'CASH', 5), ('cash', 'CASH', 6), ('ONLINE', 'ONLINE', 7), ('', 'CASH', 8)):
            with self.subTest(sent=sent):
                res = self._book(payment_method=sent, date=_future(day))
                self.assertEqual(res.status_code, 200, res.content)
                self.assertEqual(Booking.objects.filter(customer=self.customer).latest('id').payment_method, stored)

    def test_guest_booking_path_rejects_unsupported_method_before_sending_any_code(self):
        guest = Client()
        res = guest.post(reverse('book_service'), {
            'worker': self.worker.id, 'service': self.service.id, 'booking_type': 'One Time',
            'date': _future(5), 'time': '10:00-12:00', 'location': 'Kollam', 'phone': '9876543210',
            'customer_name': 'Guest', 'customer_email': 'guest@x.in', 'payment_method': 'CARD',
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 400)
        self.assertNotIn('booking_auth_data', guest.session)
        self.assertNotIn('pending_booking_data', guest.session)


# ===========================================================================
# BUG 14 - reassignment eligibility
# ===========================================================================
class ReassignmentEligibilityTests(_Base):

    def setUp(self):
        super().setUp()
        self.cancelled = Booking.objects.create(
            customer=self.customer, worker=self.worker, service=self.service, status='CANCELLED',
            cancelled_by='WORKER', date=timezone.localdate() + timedelta(days=3), time='10:00 AM - 10:30 AM',
            start_time=timezone.datetime(2000, 1, 1, 10, 0).time(), end_time=timezone.datetime(2000, 1, 1, 10, 30).time(),
            location='Kollam', phone='9876543210', estimated_cost=Decimal('500.00'), district='Kollam')
        self._n = 0

    def candidate(self, balance='1000.00', wallet=True, **kw):
        self._n += 1
        user = CustomUser.objects.create_user(f'ra_w{self._n}', f'ra{self._n}@x.in', 'Pass123!x', role='WORKER')
        defaults = dict(user=user, service=self.service, district='Kollam', work_status='AVAILABLE',
                        is_verified=True, phone=f'98700000{self._n:02d}', experience_years=5)
        defaults.update(kw)
        worker = WorkerProfile.objects.create(**defaults)
        if wallet:
            WorkerWallet.objects.create(worker=worker, balance=Decimal(balance))
        return worker

    def reassign(self):
        res = self.c.post(reverse('customer_reassign_booking', args=[self.cancelled.id]))
        self.cancelled.refresh_from_db()
        return res

    def assertNotReassigned(self):
        self.assertEqual(self.cancelled.status, 'CANCELLED')
        self.assertEqual(self.cancelled.worker_id, self.worker.id)

    def test_valid_reassignment_still_works(self):
        good = self.candidate()
        self.reassign()
        self.assertEqual((self.cancelled.status, self.cancelled.worker_id), ('PENDING', good.id))

    def test_worker_paused_for_dues_is_never_assigned(self):
        self.candidate(is_paused_for_dues=True)
        self.reassign()
        self.assertNotReassigned()

    def test_worker_failing_wallet_rules_is_never_assigned(self):
        self.candidate(balance='10.00')           # fee for a Rs 500 booking is Rs 50
        self.candidate(balance='-5.00')           # locked wallet
        self.candidate(wallet=False)              # no wallet at all
        self.reassign()
        self.assertNotReassigned()

    def test_unverified_suspended_or_unavailable_workers_are_skipped(self):
        self.candidate(is_verified=False)
        self.candidate(work_status='SUSPENDED')
        self.candidate(work_status='ON_DUTY')
        self.reassign()
        self.assertNotReassigned()

    def test_worker_on_leave_is_skipped(self):
        leave_worker = self.candidate()
        WorkerLeave.objects.create(worker=leave_worker, leave_date=self.cancelled.date)
        self.reassign()
        self.assertNotReassigned()

    def test_conflicting_bookings_block_assignment_including_pending_ones(self):
        for status in ('ACCEPTED', 'PENDING', 'IN_PROGRESS'):
            with self.subTest(status=status):
                busy = self.candidate()
                Booking.objects.create(
                    customer=self.customer, worker=busy, service=self.service, status=status,
                    date=self.cancelled.date, time='10:00 AM - 10:30 AM',
                    start_time=self.cancelled.start_time, end_time=self.cancelled.end_time,
                    location='Kollam', phone='9876543210')
                self.reassign()
                self.assertNotReassigned()

    def test_past_date_is_rejected_even_with_an_eligible_worker(self):
        self.candidate()
        Booking.objects.filter(pk=self.cancelled.pk).update(date=timezone.localdate() - timedelta(days=1))
        self.reassign()
        self.assertNotReassigned()

    def test_time_already_passed_today_is_rejected(self):
        self.candidate()
        past = timezone.localtime() - timedelta(hours=2)
        Booking.objects.filter(pk=self.cancelled.pk).update(
            date=past.date(), start_time=past.time(), end_time=(past + timedelta(minutes=30)).time())
        if past.date() != timezone.localdate():      # just after midnight: the date check already covers it
            return
        self.reassign()
        self.assertNotReassigned()

    def test_next_eligible_worker_is_used_when_the_best_one_is_not_eligible(self):
        paused_best = self.candidate(is_paused_for_dues=True)
        broke = self.candidate(balance='1.00')
        good = self.candidate()
        self.reassign()
        self.assertEqual(self.cancelled.worker_id, good.id)
        self.assertNotIn(self.cancelled.worker_id, (paused_best.id, broke.id))

    def test_eligibility_is_rechecked_under_lock_just_before_assignment(self):
        victim = self.candidate()
        calls = {'n': 0}
        real = views._booking_scheduled_start

        def hook(booking):
            # 1st call: pre-check. 2nd call runs inside the transaction, AFTER the
            # candidate list was built: the worker gets paused in between.
            calls['n'] += 1
            if calls['n'] == 2:
                WorkerProfile.objects.filter(pk=victim.pk).update(is_paused_for_dues=True)
            return real(booking)

        with mock.patch.object(views, '_booking_scheduled_start', side_effect=hook):
            self.reassign()
        self.assertNotReassigned()

    def test_customer_cancelled_booking_still_cannot_be_reassigned(self):
        self.candidate()
        Booking.objects.filter(pk=self.cancelled.pk).update(cancelled_by='CUSTOMER')
        self.reassign()
        self.assertEqual(self.cancelled.status, 'CANCELLED')


# ===========================================================================
# BUG 15 - completion race / duplicates
# ===========================================================================
class CompletionRaceTests(_Base):

    def request_completion(self, booking, **post):
        return self.w.post(reverse('worker_request_completion', args=[booking.id]), post or {'labor_amount': '800'})

    def test_duplicate_requests_transition_once_and_notify_once(self):
        b = self.accepted_in_progress()
        with mock.patch('lamoapp.views.send_email_safely') as mail:
            self.request_completion(b)
            first_updated = Booking.objects.get(pk=b.pk).updated_at
            self.request_completion(b, labor_amount='5000')       # replayed/duplicate with another amount
        b.refresh_from_db()
        self.assertEqual(b.status, 'COMPLETION_REQUESTED')
        self.assertEqual(b.final_amount, Decimal('800.00'))       # the replay did not overwrite the bill
        self.assertEqual(b.updated_at, first_updated)
        self.assertEqual(mail.call_count, 1)
        self.assertEqual(Notification.objects.filter(recipient=self.customer, booking=b, message__icontains='final amount').count(), 1)

    def test_invalid_states_are_refused(self):
        b = self.accepted_in_progress()
        for status in ('PENDING', 'ACCEPTED', 'COMPLETION_REQUESTED', 'COMPLETED', 'CANCELLED', 'DISPUTED'):
            with self.subTest(status=status):
                Booking.objects.filter(pk=b.pk).update(status=status, final_amount=None)
                self.request_completion(b)
                b.refresh_from_db()
                self.assertEqual(b.status, status)
                self.assertIsNone(b.final_amount)

    def test_request_that_loses_the_race_changes_nothing(self):
        """Another request completes the job between this request's read and its write."""
        b = self.accepted_in_progress()
        real = views._parse_final_bill

        def other_request_wins(*args, **kwargs):
            Booking.objects.filter(pk=b.pk).update(
                status='COMPLETION_REQUESTED', final_amount=Decimal('111.00'))
            return real(*args, **kwargs)

        with mock.patch('lamoapp.views.send_email_safely') as mail, \
                mock.patch.object(views, '_parse_final_bill', side_effect=other_request_wins):
            self.request_completion(b, labor_amount='900')
        b.refresh_from_db()
        self.assertEqual(b.final_amount, Decimal('111.00'))                    # the winner's bill stands
        self.assertEqual(mail.call_count, 0)
        self.assertFalse(Notification.objects.filter(recipient=self.customer, booking=b, message__icontains='final amount').exists())

    def test_no_payment_or_settlement_is_created_by_completion_requests(self):
        b = self.accepted_in_progress()
        ledger_before = WalletTransaction.objects.filter(booking=b).count()
        balance_before = self.balance()
        self.request_completion(b)
        self.request_completion(b)
        self.assertFalse(Payment.objects.filter(booking=b).exists())
        self.assertEqual(WalletTransaction.objects.filter(booking=b).count(), ledger_before)
        self.assertEqual(self.balance(), balance_before)

    def test_customer_confirmation_is_still_required_before_payment(self):
        b = self.accepted_in_progress()
        self.request_completion(b)
        self.w.post(reverse('worker_confirm_payment_received', args=[b.id]), {'payment_method': 'CASH'})
        b.refresh_from_db()
        self.assertEqual(b.status, 'COMPLETION_REQUESTED')
        self.assertFalse(b.is_paid)


# ===========================================================================
# BUGS 16-18 - wallet recharge, UTR, ledger
# ===========================================================================
class WalletRechargeTests(_Base):

    def recharge(self, amount, utr='UTR000111222', client=None):
        res = (client or self.w).post(reverse('worker_recharge_wallet'), {'amount': amount, 'reference_id': utr})
        return res

    def pending(self):
        return WalletTransaction.objects.filter(transaction_type='RECHARGE_PENDING')

    def make_admin(self):
        admin = CustomUser.objects.create_user('wr_admin', 'wr@x.in', 'Pass123!x', role='ADMIN')
        client = Client()
        client.force_login(admin)
        return client

    # ---- bug 16 -----------------------------------------------------------
    def test_invalid_amounts_are_rejected_and_create_nothing(self):
        for bad in ('Infinity', '-Infinity', 'inf', 'NaN', 'nan', '-100', '0', '0.00', '100.555', '100.001',
                    'abc', '', '1e5', '1,00.5.5', '99999999999999', '9', '5.00', '+100'):
            with self.subTest(amount=bad):
                self.recharge(bad, utr=f'UTR{abs(hash(bad)) % 10**9:09d}')
                self.assertEqual(self.pending().count(), 0)
        self.assertEqual(self.balance(), Decimal('1000.00'))

    def test_valid_amounts_are_accepted_as_pending_only(self):
        for amount, utr in (('100', 'UTR100000001'), ('100.5', 'UTR100000002'), ('100.50', 'UTR100000003'), ('10', 'UTR100000004')):
            with self.subTest(amount=amount):
                self.recharge(amount, utr=utr)
                self.assertTrue(self.pending().filter(reference_id=utr).exists())
        self.assertEqual(self.pending().get(reference_id='UTR100000003').amount, Decimal('100.50'))
        self.assertEqual(self.balance(), Decimal('1000.00'))        # nothing credited by a submission

    # ---- bug 17 -----------------------------------------------------------
    def test_same_utr_cannot_be_submitted_twice_while_pending(self):
        self.recharge('100')
        self.recharge('100')
        self.assertEqual(self.pending().filter(reference_id='UTR000111222').count(), 1)

    def test_rejected_utr_is_released_for_the_genuine_payer(self):
        admin = self.make_admin()
        self.recharge('250', utr='UTR777000111')
        tx = self.pending().get()
        admin.post(reverse('admin_reject_wallet_recharge', args=[tx.id]), {'rejection_reason': 'fraudulent claim'})
        tx.refresh_from_db()
        self.assertEqual(tx.transaction_type, 'RECHARGE_REJECTED')
        self.assertIsNone(tx.reference_id)
        self.assertIn('UTR777000111', tx.notes)                    # audit trail kept
        # the genuine payer (another worker) can now use the same UTR
        other_user = CustomUser.objects.create_user('real_w', 'rw@x.in', 'Pass123!x', role='WORKER')
        other = WorkerProfile.objects.create(user=other_user, service=self.service, district='Kollam', is_verified=True, phone='9877777777')
        WorkerWallet.objects.create(worker=other, balance=Decimal('0.00'))
        other_client = Client()
        other_client.force_login(other_user)
        self.recharge('250', utr='UTR777000111', client=other_client)
        self.assertTrue(self.pending().filter(reference_id='UTR777000111', wallet__worker=other).exists())

    def test_legacy_rejected_row_holding_a_utr_is_released_on_resubmission(self):
        WalletTransaction.objects.create(
            wallet=self.wallet, transaction_type='RECHARGE_REJECTED', amount=Decimal('100.00'),
            balance_after=Decimal('1000.00'), reference_id='UTR888000222', notes='old rejection')
        self.recharge('100', utr='UTR888000222')
        self.assertEqual(WalletTransaction.objects.filter(reference_id='UTR888000222', transaction_type='RECHARGE_PENDING').count(), 1)
        old = WalletTransaction.objects.get(transaction_type='RECHARGE_REJECTED')
        self.assertIsNone(old.reference_id)
        self.assertIn('old rejection', old.notes)

    def test_approval_credits_exactly_once_and_repeat_approval_is_a_no_op(self):
        admin = self.make_admin()
        self.recharge('300', utr='UTR999000333')
        tx = self.pending().get()
        for _ in range(3):
            admin.post(reverse('admin_approve_wallet_recharge', args=[tx.id]))
        self.assertEqual(self.balance(), Decimal('1300.00'))
        self.assertEqual(WalletTransaction.objects.filter(transaction_type='RECHARGE').count(), 1)

    def test_a_credited_utr_can_never_be_used_again(self):
        admin = self.make_admin()
        self.recharge('300', utr='UTR999000444')
        admin.post(reverse('admin_approve_wallet_recharge', args=[self.pending().get().id]))
        self.recharge('300', utr='UTR999000444')
        self.assertEqual(WalletTransaction.objects.filter(reference_id='UTR999000444').count(), 1)
        self.assertEqual(self.balance(), Decimal('1300.00'))

    def test_rejected_transaction_cannot_be_approved_afterwards(self):
        admin = self.make_admin()
        self.recharge('300', utr='UTR999000555')
        tx = self.pending().get()
        admin.post(reverse('admin_reject_wallet_recharge', args=[tx.id]), {'rejection_reason': 'no match'})
        res = admin.post(reverse('admin_approve_wallet_recharge', args=[tx.id]))
        self.assertEqual(res.status_code, 404)
        self.assertEqual(self.balance(), Decimal('1000.00'))

    # ---- bug 18 -----------------------------------------------------------
    def test_pending_and_rejected_recharges_are_not_available_funds(self):
        WorkerWallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal('0.00'))
        self.recharge('500', utr='UTR111000111')
        self.recharge('700', utr='UTR111000222')
        rejected = self.pending().get(reference_id='UTR111000222')
        rejected.transaction_type = 'RECHARGE_REJECTED'
        rejected.save()
        wallet = WorkerWallet.objects.get(pk=self.wallet.pk)
        self.assertEqual(wallet.balance, Decimal('0.00'))
        self.assertEqual(wallet.ledger_total(), Decimal('0.00'))              # pending/rejected excluded
        self.assertEqual(wallet.pending_recharge_total(), Decimal('500.00'))

    def test_ledger_total_matches_balance_after_a_credited_recharge_and_a_commission(self):
        WorkerWallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal('0.00'))
        admin = self.make_admin()
        self.recharge('400', utr='UTR222000111')
        self.recharge('900', utr='UTR222000222')                                # stays pending
        admin.post(reverse('admin_approve_wallet_recharge', args=[self.pending().get(reference_id='UTR222000111').id]))
        b = Booking.objects.create(
            customer=self.customer, worker=self.worker, service=self.service, status='PENDING',
            date=timezone.localdate() + timedelta(days=2), time='10:00 AM - 10:30 AM', location='Kollam',
            phone='9876543210', estimated_cost=Decimal('500.00'), district='Kollam')
        self.w.post(reverse('worker_accept_booking', args=[b.id]))
        wallet = WorkerWallet.objects.get(pk=self.wallet.pk)
        self.assertEqual(wallet.balance, Decimal('350.00'))                      # 400 credited - 50 commission
        self.assertEqual(wallet.ledger_total(), wallet.balance)
        self.assertEqual(wallet.pending_recharge_total(), Decimal('900.00'))

    def test_pending_recharge_cannot_be_used_to_accept_a_booking(self):
        WorkerWallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal('0.00'))
        self.recharge('5000', utr='UTR333000111')
        b = Booking.objects.create(
            customer=self.customer, worker=self.worker, service=self.service, status='PENDING',
            date=timezone.localdate() + timedelta(days=2), time='10:00 AM - 10:30 AM', location='Kollam',
            phone='9876543210', estimated_cost=Decimal('500.00'), district='Kollam')
        self.w.post(reverse('worker_accept_booking', args=[b.id]))
        b.refresh_from_db()
        self.assertEqual(b.status, 'PENDING')


# ===========================================================================
# BUG 19 - dispute resolution vs payment state
# ===========================================================================
class DisputeResolutionTests(_Base):

    def setUp(self):
        super().setUp()
        self.admin = CustomUser.objects.create_user('dr_admin', 'dr@x.in', 'Pass123!x', role='ADMIN')
        self.a = Client()
        self.a.force_login(self.admin)

    def disputed(self, labor='800'):
        b = self.accepted_in_progress()
        self.bill(b, labor_amount=labor)
        self.c.post(reverse('dispute_booking', args=[b.id]), {'dispute_reason': 'not done properly'})
        b.refresh_from_db()
        self.assertEqual(b.status, 'DISPUTED')
        return b, Complaint.objects.get(booking=b)

    def resolve(self, complaint, **extra):
        data = {'status': 'RESOLVED', 'admin_remark': 'Reviewed with both parties'}
        data.update(extra)
        return self.a.post(reverse('update_complaint_status', args=[complaint.id]), data)

    def test_completed_without_payment_evidence_is_not_marked_paid(self):
        b, complaint = self.disputed()
        self.resolve(complaint)
        b.refresh_from_db()
        complaint.refresh_from_db()
        self.assertEqual(b.status, 'COMPLETED')
        self.assertFalse(b.is_paid)
        self.assertFalse(Payment.objects.filter(booking=b, status='PAID').exists())
        self.assertIn('payment NOT confirmed', complaint.admin_remark)           # audit record

    def test_completed_with_verified_payment_updates_the_right_records_once(self):
        b, complaint = self.disputed()
        fee_rows_before = WalletTransaction.objects.filter(booking=b, transaction_type='FEE_DEDUCTION').count()
        self.resolve(complaint, payment_verified='1', payment_method='UPI')
        b.refresh_from_db()
        complaint.refresh_from_db()
        self.assertEqual((b.status, b.is_paid, b.payment_method), ('COMPLETED', True, 'UPI'))
        payment = Payment.objects.get(booking=b)
        self.assertEqual((payment.status, payment.payment_method), ('PAID', 'UPI'))
        self.assertEqual(payment.amount, Decimal('800.00'))
        self.assertIn('payment verified by admin', complaint.admin_remark)
        # only the commission true-up row may have been added, never a second full charge
        self.assertEqual(self.ledger_net(b), Decimal('-80.00'))                   # 10 % of the Rs 800 labour
        self.assertLessEqual(
            WalletTransaction.objects.filter(booking=b, transaction_type='FEE_DEDUCTION').count(), fee_rows_before + 1)

    def test_duplicate_resolution_does_not_duplicate_settlement(self):
        b, complaint = self.disputed()
        self.resolve(complaint, payment_verified='1', payment_method='CASH')
        snapshot = (self.balance(), WalletTransaction.objects.filter(booking=b).count(), Payment.objects.filter(booking=b).count())
        self.resolve(complaint, payment_verified='1', payment_method='CASH')
        self.resolve(complaint)
        self.assertEqual(
            (self.balance(), WalletTransaction.objects.filter(booking=b).count(), Payment.objects.filter(booking=b).count()),
            snapshot)
        self.assertEqual(Payment.objects.filter(booking=b).count(), 1)

    def test_adjusted_amount_is_validated_and_applied_with_commission_true_up(self):
        b, complaint = self.disputed(labor='800')
        for bad in ('Infinity', 'NaN', '-5', '0', '12.345', 'abc'):
            with self.subTest(bad=bad):
                self.resolve(complaint, adjusted_amount=bad)
                b.refresh_from_db()
                complaint.refresh_from_db()
                self.assertEqual(b.status, 'DISPUTED')                              # nothing changed
                self.assertEqual(complaint.status, 'OPEN')
        self.resolve(complaint, adjusted_amount='600')
        b.refresh_from_db()
        self.assertEqual((b.status, b.final_amount), ('COMPLETED', Decimal('600.00')))
        self.assertFalse(b.is_paid)
        self.assertEqual(self.ledger_net(b), Decimal('-60.00'))

    def test_unsupported_payment_method_in_resolution_is_rejected_without_changes(self):
        b, complaint = self.disputed()
        self.resolve(complaint, payment_verified='1', payment_method='CARD')
        b.refresh_from_db()
        self.assertEqual(b.status, 'DISPUTED')
        self.assertFalse(Payment.objects.filter(booking=b).exists())

    def test_cancel_resolution_still_works(self):
        b, complaint = self.disputed()
        self.resolve(complaint, dispute_action='CANCEL')
        b.refresh_from_db()
        self.assertEqual((b.status, b.cancelled_by), ('CANCELLED', 'ADMIN'))

    def test_only_admins_can_resolve(self):
        b, complaint = self.disputed()
        res = self.w.post(reverse('update_complaint_status', args=[complaint.id]),
                          {'status': 'RESOLVED', 'admin_remark': 'x'})
        b.refresh_from_db()
        self.assertEqual(b.status, 'DISPUTED')


# ===========================================================================
# BUG 20 - is_staff is not an administrator
# ===========================================================================
class TrackingAuthorizationTests(_Base):

    def setUp(self):
        super().setUp()
        self.booking = Booking.objects.create(
            customer=self.customer, worker=self.worker, service=self.service, status='ACCEPTED',
            date=timezone.localdate(), time='10:00 AM - 10:30 AM', location='Kollam',
            phone='9876543210', latitude=8.89, longitude=76.6, district='Kollam')
        self.url = reverse('api_booking_tracking', args=[self.booking.id])

    def get_as(self, user=None):
        client = Client()
        if user is not None:
            client.force_login(user)
        return client.get(self.url)

    def test_owner_and_assigned_worker_keep_access(self):
        self.assertEqual(self.get_as(self.customer).status_code, 200)
        self.assertEqual(self.get_as(self.worker_user).status_code, 200)

    def test_real_administrators_have_access(self):
        admin = CustomUser.objects.create_user('tr_admin', 'tra@x.in', 'Pass123!x', role='ADMIN')
        superuser = CustomUser.objects.create_superuser('tr_super', 'trs@x.in', 'Pass123!x')
        self.assertEqual(self.get_as(admin).status_code, 200)
        self.assertEqual(self.get_as(superuser).status_code, 200)

    def test_staff_flag_alone_grants_nothing(self):
        for role in ('USER', 'WORKER'):
            with self.subTest(role=role):
                staff = CustomUser.objects.create_user(f'tr_staff_{role}', f'trs{role}@x.in', 'Pass123!x', role=role, is_staff=True)
                self.assertEqual(self.get_as(staff).status_code, 403)

    def test_other_customer_and_other_worker_are_refused(self):
        other = CustomUser.objects.create_user('tr_other', 'tro@x.in', 'Pass123!x', role='USER')
        other_worker = CustomUser.objects.create_user('tr_ow', 'trow@x.in', 'Pass123!x', role='WORKER')
        self.assertEqual(self.get_as(other).status_code, 403)
        self.assertEqual(self.get_as(other_worker).status_code, 403)

    def test_anonymous_user_is_sent_to_login(self):
        res = self.get_as(None)
        self.assertEqual(res.status_code, 302)
        self.assertIn('/login', res['Location'])

    def test_staff_user_cannot_broadcast_announcements(self):
        staff = CustomUser.objects.create_user('tr_staff_b', 'trsb@x.in', 'Pass123!x', role='USER', is_staff=True)
        client = Client()
        client.force_login(staff)
        client.post(reverse('admin_broadcast_announcement'), {'target': 'ALL', 'message': 'hello everyone'})
        self.assertFalse(Notification.objects.filter(message__icontains='hello everyone').exists())


# ===========================================================================
# BUG 21 - admin URL
# ===========================================================================
class AdminUrlTests(TestCase):

    def test_resolution_rules(self):
        from lamo.urls import resolve_admin_url as r
        self.assertIsNone(r(None, True))
        self.assertIsNone(r('', True))
        self.assertIsNone(r('   ', True))
        self.assertIsNone(r('admin', True))
        self.assertIsNone(r('/Admin/', True))
        self.assertEqual(r('manage-7f3k9x2q', True), 'manage-7f3k9x2q/')
        self.assertEqual(r(' /manage-x/ ', True), 'manage-x/')
        self.assertEqual(r(None, False), 'admin/')               # local development fallback
        self.assertEqual(r('', False), 'admin/')
        self.assertEqual(r('custom', False), 'custom/')

    def _urlconf(self, environ, production):
        import lamo.urls as urls_module
        with mock.patch.dict(os.environ, environ, clear=False), override_settings(IS_PRODUCTION=production):
            if 'DJANGO_ADMIN_URL' not in environ:
                os.environ.pop('DJANGO_ADMIN_URL', None)
            module = importlib.reload(urls_module)
        clear_url_caches()
        self.addCleanup(clear_url_caches)
        self.addCleanup(importlib.reload, urls_module)
        return module

    def test_production_without_configuration_does_not_expose_admin(self):
        module = self._urlconf({}, production=True)
        for path in ('/admin/', '/admin/login/'):
            with self.assertRaises(Resolver404):
                resolve(path, urlconf=module)
        resolve('/admin-login/', urlconf=module)                  # LAMO's own admin portal is unaffected

    def test_production_custom_url_works_and_default_stays_closed(self):
        module = self._urlconf({'DJANGO_ADMIN_URL': 'manage-7f3k9x2q'}, production=True)
        self.assertEqual(resolve('/manage-7f3k9x2q/login/', urlconf=module).app_name, 'admin')
        with self.assertRaises(Resolver404):
            resolve('/admin/login/', urlconf=module)

    def test_production_rejects_the_predictable_value(self):
        module = self._urlconf({'DJANGO_ADMIN_URL': 'admin'}, production=True)
        with self.assertRaises(Resolver404):
            resolve('/admin/login/', urlconf=module)

    def test_development_fallback_keeps_admin_working(self):
        module = self._urlconf({}, production=False)
        self.assertEqual(resolve('/admin/login/', urlconf=module).app_name, 'admin')

    def test_deployment_files_declare_the_variable_without_a_value(self):
        from django.conf import settings
        root = os.path.dirname(settings.BASE_DIR)
        with open(os.path.join(root, 'render.yaml'), encoding='utf-8') as fh:
            text = fh.read()
        block = text[text.index('key: DJANGO_ADMIN_URL'):].split('\n')[1]
        self.assertIn('sync: false', block)                       # secret value lives in the dashboard
        with open(os.path.join(root, '.env.example'), encoding='utf-8') as fh:
            self.assertIn('DJANGO_ADMIN_URL', fh.read())


# ===========================================================================
# BUG 22 - rate limiter must not fail open
# ===========================================================================
class RateLimiterCacheFailureTests(TestCase):

    def setUp(self):
        rate_limit._local.clear_all()
        patcher = mock.patch.object(rate_limit, 'cache', BrokenCache())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(rate_limit._local.clear_all)

    def test_failure_counter_still_locks_when_the_cache_is_down(self):
        key = 'ratelimit:test:fail'
        results = [rate_limit.register_failure(key, 5, 900)[0] for _ in range(5)]
        self.assertEqual(results, [False, False, False, False, True])
        self.assertTrue(rate_limit.is_rate_limited(key, 5))
        self.assertGreaterEqual(rate_limit.minutes_remaining(key), 1)
        rate_limit.clear(key)
        self.assertFalse(rate_limit.is_rate_limited(key, 5))

    def test_window_counter_still_denies_when_the_cache_is_down(self):
        key = 'ratelimit:test:window'
        results = [rate_limit.register_attempt_within_window(key, 3, 3600)[0] for _ in range(5)]
        self.assertEqual(results, [True, True, True, False, False])

    def test_fallback_expires_so_nobody_is_blocked_permanently(self):
        key = 'ratelimit:test:expiry'
        for _ in range(5):
            rate_limit.register_failure(key, 5, 60)
        self.assertTrue(rate_limit.is_rate_limited(key, 5))
        real = time_module.time
        with mock.patch.object(rate_limit.time, 'time', side_effect=lambda: real() + 120):
            self.assertFalse(rate_limit.is_rate_limited(key, 5))

    def test_full_fallback_store_fails_closed_for_new_keys_but_keeps_existing_ones_working(self):
        with mock.patch.object(rate_limit._LocalCounters, 'MAX_KEYS', 4):
            allowed = [rate_limit.register_attempt_within_window(f'k{i}', 5, 3600)[0] for i in range(6)]
            # 2 keys per window (counter + expiry marker): later NEW keys are refused, not waved through
            self.assertIn(False, allowed)
            self.assertTrue(allowed[0])
            ok, _ = rate_limit.register_attempt_within_window('k0', 5, 3600)       # existing key still counts
            self.assertTrue(ok)

    def test_cooldowns_still_work_when_the_cache_is_down(self):
        rate_limit.start_cooldown('cd:key', 60)
        self.assertTrue(rate_limit.check_cooldown('cd:key', 60))
        self.assertFalse(rate_limit.check_cooldown('cd:other', 60))

    def test_admin_login_lockout_still_enforced_with_a_broken_cache(self):
        admin = CustomUser.objects.create_user('rl_admin', 'rla@x.in', 'RightPass123!x', role='ADMIN')
        client = Client()
        for _ in range(5):
            client.post(reverse('admin_login'), {'username': 'rl_admin', 'password': 'wrong'})
        res = client.post(reverse('admin_login'), {'username': 'rl_admin', 'password': 'RightPass123!x'})
        self.assertNotIn('_auth_user_id', client.session)
        self.assertContains(res, 'Too many failed login attempts')

    @override_settings(OTP_SEND_PER_TARGET_MAX=2, OTP_SEND_PER_IP_MAX=100)
    def test_otp_send_limit_still_enforced_with_a_broken_cache(self):
        request = RequestFactory().post('/')
        request.META['REMOTE_ADDR'] = '203.0.113.9'
        results = [views._otp_send_allowed(request, 'victim@x.in')[0] for _ in range(4)]
        self.assertEqual(results, [True, True, False, False])

    def test_healthy_cache_is_still_used_normally(self):
        mock.patch.stopall()
        cache.clear()
        key = 'ratelimit:test:healthy'
        for _ in range(5):
            rate_limit.register_failure(key, 5, 900)
        self.assertTrue(rate_limit.is_rate_limited(key, 5))
        self.assertIsNone(rate_limit._local.get(key))             # fallback untouched while the cache works
        rate_limit.clear(key)


# ===========================================================================
# BUG 23 - OTP attempt counter must not live only in the session
# ===========================================================================
class OtpAttemptCounterTests(TestCase):

    EMAIL = 'victim@x.in'

    def setUp(self):
        cache.clear()
        rate_limit._local.clear_all()

    def client_with_code(self, code, identifier=None):
        identifier = identifier or self.EMAIL
        client = Client()
        session = client.session
        session['booking_auth_data'] = {
            'identifier': identifier, 'pending_channel': 'email', 'pending_identifier': identifier.lower(),
            'verified_channel': None, 'verified_identifier': None, 'email': identifier.lower(), 'phone': '',
            'code_hash': hashlib.sha256(code.encode()).hexdigest(),
            'created_at': timezone.now().isoformat(), 'attempts': 0, 'customer_name': 'V',
        }
        session.save()
        return client

    def guess(self, client, code):
        return client.post(reverse('verify_otp'), {'action': 'verify', 'otp': code}, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def test_wrong_guesses_are_counted_across_fresh_sessions(self):
        for i in range(3):
            self.guess(self.client_with_code('123456'), '000000')           # a brand-new session each time
        key = rate_limit.otp_verify_target_key('booking', self.EMAIL)
        self.assertEqual(cache.get(key), 3)

    def test_a_new_session_cannot_reset_the_limit(self):
        for _ in range(5):
            self.guess(self.client_with_code('123456'), '000000')
        fresh = self.client_with_code('654321')                              # attacker's new session, with a valid code
        res = self.guess(fresh, '654321')
        self.assertEqual(res.status_code, 429)
        self.assertNotIn('_auth_user_id', fresh.session)
        self.assertTrue(res.json().get('locked_out'))

    def test_limit_is_per_destination_so_unrelated_users_are_unaffected(self):
        for _ in range(5):
            self.guess(self.client_with_code('123456'), '000000')
        res = self.guess(self.client_with_code('111111', 'someone.else@x.in'), '000000')
        self.assertEqual(res.status_code, 400)                                # ordinary "invalid code", not locked
        self.assertFalse(res.json().get('locked_out', False))

    def test_correct_code_clears_the_counter_and_is_single_use(self):
        client = self.client_with_code('123456')
        self.guess(client, '000000')
        self.guess(client, '000000')
        res = self.guess(client, '123456')
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(cache.get(rate_limit.otp_verify_target_key('booking', self.EMAIL)))
        self.assertNotIn('booking_auth_data', client.session)                # consumed: cannot be replayed

    def test_counter_survives_a_cache_outage(self):
        with mock.patch.object(rate_limit, 'cache', BrokenCache()):
            for _ in range(5):
                self.guess(self.client_with_code('123456'), '000000')
            res = self.guess(self.client_with_code('654321'), '654321')
        self.assertEqual(res.status_code, 429)

    def test_customer_registration_otp_counter_survives_new_sessions(self):
        def reg_client(code):
            client = Client()
            session = client.session
            session['customer_reg_data'] = {'name': 'Reg User', 'identifier': 'reg@x.in', 'otp': code,
                                            'created_at': timezone.now().isoformat(), 'attempts': 0}
            session.save()
            return client
        for _ in range(5):
            reg_client('123456').post(reverse('register'), {'action': 'verify_customer_reg_otp', 'otp': '000000'})
        victim_session = reg_client('654321')
        victim_session.post(reverse('register'), {'action': 'verify_customer_reg_otp', 'otp': '654321'})
        self.assertFalse(CustomUser.objects.filter(email='reg@x.in').exists())


# ===========================================================================
# BUGS 24-26 - performance / caching / input limits / startup configuration
# ===========================================================================
def _seed_catalogue(n, tag):
    customer = CustomUser.objects.create_user(f'pc{tag}', f'pc{tag}@x.in', 'Pass123!x', role='USER')
    for i in range(n):
        service = Service.objects.create(name=('Plumbing %d-%d' if i % 2 else 'Cleaning %d-%d') % (tag, i), description='d',
                                         price=100 + i, is_active=True)
        user = CustomUser.objects.create_user(f'pw{tag}_{i}', f'pw{tag}_{i}@x.in', 'Pass123!x', role='WORKER')
        worker = WorkerProfile.objects.create(user=user, service=service, district='Kollam', work_status='AVAILABLE',
                                              is_verified=True, phone=f'97{tag:02d}{i:06d}', pricing_amount=Decimal('150'))
        booking = Booking.objects.create(customer=customer, worker=worker, service=service, status='COMPLETED',
                                         date=timezone.localdate(), time='10:00 AM')
        Review.objects.create(booking=booking, customer=customer, worker=worker, rating=5, comment='ok')


class PublicPagePerformanceTests(TestCase):

    def setUp(self):
        cache.clear()

    def prime(self, client):
        client.cookies['lamo_splash_seen'] = '1'
        session = client.session
        session.update({'district': 'Kollam', 'lamo_selected_district': 'Kollam', 'lamo_splash_shown': True})
        session.save()

    def queries(self, url, fresh=True):
        if fresh:
            cache.clear()          # measure cold-cache cost (service_page has its own pre-existing 120 s cache)
        client = Client()
        self.prime(client)
        with CaptureQueriesContext(connection) as ctx:
            res = client.get(url)
        self.assertEqual(res.status_code, 200)
        return len(ctx), [q['sql'] for q in ctx.captured_queries]

    def test_query_counts_do_not_grow_with_the_number_of_services_and_workers(self):
        urls = ('/?district=Kollam', '/service_page/?district=Kollam', '/api/search-suggestions/?q=plum&district=Kollam')
        _seed_catalogue(3, 1)
        small = [self.queries(u)[0] for u in urls]
        _seed_catalogue(22, 2)
        large = [self.queries(u)[0] for u in urls]
        self.assertEqual(small, large)

    def test_query_budget(self):
        _seed_catalogue(5, 3)
        home, _ = self.queries('/?district=Kollam')
        service_page, _ = self.queries('/service_page/?district=Kollam')
        search, _ = self.queries('/api/search-suggestions/?q=plum&district=Kollam')
        # Measured before this change: 13 / 8 / 7 (all including the 4 session queries).
        self.assertLessEqual(home, 10)
        self.assertLessEqual(service_page, 6)
        self.assertLessEqual(search, 6)

    @override_settings(HOME_CATALOG_CACHE_SECONDS=60)
    def test_search_suggestions_are_served_from_the_public_cache_on_repeat_requests(self):
        _seed_catalogue(4, 4)
        first_n, _ = self.queries('/api/search-suggestions/?q=plum&district=Kollam', fresh=False)
        second_n, second_sql = self.queries('/api/search-suggestions/?q=clean&district=Kollam', fresh=False)
        self.assertLess(second_n, first_n)
        self.assertFalse([q for q in second_sql if 'lamoapp_service' in q or 'lamoapp_workerprofile' in q])

    @override_settings(HOME_CATALOG_CACHE_SECONDS=60)
    def test_home_aggregates_are_cached_per_district_and_hold_no_user_data(self):
        _seed_catalogue(3, 5)
        first, _ = self.queries('/?district=Kollam', fresh=False)
        second, second_sql = self.queries('/?district=Kollam', fresh=False)
        self.assertLess(second, first)
        self.assertFalse([q for q in second_sql if 'AVG(' in q.upper()])      # review aggregation not repeated
        keys = [k for k in ('home_public_stats_kollam', 'home_public_stats_ernakulam') if cache.get(k) is not None]
        self.assertEqual(keys, ['home_public_stats_kollam'])                  # keyed by district only
        payload = cache.get('home_public_stats_kollam')
        self.assertEqual(set(payload), {'total_counts', 'district_counts', 'min_prices', 'district_service_ids',
                                        'review_stats', 'active_services'})

    def test_district_filtering_and_counts_are_preserved(self):
        _seed_catalogue(3, 6)
        other = Service.objects.create(name='Gardening Z', description='d', price=50, is_active=True)
        res = Client().get('/?district=Kollam', **{})
        client = Client()
        self.prime(client)
        html = client.get('/?district=Kollam').content.decode()
        self.assertNotIn('Gardening Z', html)                  # no eligible worker in the district -> not listed
        self.assertIn('Plumbing', html)
        res = client.get('/service_page/?district=Kollam')
        self.assertContains(res, 'Gardening Z')
        self.assertContains(res, '0 Verified Workers')
        self.assertContains(res, '1 Verified Worker')

    def test_search_input_is_length_limited_and_district_is_not_reflected(self):
        _seed_catalogue(3, 7)
        res = Client().get('/api/search-suggestions/', {'q': 'p' * 5000, 'district': 'Evil<script>alert(1)</script>'})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertLessEqual(len(data['query']), views.SEARCH_QUERY_MAX_LENGTH)
        self.assertNotIn('Evil', res.content.decode())
        self.assertNotIn('<script>', res.content.decode())

    def test_search_results_are_limited_and_still_relevant(self):
        for i in range(30):
            Service.objects.create(name=f'Plumbing Repair {i}', description='pipes', price=10 + i, is_active=True)
        data = Client().get('/api/search-suggestions/', {'q': 'plumbing', 'district': 'Kollam'}).json()
        self.assertEqual(len(data['suggestions']), 8)
        self.assertTrue(all('lumb' in s['name'].lower() for s in data['suggestions']))
        empty = Client().get('/api/search-suggestions/', {'q': '', 'district': 'Kollam'}).json()
        self.assertEqual(empty['suggestions'], [])
        self.assertTrue(empty['popular'])

    def test_search_suggestions_still_include_worker_links_per_district(self):
        _seed_catalogue(2, 8)
        data = Client().get('/api/search-suggestions/', {'q': 'plumbing', 'district': 'kollam'}).json()
        self.assertTrue(data['suggestions'])
        self.assertIn('district=Kollam', data['suggestions'][0]['workers_url'])


class StartupConfigurationTests(TestCase):

    def test_database_connection_has_a_bounded_connect_timeout(self):
        from lamo import settings as project_settings
        timeout = project_settings._MYSQL_OPTIONS['connect_timeout']
        self.assertIsInstance(timeout, int)
        self.assertGreater(timeout, 0)

    def test_catalogue_cache_ttl_is_configurable_and_short_by_default(self):
        from lamo import settings as project_settings
        self.assertLessEqual(project_settings.HOME_CATALOG_CACHE_SECONDS, 120)
