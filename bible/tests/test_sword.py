from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from bible.services.sword.client import get_default_sword_client

User = get_user_model()


class SwordClientTests(TestCase):
    def test_sword_client_loads_glucks(self):
        verses = get_default_sword_client().get_chapter_verses(
            'LVSGLU8', 'JHN', 3
        )
        self.assertGreaterEqual(len(verses), 30)
        self.assertEqual(verses[0]['verse_start'], 1)
        self.assertTrue(all(v['verse_text'] for v in verses))

    def test_sword_client_resolves_roman_numeral_books(self):
        """Regression: pysword's ``bible.get(books=[...])`` only matches
        the module's canonical book name (``"I Kings"``), its OSIS name
        (``"1Kgs"``) or its preferred abbreviation. Passing the
        lowercase English form ``"1 kings"`` raises ValueError and
        breaks every Roman-numeral-prefixed book (1/2 Samuel, 1/2
        Kings, 1/2 Chronicles, 1/2 Corinthians, 1/2 Thessalonians,
        1/2 Timothy, 1/2 Peter, 1/2/3 John)."""
        client = get_default_sword_client()
        for book_id in ('1KI', '2KI', '1SA', '2CH', '1JN'):
            verses = client.get_chapter_verses('LVSGLU8', book_id, 1)
            self.assertGreater(
                len(verses), 0,
                f"no verses returned for {book_id} 1",
            )
            self.assertEqual(verses[0]['verse_start'], 1)

    def test_sword_client_accepts_abbr_gl8(self):
        by_id = get_default_sword_client().get_chapter_verses(
            'LVSGLU8', 'LUK', 20
        )
        by_abbr = get_default_sword_client().get_chapter_verses(
            'GLU8', 'LUK', 20
        )
        self.assertEqual(len(by_abbr), len(by_id))
        self.assertEqual(by_abbr[0]['verse_text'], by_id[0]['verse_text'])

    def test_list_chapters_comes_from_sword_structure(self):
        """Regression: audio worklist must be sourced from the SWORD
        module itself, never from ESV/KJV-derived JSON, so that the
        audio generator respects this translation's own versification."""
        client = get_default_sword_client()
        worklist = client.list_chapters('LVSGLU8')

        self.assertGreater(len(worklist), 0)

        # Every entry is (book_id, chapter) with chapter starting at 1.
        by_book: dict = {}
        for book_id, chap in worklist:
            self.assertIsInstance(book_id, str)
            self.assertIsInstance(chap, int)
            self.assertGreaterEqual(chap, 1)
            by_book.setdefault(book_id, []).append(chap)

        # Chapter lists are dense and start at 1 for each book.
        for book_id, chapters in by_book.items():
            self.assertEqual(
                chapters, list(range(1, len(chapters) + 1)),
                f"Non-contiguous chapters for {book_id}: {chapters}",
            )

        # Genesis and John must appear with plausible chapter counts
        # for this specific translation (Glück 1877).
        self.assertIn('GEN', by_book)
        self.assertGreaterEqual(len(by_book['GEN']), 50)
        self.assertIn('JHN', by_book)
        self.assertEqual(len(by_book['JHN']), 21)


class SwordAPITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='swordtest',
            email='sword@test.local',
            password='x',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_bible_passage_endpoint_glucks(self):
        url = reverse('bible-passage')
        resp = self.client.get(
            url,
            {'passage': 'John 3', 'fileset_id': 'LVSGLU8'},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body['book'], 'JHN')
        self.assertEqual(body['chapter'], 3)
        self.assertEqual(body['format'], 'text')
        self.assertGreaterEqual(len(body['verses']), 30)
        v16 = next(v for v in body['verses'] if v['verse'] == 16)
        self.assertIn('Dievs', v16['text'])

    def test_bible_passage_accepts_abbr_gl8(self):
        url = reverse('bible-passage')
        resp = self.client.get(
            url,
            {'passage': 'Luke 20', 'fileset_id': 'GLU8'},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body['book'], 'LUK')
        self.assertEqual(body['chapter'], 20)
        self.assertEqual(body['format'], 'text')
        self.assertGreater(len(body['verses']), 0)

    def test_translations_listing_includes_glucks(self):
        url = reverse('translation-list')
        resp = self.client.get(
            url,
            {'language_iso': 'lvs'},
        )
        self.assertEqual(resp.status_code, 200)
        abbrs = [t['abbr'] for t in resp.json()['results']]
        self.assertIn('GLU8', abbrs)


class AudioModeRegistryTests(TestCase):
    def test_chapter_audio_fileset_resolves_to_canonical(self):
        from bible.services.sword.registry import (
            canonical_sword_fileset_id,
        )
        self.assertEqual(
            canonical_sword_fileset_id('LVSGLU8C2DA'), 'LVSGLU8'
        )
        self.assertEqual(
            canonical_sword_fileset_id('LVSGLU8C1DA'), 'LVSGLU8'
        )

    def test_audio_mode_for_fileset(self):
        from bible.services.sword.registry import audio_mode_for_fileset
        self.assertEqual(
            audio_mode_for_fileset('LVSGLU8C2DA'), 'chapter'
        )
        self.assertEqual(
            audio_mode_for_fileset('LVSGLU8C1DA'), 'verse'
        )
        # Text fileset id / abbr / unknown ids default to verse.
        self.assertEqual(audio_mode_for_fileset('LVSGLU8'), 'verse')
        self.assertEqual(audio_mode_for_fileset('GLU8'), 'verse')
        self.assertEqual(audio_mode_for_fileset('ENGKJV'), 'verse')
        self.assertEqual(audio_mode_for_fileset(None), 'verse')

    def test_translation_listing_emits_both_audio_filesets(self):
        listing = get_default_sword_client().get_translation_listing()
        glu8 = next(t for t in listing if t['abbr'] == 'GLU8')
        audio_ids = [
            f['id'] for f in glu8['filesets'] if f['type'] == 'audio'
        ]
        self.assertEqual(audio_ids, ['LVSGLU8C1DA', 'LVSGLU8C2DA'])
