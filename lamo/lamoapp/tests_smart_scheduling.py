import os
from datetime import datetime, date, timedelta, time as datetime_time
from decimal import Decimal
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from django.contrib.auth import get_user_model
from lamoapp.models import Service, WorkerProfile, Booking, UserProfile

User = get_user_model()


class SmartSchedulingEngineTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user_cust = User.objects.create_user(
            username='sneha_cust',
            email='sneha@example.com',
            password='Password123!',
            role='USER'
        )
        UserProfile.objects.create(
            user=self.user_cust,
            phone='9876543210',
            district='Ernakulam',
            address='Kochi, Ernakulam'
        )
        self.user_worker = User.objects.create_user(
            username='manu_worker',
            email='manu@example.com',
            password='Password123!',
            role='WORKER'
        )
        self.service = Service.objects.create(
            name='AC Repair',
            description='AC servicing',
            price=Decimal('450.00'),
            is_active=True
        )
        self.worker_profile = WorkerProfile.objects.create(
            user=self.user_worker,
            service=self.service,
            district='Ernakulam',
            is_verified=True,
            work_status='AVAILABLE'
        )

    def test_pillar1_api_worker_availability_flags_booked_slots(self):
        # Create an existing accepted booking for tomorrow 10:00 AM - 12:00 PM
        tomorrow = date.today() + timedelta(days=1)
        Booking.objects.create(
            customer=self.user_cust,
            worker=self.worker_profile,
            service=self.service,
            date=tomorrow,
            time='10:00 AM - 12:00 PM',
            start_time=datetime_time(10, 0),
            end_time=datetime_time(12, 0),
            location='Kochi, Ernakulam',
            phone='9876543210',
            status='ACCEPTED'
        )

        resp = self.client.get(
            reverse('api_worker_availability', args=[self.worker_profile.id]),
            {'date': tomorrow.isoformat()}
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['busy_count'], 1)

        # 10:00 AM slot within booked 10:00 AM - 12:00 PM range must be marked busy
        slot_10 = next((s for s in data['slots'] if s['slot'].startswith('10:00 AM')), None)
        self.assertIsNotNone(slot_10)
        self.assertFalse(slot_10['available'])
        self.assertEqual(slot_10['status'], 'busy')

        # 2:00 PM slot must be available
        slot_2 = next((s for s in data['slots'] if s['slot'].startswith('2:00 PM')), None)
        self.assertIsNotNone(slot_2)
        self.assertTrue(slot_2['available'])

    def test_pillar2_worker_propose_reschedule_flow(self):
        # Accepting a counter-offer on a PENDING request confirms the job, so
        # (prepaid commission model) the worker's wallet must cover the fee.
        from lamoapp.models import WorkerWallet
        WorkerWallet.objects.update_or_create(
            worker=self.worker_profile, defaults={'balance': Decimal('500.00')}
        )

        # Customer books for tomorrow 10 AM
        tomorrow = date.today() + timedelta(days=1)
        booking = Booking.objects.create(
            customer=self.user_cust,
            worker=self.worker_profile,
            service=self.service,
            date=tomorrow,
            time='10:00 AM - 12:00 PM',
            start_time=datetime_time(10, 0),
            end_time=datetime_time(12, 0),
            location='Kochi, Ernakulam',
            phone='9876543210',
            status='PENDING'
        )

        # Worker logs in and proposes 4:30 PM
        self.client.login(username='manu_worker', password='Password123!')
        resp = self.client.post(
            reverse('worker_propose_reschedule', args=[booking.id]),
            {
                'proposed_date': tomorrow.isoformat(),
                'proposed_time': '4:30 PM',
                'reschedule_note': 'Finishing earlier job in Aluva until 4 PM'
            }
        )
        self.assertEqual(resp.status_code, 302)

        booking.refresh_from_db()
        self.assertTrue(booking.has_pending_reschedule)
        self.assertEqual(booking.proposed_time, '4:30 PM')
        self.assertEqual(booking.reschedule_note, 'Finishing earlier job in Aluva until 4 PM')

        # Customer logs in and accepts the counter-offer
        self.client.login(username='sneha_cust', password='Password123!')
        resp_accept = self.client.post(reverse('customer_accept_reschedule', args=[booking.id]))
        self.assertEqual(resp_accept.status_code, 302)

        booking.refresh_from_db()
        self.assertEqual(booking.status, 'ACCEPTED')
        self.assertEqual(booking.time, '4:30 PM')
        self.assertFalse(booking.has_pending_reschedule)
        self.assertTrue(bool(booking.arrival_otp))
        self.assertTrue(bool(booking.worker_badge_code))

    def test_pillar2_customer_decline_reschedule_clears_proposal(self):
        tomorrow = date.today() + timedelta(days=1)
        booking = Booking.objects.create(
            customer=self.user_cust,
            worker=self.worker_profile,
            service=self.service,
            date=tomorrow,
            time='10:00 AM - 12:00 PM',
            status='PENDING',
            proposed_date=tomorrow,
            proposed_time='5:00 PM',
            reschedule_note='Busy in morning'
        )
        self.assertTrue(booking.has_pending_reschedule)

        self.client.login(username='sneha_cust', password='Password123!')
        resp = self.client.post(reverse('customer_decline_reschedule', args=[booking.id]))
        self.assertEqual(resp.status_code, 302)

        booking.refresh_from_db()
        self.assertEqual(booking.status, 'PENDING')
        self.assertFalse(booking.has_pending_reschedule)
        self.assertIsNone(booking.proposed_time)

