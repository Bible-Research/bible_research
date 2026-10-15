from django.contrib.auth import get_user_model
from django.contrib.auth import login
from django.db import transaction
import re


class DeviceAndCountryMiddleware:
    """Middleware to detect device and country information from requests.
    This middleware extracts device information from the User-Agent header
    and country information from Cloudflare headers or IP address.
    """

    # Safe-method reads on these prefixes must stay truly anonymous:
    # the viewsets expose public content to unauthenticated callers,
    # so no device user is provisioned for them (issue #9).
    PUBLIC_READ_PREFIXES = (
        '/api/v1/bible',
        '/api/v1/notes',
        '/api/v1/tags',
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Process request before view is called
        self.process_device_info(request)
        self.primary_language(request)

        path = request.path
        is_public_read = (
            request.method in ('GET', 'HEAD', 'OPTIONS')
            and path.startswith(self.PUBLIC_READ_PREFIXES)
        )
        should_auto_authenticate = (
            not request.user.is_authenticated
            and not path.startswith('/admin')
            and not is_public_read
        )

        if should_auto_authenticate:
            self.auto_authenticate(request)

        print(f"User: {request.user}")

        response = self.get_response(request)
        return response

    def process_device_info(self, request):
        """Extract device information from the User-Agent."""
        user_agent = request.META.get('HTTP_USER_AGENT', 'Unknown Device')
        device = "Unknown Device"

        # Extract what's in the first set of parentheses
        # Example: Mozilla/5.0 (Android 12; Mobile; rv:131.0) AppleWebKit/537
        # Should extract: Android 12; Mobile
        parentheses_match = re.search(r'\(([^\(\)]+)\)', user_agent)
        if parentheses_match:
            content = parentheses_match.group(1).strip()
            parts = content.split(';')
            if len(parts) >= 2:
                device = f"{parts[0].strip()}; {parts[1].strip()}"
            elif len(parts) == 1:
                device = parts[0].strip()

        request.device_info = {
            'device': device,
            'user_agent': user_agent
        }

        print(f"Full user agent: {user_agent}")

    def primary_language(self, request):
        accept_language_header = request.META.get('HTTP_ACCEPT_LANGUAGE', '')

        languages = []
        if accept_language_header:
            # The header format is typically like: en-US,en;q=0.9,fr;q=0.8
            # Split by comma to get each language preference
            for lang_pref in accept_language_header.split(','):
                # Split by semicolon to separate the language
                # code from the quality value
                parts = lang_pref.strip().split(';')
                lang_code = parts[0].strip()

                # Get quality factor if present
                # (defaults to 1.0 if not specified)
                quality = 1.0
                if len(parts) > 1 and parts[1].startswith('q='):
                    try:
                        quality = float(parts[1][2:])
                    except ValueError:
                        pass

                languages.append({
                    'code': lang_code,
                    'quality': quality
                })

            # Sort languages by quality (highest first)
            languages.sort(key=lambda x: x['quality'], reverse=True)

        # Find the highest quality non-English language
        primary_language = None
        english_codes = ['en', 'en-US', 'en-GB']

        # First try to find a non-English language
        for lang in languages:
            if lang['code'] not in english_codes:
                primary_language = lang['code']
                break

        # If no non-English language found, use the highest quality language
        if not primary_language and languages:
            primary_language = languages[0]['code']

        print(f"Languages: {languages}")

        # Store language info in request for potential use in views
        request.language_info = {
            'languages': languages,
            'primary_language': primary_language
          }

    def auto_authenticate(self, request):
        device = request.device_info.get('device')
        primary_language = request.language_info.get('primary_language')

        User = get_user_model()

        try:
            username = f"{primary_language} {device}"

            if len(username) > 150:
                username = username[:150]

            try:
                user = User.objects.get(username=username)
            except User.DoesNotExist:
                with transaction.atomic():
                    user = User.objects.create_user(
                        # TODO: Generate random username, device name should
                        # be in the name and surname
                        username=username,
                        email=f"{username}@example.com",
                        password=None,
                    )
                    user.set_unusable_password()
                    user.save()
                    print(f"Created new user: {username}")

            request.user = user
            login(
                request,
                user,
                backend='django.contrib.auth.backends.ModelBackend'
            )
            print(f"Auto-authenticated as: {user.username}")
            return
        except Exception as e:
            print(f"Auto-authentication failed: {e}")
            username = "guest"
            try:
                user = User.objects.get(username=username)
            except User.DoesNotExist:
                with transaction.atomic():
                    user = User.objects.create_user(
                        username=username,
                        email=f"{username}@example.com",
                        password=None
                    )
                    user.set_unusable_password()
                    user.save()
                    print(f"Created new user: {username}")

            request.user = user
            login(
                request,
                user,
                backend='django.contrib.auth.backends.ModelBackend'
            )
            print(f"Auto-authenticated as: {user.username}")
