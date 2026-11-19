from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.conf import settings

User = get_user_model()


class SessionOptimizationTests(TestCase):
    def test_session_save_every_request_is_false(self):
        """Ensure SESSION_SAVE_EVERY_REQUEST is False to avoid redundant DB writes on read requests."""
        self.assertFalse(settings.SESSION_SAVE_EVERY_REQUEST)

    def test_session_persists_mutations(self):
        """Verify that session data is persisted across requests when keys are modified."""
        client = Client()
        # Initial request
        session = client.session
        session['district'] = 'Ernakulam'
        session['locality'] = 'Kochi'
        session['pending_booking_data'] = {'service_id': 1, 'amount': 500}
        session.save()

        # Follow-up request
        client.get('/')
        updated_session = client.session
        self.assertEqual(updated_session.get('district'), 'Ernakulam')
        self.assertEqual(updated_session.get('locality'), 'Kochi')
        self.assertEqual(updated_session.get('pending_booking_data'), {'service_id': 1, 'amount': 500})

    def test_session_cleared_on_logout(self):
        """Verify that explicit logout clears session authentication state correctly."""
        user = User.objects.create_user(
            username='sess_user',
            email='sess_user@example.com',
            password='TestPassword123!',
            role='USER',
            is_active=True
        )
        client = Client()
        client.force_login(user)

        # Check authenticated session
        resp = client.get('/user-dashboard/')
        self.assertEqual(resp.status_code, 200)

        # Logout
        client.logout()
        resp_after = client.get('/user-dashboard/')
        # Redirects to login
        self.assertEqual(resp_after.status_code, 302)
