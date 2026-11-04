import os
from datetime import datetime, date, timedelta, time as datetime_time
from decimal import Decimal
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from django.contrib.auth import get_user_model
from lamoapp.models import Service, WorkerProfile, Booking, UserProfile

User = get_user_model()


class HandshakeAndLeadTimeTest(TestCase):
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

    def test_booking_generates_badge_code_and_arrival_otp(self):
        booking = Booking.objects.create(
            customer=self.user_cust,
            worker=self.worker_profile,
            service=self.service,
            date=date.today() + timedelta(days=2),
            time='10:00 AM - 12:00 PM',
            start_time=datetime_time(10, 0),
            end_time=datetime_time(12, 0),
            location='Kochi, Ernakulam',
            phone='9876543210',
            status='ACCEPTED'
        )
        self.assertTrue(bool(booking.arrival_otp))
        self.assertEqual(len(booking.arrival_otp), 4)
        self.assertTrue(bool(booking.worker_badge_code))
        self.assertEqual(len(booking.worker_badge_code), 2)
        self.assertEqual(booking.get_worker_badge_code, booking.worker_badge_code)

    def test_same_day_custom_time_within_60_mins_rejected(self):
        self.client.login(username='sneha_cust', password='Password123!')
        now_local = timezone.localtime()
        # Choose a time only 15 minutes in the future
        too_soon = (now_local + timedelta(minutes=15)).strftime('%I:%M %p')

        resp = self.client.post(reverse('book_service'), {
            'worker': self.worker_profile.id,
            'service': self.service.id,
            'booking_type': 'One Time',
            'date': now_local.date().isoformat(),
            'time': too_soon,
            'location': 'Kochi, Ernakulam',
            'phone': '9876543210',
            'description': 'Urgent AC repair',
        })
        # Should be rejected with lead time error
        self.assertFalse(
            Booking.objects.filter(customer=self.user_cust, description='Urgent AC repair').exists()
        )

    def test_future_date_booking_succeeds(self):
        self.client.login(username='sneha_cust', password='Password123!')
        future_date = date.today() + timedelta(days=3)

        resp = self.client.post(reverse('book_service'), {
            'worker': self.worker_profile.id,
            'service': self.service.id,
            'booking_type': 'One Time',
            'date': future_date.isoformat(),
            'time': '10:00 AM - 12:00 PM',
            'location': 'Kochi, Ernakulam',
            'phone': '9876543210',
            'description': 'Scheduled AC repair',
        })
        self.assertTrue(
            Booking.objects.filter(customer=self.user_cust, description='Scheduled AC repair').exists()
        )
        booking = Booking.objects.get(customer=self.user_cust, description='Scheduled AC repair')
        self.assertTrue(bool(booking.arrival_otp))
        self.assertTrue(bool(booking.get_worker_badge_code))
