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
    """Expose the user's notifications to every template if authenticated."""
    if request.user.is_authenticated:
        from .models import Notification
        
        notifications = Notification.objects.filter(
            recipient=request.user
        ).select_related(
            'booking'
        ).order_by('-created_at')[:10]  # Get top 10 for dropdown
        
        unread_count = Notification.objects.filter(
            recipient=request.user, is_read=False
        ).count()
        
        return {
            'global_notifications': notifications,
            'global_unread_count': unread_count,
        }
    return {
        'global_notifications': [],
        'global_unread_count': 0,
    }
