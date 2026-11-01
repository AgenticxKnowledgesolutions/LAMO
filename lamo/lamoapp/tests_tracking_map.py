from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from lamoapp.models import CustomUser, Service, WorkerProfile, Booking, UserProfile

User = get_user_model()

class LiveTrackingMapTests(TestCase):
    def setUp(self):
        self.client = Client()

        # 1. Customer
        self.customer = User.objects.create_user(
            username='track_cust',
            email='cust@test.com',
            password='password123',
            role='USER'
        )
        self.customer_profile = UserProfile.objects.create(
            user=self.customer,
            district='Kollam',
            address='Sooranad, Kollam',
            latitude=8.8932,
            longitude=76.6141
        )

        # 2. Other User
        self.other_user = User.objects.create_user(
            username='other_user',
            email='other@test.com',
            password='password123',
            role='USER'
        )

        # 3. Worker
        self.worker_user = User.objects.create_user(
            username='track_worker',
            email='worker@test.com',
            password='password123',
            role='WORKER'
        )
        self.service = Service.objects.create(
            name='Electrician',
            description='Electrical repairs',
            price=350.00
        )
        self.worker_profile = WorkerProfile.objects.create(
            user=self.worker_user,
            phone='9876543210',
            service=self.service,
            district='Kollam',
            availability='Full Time',
            is_verified=True,
            latitude=8.9100,
            longitude=76.6300,
            current_place='Kollam City'
        )

        # 4. Booking
        self.booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker_profile,
            service=self.service,
            district='Kollam',
            location='Sooranad, Kollam',
            latitude=8.8932,
            longitude=76.6141,
            date='2026-10-30',
            time='10:00 AM',
            status='ACCEPTED',
            phone='8304099738'
        )

    def test_customer_can_access_tracking_telemetry(self):
        self.client.login(username='track_cust', password='password123')
        url = reverse('api_booking_tracking', args=[self.booking.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['booking_id'], self.booking.id)
        self.assertEqual(data['status'], 'ACCEPTED')
        self.assertAlmostEqual(data['customer']['latitude'], 8.8932, places=3)
        self.assertAlmostEqual(data['customer']['longitude'], 76.6141, places=3)
        self.assertAlmostEqual(data['worker']['latitude'], 8.9100, places=3)
        self.assertAlmostEqual(data['worker']['longitude'], 76.6300, places=3)
        self.assertGreater(data['distance_km'], 0)
        self.assertGreater(data['eta_minutes'], 0)
        self.assertIn('google.com/maps/dir', data['gmaps_url'])

    def test_worker_can_access_tracking_telemetry(self):
        self.client.login(username='track_worker', password='password123')
        url = reverse('api_booking_tracking', args=[self.booking.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['worker']['name'], 'track_worker')

    def test_unauthorized_user_cannot_access_tracking(self):
        self.client.login(username='other_user', password='password123')
        url = reverse('api_booking_tracking', args=[self.booking.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)
        data = response.json()
        self.assertFalse(data['success'])

    def test_worker_can_update_live_coordinates(self):
        self.client.login(username='track_worker', password='password123')
        url = reverse('api_worker_update_coordinates')
        payload = {
            'latitude': 8.9200,
            'longitude': 76.6400,
            'place_name': 'Chinnakada, Kollam'
        }
        response = self.client.post(url, data=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['place_name'], 'Chinnakada, Kollam')

        self.worker_profile.refresh_from_db()
        self.assertAlmostEqual(self.worker_profile.latitude, 8.9200, places=3)
        self.assertAlmostEqual(self.worker_profile.longitude, 76.6400, places=3)
        self.assertEqual(self.worker_profile.current_place, 'Chinnakada, Kollam')

    def test_invalid_coordinates_rejected(self):
        self.client.login(username='track_worker', password='password123')
        url = reverse('api_worker_update_coordinates')
        # Out of bounds coordinates (e.g. New York)
        payload = {
            'latitude': 40.7128,
            'longitude': -74.0060,
        }
        response = self.client.post(url, data=payload)
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data['success'])
