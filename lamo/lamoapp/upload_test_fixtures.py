"""Real (decodable) upload fixtures shared by the test-suite.

Upload validation now decodes images and structurally checks PDFs, so tests
that need a *valid* file must use real bytes, not a magic-number stub.
"""
import io

from PIL import Image

# A minimal but structurally valid one-page PDF (header, objects, xref, trailer, %%EOF).
VALID_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
    b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>\nendobj\n"
    b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
    b"0000000058 00000 n \n0000000115 00000 n \n"
    b"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n190\n%%EOF\n"
)


def _image_bytes(fmt, size=(16, 16)):
    buf = io.BytesIO()
    Image.new('RGB', size, (30, 140, 90)).save(buf, format=fmt)
    return buf.getvalue()


def make_jpeg():
    return _image_bytes('JPEG')


def make_png():
    return _image_bytes('PNG')


def make_webp():
    return _image_bytes('WEBP')
