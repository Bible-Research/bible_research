"""Tests for the ``book_not_in_fileset`` error contract.

``BiblePassageView`` must answer HTTP 404 with
``{"error_code": "book_not_in_fileset", "error": "..."}`` whenever
the resolved fileset does not carry the requested book — for both
text and audio requests — so clients can fall back deterministically.
"""
import pytest
import requests
from unittest.mock import patch
from rest_framework import status
from rest_framework.test import APIRequestFactory

from bible.services.dbt.client import ApiException
from bible.views import BiblePassageView, TranslationListView


@pytest.fixture
def factory():
    return APIRequestFactory()


def _get(factory, params):
    request = factory.get('/fake-url/', params)
    return BiblePassageView.as_view()(request)


def _assert_not_in_fileset(response):
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.data['error_code'] == 'book_not_in_fileset'
    assert response.data['error']


def _http_error(status_code):
    """A requests.HTTPError carrying a real response object."""
    resp = requests.Response()
    resp.status_code = status_code
    return requests.HTTPError(
        f'HTTP {status_code}', response=resp
    )


@pytest.mark.django_db
@patch('bible.serializers.get_default_dbt_client')
def test_dbt_404_text_request(mock_get_client, factory):
    """DBT ApiException 404 on a text request → 404 body."""
    mock_get_client.return_value.get_verses.side_effect = (
        ApiException(status=404, reason='Not Found')
    )

    response = _get(factory, {
        'passage': 'Genesis 1',
        'fileset_id': 'ENGNASN_ET',
    })
    _assert_not_in_fileset(response)


@pytest.mark.django_db
@patch('bible.serializers.get_default_dbt_client')
def test_dbt_404_audio_request(mock_get_client, factory):
    """DBT ApiException 404 on an audio request → 404 body."""
    mock_get_client.return_value.get_verses.side_effect = (
        ApiException(status=404, reason='Not Found')
    )

    response = _get(factory, {
        'passage': 'Genesis 1',
        'fileset_id': 'ENGNASN1DA',
        'response_format': 'audio',
    })
    _assert_not_in_fileset(response)


@pytest.mark.django_db
@patch('bible.serializers.get_default_dbt_client')
def test_dbt_empty_data_returns_404(mock_get_client, factory):
    """A 200 DBT response with empty ``data`` → 404 body."""
    mock_get_client.return_value.get_verses.return_value = {
        'data': []
    }

    response = _get(factory, {
        'passage': 'Genesis 1',
        'fileset_id': 'ENGNASN_ET',
    })
    _assert_not_in_fileset(response)


@pytest.mark.django_db
@patch('bible.serializers.get_default_dbt_client')
def test_audio_request_without_path_returns_404(
    mock_get_client, factory
):
    """Audio request against rows lacking ``path`` → 404."""
    mock_get_client.return_value.get_verses.return_value = {
        'data': [{'verse_start': 1, 'verse_text': 'In the beginning'}]
    }

    response = _get(factory, {
        'passage': 'Genesis 1',
        'fileset_id': 'ENGNASN1DA',
        'response_format': 'audio',
    })
    _assert_not_in_fileset(response)


@pytest.mark.django_db
@patch('bible.serializers.get_default_dbt_client')
def test_audio_request_with_null_path_returns_404(
    mock_get_client, factory
):
    """Audio row whose ``path`` is empty → 404."""
    mock_get_client.return_value.get_verses.return_value = {
        'data': [{'path': None, 'duration': 10}]
    }

    response = _get(factory, {
        'passage': 'Genesis 1',
        'fileset_id': 'ENGNASN1DA',
        'response_format': 'audio',
    })
    _assert_not_in_fileset(response)


@pytest.mark.django_db
@patch('bible.serializers.get_tts_config')
@patch('bible.serializers.gcs.chapter_audio_exists')
def test_sword_audio_missing_chapter_404(exists, tts_cfg, factory):
    """SWORD audio not yet generated → canonical 404 body."""
    tts_cfg.return_value = {'voice_name': 'lv-LV-test'}
    exists.return_value = False

    response = _get(factory, {
        'passage': 'Luke 20',
        'fileset_id': 'LVSGLU8C1DA',
        'response_format': 'audio',
    })
    _assert_not_in_fileset(response)
    # Same body shape as the DBT path: no legacy
    # format/audio_url/message fields.
    assert set(response.data) == {
        'book', 'book_name', 'chapter', 'error', 'error_code'
    }


@pytest.mark.django_db
@patch('bible.serializers.get_default_sword_client')
def test_sword_text_missing_book_404(mock_sword, factory):
    """SWORD text ValueError (book absent) → 404 body."""
    mock_sword.return_value.get_chapter_verses.side_effect = (
        ValueError('Book GEN not present in fileset LVSGLU8')
    )

    response = _get(factory, {
        'passage': 'Genesis 1',
        'fileset_id': 'LVSGLU8',
    })
    _assert_not_in_fileset(response)


