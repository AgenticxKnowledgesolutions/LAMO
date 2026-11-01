from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, Client
from django.urls import reverse

from .models import CustomUser, Service, WorkerProfile, Booking
from .tests import _make_user


class BookingModalWizardTests(TestCase):
    """
    Tests for the 3-step mobile-first booking modal with inline email OTP:
    1. Ampersand '&' rendering without '\\u0026' escaping.
    2. Logged-in customer skips Step 3 (guest-only account details).
    3. Guest customer booking yields requires_otp with email in AJAX response.
    4. Inline OTP AJAX verification completes booking, creates account, and logs in.
    5. Maximum 5 invalid OTP attempts locks out and invalidates verification code.
    6. AJAX OTP resend resets attempts and issues a new code.
    7. Duplicate email handling prompts existing user to log in.
    8. Modal template structure: collapsible map, 6-digit OTP boxes, unchecked consent by default.
    """

    def setUp(self):
        self.client = Client()

        # Service with ampersand in the title
        self.service_with_amp = Service.objects.create(
            name="Paint & Polish Pro",
            description="Painting & wall finishing service",
            price=Decimal("400.00"),
            is_active=True,
        )

        # Worker for service
        self.worker_user = _make_user("painter_bob", role="WORKER")
        self.worker_user.email = "painter_bob@example.com"
        self.worker_user.phone = "9847111000"
        self.worker_user.is_email_verified = True
        self.worker_user.save()

        self.worker = WorkerProfile.objects.create(
            user=self.worker_user,
            service=self.service_with_amp,
            district="Kollam",
            work_status="AVAILABLE",
            is_verified=True,
            address="Color Street, Kollam",
        )

        # Registered customer
        self.customer = _make_user("sneha_test_cust", role="USER")
        self.customer.email = "sneha.reg@example.com"
        self.customer.phone = "9847222111"
        self.customer.is_email_verified = True
        self.customer.save()

        self.tomorrow = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")

    def test_service_with_ampersand_renders_cleanly_without_unicode_escape(self):
        """Service title containing '&' should render literal '&' in HTML and data attributes, not '\\u0026'."""
        url = reverse('service_workers', args=[self.service_with_amp.id])
        response = self.client.get(url, {'district': 'Kollam'})
        self.assertEqual(response.status_code, 200)

        content = response.content.decode('utf-8')

        # The service title must never contain JavaScript unicode escape '\u0026'
        self.assertNotIn(r'\u0026', content)
        self.assertIn('Paint &amp; Polish Pro', content)
        self.assertIn('data-service-name="Paint &amp; Polish Pro"', content)

    def test_modal_renders_two_steps_for_all_users_step3_removed(self):
        """All users (both logged in and unauthenticated) have exactly 2 steps; Step 3 is completely removed."""
        url = reverse('service_workers', args=[self.service_with_amp.id])
        response = self.client.get(url, {'district': 'Kollam'})
        self.assertEqual(response.status_code, 200)

        content = response.content.decode('utf-8')
        # Step 3 panel and node should NEVER be rendered
        self.assertNotIn('id="bookingStep3"', content)
        self.assertNotIn('id="wizardNode3"', content)
        # Exactly Step 1 and Step 2 should be present
        self.assertIn('id="bookingStep1"', content)
        self.assertIn('id="bookingStep2"', content)
        self.assertIn('id="btnWizardConfirm"', content)

        # Logged-in user also has exactly 2 steps
        self.client.force_login(self.customer)
        resp_auth = self.client.get(url, {'district': 'Kollam'})
        content_auth = resp_auth.content.decode('utf-8')
        self.assertNotIn('id="bookingStep3"', content_auth)
        self.assertNotIn('id="wizardNode3"', content_auth)
        self.assertIn('id="bookingStep1"', content_auth)
        self.assertIn('id="bookingStep2"', content_auth)

    def test_guest_modal_renders_auth_popup_and_prefill_inputs(self):
        """Modal contains the unified auth popup, legal links, and no consent checkbox."""
        url = reverse('service_workers', args=[self.service_with_amp.id])
        response = self.client.get(url, {'district': 'Kollam'})
        self.assertEqual(response.status_code, 200)

        content = response.content.decode('utf-8')
        self.assertIn('id="bookingAuthPopup"', content)
        self.assertIn('id="authIdentifierInput"', content)
        self.assertIn('id="btnAuthSendCode"', content)
        self.assertIn('/terms/', content)
        self.assertIn('/privacy/', content)
        self.assertNotIn('id="modalAccountConsent"', content)

    def test_modal_has_collapsible_map_and_six_otp_digit_inputs(self):
        """Modal template includes collapsible map and 6 code boxes in the auth popup."""
        url = reverse('service_workers', args=[self.service_with_amp.id])
        response = self.client.get(url, {'district': 'Kollam'})
        content = response.content.decode('utf-8')

        self.assertIn('id="btnToggleBookingMap"', content)
        self.assertIn('id="bookingMapCollapse"', content)
        self.assertIn('id="modalBookingMap"', content)
        # 6 digit code boxes in auth popup
        self.assertEqual(content.count('class="auth-code-box"'), 6)
        self.assertIn('id="btnAuthVerify"', content)

    @patch("lamoapp.views.send_email_safely")
    def test_guest_submission_returns_requires_otp_with_email_and_zero_db_writes(self, mock_send_email):
        """Guest booking form submission returns requires_otp=True and writes 0 rows to DB."""
        mock_send_email.return_value = True

        users_before = CustomUser.objects.count()
        bookings_before = Booking.objects.count()

        payload = {
            "worker": self.worker.id,
            "service": self.service_with_amp.id,
            "booking_type": "One Time",
            "date": self.tomorrow,
            "time_slot": "10:00 AM - 12:00 PM",
            "time": "10:00 AM - 12:00 PM",
            "district": "Kollam",
            "location": "Residency Road, Kollam",
            "latitude": "8.8932",
            "longitude": "76.6141",
            "phone": "9847555666",
            "customer_name": "Rohan Das",
            "customer_email": "rohan.guest@example.com",
            "payment_method": "ONLINE",
        }

        resp = self.client.post(
            reverse("book_service"),
            payload,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("success"))
        self.assertTrue(data.get("requires_otp"))
        self.assertEqual(data.get("email"), "rohan.guest@example.com")

        # STRICT CONSTRAINT: Zero DB writes before verification
        self.assertEqual(CustomUser.objects.count(), users_before)
        self.assertEqual(Booking.objects.count(), bookings_before)
        self.assertIn("pending_booking_data", self.client.session)

    @patch("lamoapp.views.send_email_safely")
    def test_inline_otp_verification_creates_booking_and_signs_in(self, mock_send_email):
        """Submitting valid code via verify_otp creates account, logs in, and creates PENDING booking."""
        mock_send_email.return_value = True

        payload = {
            "worker": self.worker.id,
            "service": self.service_with_amp.id,
            "booking_type": "One Time",
            "date": self.tomorrow,
            "time_slot": "10:00 AM - 12:00 PM",
            "time": "10:00 AM - 12:00 PM",
            "district": "Kollam",
            "location": "Residency Road, Kollam",
            "phone": "9847555666",
            "customer_name": "Rohan Das",
            "customer_email": "rohan.verify@example.com",
            "payment_method": "ONLINE",
        }

        # Step 1: Submit booking form to stage pending booking in session and send code
        step1_resp = self.client.post(
            reverse("book_service"),
            payload,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )
        self.assertEqual(step1_resp.status_code, 200)
        self.assertTrue(step1_resp.json().get("requires_otp"))
        self.assertEqual(CustomUser.objects.filter(email="rohan.verify@example.com").count(), 0)
        self.assertEqual(Booking.objects.filter(service=self.service_with_amp).count(), 0)

        # Retrieve the generated code from mock email call
        self.assertTrue(mock_send_email.called)
        last_message = mock_send_email.call_args[1].get('message', '')
        import re
        match = re.search(r'\b\d{6}\b', last_message)
        self.assertIsNotNone(match)
        sent_code = match.group(0)

        # Step 2: Verify code
        otp_resp = self.client.post(
            reverse("verify_otp"),
            {"action": "verify", "otp": sent_code},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )
        self.assertEqual(otp_resp.status_code, 200)
        otp_data = otp_resp.json()
        self.assertTrue(otp_data.get("success"))
        self.assertEqual(otp_data.get("redirect_url"), reverse("my_bookings"))

        # User is created & verified, booking is finalized
        guest_user = CustomUser.objects.get(email="rohan.verify@example.com")
        self.assertTrue(guest_user.is_email_verified)
        booking = Booking.objects.get(service=self.service_with_amp)
        self.assertEqual(booking.customer, guest_user)
        self.assertEqual(booking.worker, self.worker)
        self.assertEqual(booking.status, "PENDING")
        self.assertTrue(booking.arrival_otp)

    @patch("lamoapp.views.send_email_safely")
    def test_existing_user_unified_flow_logs_in_without_duplicate_row(self, mock_send_email):
        """Unified flow logs in existing users upon code verification and keeps existing account details."""
        mock_send_email.return_value = True

        users_before = CustomUser.objects.count()

        # Existing customer email with different contact name/phone in booking form
        payload = {
            "worker": self.worker.id,
            "service": self.service_with_amp.id,
            "booking_type": "One Time",
            "date": self.tomorrow,
            "time_slot": "10:00 AM - 12:00 PM",
            "time": "10:00 AM - 12:00 PM",
            "district": "Kollam",
            "location": "Residency Road, Kollam",
            "phone": "9847999111",
            "customer_name": "Different Name",
            "customer_email": "sneha.reg@example.com",
            "payment_method": "ONLINE",
        }

        # Step 1: Stage booking and send code
        step1_resp = self.client.post(reverse("book_service"), payload, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(step1_resp.status_code, 200)
        self.assertTrue(step1_resp.json().get("requires_otp"))

        import re
        last_message = mock_send_email.call_args[1].get('message', '')
        sent_code = re.search(r'\b\d{6}\b', last_message).group(0)

        # Step 2: Verify code
        otp_resp = self.client.post(
            reverse("verify_otp"),
            {"action": "verify", "otp": sent_code},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )
        self.assertEqual(otp_resp.status_code, 200)

        # No new user row created
        self.assertEqual(CustomUser.objects.count(), users_before)

        # Booking created for existing user
        booking = Booking.objects.get(service=self.service_with_amp)
        self.assertEqual(booking.customer, self.customer)
        self.assertEqual(booking.phone, "9847999111")  # Booking-specific phone preserved

    def test_unauthenticated_mobile_input_displays_coming_soon_and_creates_zero_rows(self):
        """Submitting mobile number returns 'Mobile OTP is coming soon, please use your email' and creates 0 rows."""
        users_before = CustomUser.objects.count()
        bookings_before = Booking.objects.count()

        resp = self.client.post(
            reverse("verify_otp"),
            {"action": "send_code", "identifier": "9847555666"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertFalse(data.get("success"))
        self.assertEqual(data.get("error_message"), "Mobile OTP is coming soon, please use your email.")

        # Zero DB rows created
        self.assertEqual(CustomUser.objects.count(), users_before)
        self.assertEqual(Booking.objects.count(), bookings_before)

    @patch("lamoapp.views.send_email_safely")
    def test_otp_attempt_cap_and_lockout(self, mock_send_email):
        """5 invalid OTP attempts invalidates code and locks out in session."""
        mock_send_email.return_value = True

        # Send code
        self.client.post(
            reverse("verify_otp"),
            {"action": "send_code", "identifier": "rohan.lock@example.com"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )

        # Send 4 wrong attempts
        for attempt in range(1, 5):
            resp = self.client.post(
                reverse("verify_otp"),
                {"action": "verify", "otp": "000000"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest"
            )
            self.assertEqual(resp.status_code, 400)
            data = resp.json()
            self.assertFalse(data.get("success"))
            self.assertEqual(data.get("remaining_attempts"), 5 - attempt)

        # 5th wrong attempt locks out and invalidates OTP
        resp5 = self.client.post(
            reverse("verify_otp"),
            {"action": "verify", "otp": "000000"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )
        self.assertEqual(resp5.status_code, 400)
        data5 = resp5.json()
        self.assertTrue(data5.get("locked_out"))
        self.assertTrue(data5.get("requires_resend"))

    @patch("lamoapp.views.send_email_safely")
    def test_ajax_otp_resend_cooldown_and_success(self, mock_send_email):
        """AJAX resend action enforces 30s cooldown and sends new code after cooldown."""
        mock_send_email.return_value = True

        self.client.post(
            reverse("verify_otp"),
            {"action": "send_code", "identifier": "rohan.resend@example.com"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )

        # Early resend attempt (< 30s) is rate limited with 400
        early_resp = self.client.post(
            reverse("verify_otp"),
            {"action": "resend"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )
        self.assertEqual(early_resp.status_code, 400)
        self.assertIn("wait", early_resp.json().get("error_message", "").lower())

        # Advance timestamp by 35s to satisfy cooldown
        session = self.client.session
        auth_data = session.get("booking_auth_data", {})
        from datetime import datetime, timezone as dt_tz
        from django.utils import timezone
        auth_data["created_at"] = (timezone.now() - timedelta(seconds=35)).isoformat()
        session["booking_auth_data"] = auth_data
        session.save()

        # Resend code succeeds
        resend_resp = self.client.post(
            reverse("verify_otp"),
            {"action": "resend"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )
        self.assertEqual(resend_resp.status_code, 200)
        resend_data = resend_resp.json()
        self.assertTrue(resend_data.get("success"))

    @patch("lamoapp.views.send_email_safely")
    def test_expired_code_is_rejected(self, mock_send_email):
        """Codes older than 10 minutes are rejected with expiration message."""
        mock_send_email.return_value = True

        self.client.post(
            reverse("verify_otp"),
            {"action": "send_code", "identifier": "rohan.expire@example.com"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )

        # Simulate 11 minutes elapsed
        from django.utils import timezone
        session = self.client.session
        auth_data = session.get("booking_auth_data", {})
        auth_data["created_at"] = (timezone.now() - timedelta(minutes=11)).isoformat()
        session["booking_auth_data"] = auth_data
        session.save()

        resp = self.client.post(
            reverse("verify_otp"),
            {"action": "verify", "otp": "123456"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("expired", resp.json().get("error_message", "").lower())


class BookingSecurityAndPrivacyRegressionTests(TestCase):
    """Regression tests for Step 3 high-priority security and privacy enhancements."""

    def setUp(self):
        self.client = Client()
        self.service = Service.objects.create(
            name="Electrical Repair",
            description="Home electrical repairs",
            price=Decimal("350.00"),
            is_active=True,
        )
        self.worker_user = _make_user("electrician_al", role="WORKER")
        self.worker_user.email = "al.electric@example.com"
        self.worker_user.phone = "9847333222"
        self.worker_user.is_email_verified = True
        self.worker_user.save()

        self.worker = WorkerProfile.objects.create(
            user=self.worker_user,
            service=self.service,
            district="Ernakulam",
            work_status="AVAILABLE",
            is_verified=True,
            address="Aluva, Ernakulam",
        )

        self.customer = _make_user("priv_customer", role="USER")
        self.customer.email = "priv.customer@example.com"
        self.customer.phone = "9847444333"
        self.customer.is_email_verified = True
        self.customer.save()

    def test_incomplete_otp_does_not_consume_attempts(self):
        """Incomplete or non-digit OTP inputs are rejected without burning attempts."""
        with patch("lamoapp.views.send_email_safely", return_value=True):
            self.client.post(
                reverse("verify_otp"),
                {"action": "send_code", "identifier": "tester.otp@example.com"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest"
            )

        # Enter partial 3-digit code
        resp = self.client.post(
            reverse("verify_otp"),
            {"action": "verify", "otp": "123"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("complete 6-digit", resp.json().get("error_message", "").lower())

        # Session attempts counter must remain 0
        session = self.client.session
        auth_data = session.get("booking_auth_data", {})
        self.assertEqual(auth_data.get("attempts", 0), 0)

    def test_address_privacy_masked_until_booking_accepted(self):
        """Customer doorstep address is masked on pending jobs and revealed after acceptance."""
        secret_address = "Flat 9A, Skyline Heights, Marine Drive, Kochi"
        booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker,
            service=self.service,
            district="Ernakulam",
            date=date.today() + timedelta(days=2),
            time="10:00 AM",
            location=secret_address,
            phone="9847444333",
            status="PENDING",
        )

        self.client.force_login(self.worker_user)
        resp = self.client.get(reverse("worker_jobs"))
        self.assertEqual(resp.status_code, 200)

        # Pending booking: exact secret address must be masked
        html = resp.content.decode("utf-8")
        self.assertNotIn("Flat 9A, Skyline Heights", html)
        self.assertIn("Address revealed on accept", html)

        # Worker accepts booking
        accept_resp = self.client.post(
            reverse("worker_accept_booking", args=[booking.id]),
            follow=True
        )
        self.assertEqual(accept_resp.status_code, 200)

        # Accepted booking: secret doorstep address is now unlocked for navigation
        booking.refresh_from_db()
        self.assertEqual(booking.status, "ACCEPTED")
        self.assertTrue(bool(booking.arrival_otp))

        resp_accepted = self.client.get(reverse("worker_jobs"))
        html_accepted = resp_accepted.content.decode("utf-8")
        self.assertIn(secret_address, html_accepted)

    def test_concurrent_or_cancelled_booking_acceptance_prevented(self):
        """Worker cannot accept a booking that has already been cancelled by customer."""
        booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker,
            service=self.service,
            district="Ernakulam",
            date=date.today() + timedelta(days=3),
            time="11:00 AM",
            location="MG Road, Ernakulam",
            phone="9847444333",
            status="PENDING",
        )

        # Customer cancels booking
        self.client.force_login(self.customer)
        cancel_resp = self.client.post(
            reverse("cancel_booking", args=[booking.id]),
            {"cancellation_reason": "Need to reschedule"}
        )
        self.assertEqual(cancel_resp.status_code, 302)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "CANCELLED")

        # Worker tries to accept the now-cancelled booking
        self.client.force_login(self.worker_user)
        accept_resp = self.client.post(
            reverse("worker_accept_booking", args=[booking.id]),
            follow=True
        )
        self.assertEqual(accept_resp.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "CANCELLED")  # Must remain cancelled!

