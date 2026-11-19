"""Shared, content-based validation for user uploads (profile photos and KYC proofs).

A filename or a client-declared MIME type is never trusted on its own. Every
validator here checks, in order:

1. a safe, directory-free filename with an allowed final extension and no
   executable "inner" extension (``payload.exe.png``);
2. the size against a configurable limit;
3. the client-declared content type (when present) against an allow-list;
4. the magic bytes at the start of the file;
5. that the bytes really decode - Pillow for images, a structural check for
   PDFs - so a corrupt file or an executable renamed to ``.png`` is refused.

Validators raise ``django.core.exceptions.ValidationError`` with a message that
is safe to show to the user. On success they return the file unchanged apart
from a random UUID name, so the client's filename never reaches storage.

Limits (bytes) come from settings, which read the environment:
``PROFILE_PHOTO_MAX_BYTES`` and ``PROOF_DOCUMENT_MAX_BYTES`` (default 5 MB).
"""
import os
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError

DEFAULT_MAX_BYTES = 5 * 1024 * 1024

# Inner filename parts that must never appear (checked in addition to the final
# extension), e.g. ``payload.exe.pdf``.
DANGEROUS_INNER_PARTS = frozenset((
    'exe', 'bat', 'cmd', 'com', 'scr', 'msi', 'jar', 'dll', 'apk',
    'js', 'jse', 'vbs', 'ps1', 'sh', 'php', 'html', 'htm', 'svg',
    'cgi', 'pl', 'py', 'rb',
))

IMAGE_FORMATS = {
    # Pillow format -> (extensions, mime types)
    'JPEG': (('.jpg', '.jpeg'), ('image/jpeg', 'image/jpg', 'image/pjpeg')),
    'PNG': (('.png',), ('image/png', 'image/x-png')),
    'WEBP': (('.webp',), ('image/webp',)),
}

PROFILE_PHOTO_FORMATS = ('JPEG', 'PNG', 'WEBP')
PROOF_IMAGE_FORMATS = ('JPEG', 'PNG')

# Hard ceiling on decoded pixels (decompression-bomb guard) - ~50 megapixels.
MAX_IMAGE_PIXELS = 50_000_000

PDF_MIME_TYPES = ('application/pdf', 'application/x-pdf')
# PDF actions that execute code or launch programs. KYC documents never need them.
PDF_FORBIDDEN_TOKENS = (b'/Launch', b'/JavaScript', b'/JS ', b'/JS(', b'/JS/')


def _max_bytes(setting_name):
    try:
        value = int(getattr(settings, setting_name, DEFAULT_MAX_BYTES))
    except (TypeError, ValueError):
        value = DEFAULT_MAX_BYTES
    return value if value > 0 else DEFAULT_MAX_BYTES


def _check_filename(name, allowed_extensions):
    """Return the lower-case extension, or raise for an unsafe name."""
    base = os.path.basename(name or '')
    if not base or base != name or '\x00' in base or '..' in base:
        raise ValidationError('Invalid file name.')
    root, ext = os.path.splitext(base)
    ext = ext.lower()
    if ext not in allowed_extensions:
        raise ValidationError(
            'Unsupported file type. Allowed: {}.'.format(
                ', '.join(e.lstrip('.').upper() for e in allowed_extensions)
            )
        )
    if any(part in DANGEROUS_INNER_PARTS for part in root.lower().split('.')):
        raise ValidationError('This file name is not allowed.')
    return ext


def _read_head(upload, n=16):
    upload.seek(0)
    head = upload.read(n)
    upload.seek(0)
    return head


def _sniff_image_format(head):
    if head.startswith(b'\xff\xd8\xff'):
        return 'JPEG'
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'PNG'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'WEBP'
    return None


def _decode_image(upload, expected_format):
    """Fully decode the image with Pillow; raise ValidationError if corrupt."""
    from PIL import Image, UnidentifiedImageError
    try:
        upload.seek(0)
        with Image.open(upload) as img:
            fmt = (img.format or '').upper()
            width, height = img.size
            if fmt != expected_format:
                raise ValidationError('File content does not match the declared file type.')
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise ValidationError('Image dimensions are not supported.')
            img.verify()
        # verify() does not decode pixel data; re-open and load to catch truncation.
        upload.seek(0)
        with Image.open(upload) as img:
            img.load()
    except ValidationError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError,
            Image.DecompressionBombError, MemoryError):
        raise ValidationError('The image is corrupted or cannot be read.')
    finally:
        upload.seek(0)


