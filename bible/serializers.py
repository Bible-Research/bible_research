"""DRF serializers for the ``bible`` app.

``BiblePassageSerializer`` is the request-validation boundary for
``GET /api/v1/bible/`` only — provider dispatch, upstream fetch,
verse selection and error mapping all live in
``bible.services.providers.get_passage_body``, which the view
calls on ``serializer.validated_data``. ``to_representation``
delegates to the same function so direct callers (tests)
keep working through one code path.
"""
from rest_framework import serializers

from bible.services.providers import get_passage_body


class BiblePassageSerializer(serializers.Serializer):
    book = serializers.CharField(
      required=True,
      help_text="Standard book ID (e.g., '2CH')"
    )
    book_name = serializers.CharField(
      required=False,
      help_text="Full book name (e.g., '2 Chronicles)'"
    )
    chapter = serializers.IntegerField(
      required=True,
      min_value=1,
      help_text="Chapter number"
    )
    fileset_id = serializers.CharField(
      required=True,
      help_text="DBT fileset ID for the specific translation and format"
    )
    verses = serializers.ListField(
      child=serializers.IntegerField(min_value=1),
      required=False,
      help_text=(
        "Optional verse numbers to return (e.g. [16] or "
        "[16, 17, 18]); omit to return the whole chapter"
      )
    )
    # Named ``response_format`` (the query param) so it does not
    # shadow DRF's ``format`` kwarg; ``get_passage_body`` reads it
    # off ``validated_data`` and still accepts the legacy
    # ``format`` key from direct callers.
    response_format = serializers.ChoiceField(
      choices=['text', 'audio'],
      required=False,
      default='text',
      write_only=True,
      help_text="'text' or 'audio' (default: 'text')"
    )

    def to_representation(self, instance):
        return get_passage_body(instance)
