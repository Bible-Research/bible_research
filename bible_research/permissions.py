"""
Project-wide DRF permission classes.
"""
from rest_framework.permissions import BasePermission, SAFE_METHODS


class GuestReadOnly(BasePermission):
    """
    The legacy 'guest' account may read but never write.

    'guest' is a legacy account: the device auto-provisioning
    middleware that used to fall back to it has been removed, so
    the only way to act as 'guest' now is a previously issued
    auth token. Because such tokens may still circulate, requests
    authenticated as 'guest' are limited to safe HTTP methods;
    writes are rejected with 403. All other authenticated users
    are unaffected.
    """

    message = 'The shared guest account is read-only.'

    def has_permission(self, request, view):
        """
        Allow safe methods for everyone; allow writes for any
        authenticated user except 'guest'. Unauthenticated
        requests pass through — IsAuthenticated denies them.
        """
        if request.method in SAFE_METHODS:
            return True
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            return True
        return user.username != 'guest'
