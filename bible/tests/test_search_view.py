import pytest
from unittest.mock import patch
from rest_framework import status
from rest_framework.test import APIRequestFactory

from bible.services.search_grouping import group_verses_by_book
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


# --- group_by=book tests -------------------------------------------------


@pytest.mark.django_db
def test_invalid_group_by_rejected(factory):
    """group_by=<anything but book> → 400 with error."""
    request = factory.get('/fake-url/', {
        'query': 'love',
        'fileset_id': 'ENGESV',
        'group_by': 'chapter',
    })
    response = BibleSearchView.as_view()(request)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert 'group_by' in response.data['error']


@pytest.mark.django_db
@patch('bible.views.is_sword_fileset', return_value=False)
@patch('bible.views.get_default_dbt_client')
def test_dbt_grouped_search_groups_by_book(
    mock_get_client, mock_is_sword, factory
):
    """group_by=book → groups keyed by book_id in canonical
    order, verses sorted by (chapter, verse_start)."""
    mock_get_client.return_value.search.return_value = {
        'verses': {
            'data': [
                {
                    'book_id': 'JHN',
                    'chapter': 3,
                    'verse_start': 17,
                    'verse_text': 'sent his son',
                },
                {
                    'book_id': 'JHN',
                    'chapter': 3,
                    'verse_start': 16,
                    'verse_text': 'loved the world',
                },
                {
                    'book_id': 'GEN',
                    'chapter': 22,
                    'verse_start': 2,
                    'verse_text': 'your only son',
                },
            ]
        },
        'meta': {'pagination': {'total': 3}},
    }

    request = factory.get('/fake-url/', {
        'query': 'son',
        'fileset_id': 'ENGESV',
        'group_by': 'book',
    })
    response = BibleSearchView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    data = response.data['data']
    assert 'verses' not in data
    groups = data['groups']
    # Canonical order: GEN before JHN though JHN arrived first
    assert [g['book_id'] for g in groups] == ['GEN', 'JHN']
    assert groups[0]['count'] == 1
    assert groups[1]['count'] == 2
    # Verses within a group sorted by verse_start
    assert [
        v['verse_start'] for v in groups[1]['verses']
    ] == [16, 17]
    assert data['meta'] == {'total': 3, 'truncated': False}

    mock_get_client.return_value.search.assert_called_once_with(
        'ENGESV', 'son', limit=5000, books=None,
    )


@pytest.mark.django_db
@patch('bible.views.is_sword_fileset', return_value=False)
@patch('bible.views.get_default_dbt_client')
def test_dbt_grouped_search_sets_truncated(
    mock_get_client, mock_is_sword, factory
):
    """DBT total > fetched items → meta.truncated is True."""
    mock_get_client.return_value.search.side_effect = [
        {
            'verses': {
                'data': [
                    {
                        'book_id': 'JHN',
                        'chapter': 3,
                        'verse_start': 16,
                        'verse_text': 'loved the world',
                    },
                ]
            },
            'meta': {'pagination': {'total': 10}},
        },
        {'verses': {'data': []}},
    ]

    request = factory.get('/fake-url/', {
        'query': 'love',
        'fileset_id': 'ENGESV',
        'group_by': 'book',
    })
    response = BibleSearchView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    meta = response.data['data']['meta']
    assert meta['total'] == 10
    assert meta['truncated'] is True
    assert mock_get_client.return_value.search.call_count == 2


@pytest.mark.django_db
@patch('bible.views.is_sword_fileset', return_value=False)
@patch('bible.views.get_default_dbt_client')
def test_dbt_grouped_search_passes_books(
    mock_get_client, mock_is_sword, factory
):
    """Grouped DBT search forwards the books filter."""
    mock_get_client.return_value.search.return_value = {
        'verses': {'data': []},
        'meta': {'pagination': {'total': 0}},
    }

    request = factory.get('/fake-url/', {
        'query': 'love',
        'fileset_id': 'ENGESV',
        'group_by': 'book',
        'books': 'JHN,ROM',
    })
    response = BibleSearchView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    mock_get_client.return_value.search.assert_called_once_with(
        'ENGESV', 'love', limit=5000, books='JHN,ROM',
    )
    assert response.data['data']['groups'] == []


