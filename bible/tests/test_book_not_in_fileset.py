"""Tests for the ``book_not_in_fileset`` error contract.

``BiblePassageView`` must answer HTTP 404 with
``{"error_code": "book_not_in_fileset", "error": "..."}`` whenever
the resolved fileset does not carry the requested book — for both
text and audio requests — so clients can fall back deterministically.
"""
import pytest
import requests
from unittest.mock import patch
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from bible.services.dbt.client import ApiException


PASSAGE_URL = reverse('bible-passage')
TRANSLATIONS_URL = reverse('translation-list')


@pytest.fixture
def client():
    return APIClient()


def _get(client, params):
    return client.get(PASSAGE_URL, params)


def _assert_not_in_fileset(response):
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()['error_code'] == 'book_not_in_fileset'
    assert response.json()['error']


def _http_error(status_code):
    """A requests.HTTPError carrying a real response object."""
    resp = requests.Response()
    resp.status_code = status_code
    return requests.HTTPError(
        f'HTTP {status_code}', response=resp
    )


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
def test_dbt_404_text_request(mock_get_client, client):
    """DBT ApiException 404 on a text request → 404 body."""
    mock_get_client.return_value.get_verses.side_effect = (
        ApiException(status=404, reason='Not Found')
    )

    response = _get(client, {
        'passage': 'Genesis 1',
        'fileset_id': 'ENGNASN_ET',
    })
    _assert_not_in_fileset(response)


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
def test_dbt_404_audio_request(mock_get_client, client):
    """DBT ApiException 404 on an audio request → 404 body."""
    mock_get_client.return_value.get_verses.side_effect = (
        ApiException(status=404, reason='Not Found')
    )

    response = _get(client, {
        'passage': 'Genesis 1',
        'fileset_id': 'ENGNASN1DA',
        'response_format': 'audio',
    })
    _assert_not_in_fileset(response)


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
def test_dbt_empty_data_returns_404(mock_get_client, client):
    """A 200 DBT response with empty ``data`` → 404 body."""
    mock_get_client.return_value.get_verses.return_value = {
        'data': []
    }

    response = _get(client, {
        'passage': 'Genesis 1',
        'fileset_id': 'ENGNASN_ET',
    })
    _assert_not_in_fileset(response)


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
def test_audio_request_without_path_returns_404(
    mock_get_client, client
):
    """Audio request against rows lacking ``path`` → 404."""
    mock_get_client.return_value.get_verses.return_value = {
        'data': [{'verse_start': 1, 'verse_text': 'In the beginning'}]
    }

    response = _get(client, {
        'passage': 'Genesis 1',
        'fileset_id': 'ENGNASN1DA',
        'response_format': 'audio',
    })
    _assert_not_in_fileset(response)


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
def test_audio_request_with_null_path_returns_404(
    mock_get_client, client
):
    """Audio row whose ``path`` is empty → 404."""
    mock_get_client.return_value.get_verses.return_value = {
        'data': [{'path': None, 'duration': 10}]
    }

    response = _get(client, {
        'passage': 'Genesis 1',
        'fileset_id': 'ENGNASN1DA',
        'response_format': 'audio',
    })
    _assert_not_in_fileset(response)


@pytest.mark.django_db
@patch('bible.services.providers.get_tts_config')
@patch('bible.services.providers.gcs.chapter_audio_exists')
def test_sword_audio_missing_chapter_404(exists, tts_cfg, client):
    """SWORD audio not yet generated → canonical 404 body."""
    tts_cfg.return_value = {'voice_name': 'lv-LV-test'}
    exists.return_value = False

    response = _get(client, {
        'passage': 'Luke 20',
        'fileset_id': 'LVSGLU8C1DA',
        'response_format': 'audio',
    })
    _assert_not_in_fileset(response)
    # Same body shape as the DBT path: no legacy
    # format/audio_url/message fields.
    assert set(response.json()) == {
        'book', 'book_name', 'chapter', 'error', 'error_code'
    }


