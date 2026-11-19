"""Financial consistency of the prepaid commission wallet (audit CR1, CR2, H5).

Every test checks the worker's wallet balance and the WalletTransaction
ledger, because the ledger is the source of truth for what was charged.
"""
from decimal import Decimal
from datetime import timedelta

from django.db.models import Sum
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from lamoapp.models import (
    CustomUser, WorkerProfile, WorkerWallet, Service, Booking, BookingSeries,
    WalletTransaction,
)


class CommissionTestBase(TestCase):
    FEE = Decimal('60.00')  # 10% of the ₹600 service price

    def setUp(self):
        self.service = Service.objects.create(name='Plumbing', description='x', price=600, is_active=True)
        self.customer = CustomUser.objects.create_user('fc_cust', 'fc_c@x.in', 'Pass123!x', role='USER')
        self.worker_user = CustomUser.objects.create_user('fc_wrk', 'fc_w@x.in', 'Pass123!x', role='WORKER')
        self.worker = WorkerProfile.objects.create(
            user=self.worker_user, service=self.service, district='Kollam',
            work_status='AVAILABLE', is_verified=True,
        )
        self.wallet = WorkerWallet.objects.create(worker=self.worker, balance=Decimal('500.00'))
        self.c = Client()
        self.c.force_login(self.customer)
        self.w = Client()
        self.w.force_login(self.worker_user)

    # helpers -----------------------------------------------------------
    def balance(self):
        return WorkerWallet.objects.get(pk=self.wallet.pk).balance

    def ledger_net(self, booking):
        return WalletTransaction.objects.filter(
            booking=booking, transaction_type__in=('FEE_DEDUCTION', 'REFUND')
        ).aggregate(t=Sum('amount'))['t'] or Decimal('0.00')

    def make_booking(self, days=2, status='PENDING', start_offset=None, **extra):
        date = timezone.localdate() + timedelta(days=days)
        b = Booking.objects.create(
            customer=self.customer, worker=self.worker, service=self.service,
            date=date, time='10:00 AM - 10:30 AM', location='Kollam',
            phone='9876543210', status=status, estimated_cost=Decimal('600.00'),
            district='Kollam', **extra,
        )
        if start_offset is not None:
            start = timezone.localtime() + start_offset
            Booking.objects.filter(pk=b.pk).update(
                date=start.date(), start_time=start.time(),
                end_time=(start + timedelta(minutes=30)).time(),
                time=start.strftime('%I:%M %p'),
            )
            b.refresh_from_db()
        return b

    def accept(self, booking):
        self.w.post(reverse('worker_accept_booking', args=[booking.id]))
        booking.refresh_from_db()
        return booking


class AcceptanceChargeTests(CommissionTestBase):
    def test_accept_charges_once_and_double_submit_does_not_double_charge(self):
        b = self.accept(self.make_booking())
        self.assertEqual(b.status, 'ACCEPTED')
        self.assertEqual(self.balance(), Decimal('440.00'))
        # Second POST: booking is no longer PENDING, nothing more is charged.
        self.accept(b)
        self.assertEqual(self.balance(), Decimal('440.00'))
        self.assertEqual(self.ledger_net(b), -self.FEE)

    def test_insufficient_balance_blocks_acceptance(self):
        self.wallet.balance = Decimal('59.99')
        self.wallet.save()
        b = self.accept(self.make_booking())
        self.assertEqual(b.status, 'PENDING')
        self.assertEqual(self.balance(), Decimal('59.99'))
        self.assertFalse(WalletTransaction.objects.filter(booking=b).exists())


