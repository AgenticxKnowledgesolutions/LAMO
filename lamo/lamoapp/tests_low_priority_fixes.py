"""Regression tests for the low-priority / UI items 27-36.

27 horizontal overflow (CSS)           32 media serving with DEBUG=False
28 Register button clipped (CSS)       33 Referrer-Policy / CSP headers
29 location-modal logo cropped         34 booking reminders: idempotent, locked, retry-safe
30 404 on page load (favicon)          35 migrations out of the web start command
31 recurring creation unreachable      36 temporary files

Rendered layout cannot be verified without a browser: the CSS/template tests
below only check the *causes* that were found in the source (see the report).
"""
import importlib
import os
import re
import tempfile
from datetime import datetime, time as dtime, timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

import yaml
from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings
from django.urls import clear_url_caches, reverse
from django.utils import timezone

from lamoapp import booking_reminders, security_headers
from lamoapp.models import Booking, BookingSeries, CustomUser, Notification
from lamoapp.tests_high_severity_fixes import _Base, _future

APP_DIR = Path(settings.BASE_DIR) / 'lamoapp'
REPO_ROOT = Path(settings.BASE_DIR).parent


def _read(path):
    return Path(path).read_text(encoding='utf8', errors='replace')


# ===========================================================================
# 27 / 28 / 29 - CSS and template causes
# ===========================================================================
class ResponsiveCssSourceTests(TestCase):
    """Static checks only. Real viewport behaviour needs a browser."""

    def test_full_width_reveal_sections_are_not_translated_sideways(self):
        css = _read(APP_DIR / 'static/css/home.css')
        for rule in re.findall(r'\.scroll-reveal\.reveal-(?:left|right)[^{]*\{[^}]*\}', css):
            self.assertNotIn('translateX', rule, rule)
        # The sections that carry the modifiers are full viewport width.
        home = _read(Path(settings.BASE_DIR) / 'templates/home.html')
        self.assertIn('scroll-reveal reveal-right', home)

    def test_no_horizontal_translate_remains_on_reveal_classes(self):
        css = _read(APP_DIR / 'static/css/home.css')
        self.assertIsNone(re.search(r'reveal-(?:left|right)\s*\{[^}]*translateX\(\s*-?\d{2,}', css))

    def test_register_button_fit_rules_exist_for_desktop_widths(self):
        css = _read(APP_DIR / 'static/css/base.css')
        self.assertIn('DESKTOP NAVBAR FIT', css)
        block = css[css.index('DESKTOP NAVBAR FIT'):]
        self.assertRegex(block, r'\.navbar \.nav-pill \{ min-width: 0')
        self.assertRegex(block, r'\.navbar \.nav-actions \{ flex-shrink: 0')
        self.assertIn('max-width: 1399.98px', block)

    def test_register_link_destination_and_trigger_unchanged(self):
        base = _read(Path(settings.BASE_DIR) / 'templates/base.html')
        self.assertIn('''<a href="{% url 'register' %}" class="btn-register js-auth-trigger" data-auth-view="register">''', base)

    def test_location_modal_logo_is_not_cropped(self):
        html = _read(Path(settings.BASE_DIR) / 'templates/includes/location_modal.html')
        svg = html[html.index('<svg class="loc-brand-svg-logo"'):html.index('</svg>')]
        self.assertIn('overflow="visible"', svg)
        self.assertIn('textLength=', svg)
        vb = [float(v) for v in re.search(r'viewBox="([^"]+)"', svg).group(1).split()]
        text_x = float(re.search(r'<text x="([\d.]+)"', svg).group(1))
        text_len = float(re.search(r'textLength="([\d.]+)"', svg).group(1))
        self.assertLessEqual(text_x + text_len, vb[2], 'wordmark must fit inside the viewBox')
        css = _read(APP_DIR / 'static/css/location-modal.css')
        rule = css[css.index('.loc-brand-svg-logo {'):]
        rule = rule[:rule.index('}')]
        self.assertIn('overflow: visible', rule)
        self.assertIn('width: auto', rule)

    def test_location_modal_functionality_markup_is_preserved(self):
        html = _read(Path(settings.BASE_DIR) / 'templates/includes/location_modal.html')
        for needle in ('id="locDistrictFilterInput"', 'id="btnGpsAutoDetect"', 'id="locStepDistrictsView"',
                       'data-initial-district='):
            self.assertIn(needle, html)


