import logging

import requests
from django.conf import settings
from rest_framework import serializers

from bible.services.dbt.client import get_default_dbt_client
from bible.services.esv.client import get_default_esv_client
from bible.services.esv.registry import is_esv_fileset
from bible.services.sword.client import get_default_sword_client
from bible.services.google_tts.registry import get_tts_config
from bible.services.sword.registry import (
    canonical_sword_fileset_id,
    is_sword_fileset,
)
from bible.services.storage import gcs


logger = logging.getLogger(__name__)


def _book_not_in_fileset_body(book_id, book_name, chapter,
                              fileset_id, error=None):
    """Error body for a passage the fileset does not cover.

    ``BiblePassageView`` turns any body carrying
    ``error_code == 'book_not_in_fileset'`` into HTTP 404 so
    clients can deterministically fall back to another fileset.
    Field names follow the ``error``/``error_code`` contract used
    for provider failures. ``error`` overrides the default
    message when the caller knows a more specific cause.
    """
    return {
        'book': book_id,
        'book_name': book_name,
        'chapter': chapter,
        'error': error or (
            f"Book {book_id} chapter {chapter} is not available "
            f"in fileset {fileset_id}"
        ),
        'error_code': 'book_not_in_fileset',
    }


def _is_http_404(exc):
    """True when ``exc`` carries an HTTP 404 response."""
    response = getattr(exc, 'response', None)
    return (
        response is not None
        and getattr(response, 'status_code', None) == 404
    )


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

    def to_representation(self, instance):
        book_id = instance.get('book')
        book_name = instance.get('book_name', '')
        chapter = int(instance.get('chapter'))
        fileset_id = instance.get('fileset_id')
        # The view passes this as ``response_format`` to avoid shadowing
        # DRF's own ``format`` kwarg on APIView. Fall back to the old
        # ``format`` key for backwards compatibility if a caller still
        # constructs the serializer instance directly.
        response_format = instance.get(
            'response_format', instance.get('format', 'text')
        )

        try:
            if is_esv_fileset(fileset_id):
                try:
                    if response_format == 'audio':
                        audio_url = (
                            get_default_esv_client()
                            .get_chapter_audio_url(book_id, chapter)
                        )
                        return {
                            'book': book_id,
                            'book_name': book_name,
                            'chapter': chapter,
                            'format': 'audio',
                            'audio_url': audio_url,
                        }
                    parsed = (
                        get_default_esv_client()
                        .get_chapter_with_headings(book_id, chapter)
                    )
                    return {
                        'book': book_id,
                        'book_name': book_name,
                        'chapter': chapter,
                        'format': 'text',
                        'verses': [
                            {
                                'verse': v['verse_start'],
                                'text': v['verse_text'],
                            }
                            for v in parsed['verses']
                        ],
                        'headings': parsed['headings'],
                    }
                except requests.HTTPError as e:
                    # A 404 from api.esv.org means the passage is
                    # not covered; other HTTP failures keep the
                    # legacy catch-all behaviour.
                    if _is_http_404(e):
                        return _book_not_in_fileset_body(
                            book_id, book_name, chapter, fileset_id
                        )
                    raise

            if is_sword_fileset(fileset_id) and response_format == 'audio':
                canon = canonical_sword_fileset_id(fileset_id)
                voice_name = get_tts_config(canon)["voice_name"]
                if not gcs.chapter_audio_exists(
                    canon, book_id, chapter, voice_name,
                ):
                    return _book_not_in_fileset_body(
                        book_id, book_name, chapter, fileset_id,
                        error=(
                            'Audio not yet generated for this '
                            'chapter'
                        ),
                    )
                timestamps = gcs.read_timestamps_json(
                    canon, book_id, chapter, voice_name,
                )
                # ``file_size_bytes`` is embedded by the generator
                # (see gcs.upload_chapter_artifacts) so the request
                # path needs no separate blob.metadata HEAD. Older
                # artifacts without that key fall back to a live
                # lookup for one release of backwards compatibility.
                file_size_bytes = timestamps.get('file_size_bytes')
                if file_size_bytes is None:
                    audio_path, _ = gcs.chapter_object_paths(
                        canon, book_id, chapter, voice_name,
                    )
                    blob = gcs.get_default_client().bucket(
                        settings.AUDIO_BUCKET_NAME
                    ).get_blob(audio_path)
                    file_size_bytes = blob.size if blob else None
                return {
                    'book': book_id,
                    'book_name': book_name,
                    'chapter': chapter,
                    'format': 'audio',
                    'audio_url': gcs.signed_audio_url(
                        canon,
                        book_id,
                        chapter,
                        voice_name,
                        settings.AUDIO_SIGNED_URL_TTL_SECONDS,
                    ),
                    'duration_seconds': timestamps.get('duration_seconds'),
                    'file_size_bytes': file_size_bytes,
                }

            if is_sword_fileset(fileset_id):
                try:
                    verses = (
                        get_default_sword_client()
                        .get_chapter_verses(
                            fileset_id, book_id, chapter
                        )
                    )
                except ValueError:
                    # ``is_sword_fileset`` guarantees the fileset
                    # is known, so a ValueError here means the
                    # book/chapter is outside the module's
                    # coverage. FileNotFoundError (a missing
                    # module zip) is an OSError and still falls
                    # through to the generic catch.
                    return _book_not_in_fileset_body(
                        book_id, book_name, chapter, fileset_id
                    )
                return {
                    'book': book_id,
                    'book_name': book_name,
                    'chapter': chapter,
                    'format': 'text',
                    'verses': [
                        {'verse': v['verse_start'], 'text': v['verse_text']}
                        for v in verses
                    ],
                }

            dbt_client = get_default_dbt_client()
            try:
                passage_data = dbt_client.get_verses(
                    book_id, str(chapter), bible_id=fileset_id
                )
            except Exception as e:
                # DBT answers 404 when the fileset does not carry
                # the requested book/chapter. Other failures keep
                # the previous behaviour (generic catch below).
                if getattr(e, 'status', None) == 404:
                    return _book_not_in_fileset_body(
                        book_id, book_name, chapter, fileset_id
                    )
                raise
            rows = passage_data.get('data') or []
            if not rows:
                return _book_not_in_fileset_body(
                    book_id, book_name, chapter, fileset_id
                )
            audio_format = 'path' in rows[0]
            if audio_format:
                audio_data = rows[0]
                if (
                    response_format == 'audio'
                    and not audio_data.get('path')
                ):
                    return _book_not_in_fileset_body(
                        book_id, book_name, chapter, fileset_id
                    )
                return {
                    'book': book_id,
                    'book_name': book_name,
                    'chapter': chapter,
                    'audio_url': audio_data.get('path'),
                    'duration_seconds': audio_data.get('duration'),
                    'file_size_bytes': audio_data.get('filesize_in_bytes'),
                    'format': 'audio',
                }
            if response_format == 'audio':
                # The fileset has no audio rows for this passage
                # (e.g. an audio request against a text fileset).
                return _book_not_in_fileset_body(
                    book_id, book_name, chapter, fileset_id
                )
            return {
                'book': book_id,
                'book_name': book_name,
                'chapter': chapter,
                'format': 'text',
                'verses': [
                    {
                        'verse': v['verse_start'],
                        'text': v.get('verse_text', ''),
                    }
                    for v in rows
                    if 'verse_text' in v
                ],
            }
        except Exception as e:
            logger.error(f"Error fetching Bible passage: {str(e)}")
            return {
                'book': book_id,
                'book_name': book_name,
                'chapter': chapter,
                'verses': [],
                'message': 'No verses found for the specified passage',
            }