class RecurringSeriesPhantomMoneyTests(CommissionTestBase):
    """CR1: a series must never be accepted with unpaid visits marked settled."""

    def make_series(self, visits):
        start = timezone.localdate() + timedelta(days=2)
        series = BookingSeries.objects.create(
            customer=self.customer, worker=self.worker, service=self.service,
            frequency='Daily', time='09:00 AM - 09:30 AM', location='Kollam', phone='9876543210',
            start_date=start, end_date=start + timedelta(days=visits - 1),
        )
        bookings = []
        for i in range(visits):
            bookings.append(Booking.objects.create(
                customer=self.customer, worker=self.worker, service=self.service,
                booking_type='Recurring', date=start + timedelta(days=i),
                time='09:00 AM - 09:30 AM', location='Kollam', phone='9876543210',
                status='PENDING', estimated_cost=Decimal('600.00'), series=series,
            ))
        return series, bookings

    def test_underfunded_series_is_rejected_entirely(self):
        self.wallet.balance = Decimal('60.00')  # covers 1 of 10 visits
        self.wallet.save()
        series, visits = self.make_series(10)
        self.accept(visits[0])
        self.assertEqual(Booking.objects.filter(series=series, status='ACCEPTED').count(), 0)
        self.assertEqual(self.balance(), Decimal('60.00'))
        self.assertFalse(Booking.objects.filter(series=series, fee_settled=True).exists())

    def test_funded_series_charges_every_visit(self):
        self.wallet.balance = Decimal('600.00')
        self.wallet.save()
        series, visits = self.make_series(10)
        self.accept(visits[0])
        self.assertEqual(Booking.objects.filter(series=series, status='ACCEPTED').count(), 10)
        self.assertEqual(self.balance(), Decimal('0.00'))
        for v in visits:
            self.assertEqual(self.ledger_net(v), -self.FEE)

    def test_original_exploit_no_longer_creates_money(self):
        """Audit S15: Rs60 wallet, 10-visit series, 9 cancellations -> Rs540 from nothing."""
        self.wallet.balance = Decimal('60.00')
        self.wallet.save()
        series, visits = self.make_series(10)
        self.accept(visits[0])
        for v in visits[1:]:
            self.c.post(reverse('cancel_booking', args=[v.id]), {'cancellation_reason': 'skip'})
        self.assertLessEqual(self.balance(), Decimal('60.00'))

    def test_series_cancel_refunds_each_paid_visit_exactly_once(self):
        self.wallet.balance = Decimal('180.00')
        self.wallet.save()
        series, visits = self.make_series(3)
        self.accept(visits[0])
        self.assertEqual(self.balance(), Decimal('0.00'))
        self.c.post(reverse('series_cancel', args=[series.id]))
        self.assertEqual(self.balance(), Decimal('180.00'))
        # Cancelling again finds nothing to refund.
        self.c.post(reverse('series_cancel', args=[series.id]))
        self.assertEqual(self.balance(), Decimal('180.00'))
        for v in visits:
            self.assertEqual(self.ledger_net(v), Decimal('0.00'))


class RefundPolicyTests(CommissionTestBase):
    """CR2: commission refund rules."""

    def test_customer_cancel_before_start_refunds_once(self):
        b = self.accept(self.make_booking())
        self.c.post(reverse('cancel_booking', args=[b.id]), {'cancellation_reason': 'not needed'})
        self.assertEqual(self.balance(), Decimal('500.00'))
        # A second cancellation attempt is rejected and refunds nothing more.
        self.c.post(reverse('cancel_booking', args=[b.id]), {'cancellation_reason': 'again'})
        self.assertEqual(self.balance(), Decimal('500.00'))
        self.assertEqual(WalletTransaction.objects.filter(booking=b, transaction_type='REFUND').count(), 1)

    def test_worker_cancel_well_ahead_is_refunded(self):
        b = self.accept(self.make_booking(days=3))
        self.w.post(reverse('worker_cancel_booking', args=[b.id]), {'cancellation_reason': 'family event'})
        b.refresh_from_db()
        self.assertEqual(b.status, 'CANCELLED')
        self.assertEqual(self.balance(), Decimal('500.00'))

    def test_worker_late_cancel_forfeits_commission(self):
        b = self.accept(self.make_booking(start_offset=timedelta(hours=2)))
        self.assertEqual(self.balance(), Decimal('440.00'))
        self.w.post(reverse('worker_cancel_booking', args=[b.id]), {'cancellation_reason': 'busy'})
        b.refresh_from_db()
        self.assertEqual(b.status, 'CANCELLED')
        self.assertEqual(self.balance(), Decimal('440.00'))

    def test_worker_no_show_forfeits_commission(self):
        b = self.accept(self.make_booking(start_offset=-timedelta(hours=1)))
        self.c.post(reverse('report_worker_no_show', args=[b.id]), {'notes': 'never came'})
        b.refresh_from_db()
        self.assertEqual(b.status, 'CANCELLED')
        self.assertEqual(self.balance(), Decimal('440.00'))

    def test_customer_not_available_refunds_worker(self):
        b = self.accept(self.make_booking(start_offset=-timedelta(minutes=30)))
        self.w.post(reverse('worker_report_customer_no_show', args=[b.id]), {'notes': 'knocked, called 3 times'})
        b.refresh_from_db()
        self.assertEqual(b.status, 'CANCELLED')
        self.assertEqual(self.balance(), Decimal('500.00'))

    def test_auto_expired_unstarted_job_forfeits_commission(self):
        from lamoapp.views import check_and_expire_stale_accepted_bookings
        b = self.accept(self.make_booking())
        Booking.objects.filter(pk=b.pk).update(date=timezone.localdate() - timedelta(days=3))
        check_and_expire_stale_accepted_bookings(worker=self.worker)
        b.refresh_from_db()
        self.assertEqual(b.status, 'EXPIRED')
        self.assertEqual(self.balance(), Decimal('440.00'))

    def test_customer_cannot_cancel_in_progress_job(self):
        """H5: after arrival verification the customer cannot self-cancel."""
        b = self.accept(self.make_booking(start_offset=timedelta(minutes=5)))
        self.c.post(reverse('verify_doorstep_qr', args=[b.get_arrival_token()]))
        b.refresh_from_db()
        self.assertEqual(b.status, 'IN_PROGRESS')
        self.c.post(reverse('cancel_booking', args=[b.id]), {'cancellation_reason': 'changed mind'})
        b.refresh_from_db()
        self.assertEqual(b.status, 'IN_PROGRESS')
        self.assertEqual(self.balance(), Decimal('440.00'))

    def test_refund_never_exceeds_what_was_charged(self):
        """A booking marked settled without a ledger charge refunds nothing."""
        b = self.make_booking(status='ACCEPTED')
        Booking.objects.filter(pk=b.pk).update(platform_fee=Decimal('60.00'), fee_settled=True)
        b.refresh_from_db()
        self.c.post(reverse('cancel_booking', args=[b.id]), {'cancellation_reason': 'x'})
        self.assertEqual(self.balance(), Decimal('500.00'))
        self.assertFalse(WalletTransaction.objects.filter(booking=b, transaction_type='REFUND').exists())

    def test_reassigned_booking_charges_new_worker(self):
        """A booking that expired after acceptance is charged again to the new worker."""
        other_user = CustomUser.objects.create_user('fc_w2', 'fc_w2@x.in', 'Pass123!x', role='WORKER')
        other = WorkerProfile.objects.create(
            user=other_user, service=self.service, district='Kollam',
            work_status='AVAILABLE', is_verified=True,
        )
        other_wallet = WorkerWallet.objects.create(worker=other, balance=Decimal('100.00'))
        b = self.accept(self.make_booking())
        Booking.objects.filter(pk=b.pk).update(worker=other, status='PENDING', created_at=timezone.now())
        ow = Client()
        ow.force_login(other_user)
        ow.post(reverse('worker_accept_booking', args=[b.id]))
        b.refresh_from_db()
        self.assertEqual(b.status, 'ACCEPTED')
        self.assertEqual(WorkerWallet.objects.get(pk=other_wallet.pk).balance, Decimal('40.00'))


