from django.test import TestCase, Client
from django.urls import reverse
from lamoapp.models import CustomUser, UserProfile

class PureOtpAuthParityTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = CustomUser.objects.create_user(
            username='testunifieduser',
            email='unified@lamo.in',
            password='SecurePassword123!',
            role='USER',
            is_email_verified=True
        )
        self.profile = UserProfile.objects.create(
            user=self.user,
            phone='9876501234',
            address='Kollam, Kerala',
            district='Kollam'
        )

    def test_standalone_login_pure_otp_interface(self):
        response = self.client.get(reverse('login'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Sign in')
        self.assertContains(response, 'id="otpIdentifierInput"')
        self.assertContains(response, 'id="btnSendOtp"')
        # Ensure password tab/section is completely removed
        self.assertNotContains(response, 'id="tabLoginPassword"')
        self.assertNotContains(response, 'id="sectionLoginPassword"')

    def test_homepage_modal_pure_otp_interface(self):
        response = self.client.get(reverse('home') + '?from_splash=1')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="modalLoginIdentifier"')
        self.assertContains(response, 'id="modalLoginSendOtpBtn"')
        # Ensure password tab/section is completely removed from modal
        self.assertNotContains(response, 'id="modalLoginTabPassword"')
        self.assertNotContains(response, 'id="modalLoginPasswordSection"')

    def test_standalone_login_otp_send_and_verify(self):
        # 1. Send OTP
        res1 = self.client.post(
            reverse('login'),
            {'action': 'send_login_otp', 'identifier': '9876501234'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(res1.status_code, 200)
        d1 = res1.json()
        self.assertTrue(d1.get('success'))

        # Reload user to get generated OTP
        self.user.refresh_from_db()
        otp = self.user.email_verification_otp
        self.assertTrue(otp)

        # 2. Verify OTP
        res2 = self.client.post(
            reverse('login'),
            {'action': 'verify_login_otp', 'identifier': '9876501234', 'otp': otp},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(res2.status_code, 200)
        d2 = res2.json()
        self.assertTrue(d2.get('success'))

    def test_admin_login_username_and_password(self):
        response = self.client.get(reverse('admin_login'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Admin Username')
        self.assertContains(response, 'Admin Password')
        self.assertContains(response, 'name="username"')
        self.assertContains(response, 'name="password"')
