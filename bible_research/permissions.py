"""
Project-wide DRF permission classes.
"""
from rest_framework.permissions import BasePermission, SAFE_METHODS


class GuestReadOnly(BasePermission):
    """
    The shared 'guest' account may read but never write.

    DeviceAndCountryMiddleware auto-provisions per-device users and
    falls back to authenticating requests as the single shared
    'guest' user. Because anyone can end up acting as that account,
    requests authenticated as 'guest' are limited to safe HTTP
    methods; writes are rejected with 403. Registered users and
    auto-provisioned per-device users are unaffected.
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