@pytest.mark.django_db
@patch('bible.serializers.get_default_sword_client')
def test_sword_text_missing_chapter_404(mock_sword, factory):
    """SWORD text ValueError (no verses) → 404 body."""
    mock_sword.return_value.get_chapter_verses.side_effect = (
        ValueError('No verses found for GEN 99 in fileset_id=LVSGLU8')
    )

    response = _get(factory, {
        'passage': 'Genesis 99',
        'fileset_id': 'LVSGLU8',
    })
    _assert_not_in_fileset(response)


@pytest.mark.django_db
@patch('bible.serializers.get_default_sword_client')
def test_sword_infra_error_keeps_200_message(mock_sword, factory):
    """A missing module zip (OSError) stays on the legacy path."""
    mock_sword.return_value.get_chapter_verses.side_effect = (
        FileNotFoundError('SWORD module zip not found')
    )

    response = _get(factory, {
        'passage': 'Genesis 1',
        'fileset_id': 'LVSGLU8',
    })
    assert response.status_code == status.HTTP_200_OK
    assert response.data['verses'] == []
    assert 'book_not_in_fileset' != response.data.get('error_code')


@pytest.mark.django_db
@patch('bible.serializers.get_default_esv_client')
def test_esv_audio_404_returns_not_in_fileset(mock_esv, factory):
    """ESV audio HTTPError 404 → 404 body."""
    mock_esv.return_value.get_chapter_audio_url.side_effect = (
        _http_error(404)
    )

    response = _get(factory, {
        'passage': 'Genesis 1',
        'fileset_id': 'ENGESV_API',
        'response_format': 'audio',
    })
    _assert_not_in_fileset(response)


@pytest.mark.django_db
@patch('bible.serializers.get_default_esv_client')
def test_esv_text_404_returns_not_in_fileset(mock_esv, factory):
    """ESV text HTTPError 404 → 404 body."""
    mock_esv.return_value.get_chapter_with_headings.side_effect = (
        _http_error(404)
    )

    response = _get(factory, {
        'passage': 'Genesis 1',
        'fileset_id': 'ENGESV_API',
    })
    _assert_not_in_fileset(response)


@pytest.mark.django_db
@patch('bible.serializers.get_default_esv_client')
def test_esv_other_error_keeps_200_message(mock_esv, factory):
    """Non-404 ESV failures keep the legacy 200 + message."""
    mock_esv.return_value.get_chapter_audio_url.side_effect = (
        _http_error(500)
    )

    response = _get(factory, {
        'passage': 'Genesis 1',
        'fileset_id': 'ENGESV_API',
        'response_format': 'audio',
    })
    assert response.status_code == status.HTTP_200_OK
    assert 'book_not_in_fileset' != response.data.get('error_code')


@pytest.mark.django_db
@patch('bible.serializers.get_default_dbt_client')
def test_dbt_other_error_keeps_200_message(mock_get_client, factory):
    """Non-404 failures keep the legacy 200 + message contract."""
    mock_get_client.return_value.get_verses.side_effect = Exception(
        'boom'
    )

    response = _get(factory, {
        'passage': 'Genesis 1',
        'fileset_id': 'ENGNASN_ET',
    })
    assert response.status_code == status.HTTP_200_OK
    assert response.data['verses'] == []
    assert 'book_not_in_fileset' != response.data.get('error_code')


@pytest.mark.django_db
@patch('bible.serializers.get_default_dbt_client')
def test_dbt_text_success_unchanged(mock_get_client, factory):
    """A normal text response is unaffected by the contract."""
    mock_get_client.return_value.get_verses.return_value = {
        'data': [
            {'verse_start': 1, 'verse_text': 'In the beginning'},
        ]
    }

    response = _get(factory, {
        'passage': 'Genesis 1',
        'fileset_id': 'ENGNASN_ET',
    })
    assert response.status_code == status.HTTP_200_OK
    assert response.data['verses'] == [
        {'verse': 1, 'text': 'In the beginning'}
    ]


@pytest.mark.django_db
@patch('bible.views.TranslationService.get_live_translations')
def test_translations_response_includes_options(
    mock_translations, factory
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

    request = factory.get('/fake-url/', {'language_iso': 'lvs'})
    response = TranslationListView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    entry = response.data['results'][0]
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
@patch('bible.services.translation_service.get_esv_translation_listing')
@patch('bible.services.translation_service.get_default_sword_client')
@patch('bible.services.translation_service.get_default_dbt_client')
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
