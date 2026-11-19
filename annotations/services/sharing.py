"""Helpers for the note-sharing feature.

``PublishGateDenied`` is the publish-permission failure signal
shared by serializers and views: any write whose EFFECTIVE
result is ``published=True`` requires
``annotations.publish_note``. Viewsets map the exception to the
annotations 400 contract (``{'error': 'validation_error',
'message'}``) — never a 403, since a non-publisher should not
be able to submit the flag at all.
"""


class PublishGateDenied(Exception):
    """Write would set ``published=True`` without permission."""


def assert_can_publish(request, published):
    """Raise ``PublishGateDenied`` when *published* is truthy
    and the request's user lacks ``annotations.publish_note``.

    Anonymous users and the read-only ``guest`` account fail
    the check — ``has_perm`` is False for both unless the
    permission was explicitly granted in admin.
    """
    if not published:
        return
    user = getattr(request, 'user', None)
    allowed = bool(
        user is not None
        and user.is_authenticated
        and user.has_perm('annotations.publish_note')
    )
    if not allowed:
        raise PublishGateDenied(
            'Publishing requires the publish_note permission.'
        )
