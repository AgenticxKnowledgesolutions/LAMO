"""Regression tests for the seven HIGH-severity fixes.

1. Final-bill validation            (views._parse_final_bill / worker_request_completion)
2. Commission cap + true-up         (calculate_commission_for_amount / _true_up_commission_for_final_bill)
3. Profile-photo upload validation  (upload_validation.validate_profile_photo, edit_profile_view)
4. Worker proof validation          (upload_validation.validate_proof_document, WorkerProfileForm)
5. OTP-verified phone change        (begin_phone_change / phone_change_verify)
6. Booking-type validation          (normalize_booking_type / book_service / Booking.check_can_start)
7. Worker payment-method validation (worker_confirm_payment_received)

All tests run on the in-memory test database (lamo.settings_test).
"""
import hashlib
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from lamoapp import views
from lamoapp.forms import UserProfileForm, WorkerProfileForm
from lamoapp.models import (
    Booking, BookingSeries, CustomUser, Notification, Payment, Service,
    UserProfile, WalletTransaction, WorkerProfile, WorkerWallet,
)
from lamoapp.test_upload_fixtures import VALID_PDF, make_jpeg, make_png, make_webp
from lamoapp.upload_validation import validate_profile_photo, validate_proof_document


def _future(days=3):
    return (timezone.localdate() + timedelta(days=days)).isoformat()


class _Base(TestCase):
    """Customer + verified worker with a funded wallet and a Rs 500 service."""

    PRICE = Decimal('500.00')

    def setUp(self):
        cache.clear()
        self.service = Service.objects.create(name='Plumbing', description='x', price=self.PRICE, is_active=True)
        self.customer = CustomUser.objects.create_user('hs_cust', 'hs_c@x.in', 'Pass123!x', role='USER')
        UserProfile.objects.create(user=self.customer, phone='9811111111')
        self.worker_user = CustomUser.objects.create_user('hs_wrk', 'hs_w@x.in', 'Pass123!x', role='WORKER')
        self.worker = WorkerProfile.objects.create(
            user=self.worker_user, service=self.service, district='Kollam',
            work_status='AVAILABLE', is_verified=True, phone='9833333333',
        )
        self.wallet = WorkerWallet.objects.create(worker=self.worker, balance=Decimal('1000.00'))
        self.c = Client()
        self.c.force_login(self.customer)
        self.w = Client()
        self.w.force_login(self.worker_user)

    def balance(self):
        return WorkerWallet.objects.get(pk=self.wallet.pk).balance

    def ledger_net(self, booking):
        from django.db.models import Sum
        return WalletTransaction.objects.filter(
            booking=booking, transaction_type__in=('FEE_DEDUCTION', 'REFUND')
        ).aggregate(t=Sum('amount'))['t'] or Decimal('0.00')

    def accepted_in_progress(self, estimate=None):
        """A booking accepted through the real view (commission charged), then started."""
        self._day = getattr(self, '_day', 1) + 1       # a different day per booking: no slot clash
        b = Booking.objects.create(
            customer=self.customer, worker=self.worker, service=self.service,
            date=timezone.localdate() + timedelta(days=self._day), time='10:00 AM - 10:30 AM',
            location='Kollam', phone='9876543210', status='PENDING',
            estimated_cost=estimate if estimate is not None else self.PRICE, district='Kollam',
        )
        self.w.post(reverse('worker_accept_booking', args=[b.id]))
        b.refresh_from_db()
        assert b.status == 'ACCEPTED', b.status
        Booking.objects.filter(pk=b.pk).update(status='IN_PROGRESS', arrival_verified_at=timezone.now())
        b.refresh_from_db()
        return b

    def bill(self, booking, **post):
        res = self.w.post(reverse('worker_request_completion', args=[booking.id]), post)
        booking.refresh_from_db()
        return res

    def full_settlement(self, booking):
        """customer approves the bill, worker records payment received."""
        self.c.post(reverse('confirm_completion', args=[booking.id]))
        self.w.post(reverse('worker_confirm_payment_received', args=[booking.id]), {'payment_method': 'CASH'})
        booking.refresh_from_db()
        return booking


