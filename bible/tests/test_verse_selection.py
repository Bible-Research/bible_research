"""Tests for verse-level passage retrieval (issue #19).

``GET /api/v1/bible/?passage=John+3:16`` (or a range like
``John 3:16-18``) must return only the requested verses, while
chapter-only requests keep returning the whole chapter.
"""
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from bible.serializers import BiblePassageSerializer
from bible.services.esv.client import ESVClient


def _passage_data(fileset_id='ENGESV', verses=None):
    data = {
        'book': 'JHN',
        'book_name': 'John',
        'chapter': 3,
        'fileset_id': fileset_id,
        'response_format': 'text',
    }
    if verses is not None:
        data['verses'] = verses
    return data


def _dbt_rows(*verse_nums):
    return {
        'data': [
            {'verse_start': n, 'verse_text': f'text {n}'}
            for n in verse_nums
        ]
    }


class VerseSelectionSerializerTests(TestCase):
    """The serializer filters provider output to the requested
    verses for every provider."""

    @patch('bible.serializers.get_default_dbt_client')
    def test_dbt_single_verse(self, mock_get):
        mock_client = MagicMock()
        mock_client.get_verses.return_value = _dbt_rows(
            15, 16, 17
        )
        mock_get.return_value = mock_client

        data = _passage_data(verses=[16])
        result = BiblePassageSerializer(data).to_representation(
            data
        )

        self.assertEqual(
            result['verses'],
            [{'verse': 16, 'text': 'text 16'}],
        )

    @patch('bible.serializers.get_default_dbt_client')
    def test_dbt_verse_range(self, mock_get):
        mock_client = MagicMock()
        mock_client.get_verses.return_value = _dbt_rows(
            *range(15, 21)
        )
        mock_get.return_value = mock_client

        data = _passage_data(verses=[16, 17, 18])
        result = BiblePassageSerializer(data).to_representation(
            data
        )

        self.assertEqual(
            [v['verse'] for v in result['verses']],
            [16, 17, 18],
        )

    @patch('bible.serializers.get_default_dbt_client')
    def test_dbt_discontiguous_selection(self, mock_get):
        """'John 3:16,18' returns exactly verses 16 and 18 even
        when the provider returns the contiguous range."""
        mock_client = MagicMock()
        mock_client.get_verses.return_value = _dbt_rows(
            16, 17, 18
        )
        mock_get.return_value = mock_client

        data = _passage_data(verses=[16, 18])
        result = BiblePassageSerializer(data).to_representation(
            data
        )

        self.assertEqual(
            [v['verse'] for v in result['verses']],
            [16, 18],
        )

    @patch('bible.serializers.get_default_dbt_client')
    def test_dbt_out_of_range_verse_returns_not_found(
        self, mock_get
    ):
        mock_client = MagicMock()
        mock_client.get_verses.return_value = _dbt_rows(
            1, 2, 3
        )
        mock_get.return_value = mock_client

        data = _passage_data(verses=[99])
        result = BiblePassageSerializer(data).to_representation(
            data
        )

        self.assertNotIn('verses', result)
        self.assertEqual(result['error_code'], 'not_found')

    @patch('bible.serializers.get_default_esv_client')
    def test_esv_narrows_upstream_and_filters(self, mock_get):
        mock_client = MagicMock()
        mock_client.get_chapter_with_headings.return_value = {
            'verses': [
                {'verse_start': n, 'verse_text': f'text {n}'}
                for n in range(15, 20)
            ],
            'headings': [
                {'before_verse': 1, 'text': 'Chapter heading'},
                {'before_verse': 17, 'text': 'In-range heading'},
            ],
        }
        mock_get.return_value = mock_client

        data = _passage_data(
            fileset_id='ENGESV_API', verses=[16, 17, 18]
        )
        result = BiblePassageSerializer(data).to_representation(
            data
        )

        mock_client.get_chapter_with_headings\
            .assert_called_once_with(
                'JHN', 3, verse_start=16, verse_end=18
            )
        self.assertEqual(
            [v['verse'] for v in result['verses']],
            [16, 17, 18],
        )
        self.assertEqual(
            result['headings'],
            [{'before_verse': 17, 'text': 'In-range heading'}],
        )

    @patch('bible.serializers.get_default_esv_client')
    def test_esv_chapter_only_call_unchanged(self, mock_get):
        """Without a verse selector the ESV client is called
        exactly as before (no verse kwargs)."""
        mock_client = MagicMock()
        mock_client.get_chapter_with_headings.return_value = {
            'verses': [
                {'verse_start': 1, 'verse_text': 'text 1'}
            ],
            'headings': [],
        }
        mock_get.return_value = mock_client

        data = _passage_data(fileset_id='ENGESV_API')
        result = BiblePassageSerializer(data).to_representation(
            data
        )

        mock_client.get_chapter_with_headings\
            .assert_called_once_with('JHN', 3)
        self.assertEqual(len(result['verses']), 1)

    @patch('bible.serializers.get_default_sword_client')
    def test_sword_narrows_and_filters(self, mock_get):
        mock_client = MagicMock()
        mock_client.get_chapter_verses.return_value = [
            {'verse_start': 16, 'verse_text': 'text 16'},
            {'verse_start': 17, 'verse_text': 'text 17'},
        ]
        mock_get.return_value = mock_client

        data = _passage_data(
            fileset_id='LVSGLU8', verses=[16, 17]
        )
        result = BiblePassageSerializer(data).to_representation(
            data
        )

        mock_client.get_chapter_verses.assert_called_once_with(
            'LVSGLU8', 'JHN', 3,
            verse_start=16, verse_end=17,
        )
        self.assertEqual(
            [v['verse'] for v in result['verses']],
            [16, 17],
        )

    @patch('bible.serializers.get_default_sword_client')
    def test_sword_chapter_only_call_unchanged(self, mock_get):
        mock_client = MagicMock()
        mock_client.get_chapter_verses.return_value = [
            {'verse_start': 1, 'verse_text': 'text 1'},
        ]
        mock_get.return_value = mock_client

        data = _passage_data(fileset_id='LVSGLU8')
        result = BiblePassageSerializer(data).to_representation(
            data
        )

        mock_client.get_chapter_verses.assert_called_once_with(
            'LVSGLU8', 'JHN', 3
        )
        self.assertEqual(len(result['verses']), 1)


