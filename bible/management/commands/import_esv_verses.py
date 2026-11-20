import json
import logging
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from bible.models import Verse
from bible.utils.bible_books import get_dbt_book_id
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

    existing = {
        (v['book'], v['chapter'], v['verse'])
        for v in Verse.objects.values('book', 'chapter', 'verse')
    }
    to_create = []
    for book, book_data in data.items():
        dbt_book_id = get_dbt_book_id(book)
        if dbt_book_id is None:
            raise ValueError(
                f'No DBT book id for {book!r} in '
                'esv_bible_verses.json'
            )
        chapters = book_data['chapters']

        for chapter, chapter_data in chapters.items():
            verse_count = int(chapter_data['verse_count'])

            for verse in range(1, verse_count + 1):
                key = (book, int(chapter), verse)
                if key in existing:
                    continue
                to_create.append(Verse(
                    book=book,
                    chapter=int(chapter),
                    verse=verse,
                    dbt_book_id=dbt_book_id,
                ))
    with transaction.atomic():
        Verse.objects.bulk_create(to_create, ignore_conflicts=True)
