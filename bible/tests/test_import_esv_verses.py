"""Tests for the ``import_esv_verses`` management command."""
import json

from django.test import TestCase

from bible.management.commands.import_esv_verses import (
    populate_verses,
)
from bible.models import Verse

_JSON_PATH = 'bible/esv_bible_verses.json'


def _expected_verse_count():
    with open(_JSON_PATH) as f:
        data = json.load(f)
    return sum(
        int(chapter['verse_count'])
        for book in data.values()
        for chapter in book['chapters'].values()
    )


class PopulateVersesTests(TestCase):
    """populate_verses must be idempotent and fill gaps."""

    def test_populates_full_table_from_empty(self):
        populate_verses()
        self.assertEqual(
            Verse.objects.count(), _expected_verse_count()
        )

    def test_backfills_missing_tail_without_duplicates(self):
        """Pre-existing rows are kept; only absent verses are
        created — the Genesis 1:26-31 gap case."""
        populate_verses()
        Verse.objects.filter(
            book='Genesis', chapter=1, verse__gt=25
        ).delete()
        populate_verses()
        self.assertEqual(
            Verse.objects.count(), _expected_verse_count()
        )
        self.assertEqual(
            Verse.objects.filter(
                book='Genesis', chapter=1
            ).count(),
            31,
        )
        self.assertEqual(
            Verse.objects.filter(
                book='Genesis', chapter=1, verse=26
            ).count(),
            1,
        )

    def test_dbt_book_id_populated(self):
        populate_verses()
        self.assertFalse(
            Verse.objects.filter(dbt_book_id='').exists()
        )
        self.assertEqual(
            Verse.objects.get(
                book='Genesis', chapter=1, verse=31
            ).dbt_book_id,
            'GEN',
        )
        self.assertEqual(
            Verse.objects.get(
                book='Song of Songs', chapter=1, verse=1
            ).dbt_book_id,
            'SNG',
        )
