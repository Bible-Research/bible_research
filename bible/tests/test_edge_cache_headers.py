"""``Vercel-CDN-Cache-Control`` opt-in tests.

Safe public catalog endpoints emit the header so Vercel's CDN
can cache the proxied ``/api/*`` responses. Licensed verse
payloads, degraded bodies, error responses and user-scoped
endpoints must never emit it — the header steering the edge
must not mark anything that varies per caller or holds
licensed text.
"""
import pytest
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from bible.utils.edge_cache import (
    EDGE_CACHE_CATALOG_DIRECTIVE,
    EDGE_CACHE_HEADER,
    EDGE_CACHE_STATIC_DIRECTIVE,
)

User = get_user_model()

TRANSLATIONS_URL = reverse('translation-list')
LANGUAGES_URL = reverse('language-list')
COPYRIGHT_URL = reverse('bible-copyright')
PASSAGE_URL = reverse('bible-passage')
NOTES_URL = reverse('note-list')


@pytest.fixture
def client():
    return APIClient()


# --- header present on safe catalog endpoints ------------------


@pytest.mark.django_db
@patch('bible.views.TranslationService.get_live_translations')
def test_translations_emit_edge_cache_header(
    mock_translations, client
):
    """``/bible/translations/`` is anonymous-identical catalog
    data — both the fresh and the server-cached response carry
    the catalog directive."""
    mock_translations.return_value = ([], False)

    first = client.get(TRANSLATIONS_URL)
    second = client.get(TRANSLATIONS_URL)

    assert first.status_code == status.HTTP_200_OK
    assert first[EDGE_CACHE_HEADER] == EDGE_CACHE_CATALOG_DIRECTIVE
    assert second[EDGE_CACHE_HEADER] == (
        EDGE_CACHE_CATALOG_DIRECTIVE
    )


@pytest.mark.django_db
@patch('bible.views.get_default_dbt_client')
def test_languages_emit_edge_cache_header(mock_get_client, client):
    """``/bible/languages/`` proxies the DBT language catalog —
    anonymous-identical, so it carries the catalog directive."""
    mock_get_client.return_value.get_languages.return_value = {
        'data': [],
        'meta': {},
    }

    response = client.get(LANGUAGES_URL)

    assert response.status_code == status.HTTP_200_OK
    assert response[EDGE_CACHE_HEADER] == (
        EDGE_CACHE_CATALOG_DIRECTIVE
    )


@pytest.mark.django_db
def test_copyright_emit_edge_cache_header(client):
    """``/bible/copyright/`` is static per bible id — it carries
    the longer static directive. ``LVSGLU8`` answers from the
    SWORD registry, so no upstream call is needed."""
    response = client.get(COPYRIGHT_URL, {'bible_id': 'LVSGLU8'})

    assert response.status_code == status.HTTP_200_OK
    assert response[EDGE_CACHE_HEADER] == (
        EDGE_CACHE_STATIC_DIRECTIVE
    )


# --- header absent where it must be ----------------------------


@pytest.mark.django_db
@patch('bible.views.TranslationService.get_live_translations')
def test_degraded_translations_omit_edge_cache_header(
    mock_translations, client
):
    """A degraded (DBT-down) body is ``no-store`` — the edge
    header must not override that posture."""
    mock_translations.return_value = ([], True)

    response = client.get(TRANSLATIONS_URL)

    assert response.status_code == status.HTTP_200_OK
    assert response['Cache-Control'] == 'no-store'
    assert EDGE_CACHE_HEADER not in response


@pytest.mark.django_db
def test_error_response_omits_edge_cache_header(client):
    """A 400 on a marked endpoint carries no edge header —
    only successful catalog responses are cacheable."""
    response = client.get(
        TRANSLATIONS_URL, {'language_iso': 'e'}
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert EDGE_CACHE_HEADER not in response


@pytest.mark.django_db
@patch('bible.views.get_passage_body')
def test_verse_payload_omits_edge_cache_header(
    mock_passage, client
):
    """``/bible/`` returns licensed verse text — it must never
    be marked for third-party edge caches."""
    mock_passage.return_value = {
        'book': 'JHN',
        'book_name': 'John',
        'chapter': 3,
        'format': 'text',
        'data': [{'verse': 1, 'text': 'In the beginning…'}],
    }

    response = client.get(
        PASSAGE_URL,
        {'passage': 'John 3', 'fileset_id': 'LVSGLU8'},
    )

    assert response.status_code == status.HTTP_200_OK
    assert EDGE_CACHE_HEADER not in response


@pytest.mark.django_db
def test_user_scoped_endpoint_omits_edge_cache_header(client):
    """User-scoped endpoints vary per caller — an authenticated
    ``/notes/`` list must not be marked for the edge cache."""
    user = User.objects.create_user(
        username='edgecachetest', password='x'
    )
    client.force_authenticate(user=user)

    response = client.get(NOTES_URL)

    assert response.status_code == status.HTTP_200_OK
    assert EDGE_CACHE_HEADER not in response
