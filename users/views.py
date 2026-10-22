import logging

from rest_framework import status
from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.decorators import (
    api_view,
    permission_classes,
    throttle_classes,
)
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.authtoken.models import Token
from rest_framework.throttling import SimpleRateThrottle
from django.contrib.auth import get_user_model
from drf_spectacular.utils import (
    extend_schema,
    OpenApiResponse,
    OpenApiExample
)

from .serializers import (
    UserRegistrationSerializer,
    UserSerializer
)

User = get_user_model()
logger = logging.getLogger(__name__)


class TokenRateThrottle(SimpleRateThrottle):
    """Per-username throttle for ``POST /api/token/``.

    Keying on the supplied username (rather than the source IP)
    bounds online credential brute force against a single account
    regardless of how the requests are distributed. Requests
    without a ``username`` fall back to per-IP throttling so
    malformed enumeration probes are still bounded.
    """
    scope = 'token'

    def get_cache_key(self, request, view):
        # A non-dict body (e.g. a top-level JSON array) parses
        # fine but has no .get — guard so the serializer can
        # return its 400 instead of an AttributeError 500.
        data_get = getattr(request.data, 'get', None)
        username = data_get('username') if data_get else None
        if username:
            ident = str(username).lower()
        else:
            ident = self.get_ident(request)
        return self.cache_format % {
            'scope': self.scope,
            'ident': ident,
        }


class RegisterRateThrottle(SimpleRateThrottle):
    """Per-client throttle on ``POST /api/v1/users/register/``
    — bounds bulk account creation and username/email
    enumeration."""
    scope = 'register'

    def get_cache_key(self, request, view):
        if request.user and request.user.is_authenticated:
            ident = request.user.pk
        else:
            ident = self.get_ident(request)
        return self.cache_format % {
            'scope': self.scope,
            'ident': ident,
        }


class ThrottledObtainAuthToken(ObtainAuthToken):
    """``obtain_auth_token`` with a credential-guess throttle."""
    throttle_classes = [TokenRateThrottle]


@extend_schema(
    request=UserRegistrationSerializer,
    responses={
        201: OpenApiResponse(
            description="User registered successfully",
            examples=[
                OpenApiExample(
                    'Success Response',
                    value={
                        'user': {
                            'id': 3,
                            'username': 'newuser',
                            'email': 'user@example.com',
                            'date_joined': '2026-03-01T07:11:10Z'
                        },
                        'token': (
                            'a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6'
                        ),
                        'message': 'User registered successfully'
                    }
                )
            ]
        ),
        400: OpenApiResponse(
            description="Validation error"
        )
    },
    description=(
        "Register a new user account. "
        "Email is optional. Password must pass Django's "
        "AUTH_PASSWORD_VALIDATORS: at least 8 characters, "
        "not entirely numeric, not a common password, and "
        "not too similar to the username/email. Returns "
        "user data and authentication token."
    ),
    tags=['Users']
)
@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([RegisterRateThrottle])
def register_user(request):
    """
    Register a new user account.

    Accepts username, email, password, and password_confirm.
    Returns the created user data and authentication token.
    """
    # request.data may be a non-dict body (e.g. a JSON
    # array) — .get would AttributeError into a 500 before
    # the serializer can return its 400.
    data_get = getattr(request.data, 'get', None)
    username = data_get('username', 'N/A') if data_get else 'N/A'
    logger.info(
        f"Registration attempt for username: {username}"
    )

    serializer = UserRegistrationSerializer(data=request.data)

    if serializer.is_valid():
        user = serializer.save()
        # Create authentication token for the new user
        token, created = Token.objects.get_or_create(user=user)

        logger.info(
            f"User '{user.username}' registered successfully"
        )

        return Response(
            {
                'user': UserSerializer(user).data,
                'token': token.key,
                'message': (
                    'User registered successfully'
                )
            },
            status=status.HTTP_201_CREATED
        )

    logger.warning(
        f"Registration failed: {serializer.errors}"
    )
    return Response(
        serializer.errors,
        status=status.HTTP_400_BAD_REQUEST
    )


@extend_schema(
    responses={
        200: UserSerializer,
    },
    description=(
        "Retrieve the current authenticated user's "
        "information including username, email, and "
        "date joined."
    ),
    tags=['Users']
)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def current_user(request):
    """
    Get the current authenticated user's information.
    """
    logger.info(
        f"Current user request for: {request.user.username}"
    )
    serializer = UserSerializer(request.user)
    return Response(serializer.data)