@pytest.mark.django_db
@patch('bible.views.get_default_esv_client')
def test_esv_grouped_search_fetches_all_pages(
    mock_get_esv_client, factory
):
    """Grouped ESV search fetches every result page and groups
    matches canonically."""
    client = mock_get_esv_client.return_value
    client.search.side_effect = [
        {
            'page': 1,
            'total_results': 3,
            'total_pages': 2,
            'results': [
                {
                    'reference': 'Genesis 1:1',
                    'content': 'In the beginning God',
                },
                {
                    'reference': 'John 3:16',
                    'content': 'For God so loved',
                },
            ],
        },
        {
            'page': 2,
            'total_results': 3,
            'total_pages': 2,
            'results': [
                {
                    'reference': 'John 3:17',
                    'content': 'For God did not send',
                },
            ],
        },
    ]

    request = factory.get('/fake-url/', {
        'query': 'God',
        'fileset_id': 'ENGESV_API',
        'group_by': 'book',
    })
    response = BibleSearchView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    groups = response.data['data']['groups']
    assert [g['book_id'] for g in groups] == ['GEN', 'JHN']
    assert groups[0]['count'] == 1
    assert groups[1]['count'] == 2
    assert response.data['data']['meta'] == {
        'total': 3, 'truncated': False,
    }
    assert client.search.call_count == 2
    client.search.assert_any_call('God', 1, 100)
    client.search.assert_any_call('God', 2, 100)


@pytest.mark.django_db
@patch('bible.views.get_default_esv_client')
def test_esv_grouped_search_truncates_at_max_pages(
    mock_get_esv_client, factory
):
    """ESV total_pages > MAX_ESV_PAGES → meta.truncated True."""
    client = mock_get_esv_client.return_value
    client.search.return_value = {
        'page': 1,
        'total_results': 10000,
        'total_pages': 100,
        'results': [],
    }

    request = factory.get('/fake-url/', {
        'query': 'God',
        'fileset_id': 'ENGESV_API',
        'group_by': 'book',
    })
    response = BibleSearchView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    meta = response.data['data']['meta']
    assert meta['total'] == 10000
    assert meta['truncated'] is True
    # Page 1 + pages 2..30 = 30 calls
    assert client.search.call_count == 30


@pytest.mark.django_db
@patch('bible.views.is_sword_fileset', return_value=True)
@patch('bible.views.get_default_sword_client')
def test_sword_grouped_search(
    mock_get_sword, mock_is_sword, factory
):
    """Grouped SWORD search scans everything; never truncated."""
    mock_sword = mock_get_sword.return_value
    mock_sword.list_chapters.return_value = [
        ('GEN', 1), ('MAT', 1)
    ]
    mock_sword.get_chapter_verses.side_effect = [
        [
            {'verse_start': 1, 'verse_text': 'love one'},
            {'verse_start': 2, 'verse_text': 'no match'},
        ],
        [
            {'verse_start': 5, 'verse_text': 'love wins'},
        ],
    ]

    request = factory.get('/fake-url/', {
        'query': 'love',
        'fileset_id': 'LVSGLU8',
        'group_by': 'book',
    })
    response = BibleSearchView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    groups = response.data['data']['groups']
    assert [g['book_id'] for g in groups] == ['GEN', 'MAT']
    assert groups[0]['count'] == 1
    assert groups[1]['count'] == 1
    assert response.data['data']['meta'] == {
        'total': 2, 'truncated': False,
    }


@pytest.mark.django_db
@patch('bible.views.is_sword_fileset', return_value=True)
@patch('bible.views.get_default_sword_client')
def test_sword_grouped_books_filter(
    mock_get_sword, mock_is_sword, factory
):
    """Grouped SWORD search honors the books filter."""
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
        'group_by': 'book',
        'books': 'MAT',
    })
    response = BibleSearchView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    assert mock_sword.get_chapter_verses.call_count == 1
    groups = response.data['data']['groups']
    assert [g['book_id'] for g in groups] == ['MAT']


@pytest.mark.django_db
@patch('bible.views.is_sword_fileset', return_value=False)
@patch('bible.views.get_default_dbt_client')
def test_non_grouped_search_still_paginated(
    mock_get_client, mock_is_sword, factory
):
    """No group_by → unchanged paginated 'verses' response."""
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
    })
    response = BibleSearchView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    data = response.data['data']
    assert 'groups' not in data
    assert len(data['verses']) == 1
    assert data['meta']['pagination']['total'] == 1


# --- grouped-search edge cases -------------------------------------------


@pytest.mark.django_db
@patch('bible.views.is_sword_fileset', return_value=False)
@patch('bible.views.get_default_dbt_client')
def test_grouped_search_provider_error(
    mock_get_client, mock_is_sword, factory
):
    """Provider client raising in grouped mode → provider error
    fields; raw exception text never reaches the body."""
    mock_get_client.return_value.search.side_effect = (
        Exception("DBT exploded")
    )

    request = factory.get('/fake-url/', {
        'query': 'love',
        'fileset_id': 'ENGESV',
        'group_by': 'book',
    })
    response = BibleSearchView.as_view()(request)

    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    assert response.data['error_code'] == 'provider_error'
    assert 'DBT exploded' not in response.data['error']


