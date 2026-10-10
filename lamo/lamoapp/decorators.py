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
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect


def role_required(*allowed_roles, redirect_to='dashboard', error_message=None):
    """Allow the view only for the given ``CustomUser.role`` values."""

    def decorator(view_func):

        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            role = getattr(request.user, 'role', None)

            if role in allowed_roles:
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
