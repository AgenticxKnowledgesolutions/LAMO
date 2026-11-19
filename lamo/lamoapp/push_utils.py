import json
import logging
import threading
from django.conf import settings
from .models import PushSubscription

logger = logging.getLogger(__name__)

# Delivery attempts per subscription for transient push-service failures.
PUSH_MAX_ATTEMPTS = 3
PUSH_RETRY_BACKOFF_SECONDS = 2

try:
    from pywebpush import webpush, WebPushException
    PYWEBPUSH_AVAILABLE = True
except ImportError:
    PYWEBPUSH_AVAILABLE = False
    logger.warning("pywebpush package not installed; Web Push notifications disabled.")


def send_web_push(user, title, body, url="/worker-dashboard/"):
    """
    Sends a Web Push notification to all active browser subscriptions for a user.
    Automatically prunes expired/unregistered endpoints (HTTP 404 / 410).
    """
    if not PYWEBPUSH_AVAILABLE:
        return

    vapid_private_key = getattr(settings, 'VAPID_PRIVATE_KEY', None)
    vapid_email = getattr(settings, 'VAPID_ADMIN_EMAIL', 'admin@lamo.in')

    if not vapid_private_key:
        logger.debug("VAPID_PRIVATE_KEY is not configured; skipping web push.")
        return

    subscriptions = PushSubscription.objects.filter(user=user)
    if not subscriptions.exists():
        return

    payload = json.dumps({
        "title": title,
        "body": body,
        "url": url or "/worker-dashboard/",
        "icon": "/static/images/logo.png",
        "badge": "/static/images/logo.png",
        "tag": f"lamo-job-{user.id}",
    })

    import time as _time

    for sub in subscriptions:
        # Transient failures (network errors, 429, 5xx) are retried a couple
        # of times with a short backoff; expired subscriptions are pruned.
        for attempt in range(1, PUSH_MAX_ATTEMPTS + 1):
            try:
                webpush(
                    subscription_info={
                        "endpoint": sub.endpoint,
                        "keys": {
                            "p256dh": sub.p256dh,
                            "auth": sub.auth
                        }
                    },
                    data=payload,
                    vapid_private_key=vapid_private_key,
                    vapid_claims={
                        "sub": f"mailto:{vapid_email}"
                    },
                    ttl=3600  # 1 hour queue on browser push server
                )
                break
            except WebPushException as ex:
                status = ex.response.status_code if ex.response is not None else None
                if status in (404, 410):
                    # Worker unsubscribed or cleared browser data
                    logger.info("Pruning expired push subscription %s for user %s", sub.id, user.username)
                    sub.delete()
                    break
                transient = status is None or status == 429 or status >= 500
                if transient and attempt < PUSH_MAX_ATTEMPTS:
                    _time.sleep(PUSH_RETRY_BACKOFF_SECONDS * attempt)
                    continue
                logger.warning("WebPush failed for user %s: %s", user.username, str(ex))
                break
            except Exception as ex:
                if attempt < PUSH_MAX_ATTEMPTS:
                    _time.sleep(PUSH_RETRY_BACKOFF_SECONDS * attempt)
                    continue
                logger.error("Unexpected error sending WebPush to user %s: %s", user.username, str(ex))


def send_web_push_async(user, title, body, url="/worker-dashboard/"):
    """Dispatches send_web_push on a daemon thread so it never blocks the request."""
    if not user:
        return
    t = threading.Thread(
        target=send_web_push,
        args=(user, title, body, url),
        daemon=True
    )
    t.start()


def broadcast_web_push_async(users, title, body, url="/worker-dashboard/"):
    """Dispatches web push to multiple users concurrently in a background thread."""
    def _worker():
        for u in users:
            send_web_push(u, title, body, url)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