@pytest.mark.django_db
@patch('bible.services.providers.get_default_sword_client')
def test_sword_text_missing_book_404(mock_sword, client):
    """SWORD text ValueError (book absent) → 404 body."""
    mock_sword.return_value.get_chapter_verses.side_effect = (
        ValueError('Book GEN not present in fileset LVSGLU8')
    )

    response = _get(client, {
        'passage': 'Genesis 1',
        'fileset_id': 'LVSGLU8',
    })
    _assert_not_in_fileset(response)


@pytest.mark.django_db
@patch('bible.services.providers.get_default_sword_client')
def test_sword_text_missing_chapter_404(mock_sword, client):
    """SWORD text ValueError (no verses) → 404 body."""
    mock_sword.return_value.get_chapter_verses.side_effect = (
        ValueError('No verses found for GEN 99 in fileset_id=LVSGLU8')
    )

    response = _get(client, {
        'passage': 'Genesis 99',
        'fileset_id': 'LVSGLU8',
    })
    _assert_not_in_fileset(response)


@pytest.mark.django_db
@patch('bible.services.providers.get_default_sword_client')
def test_sword_infra_error_returns_502(mock_sword, client):
    """A missing module zip (OSError) is a provider failure —
    it surfaces as 502 ``provider_error``, not a silent 200."""
    mock_sword.return_value.get_chapter_verses.side_effect = (
        FileNotFoundError('SWORD module zip not found')
    )

    response = _get(client, {
        'passage': 'Genesis 1',
        'fileset_id': 'LVSGLU8',
    })
    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    assert response.json()['error_code'] == 'provider_error'
    assert 'verses' not in response.json()


@pytest.mark.django_db
@patch('bible.services.providers.get_default_esv_client')
def test_esv_audio_404_returns_not_in_fileset(mock_esv, client):
    """ESV audio HTTPError 404 → 404 body."""
    mock_esv.return_value.get_chapter_audio_url.side_effect = (
        _http_error(404)
    )

    response = _get(client, {
        'passage': 'Genesis 1',
        'fileset_id': 'ENGESV_API',
        'response_format': 'audio',
    })
    _assert_not_in_fileset(response)


@pytest.mark.django_db
@patch('bible.services.providers.get_default_esv_client')
def test_esv_text_404_returns_not_in_fileset(mock_esv, client):
    """ESV text HTTPError 404 → 404 body."""
    mock_esv.return_value.get_chapter_with_headings.side_effect = (
        _http_error(404)
    )

    response = _get(client, {
        'passage': 'Genesis 1',
        'fileset_id': 'ENGESV_API',
    })
    _assert_not_in_fileset(response)


@pytest.mark.django_db
@patch('bible.services.providers.get_default_esv_client')
def test_esv_other_error_returns_502(mock_esv, client):
    """Non-404 ESV failures surface as 502 ``provider_error``."""
    mock_esv.return_value.get_chapter_audio_url.side_effect = (
        _http_error(500)
    )

    response = _get(client, {
        'passage': 'Genesis 1',
        'fileset_id': 'ENGESV_API',
        'response_format': 'audio',
    })
    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    assert response.json()['error_code'] == 'provider_error'


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
def test_dbt_other_error_returns_502(mock_get_client, client):
    """Non-404 failures surface as 502 ``provider_error`` — never
    a silent 200 with an empty ``verses`` list."""
    mock_get_client.return_value.get_verses.side_effect = Exception(
        'boom'
    )

    response = _get(client, {
        'passage': 'Genesis 1',
        'fileset_id': 'ENGNASN_ET',
    })
    assert response.status_code == status.HTTP_502_BAD_GATEWAY
    assert response.json()['error_code'] == 'provider_error'
    assert 'verses' not in response.json()


@pytest.mark.django_db
@patch('bible.services.providers.get_default_dbt_client')
def test_dbt_text_success_unchanged(mock_get_client, client):
    """A normal text response is unaffected by the contract."""
    mock_get_client.return_value.get_verses.return_value = {
        'data': [
            {'verse_start': 1, 'verse_text': 'In the beginning'},
        ]
    }

    response = _get(client, {
        'passage': 'Genesis 1',
        'fileset_id': 'ENGNASN_ET',
    })
    assert response.status_code == status.HTTP_200_OK
    assert response.json()['verses'] == [
        {'verse': 1, 'text': 'In the beginning'}
    ]


