from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from lamoapp.models import UserProfile, WorkerProfile

User = get_user_model()

class UnifiedAuthAndPartnerFlowTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.cookies['lamo_splash_seen'] = '1'

        # Create verified customer user
        self.user = User.objects.create_user(
            username='sneha_customer',
            email='sneha@example.com',
            password='TestPassword123!',
            role='USER',
            is_email_verified=True,
            is_active=True
        )
        self.user_profile = UserProfile.objects.create(
            user=self.user,
            phone='9876543210'
        )

        # Create verified worker user
        self.worker_user = User.objects.create_user(
            username='ramesh_worker',
            email='ramesh@worker.com',
            password='TestPassword123!',
            role='WORKER',
            is_email_verified=True,
            is_active=True
        )
        self.worker_profile = WorkerProfile.objects.create(
            user=self.worker_user,
            phone='9123456789',
            is_verified=True
        )

    def test_login_with_username(self):
        """User can log in using their username."""
        response = self.client.post(reverse('login'), {
            'username': 'sneha_customer',
            'password': 'TestPassword123!'
        })
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('home'))

    def test_login_with_email(self):
        """User can log in using their email address in lowercase or uppercase."""
        response = self.client.post(reverse('login'), {
            'username': 'SNEHA@EXAMPLE.COM',
            'password': 'TestPassword123!'
        })
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('home'))

    def test_login_with_phone_plain(self):
        """User can log in using their 10-digit phone number."""
        response = self.client.post(reverse('login'), {
            'username': '9876543210',
            'password': 'TestPassword123!'
        })
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('home'))

    def test_login_with_phone_international(self):
        """User can log in using phone number with +91 and spaces/dashes."""
        response = self.client.post(reverse('login'), {
            'username': '+91 98765-43210',
            'password': 'TestPassword123!'
        })
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('home'))

    def test_worker_login_with_phone_redirects_to_worker_dashboard(self):
        """Worker logging in with phone is redirected to worker dashboard."""
        response = self.client.post(reverse('login'), {
            'username': '+919123456789',
            'password': 'TestPassword123!'
        })
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('worker_dashboard'))

    def test_login_honours_next_url(self):
        """Login form honours the next parameter for destination routing."""
        bookings_url = reverse('my_bookings')
        response = self.client.post(f"{reverse('login')}?next={bookings_url}", {
            'username': 'sneha_customer',
            'password': 'TestPassword123!',
            'next': bookings_url
        })
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, bookings_url)

    def test_homepage_has_partner_cta_and_unaltered_hero(self):
        """Home page has Partner CTA section and hero section remains intact."""
        response = self.client.get('/?from_splash=1')
        self.assertEqual(response.status_code, 200)

        # Partner CTA section exists
        self.assertContains(response, 'id="partner-join"')
        self.assertContains(response, 'Register as a Service Partner')
        self.assertContains(response, 'data-auth-role="WORKER"')

        # Hero section remains intact
        self.assertContains(response, 'id="home"')
        self.assertContains(response, 'class="home-hero-exact"')
        self.assertContains(response, 'Trusted Home Services,')
        self.assertContains(response, 'Right at Your Doorstep')
        self.assertContains(response, 'search-capsule-bar')
