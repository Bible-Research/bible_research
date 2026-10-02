import pytest
from unittest.mock import patch
from rest_framework import status
from rest_framework.test import APIRequestFactory

from bible.views import BibleSearchView


@pytest.fixture
def factory():
    return APIRequestFactory()


@pytest.mark.django_db
def test_missing_query_param(factory):
    """Missing query → 400 with descriptive error."""
    request = factory.get(
        '/fake-url/', {'fileset_id': 'ENGESV'}
    )
    response = BibleSearchView.as_view()(request)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert 'query' in response.data['error']


@pytest.mark.django_db
def test_missing_fileset_id_param(factory):
    """Missing fileset_id → 400 with descriptive error."""
    request = factory.get(
        '/fake-url/', {'query': 'love'}
    )
    response = BibleSearchView.as_view()(request)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert 'fileset_id' in response.data['error']


@pytest.mark.django_db
@patch('bible.views.is_sword_fileset', return_value=False)
@patch('bible.views.get_default_dbt_client')
def test_dbt_search_proxies_with_params(
    mock_get_client, mock_is_sword, factory
):
    """DBT fileset → search() called with limit and books."""
    mock_get_client.return_value.search.return_value = {
        'verses': {
            'data': [
                {
                    'book_id': 'JHN',
                    'chapter': 3,
                    'verse_start': 16,
                    'verse_text': 'For God so loved the world',
                }
            ]
        },
        'meta': {'pagination': {'total': 1}},
    }

    request = factory.get('/fake-url/', {
        'query': 'loved',
        'fileset_id': 'ENGESV',
        'limit': '5',
        'books': 'JHN',
    })
    response = BibleSearchView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    verses = response.data['data']['verses']
    assert len(verses) == 1
    assert verses[0]['book_id'] == 'JHN'
    assert verses[0]['verse_start'] == 16

    mock_get_client.return_value.search.assert_called_once_with(
        'ENGESV', 'loved',
        limit=5, page=1,
        sort_by=None, books='JHN',
    )


@pytest.mark.django_db
@patch('bible.views.is_sword_fileset', return_value=True)
@patch('bible.views.get_default_sword_client')
def test_sword_search_filters_by_query(
    mock_get_sword, mock_is_sword, factory
):
    """SWORD fileset → verses filtered by query substring."""
    mock_sword = mock_get_sword.return_value
    mock_sword.list_chapters.return_value = [
        ('GEN', 1), ('GEN', 2)
    ]
    mock_sword.get_chapter_verses.side_effect = [
        [
            {'verse_start': 1, 'verse_text': 'In the beginning'},
            {'verse_start': 2, 'verse_text': 'darkness over the deep'},
        ],
        [
            {'verse_start': 1, 'verse_text': 'No match here'},
        ],
    ]

    request = factory.get('/fake-url/', {
        'query': 'beginning',
        'fileset_id': 'LVSGLU8',
    })
    response = BibleSearchView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    verses = response.data['data']['verses']
    assert len(verses) == 1
    assert verses[0]['verse_start'] == 1
    assert verses[0]['book_id'] == 'GEN'
    assert verses[0]['chapter'] == 1
    pagination = response.data['data']['meta']['pagination']
    assert pagination['total'] == 1


@pytest.mark.django_db
@patch('bible.views.is_sword_fileset', return_value=True)
@patch('bible.views.get_default_sword_client')
def test_sword_books_filter_limits_scan(
    mock_get_sword, mock_is_sword, factory
):
    """SWORD books filter → only chapters from listed books scanned."""
    mock_sword = mock_get_sword.return_value
    mock_sword.list_chapters.return_value = [
        ('GEN', 1), ('EXO', 1), ('MAT', 1)
    ]
    mock_sword.get_chapter_verses.return_value = [
        {'verse_start': 1, 'verse_text': 'love'}
    ]

    request = factory.get('/fake-url/', {
        'query': 'love',
        'fileset_id': 'LVSGLU8',
        'books': 'GEN,MAT',
    })
    response = BibleSearchView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    assert mock_sword.get_chapter_verses.call_count == 2
    called_books = {
        call.args[1]
        for call in mock_sword.get_chapter_verses.call_args_list
    }
    assert called_books == {'GEN', 'MAT'}


@pytest.mark.django_db
@patch('bible.views.get_default_esv_client')
def test_esv_search_with_engesv_api_fileset(
    mock_get_esv_client, factory
):
    """ENGESV_API fileset → uses ESV API search."""
    mock_esv_client = mock_get_esv_client.return_value
    mock_esv_client.search.return_value = {
        'page': 1,
        'total_results': 2,
        'total_pages': 1,
        'results': [
            {
                'reference': 'John 3:16',
                'content': 'For God so loved the world, that he gave '
                           'his only Son.'
            },
            {
                'reference': 'Romans 8:28',
                'content': 'And we know that for those who love God '
                           'all things work together for good.'
            }
        ]
    }

    request = factory.get('/fake-url/', {
        'query': 'love',
        'fileset_id': 'ENGESV_API',
        'limit': '10',
    })
    response = BibleSearchView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    verses = response.data['data']['verses']
    assert len(verses) == 2

    # Check first verse (John 3:16)
    assert verses[0]['book_id'] == 'JHN'
    assert verses[0]['chapter'] == 3
    assert verses[0]['verse_start'] == 16
    assert 'For God so loved the world' in verses[0]['verse_text']

    # Check second verse (Romans 8:28)
    assert verses[1]['book_id'] == 'ROM'
    assert verses[1]['chapter'] == 8
    assert verses[1]['verse_start'] == 28
    assert 'love God' in verses[1]['verse_text']

    # Check pagination metadata
    pagination = response.data['data']['meta']['pagination']
    assert pagination['total'] == 2
    assert pagination['count'] == 2
    assert pagination['per_page'] == 10
    assert pagination['current_page'] == 1
    assert pagination['total_pages'] == 1

    # Verify ESV client was called correctly
    mock_esv_client.search.assert_called_once_with('love', 1, 10)


