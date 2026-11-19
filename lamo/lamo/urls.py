"""
URL configuration for lamo project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Import an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Import an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include, re_path

from django.conf import settings
from django.conf.urls.static import static
from django.http import Http404, HttpResponseForbidden
from django.views.static import serve
import os
import posixpath
import re


def _deny_private_media(request):
    """Worker proof documents are private verification records.

    They are only reachable through the admin-authenticated
    ``lamoapp.views.proof_download`` endpoint.  This rule is registered
    unconditionally, so even when DEBUG=True or DJANGO_SERVE_MEDIA=True
    serves /media/, identity documents are never delivered from their
    public URL.
    """
    return HttpResponseForbidden('Access denied.')


def resolve_admin_url(raw, is_production):
    """Return the URL prefix for the Django admin site, or None to not mount it.

    * Development / tests: ``DJANGO_ADMIN_URL`` if set, else the familiar ``admin/``.
    * Production: ``DJANGO_ADMIN_URL`` is REQUIRED. When it is missing, blank,
      or still the predictable ``admin``, the Django admin is not exposed at all
      (it never silently falls back to ``/admin/``). LAMO's own admin dashboard
      and ``/admin-login/`` portal are unaffected.

    A hidden path is only an extra layer: Django's own authentication and
    permissions still protect the admin site.
    """
    cleaned = (raw or '').strip().strip('/')
    if is_production:
        if not cleaned or cleaned.lower() == 'admin':
            return None
        return cleaned + '/'
    return (cleaned or 'admin') + '/'


admin_url = resolve_admin_url(os.environ.get('DJANGO_ADMIN_URL'), getattr(settings, 'IS_PRODUCTION', False))

urlpatterns = [
    path('', include('lamoapp.urls')),
    # Private proof documents are never publicly served.
    re_path(
        r'^{}worker_proofs/.*$'.format(
            re.escape(settings.MEDIA_URL.lstrip('/'))
        ),
        _deny_private_media,
    ),
]


def _serve_public_media(request, path, document_root, prefixes):
    """``serve`` for whitelisted prefixes, re-checked AFTER normalisation.

    ``django.views.static.serve`` collapses ``services/../worker_proofs/x`` to
    ``worker_proofs/x`` - still inside MEDIA_ROOT, so it would be served. The
    normalised path must therefore itself start with an allowed prefix (and may
    not contain ``..`` at all), otherwise it is a 404.
    """
    parts = path.replace('\\', '/').split('/')
    normalised = posixpath.normpath(path.replace('\\', '/')).lstrip('/')
    if '..' in parts or normalised.split('/')[0] not in prefixes or normalised.split('/')[0] == 'worker_proofs':
        raise Http404('Not found')
    return serve(request, normalised, document_root=document_root)


def public_media_patterns(media_url, prefixes, document_root):
    """Serve ONLY the public upload prefixes from local disk.

    ``django.conf.urls.static.static()`` returns no routes at all when
    DEBUG=False, so ``DJANGO_SERVE_MEDIA=True`` used to do nothing in
    production. This serves the local-disk *fallback* for public images
    (services/, workers/, users/ - used only when Cloudinary is not configured)
    through an explicit whitelist: private directories such as worker_proofs/
    never match, and anything else under MEDIA_ROOT is not reachable.
    """
    allowed = tuple(p.strip('/') for p in prefixes if p.strip('/') and p.strip('/') != 'worker_proofs')
    if not allowed:
        return []
    return [re_path(
        r'^{}(?P<path>(?:{})/.+)$'.format(
            re.escape(media_url.lstrip('/')), '|'.join(re.escape(p) for p in allowed)),
        _serve_public_media,
        {'document_root': document_root, 'prefixes': allowed},
    )]


if admin_url:
    urlpatterns.insert(0, path(admin_url, admin.site.urls))

if settings.DEBUG:
    # Development: everything except worker_proofs/ (denied by the rule above).
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
elif os.getenv('DJANGO_SERVE_MEDIA', 'False').lower() in ('true', '1', 'yes'):
    urlpatterns += public_media_patterns(
        settings.MEDIA_URL,
        getattr(settings, 'CLOUDINARY_PUBLIC_PREFIXES', ()),
        settings.MEDIA_ROOT,
    )


# Friendly error pages (templates/403.html, templates/404.html and templates/500.html).
# Only used when DEBUG=False; Django shows its own debug page otherwise.
handler403 = 'lamoapp.views.custom_403_view'
handler404 = 'lamoapp.views.custom_404_view'
handler500 = 'lamoapp.views.custom_500_view'
