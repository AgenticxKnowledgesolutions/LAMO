from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from lamoapp.models import UserProfile, WorkerProfile
import json

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

    def test_login_with_username_backward_compatible(self):
        """User can log in using their username and password (backward compatibility)."""
        response = self.client.post(reverse('login'), {
            'username': 'sneha_customer',
            'password': 'TestPassword123!'
        })
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('home'))

    def test_login_with_phone_backward_compatible(self):
        """User can log in using their 10-digit phone number and password."""
        response = self.client.post(reverse('login'), {
            'username': '9876543210',
            'password': 'TestPassword123!'
        })
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('home'))

    def test_send_login_otp_and_verify(self):
        """User can request a login OTP via email/phone and verify to sign in passwordless."""
        # 1. Request OTP using mobile number
        response = self.client.post(reverse('login'), {
            'action': 'send_login_otp',
            'identifier': '9876543210'
        })
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data['success'])

        # 2. Retrieve generated OTP from user record
        self.user.refresh_from_db()
        otp = self.user.email_verification_otp
        self.assertTrue(otp and len(otp) == 6)

        # 3. Verify OTP
        verify_resp = self.client.post(reverse('login'), {
            'action': 'verify_login_otp',
            'identifier': '9876543210',
            'otp': otp
        })
        self.assertEqual(verify_resp.status_code, 200)
        verify_data = json.loads(verify_resp.content)
        self.assertTrue(verify_data['success'])
        self.assertEqual(verify_data['redirect_url'], reverse('home'))

    def test_worker_send_login_otp_and_verify(self):
        """Worker can sign in using phone + OTP and is redirected to worker dashboard."""
        response = self.client.post(reverse('login'), {
            'action': 'send_login_otp',
            'identifier': '9123456789'
        })
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data['success'])

        self.worker_user.refresh_from_db()
        otp = self.worker_user.email_verification_otp

        verify_resp = self.client.post(reverse('login'), {
            'action': 'verify_login_otp',
            'identifier': '9123456789',
            'otp': otp
        })
        self.assertEqual(verify_resp.status_code, 200)
        verify_data = json.loads(verify_resp.content)
        self.assertTrue(verify_data['success'])
        self.assertEqual(verify_data['redirect_url'], reverse('worker_dashboard'))

    def test_customer_registration_via_otp(self):
        """Customer can register passwordless with name + email/phone + OTP."""
        # 1. Send registration OTP
        response = self.client.post(reverse('register'), {
            'action': 'send_customer_reg_otp',
            'name': 'Asha Nair',
            'identifier': 'asha@example.com'
        })
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data['success'])

        # 2. Get OTP from session
        session_data = self.client.session.get('customer_reg_data')
        self.assertIsNotNone(session_data)
        otp = session_data['otp']

        # 3. Verify and create account
        verify_resp = self.client.post(reverse('register'), {
            'action': 'verify_customer_reg_otp',
            'otp': otp
        })
        self.assertEqual(verify_resp.status_code, 200)
        verify_data = json.loads(verify_resp.content)
        self.assertTrue(verify_data['success'])

        # 4. Assert user created with USER role and email
        created_user = User.objects.get(email='asha@example.com')
        self.assertEqual(created_user.role, 'USER')
        self.assertTrue(created_user.is_email_verified)

    def test_homepage_has_become_a_worker_and_unaltered_hero(self):
        """Home page has Become a Worker references and hero section remains completely intact."""
        response = self.client.get('/?from_splash=1')
        self.assertEqual(response.status_code, 200)

        # Worker section and navbar wording
        self.assertContains(response, 'Become a Worker')
        self.assertContains(response, 'Register as a Worker')
        self.assertContains(response, 'Worker Sign In')
        self.assertContains(response, 'data-auth-role="WORKER"')

        # Hero section remains 100% intact
        self.assertContains(response, 'id="home"')
        self.assertContains(response, 'class="home-hero-exact"')
        self.assertContains(response, 'Trusted Home Services,')
        self.assertContains(response, 'Right at Your Doorstep')
        self.assertContains(response, 'search-capsule-bar')