@pytest.mark.django_db
@patch('bible.views.get_default_esv_client')
def test_esv_search_handles_verse_ranges(
    mock_get_esv_client, factory
):
    """ESV API search should handle verse ranges by taking first verse."""
    mock_esv_client = mock_get_esv_client.return_value
    mock_esv_client.search.return_value = {
        'page': 1,
        'total_results': 1,
        'total_pages': 1,
        'results': [
            {
                'reference': 'Genesis 1:1-2',
                'content': 'In the beginning, God created the heavens '
                           'and the earth.'
            }
        ]
    }

    request = factory.get('/fake-url/', {
        'query': 'beginning',
        'fileset_id': 'ENGESV_API',
    })
    response = BibleSearchView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    verses = response.data['data']['verses']
    assert len(verses) == 1
    assert verses[0]['book_id'] == 'GEN'
    assert verses[0]['chapter'] == 1
    assert verses[0]['verse_start'] == 1  # Should take first verse of range


@pytest.mark.django_db
@patch('bible.views.get_default_esv_client')
def test_esv_search_handles_books_with_numbers(
    mock_get_esv_client, factory
):
    """ESV API search should handle book names with numbers (e.g., 1 John)."""
    mock_esv_client = mock_get_esv_client.return_value
    mock_esv_client.search.return_value = {
        'page': 1,
        'total_results': 1,
        'total_pages': 1,
        'results': [
            {
                'reference': '1 John 4:8',
                'content': 'Anyone who does not love does not know God, '
                           'because God is love.'
            }
        ]
    }

    request = factory.get('/fake-url/', {
        'query': 'God is love',
        'fileset_id': 'ENGESV_API',
    })
    response = BibleSearchView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    verses = response.data['data']['verses']
    assert len(verses) == 1
    assert verses[0]['book_id'] == '1JN'
    assert verses[0]['chapter'] == 4
    assert verses[0]['verse_start'] == 8


@pytest.mark.django_db
@patch('bible.views.get_default_esv_client')
def test_esv_search_handles_malformed_references(
    mock_get_esv_client, factory
):
    """ESV API search should skip malformed references and continue."""
    mock_esv_client = mock_get_esv_client.return_value
    mock_esv_client.search.return_value = {
        'page': 1,
        'total_results': 2,
        'total_pages': 1,
        'results': [
            {
                'reference': 'John 3:16',
                'content': 'For God so loved the world.'
            },
            {
                'reference': 'Invalid Reference',
                'content': 'This should be skipped.'
            }
        ]
    }

    request = factory.get('/fake-url/', {
        'query': 'love',
        'fileset_id': 'ENGESV_API',
    })
    response = BibleSearchView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    verses = response.data['data']['verses']
    assert len(verses) == 1  # Only valid reference should be included
    assert verses[0]['book_id'] == 'JHN'


class FakeProviderError(Exception):
    """Mimics the DBT OpenAPI ``ApiException`` (``exc.status``)."""

    def __init__(self, status_code):
        super().__init__(f'HTTP {status_code}')
        self.status = status_code


@pytest.mark.django_db
@patch('bible.views.is_sword_fileset', return_value=False)
@patch('bible.views.get_default_dbt_client')
def test_dbt_search_provider_error_no_credential_leak(
    mock_get_client, mock_is_sword, factory
):
    """A DBT search failure surfaces provider error fields — raw
    exception text can carry ``?key=`` credentials and must never
    reach the response body."""
    mock_get_client.return_value.search.side_effect = (
        ConnectionError(
            'Max retries exceeded with url: /search?key=SECRET'
        )
    )

    request = factory.get('/fake-url/', {
        'query': 'loved',
        'fileset_id': 'ENGESV',
    })
    response = BibleSearchView.as_view()(request)

    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    assert response.data['error_code'] == 'provider_error'
    assert 'SECRET' not in response.data['error']


@pytest.mark.django_db
@patch('bible.views.is_sword_fileset', return_value=False)
@patch('bible.views.get_default_dbt_client')
def test_dbt_search_rate_limited(
    mock_get_client, mock_is_sword, factory
):
    """An upstream 429 maps to HTTP 429 + rate_limited."""
    mock_get_client.return_value.search.side_effect = (
        FakeProviderError(429)
    )

    request = factory.get('/fake-url/', {
        'query': 'loved',
        'fileset_id': 'ENGESV',
    })
    response = BibleSearchView.as_view()(request)

    assert response.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    assert response.data['error_code'] == 'rate_limited'


@pytest.mark.django_db
@patch('bible.views.get_default_esv_client')
def test_esv_search_failure_surfaces_provider_error(
    mock_get_esv_client, factory
):
    """An ESV search failure also goes through provider error
    fields instead of embedding str(exc)."""
    mock_get_esv_client.return_value.search.side_effect = (
        Exception("ESV exploded")
    )

    request = factory.get('/fake-url/', {
        'query': 'love',
        'fileset_id': 'ENGESV_API',
    })
    response = BibleSearchView.as_view()(request)

    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    assert response.data['error_code'] == 'provider_error'
    assert 'ESV exploded' not in response.data['error']