# ===========================================================================
# TASK 1 - final bill validation
# ===========================================================================
class FinalBillValidationTests(_Base):

    def test_valid_labour_and_material_are_summed_and_validated_separately(self):
        b = self.accepted_in_progress()
        self.bill(b, labor_amount='700', material_amount='100')
        self.assertEqual(b.status, 'COMPLETION_REQUESTED')
        self.assertEqual(b.final_amount, Decimal('800.00'))
        self.assertEqual(b.material_amount, Decimal('100.00'))
        self.assertIsNone(b.bill_confirmed_at)

    def test_blank_bill_defaults_to_estimate(self):
        b = self.accepted_in_progress()
        self.bill(b)
        self.assertEqual(b.status, 'COMPLETION_REQUESTED')
        self.assertEqual(b.final_amount, self.PRICE)

    def test_bad_amounts_are_rejected_and_nothing_changes(self):
        bad_posts = [
            {'labor_amount': '0'},
            {'labor_amount': '-50'},
            {'labor_amount': 'abc'},
            {'labor_amount': 'NaN'},
            {'labor_amount': 'Infinity'},
            {'labor_amount': '-Infinity'},
            {'labor_amount': '1e999'},
            {'labor_amount': '99999999999'},          # above the column limit
            {'labor_amount': '100.123'},              # more than 2 decimals
            {'labor_amount': '100', 'material_amount': '-1'},
            {'labor_amount': '100', 'material_amount': 'NaN'},
            {'final_amount': '0'},
            {'final_amount': 'Infinity'},
            {'final_amount': '100', 'material_amount': '200'},   # material > total
        ]
        for post in bad_posts:
            with self.subTest(post=post):
                b = self.accepted_in_progress()
                self.bill(b, **post)
                self.assertEqual(b.status, 'IN_PROGRESS')
                self.assertIsNone(b.final_amount)
                Booking.objects.filter(pk=b.pk).delete()

    @override_settings(FINAL_BILL_MAX_AMOUNT='10000')
    def test_absolute_ceiling_rejects_unreasonable_amount(self):
        b = self.accepted_in_progress()
        self.bill(b, labor_amount='20000')
        self.assertEqual(b.status, 'IN_PROGRESS')
        b = self.accepted_in_progress()
        self.bill(b, labor_amount='9000')
        self.assertEqual(b.status, 'COMPLETION_REQUESTED')

    @override_settings(FINAL_BILL_MAX_ADJUSTMENT_PERCENT='100')
    def test_bill_far_above_estimate_goes_to_admin_review_not_customer(self):
        admin = CustomUser.objects.create_user('hs_admin', 'hs_a@x.in', 'Pass123!x', role='ADMIN')
        b = self.accepted_in_progress()                   # estimate 500
        self.bill(b, labor_amount='1500')                 # +200 % > 100 %
        self.assertEqual(b.status, 'IN_PROGRESS')
        self.assertTrue(Notification.objects.filter(recipient=admin, booking=b, message__icontains='Review needed').exists())
        self.assertFalse(Notification.objects.filter(recipient=self.customer, booking=b, message__icontains='final amount').exists())
        self.bill(b, labor_amount='900')                  # +80 %: allowed
        self.assertEqual(b.status, 'COMPLETION_REQUESTED')

    def test_payment_cannot_settle_before_customer_approves_bill(self):
        b = self.accepted_in_progress()
        self.bill(b, labor_amount='800')
        self.w.post(reverse('worker_confirm_payment_received', args=[b.id]), {'payment_method': 'CASH'})
        b.refresh_from_db()
        self.assertEqual(b.status, 'COMPLETION_REQUESTED')
        self.assertFalse(b.is_paid)
        self.full_settlement(b)
        self.assertEqual(b.status, 'COMPLETED')
        self.assertTrue(b.is_paid)

    def test_resubmitted_bill_needs_fresh_customer_approval(self):
        b = self.accepted_in_progress()
        self.bill(b, labor_amount='800')
        self.c.post(reverse('confirm_completion', args=[b.id]))
        b.refresh_from_db()
        self.assertIsNotNone(b.bill_confirmed_at)
        Booking.objects.filter(pk=b.pk).update(status='IN_PROGRESS')   # job reopened
        self.bill(b, labor_amount='900')
        self.assertIsNone(b.bill_confirmed_at)

    def test_other_worker_cannot_bill_someones_job(self):
        b = self.accepted_in_progress()
        other = CustomUser.objects.create_user('hs_wrk2', 'hs_w2@x.in', 'Pass123!x', role='WORKER')
        WorkerProfile.objects.create(user=other, service=self.service, district='Kollam', is_verified=True, phone='9844444444')
        c = Client()
        c.force_login(other)
        res = c.post(reverse('worker_request_completion', args=[b.id]), {'labor_amount': '800'})
        self.assertEqual(res.status_code, 404)
        b.refresh_from_db()
        self.assertEqual(b.status, 'IN_PROGRESS')


# ===========================================================================
# TASK 2 - commission cap and true-up
# ===========================================================================
class CommissionCalculationTests(TestCase):

    def setUp(self):
        self.service = Service.objects.create(name='S', description='x', price=500, is_active=True)

    def fee(self, amount, service=None):
        return views.calculate_commission_for_amount(service or self.service, amount)

    def test_normal_job_keeps_existing_policy(self):
        self.assertEqual(self.fee(500), Decimal('50.00'))
        self.assertEqual(self.fee(2000), Decimal('200.00'))

    def test_minimum_still_applies_when_job_is_large_enough(self):
        self.assertEqual(self.fee(100), Decimal('15.00'))
        self.assertEqual(self.fee(15), Decimal('15.00'))

    def test_commission_never_exceeds_the_job_amount(self):
        self.assertEqual(self.fee(10), Decimal('10.00'))   # was 15
        self.assertEqual(self.fee(14), Decimal('14.00'))
        self.assertEqual(self.fee(Decimal('0.50')), Decimal('0.50'))

    def test_zero_negative_and_invalid_amounts_pay_nothing(self):
        for value in (0, '0', None, '', -100, '-5', 'abc', 'NaN', 'Infinity'):
            with self.subTest(value=value):
                self.assertEqual(self.fee(value), Decimal('0.00'))

    def test_fixed_commission_is_capped_too(self):
        fixed = Service.objects.create(name='F', description='x', price=500, commission_type='FIXED', commission_value=50)
        self.assertEqual(self.fee(30, fixed), Decimal('30.00'))
        self.assertEqual(self.fee(500, fixed), Decimal('50.00'))

    def test_launch_period_worker_pays_nothing(self):
        user = CustomUser.objects.create_user('l_w', 'l@x.in', 'Pass123!x', role='WORKER')
        worker = WorkerProfile.objects.create(
            user=user, service=self.service, district='Kollam', is_verified=True,
            launch_ends_at=timezone.now() + timedelta(days=5), phone='9855555555',
        )
        self.assertEqual(views.calculate_commission_for_amount(self.service, 500, worker=worker), Decimal('0.00'))

    def test_booking_fee_excludes_material_cost(self):
        user = CustomUser.objects.create_user('m_c', 'm@x.in', 'Pass123!x', role='USER')
        b = Booking(customer=user, service=self.service, final_amount=Decimal('5000'),
                    material_amount=Decimal('4000'), estimated_cost=Decimal('500'))
        self.assertEqual(views.calculate_booking_platform_fee(b), Decimal('100.00'))   # 10 % of 1000 labour


