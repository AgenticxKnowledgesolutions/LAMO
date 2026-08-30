from django.test import TestCase, Client
from django.urls import reverse
from unittest.mock import patch
from .models import CustomUser, Service, WorkerProfile, Booking, Complaint
from .ai_utils import match_service_and_workers, classify_complaint_severity


class AIUtilsTest(TestCase):
    def setUp(self):
        self.service1 = Service.objects.create(
            name="Plumbing",
            description="Fix leaky pipes, water tanks, and taps",
            price=500.00,
            is_active=True
        )
        self.service2 = Service.objects.create(
            name="Electrical Work",
            description="Wiring, switchboard installation, and repairs",
            price=750.00,
            is_active=True
        )

        self.worker_user = CustomUser.objects.create_user(
            username="plumber_joe",
            email="joe@test.com",
            password="Password123!",
            role="WORKER"
        )
        self.worker = WorkerProfile.objects.create(
            user=self.worker_user,
            service=self.service1,
            district="Ernakulam",
            work_status="AVAILABLE",
            is_verified=True
        )

    def test_match_service_db_fallback(self):
        result = match_service_and_workers("Plumbing", district="Ernakulam")
        self.assertIsNotNone(result['service'])
        self.assertEqual(result['service'].name, "Plumbing")
        self.assertIn(self.worker, result['workers'])

    @patch('lamoapp.ai_utils._call_gemini_api')
    def test_match_service_ai(self, mock_gemini):
        mock_gemini.return_value = "Plumbing"
        result = match_service_and_workers("Water is leaking under my kitchen sink")
        self.assertIsNotNone(result['service'])
        self.assertEqual(result['service'].name, "Plumbing")

    @patch('lamoapp.ai_utils._call_gemini_api')
    def test_classify_complaint_severity_high(self, mock_gemini):
        mock_gemini.return_value = "HIGH"
        severity = classify_complaint_severity("Dangerous wiring", "Exposed live wire causing sparks")
        self.assertEqual(severity, "HIGH")

    @patch('lamoapp.ai_utils._call_gemini_api')
    def test_classify_complaint_severity_fallback(self, mock_gemini):
        mock_gemini.return_value = None
        severity = classify_complaint_severity("Late arrival", "Worker arrived 30 mins late")
        self.assertEqual(severity, "MEDIUM")


class BookingLifecycleTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.customer = CustomUser.objects.create_user(
            username="customer1",
            email="cust@test.com",
            password="Password123!",
            role="CUSTOMER"
        )
        self.service = Service.objects.create(
            name="Cleaning",
            description="Deep house cleaning",
            price=1200.00,
            is_active=True
        )
        self.worker_user = CustomUser.objects.create_user(
            username="cleaner_mary",
            email="mary@test.com",
            password="Password123!",
            role="WORKER"
        )
        self.worker = WorkerProfile.objects.create(
            user=self.worker_user,
            service=self.service,
            district="Kochi",
            work_status="AVAILABLE",
            is_verified=True
        )

    def test_prevent_booking_when_worker_unavailable(self):
        self.worker.work_status = "BUSY"
        self.worker.save()

        self.client.login(username="customer1", password="Password123!")
        response = self.client.post(reverse('book_service'), {
            'worker_id': self.worker.id,
            'service_id': self.service.id,
            'booking_type': 'DIRECT',
            'date': '2026-09-01',
            'time': '10:00',
            'location': 'Kochi',
            'phone': '9876543210',
            'description': 'Clean room'
        })
        self.assertEqual(Booking.objects.count(), 0)

    def test_prevent_double_booking_same_date(self):
        Booking.objects.create(
            customer=self.customer,
            worker=self.worker,
            service=self.service,
            date='2026-09-01',
            time='10:00',
            location='Kochi',
            phone='9876543210',
            status='ACCEPTED'
        )

        self.client.login(username="customer1", password="Password123!")
        response = self.client.post(reverse('book_service'), {
            'worker_id': self.worker.id,
            'service_id': self.service.id,
            'booking_type': 'DIRECT',
            'date': '2026-09-01',
            'time': '14:00',
            'location': 'Kochi',
            'phone': '9876543210',
            'description': 'Second booking test'
        })
        self.assertEqual(Booking.objects.count(), 1)

    def test_cancel_booking(self):
        booking = Booking.objects.create(
            customer=self.customer,
            worker=self.worker,
            service=self.service,
            date='2026-09-02',
            time='11:00',
            location='Kochi',
            phone='9876543210',
            status='PENDING'
        )

        self.client.login(username="customer1", password="Password123!")
        response = self.client.post(reverse('cancel_booking', args=[booking.id]))
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'CANCELLED')
