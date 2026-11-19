import json
import logging
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from bible.models import Verse
from bible.utils.provider_errors import (
    capture_internal_exception,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Populate the Verse model from the ESV Bible verses JSON file'

    def handle(self, *args, **kwargs):
        start_time = datetime.now()
        print(f'Starting ESV Bible verses import at {start_time}')

        try:
            populate_verses()
        except Exception as e:
            logger.exception('Error importing ESV Bible verses')
            capture_internal_exception(
                e, tags={'job': 'import_esv_verses'},
            )
            raise CommandError(
                f'ESV Bible verses import failed: {e}'
            )
        end_time = datetime.now()
        print(
            'ESV Bible verses import completed successfully '
            f'at {end_time}'
        )
        print(f'Total time taken: {end_time - start_time}')


def populate_verses():
    with open('bible/esv_bible_verses.json') as f:
        data = json.load(f)

    for book, book_data in data.items():
        chapters = book_data['chapters']

        for chapter, chapter_data in chapters.items():
            verse_count = chapter_data['verse_count']

            for verse in range(1, int(verse_count) + 1):
                # Perform commit per verse to check db performance (for fun)
                verse_obj = Verse(
                    book=book,
                    chapter=int(chapter),
                    verse=verse
                )
                verse_obj.save()
