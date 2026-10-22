"""
Custom authentication classes for the Bible Research project.
"""
from rest_framework.authentication import TokenAuthentication


class BearerTokenAuthentication(TokenAuthentication):
    """
    Simple token based authentication using the keyword 'Bearer'
    instead of 'Token'.

    Clients should authenticate by passing the token key in the
    "Authorization" HTTP header, prepended with "Bearer ". E.g.:

        Authorization: Bearer 9944b09199c62bcf9418ad846dd0e4bbdfc6ee4b
    """
    keyword = 'Bearer'
