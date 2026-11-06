"""Reusable role based access control decorators.

The project historically repeated this pattern inside every view::

    if request.user.role != 'ADMIN':
        messages.error(request, 'You are not authorized ...')
        return redirect('dashboard')

These decorators wrap exactly that behaviour so new views can express the
same rule in one line. They are additive - existing inline checks keep
working unchanged.
"""

from functools import wraps

from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect


def role_required(*allowed_roles, redirect_to='dashboard', error_message=None):
    """Allow the view only for the given ``CustomUser.role`` values."""

    def decorator(view_func):

        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not getattr(request.user, 'is_active', True):
                logout(request)
                messages.error(request, 'Your account has been deactivated. Please contact support.')
                return redirect('login')

            role = getattr(request.user, 'role', None)

            # Special verification for workers: suspended worker accounts cannot access worker views
            if role == 'WORKER':
                try:
                    wp = getattr(request.user, 'worker_profile', None)
                    if wp and getattr(wp, 'work_status', '') == 'SUSPENDED':
                        logout(request)
                        messages.error(request, 'Your worker account has been suspended. Please contact support.')
                        return redirect('login')
                except Exception:
                    pass

            is_authorized = role in allowed_roles
            if 'ADMIN' in allowed_roles and (getattr(request.user, 'is_superuser', False) or getattr(request.user, 'is_staff', False)):
                is_authorized = True

            if is_authorized:
                return view_func(request, *args, **kwargs)

            messages.error(
                request,
                error_message or 'You are not authorized to perform this action.'
            )
            return redirect(redirect_to)

        return login_required(_wrapped)

    return decorator


admin_required = role_required('ADMIN')
worker_required = role_required('WORKER')
user_required = role_required('USER')
