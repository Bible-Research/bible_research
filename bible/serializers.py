import logging
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
from bible.utils.provider_errors import provider_error_fields


logger = logging.getLogger(__name__)


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

            if is_sword_fileset(fileset_id) and response_format == 'audio':
                canon = canonical_sword_fileset_id(fileset_id)
                voice_name = get_tts_config(canon)["voice_name"]
                if not gcs.chapter_audio_exists(
                    canon, book_id, chapter, voice_name,
                ):
                    return {
                        'book': book_id,
                        'book_name': book_name,
                        'chapter': chapter,
                        'format': 'audio',
                        'audio_url': None,
                        'message': (
                            'Audio not yet generated for this chapter'
                        ),
                    }
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
                verses = get_default_sword_client().get_chapter_verses(
                    fileset_id, book_id, chapter
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
            passage_data = dbt_client.get_verses(
                book_id, str(chapter), bible_id=fileset_id
            )
            audio_format = 'path' in passage_data['data'][0]
            if audio_format:
                audio_data = passage_data['data'][0]
                return {
                    'book': book_id,
                    'book_name': book_name,
                    'chapter': chapter,
                    'audio_url': audio_data.get('path'),
                    'duration_seconds': audio_data.get('duration'),
                    'file_size_bytes': audio_data.get('filesize_in_bytes'),
                    'format': 'audio',
                }
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
                    for v in passage_data['data']
                    if 'verse_text' in v
                ],
            }
        except Exception as e:
            logger.error(f"Error fetching Bible passage: {str(e)}")
            return {
                'book': book_id,
                'book_name': book_name,
                'chapter': chapter,
                **provider_error_fields(e),
            }
