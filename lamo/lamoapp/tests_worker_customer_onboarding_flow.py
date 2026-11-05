from decimal import Decimal
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from django.core.signing import TimestampSigner

from lamoapp.models import CustomUser, WorkerProfile, Service, WorkerWallet


class WorkerCustomerOnboardingFlowTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.service = Service.objects.create(
            name='Electrician',
            description='Electrical repair and installations',
            is_active=True
        )

        # Admin user
        self.admin_user = CustomUser.objects.create_user(
            username='admin_boss',
            email='admin@lamo.in',
            password='Password@123',
            role='ADMIN',
            is_email_verified=True
        )

        # Customer user (unverified email initially)
        self.customer = CustomUser.objects.create_user(
            username='customer_arun',
            email='arun@example.com',
            password='Password@123',
            role='USER',
            is_email_verified=False
        )
        self.customer.email_verification_otp = '123456'
        self.customer.otp_created_at = timezone.now()
        self.customer.save()

        # Worker user (unverified email, unverified profile initially)
        self.worker_user = CustomUser.objects.create_user(
            username='worker_manu',
            email='manu@example.com',
            password='Password@123',
            role='WORKER',
            is_email_verified=False
        )
        self.worker_user.email_verification_otp = '654321'
        self.worker_user.otp_created_at = timezone.now()
        self.worker_user.save()

        self.worker_profile = WorkerProfile.objects.create(
            user=self.worker_user,
            phone='9876543210',
            service=self.service,
            experience_years=5,
            district='Ernakulam',
            availability='Full Time',
            pricing_type='hour',
            pricing_amount=Decimal('250.00'),
            address='123 Worker Street, Kochi',
            proof_type='aadhaar',
            proof_document='worker_proofs/test_aadhaar.pdf',
            is_verified=False
        )

    def test_customer_otp_verification_auto_logs_in(self):
        """Customer verifying email OTP should auto-login and go to home without hitting /login/."""
        session = self.client.session
        session['otp_user_id'] = self.customer.id
        session.save()

        response = self.client.post(reverse('verify_otp'), {
            'action': 'verify',
            'otp': '123456'
        })

        # Must NOT redirect to login! Should redirect to home
        self.assertRedirects(response, reverse('home'))

        # Customer must now be authenticated
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.is_email_verified)
        self.assertEqual(int(self.client.session['_auth_user_id']), self.customer.id)

    def test_worker_otp_verification_redirects_to_application_status(self):
        """Worker verifying email OTP should redirect to /worker/application-status/, NOT /login/."""
        session = self.client.session
        session['otp_user_id'] = self.worker_user.id
        session.save()

        response = self.client.post(reverse('verify_otp'), {
            'action': 'verify',
            'otp': '654321'
        })

        # Must redirect to application status page
        self.assertRedirects(response, reverse('worker_application_status'))

        self.worker_user.refresh_from_db()
        self.assertTrue(self.worker_user.is_email_verified)
        # Worker must NOT be authenticated yet because admin has not verified them
        self.assertNotIn('_auth_user_id', self.client.session)

        # Following to application status page shows the Under Review view
        status_response = self.client.get(reverse('worker_application_status'))
        self.assertEqual(status_response.status_code, 200)
        self.assertContains(status_response, 'Application Under Review')
        self.assertContains(status_response, f'#WP-{self.worker_profile.id}')
        self.assertContains(status_response, 'Electrician')
        self.assertContains(status_response, 'Ernakulam')

    def test_unverified_worker_login_otp_request_blocked_upfront(self):
        """Unverified worker attempting to send login OTP on login page is rejected upfront."""
        response = self.client.post(reverse('login'), {
            'action': 'send_login_otp',
            'identifier': 'manu@example.com'
        })
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data.get('success'))
        self.assertTrue(data.get('pending_verification'))
        self.assertIn('pending admin verification', data.get('error'))

    def test_admin_verify_worker_and_magic_login(self):
        """Admin verifying worker sends email with 1-click magic link, and magic link logs worker in."""
        self.client.force_login(self.admin_user)

        response = self.client.post(reverse('worker_verify', args=[self.worker_profile.id]))
        self.assertRedirects(response, reverse('workers_list'))

        self.worker_profile.refresh_from_db()
        self.assertTrue(self.worker_profile.is_verified)
        self.assertIsNotNone(self.worker_profile.launch_ends_at)

        # Worker wallet should have been created
        self.assertTrue(WorkerWallet.objects.filter(worker=self.worker_profile).exists())

        # Test Magic Link generation and direct login
        signer = TimestampSigner(salt='worker-magic-login')
        token = signer.sign(str(self.worker_user.id))

        # Logout admin and access magic link as guest
        self.client.logout()
        magic_response = self.client.get(reverse('worker_magic_login', args=[token]))

        # Should redirect directly to worker dashboard!
        self.assertRedirects(magic_response, reverse('worker_dashboard'))

        # Worker must now be authenticated
        self.assertEqual(int(self.client.session['_auth_user_id']), self.worker_user.id)

