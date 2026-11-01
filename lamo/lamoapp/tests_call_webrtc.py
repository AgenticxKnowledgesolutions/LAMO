from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
import json
from decimal import Decimal

from lamoapp.models import CustomUser, Service, WorkerProfile, Booking, CallSession, BookingMessage


class WebRTCCallSessionTests(TestCase):
    def setUp(self):
        self.client = Client()

        # 1. Customer
        self.customer = CustomUser.objects.create_user(
            username='call_customer',
            email='call_customer@example.com',
            password='Password123!',
            role='USER'
        )

        # 2. Worker User & Profile
        self.worker_user = CustomUser.objects.create_user(
            username='call_worker',
            email='call_worker@example.com',
            password='Password123!',
            role='WORKER'
        )
        self.service = Service.objects.create(name='Plumbing Test', price=Decimal('300.00'))
        self.worker = WorkerProfile.objects.create(
            user=self.worker_user,
            service=self.service,
            district='Kollam',
            pricing_amount=Decimal('350.00'),
            is_verified=True
        )

        # 3. Third party user (unauthorized attacker)
        self.intruder = CustomUser.objects.create_user(
            username='call_intruder',
            email='intruder@example.com',
            password='Password123!',
            role='USER'
        )

        # 4. Accepted Booking
        self.booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker,
            service=self.service,
            status='ACCEPTED',
            location='Kollam Town',
            district='Kollam',
            phone='9876543210'
        )

    def test_customer_initiate_call_success(self):
        self.client.force_login(self.customer)
        url = reverse('api_call_initiate', args=[self.booking.id])
        payload = json.dumps({'offer_sdp': 'v=0\r\no=- 12345 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n'})

        response = self.client.post(url, data=payload, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get('success'))
        self.assertIn('call_id', data)

        call = CallSession.objects.get(id=data['call_id'])
        self.assertEqual(call.caller, self.customer)
        self.assertEqual(call.receiver, self.worker_user)
        self.assertEqual(call.status, 'RINGING')

    def test_unauthorized_user_cannot_initiate_call(self):
        self.client.force_login(self.intruder)
        url = reverse('api_call_initiate', args=[self.booking.id])
        payload = json.dumps({'offer_sdp': 'dummy_sdp'})

        response = self.client.post(url, data=payload, content_type='application/json')
        self.assertEqual(response.status_code, 403)

    def test_check_incoming_call_detected(self):
        # Customer initiates call
        call = CallSession.objects.create(
            booking=self.booking,
            caller=self.customer,
            receiver=self.worker_user,
            status='RINGING',
            offer_sdp='v=0 test'
        )

        # Worker polls for incoming call
        self.client.force_login(self.worker_user)
        response = self.client.get(reverse('api_call_check_incoming'))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get('has_incoming'))
        self.assertEqual(data.get('call_id'), call.id)
        self.assertEqual(data.get('booking_id'), self.booking.id)

    def test_worker_answer_call_and_connect(self):
        call = CallSession.objects.create(
            booking=self.booking,
            caller=self.customer,
            receiver=self.worker_user,
            status='RINGING',
            offer_sdp='v=0 offer'
        )

        self.client.force_login(self.worker_user)
        url = reverse('api_call_answer', args=[self.booking.id, call.id])
        payload = json.dumps({'answer_sdp': 'v=0 answer'})

        response = self.client.post(url, data=payload, content_type='application/json')
        self.assertEqual(response.status_code, 200)

        call.refresh_from_db()
        self.assertEqual(call.status, 'CONNECTED')
        self.assertIsNotNone(call.connected_at)
        self.assertIn('v=0 answer', call.answer_sdp)

    def test_ice_candidate_exchange(self):
        call = CallSession.objects.create(
            booking=self.booking,
            caller=self.customer,
            receiver=self.worker_user,
            status='CONNECTED',
            offer_sdp='v=0 offer',
            answer_sdp='v=0 answer',
            connected_at=timezone.now()
        )

        # Worker sends ICE candidate
        self.client.force_login(self.worker_user)
        ice_url = reverse('api_call_ice', args=[self.booking.id, call.id])
        cand_payload = json.dumps({'candidate': {'candidate': 'candidate:1 1 UDP ...', 'sdpMid': '0'}})
        res = self.client.post(ice_url, data=cand_payload, content_type='application/json')
        self.assertEqual(res.status_code, 200)

        # Customer polls and receives worker candidate
        self.client.force_login(self.customer)
        poll_url = reverse('api_call_poll', args=[self.booking.id, call.id])
        poll_res = self.client.get(poll_url)
        self.assertEqual(poll_res.status_code, 200)
        poll_data = poll_res.json()
        self.assertEqual(len(poll_data.get('candidates', [])), 1)

    def test_end_call_logs_duration(self):
        call = CallSession.objects.create(
            booking=self.booking,
            caller=self.customer,
            receiver=self.worker_user,
            status='CONNECTED',
            offer_sdp='v=0 offer',
            answer_sdp='v=0 answer',
            connected_at=timezone.now() - timedelta(seconds=75)
        )

        self.client.force_login(self.customer)
        end_url = reverse('api_call_end', args=[self.booking.id, call.id])
        res = self.client.post(end_url, data='{}', content_type='application/json')
        self.assertEqual(res.status_code, 200)

        call.refresh_from_db()
        self.assertEqual(call.status, 'ENDED')
        self.assertGreaterEqual(call.duration_seconds, 70)

        # Verify chat message log was created
        msg = BookingMessage.objects.filter(booking=self.booking).latest('created_at')
        self.assertIn('In-App Audio Call completed', msg.message)
