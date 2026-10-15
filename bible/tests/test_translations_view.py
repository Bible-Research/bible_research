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
    mock_translations.return_value = []

    response = client.get(TRANSLATIONS_URL)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()['results'] == []
    mock_translations.assert_called_once_with(None)


@pytest.mark.django_db
@patch('bible.views.TranslationService.get_live_translations')
def test_empty_language_iso(mock_translations, client):
    """``?language_iso=`` reaches the service as ``''`` — falsy,
    so the service treats it like "all" (same as ``None``)."""
    mock_translations.return_value = []

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
    mock_translations.return_value = []

    response = client.get(
        TRANSLATIONS_URL, {'language_iso': 'ENG'}
    )

    assert response.status_code == status.HTTP_200_OK
    mock_translations.assert_called_once_with('ENG')


@pytest.mark.django_db
@patch('bible.views.TranslationService.get_live_translations')
def test_response_field_mapping(mock_translations, client):
    """Each result item is reshaped to the public field set —
    extra service-internal keys are dropped and ``iso`` is
    renamed to ``language_iso``."""
    mock_translations.return_value = [{
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
    }]

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