@pytest.mark.django_db
@patch('bible.views.TranslationService.get_live_translations')
def test_translations_response_includes_options(
    mock_translations, client
):
    """The translations endpoint exposes the grouped options."""
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
    }]

    response = client.get(
        TRANSLATIONS_URL, {'language_iso': 'lvs'}
    )

    assert response.status_code == status.HTTP_200_OK
    entry = response.json()['results'][0]
    assert entry['text_options'] == [
        {'id': 'GLU8:text:1', 'kind': 'text'}
    ]
    assert entry['audio_options'] == [
        {'id': 'GLU8:generated:1', 'kind': 'generated'}
    ]
    assert entry['filesets'] == [
        {'id': 'LVSGLU8', 'type': 'text_plain', 'size': 'C'}
    ]


@pytest.mark.django_db
@patch('bible.services.providers.get_esv_translation_listing')
@patch('bible.services.providers.get_default_sword_client')
@patch('bible.services.providers.get_default_dbt_client')
def test_service_groups_dbt_filesets(
    mock_dbt, mock_sword, mock_esv
):
    """get_live_translations attaches options to DBT entries."""
    mock_dbt.return_value.get_bibles.return_value = {
        'data': [{
            'abbr': 'LAVNLI',
            'name': 'New Latvian',
            'language': 'Latvian',
            'iso': 'lvs',
            'filesets': {
                'dbp': [
                    {
                        'id': 'LATBSLN1DA',
                        'type': 'audio',
                        'size': 'NT',
                    },
                    {
                        'id': 'LATBSLN1DA-opus16',
                        'type': 'audio',
                        'size': 'NT',
                    },
                ],
                'dbp-vid': [
                    {
                        'id': 'LATBSLV',
                        'type': 'video_stream',
                        'size': 'C',
                    },
                ],
            },
        }]
    }
    mock_sword.return_value.get_translation_listing.return_value = []
    mock_esv.return_value = []

    from bible.services.translation_service import (
        TranslationService,
    )
    translations = TranslationService.get_live_translations('lvs')

    assert len(translations) == 1
    entry = translations[0]
    assert entry['filesets'] == [
        {'id': 'LATBSLN1DA', 'type': 'audio', 'size': 'NT'},
        {
            'id': 'LATBSLN1DA-opus16',
            'type': 'audio',
            'size': 'NT',
        },
    ]
    audio = entry['audio_options']
    assert len(audio) == 1
    assert audio[0]['kind'] == 'audio'
    assert audio[0]['by_testament'] == {
        'NT': {
            'mp3': 'LATBSLN1DA',
            'opus16': 'LATBSLN1DA-opus16',
        }
    }
    assert entry['text_options'] == []


def test_process_translations_passes_codec_and_bitrate():
    """DBT ``codec``/``bitrate`` fields pass through untouched."""
    from bible.services.translation_service import (
        TranslationService,
    )
    translations = [{
        'abbr': 'LAVNLI',
        'filesets': {
            'dbp': [
                {
                    'id': 'LATBSLN1DA',
                    'type': 'audio',
                    'size': 'NT',
                    'codec': 'mp3',
                    'bitrate': '64',
                },
                {
                    'id': 'LATBSLN1DA-opus16',
                    'type': 'audio',
                    'size': 'NT',
                },
            ],
        },
    }]

    processed = TranslationService._process_translations(
        translations
    )

    assert processed[0]['filesets'][0] == {
        'id': 'LATBSLN1DA',
        'type': 'audio',
        'size': 'NT',
        'codec': 'mp3',
        'bitrate': '64',
    }
    # Absent codec/bitrate keys add nothing to the entry.
    assert processed[0]['filesets'][1] == {
        'id': 'LATBSLN1DA-opus16',
        'type': 'audio',
        'size': 'NT',
    }
