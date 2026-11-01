"""Template context shared by the Leaflet maps.

The booking page and the worker-page booking modal both need a tile provider
that is allowed to serve application traffic. The values come from settings
(``MAP_TILE_*``), which read the environment, so the provider can be changed
per deployment without touching templates or JavaScript.
"""

from django.conf import settings


def map_config(request):
    """Expose the configured map tile settings to every template."""

    return {
        'map_tile_url': getattr(settings, 'MAP_TILE_URL', ''),
        'map_tile_attribution': getattr(settings, 'MAP_TILE_ATTRIBUTION', ''),
        'map_tile_subdomains': getattr(settings, 'MAP_TILE_SUBDOMAINS', ''),
        'map_tile_max_zoom': getattr(settings, 'MAP_TILE_MAX_ZOOM', 19),
    }


def global_notifications(request):
    """Expose the user's notifications and location context to every template."""
    from .models import WorkerProfile

    session = getattr(request, 'session', None)
    current_district = (
        (session.get('district') if session else '')
        or (session.get('lamo_selected_district') if session else '')
        or (
            getattr(request.user.user_profile, 'district', '')
            if request.user.is_authenticated and hasattr(request.user, 'user_profile')
            else ''
        )
        or ''
    )

    current_locality = (
        (session.get('locality') if session else '')
        or (session.get('lamo_selected_locality') if session else '')
        or (
            getattr(request.user.user_profile, 'locality', '')
            if request.user.is_authenticated and hasattr(request.user, 'user_profile')
            else ''
        )
        or ''
    )
    current_lat = (session.get('lat') if session else '') or (session.get('lamo_selected_lat') if session else '') or ''
    current_lng = (session.get('lng') if session else '') or (session.get('lamo_selected_lng') if session else '') or ''

    import json
    from .location_data import get_kerala_location_tree
    kerala_location_tree = get_kerala_location_tree()
    kerala_location_tree_json = json.dumps(kerala_location_tree)
    kerala_districts = WorkerProfile.DISTRICT_CHOICES

    current_language = (
        (session.get('lamo_lang') if session else None)
        or request.COOKIES.get('lamo_lang', 'en')
    )
    if current_language not in ('en', 'ml'):
        current_language = 'en'

    vapid_public_key = getattr(settings, 'VAPID_PUBLIC_KEY', '')

    base_context = {
        'kerala_districts': kerala_districts,
        'current_district': current_district,
        'current_locality': current_locality,
        'current_lat': current_lat,
        'current_lng': current_lng,
        'kerala_location_tree': kerala_location_tree,
        'kerala_location_tree_json': kerala_location_tree_json,
        'current_language': current_language,
        'vapid_public_key': vapid_public_key,
    }

    if request.user.is_authenticated:
        from .models import Notification, PushSubscription

        notifications = Notification.objects.filter(
            recipient=request.user
        ).select_related(
            'booking'
        ).order_by('-created_at')[:10]  # Get top 10 for dropdown

        unread_count = Notification.objects.filter(
            recipient=request.user, is_read=False
        ).count()

        has_push_sub = PushSubscription.objects.filter(user=request.user).exists()

        return {
            **base_context,
            'global_notifications': notifications,
            'global_unread_count': unread_count,
            'has_push_subscription': has_push_sub,
        }
    return {
        **base_context,
        'global_notifications': [],
        'global_unread_count': 0,
    }
