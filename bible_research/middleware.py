"""Request middleware for the ``bible_research`` project package."""
import logging
import threading
from collections import OrderedDict

logger = logging.getLogger(__name__)

# DRF's ``TokenAuthentication`` accepts ``Token <key>``;
# ``BearerTokenAuthentication`` accepts ``Bearer <key>``.
_TOKEN_SCHEMES = frozenset({'token', 'bearer'})

# Private bounded LRU of token key -> user id, so the middleware
# does not double the per-request auth query on every API call.
# A miss is cached too (as ``0``): token keys are immutable, so
# an invalid key can never become valid. Deliberately NOT the
# shared Django cache — a spray of unique bad tokens would
# otherwise evict legitimate entries (throttle counters,
# provider caches).
_TOKEN_USER_CACHE_MAX = 200
_token_user_cache = OrderedDict()
_token_user_cache_lock = threading.Lock()


class SentryUserMiddleware:
    """Attach the authenticated user's pk to the Sentry scope.

    ``send_default_pii`` stays ``False`` — only the numeric pk is
    attached, never username/email/IP. Placed after
    ``AuthenticationMiddleware`` so session-authenticated users
    (the admin) are read straight off ``request.user``. DRF token
    auth resolves inside the view — after middleware runs — so
    ``Authorization`` headers are resolved here directly against
    the token table; without this, mid-view captures would carry
    no user.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user_id = _resolve_sentry_user_id(request)
        if user_id:
            try:
                import sentry_sdk
            except ImportError:
                pass
            else:
                sentry_sdk.set_user({'id': user_id})
        return self.get_response(request)


def _resolve_sentry_user_id(request):
    """The request's user pk, or ``None`` when unauthenticated."""
    user = getattr(request, 'user', None)
    if user is not None and getattr(
        user, 'is_authenticated', False
    ):
        return user.pk

    header = request.META.get('HTTP_AUTHORIZATION') or ''
    scheme, _, key = header.partition(' ')
    if scheme.lower() not in _TOKEN_SCHEMES or not key.strip():
        return None
    key = key.strip()

    with _token_user_cache_lock:
        cached = _token_user_cache.get(key)
        if cached is not None:
            _token_user_cache.move_to_end(key)
            return cached or None

    from rest_framework.authtoken.models import Token
    try:
        user_id = Token.objects.values_list(
            'user_id', flat=True,
        ).get(key=key)
    except Token.DoesNotExist:
        user_id = 0
    except Exception:
        # A DB blip must not break the request — monitoring
        # degrades to no user tag.
        logger.debug(
            'SentryUserMiddleware token lookup failed',
            exc_info=True,
        )
        return None
    with _token_user_cache_lock:
        _token_user_cache[key] = user_id
        while len(_token_user_cache) > _TOKEN_USER_CACHE_MAX:
            _token_user_cache.popitem(last=False)
    return user_id or None
