import json
from unittest.mock import patch
from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from lamoapp.models import Notification, PushSubscription

User = get_user_model()


class BilingualLanguageTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.cookies['lamo_splash_seen'] = '1'
        self.user = User.objects.create_user(
            username='keralacustomer',
            email='customer@example.com',
            password='Password123!',
            role='USER'
        )

    def test_set_language_json_ml(self):
        """POST /api/language/set/ with JSON payload sets session and cookie to 'ml'."""
        response = self.client.post(
            reverse('set_language'),
            data=json.dumps({'lang': 'ml'}),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get('ok'))
        self.assertEqual(data.get('lang'), 'ml')
        self.assertEqual(self.client.session.get('lamo_lang'), 'ml')
        self.assertIn('lamo_lang', response.cookies)
        self.assertEqual(response.cookies['lamo_lang'].value, 'ml')

    def test_set_language_form_en(self):
        """POST /api/language/set/ with form-urlencoded sets session to 'en'."""
        response = self.client.post(
            reverse('set_language'),
            data={'lang': 'en'}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data.get('lang'), 'en')
        self.assertEqual(self.client.session.get('lamo_lang'), 'en')

    def test_set_language_invalid_fallback(self):
        """Invalid language fallback to 'en'."""
        response = self.client.post(
            reverse('set_language'),
            data=json.dumps({'lang': 'fr'}),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data.get('lang'), 'en')
        self.assertEqual(self.client.session.get('lamo_lang'), 'en')

    def test_context_processor_exposes_bilingual_and_push_vars(self):
        """Context processor exposes current_language, vapid_public_key, and has_push_subscription."""
        self.client.login(username='keralacustomer', password='Password123!')
        session = self.client.session
        session['lamo_lang'] = 'ml'
        session.save()

        response = self.client.get('/?from_splash=1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['current_language'], 'ml')
        self.assertIn('vapid_public_key', response.context)
        self.assertFalse(response.context['has_push_subscription'])


class WebPushNotificationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.cookies['lamo_splash_seen'] = '1'
        self.user = User.objects.create_user(
            username='pushuser',
            email='pushuser@example.com',
            password='Password123!',
            role='USER'
        )

    def test_save_push_subscription_endpoint(self):
        """POST /push/subscribe/ persists browser push endpoint and keys."""
        self.client.login(username='pushuser', password='Password123!')
        payload = {
            'endpoint': 'https://fcm.googleapis.com/fcm/send/sample_push_endpoint_token_123',
            'keys': {
                'p256dh': 'BIPxyQ8rC...sample_key',
                'auth': 'sample_auth_secret'
            }
        }
        response = self.client.post(
            reverse('save_push_subscription'),
            data=json.dumps(payload),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get('ok'))

        sub = PushSubscription.objects.get(user=self.user)
        self.assertEqual(sub.endpoint, payload['endpoint'])
        self.assertEqual(sub.p256dh, payload['keys']['p256dh'])
        self.assertEqual(sub.auth, payload['keys']['auth'])

    def test_save_push_subscription_invalid_payload(self):
        """POST /push/subscribe/ with missing keys returns 400 Bad Request."""
        self.client.login(username='pushuser', password='Password123!')
        payload = {'endpoint': 'https://example.com/bad'}
        response = self.client.post(
            reverse('save_push_subscription'),
            data=json.dumps(payload),
            content_type='application/json'
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json().get('ok'))

    @patch('lamoapp.push_utils.send_web_push_async')
    def test_notification_creation_triggers_automated_push(self, mock_send_push):
        """Notification.objects.create automatically triggers send_web_push_async."""
        notif = Notification.objects.create(
            recipient=self.user,
            notification_type='BOOKING',
            message='Your electrician has accepted booking #16.'
        )
        self.assertIsNotNone(notif.pk)
        mock_send_push.assert_called_once()
        args = mock_send_push.call_args[0]
        self.assertEqual(args[0], self.user)
        self.assertIn('Booking', args[1])
        self.assertEqual(args[2], 'Your electrician has accepted booking #16.')
