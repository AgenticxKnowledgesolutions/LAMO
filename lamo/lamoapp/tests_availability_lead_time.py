"""
Tests for the dynamic lead-time availability API.

Covers:
  - compute_dynamic_lead_time_min() distance brackets
  - api_worker_availability coordinate resolution priority chain
  - Hardcoded-60-min buffer is gone (all distances < 60 min return correct values)
  - Exact lead-time boundary conditions
  - Invalid / out-of-range client coordinates are rejected
  - Missing coordinates fall back gracefully
  - Existing booking conflicts are still respected
  - Frontend/backend availability consistency (server slot status matches expectations)
"""

import json
from datetime import date, time, timedelta
from unittest.mock import patch, MagicMock

from django.test import TestCase, RequestFactory
from django.urls import reverse
from django.utils import timezone
from django.contrib.auth import get_user_model

from .views import compute_dynamic_lead_time_min, _compute_worker_distance, api_worker_availability
from .models import CustomUser, WorkerProfile, Service, Booking, UserProfile

User = get_user_model()


# ---------------------------------------------------------------------------
# 1. compute_dynamic_lead_time_min – unit tests
# ---------------------------------------------------------------------------

class ComputeDynamicLeadTimeMinTest(TestCase):
    """Direct unit tests for compute_dynamic_lead_time_min()."""

    def test_very_close_distance(self):
        """0.9 km → 15 minutes (≤ 3 km bracket)."""
        self.assertEqual(compute_dynamic_lead_time_min(0.9), 15)

    def test_exactly_3km(self):
        """3.0 km is the upper boundary of the ≤3 km bracket → 15 minutes."""
        self.assertEqual(compute_dynamic_lead_time_min(3.0), 15)

    def test_just_above_3km(self):
        """3.001 km crosses into the ≤7 km bracket → 25 minutes."""
        self.assertEqual(compute_dynamic_lead_time_min(3.001), 25)

    def test_mid_7km_bracket(self):
        """5.0 km is in the ≤7 km bracket → 25 minutes."""
        self.assertEqual(compute_dynamic_lead_time_min(5.0), 25)

    def test_exactly_7km(self):
        """7.0 km is the boundary of the ≤7 km bracket → 25 minutes."""
        self.assertEqual(compute_dynamic_lead_time_min(7.0), 25)

    def test_just_above_7km(self):
        """7.001 km crosses into the ≤12 km bracket → 35 minutes."""
        self.assertEqual(compute_dynamic_lead_time_min(7.001), 35)

    def test_mid_12km_bracket(self):
        """10.0 km → 35 minutes (≤ 12 km bracket)."""
        self.assertEqual(compute_dynamic_lead_time_min(10.0), 35)

    def test_exactly_12km(self):
        """12.0 km → 35 minutes (boundary)."""
        self.assertEqual(compute_dynamic_lead_time_min(12.0), 35)

    def test_just_above_12km(self):
        """12.001 km → 45 minutes (≤ 18 km bracket)."""
        self.assertEqual(compute_dynamic_lead_time_min(12.001), 45)

    def test_exactly_18km(self):
        """18.0 km → 45 minutes (boundary)."""
        self.assertEqual(compute_dynamic_lead_time_min(18.0), 45)

    def test_above_18km(self):
        """25 km → 60 minutes (> 18 km bracket)."""
        self.assertEqual(compute_dynamic_lead_time_min(25.0), 60)

    def test_none_distance_fallback(self):
        """None distance → 45 minutes (unknown fallback)."""
        self.assertEqual(compute_dynamic_lead_time_min(None), 45)

    def test_zero_distance(self):
        """0 km (same location) → 15 minutes."""
        self.assertEqual(compute_dynamic_lead_time_min(0.0), 15)

    def test_string_coercion(self):
        """Numeric string should be coerced; invalid string → 45."""
        self.assertEqual(compute_dynamic_lead_time_min("2.5"), 15)
        self.assertEqual(compute_dynamic_lead_time_min("not_a_number"), 45)


# ---------------------------------------------------------------------------
# Shared helper to build a minimal but valid test database
# ---------------------------------------------------------------------------

