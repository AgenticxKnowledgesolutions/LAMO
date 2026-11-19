"""Referrer-Policy and Content-Security-Policy response headers.

The policy is derived from what the templates actually load (audited from the
source): Bootstrap / Bootstrap Icons (cdn.jsdelivr.net), AOS and Leaflet
(unpkg.com), Google Fonts (fonts.googleapis.com / fonts.gstatic.com),
Cloudinary images, Unsplash images, OpenStreetMap / Esri map tiles, and
Nominatim reverse-geocoding requests.

Pages use many inline <script> / <style> blocks and inline event handlers, so
``'unsafe-inline'`` is still required for script-src and style-src. That is a
known weakness, which is why the policy ships as **Report-Only** by default:
nothing can be blocked while violations are collected. Move to enforcement with
``DJANGO_CSP_MODE=enforce`` after the browser console / report endpoint is clean.

``DJANGO_CSP_MODE``: ``report-only`` (default) | ``enforce`` | ``off``.
``DJANGO_CSP_REPORT_URI``: optional URL that receives violation reports.
"""
import os
from urllib.parse import urlparse

from django.conf import settings

REFERRER_POLICY = 'strict-origin-when-cross-origin'

_JSDELIVR = 'https://cdn.jsdelivr.net'
_UNPKG = 'https://unpkg.com'


def _tile_hosts():
    """Image hosts for map tiles, including a custom MAP_TILE_URL if configured."""
    hosts = [
        'https://*.tile.openstreetmap.org',
        'https://*.tile.openstreetmap.fr',
        'https://server.arcgisonline.com',
    ]
    url = getattr(settings, 'MAP_TILE_URL', '') or ''
    netloc = urlparse(url).netloc
    if url.startswith('https://') and netloc:
        origin = 'https://' + netloc.replace('{s}', '*')
        if origin not in hosts:
            hosts.append(origin)
    return hosts


def build_csp():
    cloudinary = ['https://res.cloudinary.com']
    img = (["'self'", 'data:', 'blob:'] + cloudinary
           + ['https://images.unsplash.com', _UNPKG] + _tile_hosts())
    directives = [
        ('default-src', ["'self'"]),
        # 'unsafe-inline': templates contain inline scripts and handlers (see module docstring).
        ('script-src', ["'self'", "'unsafe-inline'", _JSDELIVR, _UNPKG]),
        ('style-src', ["'self'", "'unsafe-inline'", _JSDELIVR, _UNPKG, 'https://fonts.googleapis.com']),
        ('font-src', ["'self'", 'data:', _JSDELIVR, 'https://fonts.gstatic.com']),
        ('img-src', img),
        ('media-src', ["'self'", 'blob:'] + cloudinary),
        ('connect-src', ["'self'", 'https://nominatim.openstreetmap.org'] + cloudinary),
        ('worker-src', ["'self'"]),
        ('manifest-src', ["'self'"]),
        ('object-src', ["'none'"]),
        ('base-uri', ["'self'"]),
        ('form-action', ["'self'"]),
        # Matches X-Frame-Options: DENY.
        ('frame-ancestors', ["'none'"]),
    ]
    parts = [name + ' ' + ' '.join(values) for name, values in directives]
    report_uri = (os.environ.get('DJANGO_CSP_REPORT_URI') or '').strip()
    if report_uri:
        parts.append('report-uri ' + report_uri)
    return '; '.join(parts)


def csp_mode():
    mode = (os.environ.get('DJANGO_CSP_MODE') or 'report-only').strip().lower()
    return mode if mode in ('report-only', 'enforce', 'off') else 'report-only'


class SecurityHeadersMiddleware:
    """Adds Referrer-Policy and Content-Security-Policy[-Report-Only]."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.mode = csp_mode()
        self.policy = build_csp()

    def __call__(self, request):
        response = self.get_response(request)
        if 'Referrer-Policy' not in response:
            response['Referrer-Policy'] = REFERRER_POLICY
        if self.mode != 'off':
            header = 'Content-Security-Policy' if self.mode == 'enforce' else 'Content-Security-Policy-Report-Only'
            if header not in response:
                response[header] = self.policy
        return response