class RescheduleCommissionTests(CommissionTestBase):
    def test_accepting_worker_proposal_on_pending_request_charges_commission(self):
        b = self.make_booking()
        d = (timezone.localdate() + timedelta(days=4)).strftime('%Y-%m-%d')
        self.w.post(reverse('worker_propose_reschedule', args=[b.id]),
                    {'proposed_date': d, 'proposed_time': '11:00 AM - 11:30 AM'})
        self.c.post(reverse('customer_accept_reschedule', args=[b.id]))
        b.refresh_from_db()
        self.assertEqual(b.status, 'ACCEPTED')
        self.assertEqual(self.balance(), Decimal('440.00'))
        self.assertEqual(self.ledger_net(b), -self.FEE)

    def test_accepting_proposal_on_accepted_job_does_not_charge_again(self):
        b = self.accept(self.make_booking())
        d = (timezone.localdate() + timedelta(days=4)).strftime('%Y-%m-%d')
        self.w.post(reverse('worker_propose_reschedule', args=[b.id]),
                    {'proposed_date': d, 'proposed_time': '11:00 AM - 11:30 AM'})
        b.refresh_from_db()
        self.assertTrue(b.has_pending_reschedule)  # H4: visible on ACCEPTED jobs
        self.c.post(reverse('customer_accept_reschedule', args=[b.id]))
        b.refresh_from_db()
        self.assertEqual(str(b.date), d)
        self.assertEqual(self.balance(), Decimal('440.00'))


class CommissionConfigTests(TestCase):
    """N2: service-specific commission, defaults unchanged."""

    def setUp(self):
        self.service = Service.objects.create(name='Cfg', description='x', price=1000, is_active=True)

    def test_default_rule_is_ten_percent_min_fifteen(self):
        from lamoapp.views import calculate_commission_for_amount
        self.assertEqual(calculate_commission_for_amount(self.service, Decimal('1000')), Decimal('100.00'))
        self.assertEqual(calculate_commission_for_amount(self.service, Decimal('50')), Decimal('15'))

    def test_percent_and_fixed_overrides(self):
        from lamoapp.views import calculate_commission_for_amount
        self.service.commission_type = 'PERCENT'
        self.service.commission_value = Decimal('5')
        self.assertEqual(calculate_commission_for_amount(self.service, Decimal('1000')), Decimal('50.00'))
        self.service.commission_type = 'FIXED'
        self.service.commission_value = Decimal('25')
        self.assertEqual(calculate_commission_for_amount(self.service, Decimal('1000')), Decimal('25.00'))