class CommissionTrueUpTests(_Base):
    """Rs 500 estimate -> Rs 50 at acceptance; the final bill moves only the difference."""

    def test_higher_final_bill_deducts_only_the_difference(self):
        b = self.accepted_in_progress()
        self.assertEqual(self.balance(), Decimal('950.00'))           # 1000 - 50
        self.bill(b, labor_amount='2000')                             # fee should be 200
        self.full_settlement(b)
        self.assertEqual(b.status, 'COMPLETED')
        self.assertEqual(self.ledger_net(b), Decimal('-200.00'))      # NOT -250
        self.assertEqual(self.balance(), Decimal('800.00'))           # 1000 - 200
        self.assertEqual(b.platform_fee, Decimal('200.00'))
        adjustment = WalletTransaction.objects.get(booking=b, notes__startswith='Commission adjustment')
        self.assertEqual(adjustment.amount, Decimal('-150.00'))
        self.assertEqual(adjustment.balance_after, Decimal('800.00'))

    def test_settlement_is_idempotent(self):
        b = self.accepted_in_progress()
        self.bill(b, labor_amount='2000')
        self.full_settlement(b)
        before = (self.balance(), WalletTransaction.objects.filter(booking=b).count())
        views._settle_booking_completion_financials(b)
        views._settle_booking_completion_financials(b)
        self.w.post(reverse('worker_confirm_payment_received', args=[b.id]), {'payment_method': 'CASH'})
        self.assertEqual((self.balance(), WalletTransaction.objects.filter(booking=b).count()), before)

    def test_lower_final_bill_refunds_the_difference(self):
        b = self.accepted_in_progress()
        self.bill(b, labor_amount='200')                              # fee 20, was 50 -> refund 30
        self.full_settlement(b)
        self.assertEqual(self.ledger_net(b), Decimal('-20.00'))
        self.assertEqual(self.balance(), Decimal('980.00'))
        self.assertTrue(WalletTransaction.objects.filter(booking=b, transaction_type='REFUND', amount=Decimal('30.00')).exists())

    def test_same_final_bill_moves_nothing(self):
        b = self.accepted_in_progress()
        self.bill(b)                                                  # defaults to the estimate
        self.full_settlement(b)
        self.assertEqual(self.ledger_net(b), Decimal('-50.00'))
        self.assertEqual(self.balance(), Decimal('950.00'))
        self.assertEqual(WalletTransaction.objects.filter(booking=b).count(), 1)

    def test_material_is_exempt_from_the_final_commission(self):
        b = self.accepted_in_progress()
        self.bill(b, labor_amount='1000', material_amount='4000')     # eligible labour Rs 1000 -> fee 100
        self.full_settlement(b)
        self.assertEqual(self.ledger_net(b), Decimal('-100.00'))

    def test_small_job_commission_is_capped_at_job_value(self):
        b = self.accepted_in_progress(estimate=Decimal('10.00'))
        self.assertEqual(self.balance(), Decimal('990.00'))           # fee 10, not 15
        self.assertEqual(b.platform_fee, Decimal('10.00'))

    def test_launch_offer_waiver_is_honoured(self):
        self.worker.launch_ends_at = timezone.now() + timedelta(days=5)
        self.worker.save()
        b = self.accepted_in_progress()
        self.assertEqual(self.balance(), Decimal('1000.00'))
        self.bill(b, labor_amount='2000')
        self.full_settlement(b)
        self.assertEqual(self.balance(), Decimal('1000.00'))

    def test_extra_charge_that_overdraws_the_wallet_pauses_dues(self):
        WorkerWallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal('50.00'))
        b = self.accepted_in_progress()                               # balance 0
        self.bill(b, labor_amount='2000')                             # extra 150 -> -150
        self.full_settlement(b)
        self.assertEqual(self.balance(), Decimal('-150.00'))
        self.worker.refresh_from_db()
        self.assertTrue(self.worker.is_paused_for_dues)


# ===========================================================================
# TASKS 3 & 4 - uploads
# ===========================================================================
def _upload(name, data, ctype):
    return SimpleUploadedFile(name, data, content_type=ctype)


