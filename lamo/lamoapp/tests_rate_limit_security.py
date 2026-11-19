import ipaddress
from django.test import RequestFactory, TestCase
from django.core.cache import cache

from lamoapp.rate_limit import (
    client_ip,
    is_cloudflare_ip,
    _safe_parse_ip,
    login_key,
    register_failure,
    is_rate_limited,
    LOGIN_MAX_ATTEMPTS,
    LOGIN_LOCKOUT_SECONDS,
)


class RateLimitIpSecurityTest(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()

    def test_normal_cloudflare_request(self):
        """When request arrives from a Cloudflare proxy, HTTP_CF_CONNECTING_IP is trusted."""
        # 162.158.1.1 is in Cloudflare IPv4 range (162.158.0.0/15)
        # 10.0.0.1 is Render internal proxy
        req = self.factory.get('/admin/login/')
        req.META = {
            'REMOTE_ADDR': '10.0.0.1',
            'HTTP_X_FORWARDED_FOR': '198.51.100.42, 162.158.1.1',
            'HTTP_CF_CONNECTING_IP': '198.51.100.42',
        }
        self.assertEqual(client_ip(req), '198.51.100.42')

    def test_spoofed_x_forwarded_for_without_cloudflare(self):
        """Attacker prepending fake IPs to X-Forwarded-For cannot spoof identity.

        Attacker with real IP 203.0.113.195 connects to Render.
        Attacker sent 'X-Forwarded-For: 1.1.1.1, 8.8.8.8'.
        Render appends attacker's real connecting IP: '1.1.1.1, 8.8.8.8, 203.0.113.195'.
        The extractor must return 203.0.113.195, NOT 1.1.1.1.
        """
        req = self.factory.get('/admin/login/')
        req.META = {
            'REMOTE_ADDR': '10.0.0.1',
            'HTTP_X_FORWARDED_FOR': '1.1.1.1, 8.8.8.8, 203.0.113.195',
        }
        self.assertEqual(client_ip(req), '203.0.113.195')

    def test_direct_request_bypassing_cloudflare_rejects_cf_connecting_ip(self):
        """Attacker sending HTTP_CF_CONNECTING_IP directly to Render origin must have it rejected."""
        # Attacker with IP 203.0.113.195 connects directly to Render bypassing Cloudflare.
        # They spoof 'CF-Connecting-IP: 1.2.3.4'.
        # Since the connecting IP (203.0.113.195) is NOT in Cloudflare ranges,
        # CF-Connecting-IP must be discarded and 203.0.113.195 must be returned.
        req = self.factory.get('/admin/login/')
        req.META = {
            'REMOTE_ADDR': '10.0.0.1',
            'HTTP_X_FORWARDED_FOR': '1.2.3.4, 203.0.113.195',
            'HTTP_CF_CONNECTING_IP': '1.2.3.4',
        }
        self.assertEqual(client_ip(req), '203.0.113.195')

    def test_missing_ip_headers_fallback(self):
        """When proxy headers are missing, falls back to REMOTE_ADDR or safe default."""
        req = self.factory.get('/admin/login/')
        req.META = {
            'REMOTE_ADDR': '192.0.2.1',
        }
        self.assertEqual(client_ip(req), '192.0.2.1')

        # Completely empty META
        req2 = self.factory.get('/admin/login/')
        req2.META = {}
        self.assertEqual(client_ip(req2), '127.0.0.1')

    def test_invalid_and_malformed_ip_headers(self):
        """Malformed, garbage, injection, or out-of-range IP strings must be safely handled."""
        req = self.factory.get('/admin/login/')
        req.META = {
            'REMOTE_ADDR': 'not-an-ip',
            'HTTP_X_FORWARDED_FOR': 'garbage_data, 999.999.999.999, <script>',
            'HTTP_CF_CONNECTING_IP': 'drop table users;',
        }
        # Safely falls back without throwing ValueError
        self.assertEqual(client_ip(req), '127.0.0.1')

    def test_ipv6_cloudflare_request(self):
        """Handles IPv6 addresses for both client and Cloudflare proxy correctly."""
        req = self.factory.get('/admin/login/')
        req.META = {
            'REMOTE_ADDR': '10.0.0.1',
            # 2400:cb00::1 is Cloudflare IPv6
            'HTTP_X_FORWARDED_FOR': '2001:db8:85a3::8a2e:370:7334, 2400:cb00::1',
            'HTTP_CF_CONNECTING_IP': '2001:db8:85a3::8a2e:370:7334',
        }
        self.assertEqual(client_ip(req), '2001:db8:85a3::8a2e:370:7334')

    def test_admin_login_rate_limiting_cannot_be_bypassed_by_spoofed_headers(self):
        """An attacker cannot reset/evade rate limiting by manipulating X-Forwarded-For headers."""
        attacker_ip = '198.51.100.99'
        username = 'admin'

        # Attacker attempts logins from their real IP, sending changing spoofed headers each time
        for i in range(LOGIN_MAX_ATTEMPTS):
            req = self.factory.post('/admin/login/')
            req.META = {
                'REMOTE_ADDR': '10.0.0.1',
                'HTTP_X_FORWARDED_FOR': f'10.1.{i}.{i}, 1.2.3.{i}, {attacker_ip}',
                'HTTP_CF_CONNECTING_IP': f'1.2.3.{i}',  # Direct request, untrusted
            }
            # Verify client_ip correctly identifies the attacker despite rotating headers
            self.assertEqual(client_ip(req), attacker_ip)
            key = login_key(req, username)
            register_failure(key, LOGIN_MAX_ATTEMPTS, LOGIN_LOCKOUT_SECONDS)

        # Subsequent attempt with another spoofed header
        req_attempt_next = self.factory.post('/admin/login/')
        req_attempt_next.META = {
            'REMOTE_ADDR': '10.0.0.1',
            'HTTP_X_FORWARDED_FOR': f'55.66.77.88, {attacker_ip}',
            'HTTP_CF_CONNECTING_IP': '55.66.77.88',
        }
        key_next = login_key(req_attempt_next, username)
        self.assertTrue(
            is_rate_limited(key_next, LOGIN_MAX_ATTEMPTS),
            'Attacker should be locked out despite rotating spoofed headers'
        )
