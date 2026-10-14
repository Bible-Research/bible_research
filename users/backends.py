"""
Custom authentication backend for case-insensitive username
authentication.
"""
from django.contrib.auth.models import User
from django.contrib.auth.backends import ModelBackend


class CaseInsensitiveAuthBackend(ModelBackend):
    """
    Authentication backend that allows users to log in with
    username in any case.

    For example, if a user registered as 'JohnDoe', they can
    log in with 'johndoe', 'JOHNDOE', 'JohnDoe', etc.
    """

    def authenticate(
        self, request, username=None, password=None, **kwargs
    ):
        """
        Authenticate user with case-insensitive username lookup.

        Args:
            request: The HTTP request object
            username: Username provided by user (any case)
            password: Password provided by user
            **kwargs: Additional keyword arguments

        Returns:
            User object if authentication succeeds, None otherwise
        """
        if username is None or password is None:
            return None

        try:
            # Case-insensitive username lookup
            user = User.objects.get(
                username__iexact=username
            )
        except User.DoesNotExist:
            # Run the default password hasher once to reduce
            # the timing difference between an existing and a
            # nonexistent user
            User().set_password(password)
            return None
        except User.MultipleObjectsReturned:
            # Usernames that differ only by case (e.g. 'Alice'
            # and 'alice') all match iexact. Prefer the
            # exact-case match; otherwise use the first one.
            matches = User.objects.filter(
                username__iexact=username
            )
            user = next(
                (u for u in matches if u.username == username),
                matches.first()
            )

        # Check password and reject users that may not log in
        # (e.g. is_active=False), matching ModelBackend.
        if (
            user.check_password(password)
            and self.user_can_authenticate(user)
        ):
            return user

        return None
