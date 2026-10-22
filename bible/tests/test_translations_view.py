"""HTTP-level tests for ``GET /api/v1/bible/translations/``.

Covers the provider error contract (the ``except`` branch of
``TranslationListView.get``, views.py:939-944) and the
``language_iso`` query-param edges. Provider-level filtering
lives in the client/service unit tests.
"""
import pytest
from unittest.mock import patch
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient


TRANSLATIONS_URL = reverse('translation-list')


class FakeProviderError(Exception):
    """Mimics the DBT OpenAPI ``ApiException`` (``exc.status``)."""

    def __init__(self, status_code, message=None):
        super().__init__(message or f'HTTP {status_code}')
        self.status = status_code


@pytest.fixture
def client():
    return APIClient()


@pytest.mark.django_db
@pytest.mark.parametrize(
    'upstream,http_status,error_code',
    [
        (500, status.HTTP_502_BAD_GATEWAY, 'provider_error'),
        (429, status.HTTP_429_TOO_MANY_REQUESTS, 'rate_limited'),
        (404, status.HTTP_404_NOT_FOUND, 'not_found'),
    ],
    ids=['500-provider_error', '429-rate_limited', '404-not_found'],
)
@patch('bible.views.TranslationService.get_live_translations')
def test_exception_returns_provider_error(
    mock_translations, client, upstream, http_status, error_code
):
    """An upstream failure maps to its HTTP status + error_code;
    the exception text — which can carry credentials — never
    reaches the response body."""
    exc = FakeProviderError(
        upstream, 'GET /bibles?key=SECRET failed'
    )
    mock_translations.side_effect = exc

    response = client.get(TRANSLATIONS_URL)

    assert response.status_code == http_status
    body = response.json()
    assert body['error_code'] == error_code
    assert str(exc) not in body['error']
    assert 'SECRET' not in body['error']


@pytest.mark.django_db
@patch('bible.views.TranslationService.get_live_translations')
def test_missing_language_iso_lists_all(
    mock_translations, client
):
    """No ``language_iso`` → service is called with ``None``
    (list everything) and the response is a results list."""
    mock_translations.return_value = ([], False)

    response = client.get(TRANSLATIONS_URL)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()['results'] == []
    mock_translations.assert_called_once_with(None)


@pytest.mark.django_db
@patch('bible.views.TranslationService.get_live_translations')
def test_empty_language_iso(mock_translations, client):
    """``?language_iso=`` reaches the service as ``''`` — falsy,
    so the service treats it like "all" (same as ``None``)."""
    mock_translations.return_value = ([], False)

    response = client.get(
        TRANSLATIONS_URL, {'language_iso': ''}
    )

    assert response.status_code == status.HTTP_200_OK
    mock_translations.assert_called_once_with('')


@pytest.mark.django_db
@patch('bible.views.TranslationService.get_live_translations')
def test_language_iso_case_passed_through(
    mock_translations, client
):
    """``?language_iso=ENG`` reaches the service unchanged — the
    view does no case normalization; the service uppercases for
    the DBT param and lowercases for the local filters, so no
    normalization is needed here."""
    mock_translations.return_value = ([], False)

    response = client.get(
        TRANSLATIONS_URL, {'language_iso': 'ENG'}
    )

    assert response.status_code == status.HTTP_200_OK
    mock_translations.assert_called_once_with('ENG')


