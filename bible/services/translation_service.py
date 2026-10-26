# bible/services/translation_service.py

import logging

from .fileset_groups import group_filesets
from .providers import (
    ApiBibleProvider,
    DbtProvider,
    ESVProvider,
    SwordProvider,
)

logger = logging.getLogger(__name__)

# Registry providers whose listings merge into the translations
# response alongside the live DBT ``/bibles`` list. Each answers
# its own entries through the facade's ``translation_listing``.
_REGISTRY_PROVIDERS = (
    SwordProvider,
    ESVProvider,
    ApiBibleProvider,
)


class TranslationService:
    """
    Service to fetch translation data directly from the DBT API.
    """

    @classmethod
    def get_live_translations(cls, language_iso=None):
        """
        Fetches translations and filters out video and audio stream content.

        Args:
            language_iso (str, optional): The language ISO code to filter by.
                Defaults to None.

        Returns:
            list: A list of translations without video and audio stream.
        """
        logger.info(
            "Fetching live translations for language_iso: %s",
            language_iso,
        )

        processed = []
        try:
            translations = DbtProvider().translation_listing(
                language_iso
            )
            logger.info(
                "Received %d translations from DBT API.",
                len(translations),
            )
            logger.debug("Raw translations from API: %s", translations)
            processed = cls._process_translations(translations)
        except Exception as e:
            logger.error(
                "DBT API Error fetching translations: %s", e, exc_info=True
            )

        registry_entries = []
        for provider_cls in _REGISTRY_PROVIDERS:
            registry_entries += provider_cls().translation_listing(
                language_iso
            )
        merged = processed + registry_entries
        # Attach normalized option groups so clients can offer
        # version-level text/audio choices without parsing raw
        # fileset ids. ``filesets`` stays untouched for backwards
        # compatibility.
        for entry in merged:
            options = group_filesets(entry)
            entry['text_options'] = options['text_options']
            entry['audio_options'] = options['audio_options']
        return merged

    @classmethod
    def _process_translations(cls, translations):
        """
        Removes video, audio_stream, and audio_drama_stream content
        and prepares fileset information.

        Args:
            translations (list): A list of translations to process.

        Returns:
            list: A list of processed translations with excluded
                content removed.
        """
        logger.info("Processing %d translations.", len(translations))
        processed = []

        # Define specific types to exclude
        excluded_types = {'video', 'audio_stream', 'audio_drama_stream'}

        for trans in translations:
            all_filesets = []
            for source, fileset_list in trans.get('filesets', {}).items():
                # Skip the video source entirely
                if source == 'dbp-vid':
                    continue

                filtered_filesets = []
                for fs in fileset_list:
                    fs_type = fs.get('type', '')

                    is_excluded = (
                        'video' in fs_type
                        or fs_type in excluded_types
                    )

                    if not is_excluded:
                        filtered_filesets.append(fs)

                all_filesets.extend(filtered_filesets)

            if not all_filesets:
                logger.warning(
                    "No valid filesets for translation %s. Skipping.",
                    trans.get('abbr'),
                )
                continue

            processed_filesets = []
            for fs in all_filesets:
                entry = {
                    'id': fs.get('id'),
                    'type': fs.get('type'),
                    'size': fs.get('size'),
                }
                # Pass codec/bitrate through when DBT provides
                # them; the ``-opus16`` id suffix remains the
                # authoritative codec marker for grouping.
                if fs.get('codec'):
                    entry['codec'] = fs['codec']
                if fs.get('bitrate'):
                    entry['bitrate'] = fs['bitrate']
                processed_filesets.append(entry)

            trans['filesets'] = processed_filesets
            processed.append(trans)

        logger.info(
            "Finished processing. Returning %d translations.", len(processed)
        )
        return processed
