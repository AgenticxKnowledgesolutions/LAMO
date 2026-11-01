from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from .models import CustomUser, UserProfile, Service, WorkerProfile, Booking, Notification
from .tests import _make_user, _make_worker


class GuestAccountCreationFlowTests(TestCase):
    """
    Comprehensive tests for Phase 5:
    First-time customer account creation flow during booking,
    email OTP verification, security boundaries, collision prevention,
    returning customer prefill, and preservation of worker workflows.
    """

    def setUp(self):
        self.client = Client()

        # Create active service
        self.service = Service.objects.create(
            name="Carpentry Works",
            description="Furniture and woodwork repair",
            price=Decimal("450.00"),
            is_active=True,
        )

        # Create verified available worker
        self.worker_user = _make_user("carpenter_pro", role="WORKER")
        self.worker_user.email = "carpenter_pro@example.com"
        self.worker_user.phone = "9847111222"
        self.worker_user.is_email_verified = True
        self.worker_user.save()

        self.worker = WorkerProfile.objects.create(
            user=self.worker_user,
            service=self.service,
            district="Kollam",
            work_status="AVAILABLE",
            is_verified=True,
            address="Wood Street, Kollam",
        )

        # Existing verified customer
        self.existing_customer = _make_user("sneha_customer", role="USER")
        self.existing_customer.email = "sneha_cust@example.com"
        self.existing_customer.first_name = "Sneha"
        self.existing_customer.last_name = "R"
        self.existing_customer.is_email_verified = True
        self.existing_customer.save()

        self.existing_profile, _ = UserProfile.objects.get_or_create(user=self.existing_customer)
        self.existing_profile.phone = "9847333444"
        self.existing_profile.address = "Civil Station Road, Kollam"
        self.existing_profile.district = "Kollam"
        self.existing_profile.latitude = Decimal("8.8932")
        self.existing_profile.longitude = Decimal("76.6141")
        self.existing_profile.save()

        # Admin user
        self.admin = _make_user("admin_manager", role="ADMIN")
        self.admin.email = "admin_manager@example.com"
        self.admin.is_email_verified = True
        self.admin.save()

        # Default valid booking date (tomorrow)
        self.tomorrow = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")

    @patch("lamoapp.views.send_email_safely")
    def test_guest_booking_stages_in_session_with_zero_db_writes(self, mock_send_email):
        """1. Guest booking stages pending booking in session and sends code with 0 DB writes."""
        mock_send_email.return_value = True

        users_before = CustomUser.objects.count()
        bookings_before = Booking.objects.count()

        payload = {
            "worker": self.worker.id,
            "service": self.service.id,
            "booking_type": "One Time",
            "date": self.tomorrow,
            "time_slot": "10:00 AM - 12:00 PM",
            "time": "10:00 AM - 12:00 PM",
            "district": "Kollam",
            "location": "Beach Road, Kollam",
            "latitude": "8.8853",
            "longitude": "76.5864",
            "phone": "9847999888",
            "customer_name": "Aarav Sharma",
            "customer_email": "aarav.guest@example.com",
            "payment_method": "ONLINE",
        }

        resp = self.client.post(reverse("book_service"), payload, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("success"))
        self.assertTrue(data.get("requires_otp"))
        self.assertEqual(data.get("redirect_url"), reverse("verify_otp"))

        # Zero DB writes before verification code is verified
        self.assertEqual(CustomUser.objects.count(), users_before)
        self.assertEqual(Booking.objects.count(), bookings_before)
        self.assertIn("pending_booking_data", self.client.session)
        mock_send_email.assert_called_once()

    def test_unauthenticated_booking_without_name_rejected(self):
        """2. Unauthenticated booking without a full customer name is rejected."""
        payload = {
            "worker": self.worker.id,
            "service": self.service.id,
            "booking_type": "One Time",
            "date": self.tomorrow,
            "time_slot": "10:00 AM - 12:00 PM",
            "time": "10:00 AM - 12:00 PM",
            "district": "Kollam",
            "location": "Beach Road, Kollam",
            "phone": "9847999777",
            "customer_name": "",
            "customer_email": "noname@example.com",
        }

        resp = self.client.post(reverse("book_service"), payload, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertFalse(data.get("success"))
        self.assertIn("full name", data.get("error_message", "").lower())
        self.assertFalse(CustomUser.objects.filter(email="noname@example.com").exists())
        self.assertEqual(Booking.objects.count(), 0)

    @patch("lamoapp.views.send_email_safely")
    def test_successful_email_otp_verification_creates_booking_and_logs_in(self, mock_email):
        """3. Entering valid OTP verifies account, finalizes booking, logs in customer."""
        # Setup guest user and session
        guest = CustomUser.objects.create_user(
            username="aarav_guest_test",
            email="aarav_test@example.com",
            first_name="Aarav",
            last_name="Test",
            role="USER",
            is_email_verified=False,
        )
        guest.email_verification_otp = "654321"
        guest.otp_created_at = timezone.now()
        guest.save()

        UserProfile.objects.create(user=guest, phone="9847112233", address="Kollam Town")

        session = self.client.session
        session["otp_user_id"] = guest.id
        session["pending_booking_data"] = {
            "worker_id": self.worker.id,
            "service_id": self.service.id,
            "booking_type": "One Time",
            "date": self.tomorrow,
            "time": "10:00 AM - 12:00 PM",
            "district": "Kollam",
            "location": "Beach Road, Kollam",
            "latitude": "8.8853",
            "longitude": "76.5864",
            "phone": "9847112233",
            "description": "Table leg broken",
            "estimated_cost": "450.00",
            "payment_method": "ONLINE",
        }
        session.save()

        # Post OTP verification
        resp = self.client.post(reverse("verify_otp"), {
            "action": "verify",
            "otp": "654321",
        })
        self.assertRedirects(resp, reverse("my_bookings"))

        # Verify account activated and logged in
        guest.refresh_from_db()
        self.assertTrue(guest.is_email_verified)
        self.assertEqual(int(self.client.session.get("_auth_user_id")), guest.id)

        # Booking created
        booking = Booking.objects.filter(customer=guest).first()
        self.assertIsNotNone(booking)
        self.assertEqual(booking.worker, self.worker)
        self.assertEqual(booking.service, self.service)
        self.assertEqual(booking.phone, "9847112233")
        self.assertEqual(booking.location, "Beach Road, Kollam")
        self.assertTrue(booking.arrival_otp)  # Arrival OTP generated

        # Session pending data cleared
        self.assertNotIn("pending_booking_data", self.client.session)

        # Notifications sent to worker and admin
        self.assertTrue(Notification.objects.filter(recipient=self.worker.user, booking=booking).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.admin, booking=booking).exists())

    def test_incorrect_and_expired_otp_rejects_and_prevents_booking(self):
        """4. Incorrect or expired OTP does not verify user or create booking."""
        guest = CustomUser.objects.create_user(
            username="fail_guest",
            email="fail_guest@example.com",
            role="USER",
            is_email_verified=False,
        )
        guest.email_verification_otp = "112233"
        guest.otp_created_at = timezone.now()
        guest.save()

        session = self.client.session
        session["otp_user_id"] = guest.id
        session["pending_booking_data"] = {
            "worker_id": self.worker.id,
            "service_id": self.service.id,
            "booking_type": "One Time",
            "date": self.tomorrow,
            "time": "10:00 AM - 12:00 PM",
            "district": "Kollam",
            "location": "Fail Road",
            "phone": "9847000111",
        }
        session.save()

        # Submit wrong OTP
        resp = self.client.post(reverse("verify_otp"), {
            "action": "verify",
            "otp": "999999",
        })
        self.assertEqual(resp.status_code, 200)
        guest.refresh_from_db()
        self.assertFalse(guest.is_email_verified)
        self.assertEqual(Booking.objects.filter(customer=guest).count(), 0)

        # Submit expired OTP (older than 10 minutes)
        guest.otp_created_at = timezone.now() - timedelta(minutes=15)
        guest.save()

        resp_expired = self.client.post(reverse("verify_otp"), {
            "action": "verify",
            "otp": "112233",
        })
        self.assertEqual(resp_expired.status_code, 200)
        guest.refresh_from_db()
        self.assertFalse(guest.is_email_verified)
        self.assertEqual(Booking.objects.filter(customer=guest).count(), 0)

    @patch("lamoapp.views.send_email_safely")
    def test_existing_customer_unified_flow_sends_code_and_logs_in(self, mock_send_email):
        """5. Unauthenticated booking with existing customer email sends code and logs in on verification."""
        mock_send_email.return_value = True

        users_before = CustomUser.objects.count()

        payload = {
            "worker": self.worker.id,
            "service": self.service.id,
            "booking_type": "One Time",
            "date": self.tomorrow,
            "time_slot": "10:00 AM - 12:00 PM",
            "time": "10:00 AM - 12:00 PM",
            "district": "Kollam",
            "location": "Beach Road",
            "phone": "9847111333",
            "customer_name": "Sneha R",
            "customer_email": self.existing_customer.email,  # Existing verified USER
            "payment_method": "ONLINE",
        }

        resp = self.client.post(reverse("book_service"), payload, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("success"))
        self.assertTrue(data.get("requires_otp"))

        # Zero new users created
        self.assertEqual(CustomUser.objects.count(), users_before)

        # Verify code
        import re
        last_message = mock_send_email.call_args[1].get('message', '')
        sent_code = re.search(r'\b\d{6}\b', last_message).group(0)

        verify_resp = self.client.post(
            reverse("verify_otp"),
            {"action": "verify", "otp": sent_code},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )
        self.assertEqual(verify_resp.status_code, 200)
        self.assertEqual(CustomUser.objects.count(), users_before)
        self.assertEqual(Booking.objects.filter(customer=self.existing_customer).count(), 1)

    def test_worker_and_admin_email_collision_blocked(self):
        """6. Guest booking with worker or admin email is strictly blocked."""
        # Worker email
        payload_worker = {
            "worker": self.worker.id,
            "service": self.service.id,
            "booking_type": "One Time",
            "date": self.tomorrow,
            "time_slot": "10:00 AM - 12:00 PM",
            "district": "Kollam",
            "location": "Beach Road",
            "phone": "9847111444",
            "customer_name": "Worker Person",
            "customer_email": self.worker_user.email,
            "create_account_consent": "1",
        }
        resp = self.client.post(reverse("book_service"), payload_worker, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("worker or admin", resp.json().get("error_message", "").lower())

        # Admin email
        payload_admin = dict(payload_worker, customer_email=self.admin.email)
        resp2 = self.client.post(reverse("book_service"), payload_admin, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp2.status_code, 400)
        self.assertIn("worker or admin", resp2.json().get("error_message", "").lower())

    def test_phone_number_collision_prevention(self):
        """7. Guest entering a phone already assigned to another user is rejected."""
        payload = {
            "worker": self.worker.id,
            "service": self.service.id,
            "booking_type": "One Time",
            "date": self.tomorrow,
            "time_slot": "10:00 AM - 12:00 PM",
            "district": "Kollam",
            "location": "Beach Road",
            "phone": self.existing_profile.phone,  # Belonging to existing customer
            "customer_name": "New Guest",
            "customer_email": "brand_new_guest@example.com",
            "create_account_consent": "1",
        }
        resp = self.client.post(reverse("book_service"), payload, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("already associated with an existing account", resp.json().get("error_message", "").lower())

    @patch("lamoapp.views.send_email_safely")
    def test_prevent_duplicate_booking_on_repeated_verification_or_refresh(self, mock_email):
        """8. Repeating verification or refreshing does not create duplicate bookings."""
        guest = CustomUser.objects.create_user(
            username="single_booking_guest",
            email="single_booking@example.com",
            role="USER",
            is_email_verified=False,
        )
        guest.email_verification_otp = "445566"
        guest.otp_created_at = timezone.now()
        guest.save()

        UserProfile.objects.create(user=guest, phone="9847555666", address="Kollam")

        session = self.client.session
        session["otp_user_id"] = guest.id
        session["pending_booking_data"] = {
            "worker_id": self.worker.id,
            "service_id": self.service.id,
            "booking_type": "One Time",
            "date": self.tomorrow,
            "time": "10:00 AM - 12:00 PM",
            "district": "Kollam",
            "location": "Beach Road",
            "phone": "9847555666",
        }
        session.save()

        # First verification succeeds and creates booking
        resp1 = self.client.post(reverse("verify_otp"), {
            "action": "verify",
            "otp": "445566",
        })
        self.assertRedirects(resp1, reverse("my_bookings"))
        self.assertEqual(Booking.objects.filter(customer=guest).count(), 1)

        # Refresh / second verification attempt does not create another booking
        resp2 = self.client.post(reverse("verify_otp"), {
            "action": "verify",
            "otp": "445566",
        })
        self.assertEqual(Booking.objects.filter(customer=guest).count(), 1)

    def test_booking_ownership_and_customer_role_enforcement(self):
        """9. Only USER role can book; WORKER and ADMIN cannot book."""
        # Worker attempt
        self.client.login(username=self.worker_user.username, password="Password123!")
        resp_worker = self.client.post(reverse("book_service"), {
            "worker": self.worker.id,
            "service": self.service.id,
        })
        self.assertEqual(resp_worker.status_code, 302)
        self.assertEqual(resp_worker.url, reverse("dashboard"))

        # Admin attempt
        self.client.logout()
        self.client.login(username=self.admin.username, password="Password123!")
        resp_admin = self.client.post(reverse("book_service"), {
            "worker": self.worker.id,
            "service": self.service.id,
        })
        self.assertEqual(resp_admin.status_code, 302)
        self.assertEqual(resp_admin.url, reverse("dashboard"))

    @patch("lamoapp.views.send_email_safely")
    def test_existing_authenticated_customer_booking(self, mock_email):
        """10. Authenticated customer books directly without OTP staging."""
        self.client.login(username=self.existing_customer.username, password="Password123!")

        payload = {
            "worker": self.worker.id,
            "service": self.service.id,
            "booking_type": "One Time",
            "date": self.tomorrow,
            "time_slot": "10:00 AM - 12:00 PM",
            "time": "10:00 AM - 12:00 PM",
            "district": "Kollam",
            "location": "Updated House Address, Kollam",
            "phone": "9847333444",
            "payment_method": "ONLINE",
        }

        resp = self.client.post(reverse("book_service"), payload, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("redirect_url"), reverse("my_bookings"))

        # Booking created immediately
        booking = Booking.objects.filter(customer=self.existing_customer).first()
        self.assertIsNotNone(booking)
        self.assertEqual(booking.location, "Updated House Address, Kollam")

        # UserProfile updated with new address
        self.existing_profile.refresh_from_db()
        self.assertEqual(self.existing_profile.address, "Updated House Address, Kollam")

    def test_returning_customer_saved_details_prefilled(self):
        """11. Returning authenticated customer has saved details populated in GET contexts."""
        self.client.login(username=self.existing_customer.username, password="Password123!")

        # Check book_service GET
        resp_book = self.client.get(f"{reverse('book_service')}?worker={self.worker.id}&service={self.service.id}")
        self.assertEqual(resp_book.status_code, 200)
        self.assertIn("customer_prefill", resp_book.context)
        prefill = resp_book.context["customer_prefill"]
        self.assertEqual(prefill["phone"], self.existing_profile.phone)
        self.assertEqual(prefill["location"], self.existing_profile.address)

        # Check service_workers GET
        resp_workers = self.client.get(f"{reverse('service_workers', args=[self.service.id])}?district=Kollam")
        self.assertEqual(resp_workers.status_code, 200)
        self.assertIn("customer_prefill", resp_workers.context)
        self.assertEqual(resp_workers.context["customer_prefill"]["phone"], self.existing_profile.phone)

    @patch("lamoapp.views.send_otp_email")
    def test_worker_registration_and_dashboard_remain_unchanged(self, mock_send_otp):
        """12. Worker registration and workflows remain 100% intact."""
        mock_send_otp.return_value = ("123456", True)

        # Worker registration GET
        resp_reg_get = self.client.get(reverse("register") + "?role=worker")
        self.assertEqual(resp_reg_get.status_code, 200)

        from django.core.files.uploadedfile import SimpleUploadedFile
        proof_doc = SimpleUploadedFile(
            "id_proof.jpg",
            b"\xFF\xD8\xFF\xE0\x00\x10JFIF" + b"\x00" * 30,
            content_type="image/jpeg",
        )

        # Worker registration POST with all required fields
        reg_payload = {
            "role": "WORKER",
            "username": "new_carpenter_worker",
            "email": "new_carpenter@example.com",
            "password": "Password123!",
            "confirm_password": "Password123!",
            "phone": "9847888999",
            "service": str(self.service.id),
            "experience_years": "5",
            "district": "Kollam",
            "availability": "Full Time",
            "pricing_type": "hour",
            "pricing_amount": "350.00",
            "address": "123 Workshop Road, Kollam Town, Kerala",
            "proof_type": "aadhaar",
            "proof_document": proof_doc,
        }
        resp_reg_post = self.client.post(reverse("register"), reg_payload)
        # Worker registration redirects to verify_otp
        self.assertRedirects(resp_reg_post, reverse("verify_otp"))

        # Verify created as unverified WORKER with WorkerProfile
        new_worker_user = CustomUser.objects.filter(username="new_carpenter_worker").first()
        self.assertIsNotNone(new_worker_user)
        self.assertEqual(new_worker_user.role, "WORKER")
        self.assertFalse(new_worker_user.is_email_verified)

        wprofile = WorkerProfile.objects.filter(user=new_worker_user).first()
        self.assertIsNotNone(wprofile)
        self.assertEqual(wprofile.service, self.service)
        self.assertEqual(wprofile.district, "Kollam")

        # Worker login redirects to worker dashboard
        self.worker_user.is_email_verified = True
        self.worker_user.save()
        resp_login = self.client.post(reverse("login"), {
            "username": self.worker_user.username,
            "password": "Password123!",
        })
        self.assertRedirects(resp_login, reverse("worker_dashboard"))
