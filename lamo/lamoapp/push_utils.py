import json
import logging
import threading
from django.conf import settings
from .models import PushSubscription

logger = logging.getLogger(__name__)

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

    for sub in subscriptions:
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
        except WebPushException as ex:
            if ex.response is not None and ex.response.status_code in (404, 410):
                # Worker unsubscribed or cleared browser data
                logger.info("Pruning expired push subscription %s for user %s", sub.id, user.username)
                sub.delete()
            else:
                logger.warning("WebPush failed for user %s: %s", user.username, str(ex))
        except Exception as ex:
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