def _check_pdf(upload):
    """Structural PDF check: header, trailer, object structure, no active content."""
    upload.seek(0)
    data = upload.read()
    upload.seek(0)
    if not data.startswith(b'%PDF-'):
        raise ValidationError('File content does not match the declared file type.')
    if b'%%EOF' not in data[-2048:]:
        raise ValidationError('The PDF is incomplete or corrupted.')
    if b'endobj' not in data or b' obj' not in data:
        raise ValidationError('The PDF is corrupted or invalid.')
    if b'trailer' not in data and b'/Root' not in data:
        raise ValidationError('The PDF is corrupted or invalid.')
    for token in PDF_FORBIDDEN_TOKENS:
        if token in data:
            raise ValidationError('PDFs containing scripts or launch actions are not allowed.')


def validate_image_upload(upload, *, formats=PROFILE_PHOTO_FORMATS,
                          max_bytes_setting='PROFILE_PHOTO_MAX_BYTES',
                          label='Image'):
    """Validate an uploaded image; return it renamed to a random safe name."""
    extensions = tuple(e for f in formats for e in IMAGE_FORMATS[f][0])
    mimes = tuple(m for f in formats for m in IMAGE_FORMATS[f][1])
    ext = _check_filename(getattr(upload, 'name', ''), extensions)

    limit = _max_bytes(max_bytes_setting)
    if upload.size > limit:
        raise ValidationError('{} must be less than {:g} MB.'.format(label, limit / (1024 * 1024)))
    if upload.size <= 0:
        raise ValidationError('The uploaded file is empty.')

    declared = (getattr(upload, 'content_type', '') or '').lower()
    if declared and declared not in mimes:
        raise ValidationError('Unsupported file type. Allowed: {}.'.format(
            ', '.join(f for f in formats)))

    sniffed = _sniff_image_format(_read_head(upload))
    if sniffed is None or sniffed not in formats:
        raise ValidationError('File content does not match the declared file type.')
    if ext not in IMAGE_FORMATS[sniffed][0]:
        raise ValidationError('File extension does not match the file content.')

    _decode_image(upload, sniffed)
    upload.name = '{}{}'.format(uuid.uuid4().hex, ext)
    return upload


def validate_profile_photo(upload):
    return validate_image_upload(upload, label='Profile image')


def validate_proof_document(upload):
    """Validate a KYC proof (PDF, JPEG or PNG); return it with a random name."""
    ext = _check_filename(getattr(upload, 'name', ''), ('.pdf', '.jpg', '.jpeg', '.png'))
    limit = _max_bytes('PROOF_DOCUMENT_MAX_BYTES')
    if upload.size > limit:
        raise ValidationError('Proof document must be less than {:g} MB.'.format(limit / (1024 * 1024)))
    if upload.size <= 0:
        raise ValidationError('The uploaded file is empty.')

    declared = (getattr(upload, 'content_type', '') or '').lower()
    allowed_mimes = PDF_MIME_TYPES + IMAGE_FORMATS['JPEG'][1] + IMAGE_FORMATS['PNG'][1]
    if declared and declared not in allowed_mimes:
        raise ValidationError('Unsupported file type. Only PDF, JPG, JPEG or PNG allowed.')

    head = _read_head(upload)
    if head.startswith(b'%PDF-'):
        if ext != '.pdf':
            raise ValidationError('File extension does not match the file content.')
        _check_pdf(upload)
    else:
        sniffed = _sniff_image_format(head)
        if sniffed not in PROOF_IMAGE_FORMATS:
            raise ValidationError('File content does not match the declared file type.')
        if ext not in IMAGE_FORMATS[sniffed][0]:
            raise ValidationError('File extension does not match the file content.')
        _decode_image(upload, sniffed)

    upload.name = '{}{}'.format(uuid.uuid4().hex, ext)
    return upload