class ESVClientVerseRangeTests(TestCase):
    """The ESV client narrows the upstream ``q`` reference when a
    verse range is given."""

    def _client(self):
        client = ESVClient(api_key='test-key')
        mock_resp = MagicMock()
        mock_resp.json.return_value = {'passages': ['']}
        mock_resp.raise_for_status = MagicMock()
        client.session.get = MagicMock(return_value=mock_resp)
        return client

    @override_settings(ESV_KEY='test-key')
    def test_verse_range_in_query(self):
        client = self._client()
        client.get_chapter_with_headings(
            'JHN', 3, verse_start=16, verse_end=18
        )
        params = client.session.get.call_args[1]['params']
        self.assertEqual(params['q'], 'John 3:16-18')

    @override_settings(ESV_KEY='test-key')
    def test_single_verse_in_query(self):
        client = self._client()
        client.get_chapter_with_headings(
            'JHN', 3, verse_start=16, verse_end=16
        )
        params = client.session.get.call_args[1]['params']
        self.assertEqual(params['q'], 'John 3:16')

    @override_settings(ESV_KEY='test-key')
    def test_no_selector_sends_chapter_ref(self):
        client = self._client()
        client.get_chapter_with_headings('JHN', 3)
        params = client.session.get.call_args[1]['params']
        self.assertEqual(params['q'], 'John 3')


class VerseSelectionViewTests(TestCase):
    """The view accepts ``Chapter:Verse`` selectors and rejects
    malformed ones with 400."""

    def _get(self, passage, fileset_id='ENGESV'):
        return APIClient().get(
            '/api/v1/bible/',
            {'passage': passage, 'fileset_id': fileset_id},
        )

    @patch('bible.serializers.get_default_dbt_client')
    def test_single_verse_request(self, mock_get):
        mock_client = MagicMock()
        mock_client.get_verses.return_value = _dbt_rows(
            *range(1, 37)
        )
        mock_get.return_value = mock_client

        response = self._get('John 3:16')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data['verses'],
            [{'verse': 16, 'text': 'text 16'}],
        )

    @patch('bible.serializers.get_default_dbt_client')
    def test_verse_range_request(self, mock_get):
        mock_client = MagicMock()
        mock_client.get_verses.return_value = _dbt_rows(
            *range(1, 37)
        )
        mock_get.return_value = mock_client

        response = self._get('John 3:16-18')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [v['verse'] for v in response.data['verses']],
            [16, 17, 18],
        )

    @patch('bible.serializers.get_default_dbt_client')
    def test_multi_select_request(self, mock_get):
        mock_client = MagicMock()
        mock_client.get_verses.return_value = _dbt_rows(
            *range(1, 37)
        )
        mock_get.return_value = mock_client

        response = self._get('John 3:16-18,20')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [v['verse'] for v in response.data['verses']],
            [16, 17, 18, 20],
        )

    @patch('bible.serializers.get_default_dbt_client')
    def test_chapter_only_request_unchanged(self, mock_get):
        mock_client = MagicMock()
        mock_client.get_verses.return_value = _dbt_rows(1, 2)
        mock_get.return_value = mock_client

        response = self._get('John 3')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [v['verse'] for v in response.data['verses']],
            [1, 2],
        )

    @patch('bible.serializers.get_default_dbt_client')
    def test_out_of_range_verse_returns_404(self, mock_get):
        mock_client = MagicMock()
        mock_client.get_verses.return_value = _dbt_rows(1, 2)
        mock_get.return_value = mock_client

        response = self._get('John 3:99')

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.data['error_code'], 'not_found'
        )

    def test_invalid_verse_selector_returns_400(self):
        for passage in (
            'John 3:abc',
            'John 3:',
            'John 3:18-16',
            'John 3:0',
            'John 3:-4',
        ):
            response = self._get(passage)
            self.assertEqual(
                response.status_code,
                400,
                f"{passage!r} should be a 400, got "
                f"{response.status_code}: {response.data}",
            )
