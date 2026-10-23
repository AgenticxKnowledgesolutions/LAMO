from django.test import TestCase, Client
from django.urls import reverse
from lamoapp.models import CustomUser, Service, WorkerProfile


class LocationSelectionPopupTests(TestCase):
    def setUp(self):
        self.client = Client()

        # Create two sample active services
        self.service_plumbing = Service.objects.create(
            name="Plumbing Services",
            description="Fix leaky pipes, taps, and sanitary fittings",
            price=299.00,
            is_active=True,
        )
        self.service_electrical = Service.objects.create(
            name="Electrical Repair",
            description="Wiring, switchboard, fan repair and fuse fixes",
            price=349.00,
            is_active=True,
        )

        # Worker in Kollam
        self.user_worker_kollam = CustomUser.objects.create_user(
            username="worker_kollam",
            email="kollam@example.com",
            password="Password123!",
            role="WORKER",
            is_email_verified=True,
        )
        self.worker_kollam = WorkerProfile.objects.create(
            user=self.user_worker_kollam,
            service=self.service_plumbing,
            district="Kollam",
            phone="9876543210",
            experience_years=5,
            is_verified=True,
            work_status="AVAILABLE",
            pricing_amount=299.00,
        )

        # Worker in Thiruvananthapuram
        self.user_worker_tvm = CustomUser.objects.create_user(
            username="worker_tvm",
            email="tvm@example.com",
            password="Password123!",
            role="WORKER",
            is_email_verified=True,
        )
        self.worker_tvm = WorkerProfile.objects.create(
            user=self.user_worker_tvm,
            service=self.service_electrical,
            district="Thiruvananthapuram",
            phone="9876543211",
            experience_years=4,
            is_verified=True,
            work_status="AVAILABLE",
            pricing_amount=349.00,
        )

    def test_01_first_time_visitor_splash_screen(self):
        """First-time visitor to '/' sees the animated splash screen."""
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertTemplateUsed(res, "splash.html")
        self.assertContains(res, "Home services, simplified")
        self.assertIn("lamo_splash_seen", res.cookies)

    def test_02_home_page_after_splash_has_location_popup_and_14_districts(self):
        """After splash screen, '/' renders home.html with the Choose Your Location modal and all 14 districts."""
        res = self.client.get("/?from_splash=1")
        self.assertEqual(res.status_code, 200)
        self.assertTemplateUsed(res, "home.html")
        self.assertTemplateUsed(res, "includes/location_modal.html")

        # Modal elements
        self.assertContains(res, 'id="locationModalOverlay"')
        self.assertContains(res, "Choose Your Location")
        self.assertContains(res, "Find trusted home services available in your area.")
        self.assertContains(res, 'id="btnContinueLocation"')

        # All 14 Kerala districts in modal
        for code, name in WorkerProfile.DISTRICT_CHOICES:
            self.assertContains(res, f'data-district="{code}"')

        # Since no district is selected yet, selected_district is empty
        self.assertEqual(res.context["selected_district"], "")

    def test_03_guest_can_select_district_without_login(self):
        """Guest selects district via AJAX endpoint, district is stored in session and services update."""
        res = self.client.get("/?district=Kollam&ajax=1", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["selected_district"], "Kollam")

        # Check session
        session = self.client.session
        self.assertEqual(session.get("district"), "Kollam")

        # Services in Kollam: only Plumbing (1 service)
        service_names = [s["name"] for s in data["services"]]
        self.assertIn("Plumbing Services", service_names)
        self.assertNotIn("Electrical Repair", service_names)

    def test_04_returning_visitor_with_session_district_does_not_require_initial_selection(self):
        """Returning visitor with existing district session renders that district directly without empty state."""
        session = self.client.session
        session["district"] = "Kollam"
        session["lamo_splash_shown"] = True
        session.save()
        self.client.cookies["lamo_splash_seen"] = "1"

        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertTemplateUsed(res, "home.html")
        self.assertEqual(res.context["selected_district"], "Kollam")
        # Location modal has data-initial-district="Kollam" so JS won't auto-open it
        self.assertContains(res, 'data-initial-district="Kollam"')
        # Navbar displays Kollam
        self.assertContains(res, 'id="navCurrentDistrict"')
        self.assertContains(res, "Kollam")

    def test_05_switching_district_via_ajax(self):
        """Switching district to Thiruvananthapuram updates session and returns TVM services."""
        session = self.client.session
        session["district"] = "Kollam"
        session.save()

        res = self.client.get("/?district=Thiruvananthapuram&ajax=1", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["selected_district"], "Thiruvananthapuram")

        # TVM has Electrical, not Plumbing
        service_names = [s["name"] for s in data["services"]]
        self.assertIn("Electrical Repair", service_names)
        self.assertNotIn("Plumbing Services", service_names)

        # Verify session was updated
        self.assertEqual(self.client.session.get("district"), "Thiruvananthapuram")

    def test_06_district_with_no_available_services(self):
        """District with zero active workers (e.g., Wayanad) returns 0 services."""
        res = self.client.get("/?district=Wayanad&ajax=1", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["services_count"], 0)
        self.assertEqual(len(data["services"]), 0)

    def test_07_navbar_location_button_rendered_across_pages(self):
        """Navbar location button is rendered on base.html pages like /about/ and /contact/."""
        session = self.client.session
        session["district"] = "Kollam"
        session.save()

        res_about = self.client.get("/about/")
        self.assertEqual(res_about.status_code, 200)
        self.assertContains(res_about, 'id="navLocationBtn"')
        self.assertContains(res_about, "Kollam")
        self.assertContains(res_about, 'id="locationModalOverlay"')
