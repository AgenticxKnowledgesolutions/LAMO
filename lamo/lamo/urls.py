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
from django.http import HttpResponseForbidden
import os
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


urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('lamoapp.urls')),
    # Private proof documents are never publicly served.
    re_path(
        r'^{}worker_proofs/.*$'.format(
            re.escape(settings.MEDIA_URL.lstrip('/'))
        ),
        _deny_private_media,
    ),
]


# Serve media files in development (when DEBUG=True)
# Also serve when explicitly enabled via environment variable
if settings.DEBUG or os.getenv('DJANGO_SERVE_MEDIA', 'False').lower() in ('true', '1', 'yes'):
    urlpatterns += static(
        settings.MEDIA_URL,
        document_root=settings.MEDIA_ROOT
    )


# Friendly error pages (templates/403.html, templates/404.html and templates/500.html).
# Only used when DEBUG=False; Django shows its own debug page otherwise.
handler403 = 'lamoapp.views.custom_403_view'
handler404 = 'lamoapp.views.custom_404_view'
handler500 = 'lamoapp.views.custom_500_view'