@pytest.mark.django_db
@patch('bible.views.is_sword_fileset', return_value=False)
@patch('bible.views.get_default_dbt_client')
def test_grouped_search_ignores_bad_page_param(
    mock_get_client, mock_is_sword, factory
):
    """group_by=book&page=abc → page is never parsed, no 400."""
    mock_get_client.return_value.search.return_value = {
        'verses': {'data': []},
        'meta': {'pagination': {'total': 0}},
    }

    request = factory.get('/fake-url/', {
        'query': 'love',
        'fileset_id': 'ENGESV',
        'group_by': 'book',
        'page': 'abc',
    })
    response = BibleSearchView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK


@pytest.mark.django_db
@patch('bible.views.is_sword_fileset', return_value=False)
@patch('bible.views.get_default_dbt_client')
def test_dbt_grouped_search_uses_reported_page_size(
    mock_get_client, mock_is_sword, factory
):
    """Page 1 reports per_page below the requested limit →
    page 2 is fetched at the effective size so no results
    are skipped."""
    mock_get_client.return_value.search.side_effect = [
        {
            'verses': {
                'data': [
                    {
                        'book_id': 'JHN',
                        'chapter': 3,
                        'verse_start': 16,
                        'verse_text': 'loved the world',
                    },
                    {
                        'book_id': 'JHN',
                        'chapter': 3,
                        'verse_start': 17,
                        'verse_text': 'sent his son',
                    },
                ]
            },
            'meta': {
                'pagination': {'total': 3, 'per_page': 2},
            },
        },
        {
            'verses': {
                'data': [
                    {
                        'book_id': 'JHN',
                        'chapter': 3,
                        'verse_start': 18,
                        'verse_text': 'not perish',
                    },
                ]
            },
            'meta': {
                'pagination': {'total': 3, 'per_page': 2},
            },
        },
    ]

    request = factory.get('/fake-url/', {
        'query': 'love',
        'fileset_id': 'ENGESV',
        'group_by': 'book',
    })
    response = BibleSearchView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    groups = response.data['data']['groups']
    assert len(groups) == 1
    assert groups[0]['count'] == 3
    assert [
        v['verse_start'] for v in groups[0]['verses']
    ] == [16, 17, 18]
    assert response.data['data']['meta'] == {
        'total': 3, 'truncated': False,
    }
    # Follow-up page requested at the effective page size.
    mock_get_client.return_value.search.assert_any_call(
        'ENGESV', 'love', limit=2, page=2, books=None,
    )


def test_unknown_book_id_sorts_last():
    """An unmapped book_id lands in a trailing group."""
    groups = group_verses_by_book([
        {
            'book_id': 'XXX',
            'chapter': 1,
            'verse_start': 1,
            'verse_text': 'odd',
        },
        {
            'book_id': 'GEN',
            'chapter': 1,
            'verse_start': 1,
            'verse_text': 'beginning',
        },
    ])
    assert [g['book_id'] for g in groups] == ['GEN', 'XXX']


def test_verse_missing_book_id_is_skipped():
    """Verses without a string book_id are dropped."""
    groups = group_verses_by_book([
        {
            'book_id': None,
            'chapter': 1,
            'verse_start': 1,
            'verse_text': 'orphan',
        },
        {
            'chapter': 1,
            'verse_start': 2,
            'verse_text': 'no id at all',
        },
        {
            'book_id': 'GEN',
            'chapter': 1,
            'verse_start': 1,
            'verse_text': 'beginning',
        },
    ])
    assert [g['book_id'] for g in groups] == ['GEN']
    assert groups[0]['count'] == 1


def test_duplicate_verses_collapse():
    """Duplicate (book_id, chapter, verse_start) verses dedupe
    to the first occurrence."""
    groups = group_verses_by_book([
        {
            'book_id': 'JHN',
            'chapter': 3,
            'verse_start': 16,
            'verse_text': 'range 16-18 text',
        },
        {
            'book_id': 'JHN',
            'chapter': 3,
            'verse_start': 16,
            'verse_text': 'single verse text',
        },
        {
            'book_id': 'JHN',
            'chapter': 3,
            'verse_start': 17,
            'verse_text': 'next verse',
        },
    ])
    assert len(groups) == 1
    assert groups[0]['count'] == 2
    assert groups[0]['verses'][0]['verse_text'] == (
        'range 16-18 text'
    )


# --- provider error contract tests -------------------------------------


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
