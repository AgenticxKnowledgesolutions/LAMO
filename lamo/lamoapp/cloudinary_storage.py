"""Prefix-routing default storage: public images -> Cloudinary, else local.

Why a routing *default* backend instead of per-field ``storage=`` kwargs
------------------------------------------------------------------------
``FileField.deconstruct()`` serialises any non-default ``storage=`` kwarg
into migrations, so touching the four public image fields would force a new
migration (forbidden by the zero-migration constraint). Routing inside the
*default* storage backend instead is migration-free: the model definitions
are byte-identical, and ``makemigrations --check`` stays clean. The end
state is the same - public image uploads land on Cloudinary while private
worker proofs always stay on local disk.

Which prefixes route where
--------------------------
CLOUDINARY_PUBLIC_PREFIXES (default ``services/ workers/ users/``):

* ``services/``  - Service.image and ServiceRequest.image
* ``workers/``   - WorkerProfile.profile_image
* ``users/``     - UserProfile.profile_image

Everything else - in particular ``worker_proofs/`` - is delegated to the
local ``FileSystemStorage`` rooted at ``MEDIA_ROOT``, so
``lamoapp.views.proof_download`` keeps serving private verification records
from disk exactly as before. Any unconfigured/unrecognised name also falls
back to local storage (fail-closed for privacy: a proof must never end up
on a public CDN because of a typo in the prefix list).

When Cloudinary is disabled
---------------------------
Cloudinary is *opt-in*: it activates only when all three of
``CLOUDINARY_CLOUD_NAME`` / ``CLOUDINARY_API_KEY`` /
``CLOUDINARY_API_SECRET`` are non-blank. Otherwise every name - including
the public prefixes - is stored locally, so a fresh checkout, CI and the
test suite behave exactly as they always have (no network, no credentials).

Secrets handling
----------------
Credentials are read lazily from Django settings on every operation (not
cached at import), so ``override_settings`` works in tests and a rotated
key takes effect without a restart. They are passed straight to
``cloudinary.config()`` and never logged. The test suite asserts no secret
value appears in log output on upload failure.
"""

import logging
import os

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.utils.deconstruct import deconstructible

logger = logging.getLogger(__name__)

#: Upload prefixes whose files are public images. Keep in sync with the
#: ``upload_to=`` values on the four public ImageFields
#: (Service.image, ServiceRequest.image, WorkerProfile.profile_image,
#: UserProfile.profile_image). ``worker_proofs/`` is deliberately absent:
#: identity documents must never leave local disk.
DEFAULT_PUBLIC_PREFIXES = ('services/', 'workers/', 'users/')


def _cloudinary_prefixes():
    """Return the configured public prefixes as a normalised tuple."""
    raw = getattr(settings, 'CLOUDINARY_PUBLIC_PREFIXES', None)
    if raw is None:
        prefixes = DEFAULT_PUBLIC_PREFIXES
    elif isinstance(raw, str):
        prefixes = tuple(raw.split())
    else:
        prefixes = tuple(raw)
    normalised = []
    for prefix in prefixes:
        prefix = str(prefix).strip().lstrip('/')
        if not prefix:
            continue
        if not prefix.endswith('/'):
            prefix += '/'
        normalised.append(prefix)
    return tuple(normalised)


def _cloudinary_credentials():
    """Return ``(cloud_name, api_key, api_secret)`` stripped of whitespace.

    Any blank part disables Cloudinary (returns Nones in those positions)
    so all storage stays local.
    """
    cloud_name = (getattr(settings, 'CLOUDINARY_CLOUD_NAME', '') or '').strip()
    api_key = (getattr(settings, 'CLOUDINARY_API_KEY', '') or '').strip()
    api_secret = (getattr(settings, 'CLOUDINARY_API_SECRET', '') or '').strip()
    if not (cloud_name and api_key and api_secret):
        return None, None, None
    return cloud_name, api_key, api_secret


def is_cloudinary_configured():
    """Return True only when all three Cloudinary credentials are set."""
    cloud_name, api_key, api_secret = _cloudinary_credentials()
    return bool(cloud_name and api_key and api_secret)


def _routes_to_cloudinary(name):
    """Return True when ``name`` is a public image AND Cloudinary is on."""
    if not name:
        return False
    normalised = str(name).replace('\\', '/').lstrip('/')
    if '/' not in normalised:
        return False
    first_prefix = normalised.split('/')[0] + '/'
    return is_cloudinary_configured() and first_prefix in _cloudinary_prefixes()


def _public_id_for(name):
    """Map a storage name to a Cloudinary public_id, preserving folders.

    ``services/plumbing.jpg`` -> ``services/plumbing``: the extension is
    stripped because Cloudinary re-appends the delivery format itself, and a
    stored ``.jpg`` suffix would otherwise produce ``.../plumbing.jpg.jpg``
    style URLs. Backslashes and a leading slash are normalised so Windows
    paths and absolute-looking names cannot escape the media namespace.
    """
    normalised = str(name).replace('\\', '/').lstrip('/')
    root, _ext = os.path.splitext(normalised)
    return root