@pytest.mark.django_db
@pytest.mark.parametrize(
    'language_iso',
    ['e', 'english', 'en1', 'en;x', 'e n'],
    ids=['too-short', 'too-long', 'digit', 'semicolon', 'space'],
)
def test_invalid_language_iso_rejected(client, language_iso):
    """``language_iso`` must match ``^[a-zA-Z]{2,3}$`` — anything
    else is garbage that would be forwarded to the DBT filter,
    so it 400s before the service is called (translations #4)."""
    response = client.get(
        TRANSLATIONS_URL, {'language_iso': language_iso}
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    body = response.json()
    assert body['error']
    assert body['error_code'] == 'invalid_parameter'


@pytest.mark.django_db
@patch('bible.views.TranslationService.get_live_translations')
def test_response_field_mapping(mock_translations, client):
    """Each result item is reshaped to the public field set —
    extra service-internal keys are dropped and ``iso`` is
    renamed to ``language_iso``."""
    mock_translations.return_value = ([{
        'abbr': 'GLU8',
        'name': 'Latvian Glück 8th edition',
        'language': 'Latvian',
        'iso': 'lvs',
        'filesets': [
            {'id': 'LVSGLU8', 'type': 'text_plain', 'size': 'C'},
        ],
        'text_options': [{'id': 'GLU8:text:1', 'kind': 'text'}],
        'audio_options': [
            {'id': 'GLU8:generated:1', 'kind': 'generated'}
        ],
        'internal_extra': 'dropped',
    }], False)

    response = client.get(
        TRANSLATIONS_URL, {'language_iso': 'lvs'}
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()['results'] == [{
        'abbr': 'GLU8',
        'name': 'Latvian Glück 8th edition',
        'language': 'Latvian',
        'language_iso': 'lvs',
        'filesets': [
            {'id': 'LVSGLU8', 'type': 'text_plain', 'size': 'C'},
        ],
        'text_options': [
            {'id': 'GLU8:text:1', 'kind': 'text'}
        ],
        'audio_options': [
            {'id': 'GLU8:generated:1', 'kind': 'generated'}
        ],
    }]


# --- response caching (S7 / translations #1) --------------------


@pytest.mark.django_db
@patch('bible.views.TranslationService.get_live_translations')
def test_second_request_serves_from_cache(
    mock_translations, client
):
    """The first request caches the response body; the second
    serves it without another service call — the live DBT fetch
    happens at most once per TTL per ``language_iso``."""
    mock_translations.return_value = ([], False)

    first = client.get(TRANSLATIONS_URL)
    second = client.get(TRANSLATIONS_URL)

    assert first.status_code == status.HTTP_200_OK
    assert second.json() == first.json()
    mock_translations.assert_called_once_with(None)
    assert first['Cache-Control'].startswith('public')
    assert second['Cache-Control'].startswith('public')


@pytest.mark.django_db
@patch('bible.views.TranslationService.get_live_translations')
def test_cache_key_includes_language_iso(
    mock_translations, client
):
    """``language_iso`` is part of the cache key (case-folded):
    distinct filters fetch upstream, a repeat hits the cache."""
    mock_translations.return_value = ([], False)

    client.get(TRANSLATIONS_URL, {'language_iso': 'eng'})
    client.get(TRANSLATIONS_URL, {'language_iso': 'lvs'})
    client.get(TRANSLATIONS_URL, {'language_iso': 'ENG'})
    client.get(TRANSLATIONS_URL, {'language_iso': 'lvs'})

    # eng + lvs fetched once each; 'ENG' hits the 'eng' entry
    # (the service normalizes case itself) and the second 'lvs'
    # hits the 'lvs' entry.
    assert mock_translations.call_count == 2


@pytest.mark.django_db
@patch('bible.views.TranslationService.get_live_translations')
def test_degraded_response_never_cached(
    mock_translations, client
):
    """A degraded (DBT-down) body must not be cached server-side
    or downstream — the endpoint recovers as soon as DBT does."""
    mock_translations.side_effect = [
        ([{'abbr': 'GLU8'}], True),
        ([], False),
    ]

    degraded = client.get(TRANSLATIONS_URL)
    recovered = client.get(TRANSLATIONS_URL)

    assert degraded.status_code == status.HTTP_200_OK
    assert degraded.json()['degraded'] is True
    assert degraded['Cache-Control'] == 'no-store'
    assert recovered.json()['degraded'] is False
    assert mock_translations.call_count == 2


# --- DBT-failure contract (translations #3) ---------------------


@pytest.mark.django_db
@patch(
    'bible.services.translation_service.DbtProvider'
    '.translation_listing',
    side_effect=Exception('dbt down'),
)
def test_dbt_failure_returns_degraded_registry_only(
    _mock, client
):
    """The additive contract: a DBT outage yields 200 with the
    bundled registry entries and ``degraded: true`` — not a 502.
    A request filtered by ``lvs`` keeps only the SWORD entry."""
    response = client.get(
        TRANSLATIONS_URL, {'language_iso': 'lvs'}
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body['degraded'] is True
    assert [t['abbr'] for t in body['results']] == ['GLU8']
    assert body['results'][0]['filesets']


@pytest.mark.django_db
@patch(
    'bible.services.translation_service.DbtProvider'
    '.translation_listing',
)
def test_dbt_and_registry_entries_merge_by_abbr(
    mock_listing, client
):
    """``language_iso=eng``: DBT's ESV row and the ``ENGESV_API``
    registry row share ``abbr:ESV`` — they must merge into one
    entry whose filesets combine both sources (deduped by
    ``(id, type)`` so the registry's text+audio pair survives)."""
    mock_listing.return_value = [{
        'abbr': 'ESV',
        'name': 'English Standard Version',
        'language': 'English',
        'iso': 'eng',
        'filesets': {
            'dbp': [
                {
                    'id': 'ENGESV',
                    'type': 'text_plain',
                    'size': 'C',
                },
            ],
        },
    }]

    response = client.get(
        TRANSLATIONS_URL, {'language_iso': 'eng'}
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    esv_rows = [t for t in body['results'] if t['abbr'] == 'ESV']
    assert len(esv_rows) == 1
    fileset_keys = {
        (fs['id'], fs['type']) for fs in esv_rows[0]['filesets']
    }
    assert fileset_keys == {
        ('ENGESV', 'text_plain'),
        ('ENGESV_API', 'text_plain'),
        ('ENGESV_API', 'audio'),
    }


@pytest.mark.django_db
@patch('bible.views.TranslationService.get_live_translations')
def test_malformed_service_row_maps_defensively(
    mock_translations, client
):
    """A service row missing keys degrades to null fields — it
    must not KeyError into a misleading 502 (#6)."""
    mock_translations.return_value = ([{}], False)

    response = client.get(TRANSLATIONS_URL)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()['results'] == [{
        'abbr': None,
        'name': None,
        'language': None,
        'language_iso': None,
        'filesets': [],
        'text_options': [],
        'audio_options': [],
    }]