class ProfilePhotoValidationTests(TestCase):

    def test_valid_jpeg_png_webp_accepted_and_renamed(self):
        for name, data, ctype in (
            ('me.jpg', make_jpeg(), 'image/jpeg'),
            ('me.jpeg', make_jpeg(), 'image/jpeg'),
            ('me.png', make_png(), 'image/png'),
            ('me.webp', make_webp(), 'image/webp'),
        ):
            with self.subTest(name=name):
                out = validate_profile_photo(_upload(name, data, ctype))
                self.assertRegex(out.name, r'^[0-9a-f]{32}\.(jpg|jpeg|png|webp)$')

    def test_rejected_files(self):
        valid_png = make_png()
        cases = {
            'executable disguised as image': _upload('photo.png', b'MZ\x90\x00\x03' + b'\x00' * 100, 'image/png'),
            'exe extension': _upload('photo.exe', valid_png, 'image/png'),
            'double extension': _upload('photo.php.png', valid_png, 'image/png'),
            'trailing double extension': _upload('photo.png.exe', valid_png, 'image/png'),
            'svg': _upload('photo.svg', b'<svg xmlns="http://www.w3.org/2000/svg"/>', 'image/svg+xml'),
            'gif': _upload('photo.gif', b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00;', 'image/gif'),
            'png bytes with jpg extension': _upload('photo.jpg', valid_png, 'image/jpeg'),
            'declared mime mismatch': _upload('photo.png', valid_png, 'text/html'),
            'truncated/corrupt png': _upload('photo.png', valid_png[: len(valid_png) // 2], 'image/png'),
            'header only': _upload('photo.jpg', b'\xff\xd8\xff\xe0' + b'\x00' * 20, 'image/jpeg'),
            'pdf as photo': _upload('photo.png', VALID_PDF, 'image/png'),
            'empty file': _upload('photo.png', b'', 'image/png'),
        }
        for label, upload in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(ValidationError):
                    validate_profile_photo(upload)

    def test_path_traversal_name_rejected(self):
        upload = _upload('x.png', make_png(), 'image/png')
        upload._name = '../../etc/passwd.png'
        with self.assertRaises(ValidationError):
            validate_profile_photo(upload)

    @override_settings(PROFILE_PHOTO_MAX_BYTES=1024)
    def test_oversized_file_rejected_using_configurable_limit(self):
        from PIL import Image
        import io
        import os
        buf = io.BytesIO()
        Image.frombytes('RGB', (120, 120), os.urandom(120 * 120 * 3)).save(buf, 'PNG')
        self.assertGreater(len(buf.getvalue()), 1024)
        with self.assertRaises(ValidationError):
            validate_profile_photo(_upload('big.png', buf.getvalue(), 'image/png'))

    def test_form_validator_is_applied_to_both_profile_forms(self):
        for form_cls in (UserProfileForm, WorkerProfileForm):
            form = form_cls()
            form.cleaned_data = {'profile_image': _upload('p.png', b'MZ' + b'\x00' * 50, 'image/png')}
            with self.assertRaises(ValidationError):
                form.clean_profile_image()
            form.cleaned_data = {'profile_image': _upload('p.png', make_png(), 'image/png')}
            self.assertIsNotNone(form.clean_profile_image())


class ProfilePhotoAjaxEndpointTests(TestCase):

    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, True)
        self.user = CustomUser.objects.create_user('ph_c', 'ph@x.in', 'Pass123!x', role='USER')
        self.profile = UserProfile.objects.create(user=self.user, phone='9811111111')
        self.client.force_login(self.user)

    def _post(self, upload):
        return self.client.post(
            reverse('edit_profile'), {'profile_image': upload},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

    def test_valid_image_is_stored(self):
        with override_settings(MEDIA_ROOT=self.media):
            res = self._post(_upload('me.png', make_png(), 'image/png'))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['status'], 'success')
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.profile_image.name)
        self.assertNotIn('me.png', self.profile.profile_image.name)

    def test_disguised_executable_is_refused_with_400(self):
        with override_settings(MEDIA_ROOT=self.media):
            res = self._post(_upload('me.png', b'MZ\x90\x00' + b'\x00' * 200, 'image/png'))
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()['status'], 'error')
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.profile_image)

    def test_corrupt_image_is_refused(self):
        png = make_png()
        with override_settings(MEDIA_ROOT=self.media):
            res = self._post(_upload('me.png', png[: len(png) // 2], 'image/png'))
        self.assertEqual(res.status_code, 400)


class ProofDocumentValidationTests(TestCase):

    def test_valid_documents_accepted(self):
        for name, data, ctype in (
            ('kyc.pdf', VALID_PDF, 'application/pdf'),
            ('kyc.jpg', make_jpeg(), 'image/jpeg'),
            ('kyc.png', make_png(), 'image/png'),
        ):
            with self.subTest(name=name):
                out = validate_proof_document(_upload(name, data, ctype))
                self.assertRegex(out.name, r'^[0-9a-f]{32}\.(pdf|jpg|png)$')

    def test_rejected_documents(self):
        cases = {
            'fake pdf extension (text)': _upload('kyc.pdf', b'this is not a pdf', 'application/pdf'),
            'fake pdf extension (exe)': _upload('kyc.pdf', b'MZ\x90\x00\x03', 'application/pdf'),
            'executable as image': _upload('kyc.png', b'MZ\x90\x00' + b'\x00' * 100, 'image/png'),
            'malformed pdf (header only)': _upload('kyc.pdf', b'%PDF-1.4\n', 'application/pdf'),
            'truncated pdf (no EOF)': _upload('kyc.pdf', VALID_PDF.replace(b'%%EOF', b''), 'application/pdf'),
            'pdf with javascript': _upload('kyc.pdf', VALID_PDF.replace(b'/Type /Page ', b'/JavaScript (x) /Type /Page '), 'application/pdf'),
            'pdf with launch action': _upload('kyc.pdf', VALID_PDF.replace(b'/Type /Page ', b'/Launch /Type /Page '), 'application/pdf'),
            'pdf named .png': _upload('kyc.png', VALID_PDF, 'image/png'),
            'png bytes named .pdf': _upload('kyc.pdf', make_png(), 'application/pdf'),
            'corrupt jpeg': _upload('kyc.jpg', make_jpeg()[:60], 'image/jpeg'),
            'double extension': _upload('kyc.exe.pdf', VALID_PDF, 'application/pdf'),
            'trailing exe': _upload('kyc.pdf.exe', VALID_PDF, 'application/pdf'),
            'html': _upload('kyc.html', b'<html></html>', 'text/html'),
            'webp (not an allowed proof type)': _upload('kyc.webp', make_webp(), 'image/webp'),
            'wrong declared mime': _upload('kyc.pdf', VALID_PDF, 'text/plain'),
        }
        for label, upload in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(ValidationError):
                    validate_proof_document(upload)

    def test_path_traversal_rejected(self):
        upload = _upload('x.pdf', VALID_PDF, 'application/pdf')
        upload._name = '../../etc/passwd.pdf'
        with self.assertRaises(ValidationError):
            validate_proof_document(upload)

    @override_settings(PROOF_DOCUMENT_MAX_BYTES=2048)
    def test_oversized_document_rejected_using_configurable_limit(self):
        big = VALID_PDF.replace(b'%%EOF', b'% ' + b'0' * 4096 + b'\n%%EOF')
        with self.assertRaises(ValidationError):
            validate_proof_document(_upload('big.pdf', big, 'application/pdf'))

    def test_worker_form_runs_the_content_check(self):
        form = WorkerProfileForm()
        form.cleaned_data = {'proof_document': _upload('kyc.pdf', b'not really a pdf', 'application/pdf')}
        with self.assertRaises(ValidationError):
            form.clean_proof_document()
        form.cleaned_data = {'proof_document': _upload('kyc.pdf', VALID_PDF, 'application/pdf')}
        out = form.clean_proof_document()
        self.assertRegex(out.name, r'^[0-9a-f]{32}\.pdf$')

    def test_previously_stored_proof_stays_private_but_reachable_by_admin(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, True)
        service = Service.objects.create(name='S', description='x', price=100, is_active=True)
        user = CustomUser.objects.create_user('kyc_w', 'k@x.in', 'Pass123!x', role='WORKER')
        admin = CustomUser.objects.create_user('kyc_a', 'ka@x.in', 'Pass123!x', role='ADMIN')
        with override_settings(MEDIA_ROOT=media):
            # A legacy stored file that would fail today's stricter content check.
            worker = WorkerProfile.objects.create(
                user=user, service=service, district='Kollam', phone='9866666666',
                proof_document=SimpleUploadedFile('legacy.pdf', b'%PDF-1.4 legacy stub', content_type='application/pdf'),
            )
            self.client.force_login(admin)
            self.assertEqual(self.client.get(reverse('worker_proof_document', args=[worker.pk])).status_code, 200)
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse('worker_proof_document', args=[worker.pk])).status_code, 403)
            form = WorkerProfileForm(instance=worker)
            form.cleaned_data = {'proof_document': worker.proof_document}
            self.assertEqual(form.clean_proof_document(), worker.proof_document)   # unchanged file is not re-validated


# ===========================================================================
# TASK 5 - OTP-verified phone change
# ===========================================================================
class PhoneChangeOtpTests(TestCase):

    def setUp(self):
        cache.clear()
        self.sent = []
        patcher = mock.patch('lamoapp.views.send_otp_sms', side_effect=lambda phone, otp: self.sent.append((phone, otp)) or True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service = Service.objects.create(name='S', description='x', price=100, is_active=True)
        self.user = CustomUser.objects.create_user('pc_u', 'pc@x.in', 'Pass123!x', role='USER')
        self.profile = UserProfile.objects.create(user=self.user, phone='9811111111')
        self.other = CustomUser.objects.create_user('pc_o', 'pco@x.in', 'Pass123!x', role='USER')
        self.other_profile = UserProfile.objects.create(user=self.other, phone='9822222222')
        self.wuser = CustomUser.objects.create_user('pc_w', 'pcw@x.in', 'Pass123!x', role='WORKER')
        self.worker = WorkerProfile.objects.create(
            user=self.wuser, service=self.service, district='Kollam', phone='9833333333', is_verified=True)
        self.client.force_login(self.user)

    # helpers
    def request_change(self, number, client=None):
        return (client or self.client).post(reverse('edit_profile'), {'phone': number})

    def code(self):
        return self.sent[-1][1]

    def verify(self, code, client=None):
        return (client or self.client).post(reverse('phone_change_verify'), {'action': 'verify', 'otp': code})

    def phone(self):
        self.profile.refresh_from_db()
        return self.profile.phone

    def test_change_without_otp_never_updates_the_number(self):
        res = self.request_change('9899999999')
        self.assertRedirects(res, reverse('phone_change_verify'))
        self.assertEqual(self.phone(), '9811111111')
        self.assertEqual(self.sent[-1][0], '9899999999')          # code goes to the NEW number
        # Visiting other pages or the verify page itself changes nothing.
        self.client.get(reverse('phone_change_verify'))
        self.assertEqual(self.phone(), '9811111111')

    def test_valid_otp_updates_the_number(self):
        self.request_change('9899999999')
        res = self.verify(self.code())
        self.assertEqual(res.status_code, 302)
        self.assertEqual(self.phone(), '9899999999')

    def test_number_is_normalised_like_the_rest_of_the_app(self):
        self.request_change('+91 98999 99999')
        self.assertEqual(self.sent[-1][0], '9899999999')
        self.verify(self.code())
        self.assertEqual(self.phone(), '9899999999')

    def test_invalid_otp_is_rejected(self):
        self.request_change('9899999999')
        wrong = '000000' if self.code() != '000000' else '111111'
        self.verify(wrong)
        self.assertEqual(self.phone(), '9811111111')

    def test_attempts_are_limited_and_correct_code_is_refused_after_lockout(self):
        self.request_change('9899999999')
        good = self.code()
        wrong = '000000' if good != '000000' else '111111'
        for _ in range(5):
            self.verify(wrong)
        self.verify(good)
        self.assertEqual(self.phone(), '9811111111')

    def test_expired_otp_is_rejected(self):
        self.request_change('9899999999')
        session = self.client.session
        pending = session['phone_change_pending']
        pending['created_at'] = (timezone.now() - timedelta(minutes=11)).isoformat()
        session['phone_change_pending'] = pending
        session.save()
        self.verify(self.code())
        self.assertEqual(self.phone(), '9811111111')

    def test_otp_cannot_be_reused(self):
        self.request_change('9899999999')
        code = self.code()
        self.verify(code)
        self.assertEqual(self.phone(), '9899999999')
        UserProfile.objects.filter(pk=self.profile.pk).update(phone='9811111111')   # simulate a later reset
        self.verify(code)
        self.assertEqual(self.phone(), '9811111111')

    def test_code_for_one_number_cannot_confirm_another(self):
        self.request_change('9899999999')
        code_a = self.code()
        self.request_change('9877777777')               # user changes the pending number
        self.verify(code_a)                              # old code no longer valid
        self.assertEqual(self.phone(), '9811111111')
        self.verify(self.code())
        self.assertEqual(self.phone(), '9877777777')     # only the number the code was sent to

    def test_cross_role_duplicate_number_is_refused_and_no_code_is_sent(self):
        for taken in ('9833333333', '9822222222'):         # a worker's and a customer's number
            with self.subTest(taken=taken):
                res = self.request_change(taken)
                self.assertEqual(res.status_code, 200)     # form re-rendered with an error
                self.assertEqual(self.sent, [])
                self.assertEqual(self.phone(), '9811111111')

    def test_another_user_cannot_complete_my_pending_change(self):
        self.request_change('9899999999')
        stolen = self.client.session['phone_change_pending']
        attacker = Client()
        attacker.force_login(self.other)
        session = attacker.session
        session['phone_change_pending'] = stolen            # copied pending data, but user_id is mine
        session.save()
        attacker.post(reverse('phone_change_verify'), {'action': 'verify', 'otp': self.code()})
        self.other_profile.refresh_from_db()
        self.assertEqual(self.other_profile.phone, '9822222222')
        self.assertEqual(self.phone(), '9811111111')

    def test_second_user_cannot_take_a_number_claimed_in_the_meantime(self):
        self.request_change('9899999999')
        code_user = self.code()
        other_client = Client()
        other_client.force_login(self.other)
        self.request_change('9899999999', client=other_client)   # the number is still free: code sent
        code_other = self.code()
        self.verify(code_user)                                    # first user verifies and gets it
        self.assertEqual(self.phone(), '9899999999')
        self.verify(code_other, client=other_client)              # second is refused at write time
        self.other_profile.refresh_from_db()
        self.assertEqual(self.other_profile.phone, '9822222222')

    def test_concurrent_update_lock_blocks_a_simultaneous_write(self):
        self.request_change('9899999999')
        cache.add('phonechange:lock:9899999999', 999, 30)         # another request is mid-update
        self.verify(self.code())
        self.assertEqual(self.phone(), '9811111111')

    def test_resend_is_rate_limited(self):
        for _ in range(3):
            self.request_change('9899999999')
        sent_before = len(self.sent)
        self.request_change('9899999999')
        self.assertEqual(len(self.sent), sent_before)

    def test_unavailable_sms_does_not_change_the_number(self):
        with mock.patch('lamoapp.views.sms_delivery_available', return_value=False):
            res = self.request_change('9899999999')
        self.assertRedirects(res, reverse('edit_profile'))
        self.assertEqual(self.phone(), '9811111111')
        self.assertEqual(self.sent, [])

    def test_cancel_discards_the_pending_change(self):
        self.request_change('9899999999')
        code = self.code()
        self.client.post(reverse('phone_change_verify'), {'action': 'cancel'})
        self.verify(code)
        self.assertEqual(self.phone(), '9811111111')

    def test_worker_phone_change_updates_the_worker_profile_only_after_otp(self):
        w = Client()
        w.force_login(self.wuser)
        ok, _msg = self._begin_for(w, self.wuser, '9899999999')
        self.assertTrue(ok)
        w.post(reverse('phone_change_verify'), {'action': 'verify', 'otp': '000000'})
        self.worker.refresh_from_db()
        self.assertEqual(self.worker.phone, '9833333333')
        w.post(reverse('phone_change_verify'), {'action': 'verify', 'otp': self.code()})
        self.worker.refresh_from_db()
        self.assertEqual(self.worker.phone, '9899999999')

    def _begin_for(self, client, user, number):
        from django.test import RequestFactory
        request = RequestFactory().post('/')
        request.session = client.session
        request.META['REMOTE_ADDR'] = '127.0.0.1'
        ok, msg = views.begin_phone_change(request, user, number)
        request.session.save()
        return ok, msg

    def test_login_and_registration_otp_state_is_untouched(self):
        self.request_change('9899999999')
        self.assertNotIn('booking_auth_data', self.client.session)
        self.assertNotIn('otp_user_id', self.client.session)


# ===========================================================================
# TASK 6 - booking type validation
# ===========================================================================
class BookingTypeNormalisationTests(TestCase):

    def test_canonical_values(self):
        n = views.normalize_booking_type
        for raw in ('One Time', 'one time', 'ONE TIME', 'one_time', 'one-time', ' One  Time ', 'onetime', '', None):
            self.assertEqual(n(raw), 'One Time', raw)
        for raw in ('Recurring', 'RECURRING', 'recurring', 'ReCuRrInG', ' recurring '):
            self.assertEqual(n(raw), 'Recurring', raw)
        for raw in ('Emergency', 'emergency', 'EMERGENCY'):
            self.assertEqual(n(raw), 'Emergency', raw)
        for raw in ('Normal', 'weekly', 'one time; drop table', 'recurring2', 'None'):
            self.assertIsNone(n(raw), raw)


class BookingTypeEndpointTests(_Base):

    def post(self, booking_type, **extra):
        data = {
            'worker': self.worker.id, 'service': self.service.id, 'booking_type': booking_type,
            'date': _future(5), 'time': '10:00-12:00', 'location': 'Kollam', 'phone': '9876543210',
            'description': 'x', 'start_date': _future(5), 'end_date': _future(30), 'frequency': 'Weekly',
        }
        data.update(extra)
        return self.c.post(reverse('book_service'), data)

    def test_one_time_booking_still_works(self):
        self.assertEqual(self.post('One Time').status_code, 302)
        self.assertEqual(Booking.objects.get(customer=self.customer).booking_type, 'One Time')

    def test_missing_booking_type_defaults_to_one_time(self):
        data = {'worker': self.worker.id, 'service': self.service.id, 'date': _future(5), 'time': '10:00-12:00',
                'location': 'Kollam', 'phone': '9876543210'}
        self.c.post(reverse('book_service'), data)
        self.assertEqual(Booking.objects.get(customer=self.customer).booking_type, 'One Time')

    def test_recurring_in_every_case_is_still_blocked(self):
        for raw in ('Recurring', 'RECURRING', 'recurring', 'ReCuRrInG', ' recurring '):
            with self.subTest(raw=raw):
                res = self.post(raw, **{})
                self.assertEqual(Booking.objects.filter(customer=self.customer).count(), 0)
                self.assertEqual(BookingSeries.objects.count(), 0)
                self.assertNotEqual(res.status_code, 500)

    def test_recurring_ajax_response_says_unavailable(self):
        res = self.c.post(reverse('book_service'), {
            'worker': self.worker.id, 'service': self.service.id, 'booking_type': 'recurring',
            'date': _future(5), 'time': '10:00-12:00', 'location': 'Kollam', 'phone': '9876543210',
            'start_date': _future(5), 'end_date': _future(30), 'frequency': 'Weekly',
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 400)
        self.assertIn('unavailable', res.json()['error_message'].lower())

    def test_invalid_booking_type_is_rejected(self):
        for raw in ('Normal', 'weekly', 'x' * 50):
            with self.subTest(raw=raw):
                res = self.c.post(reverse('book_service'), {
                    'worker': self.worker.id, 'service': self.service.id, 'booking_type': raw,
                    'date': _future(5), 'time': '10:00-12:00', 'location': 'Kollam', 'phone': '9876543210',
                }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
                self.assertEqual(res.status_code, 400)
                self.assertEqual(Booking.objects.filter(customer=self.customer).count(), 0)

    def test_emergency_type_through_normal_endpoint_is_rejected(self):
        for raw in ('Emergency', 'emergency'):
            with self.subTest(raw=raw):
                res = self.c.post(reverse('book_service'), {
                    'worker': self.worker.id, 'service': self.service.id, 'booking_type': raw,
                    'date': _future(5), 'time': '10:00-12:00', 'location': 'Kollam', 'phone': '9876543210',
                }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
                self.assertEqual(res.status_code, 400)
                self.assertEqual(Booking.objects.filter(customer=self.customer).count(), 0)

    def test_dedicated_emergency_flow_still_works(self):
        res = self.c.post(reverse('book_service'), {
            'service': self.service.id, 'district': 'Kollam', 'problem': 'Burst pipe',
            'location': 'Kollam', 'phone': '9876543210', 'time': 'ASAP',
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 200)
        booking = Booking.objects.get(customer=self.customer)
        self.assertEqual((booking.booking_type, booking.status), ('Emergency', 'EMERGENCY_PENDING'))

    def test_guest_otp_pending_booking_with_bad_type_is_not_created(self):
        request = mock.Mock()
        pending = {'worker_id': self.worker.id, 'service_id': self.service.id, 'booking_type': 'Emergency',
                   'date': _future(5), 'time': '10:00-12:00', 'location': 'Kollam', 'phone': '9876543210'}
        with mock.patch('lamoapp.views.messages'):
            self.assertIsNone(views._create_booking_from_pending(request, self.customer, pending))
        self.assertEqual(Booking.objects.filter(customer=self.customer).count(), 0)

    def test_existing_recurring_series_stays_manageable(self):
        start = timezone.localdate() + timedelta(days=2)
        series = BookingSeries.objects.create(
            customer=self.customer, worker=self.worker, service=self.service, frequency='Daily',
            time='09:00 AM - 09:30 AM', location='Kollam', phone='9876543210',
            start_date=start, end_date=start + timedelta(days=2))
        Booking.objects.create(
            customer=self.customer, worker=self.worker, service=self.service, booking_type='Recurring',
            date=start, time='09:00 AM - 09:30 AM', location='Kollam', phone='9876543210',
            status='PENDING', estimated_cost=100, series=series)
        self.assertEqual(self.c.get(reverse('my_series')).status_code, 200)
        self.c.post(reverse('series_cancel', args=[series.id]))
        series.refresh_from_db()
        self.assertFalse(series.is_active)


class EmergencyStartRuleTests(_Base):

    def _emergency(self, date):
        return Booking.objects.create(
            customer=self.customer, worker=self.worker, service=self.service, booking_type='Emergency',
            date=date, time='ASAP', status='ACCEPTED', location='Kollam', phone='9876543210')

    def test_genuine_emergency_can_start_immediately(self):
        ok, _ = self._emergency(timezone.localdate()).check_can_start()
        self.assertTrue(ok)
        ok, _ = self._emergency(None).check_can_start()
        self.assertTrue(ok)

    def test_future_dated_emergency_gets_the_normal_early_start_check(self):
        ok, reason = self._emergency(timezone.localdate() + timedelta(days=4)).check_can_start()
        self.assertFalse(ok)
        self.assertIn('cannot be started early', reason)


# ===========================================================================
# TASK 7 - worker payment-method validation
# ===========================================================================
class WorkerPaymentMethodTests(_Base):

    def awaiting_payment(self):
        b = self.accepted_in_progress()
        self.bill(b, labor_amount='800')
        self.c.post(reverse('confirm_completion', args=[b.id]))
        b.refresh_from_db()
        return b

    def confirm(self, booking, **post):
        res = self.w.post(reverse('worker_confirm_payment_received', args=[booking.id]), post)
        booking.refresh_from_db()
        return res

    def test_cash_and_upi_still_complete_the_job(self):
        for method in ('CASH', 'UPI', 'cash', 'upi'):
            with self.subTest(method=method):
                b = self.awaiting_payment()
                self.confirm(b, payment_method=method)
                self.assertEqual(b.status, 'COMPLETED')
                self.assertEqual(b.payment_method, method.upper())
                payment = Payment.objects.get(booking=b)
                self.assertEqual(payment.status, 'PAID')
                self.assertEqual(payment.payment_method, method.upper())

    def test_card_netbanking_and_unknown_methods_are_rejected_not_converted(self):
        for method in ('CARD', 'NETBANKING', 'card', 'ONLINE', 'BITCOIN', 'upi;cash'):
            with self.subTest(method=method):
                b = self.awaiting_payment()
                res = self.confirm(b, payment_method=method)
                self.assertEqual(res.status_code, 302)
                self.assertEqual(b.status, 'COMPLETION_REQUESTED')
                self.assertFalse(b.is_paid)
                self.assertFalse(Payment.objects.filter(booking=b, status='PAID').exists())

    def test_rejection_message_is_shown(self):
        b = self.awaiting_payment()
        res = self.w.post(reverse('worker_confirm_payment_received', args=[b.id]), {'payment_method': 'CARD'}, follow=True)
        self.assertContains(res, 'Card and Netbanking payments are not supported')

    def test_missing_method_with_nothing_on_record_asks_for_a_choice(self):
        b = self.awaiting_payment()
        self.confirm(b)
        self.assertEqual(b.status, 'COMPLETION_REQUESTED')

    def test_missing_method_reuses_a_supported_method_already_on_record(self):
        b = self.awaiting_payment()
        Booking.objects.filter(pk=b.pk).update(payment_method='UPI')
        self.confirm(b)
        self.assertEqual(b.status, 'COMPLETED')
        self.assertEqual(b.payment_method, 'UPI')

    def test_worker_receipt_is_recorded_as_worker_reported_not_independently_verified(self):
        b = self.awaiting_payment()
        self.confirm(b, payment_method='UPI')
        payment = Payment.objects.get(booking=b)
        self.assertEqual(payment.gateway, 'direct_to_worker')      # worker-reported, never gateway/bank verified
        note = Notification.objects.filter(recipient=self.customer, booking=b).order_by('-id').first()
        self.assertNotIn('has been verified', note.message)

    def test_customer_path_still_rejects_card_and_netbanking(self):
        b = self.awaiting_payment()
        for method in ('CARD', 'NETBANKING'):
            self.c.post(reverse('confirm_payment', args=[b.id]), {'payment_method': method})
            self.assertFalse(Payment.objects.filter(booking=b).exists())

    def test_historical_card_and_netbanking_records_remain_accessible(self):
        admin = CustomUser.objects.create_user('hs_adm2', 'hs_a2@x.in', 'Pass123!x', role='ADMIN')
        for i, method in enumerate(('CARD', 'NETBANKING')):
            b = Booking.objects.create(
                customer=self.customer, worker=self.worker, service=self.service, status='COMPLETED',
                is_paid=True, payment_method=method, date=timezone.localdate() - timedelta(days=i + 1),
                time='10:00 AM - 10:30 AM', location='Kollam', phone='9876543210', estimated_cost=100)
            Payment.objects.create(booking=b, amount=100, payment_method=method, status='PAID',
                                   payment_reference=f'HIST-{method}', gateway='manual')
        self.assertEqual(self.c.get(reverse('my_bookings')).status_code, 200)
        admin_client = Client()
        admin_client.force_login(admin)
        res = admin_client.get(reverse('admin_payments'))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(Payment.objects.filter(payment_method__in=('CARD', 'NETBANKING')).count(), 2)

    def test_admin_verification_of_a_customer_report_still_works(self):
        admin = CustomUser.objects.create_user('hs_adm3', 'hs_a3@x.in', 'Pass123!x', role='ADMIN')
        b = self.awaiting_payment()
        self.c.post(reverse('confirm_payment', args=[b.id]), {'payment_method': 'UPI'})
        payment = Payment.objects.get(booking=b)
        self.assertEqual((payment.status, payment.gateway), ('PENDING', 'manual'))
        admin_client = Client()
        admin_client.force_login(admin)
        admin_client.post(reverse('admin_confirm_payment', args=[payment.id]))
        b.refresh_from_db()
        self.assertEqual(b.status, 'COMPLETED')