def _configure_cloudinary():
    """Point the Cloudinary SDK at the configured credentials (lazy).

    Imported and configured inside the call - never at module import - so a
    checkout without the ``cloudinary`` package, or without credentials,
    still boots and serves everything locally.
    """
    import cloudinary

    cloud_name, api_key, api_secret = _cloudinary_credentials()
    cloudinary.config(
        cloud_name=cloud_name,
        api_key=api_key,
        api_secret=api_secret,
        secure=True,
    )
    return cloudinary


def _scrub_credentials(text):
    """Replace any configured credential value with ``[REDACTED]``.

    The Cloudinary SDK echoes request parameters in its exception messages,
    and ``logger.exception`` would additionally dump the raw traceback - both
    would land credentials in the log. Every failure path therefore logs a
    tagged, scrubbed one-liner with no traceback instead.
    """
    scrubbed = str(text)
    for value in _cloudinary_credentials():
        if value:
            scrubbed = scrubbed.replace(value, '[REDACTED]')
    return scrubbed


@deconstructible
class PrefixCloudinaryStorage(FileSystemStorage):
    """Default storage: public image prefixes -> Cloudinary, rest -> disk.

    ``@deconstructible`` with no custom ``__init__`` args keeps
    ``makemigrations --check`` clean: deconstruction is identical to plain
    ``FileSystemStorage``. Instances are still safe to construct with the
    usual ``location``/``base_url`` overrides (used by the test suite).
    """

    # -- writes ------------------------------------------------------
    def _save(self, name, content):
        if not _routes_to_cloudinary(name):
            return super()._save(name, content)
        cloudinary = _configure_cloudinary()
        public_id = _public_id_for(name)
        try:
            # ``unique_filename`` preserves Django's get_available_name
            # collision behaviour (never silently overwrite); ``overwrite``
            # stays False for the same reason. ``resource_type='auto'``
            # accepts the same jpg/png/webp uploads the forms validate.
            cloudinary.uploader.upload(
                content,
                public_id=public_id,
                unique_filename=True,
                overwrite=False,
                resource_type='auto',
            )
        except Exception as exc:
            # Never log the file bytes or credentials - only the tagged,
            # scrubbed failure (see _scrub_credentials).
            logger.error(
                'cloudinary_upload_failed name=%s error=%s',
                name, _scrub_credentials(str(exc)),
            )
            raise
        return name

    def get_available_name(self, name, max_length=None):
        if _routes_to_cloudinary(name):
            # Uniqueness is enforced server-side via ``unique_filename``;
            # returning the name keeps the stored DB value stable.
            return name
        return super().get_available_name(name, max_length=max_length)

    # -- reads -------------------------------------------------------
    def _open(self, name, mode='rb'):
        if not _routes_to_cloudinary(name):
            return super()._open(name, mode=mode)
        cloudinary = _configure_cloudinary()
        from django.core.files.base import ContentFile

        try:
            response = cloudinary.api.resource(
                _public_id_for(name), resource_type='image',
            )
            url = response.get('secure_url')
            if not url:
                raise IOError('Cloudinary resource has no secure_url')
            import urllib.request

            with urllib.request.urlopen(url, timeout=15) as remote:
                return ContentFile(remote.read(), name=name)
        except Exception as exc:
            logger.error(
                'cloudinary_download_failed name=%s error=%s',
                name, _scrub_credentials(str(exc)),
            )
            raise

    def url(self, name):
        if not _routes_to_cloudinary(name):
            return super().url(name)
        cloudinary = _configure_cloudinary()
        url, _options = cloudinary.utils.cloudinary_url(
            _public_id_for(name),
            resource_type='image',
            secure=True,
            force_version=False,
        )
        return url

    def exists(self, name):
        if not _routes_to_cloudinary(name):
            return super().exists(name)
        cloudinary = _configure_cloudinary()
        try:
            cloudinary.api.resource(
                _public_id_for(name), resource_type='image',
            )
            return True
        except Exception:
            return False

    def delete(self, name):
        if not _routes_to_cloudinary(name):
            return super().delete(name)
        cloudinary = _configure_cloudinary()
        try:
            cloudinary.uploader.destroy(
                _public_id_for(name),
                resource_type='image',
                invalidate=True,
            )
        except Exception as exc:
            logger.error(
                'cloudinary_delete_failed name=%s error=%s',
                name, _scrub_credentials(str(exc)),
            )
            raise

    def size(self, name):
        if not _routes_to_cloudinary(name):
            return super().size(name)
        cloudinary = _configure_cloudinary()
        try:
            response = cloudinary.api.resource(
                _public_id_for(name), resource_type='image',
            )
            return int(response.get('bytes', 0))
        except Exception as exc:
            logger.error(
                'cloudinary_size_failed name=%s error=%s',
                name, _scrub_credentials(str(exc)),
            )
            raise

    def path(self, name):
        if _routes_to_cloudinary(name):
            # No local path exists for a Cloudinary object; callers needing
            # bytes must use ``open()`` (which streams the remote file).
            # Raising - instead of returning a bogus MEDIA_ROOT path - keeps
            # proof_download honest: proofs never route to Cloudinary, so
            # they always get a real path below.
            raise NotImplementedError(
                'Cloudinary-backed files have no local path; use open().'
            )
        return super().path(name)
