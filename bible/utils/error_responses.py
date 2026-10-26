"""Shared ``{"error", "error_code"}`` responses for client errors.

Every hand-rolled 400/404 body in ``bible/views.py`` emits this
shape so the React app can always branch on ``error_code`` — see
the error contract in AGENTS.md. ``error`` carries a fixed,
caller-authored message only; exception text is never embedded
(it can contain request URLs with credentials).
"""
from rest_framework import status
from rest_framework.response import Response

# error_code values for client-side input failures. These are
# new additive codes — no existing frontend branch keys on them,
# they exist so every error body carries the contract fields.
MISSING_PARAMETER = 'missing_parameter'
INVALID_PARAMETER = 'invalid_parameter'


def bad_request(message, code=INVALID_PARAMETER):
    """Return an HTTP 400 ``{"error", "error_code"}`` response."""
    return Response(
        {'error': message, 'error_code': code},
        status=status.HTTP_400_BAD_REQUEST,
    )


def not_found(message, code='not_found'):
    """Return an HTTP 404 ``{"error", "error_code"}`` response."""
    return Response(
        {'error': message, 'error_code': code},
        status=status.HTTP_404_NOT_FOUND,
    )
