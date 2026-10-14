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
    audio_mode_for_fileset,
    canonical_sword_fileset_id,
    is_sword_fileset,
)
from bible.services.storage import gcs
from bible.utils.provider_errors import (
    PassageNotFoundError,
    provider_error_fields,
)


logger = logging.getLogger(__name__)


def _select_verses(rows, verse_numbers):
    """Filter ``[{'verse': n, 'text': ...}]`` rows to the
    requested verse numbers (``None`` keeps every row)."""
    if not verse_numbers:
        return rows
    wanted = set(verse_numbers)
    return [v for v in rows if v['verse'] in wanted]


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
    verses = serializers.ListField(
      child=serializers.IntegerField(min_value=1),
      required=False,
      help_text=(
        "Optional verse numbers to return (e.g. [16] or "
        "[16, 17, 18]); omit to return the whole chapter"
      )
    )

    def to_representation(self, instance):
        book_id = instance.get('book')
        book_name = instance.get('book_name', '')
        chapter = int(instance.get('chapter'))
        fileset_id = instance.get('fileset_id')
        # Optional verse selection (e.g. 'John 3:16-18'). Only
        # verses in the selection are returned; providers that
        # support narrowing get min/max as a hint to limit how
        # much they fetch upstream.
        verse_numbers = instance.get('verses')
        verse_start = min(verse_numbers) if verse_numbers else None
        verse_end = max(verse_numbers) if verse_numbers else None
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
                    esv_client = get_default_esv_client()
                    if verse_numbers:
                        parsed = esv_client.get_chapter_with_headings(
                            book_id, chapter,
                            verse_start=verse_start,
                            verse_end=verse_end,
                        )
                    else:
                        parsed = esv_client.get_chapter_with_headings(
                            book_id, chapter
                        )
                    selected = _select_verses(
                        [
                            {
                                'verse': v['verse_start'],
                                'text': v['verse_text'],
                            }
                            for v in parsed['verses']
                        ],
                        verse_numbers,
                    )
                    # A successful-but-empty ESV body (or a verse
                    # selection outside the chapter) parses to no
                    # verses — missing content, not provider_error.
                    if not selected:
                        raise PassageNotFoundError(
                            f"No verses found for {book_id} "
                            f"{chapter} in fileset_id={fileset_id}"
                        )
                    headings = parsed['headings']
                    if verse_numbers:
                        wanted = set(verse_numbers)
                        headings = [
                            h for h in headings
                            if h.get('before_verse') in wanted
                        ]
                    return {
                        'book': book_id,
                        'book_name': book_name,
                        'chapter': chapter,
                        'format': 'text',
                        'verses': selected,
                        'headings': headings,
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
                # The audio fileset id selects the generation mode:
                # LVSGLU8C1DA -> verse-mode artifacts under audio/,
                # LVSGLU8C2DA -> chapter-mode under audio_chapter/.
                audio_mode = audio_mode_for_fileset(fileset_id)
                if not gcs.chapter_audio_exists(
                    canon, book_id, chapter, voice_name,
                    mode=audio_mode,
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
                    mode=audio_mode,
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
                        mode=audio_mode,
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
                        mode=audio_mode,
                    ),
                    'duration_seconds': timestamps.get('duration_seconds'),
                    'file_size_bytes': file_size_bytes,
                }

            if is_sword_fileset(fileset_id):
                sword_client = get_default_sword_client()
                try:
                    if verse_numbers:
                        verses = sword_client.get_chapter_verses(
                            fileset_id, book_id, chapter,
                            verse_start=verse_start,
                            verse_end=verse_end,
                        )
                    else:
                        verses = sword_client.get_chapter_verses(
                            fileset_id, book_id, chapter
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
                selected = _select_verses(
                    [
                        {
                            'verse': v['verse_start'],
                            'text': v['verse_text'],
                        }
                        for v in verses
                    ],
                    verse_numbers,
                )
                if not selected:
                    raise PassageNotFoundError(
                        f"No verses found for {book_id} "
                        f"{chapter} in fileset_id={fileset_id}"
                    )
                return {
                    'book': book_id,
                    'book_name': book_name,
                    'chapter': chapter,
                    'format': 'text',
                    'verses': selected,
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
            # DBT rows are post-filtered rather than narrowed
            # upstream via ``verse_start``/``verse_end`` query
            # params: for audio filesets (``*DA`` ids) verse
            # filters could exclude whole-chapter audio files and
            # turn a valid request into a spurious 404. The exact
            # selection is still enforced below for text.
            selected = _select_verses(
                [
                    {
                        'verse': v['verse_start'],
                        'text': v.get('verse_text', ''),
                    }
                    for v in rows
                    if 'verse_text' in v
                ],
                verse_numbers,
            )
            if not selected:
                raise PassageNotFoundError(
                    f"No verses found for {book_id} "
                    f"{chapter} in fileset_id={fileset_id}"
                )
            return {
                'book': book_id,
                'book_name': book_name,
                'chapter': chapter,
                'format': 'text',
                'verses': selected,
            }
        except Exception as e:
            logger.exception(f"Error fetching Bible passage: {e}")
            return {
                'book': book_id,
                'book_name': book_name,
                'chapter': chapter,
                **provider_error_fields(e),
            }
