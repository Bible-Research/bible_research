import re

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import (
    validate_password,
)
from django.core.exceptions import (
    ValidationError as DjangoValidationError,
)
from django.db import IntegrityError
from rest_framework import serializers
from rest_framework.validators import UniqueValidator

User = get_user_model()

# Usernames may not contain whitespace — the case-insensitive
# login backend already treats the username namespace loosely,
# and spaced names serve no legitimate purpose (they were the
# vehicle for the removed device-middleware impersonation).
_NO_WHITESPACE_RE = re.compile(r"^\S+$")


def _no_whitespace_username(value):
    """Reject usernames containing any whitespace character."""
    if not _NO_WHITESPACE_RE.match(value):
        raise serializers.ValidationError(
            "Username may not contain whitespace."
        )


class UserRegistrationSerializer(serializers.ModelSerializer):
    """
    Serializer for user registration.
    Handles user creation with username, email, and password.
    """
    email = serializers.EmailField(
        required=False,
        allow_blank=True,
        validators=[
            UniqueValidator(
                queryset=User.objects.all(),
                message="A user with this email already exists."
            )
        ]
    )
    username = serializers.CharField(
        required=True,
        validators=[
            _no_whitespace_username,
            # Login looks usernames up with ``iexact`` — the
            # uniqueness check must be case-insensitive too, or
            # 'Alice'/'alice' coexist and auth resolves
            # nondeterministically.
            UniqueValidator(
                queryset=User.objects.all(),
                lookup='iexact',
                message="A user with this username already exists."
            )
        ]
    )
    password = serializers.CharField(
        write_only=True,
        required=True,
        style={'input_type': 'password'},
        min_length=8,
        help_text=(
            "Password must be at least 8 characters long."
        )
    )
    password_confirm = serializers.CharField(
        write_only=True,
        required=True,
        style={'input_type': 'password'},
        help_text="Enter the same password for confirmation."
    )

    class Meta:
        model = User
        fields = [
            'id',
            'username',
            'email',
            'password',
            'password_confirm'
        ]
        read_only_fields = ['id']

    def validate_password(self, value):
        """Run the field through AUTH_PASSWORD_VALIDATORS.

        Settings configure all four standard validators
        (similarity, minimum length, common-password and
        numeric-only) — without this hook only the ``min_length``
        field kwarg ran, so ``12345678``/``password1`` were
        accepted. An unsaved ``User`` instance lets the
        similarity validator compare against username/email.
        """
        user = User(
            username=self.initial_data.get('username', ''),
            email=self.initial_data.get('email', ''),
        )
        try:
            validate_password(value, user=user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages)
        return value

    def validate(self, attrs):
        """
        Validate that password and password_confirm match.
        """
        if attrs['password'] != attrs['password_confirm']:
            raise serializers.ValidationError({
                'password_confirm': (
                    "Password fields didn't match."
                )
            })
        return attrs

    def create(self, validated_data):
        """
        Create a new user with encrypted password.
        """
        # Remove password_confirm as it's not needed
        # for user creation
        validated_data.pop('password_confirm')

        # Create user with create_user method to ensure
        # password is hashed. A UniqueValidator→insert race can
        # still hit the DB unique constraint — map the
        # IntegrityError to a 400 instead of a 500.
        try:
            user = User.objects.create_user(
                username=validated_data['username'],
                email=validated_data.get('email', ''),
                password=validated_data['password']
            )
        except IntegrityError:
            raise serializers.ValidationError({
                'username': (
                    "A user with this username already exists."
                )
            })
        return user


class UserSerializer(serializers.ModelSerializer):
    """
    Serializer for user data display.
    Used for retrieving user information.
    """
    account_kind = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id',
            'username',
            'email',
            'date_joined',
            'account_kind',
        ]
        read_only_fields = ['id', 'date_joined', 'account_kind']

    def get_account_kind(self, obj):
        """``guest`` for the legacy read-only account,
        ``registered`` for everyone else — lets clients stop
        string-matching ``username == 'guest'``."""
        if obj.username == 'guest':
            return 'guest'
        return 'registered'