# ===========================================================================
# 30 - the 404 on page load
# ===========================================================================
class MissingResourceTests(TestCase):

    def test_every_static_reference_in_templates_exists(self):
        roots = [APP_DIR / 'static', Path(settings.BASE_DIR) / 'static']
        missing = []
        for tpl in (Path(settings.BASE_DIR) / 'templates').rglob('*.html'):
            for ref in re.findall(r"""\{%\s*static\s+['"]([^'"]+)['"]""", _read(tpl)):
                if not any((r / ref).exists() for r in roots):
                    missing.append((tpl.name, ref))
        self.assertEqual(missing, [])

    def test_base_template_declares_a_favicon(self):
        # First visit renders the splash page, later visits the full site.
        for url in (reverse('home'), reverse('home') + '?from_splash=1'):
            html = Client().get(url).content.decode()
            self.assertRegex(html, r'<link rel="icon"[^>]+lamo_icon_dark\.png', url)
        self.assertTrue((APP_DIR / 'static/img/lamo_icon_dark.png').exists())

    def test_favicon_ico_no_longer_404s(self):
        res = Client().get('/favicon.ico')
        self.assertEqual(res.status_code, 302)
        self.assertIn('lamo_icon_dark.png', res['Location'])


# ===========================================================================
# 31 - recurring booking creation
# ===========================================================================
class RecurringCreationBlockedTests(_Base):

    def payload(self, booking_type='Recurring', **extra):
        data = {
            'worker': self.worker.id, 'service': self.service.id, 'booking_type': booking_type,
            'date': _future(5), 'time': '10:00-12:00', 'location': 'Kollam', 'phone': '9876543210',
            'description': 'x', 'start_date': _future(5), 'end_date': _future(30), 'frequency': 'Weekly',
            'preferred_day': 'Monday', 'customer_name': 'Guest', 'customer_email': 'guest@example.com',
        }
        data.update(extra)
        return data

    def assert_nothing_created(self):
        self.assertEqual(Booking.objects.count(), 0)
        self.assertEqual(BookingSeries.objects.count(), 0)

    def test_logged_in_form_post_is_rejected(self):
        res = self.c.post(reverse('book_service'), self.payload())
        self.assertEqual(res.status_code, 302)
        self.assert_nothing_created()

    def test_logged_in_ajax_gets_clear_400_json(self):
        res = self.c.post(reverse('book_service'), self.payload(), HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(res.status_code, 400)
        body = res.json()
        self.assertFalse(body['success'])
        self.assertIn('Recurring bookings are temporarily unavailable', body['error_message'])
        self.assert_nothing_created()

    def test_guest_is_rejected_before_any_otp_or_session_state(self):
        guest = Client()
        with mock.patch('lamoapp.views._send_booking_verification_code') as send:
            for ajax in (False, True):
                kwargs = {'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'} if ajax else {}
                res = guest.post(reverse('book_service'), self.payload(), **kwargs)
                if ajax:
                    self.assertEqual(res.status_code, 400)
            send.assert_not_called()
        self.assertNotIn('pending_booking_data', guest.session)
        self.assert_nothing_created()

    def test_case_and_spacing_variants_cannot_bypass(self):
        for raw in ('recurring', 'RECURRING', ' Re-Curring ', 'recurring', 'Recurring'):
            with self.subTest(raw=raw):
                self.c.post(reverse('book_service'), self.payload(raw))
                self.assert_nothing_created()

    def test_pending_session_with_recurring_cannot_create_series(self):
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory
        from lamoapp import views
        request = RequestFactory().post('/')
        request.user = self.customer
        request.session = self.c.session
        setattr(request, '_messages', FallbackStorage(request))
        pending = {'worker_id': self.worker.id, 'service_id': self.service.id, 'booking_type': 'Recurring',
                   'date': _future(5), 'time': '10:00-12:00', 'location': 'Kollam', 'phone': '9876543210',
                   'start_date': _future(5), 'end_date': _future(30), 'frequency': 'Weekly'}
        self.assertIsNone(views._create_booking_from_pending(request, self.customer, pending))
        self.assert_nothing_created()

    def test_series_creation_code_is_not_reachable_from_book_service(self):
        source = _read(APP_DIR / 'views.py')
        book = source[source.index('def book_service(request):'):]
        book = book[:book.index('\ndef ', 10)]
        self.assertNotIn('BookingSeries.objects.create', book)
        self.assertNotIn('_handle_recurring_booking', book)

    def test_one_time_booking_still_works(self):
        res = self.c.post(reverse('book_service'), self.payload('One Time'))
        self.assertEqual(res.status_code, 302)
        self.assertEqual(Booking.objects.get(customer=self.customer).booking_type, 'One Time')

    def test_emergency_booking_flow_still_works(self):
        res = self.c.post(reverse('book_service'), {
            'service': self.service.id, 'problem': 'Pipe burst', 'time': 'ASAP', 'location': 'Kollam',
            'phone': '9876543210', 'description': 'leak'})
        self.assertNotEqual(res.status_code, 500)
        self.assertEqual(BookingSeries.objects.count(), 0)


class ExistingRecurringHistoryTests(_Base):

    def setUp(self):
        super().setUp()
        start = timezone.localdate() + timedelta(days=2)
        self.series = BookingSeries.objects.create(
            customer=self.customer, worker=self.worker, service=self.service, frequency='Daily',
            time='09:00 AM - 09:30 AM', location='Kollam', phone='9876543210',
            start_date=start, end_date=start + timedelta(days=2))
        self.visits = [Booking.objects.create(
            customer=self.customer, worker=self.worker, service=self.service, booking_type='Recurring',
            date=start + timedelta(days=i), time='09:00 AM - 09:30 AM', location='Kollam',
            phone='9876543210', status='PENDING', estimated_cost=100, series=self.series) for i in range(2)]

    def test_history_page_lists_series(self):
        res = self.c.get(reverse('my_series'))
        self.assertEqual(res.status_code, 200)

    def test_customer_can_pause_and_resume(self):
        self.c.post(reverse('series_toggle_active', args=[self.series.id]))
        self.series.refresh_from_db()
        self.assertFalse(self.series.is_active)
        self.c.post(reverse('series_toggle_active', args=[self.series.id]))
        self.series.refresh_from_db()
        self.assertTrue(self.series.is_active)

    def test_customer_can_cancel_and_history_is_kept(self):
        self.c.post(reverse('series_cancel', args=[self.series.id]))
        self.series.refresh_from_db()
        self.assertFalse(self.series.is_active)
        self.assertEqual(Booking.objects.filter(series=self.series).count(), 2)   # rows are kept, not deleted
        self.assertEqual(BookingSeries.objects.count(), 1)

    def test_other_customer_cannot_manage_the_series(self):
        other = CustomUser.objects.create_user('hs_other', 'o@x.in', 'Pass123!x', role='USER')
        c = Client()
        c.force_login(other)
        c.post(reverse('series_cancel', args=[self.series.id]))
        self.series.refresh_from_db()
        self.assertTrue(self.series.is_active)

    def test_worker_can_still_accept_an_existing_series(self):
        res = self.w.post(reverse('worker_accept_booking', args=[self.visits[0].id]))
        self.assertIn(res.status_code, (200, 302))
        self.assertTrue(all(Booking.objects.get(pk=v.pk).status == 'ACCEPTED' for v in self.visits))


# ===========================================================================
# 32 - media serving
# ===========================================================================
class MediaServingTests(TestCase):

    def _urls(self, serve_media, root):
        import lamo.urls as urls_module
        env = {'DJANGO_SERVE_MEDIA': serve_media}
        with mock.patch.dict(os.environ, env, clear=False), override_settings(MEDIA_ROOT=root):
            module = importlib.reload(urls_module)
        clear_url_caches()
        self.addCleanup(clear_url_caches)
        self.addCleanup(importlib.reload, urls_module)
        return module

    def _client_for(self, module):
        return override_settings(ROOT_URLCONF=module.__name__)

    def test_flag_now_serves_public_prefixes_with_debug_false(self):
        self.assertFalse(settings.DEBUG)
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'services').mkdir()
            (Path(root) / 'services' / 'a.txt').write_bytes(b'public-image')
            (Path(root) / 'worker_proofs').mkdir()
            (Path(root) / 'worker_proofs' / 'id.txt').write_bytes(b'SECRET-KYC')
            (Path(root) / 'other').mkdir()
            (Path(root) / 'other' / 'x.txt').write_bytes(b'not-public')
            module = self._urls('True', Path(root))
            with self._client_for(module):
                c = Client()
                ok = c.get('/media/services/a.txt')
                self.assertEqual(ok.status_code, 200)
                self.assertEqual(b''.join(ok.streaming_content), b'public-image')
                ok.close()
                self.assertEqual(c.get('/media/worker_proofs/id.txt').status_code, 403)
                self.assertEqual(c.get('/media/other/x.txt').status_code, 404)
                self.assertEqual(c.get('/media/services/../worker_proofs/id.txt').status_code, 404)

    def test_flag_off_serves_nothing_from_media(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'services').mkdir()
            (Path(root) / 'services' / 'a.txt').write_bytes(b'x')
            module = self._urls('False', Path(root))
            with self._client_for(module):
                self.assertEqual(Client().get('/media/services/a.txt').status_code, 404)

    def test_whitelist_never_includes_private_prefixes(self):
        import lamo.urls as u
        pats = u.public_media_patterns('/media/', ['services/', 'worker_proofs/', 'users/'], '/tmp')
        regex = pats[0].pattern.regex
        self.assertTrue(regex.match('media/users/a.png'))
        self.assertFalse(regex.match('media/worker_proofs/a.pdf'))
        self.assertEqual(u.public_media_patterns('/media/', ['worker_proofs/'], '/tmp'), [])

    def test_cloudinary_configuration_is_intact(self):
        self.assertEqual(settings.STORAGES['default']['BACKEND'], 'lamoapp.cloudinary_storage.PrefixCloudinaryStorage')
        self.assertEqual(set(settings.CLOUDINARY_PUBLIC_PREFIXES), {'services/', 'workers/', 'users/'})
        self.assertNotIn('worker_proofs/', settings.CLOUDINARY_PUBLIC_PREFIXES)
        self.assertIn('res.cloudinary.com', security_headers.build_csp())

    def test_proof_document_endpoint_requires_admin(self):
        url = reverse('worker_proof_document', args=[1])
        res = Client().get(url)
        self.assertIn(res.status_code, (302, 403, 404))
        self.assertNotEqual(res.status_code, 200)

    def test_render_yaml_no_longer_enables_media_serving(self):
        data = yaml.safe_load(_read(REPO_ROOT / 'render.yaml'))
        web = next(s for s in data['services'] if s['type'] == 'web')
        env = {e['key']: e.get('value') for e in web['envVars']}
        self.assertEqual(env['DJANGO_SERVE_MEDIA'], 'False')


