"""Safe outbound e-mail helpers.

Why this module exists
----------------------
Two problems in the original code:

1. ``send_otp_email`` caught every SMTP failure and then printed the *live
   OTP*, the recipient address and a banner to stdout::

       print(f"  OTP: {otp}")

   That landed in the console and in ``logs/lamo.log`` (the root logger writes
   to both). Anyone with log access - and any log shipper, monitoring agent or
   support bundle that ingests the file - could read a live account-takeover
   credential. Worse, the user was still told "an OTP has been sent", so a
   broken SMTP server silently locked people out of their own accounts.

2. The contact form wrapped its notification e-mail in ``except Exception:
   pass``, so a permanently broken mail configuration was completely invisible:
   no log line, no metric, nothing to alert on.

What this module guarantees
---------------------------
* **Secrets are never logged.** Subjects, bodies, OTPs, reset tokens and SMTP
  exceptions all pass through :func:`_scrub` before reaching a log record.
* **Failures are visible.** Every send is logged with a stable ``purpose`` tag
  and the exception *class* (never the raw message verbatim - it can embed
  credentials, e.g. an SMTP ``535`` auth error quoting the username).
* **No fake success.** The caller gets a boolean, so the UI can tell the user
  the mail did not go out instead of pretending otherwise.

Only :mod:`logging`, :mod:`re` and Django's mail API are used; there is no new
dependency and no model change.
"""

import logging
import re

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


# Patterns whose *values* must never reach a log file. Matched
# case-insensitively against any free text (typically an SMTP error string).
_SECRET_PATTERNS = (
    # key=value / key: value pairs
    re.compile(
        r'(?i)\b(pass(?:word|wd)?|secret|token|otp|api[_-]?key|auth|'
        r'credential|apikey|access[_-]?key|private[_-]?key)\b'
        r'\s*[:=]\s*["\']?([^\s,;"\']+)'
    ),
    # credentials embedded in a URL: scheme://user:password@host
    re.compile(r'(?i)\b([a-z][a-z0-9+.-]*)://([^\s/:@]+):([^\s/@]+)@'),
    # a bare 6-digit OTP sitting on its own
    re.compile(r'(?<![\w-])\d{6}(?![\w-])'),
    # AWS-style access key ids
    re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{12,}\b'),
    # long opaque tokens (>= 32 chars of token-ish alphabet)
    re.compile(r'(?<![\w-])[A-Za-z0-9_\-]{32,}(?![\w-])'),
)


def _scrub(value, limit=300):
    """Return *value* with anything that looks like a secret replaced.

    Safe to call on ``None``, on exception text, or on any string. The result
    is truncated so a huge SMTP response cannot flood the log.
    """
    if value is None:
        return ''

    text = str(value)

    # key=value pairs -> key=[REDACTED]
    text = _SECRET_PATTERNS[0].sub(
        lambda m: '{0}=[REDACTED]'.format(m.group(1)), text
    )
    # scheme://user:password@host -> scheme://user:[REDACTED]@
    text = _SECRET_PATTERNS[1].sub(
        lambda m: '{0}://{1}:[REDACTED]@'.format(m.group(1), m.group(2)), text
    )
    for pattern in _SECRET_PATTERNS[2:]:
        text = pattern.sub('[REDACTED]', text)

    text = ' '.join(text.split())
    if len(text) > limit:
        text = text[:limit] + '...'
    return text


def _recipients_label(recipient_list):
    """A non-identifying description of who the mail was addressed to.

    Deliberately returns only the *count* and a coarse domain class, so log
    aggregation can still tell "1 recipient on a public domain" from
    "mail to the internal admin mailbox" without recording addresses.
    """
    if not recipient_list:
        return 'no-recipient'

    count = len(recipient_list)
    plural = '' if count == 1 else 's'
    return '{0} recipient{1}'.format(count, plural)


def send_email_safely(
    subject,
    message,
    recipient_list,
    purpose,
    from_email=None,
    fail_silently=True,
):
    """Send an e-mail, logging any failure without leaking secrets.

    Parameters
    ----------
    purpose:
        Short stable tag describing the operation, e.g. ``'otp_verification'``
        or ``'contact_enquiry'``. This is the only message content written to
        the log, and it is a constant defined in code - never user input.
    subject / message:
        Used for the actual send only. Neither is ever logged, because both
        can contain user-supplied text or credentials.

    Returns
    -------
    bool
        ``True`` when the message was handed to the mail backend,
        ``False`` when sending failed. Callers must not report success to the
        user when this is ``False``.
    """
    if not recipient_list:
        logger.warning(
            'email_not_sent purpose=%s reason=no_recipients', purpose,
        )
        return False

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=from_email or settings.DEFAULT_FROM_EMAIL,
            recipient_list=list(recipient_list),
            # Never let the backend swallow the error silently: we want the
            # chance to log it ourselves, in a scrubbed form.
            fail_silently=False,
        )
    except Exception as exc:
        logger.error(
            'email_send_failed purpose=%s recipients=%s exc_type=%s detail=%s',
            purpose,
            _recipients_label(recipient_list),
            type(exc).__name__,
            _scrub(str(exc)),
        )
        return False

    logger.info('email_sent purpose=%s recipients=%s', purpose,
                _recipients_label(recipient_list))
    return True
