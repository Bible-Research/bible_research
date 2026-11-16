import pytest
from unittest.mock import patch
from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient


STRUCTURE_URL = reverse('bible-structure')


class FakeProviderError(Exception):
    """Mimics the DBT OpenAPI ``ApiException`` (``exc.status``)."""

    def __init__(self, status_code):
        super().__init__(f'HTTP {status_code}')
        self.status = status_code


def _dbt_book(book_id, name, chapters, testament, name_short=None):
    return {
        'book_id': book_id,
        'book_id_usfx': book_id[:2],
        'name': name,
        'name_short': name_short or name,
        'testament': testament,
        'chapters': list(range(1, chapters + 1)),
    }


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture(autouse=True)
def clear_caches():
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
def test_structure_requires_fileset_id(client):
    response = client.get(STRUCTURE_URL)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()['error_code'] == 'missing_parameter'


@pytest.mark.django_db
def test_structure_rejects_invalid_fileset_charset(client):
    response = client.get(
        STRUCTURE_URL, {'fileset_id': 'bad id!'}
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
def test_dbt_fileset_returns_own_books(mock_get_client, client):
    """A DBT fileset reads its own bible's book list; non-USFM
    rows (``C01``) and deuterocanon ids are filtered out."""
    mock_get_client.return_value.get_books.return_value = {
        'data': [
            _dbt_book('C01', 'Video', 3, 'OT'),
            _dbt_book('TOB', 'Tobit', 14, 'OT'),
            _dbt_book('JHN', 'Jāņa evanģēlijs', 21, 'NT'),
            _dbt_book('GEN', '1. Mozus grāmata', 50, 'OT'),
        ]
    }

    response = client.get(STRUCTURE_URL, {'fileset_id': 'LAVNLI'})

    assert response.status_code == status.HTTP_200_OK
    books = response.json()['books']
    assert [b['book_id'] for b in books] == ['GEN', 'JHN']
    assert books[0]['name'] == '1. Mozus grāmata'
    assert books[0]['testament'] == 'OT'
    assert books[0]['chapters'] == list(range(1, 51))
    assert response['Vercel-CDN-Cache-Control']
    assert 'public' in response['Cache-Control']
    mock_get_client.return_value.get_books.assert_called_once_with(
        'LAVNLI', verify_content=True
    )


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
def test_engkjv_uses_bundled_english_structure(
    mock_get_client, client
):
    """``ENGKJV`` must not surface DBT's all-caps book names."""
    response = client.get(STRUCTURE_URL, {'fileset_id': 'ENGKJV'})

    assert response.status_code == status.HTTP_200_OK
    books = response.json()['books']
    assert len(books) == 66
    assert books[0]['book_id'] == 'GEN'
    assert books[0]['name'] == 'Genesis'
    assert books[0]['chapters'] == list(range(1, 51))
    assert books[-1]['book_id'] == 'REV'
    mock_get_client.return_value.get_books.assert_not_called()


@pytest.mark.django_db
@patch('bible.services.structure.get_default_dbt_client')
def test_sword_fileset_gets_canonical_names(
    mock_get_client, client
):
    """LVSGLU8 coverage comes from the local module; names are
    overlaid from the canonical Latvian bible (LAVNLI) because
    the module stores English names."""
    mock_get_client.return_value.get_books.return_value = {
        'data': [
            _dbt_book('GEN', '1. Mozus grāmata', 50, 'OT'),
            _dbt_book('JHN', 'Jāņa evanģēlijs', 21, 'NT'),
        ]
    }

    response = client.get(STRUCTURE_URL, {'fileset_id': 'LVSGLU8'})

    assert response.status_code == status.HTTP_200_OK
    books = response.json()['books']
    by_id = {b['book_id']: b for b in books}
    # Real module coverage: 66 books, GEN has 50 chapters.
    assert len(books) == 66
    assert by_id['GEN']['chapters'] == list(range(1, 51))
    # Canonical Latvian names overlaid, not module's 'Genesis'.
    assert by_id['GEN']['name'] == '1. Mozus grāmata'
    assert by_id['JHN']['name'] == 'Jāņa evanģēlijs'
    # Books missing from the canonical map keep module names.
    assert by_id['EXO']['name'] == 'Exodus'


@pytest.mark.django_db
@patch('bible.services.structure.get_default_dbt_client')
def test_sword_canonical_failure_is_degraded_and_no_store(
    mock_get_client, client
):
    """A failed canonical-name overlay degrades to the module's
    English names: the body is flagged and ``no-store`` —
    never edge-marked and never written to the process cache,
    so a repeat request retries the fetch."""
    mock_get_client.return_value.get_books.side_effect = (
        FakeProviderError(500)
    )

    response = client.get(STRUCTURE_URL, {'fileset_id': 'LVSGLU8'})

    assert response.status_code == status.HTTP_200_OK
    assert response.json()['degraded'] is True
    assert response['Cache-Control'] == 'no-store'
    assert 'Vercel-CDN-Cache-Control' not in response
    by_id = {b['book_id']: b for b in response.json()['books']}
    assert by_id['GEN']['name'] == 'Genesis'

    # Not process-cached: the repeat request retries upstream.
    second = client.get(STRUCTURE_URL, {'fileset_id': 'LVSGLU8'})
    assert second.json()['degraded'] is True
    assert (
        mock_get_client.return_value.get_books.call_count == 2
    )


@pytest.mark.django_db
@patch('bible.services.structure.get_default_dbt_client')
def test_provider_without_surface_uses_canonical_fallback(
    mock_get_client, client
):
    """API.Bible filesets have no structure source — the
    canonical bible for their language answers (eng→ENGESV)."""
    mock_get_client.return_value.get_books.return_value = {
        'data': [_dbt_book('GEN', 'Genesis', 50, 'OT')]
    }

    response = client.get(
        STRUCTURE_URL, {'fileset_id': 'ENGNIV_API'}
    )

    assert response.status_code == status.HTTP_200_OK
    books = response.json()['books']
    assert books == [{
        'book_id': 'GEN',
        'name': 'Genesis',
        'name_short': 'Genesis',
        'testament': 'OT',
        'chapters': list(range(1, 51)),
    }]
    mock_get_client.return_value.get_books.assert_called_once_with(
        'ENGESV'
    )


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
@patch('bible.services.structure.get_default_dbt_client')
def test_fileset_bible_resolution_via_listing(
    mock_structure_client, mock_provider_client, client
):
    """A fileset whose prefix is not the bible id
    (``LATLVRN1DA`` → bible ``LAVLVR``) is resolved through the
    cached translations listing."""
    mock_provider_client.return_value.get_books.side_effect = [
        FakeProviderError(404),
        {'data': [_dbt_book('PSA', 'Psalmi', 150, 'OT')]},
    ]
    mock_structure_client.return_value.get_bibles.return_value = {
        'data': [{
            'abbr': 'LAVLVR',
            'filesets': {
                'dbp-prod': [
                    {'id': 'LAVLVR'},
                    {'id': 'LATLVRN1DA'},
                ],
            },
        }]
    }

    response = client.get(
        STRUCTURE_URL, {'fileset_id': 'LATLVRN1DA'}
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()['books'] == [{
        'book_id': 'PSA',
        'name': 'Psalmi',
        'name_short': 'Psalmi',
        'testament': 'OT',
        'chapters': list(range(1, 151)),
    }]
    calls = mock_provider_client.return_value.get_books
    assert calls.call_args_list[0][0][0] == 'LATLVR'
    assert calls.call_args_list[1][0][0] == 'LAVLVR'


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
@patch('bible.services.structure.get_default_dbt_client')
def test_unknown_dbt_fileset_returns_not_found(
    mock_structure_client, mock_provider_client, client
):
    """A fileset id DBT does not know — and that the listing
    scan cannot map to a bible — must surface the upstream 404
    as ``not_found``: the re-raise lives inside the ``except``
    suite, so it can never degrade into a ``NameError`` → 502."""
    mock_provider_client.return_value.get_books.side_effect = (
        FakeProviderError(404)
    )
    mock_structure_client.return_value.get_bibles.return_value = {
        'data': []
    }

    response = client.get(STRUCTURE_URL, {'fileset_id': 'XXXXXX'})

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()['error_code'] == 'not_found'
    assert 'Vercel-CDN-Cache-Control' not in response


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
@patch('bible.services.structure.get_default_dbt_client')
def test_failed_listing_scan_still_returns_not_found(
    mock_structure_client, mock_provider_client, client
):
    """A failing fileset→bible listing scan must not mask the
    original upstream 404 behind a different error."""
    mock_provider_client.return_value.get_books.side_effect = (
        FakeProviderError(404)
    )
    mock_structure_client.return_value.get_bibles.side_effect = (
        FakeProviderError(500)
    )

    response = client.get(STRUCTURE_URL, {'fileset_id': 'XXXXXX'})

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()['error_code'] == 'not_found'


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
def test_provider_failure_follows_error_contract(
    mock_get_client, client
):
    mock_get_client.return_value.get_books.side_effect = (
        FakeProviderError(500)
    )
    response = client.get(STRUCTURE_URL, {'fileset_id': 'ENGESV'})
    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    body = response.json()
    assert body['error_code'] == 'provider_error'
    assert 'Vercel-CDN-Cache-Control' not in response


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
def test_rate_limited_maps_to_429(mock_get_client, client):
    mock_get_client.return_value.get_books.side_effect = (
        FakeProviderError(429)
    )
    response = client.get(STRUCTURE_URL, {'fileset_id': 'ENGESV'})
    assert response.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    assert response.json()['error_code'] == 'rate_limited'


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
def test_repeat_request_is_cached(mock_get_client, client):
    mock_get_client.return_value.get_books.return_value = {
        'data': [_dbt_book('GEN', 'Genesis', 50, 'OT')]
    }
    first = client.get(STRUCTURE_URL, {'fileset_id': 'ENGESV'})
    second = client.get(STRUCTURE_URL, {'fileset_id': 'ENGESV'})
    assert first.status_code == status.HTTP_200_OK
    assert second.json() == first.json()
    mock_get_client.return_value.get_books.assert_called_once()


@pytest.mark.django_db
@patch('bible.services.structure.get_default_dbt_client')
def test_empty_canonical_result_is_not_cached(
    mock_get_client, client
):
    """An upstream ``{'data': []}`` is an anomaly — caching the
    empty canonical list would pin SWORD responses ``degraded``
    for the whole TTL (each retry hits the cached ``[]``). The
    next request must retry upstream and recover."""
    get_books = mock_get_client.return_value.get_books
    get_books.side_effect = [
        {'data': []},
        {'data': [_dbt_book('GEN', '1. Mozus grāmata', 50, 'OT')]},
    ]

    first = client.get(STRUCTURE_URL, {'fileset_id': 'LVSGLU8'})
    assert first.status_code == status.HTTP_200_OK
    assert first.json()['degraded'] is True
    assert first.json()['books'][0]['name'] == 'Genesis'

    second = client.get(STRUCTURE_URL, {'fileset_id': 'LVSGLU8'})
    assert second.status_code == status.HTTP_200_OK
    assert 'degraded' not in second.json()
    by_id = {b['book_id']: b for b in second.json()['books']}
    assert by_id['GEN']['name'] == '1. Mozus grāmata'
    assert get_books.call_count == 2


@pytest.mark.django_db
@patch('bible.services.structure.get_default_dbt_client')
def test_empty_fallback_body_is_not_cached_per_fileset(
    mock_get_client, client
):
    """The per-fileset cache must not pin ``{books: []}``
    either: a canonical-fallback fileset whose first fetch
    comes back empty retries on the next request."""
    get_books = mock_get_client.return_value.get_books
    get_books.side_effect = [
        {'data': []},
        {'data': [_dbt_book('GEN', 'Genesis', 50, 'OT')]},
    ]

    first = client.get(STRUCTURE_URL, {'fileset_id': 'ENGNIV_API'})
    assert first.status_code == status.HTTP_200_OK
    assert first.json()['books'] == []

    second = client.get(STRUCTURE_URL, {'fileset_id': 'ENGNIV_API'})
    assert second.status_code == status.HTTP_200_OK
    books = second.json()['books']
    assert [b['book_id'] for b in books] == ['GEN']
    assert books[0]['name'] == 'Genesis'
    assert get_books.call_count == 2
