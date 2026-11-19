from django.test import TestCase, Client
from django.urls import reverse


class HealthCheckEndpointTest(TestCase):
    def setUp(self):
        self.client = Client()

    def test_health_check_returns_200_ok(self):
        """Health check returns 200 OK with plain text payload."""
        response = self.client.get(reverse('health_check'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode('utf-8').strip(), 'OK')
        self.assertIn('text/plain', response['Content-Type'])

    def test_health_check_zero_database_queries(self):
        """Health check executes ZERO database queries (assertNumQueries=0)."""
        with self.assertNumQueries(0):
            response = self.client.get('/health/')
            self.assertEqual(response.status_code, 200)

    def test_health_check_does_not_require_authentication(self):
        """Endpoint is unauthenticated so container orchestrators never get 302/401/403."""
        response = self.client.get('/health/')
        self.assertEqual(response.status_code, 200)

    def test_health_check_supports_head_request(self):
        """Some HTTP monitors issue HEAD requests."""
        response = self.client.head('/health/')
        self.assertEqual(response.status_code, 200)