# ===========================================================================
# 33 - security headers
# ===========================================================================
class SecurityHeaderTests(TestCase):

    def test_referrer_policy_is_returned(self):
        res = Client().get(reverse('home'))
        self.assertEqual(res['Referrer-Policy'], 'strict-origin-when-cross-origin')
        self.assertEqual(settings.SECURE_REFERRER_POLICY, 'strict-origin-when-cross-origin')

    def test_csp_is_report_only_by_default(self):
        res = Client().get(reverse('home'))
        self.assertIn('Content-Security-Policy-Report-Only', res)
        self.assertNotIn('Content-Security-Policy', {h for h in res.headers if h == 'Content-Security-Policy'})

    def test_enforce_mode_switches_header_name(self):
        with mock.patch.dict(os.environ, {'DJANGO_CSP_MODE': 'enforce'}):
            mw = security_headers.SecurityHeadersMiddleware(lambda r: __import__('django.http').http.HttpResponse('x'))
            res = mw(object())
        self.assertIn('Content-Security-Policy', res)
        self.assertNotIn('Content-Security-Policy-Report-Only', res)

    def test_off_mode_adds_no_csp(self):
        with mock.patch.dict(os.environ, {'DJANGO_CSP_MODE': 'off'}):
            mw = security_headers.SecurityHeadersMiddleware(lambda r: __import__('django.http').http.HttpResponse('x'))
            res = mw(object())
        self.assertNotIn('Content-Security-Policy-Report-Only', res)
        self.assertEqual(res['Referrer-Policy'], 'strict-origin-when-cross-origin')

    def test_invalid_mode_falls_back_to_report_only(self):
        with mock.patch.dict(os.environ, {'DJANGO_CSP_MODE': 'bogus'}):
            self.assertEqual(security_headers.csp_mode(), 'report-only')

    def test_policy_keeps_required_integrations_and_has_no_wildcards(self):
        csp = security_headers.build_csp()
        for host in ('https://cdn.jsdelivr.net', 'https://unpkg.com', 'https://fonts.googleapis.com',
                     'https://fonts.gstatic.com', 'https://res.cloudinary.com', 'https://nominatim.openstreetmap.org',
                     'https://*.tile.openstreetmap.org', 'https://server.arcgisonline.com'):
            self.assertIn(host, csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertIn("base-uri 'self'", csp)
        self.assertIn("form-action 'self'", csp)
        for directive in csp.split('; '):
            self.assertNotRegex(directive, r"(^|\s)(\*|https:|http:)(\s|$)")      # no bare wildcards

    def test_every_external_host_in_templates_is_allowed(self):
        csp = security_headers.build_csp()
        hosts = set()
        for tpl in (Path(settings.BASE_DIR) / 'templates').rglob('*.html'):
            hosts.update(re.findall(r"""(?:src|href)=["'](https://[^/"']+)""", _read(tpl)))
        # <a href> links to other sites are navigation, not loaded resources.
        loaded = {h for h in hosts if h != 'https://agenticx.co.in'}
        self.assertEqual({h for h in loaded if h not in csp}, set())

    def test_report_uri_is_optional(self):
        with mock.patch.dict(os.environ, {'DJANGO_CSP_REPORT_URI': '/csp-report/'}):
            self.assertIn('report-uri /csp-report/', security_headers.build_csp())
        os.environ.pop('DJANGO_CSP_REPORT_URI', None)
        self.assertNotIn('report-uri', security_headers.build_csp())

    def test_existing_security_settings_are_preserved(self):
        text = _read(Path(settings.BASE_DIR) / 'lamo/settings.py')
        for needle in ('SECURE_HSTS_SECONDS = 31536000', 'SESSION_COOKIE_SECURE = True',
                       'CSRF_COOKIE_SECURE = True', "X_FRAME_OPTIONS = 'DENY'", 'SECURE_SSL_REDIRECT = True'):
            self.assertIn(needle, text)
        self.assertIn('lamoapp.security_headers.SecurityHeadersMiddleware', settings.MIDDLEWARE)


# ===========================================================================
# 34 - booking reminders
# ===========================================================================
class BookingReminderTests(_Base):

    def setUp(self):
        super().setUp()
        self.now = timezone.make_aware(datetime.combine(timezone.localdate(), dtime(12, 0)))
        self.booking = Booking.objects.create(
            customer=self.customer, worker=self.worker, service=self.service,
            date=self.now.date(), time='12:30 PM - 01:00 PM', start_time=dtime(12, 30), end_time=dtime(13, 0),
            location='Kollam', phone='9876543210', status='ACCEPTED', estimated_cost=100)

    def reminder_count(self):
        return Notification.objects.filter(booking=self.booking, message__startswith='Upcoming Service Reminder:').count()

    def test_repeated_processing_sends_each_reminder_once(self):
        first = booking_reminders.process_booking_schedule_notifications(now=self.now)
        second = booking_reminders.process_booking_schedule_notifications(now=self.now)
        third = booking_reminders.process_booking_schedule_notifications(now=self.now)
        self.assertEqual(first, 1)
        self.assertEqual((second, third), (0, 0))
        self.assertEqual(self.reminder_count(), 2)          # one for the customer, one for the worker

    def test_management_command_twice_is_idempotent(self):
        call_command('process_booking_reminders', verbosity=0)
        before = Notification.objects.count()
        call_command('process_booking_reminders', verbosity=0)
        self.assertEqual(Notification.objects.count(), before)

    def test_second_concurrent_run_is_skipped_while_lock_is_held(self):
        acquired, token = booking_reminders._acquire_maintenance_lock()
        self.assertTrue(acquired)
        with mock.patch.object(booking_reminders, 'process_booking_schedule_notifications') as sched:
            results = booking_reminders.run_scheduled_booking_maintenance()
        self.assertTrue(results['skipped'])
        sched.assert_not_called()
        booking_reminders._release_maintenance_lock(token)

    def test_overlapping_run_started_during_a_run_does_nothing(self):
        inner = {}

        def reentrant(now=None):
            inner['results'] = booking_reminders.run_scheduled_booking_maintenance()
            return 0
        with mock.patch.object(booking_reminders, 'process_booking_schedule_notifications', side_effect=reentrant):
            outer = booking_reminders.run_scheduled_booking_maintenance()
        self.assertFalse(outer['skipped'])
        self.assertTrue(inner['results']['skipped'])

    def test_lock_is_released_after_a_run_and_after_a_failure(self):
        booking_reminders.run_scheduled_booking_maintenance()
        self.assertFalse(booking_reminders.run_scheduled_booking_maintenance()['skipped'])
        with mock.patch.object(booking_reminders, 'process_stuck_bookings', side_effect=RuntimeError('boom')):
            booking_reminders.run_scheduled_booking_maintenance()
        self.assertFalse(booking_reminders.run_scheduled_booking_maintenance()['skipped'])

    def test_a_failing_step_is_logged_and_does_not_stop_the_others(self):
        with mock.patch.object(booking_reminders, 'process_booking_schedule_notifications', side_effect=RuntimeError('boom')), \
                mock.patch.object(booking_reminders, 'process_stuck_bookings', return_value=7), \
                self.assertLogs('lamoapp.booking_reminders', level='ERROR') as logs:
            results = booking_reminders.run_scheduled_booking_maintenance()
        self.assertEqual(results['errors'], 1)
        self.assertEqual(results['stuck_followups'], 7)
        self.assertTrue(any('reminders' in line for line in logs.output))

    def test_failed_reminder_is_retried_on_the_next_run(self):
        real = Notification.objects.create
        calls = {'n': 0}

        def flaky(*args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError('db hiccup')
            return real(*args, **kwargs)
        with mock.patch.object(Notification.objects, 'create', side_effect=flaky):
            booking_reminders.process_booking_schedule_notifications(now=self.now)
        self.assertEqual(self.reminder_count(), 0)           # nothing was marked as sent
        booking_reminders.process_booking_schedule_notifications(now=self.now)
        self.assertEqual(self.reminder_count(), 2)

    def test_command_exits_nonzero_when_a_step_failed(self):
        with mock.patch.object(booking_reminders, 'process_stuck_bookings', side_effect=RuntimeError('boom')):
            with self.assertRaises(CommandError):
                call_command('process_booking_reminders', verbosity=0, stdout=StringIO())

    def test_run_still_works_if_the_cache_is_down(self):
        with mock.patch('django.core.cache.cache.add', side_effect=RuntimeError('redis down')):
            results = booking_reminders.run_scheduled_booking_maintenance()
        self.assertFalse(results['skipped'])
        self.assertEqual(results['errors'], 0)

    def test_on_request_trigger_is_still_available_as_fallback(self):
        from lamoapp import views
        with override_settings(BOOKING_MAINTENANCE_ON_REQUEST=True), \
                mock.patch('lamoapp.booking_reminders.run_scheduled_booking_maintenance') as run:
            views.trigger_booking_reminders_throttled()
            views.trigger_booking_reminders_throttled()       # throttled: only one run per minute
        self.assertEqual(run.call_count, 1)
        cache.clear()
        with override_settings(BOOKING_MAINTENANCE_ON_REQUEST=False), \
                mock.patch('lamoapp.booking_reminders.run_scheduled_booking_maintenance') as run:
            views.trigger_booking_reminders_throttled()
        run.assert_not_called()


# ===========================================================================
# 35 - deployment configuration
# ===========================================================================
class RenderBlueprintTests(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.blueprint = yaml.safe_load(_read(REPO_ROOT / 'render.yaml'))
        cls.web = next(s for s in cls.blueprint['services'] if s['type'] == 'web')

    def test_start_command_only_starts_gunicorn(self):
        start = self.web['startCommand']
        self.assertNotIn('migrate', start)
        self.assertIn('gunicorn lamo.wsgi', start)
        self.assertTrue(start.startswith('cd lamo && '))
        self.assertIn('--workers 2', start)

    def test_migrations_run_once_in_pre_deploy(self):
        pre = self.web['preDeployCommand']
        self.assertEqual(pre, 'cd lamo && python manage.py migrate --noinput')
        self.assertNotIn('migrate', self.web['buildCommand'])

    def test_wsgi_module_in_start_command_exists(self):
        self.assertTrue((Path(settings.BASE_DIR) / 'lamo' / 'wsgi.py').exists())
        self.assertTrue(hasattr(importlib.import_module('lamo.wsgi'), 'application'))

    def test_cron_service_runs_the_reminder_command(self):
        cron = next(s for s in self.blueprint['services'] if s['type'] == 'cron')
        self.assertEqual(cron['startCommand'], 'cd lamo && python manage.py process_booking_reminders')
        self.assertRegex(cron['schedule'], r'^\*/\d+ \* \* \* \*$')
        env = {e['key']: e for e in cron['envVars']}
        for key in ('DJANGO_SECRET_KEY', 'DATABASE_URL', 'DJANGO_CACHE_URL'):
            self.assertIs(env[key].get('sync'), False)        # secrets are entered in the dashboard
        self.assertEqual(env['DJANGO_ENV']['value'], 'production')

    def test_command_referenced_by_cron_exists(self):
        from django.core.management import get_commands
        self.assertIn('process_booking_reminders', get_commands())

    def test_no_secret_values_in_blueprint(self):
        for svc in self.blueprint['services']:
            for e in svc['envVars']:
                if e['key'].endswith(('SECRET_KEY', 'API_KEY', 'API_SECRET', 'PASSWORD', 'AUTH_TOKEN')) \
                        or e['key'] in ('DATABASE_URL', 'DJANGO_CACHE_URL'):
                    self.assertNotIn('value', e, e['key'])

    def test_new_environment_variables_are_documented(self):
        documented = _read(REPO_ROOT / '.env.example') + _read(REPO_ROOT / 'README.md')
        for key in ('DJANGO_CSP_MODE', 'DJANGO_CSP_REPORT_URI', 'BOOKING_MAINTENANCE_ON_REQUEST',
                    'DJANGO_SERVE_MEDIA', 'DJANGO_ADMIN_URL'):
            self.assertIn(key, documented, key)


# ===========================================================================
# 36 - temporary files
# ===========================================================================
class TemporaryFilesTests(TestCase):

    def test_debug_artifacts_are_gone_and_ignored(self):
        for rel in ('temp_render.py', 'temp_out.txt', 'home_render.html',
                    'lamo/temp_render.py', 'lamo/temp_out.txt'):
            self.assertFalse((REPO_ROOT / rel).exists(), rel)
        ignore = _read(REPO_ROOT / '.gitignore')
        for name in ('temp_render.py', 'temp_out.txt', 'home_render.html'):
            self.assertIn(name, ignore)

    def test_nothing_in_the_code_base_references_them(self):
        pattern = re.compile(r'temp_render|temp_out\.txt|home_render\.html')
        for path in list(APP_DIR.rglob('*.py')) + list((Path(settings.BASE_DIR) / 'templates').rglob('*.html')) \
                + [REPO_ROOT / 'render.yaml']:
            if path.name == 'tests_low_priority_fixes.py':
                continue
            self.assertIsNone(pattern.search(_read(path)), str(path))