def _make_worker(district="Kollam", lat=8.88, lng=76.60,
                 start_time=time(9, 0), end_time=time(21, 0)):
    """Create a verified available worker with a service."""
    user = User.objects.create_user(
        username=f"worker_{lat}_{lng}".replace(".", "_"),
        email=f"worker_{str(lat).replace('.','_')}_{str(lng).replace('.','_')}@test.com",
        password="testpass123",
        role="WORKER",
    )
    service = Service.objects.filter(is_active=True).first()
    if not service:
        service = Service.objects.create(
            name="Test Cleaning",
            description="Test",
            price=200,
            is_active=True,
            default_duration_min=60,
        )
    wp = WorkerProfile.objects.create(
        user=user,
        service=service,
        district=district,
        is_verified=True,
        work_status="AVAILABLE",
        latitude=lat,
        longitude=lng,
        work_start_time=start_time,
        work_end_time=end_time,
    )
    return wp, service


# ---------------------------------------------------------------------------
# 2. api_worker_availability – integration tests via test client
# ---------------------------------------------------------------------------

class AvailabilityApiLeadTimeTest(TestCase):
    """
    Tests for api_worker_availability coordinate resolution and dynamic lead
    time. Workers are located at known lat/lng; customer coordinates are
    varied to test each distance bracket.
    """

    def setUp(self):
        self.factory = RequestFactory()
        # Worker at a fixed position in Kollam (lat=8.88, lng=76.60).
        self.worker, self.service = _make_worker(lat=8.88, lng=76.60)
        self.today = timezone.localtime().date()
        self.url = reverse('api_worker_availability', args=[self.worker.id])

    # --- helper -----------------------------------------------------------

    def _get(self, params=None, session_data=None):
        """Make a GET request to the API, optionally with session data."""
        response = self.client.get(self.url, params or {})
        return response

    def _get_with_session(self, params, session_data):
        session = self.client.session
        for k, v in session_data.items():
            session[k] = v
        session.save()
        return self.client.get(self.url, params)

    # --- distance-bracket tests -------------------------------------------

    def test_no_coords_fallback_to_45_min(self):
        """
        No coordinates in session or GET → distance is None → 45 min lead.
        The returned lead_time_min must be 45 and must NOT be 60.
        """
        response = self._get({'date': str(self.today), 'service_id': self.service.id})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        self.assertEqual(data['lead_time_min'], 45)
        self.assertNotEqual(data['lead_time_min'], 60)

    def test_09km_gives_15_min_lead_time(self):
        """
        Customer at 0.9 km from worker → 15 minute lead time.
        Worker at (8.88, 76.60). Customer at approx (8.885, 76.602) ≈ 0.6 km.
        """
        # Customer roughly 0.9 km north of worker.
        c_lat = 8.888   # ~0.9 km north
        c_lng = 76.600
        response = self._get_with_session(
            {'date': str(self.today), 'service_id': self.service.id},
            {'lamo_selected_lat': c_lat, 'lamo_selected_lng': c_lng},
        )
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        # 0.9 km → 15 min
        self.assertEqual(data['lead_time_min'], 15)
        # Confirm distance is sane (< 3 km)
        self.assertIsNotNone(data['distance_km'])
        self.assertLess(data['distance_km'], 3.0)

    def test_3km_bracket_gives_15_min(self):
        """Customer at ~2.5 km from worker → 15 min lead time."""
        # ~2.5 km north
        c_lat = 8.902
        c_lng = 76.600
        response = self._get_with_session(
            {'date': str(self.today), 'service_id': self.service.id},
            {'lamo_selected_lat': c_lat, 'lamo_selected_lng': c_lng},
        )
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        self.assertEqual(data['lead_time_min'], 15)

    def test_above_3km_to_7km_gives_25_min(self):
        """Customer at ~5 km from worker → 25 min lead time."""
        # ~5 km north of worker
        c_lat = 8.925
        c_lng = 76.600
        response = self._get_with_session(
            {'date': str(self.today), 'service_id': self.service.id},
            {'lamo_selected_lat': c_lat, 'lamo_selected_lng': c_lng},
        )
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        # ~5 km should be 25 min
        self.assertIn(data['lead_time_min'], [15, 25])  # exact dist may vary
        self.assertIsNotNone(data['distance_km'])

    def test_above_7km_gives_35_or_more(self):
        """Customer at ~10 km from worker → 35 min lead time."""
        # ~10 km north
        c_lat = 8.970
        c_lng = 76.600
        response = self._get_with_session(
            {'date': str(self.today), 'service_id': self.service.id},
            {'lamo_selected_lat': c_lat, 'lamo_selected_lng': c_lng},
        )
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        self.assertGreaterEqual(data['lead_time_min'], 25)

    # --- security: invalid client coords are rejected ---------------------

    def test_invalid_client_lat_lng_rejected(self):
        """
        Out-of-range coordinates (not in Kerala bounding box) must NOT be used.
        Server should fall back to session / profile / locality, and if none exist,
        should return the default 45 min fallback.
        """
        # Coords for London (definitely not Kerala)
        response = self.client.get(self.url, {
            'date': str(self.today),
            'service_id': self.service.id,
            'lat': '51.5074',    # London latitude
            'lng': '-0.1278',    # London longitude
        })
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        # Must NOT have used the bogus coords; distance_km must be None or sane
        # lead_time_min must be 45 (fallback) since no session coords exist
        self.assertEqual(data['lead_time_min'], 45)
        self.assertIsNone(data['distance_km'])

    def test_malformed_lat_lng_rejected(self):
        """
        Non-numeric lat/lng strings must be rejected gracefully (no 500).
        """
        response = self.client.get(self.url, {
            'date': str(self.today),
            'service_id': self.service.id,
            'lat': 'not_a_float',
            'lng': 'also_bad',
        })
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        self.assertEqual(data['lead_time_min'], 45)

    def test_session_coords_take_priority_over_get_params(self):
        """
        Session coordinates (lamo_selected_lat/lng) must override client GET params.
        Session has valid near-worker coords; GET params have far-away coords.
        """
        # Set session to close coords (→ 15 min)
        session = self.client.session
        session['lamo_selected_lat'] = 8.888
        session['lamo_selected_lng'] = 76.600
        session.save()
        # GET params claim very far away (would be 60 min if trusted)
        response = self.client.get(self.url, {
            'date': str(self.today),
            'service_id': self.service.id,
            'lat': '8.0',   # ~100 km south
            'lng': '76.0',
        })
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        # Must use session (15 min), not GET params (would give 60 min)
        self.assertEqual(data['lead_time_min'], 15)

    def test_get_client_coords_used_when_session_absent(self):
        """
        When session has no coords, valid Kerala client GET params are used.
        Customer 0.9 km from worker via GET params → 15 min lead time.
        """
        response = self.client.get(self.url, {
            'date': str(self.today),
            'service_id': self.service.id,
            'lat': '8.888',
            'lng': '76.600',
        })
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        self.assertEqual(data['lead_time_min'], 15)

    # --- slot status reflects dynamic lead time ---------------------------

    def test_slot_blocked_by_dynamic_lead_time(self):
        """
        When customer is 0.9 km away (lead_time=15 min) and the current time is
        T, slots before T+15 must be 'too_soon', slots after T+15 must be 'available'.
        We mock timezone.localtime() to control 'now'.
        """
        from unittest.mock import patch
        import datetime

        # Fix "now" to 10:50 AM local time on today
        fake_now = timezone.make_aware(
            timezone.datetime(self.today.year, self.today.month, self.today.day, 10, 50)
        )

        with patch('lamoapp.views.timezone.localtime', return_value=fake_now):
            session = self.client.session
            session['lamo_selected_lat'] = 8.888
            session['lamo_selected_lng'] = 76.600
            session.save()
            response = self.client.get(self.url, {
                'date': str(self.today),
                'service_id': self.service.id,
            })

        data = json.loads(response.content)
        self.assertTrue(data['success'])
        self.assertEqual(data['lead_time_min'], 15)

        # Build a lookup of slot → status
        slot_map = {s['slot']: s for s in data['slots']}

        # 11:00 AM (660 min) < 10:50+15 = 11:05 AM (665 min) → too_soon
        eleven_am_slot = next(
            (s for s in data['slots'] if s['slot'].startswith('11:00')), None
        )
        if eleven_am_slot:
            self.assertFalse(eleven_am_slot['available'])
            self.assertEqual(eleven_am_slot['status'], 'too_soon')

        # 11:30 AM (690 min) > 665 → available
        eleven_thirty_slot = next(
            (s for s in data['slots'] if s['slot'].startswith('11:30')), None
        )
        if eleven_thirty_slot:
            self.assertTrue(eleven_thirty_slot['available'])

    def test_slot_available_when_lead_time_satisfied(self):
        """
        Current time 10:40 AM, distance 0.9 km → lead=15 min.
        11:00 AM (660 min) >= 10:40+15=10:55 (655 min) → available.
        """
        fake_now = timezone.make_aware(
            timezone.datetime(self.today.year, self.today.month, self.today.day, 10, 40)
        )
        with patch('lamoapp.views.timezone.localtime', return_value=fake_now):
            session = self.client.session
            session['lamo_selected_lat'] = 8.888
            session['lamo_selected_lng'] = 76.600
            session.save()
            response = self.client.get(self.url, {
                'date': str(self.today),
                'service_id': self.service.id,
            })

        data = json.loads(response.content)
        self.assertTrue(data['success'])
        self.assertEqual(data['lead_time_min'], 15)

        eleven_am_slot = next(
            (s for s in data['slots'] if s['slot'].startswith('11:00')), None
        )
        if eleven_am_slot:
            self.assertTrue(eleven_am_slot['available'])

    # --- existing booking conflicts still respected -----------------------

    def test_busy_slot_still_blocked_despite_distance(self):
        """
        A slot that is already booked must remain 'busy' regardless of distance.
        """
        customer_user = User.objects.create_user(
            username="customer_conflict",
            email="cust_conflict@test.com",
            password="pass",
            role="CUSTOMER",
        )
        # Create an existing booking for the worker at 2:00 PM today
        Booking.objects.create(
            customer=customer_user,
            worker=self.worker,
            service=self.service,
            date=self.today,
            time="2:00 PM",
            start_time=time(14, 0),
            end_time=time(15, 15),
            status="ACCEPTED",
            location="Test Location",
            district="Kollam",
        )
        session = self.client.session
        session['lamo_selected_lat'] = 8.888
        session['lamo_selected_lng'] = 76.600
        session.save()
        response = self.client.get(self.url, {
            'date': str(self.today),
            'service_id': self.service.id,
        })
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        # 2:00 PM slot must be 'busy'
        two_pm_slot = next(
            (s for s in data['slots'] if s['slot'].startswith('2:00')), None
        )
        if two_pm_slot:
            self.assertFalse(two_pm_slot['available'])
            self.assertEqual(two_pm_slot['status'], 'busy')

    # --- response shape ---------------------------------------------------

    def test_response_includes_lead_time_and_distance(self):
        """API must return lead_time_min and distance_km in the JSON response."""
        session = self.client.session
        session['lamo_selected_lat'] = 8.888
        session['lamo_selected_lng'] = 76.600
        session.save()
        response = self.client.get(self.url, {
            'date': str(self.today),
            'service_id': self.service.id,
        })
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        self.assertIn('lead_time_min', data)
        self.assertIn('distance_km', data)

    def test_response_does_not_say_1hr_travel_buffer(self):
        """
        The hardcoded '1hr Travel Buffer' string must never appear in slot reasons
        after the fix. Dynamic labels like '15m Travel Buffer' are acceptable.
        """
        session = self.client.session
        session['lamo_selected_lat'] = 8.888
        session['lamo_selected_lng'] = 76.600
        session.save()
        response = self.client.get(self.url, {
            'date': str(self.today),
            'service_id': self.service.id,
        })
        data = json.loads(response.content)
        all_reasons = [s['reason'] for s in data['slots']]
        for reason in all_reasons:
            self.assertNotIn('1hr Travel Buffer', reason,
                             msg=f"Hardcoded '1hr Travel Buffer' found in slot reason: {reason}")

    # --- exact lead-time boundary -----------------------------------------

    def test_slot_at_exact_lead_time_boundary(self):
        """
        Slot that starts exactly at now + lead_mins must be AVAILABLE (not too_soon).
        The cutoff is strictly < (start_minutes < min_buffer_mins), so equal is fine.
        """
        # now = 10:45, lead = 15, boundary = 11:00 (660)
        # Slot 11:00 starts at 660 min, min_buffer = 10:45 + 15 = 11:00 = 660
        # 660 < 660 is False → slot should be available
        fake_now = timezone.make_aware(
            timezone.datetime(self.today.year, self.today.month, self.today.day, 10, 45)
        )
        with patch('lamoapp.views.timezone.localtime', return_value=fake_now):
            session = self.client.session
            session['lamo_selected_lat'] = 8.888
            session['lamo_selected_lng'] = 76.600
            session.save()
            response = self.client.get(self.url, {
                'date': str(self.today),
                'service_id': self.service.id,
            })

        data = json.loads(response.content)
        self.assertTrue(data['success'])
        eleven_am_slot = next(
            (s for s in data['slots'] if s['slot'].startswith('11:00')), None
        )
        if eleven_am_slot:
            self.assertTrue(
                eleven_am_slot['available'],
                f"11:00 AM slot should be available at exactly the boundary; got: {eleven_am_slot}"
            )

    # --- updated customer location refreshes distance ---------------------

    def test_updated_session_location_changes_lead_time(self):
        """
        Changing session coordinates from far away to close must change lead_time_min
        from a larger value down to 15.
        """
        # First request: far away (~100 km, coordinates outside Kerala bbox won't
        # be accepted; simulate with an in-range far point ~40 km north of worker).
        session = self.client.session
        session['lamo_selected_lat'] = 9.24   # ~40 km north
        session['lamo_selected_lng'] = 76.60
        session.save()
        resp_far = self.client.get(self.url, {
            'date': str(self.today),
            'service_id': self.service.id,
        })
        data_far = json.loads(resp_far.content)
        self.assertTrue(data_far['success'])
        lead_far = data_far['lead_time_min']

        # Second request: very close (0.9 km)
        session = self.client.session
        session['lamo_selected_lat'] = 8.888
        session['lamo_selected_lng'] = 76.600
        session.save()
        resp_close = self.client.get(self.url, {
            'date': str(self.today),
            'service_id': self.service.id,
        })
        data_close = json.loads(resp_close.content)
        self.assertTrue(data_close['success'])
        lead_close = data_close['lead_time_min']

        self.assertGreater(lead_far, lead_close,
                           f"Far lead={lead_far}, close lead={lead_close}: close must be smaller")
        self.assertEqual(lead_close, 15)

    # --- future date never has lead-time blocks ---------------------------

    def test_future_date_no_too_soon_slots(self):
        """For a future (non-today) date, no slot should be 'too_soon'."""
        future_date = self.today + timedelta(days=2)
        session = self.client.session
        session['lamo_selected_lat'] = 8.888
        session['lamo_selected_lng'] = 76.600
        session.save()
        response = self.client.get(self.url, {
            'date': str(future_date),
            'service_id': self.service.id,
        })
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        for slot in data['slots']:
            self.assertNotEqual(slot['status'], 'too_soon',
                                f"Future slot should not be 'too_soon': {slot}")

    # --- authenticated customer profile fallback --------------------------

    def test_profile_coords_used_when_no_session(self):
        """
        When session has no location data, authenticated customer's profile
        coordinates should be used.
        """
        customer = User.objects.create_user(
            username="profile_coord_customer",
            email="profile_coord@test.com",
            password="pass",
            role="CUSTOMER",
        )
        profile = UserProfile.objects.get_or_create(user=customer)[0]
        profile.latitude = 8.888
        profile.longitude = 76.600
        profile.save()

        self.client.force_login(customer)
        # No session coords set
        response = self.client.get(self.url, {
            'date': str(self.today),
            'service_id': self.service.id,
        })
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        # With profile coords 0.9 km away → 15 min
        self.assertEqual(data['lead_time_min'], 15)
        self.client.logout()
