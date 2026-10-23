from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase, Client
from django.urls import reverse

from .models import CustomUser, Service, WorkerProfile
from .tests import _make_user, _make_worker


class GuestFlowTests(TestCase):
    """Tests for the guest-first booking flow."""

    def setUp(self):
        self.client = Client()
        self.service1 = Service.objects.create(
            name="Plumbing Service",
            description="Fix pipes and taps",
            price=Decimal("500.00"),
            is_active=True,
        )
        self.service2 = Service.objects.create(
            name="Electrical Service",
            description="Wiring and fixtures",
            price=Decimal("400.00"),
            is_active=True,
        )

        # Workers in different districts & statuses
        self.kollam_worker_user = _make_user("kollam_worker", role="WORKER")
        self.kollam_worker_user.phone = "9876543210"
        self.kollam_worker_user.email = "secret_kollam@example.com"
        self.kollam_worker_user.is_email_verified = True
        self.kollam_worker_user.save()
        self.kollam_worker = WorkerProfile.objects.create(
            user=self.kollam_worker_user,
            service=self.service1,
            district="Kollam",
            work_status="AVAILABLE",
            is_verified=True,
            address="123 Private Street, Kollam",
        )

        # Worker in Ernakulam
        self.ernakulam_worker = _make_worker("ernakulam_worker", self.service2, district="Ernakulam", status="AVAILABLE")

        # Unverified worker in Kollam for service2
        self.unverified_worker_user = _make_user("unverified_worker", role="WORKER")
        self.unverified_worker = WorkerProfile.objects.create(
            user=self.unverified_worker_user,
            service=self.service2,
            district="Kollam",
            work_status="AVAILABLE",
            is_verified=False,
        )

        # Unavailable worker in Kollam for service2
        self.unavailable_worker_user = _make_user("unavailable_worker", role="WORKER")
        self.unavailable_worker = WorkerProfile.objects.create(
            user=self.unavailable_worker_user,
            service=self.service2,
            district="Kollam",
            work_status="UNAVAILABLE",
            is_verified=True,
        )

        self.customer = _make_user("customer_user", role="USER")
        self.customer.is_email_verified = True
        self.customer.save()

        self.admin = _make_user("admin_user", role="ADMIN")
        self.admin.is_email_verified = True
        self.admin.save()

    def test_login_next_stores_safe_path_and_drops_unsafe(self):
        """GET /login/?next=<path> stores safe paths in session; unsafe values are dropped."""
        safe_path = f"/services/{self.service1.id}/workers/?district=Kollam"

        # Safe path
        resp = self.client.get(reverse("login") + f"?next={safe_path}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.session.get("registration_next"), safe_path)

        # Unsafe values
        for unsafe in ["//evil.com", "https://evil.com", "/\\evil", "javascript:alert(1)"]:
            resp = self.client.get(reverse("login") + f"?next={unsafe}")
            self.assertEqual(resp.status_code, 200)
            self.assertNotIn("registration_next", self.client.session)

    def test_login_redirects_user_to_next_and_consumes_key(self):
        """A USER login redirects to the stored next path and consumes the session key."""
        target_path = f"/services/{self.service1.id}/workers/?district=Kollam"
        # Seed the next parameter
        self.client.get(reverse("login") + f"?next={target_path}")
        self.assertEqual(self.client.session.get("registration_next"), target_path)

        # Log in as customer
        resp = self.client.post(reverse("login"), {
            "username": self.customer.username,
            "password": "Password123!",
        })
        self.assertRedirects(resp, target_path)
        # Session key should be consumed
        self.assertNotIn("registration_next", self.client.session)

    def test_admin_login_with_next_still_redirects_to_admin_dashboard(self):
        """An ADMIN login always redirects to admin_dashboard even if next was present."""
        target_path = f"/services/{self.service1.id}/workers/?district=Kollam"
        self.client.get(reverse("login") + f"?next={target_path}")

        resp = self.client.post(reverse("login"), {
            "username": self.admin.username,
            "password": "Password123!",
        })
        self.assertRedirects(resp, reverse("admin_dashboard"))

    def test_service_workers_public_and_lists_only_verified_available(self):
        """Guest GET service_workers returns 200 and lists only verified AVAILABLE workers of that district."""
        url = reverse("service_workers", args=[self.service1.id]) + "?district=Kollam"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.kollam_worker_user.username)
        # Service 2 in Kollam has no verified AVAILABLE workers
        url2 = reverse("service_workers", args=[self.service2.id]) + "?district=Kollam"
        resp2 = self.client.get(url2)
        self.assertEqual(resp2.status_code, 200)
        self.assertNotContains(resp2, self.unverified_worker_user.username)
        self.assertNotContains(resp2, self.unavailable_worker_user.username)

    @patch("lamoapp.ai_utils.build_worker_recommendations")
    def test_service_workers_ai_requirement_guard_for_guests(self, mock_ai):
        """Guest request with ai_requirement does not invoke build_worker_recommendations; logged-in does."""
        url = reverse("service_workers", args=[self.service1.id]) + "?district=Kollam&ai_requirement=urgent+plumbing"

        # Guest request
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        mock_ai.assert_not_called()

        # Logged-in user request
        mock_ai.return_value = []
        self.client.login(username=self.customer.username, password="Password123!")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        mock_ai.assert_called_once()

    def test_guest_home_district_filtering_and_session(self):
        """Guest GET home?district=<valid> filters services and stores in session; invalid is ignored."""
        # Valid district Kollam: only service1 has an active, verified, available worker in Kollam
        resp = self.client.get(reverse("home") + "?district=Kollam")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Plumbing Service")
        self.assertNotContains(resp, "Electrical Service")
        self.assertEqual(self.client.session.get("district"), "Kollam")

        # Invalid district value is ignored
        resp_invalid = self.client.get(reverse("home") + "?district=Atlantis")
        self.assertEqual(resp_invalid.status_code, 200)
        # Session district should still be Kollam (not overridden by Atlantis)
        self.assertEqual(self.client.session.get("district"), "Kollam")

        # Clearing district via change_district=1
        resp_clear = self.client.get(reverse("home") + "?district=&change_district=1")
        self.assertEqual(resp_clear.status_code, 200)
        self.assertNotIn("district", self.client.session)

        # No district keeps old behavior (both services rendered, with service_detail link)
        resp_no_dist = self.client.get(reverse("home") + "?from_splash=1")
        self.assertEqual(resp_no_dist.status_code, 200)
        self.assertContains(resp_no_dist, f"/services/{self.service1.id}/")
        self.assertContains(resp_no_dist, f"/services/{self.service2.id}/")

    def test_service_workers_response_no_private_worker_data_for_guest(self):
        """service_workers response for a guest contains no worker phone, email, or address strings."""
        url = reverse("service_workers", args=[self.service1.id]) + "?district=Kollam"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "9876543210")
        self.assertNotContains(resp, "secret_kollam@example.com")
        self.assertNotContains(resp, "123 Private Street")

    def test_register_role_worker_preselection_hook(self):
        """GET /register/?role=worker responds 200 and page contains role-preselection hook."""
        resp = self.client.get(reverse("register") + "?role=worker")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'requestedRole = "worker"')
        self.assertContains(resp, "targetCard.click()")

    def test_endpoint_statuses_guest_user_worker_admin(self):
        """Verify endpoints and role redirect statuses as specified in VERIFY section."""
        endpoints = [
            ("/", 200),
            ("/?district=Kollam", 200),
            ("/service_page/", 200),
            (f"/services/{self.service1.id}/", 200),
            (f"/services/{self.service1.id}/workers/?district=Kollam", 200),
            ("/login/?next=/services/1/workers/%3Fdistrict%3DKollam", 200),
            ("/register/?role=worker", 200),
        ]
        # Guest tests
        for path, status in endpoints:
            r = self.client.get(path)
            self.assertEqual(r.status_code, status, f"Guest GET {path} failed")

        # Authenticated Customer tests
        customer_endpoints = [
            ("/", 200),
            ("/?district=Kollam", 200),
            ("/service_page/", 200),
            (f"/services/{self.service1.id}/", 200),
            (f"/services/{self.service1.id}/workers/?district=Kollam", 200),
            ("/my-bookings/", 200),
        ]
        self.client.login(username=self.customer.username, password="Password123!")
        for path, status in customer_endpoints:
            r = self.client.get(path)
            self.assertEqual(r.status_code, status, f"Customer GET {path} failed")

        # Worker tests (/ redirects to worker_dashboard)
        self.client.logout()
        self.client.login(username=self.kollam_worker_user.username, password="Password123!")
        r_worker_home = self.client.get("/")
        self.assertRedirects(r_worker_home, reverse("worker_dashboard"))

        # Admin tests (/ redirects to admin_dashboard)
        self.client.logout()
        self.client.login(username=self.admin.username, password="Password123!")
        r_admin_home = self.client.get("/")
        self.assertRedirects(r_admin_home, reverse("admin_dashboard"))
